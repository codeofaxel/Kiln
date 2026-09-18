"""The print's end checks the charge against the wire, and the start says one line.

Pinned here:
  - the start template holds the planned count it charged;
  - on the MQTT backend, tray changes during a print Kiln started are counted
    against that charge instead of being reported again, and the print's end
    hands both numbers to the bridge, once;
  - the bridge's blade consult answers only when the blade wants attention,
    with the word, the line, the confidence and the next step -- and never
    raises;
  - the start door and the pre-flight carry that one line, advisory only.
"""

from __future__ import annotations

import json
import sys
import types
from unittest import mock

import pytest

from kiln import _pro_cutter_bridge as bridge
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import PrintResult

from .test_filament_handling import bambu  # noqa: F401

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name


def _lean_start(bambu, monkeypatch):
    import kiln.printers.base as base

    bambu._printer_model = "bambu_a1"
    monkeypatch.setattr("kiln.printers.print_gate.run_adapter_gate", lambda *a, **k: None)
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
    monkeypatch.setattr(bambu, "_start_print_impl", lambda file_name, **kw: PrintResult(success=True, message="ok"))


def _push(adapter: BambuAdapter, **fields) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _engaged(monkeypatch, bambu):
    from kiln.printers.engagement import Engagement, machine_id

    monkeypatch.setattr(
        "kiln.printers.engagement.current",
        lambda: Engagement(machine=machine_id(bambu), label="", job=None, since=0.0, reason="started"),
    )


class TestReconciliation:
    def test_the_start_holds_what_it_charged(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_print_cuts", lambda name, file, **kw: 9)
        bambu.start_print("two-colour.gcode.3mf")
        assert bambu._cutter_print == {"file": "two-colour.gcode.3mf", "planned": 9, "observed": 0}

    def test_tray_changes_during_kilns_print_are_counted_not_reported(self, bambu, monkeypatch):
        _engaged(monkeypatch, bambu)
        reported = []
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_observed_switch", lambda *a, **k: reported.append(1))
        _push(bambu, ams={"tray_now": "0"})  # the tray the print starts on
        bambu._cutter_print = {"file": "f.3mf", "planned": 3, "observed": 0}
        _push(bambu, ams={"tray_now": "1"})
        _push(bambu, ams={"tray_now": "2"})
        _push(bambu, ams={"tray_now": "2"})  # not a change
        assert reported == []
        assert bambu._cutter_print["observed"] == 2

    def test_the_prints_end_hands_both_numbers_over_once(self, bambu, monkeypatch):
        sent = []
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_print_reconciliation", lambda name, **kw: sent.append((name, kw)))
        bambu._printer_model = "bambu_a1"
        bambu._cutter_print = {"file": "f.3mf", "planned": 3, "observed": 2}
        bambu._reconcile_cutter_print("job-1")
        bambu._reconcile_cutter_print("job-1")  # the record is spent
        assert sent == [("bambu", {"job": "f.3mf", "planned": 3, "observed": 2, "printer_model": "bambu_a1"})]

    def test_a_print_whose_file_could_not_be_read_reconciles_nothing(self, bambu, monkeypatch):
        sent = []
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_print_reconciliation", lambda name, **kw: sent.append(1))
        bambu._cutter_print = {"file": "f.3mf", "planned": None, "observed": 4}
        bambu._reconcile_cutter_print("job-1")
        assert sent == []

    def test_the_bridge_sends_the_pair_under_the_jobs_key(self, monkeypatch):
        calls = []
        pkg = types.ModuleType("kiln_pro")
        sub = types.ModuleType("kiln_pro.cutter_intelligence")
        mod = types.ModuleType("kiln_pro.cutter_intelligence.counter")
        mod.record_cut_events = lambda pid, **payload: calls.append((pid, payload))
        monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence", sub)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence.counter", mod)
        monkeypatch.setattr(bridge, "_declared_model", lambda name: None)
        bridge.record_print_reconciliation("a1", job="f.3mf", planned=3, observed=2)
        (pid, payload), = calls
        assert pid == "a1"
        assert payload["reconcile_job"] == "f.3mf" and payload["reconcile_planned"] == 3 and payload["reconcile_observed"] == 2
        assert payload["dedupe_key"] == "reconcile:a1:f.3mf"


class TestTheConsult:
    def _status(self, monkeypatch, word: str, **extra):
        pkg = types.ModuleType("kiln_pro")
        sub = types.ModuleType("kiln_pro.cutter_intelligence")
        cat = types.ModuleType("kiln_pro.cutter_intelligence.catalogue")
        faults = types.ModuleType("kiln_pro.cutter_intelligence.faults")
        resolver = types.ModuleType("kiln_pro.cutter_intelligence.store_resolver")
        wear = types.ModuleType("kiln_pro.cutter_intelligence.wear")
        cat.row_for_model = lambda model: object()
        faults.faults_for = lambda name, supplied=None: []

        class _Backend:
            def get(self, pid):
                return None

        resolver.resolve_backend = lambda tool_name: (_Backend(), None)

        class _Status:
            def to_dict(self):
                return {"word": word, "why": "4,100 cuts counted, 82% of the maker's 5000 to 7000.", "confidence": "from_the_maker", "next_step": "order a spare", **extra}

        wear.cutter_status = lambda *a, **k: _Status()
        for name, m in (("kiln_pro", pkg), ("kiln_pro.cutter_intelligence", sub), ("kiln_pro.cutter_intelligence.catalogue", cat),
                        ("kiln_pro.cutter_intelligence.faults", faults), ("kiln_pro.cutter_intelligence.store_resolver", resolver),
                        ("kiln_pro.cutter_intelligence.wear", wear)):
            monkeypatch.setitem(sys.modules, name, m)
        monkeypatch.setattr(bridge, "_declared_model", lambda name: "bambu_a1")

    @pytest.mark.parametrize("word", ["approaching", "due", "overdue", "check_now"])
    def test_a_blade_that_wants_attention_gets_one_line(self, monkeypatch, word):
        self._status(monkeypatch, word)
        out = bridge.consult_blade("a1")
        assert out == {"word": word, "line": "4,100 cuts counted, 82% of the maker's 5000 to 7000.", "confidence": "from_the_maker", "next_step": "order a spare"}

    @pytest.mark.parametrize("word", ["ok", "unknown", "no_interval", "not_applicable", "gated"])
    def test_anything_else_says_nothing(self, monkeypatch, word):
        self._status(monkeypatch, word)
        assert bridge.consult_blade("a1") is None

    def test_a_crashing_consult_says_nothing_and_never_raises(self, monkeypatch):
        self._status(monkeypatch, "due")
        sys.modules["kiln_pro.cutter_intelligence.wear"].cutter_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        assert bridge.consult_blade("a1") is None

    def test_without_kiln_pro_the_hosted_door_is_asked_with_a_short_timeout(self, monkeypatch):
        for name in ("kiln_pro", "kiln_pro.cutter_intelligence", "kiln_pro.cutter_intelligence.catalogue"):
            monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.setattr(bridge, "_declared_model", lambda name: "bambu_a1")
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        monkeypatch.setattr(bridge, "recent_faults_for", lambda name, days=30: [])
        import kiln.server as server

        seen = {}

        def fake_call(tool, _timeout=30.0, **kwargs):
            seen.update({"tool": tool, "timeout": _timeout, **kwargs})
            return {"success": True, "word": "due", "why": "due line", "confidence": "verified", "next_step": "swap it"}

        monkeypatch.setattr(server, "_pro_api_call", fake_call)
        out = bridge.consult_blade("a1")
        assert seen["tool"] == "cutter_wear_status" and seen["timeout"] == bridge._CONSULT_TIMEOUT_S
        assert seen["printer_id"] == "a1" and seen["printer_model"] == "bambu_a1"
        assert out["word"] == "due" and out["confidence"] == "verified"


class TestTheDoors:
    """The start door and the pre-flight, driven the way their own tests drive them."""

    def test_the_start_door_carries_the_line(self, monkeypatch):
        import os
        from unittest.mock import patch

        import kiln.server as srv

        from .test_every_start_says_so import _two_printers

        garage, _workshop = _two_printers(monkeypatch)
        monkeypatch.setattr(
            "kiln._pro_cutter_bridge.consult_blade",
            lambda name, printer_model=None: {"word": "due", "line": "5,000 cuts counted, at the maker's 5000 to 7000.", "confidence": "from_the_maker", "next_step": "swap"},
        )
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1"}):
            out = srv.start_print(file_name="part.gcode", printer_name="garage")
        assert out.get("success") is not False, out
        assert out["blade_advisory"]["word"] == "due"
        assert garage.started == ["part.gcode"]

    def test_a_healthy_blade_adds_nothing_to_the_start(self, monkeypatch):
        import os
        from unittest.mock import patch

        import kiln.server as srv

        from .test_every_start_says_so import _two_printers

        _two_printers(monkeypatch)
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1"}):
            out = srv.start_print(file_name="part.gcode", printer_name="garage")
        assert "blade_advisory" not in out

    def test_the_preflight_carries_the_line_as_an_advisory_that_never_fails_it(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiln.printers.base import PrinterStatus

        state = MagicMock()
        state.connected = True
        state.state = PrinterStatus.IDLE
        state.tool_temp_actual = 25.0
        state.tool_temp_target = 0.0
        state.bed_temp_actual = 25.0
        state.bed_temp_target = 0.0
        monkeypatch.setattr(
            "kiln._pro_cutter_bridge.consult_blade",
            lambda name, printer_model=None: {"word": "check_now", "line": "the cutter fault fired twice this month", "confidence": "estimate", "next_step": "look"},
        )
        with patch("kiln.server._get_adapter") as adapter, patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0)), \
                patch("kiln.server.get_db") as db, patch("kiln.server._registry") as registry:
            adapter.return_value.get_state.return_value = state
            registry.count = 1
            registry.list_names.return_value = ["a1"]
            db.return_value.get_printer_learning_insights.return_value = {"total_outcomes": 0}
            from kiln.server import preflight_check

            result = preflight_check()
        blade = [c for c in result["checks"] if c["name"] == "cutter_blade"]
        assert len(blade) == 1
        assert blade[0]["passed"] is True and blade[0]["advisory"] is True
        assert blade[0]["word"] == "check_now" and "twice this month" in blade[0]["message"]
        assert result["ready"] is True
