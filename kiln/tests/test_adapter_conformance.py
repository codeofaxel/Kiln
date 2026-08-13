"""Backstop for scripts/audit_adapter_conformance.py.

The gate exists because a contract field one backend populates and the
others leave empty is invisible to the type system and to every test —
each adapter's tests are written against that adapter's own behaviour, so
nothing ever lines the eight up side by side.  That blind spot has cost
twice: the print-outcome hook wired into one adapter ("seven of the eight
reported no prints at all"), and ``state_age_seconds``, which the stale
-readings warning depends on.

These tests prove the gate CAN fail, one failure mode at a time.  A gate
that cannot fail is theatre, and this one is a ledger — the easiest kind
to quietly render inert.
"""

from __future__ import annotations

import importlib.util
import pathlib
import textwrap

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "audit_adapter_conformance.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("audit_adapter_conformance", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate():
    return _load()


ADAPTER = textwrap.dedent(
    '''
    class FakeAdapter(PrinterAdapter):
        def get_state(self):
            return PrinterState(connected=True, state="idle",
                                tool_temp_actual=1.0, tool_temp_target=1.0,
                                bed_temp_actual=1.0, bed_temp_target=1.0)
        def get_job(self):
            return JobProgress(file_name="a", completion=0.0,
                               print_time_seconds=1, print_time_left_seconds=1)
        def _start_print_impl(self, f, **k):
            return PrintResult(success=True, message="ok")
    '''
)

FULL_ROWS = {
    "PrinterState.connected": "provided",
    "PrinterState.state": "provided",
    "PrinterState.tool_temp_actual": "provided",
    "PrinterState.tool_temp_target": "provided",
    "PrinterState.bed_temp_actual": "provided",
    "PrinterState.bed_temp_target": "provided",
    "PrinterState.print_error": {"status": "not_in_protocol", "why": "no code"},
    "PrinterState.state_age_seconds": {"status": "not_in_protocol", "why": "fresh"},
    "PrinterState.last_job_result": {"status": "not_in_protocol", "why": "none"},
    "JobProgress.file_name": "provided",
    "JobProgress.completion": "provided",
    "JobProgress.print_time_seconds": "provided",
    "JobProgress.print_time_left_seconds": "provided",
    "PrintResult.success": "provided",
    "PrintResult.message": "provided",
    "PrintResult.job_id": {"status": "not_in_protocol", "why": "nobody sets it"},
}


def _rig(gate, tmp_path, monkeypatch, rows, *, source=ADAPTER, name="fake"):
    """Point the gate at a one-adapter tree with the given ledger rows."""
    adapters = tmp_path / "printers"
    adapters.mkdir()
    (adapters / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(gate, "ADAPTER_DIR", adapters)

    ledger = tmp_path / "ledger.yaml"
    import yaml

    ledger.write_text(yaml.safe_dump({"adapters": rows}), encoding="utf-8")
    monkeypatch.setattr(gate, "LEDGER", ledger)
    return gate.audit()


def test_the_real_tree_is_clean(gate):
    """The shipped ledger matches the shipped adapters."""
    assert gate.audit() == []


def test_a_declared_field_that_stopped_being_set_is_caught(gate, tmp_path, monkeypatch):
    """THE failure this gate is for: a backend quietly stops reporting
    something and nothing goes red."""
    rows = dict(FULL_ROWS)
    rows["PrinterState.state_age_seconds"] = "provided"  # the adapter never sets it
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["regression"]
    assert "state_age_seconds" in found[0]["detail"]


def test_a_new_adapter_with_no_rows_fails_at_conception(gate, tmp_path, monkeypatch):
    found = _rig(gate, tmp_path, monkeypatch, {})
    assert [f["kind"] for f in found] == ["unclassified_adapter"]


def test_an_absence_without_a_reason_is_refused(gate, tmp_path, monkeypatch):
    rows = dict(FULL_ROWS)
    rows["PrinterState.print_error"] = {"status": "not_in_protocol"}
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["missing_reason"]


def test_an_absence_that_is_no_longer_absent_is_caught(gate, tmp_path, monkeypatch):
    """A protocol claim that reality has overtaken must be promoted, not
    left standing — otherwise the ledger teaches the wrong thing."""
    rows = dict(FULL_ROWS)
    rows["JobProgress.completion"] = {"status": "not_in_protocol", "why": "stale"}
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["stale_absence"]


def test_a_deferred_row_expires(gate, tmp_path, monkeypatch):
    rows = dict(FULL_ROWS)
    rows["PrinterState.print_error"] = {
        "status": "deferred", "why": "unverified", "by": "2020-01-01",
    }
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["past_due"]


def test_a_deferral_whose_gap_got_closed_is_caught(gate, tmp_path, monkeypatch):
    """Found by using the gate: closing a deferred gap left the row
    standing, and the gate said clean.  A stale deferral keeps excusing an
    absence that no longer exists, and leaves the field unprotected
    against regressing back — which is the one thing the ledger is for."""
    rows = dict(FULL_ROWS)
    rows["JobProgress.completion"] = {
        "status": "deferred", "why": "was a gap", "by": "2099-01-01",
    }
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["stale_deferral"]


def test_a_row_for_a_field_that_no_longer_exists_is_caught(gate, tmp_path, monkeypatch):
    rows = dict(FULL_ROWS)
    rows["PrinterState.invented_field"] = "provided"
    found = _rig(gate, tmp_path, monkeypatch, {"fake": {"fields": rows}})
    assert [f["kind"] for f in found] == ["stale_row"]


def test_a_row_for_an_adapter_that_no_longer_exists_is_caught(gate, tmp_path, monkeypatch):
    found = _rig(
        gate, tmp_path, monkeypatch,
        {"fake": {"fields": FULL_ROWS}, "retired": {"fields": FULL_ROWS}},
    )
    assert [f["kind"] for f in found] == ["stale_adapter"]


def test_a_delegation_claim_is_verified_not_trusted(gate, tmp_path, monkeypatch):
    """Creality really does forward everything, which is why the status
    exists — but an adapter that builds its own objects must not be able
    to escape the ledger by claiming it doesn't."""
    found = _rig(
        gate, tmp_path, monkeypatch,
        {"fake": {"delegates_to": "somewhere"}},   # but ADAPTER constructs its own
    )
    assert [f["kind"] for f in found] == ["delegation_claim_unsupported"]


def test_a_real_wrapper_passes_on_its_delegation_claim(gate, tmp_path, monkeypatch):
    wrapper = textwrap.dedent(
        """
        class WrapAdapter(PrinterAdapter):
            def get_state(self):
                return self._backend.get_state()
            def get_job(self):
                return self._backend.get_job()
        """
    )
    found = _rig(
        gate, tmp_path, monkeypatch,
        {"fake": {"delegates_to": "moonraker"}}, source=wrapper,
    )
    assert found == []


def test_serials_missing_elapsed_time_is_declared(gate):
    """A direct-USB print can never report its own duration — M27 gives
    SD-card byte progress, not a clock.  That is a real bound on
    print-hours capture, and it has to be written down rather than
    discovered again later as a surprise zero."""
    ledger = gate.load_ledger()["adapters"]["serial_adapter"]["fields"]
    row = ledger["JobProgress.print_time_seconds"]
    assert row["status"] == "not_in_protocol"
    assert "elapsed" in row["why"] or "duration" in row["why"]


def test_every_watched_field_names_its_consequence(gate):
    """WATCHED is the scoping decision that keeps this ledger readable.
    A field watched without a stated consequence invites the next person
    to add all thirty and drown the signal."""
    for field, why in gate.WATCHED.items():
        assert why.strip(), f"{field} is watched with no reason given"
        contract, _, name = field.partition(".")
        assert contract in gate.CONTRACTS
        assert name in gate.contract_fields()[contract]
