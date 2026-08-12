"""Two printers of one brand are two printers.

Every adapter answers ``name`` with its FAMILY — ``BambuAdapter.name`` is
``"bambu"`` for every Bambu ever plugged in, and the other seven do the same
with their own word.  The outcome lifecycle keys on that string: the
previous-state table that detects a terminal transition, the idempotency
ledger that stops one ending being recorded twice, the cancel-intent table
that tells a cancel from a finish, and the ``printer_name`` written onto the
outcome row itself.

On a one-printer bench that is invisible.  On two Bambus it means the
machines are the same machine to all four: one printer's transition is
consumed by the other's poll, one ending suppresses the other's, a cancel
registered against one resolves the other's row, and the rows that survive
cannot say which machine made the part.

``progress_motion.observation_key`` already refused this trade — it keys on
the adapter INSTANCE and its docstring names this exact hazard — but it only
ever governed the motion samples.  The lifecycle kept using the family word.

The registry is what knows better: a printer is registered under the name its
owner chose (``"garage"``, ``"a1"``), and that name is what every other
surface already attributes prints to.  It just never reached the adapter.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiln import auto_record_hook as hook
from kiln.printers import progress_motion as pm
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _reset_lifecycle_state():
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()
    yield
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch):
    """Count the outcome rows instead of writing them."""
    calls: list[dict] = []

    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: calls.append(kw) or {"success": True},
    )
    return calls


def _bambu(monkeypatch):
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    return BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )


def _push(adapter, gcode_state: str, *, job: str) -> None:
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": gcode_state,
            "subtask_name": job,
            "gcode_file": f"/sdcard/{job}.3mf",
            "print_error": 0,
        }
    }
    adapter._on_message(
        None, None, SimpleNamespace(payload=json.dumps(payload).encode())
    )


def _two_registered_bambus(monkeypatch):
    """The bench this is about: two Bambus, registered under their own names."""
    registry = PrinterRegistry()
    garage, workshop = _bambu(monkeypatch), _bambu(monkeypatch)
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    return garage, workshop


def test_each_bambu_records_its_own_ending(_no_db_writes, monkeypatch):
    """One print each, and both must be recorded — under their own names.

    The idempotency ledger is keyed ``(printer_name, job_id)``.  With both
    machines answering to "bambu", printing the same file on both means the
    second ending is swallowed as a replay of the first.  Printing the same
    file on two printers is not an exotic case; it is what a second printer
    is FOR.
    """
    garage, workshop = _two_registered_bambus(monkeypatch)

    for printer in (garage, workshop):
        _push(printer, "RUNNING", job="bracket")
    for printer in (garage, workshop):
        _push(printer, "FINISH", job="bracket")

    assert sorted(c["printer_name"] for c in _no_db_writes) == ["garage", "workshop"]


def test_each_ending_is_attributed_to_the_machine_that_made_it(
    _no_db_writes, monkeypatch
):
    """Two different files, so nothing is suppressed — only misfiled.

    Both rows survived even before this, because the job ids differed.  What
    they could not say is WHICH machine made the part, and a learning loop
    that cannot tell two printers apart averages their calibration, their
    failure modes and their nozzle wear into one fictional machine.
    """
    garage, workshop = _two_registered_bambus(monkeypatch)

    _push(garage, "RUNNING", job="bracket")
    _push(workshop, "RUNNING", job="spool-holder")
    _push(garage, "FINISH", job="bracket")
    _push(workshop, "FINISH", job="spool-holder")

    by_job = {c["job_id"]: c["printer_name"] for c in _no_db_writes}
    assert by_job == {"bracket": "garage", "spool-holder": "workshop"}


def test_a_cancel_is_spent_on_the_printer_it_was_meant_for(
    _no_db_writes, monkeypatch
):
    """Cancel intent is registered per name and consumed by the FIRST ending.

    A cancel is how the loop tells "ended without completing" from "finished".
    Filed under a key both machines answer to, one machine's cancel is spent
    resolving whichever ending arrives first — so the outcomes swap: the print
    that finished is recorded cancelled, and the one the user actually
    cancelled is recorded a success.  Measured before the fix, in that order.
    """
    garage, workshop = _two_registered_bambus(monkeypatch)

    _push(garage, "RUNNING", job="bracket")
    _push(workshop, "RUNNING", job="spool-holder")
    # The user cancels the WORKSHOP print; the garage print finishes normally.
    hook.register_cancel_intent("workshop")
    _push(garage, "IDLE", job="bracket")
    _push(workshop, "IDLE", job="spool-holder")

    by_printer = {c["printer_name"]: c["outcome"] for c in _no_db_writes}
    assert by_printer == {"garage": "success", "workshop": "cancelled"}


def test_a_cancelled_print_is_not_recorded_as_a_success(
    _no_db_writes, monkeypatch
):
    """ONE printer, free tier, nothing exotic — and the cancel never landed.

    ``cancel_print`` files intent under the name the registry knows
    (``_resolve_effective_printer_name`` — "default" on a stock install) while
    the hook consumed it under ``adapter.name`` — "bambu".  The two never met,
    so the flag did nothing and the idle that follows a cancel was inferred to
    be a natural finish.  The comment at the ``cancel_print`` call site states
    the opposite outcome as settled fact, which is how it went unnoticed.
    """
    registry = PrinterRegistry()
    printer = _bambu(monkeypatch)
    registry.register("default", printer)

    _push(printer, "RUNNING", job="bracket")
    hook.register_cancel_intent("default")   # exactly what cancel_print does
    _push(printer, "IDLE", job="bracket")

    assert [(c["printer_name"], c["outcome"]) for c in _no_db_writes] == [
        ("default", "cancelled")
    ]


def test_an_unregistered_adapter_still_reports_under_its_family_name(
    _no_db_writes, monkeypatch
):
    """No registry, no stamp — and a print must still be recorded.

    Adapters get built directly in scripts and in tests.  Falling back to the
    family name is what they reported before, and an outcome filed under a
    coarse name beats an outcome nobody files.
    """
    printer = _bambu(monkeypatch)   # deliberately never registered

    _push(printer, "RUNNING", job="bracket")
    _push(printer, "FINISH", job="bracket")

    assert [c["printer_name"] for c in _no_db_writes] == ["bambu"]
