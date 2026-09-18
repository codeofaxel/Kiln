"""Every door that cuts filament reports it, and none of them is slowed or broken by the report.

A machine with a filament cutter cuts on a load, on a colour change and on
some machines on the firmware's own cancel.  No maker counts cuts; Kiln
does, at the events it can honestly see.  These tests pin the doors:

  - the start_print template every print passes through reports the sliced
    file's planned changes -- success or failure downstream, and never a
    resume;
  - the filament-op finish every backend's load and unload pass through
    reports the verb, and a purge or a wipe reports nothing;
  - the MQTT backend's cancel reports ``cancel``;
  - its push stream reports a tray change it saw, once, on its edge,
    and stays quiet while Kiln itself is driving the machine;
  - the hosted stub for a blade-status request carries this install's recent
    fault codes along;
  - a report that raises never reaches the door's result.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import FilamentOpPlan, FilamentOpResult, PrintResult

from .test_filament_handling import bambu  # noqa: F401

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name


@pytest.fixture
def reports(monkeypatch):
    """Capture what the doors hand the bridge, and keep the bridge off the network."""
    import kiln._pro_cutter_bridge as bridge

    seen: dict[str, list] = {"start": [], "command": [], "switch": []}
    monkeypatch.setattr(bridge, "record_print_cuts", lambda name, file, **kw: seen["start"].append((name, file, kw)))
    monkeypatch.setattr(bridge, "record_command_cut", lambda name, verb, **kw: seen["command"].append((name, verb, kw)))
    monkeypatch.setattr(bridge, "record_observed_switch", lambda name, **kw: seen["switch"].append((name, kw)))
    return seen


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


class TestTheStartDoor:
    def test_every_start_reports_the_file_it_started(self, bambu, monkeypatch, reports):
        _lean_start(bambu, monkeypatch)
        assert bambu.start_print("two-colour.gcode.3mf").success
        assert reports["start"] == [("bambu", "two-colour.gcode.3mf", {"printer_model": "bambu_a1"})]

    def test_a_resume_is_not_a_new_print(self, bambu, monkeypatch, reports):
        _lean_start(bambu, monkeypatch)
        bambu.start_print("transformed_resume_ab12.3mf")
        assert reports["start"] == []

    def test_the_report_never_blocks_the_start(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_print_cuts", mock.Mock(side_effect=RuntimeError("disk")))
        assert bambu.start_print("fine.3mf").success


class TestTheFilamentDoor:
    def _finish(self, bambu, action: str) -> FilamentOpResult:
        plan = FilamentOpPlan(action=action, temperature=220.0, temperature_source="test")
        return bambu._finish_filament_op(plan, FilamentOpResult(success=True, action=action, message="done"))

    @pytest.mark.parametrize("action", ["load", "unload"])
    def test_a_finished_load_or_unload_reports_its_verb(self, bambu, monkeypatch, reports, action):
        bambu._printer_model = "bambu_a1"
        monkeypatch.setattr(bambu, "_leave_heater_off", lambda *a, **k: None, raising=False)
        self._finish(bambu, action)
        assert [(n, v) for n, v, _ in reports["command"]] == [("bambu", action)]
        assert reports["command"][0][2] == {"printer_model": "bambu_a1"}

    @pytest.mark.parametrize("action", ["purge", "wipe"])
    def test_a_purge_or_a_wipe_cuts_nothing(self, bambu, monkeypatch, reports, action):
        self._finish(bambu, action)
        assert reports["command"] == []

    def test_a_plan_only_or_a_mid_sequence_step_reports_nothing(self, bambu, reports):
        plan = FilamentOpPlan(action="load", temperature=220.0, temperature_source="test", options={"plan_only": True})
        bambu._finish_filament_op(plan, FilamentOpResult(success=True, action="load", message="planned"))
        mid = FilamentOpPlan(action="unload", temperature=220.0, temperature_source="test")
        bambu._finish_filament_op(mid, FilamentOpResult(success=True, action="unload", message="step 1", next_step={"n": 2}))
        assert reports["command"] == []

    def test_the_report_never_changes_the_result(self, bambu, monkeypatch):
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_command_cut", mock.Mock(side_effect=RuntimeError("disk")))
        out = self._finish(bambu, "load")
        assert out.success and out.message.startswith("done")


class TestTheBambuCancel:
    def test_a_cancel_reports_cancel(self, bambu, reports):
        bambu._printer_model = "bambu_a1"
        assert bambu.cancel_print().success
        assert [(n, v) for n, v, _ in reports["command"]] == [("bambu", "cancel")]

    def test_the_report_never_blocks_the_stop(self, bambu, monkeypatch):
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_command_cut", mock.Mock(side_effect=RuntimeError("disk")))
        assert bambu.cancel_print().success
        assert bambu._mqtt_client.publish.called


class TestTheObservedTrayChange:
    def test_a_tray_change_on_the_wire_is_reported_once_on_its_edge(self, bambu, monkeypatch, reports):
        monkeypatch.setattr(bambu, "_kiln_is_driving", lambda: False)
        _push(bambu, ams={"tray_now": "255"})
        _push(bambu, ams={"tray_now": "1"})
        _push(bambu, ams={"tray_now": "1"})  # the same tray again is not a change
        _push(bambu, nozzle_temper=210)  # a frame without the section says nothing
        assert [(n, kw["from_tray"], kw["to_tray"]) for n, kw in reports["switch"]] == [("bambu", "255", "1")]

    def test_the_first_frame_that_names_a_tray_is_not_a_change(self, bambu, monkeypatch, reports):
        monkeypatch.setattr(bambu, "_kiln_is_driving", lambda: False)
        bambu._last_status.pop("ams", None)
        _push(bambu, ams={"tray_now": "2"})
        assert reports["switch"] == []

    def test_a_change_kiln_caused_is_not_reported_by_the_observer(self, bambu, monkeypatch, reports):
        import time

        bambu._filament_command_sent_at = time.monotonic()
        _push(bambu, ams={"tray_now": "255"})
        _push(bambu, ams={"tray_now": "3"})
        assert reports["switch"] == []

    def test_a_change_during_a_print_kiln_started_is_already_counted(self, bambu, monkeypatch, reports):
        from kiln.printers.engagement import Engagement, machine_id

        monkeypatch.setattr(
            "kiln.printers.engagement.current",
            lambda: Engagement(machine=machine_id(bambu), label="", job=None, since=0.0, reason="started"),
        )
        _push(bambu, ams={"tray_now": "0"})
        _push(bambu, ams={"tray_now": "1"})
        assert reports["switch"] == []

    def test_the_observer_never_breaks_the_push_path(self, bambu, monkeypatch):
        monkeypatch.setattr(bambu, "_kiln_is_driving", lambda: False)
        monkeypatch.setattr("kiln._pro_cutter_bridge.record_observed_switch", mock.Mock(side_effect=RuntimeError("bus")))
        _push(bambu, ams={"tray_now": "255"})
        _push(bambu, ams={"tray_now": "1"})
        assert bambu._last_status["ams"]["tray_now"] == "1"


class TestTheHostedStub:
    def test_a_blade_status_request_carries_recent_faults(self, monkeypatch):
        import kiln.server as srv

        class _MCP:
            def __init__(self):
                self.tools = {}

            def tool(self, **_kw):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return deco

        sent = {}
        monkeypatch.setattr(srv, "_pro_api_call", lambda name, **kw: sent.setdefault(name, kw) or {"success": True})
        monkeypatch.setattr("kiln._pro_cutter_bridge.recent_faults_for", lambda name, days=30: [{"code": "1200-8001", "at": "t"}])
        monkeypatch.setattr("kiln.printer_nozzle_reading.with_local_reading", lambda name, kw: kw)
        fake = _MCP()
        srv._register_pro_tool_stubs(fake)
        assert "cutter_wear_status" in fake.tools, "the regenerated manifest lists the blade door"
        fake.tools["cutter_wear_status"](printer_id="a1")
        assert sent["cutter_wear_status"]["recent_faults"] == [{"code": "1200-8001", "at": "t"}]
