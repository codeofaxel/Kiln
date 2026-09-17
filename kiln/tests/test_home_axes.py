"""``home_axes`` -- the Home button, from Kiln, on every backend.

Born 2026-09-15 on a Bambu A1: the head had been left low over the plate
corner and powered off in fear, and when the owner asked "can you bring it
home?" the honest answer was "through a purge, or with a raw command".
Every printer screen has a Home button; Kiln had none.

Three tiers, each pinned here:

* a model with a vendor-cited sequence runs it (the A1: raise FIRST, then
  X, then Z on the strip after heating, then park over the chute);
* a backend without one sends the generic G28 and says the path is unknown;
* a Bambu model without a record refuses -- its own family never sends a
  bare G28 from an unknown height, so neither does Kiln.
"""

from __future__ import annotations

import inspect
import json
from unittest import mock

import pytest

from kiln.printers.base import (
    HomeResult,
    HomingUnsupported,
    PlateClearRequired,
    PrinterAdapter,
    PrinterError,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.command_verdict import CommandVerdict

from .test_filament_handling import (  # noqa: F401
    _all_adapter_classes,
    _build,
    _hot,
    _scripts,
    _Stub,
    bambu,
    no_kiln_pro,
)

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name in every Bambu test


@pytest.fixture(autouse=True)
def _no_served_network(monkeypatch):
    """No test asks the hosted service for a plan."""
    monkeypatch.setattr("kiln._pro_motion_bridge._served_plan", lambda request: None)


@pytest.fixture(autouse=True)
def _fresh_plate_record(tmp_path, monkeypatch):
    """Every test starts with no plate record.

    The record is written to disk on purpose (a cleared plate stays clear
    across processes), so without this one test's ``plate_clear=True``
    would answer the next test's question.
    """
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))


class TestContract:
    @pytest.mark.parametrize("name", sorted(_all_adapter_classes()))
    def test_every_adapter_has_home_axes_and_none_overrides_the_template(self, name):
        cls = _all_adapter_classes()[name]
        assert callable(getattr(cls, "home_axes", None))
        owners = [k for k in cls.__mro__ if k is not PrinterAdapter and "home_axes" in vars(k)]
        assert not owners, f"{cls.__name__} overrides home_axes; put backend logic in _home_axes_impl"

    def test_the_template_is_engagement_gated(self):
        assert getattr(PrinterAdapter.home_axes, "_kiln_engagement_wrapped", False)

    def test_home_axes_is_classified_confirm_with_physical_effect(self):
        from pathlib import Path

        data = json.loads((Path(inspect.getfile(PrinterAdapter)).parent.parent / "data" / "tool_safety.json").read_text())
        assert data["classifications"]["home_axes"] == {"level": "confirm", "physical_effect": True}


def _idle(adapter, monkeypatch, status=PrinterStatus.IDLE):
    monkeypatch.setattr(
        adapter, "get_state",
        lambda: PrinterState(connected=True, state=status, tool_temp_actual=25.0),
    )


class TestGenericBackend:
    """A backend with no vendor sequence hands the job to the firmware's own routine."""

    def test_sends_g28_and_reports_accepted_with_the_path_unknown(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        sent: list[list[str]] = []
        monkeypatch.setattr(
            adapter, "send_gcode",
            lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued", corroboration="http_2xx"),
        )
        result = adapter.home_axes()
        assert isinstance(result, HomeResult)
        assert sent == [["G28"]]
        assert result.outcome == "accepted" and result.success is True
        assert result.sequence_source == "firmware_home_routine"
        assert result.homed_axes == ["X", "Y", "Z"]
        assert "cannot see whether that routine lifts Z" in result.message and "firmware's own homing routine" in result.message
        assert result.details["verification_source"] == "not_read_back"
        assert adapter.homing_commanded == frozenset("XYZ")

    def test_axes_subset_is_passed_through(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        sent: list[list[str]] = []
        monkeypatch.setattr(
            adapter, "send_gcode",
            lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued"),
        )
        result = adapter.home_axes(axes="zx")
        assert sent == [["G28 X Z"]]
        assert result.homed_axes == ["X", "Z"]

    def test_a_refused_send_is_failed(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.refused("printer not operational"))
        result = adapter.home_axes()
        assert result.success is False and result.outcome == "failed"
        assert adapter.homing_commanded == frozenset()

    def test_refused_while_printing_and_while_paused(self, monkeypatch):
        adapter = _build("octoprint")
        for status, phrase in ((PrinterStatus.PRINTING, "running"), (PrinterStatus.PAUSED, "paused")):
            _idle(adapter, monkeypatch, status)
            with pytest.raises(PrinterError, match=phrase):
                adapter.home_axes()

    def test_nonsense_axes_is_refused(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        with pytest.raises(PrinterError, match="names none of X, Y, Z"):
            adapter.home_axes(axes="Q")

    def test_a_backend_that_cannot_send_gcode_refuses_by_name(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(type(adapter), "capabilities", property(lambda self: mock.Mock(can_send_gcode=False)))
        with pytest.raises(HomingUnsupported, match="own screen"):
            adapter.home_axes()




class TestDoors:
    @pytest.fixture
    def door(self, monkeypatch):
        import kiln.server as srv

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.name = "octo"
        adapter.home_axes.return_value = HomeResult(
            success=True, outcome="accepted", message="sent G28", axes="XYZ",
            homed_axes=["X", "Y", "Z"], mechanism="gcode", sequence_source="firmware_home_routine",
            resting_position={"described": "the firmware's home position"},
        )
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, name or "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
        return adapter

    def test_mcp_envelope_carries_outcome_and_resting_position(self, door):
        from kiln.plugins.homing_tools import home_axes

        out = home_axes()
        assert out["success"] is True and out["outcome"] == "accepted"
        assert out["resting_position"] == {"described": "the firmware's home position"}
        assert out["printer_name"] == "default"
        door.home_axes.assert_called_once()
        assert door.home_axes.call_args.kwargs["axes"] == "XYZ"
        assert door.home_axes.call_args.kwargs["wait_ceiling_seconds"] > 0

    def test_a_heating_sequence_carries_the_burn_warning(self, door):
        from kiln.plugins.homing_tools import home_axes

        door.home_axes.return_value.heats_nozzle_to_c = 170.0
        assert "burn hazard" in home_axes()["safety"].lower()

    def test_plan_only_skips_the_confirm_and_the_rate_limit_but_not_auth(self, door, monkeypatch):
        """``plan_only`` sends nothing, so no confirmation and no rate limit
        -- but it still reads the printer and hands back the vendor
        sequence, so it is gated as a read.  Before this pin it skipped
        auth altogether."""
        import kiln.server as srv
        from kiln.plugins.homing_tools import home_axes, park_head

        seen: list[str] = []
        monkeypatch.setattr(srv, "_check_auth", lambda scope: seen.append(scope) or {"success": False, "error": {"code": "AUTH", "message": "no"}})
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: pytest.fail("plan_only must not ask for confirmation"))
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: pytest.fail("plan_only must not count against the rate limit"))
        assert home_axes(plan_only=True)["error"]["code"] == "AUTH"
        assert park_head(plan_only=True)["error"]["code"] == "AUTH"
        assert seen == ["read", "read"]
        door.home_axes.assert_not_called()
        door.park_head.assert_not_called()

    def test_unsupported_is_named_with_what_to_do(self, door):
        from kiln.plugins.homing_tools import home_axes

        door.home_axes.side_effect = HomingUnsupported("no sequence; home from the printer's own screen")
        out = home_axes()
        assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED"
        assert "own screen" in out["error"]["message"]

    def test_kiln_home_runs_the_same_tool(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["home", "--axes", "XY", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["status"] == "success"
        assert door.home_axes.call_args.kwargs["axes"] == "XY"

    def test_kiln_home_refusal_exits_nonzero_with_what_to_do(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        door.home_axes.side_effect = HomingUnsupported("home from the printer's own screen")
        result = CliRunner().invoke(cli, ["home"])
        assert result.exit_code == 1 and "own screen" in result.output

    def test_the_cli_door_calls_the_shared_runtime_config(self):
        from kiln.cli import main as cli_main

        assert "ensure_runtime_config()" in inspect.getsource(cli_main.home_cmd.callback)


class TestFirmwareThatReportsHomed:
    """Klipper and RepRapFirmware say which axes are homed; Kiln repeats them."""

    def test_klipper_confirms_from_toolhead_homed_axes_and_reads_safe_z_home(self, monkeypatch):
        adapter = _build("moonraker")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok", corroboration="moonraker_ok"))
        queries: list[dict] = []

        def fake_get(path, params=None, **kw):
            queries.append(params or {})
            if "toolhead" in (params or {}):
                return {"result": {"status": {"toolhead": {"homed_axes": "xyz"}}}}
            if "configfile" in (params or {}):
                return {"result": {"status": {"configfile": {"settings": {"safe_z_home": {"home_xy_position": "150,150"}}}}}}
            return {}

        monkeypatch.setattr(adapter, "_get_json", fake_get)
        result = adapter.home_axes()
        assert result.outcome == "confirmed" and result.success
        assert result.details["firmware_homed_axes"] == ["X", "Y", "Z"]
        assert result.details["z_lifts_before_home"] is True
        assert "lifts Z before X and Y move" in result.message
        assert result.sequence_source == "firmware_home_routine"

    def test_klipper_partial_flag_is_accepted_and_names_what_is_missing(self, monkeypatch):
        adapter = _build("moonraker")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok"))
        monkeypatch.setattr(adapter, "_get_json", lambda path, params=None, **kw: (
            {"result": {"status": {"toolhead": {"homed_axes": "xy"}}}} if "toolhead" in (params or {})
            else {"result": {"status": {"configfile": {"settings": {}}}}}))
        result = adapter.home_axes()
        assert result.outcome == "accepted"
        assert result.details["verification_source"] == "firmware_homed_flag_partial"
        assert "not Z" in result.message
        assert result.details["z_lifts_before_home"] is False
        assert "does NOT lift Z" in result.message

    def test_creality_delegates_to_its_klipper_backend(self, monkeypatch):
        adapter = _build("creality")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter._backend, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok"))
        monkeypatch.setattr(adapter._backend, "_get_json", lambda path, params=None, **kw: (
            {"result": {"status": {"toolhead": {"homed_axes": "xyz"}}}} if "toolhead" in (params or {}) else {}))
        assert adapter.home_axes().outcome == "confirmed"

    def test_reprapfirmware_3_confirms_from_the_object_model(self, monkeypatch):
        adapter = _build("duet")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "_generation", lambda: 3)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok"))
        monkeypatch.setattr(adapter, "_model", lambda key: [
            {"letter": "X", "homed": True}, {"letter": "Y", "homed": True}, {"letter": "Z", "homed": True},
        ] if key == "move.axes" else None)
        result = adapter.home_axes()
        assert result.outcome == "confirmed" and result.details["firmware_homed_axes"] == ["X", "Y", "Z"]

    def test_reprapfirmware_2_reads_the_legacy_flags(self, monkeypatch):
        adapter = _build("duet")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "_generation", lambda: 2)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok"))
        monkeypatch.setattr(adapter, "_get_json", lambda path, params=None, **kw: {"coords": {"axesHomed": [1, 1, 0]}})
        result = adapter.home_axes(axes="XY")
        assert result.outcome == "confirmed" and result.details["firmware_homed_axes"] == ["X", "Y"]

    def test_marlin_over_usb_reports_its_position_after_homing(self, monkeypatch):
        adapter = _build("serial")
        _idle(adapter, monkeypatch)
        sent: list[str] = []
        monkeypatch.setattr(adapter, "_send_command", lambda cmd, **kw: sent.append(cmd) or (
            "X:0.00 Y:0.00 Z:10.00 E:0.00 Count X:0 Y:0 Z:8000" if cmd == "M114" else "ok"))
        result = adapter.home_axes()
        assert result.outcome == "accepted"  # Marlin reports no homed flag
        assert result.resting_position["z"] == 10.0 and "position report" in result.resting_position["source"]
        assert "M114" in sent

    def test_a_backend_that_cannot_send_gcode_is_refused_by_name(self, monkeypatch):
        adapter = _build("prusalink")
        _idle(adapter, monkeypatch)
        with pytest.raises(HomingUnsupported, match="own screen"):
            adapter.home_axes()




class TestStepMode:
    """Step mode is the contract every backend honours: plan_only sends
    nothing, step=N sends one described motion, a bad step is refused
    before anything moves.  The served vendor sequences exercise it in
    depth; the public floor pins the shape on the firmware's own routine.
    """

    def test_a_bad_step_value_is_refused_before_anything(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        sent: list = []
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("ok"))
        with pytest.raises(PrinterError, match="whole number"):
            adapter.home_axes(step=0)
        with pytest.raises(PrinterError, match="whole number"):
            adapter.home_axes(step=True)
        assert sent == []

    def test_plan_only_is_allowed_while_printing(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch, PrinterStatus.PRINTING)
        sent: list = []
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("ok"))
        result = adapter.home_axes(plan_only=True)
        assert result.details["sent"] is False and sent == []
        with pytest.raises(PrinterError, match="running"):
            adapter.home_axes()

    def test_generic_backend_plans_one_step(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        sent: list = []
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("ok"))
        plan = adapter.home_axes(plan_only=True)
        assert sent == [] and len(plan.steps) == 1 and plan.steps[0]["gcode"] == ["G28"]
        with pytest.raises(PrinterError, match="one step"):
            adapter.home_axes(step=2)

    def test_the_doors_pass_step_and_plan_through(self, monkeypatch):
        from click.testing import CliRunner

        import kiln.server as srv
        from kiln.cli.main import cli
        from kiln.plugins.homing_tools import home_axes

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.name = "a1"
        adapter.home_axes.return_value = HomeResult(success=True, outcome="accepted", message="ok", axes="XYZ")
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        confirmations: list = []
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: confirmations.append(a) or None)

        home_axes(step=3)
        assert adapter.home_axes.call_args.kwargs["step"] == 3
        home_axes(plan_only=True)
        assert adapter.home_axes.call_args.kwargs["plan_only"] is True
        assert len(confirmations) == 1  # planning needs no confirmation; sending does
        result = CliRunner().invoke(cli, ["home", "--step", "2", "--json"])
        assert result.exit_code == 0, result.output
        assert adapter.home_axes.call_args.kwargs["step"] == 2
        result = CliRunner().invoke(cli, ["home", "--plan", "--json"])
        assert result.exit_code == 0 and adapter.home_axes.call_args.kwargs["plan_only"] is True




class TestParkHead:
    """The retreat: raise, home X, off the plate -- never a Z touch, never heat.

    Asked for on 2026-09-16 after the home's Z descent scared the owner into
    killing the power: "a tool that can move the nozzle somewhere safe and
    away from the print bed."  Park is home without the step that touches.
    """

    @pytest.mark.parametrize("name", sorted(_all_adapter_classes()))
    def test_every_adapter_has_park_head_and_none_overrides_the_template(self, name):
        cls = _all_adapter_classes()[name]
        assert callable(getattr(cls, "park_head", None))
        owners = [k for k in cls.__mro__ if k is not PrinterAdapter and "park_head" in vars(k)]
        assert not owners, f"{cls.__name__} overrides park_head; put backend logic in _park_head_impl"

    def test_the_template_is_engagement_gated_and_classified(self):
        assert getattr(PrinterAdapter.park_head, "_kiln_engagement_wrapped", False)
        from pathlib import Path

        data = json.loads((Path(inspect.getfile(PrinterAdapter)).parent.parent / "data" / "tool_safety.json").read_text())
        assert data["classifications"]["park_head"] == {"level": "confirm", "physical_effect": True}


    def test_park_refuses_while_printing_and_while_paused(self, monkeypatch):
        adapter = _build("octoprint")
        for status, phrase in ((PrinterStatus.PRINTING, "running"), (PrinterStatus.PAUSED, "paused")):
            _idle(adapter, monkeypatch, status)
            with pytest.raises(PrinterError, match=phrase):
                adapter.park_head()

    def test_generic_backend_parks_at_the_firmware_home_and_says_so(self, monkeypatch):
        adapter = _build("octoprint")
        _idle(adapter, monkeypatch)
        sent: list = []
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("ok"))
        result = adapter.park_head()
        assert sent == [["G28"]]
        assert result.action == "park" and result.outcome == "accepted"
        assert result.message.startswith("Parked at the firmware's own home position")
        assert "the home IS the park" in result.message

    def test_klipper_park_confirms_from_its_homed_flags(self, monkeypatch):
        adapter = _build("moonraker")
        _idle(adapter, monkeypatch)
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: CommandVerdict.accepted_only("ok"))
        monkeypatch.setattr(adapter, "_get_json", lambda path, params=None, **kw: (
            {"result": {"status": {"toolhead": {"homed_axes": "xyz"}}}} if "toolhead" in (params or {})
            else {"result": {"status": {"configfile": {"settings": {"safe_z_home": {}}}}}}))
        result = adapter.park_head()
        assert result.action == "park" and result.outcome == "confirmed"

    def test_prusalink_refuses_park_by_name(self, monkeypatch):
        adapter = _build("prusalink")
        _idle(adapter, monkeypatch)
        with pytest.raises(HomingUnsupported, match="own screen"):
            adapter.park_head()

    def test_the_doors_run_park_through_the_same_engine(self, monkeypatch):
        from click.testing import CliRunner

        import kiln.server as srv
        from kiln.cli.main import cli
        from kiln.plugins.homing_tools import park_head

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.name = "a1"
        adapter.park_head.return_value = HomeResult(success=True, outcome="accepted", message="parked", axes="XY", action="park")
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
        out = park_head(step=1)
        assert out["success"] is True and out["action"] == "park"
        adapter.park_head.assert_called_once()
        assert adapter.park_head.call_args.kwargs["step"] == 1
        adapter.home_axes.assert_not_called()
        result = CliRunner().invoke(cli, ["park", "--plan", "--json"])
        assert result.exit_code == 0, result.output
        assert adapter.park_head.call_args.kwargs["plan_only"] is True

    def test_park_unsupported_names_the_screen_through_the_door(self, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import park_head

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.name = "x1c"
        adapter.park_head.side_effect = HomingUnsupported("no verified park position; park from the printer's own screen")
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
        out = park_head()
        assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED" and "own screen" in out["error"]["message"]




def _fake_home_doc(printer_id: str = "bambu_a1", *, verb: str = "home", z_on_plate: bool = False) -> dict:
    """A plan document with made-up figures -- the shape, never a real sequence."""
    steps = [
        {"number": 1, "label": "raise", "you_will_see": "the head lifts a little", "stops_when": "the probe move ends",
         "gcode": ["G91", "G1 Z7 F100", "G90"], "leaves": ["limits pushed"]},
        {"number": 2, "label": "home X and Y", "you_will_see": "the head goes left and the bed rolls back",
         "stops_when": "the endstops", "gcode": ["G28 X"], "leaves": []},
    ]
    if verb == "home":
        steps.append({"number": 3, "label": "home Z", "you_will_see": "the nozzle descends", "stops_when": "the sensor",
                      "gcode": ["G28 Z"], "leaves": ["heater ON"], "touches_plate": z_on_plate})
    steps.append({"number": len(steps) + 1, "label": "park", "you_will_see": "the head parks off the plate",
                  "stops_when": "the move ends", "gcode": ["G1 X-9 F100", "G1 Y0 F100"], "leaves": []})
    return {
        "schema": "motion_plan/1", "printer_id": printer_id, "verb": verb, "ok": True, "steps": steps,
        "homed_axes": ["X", "Y", "Z"] if verb == "home" else ["X", "Y"], "heats_nozzle_to_c": 170.0 if verb == "home" else None,
        "sequence_source": "vendor_start_sequence", "summary": f"Ran the {verb} the machine's own way.",
        "resting_position": {"x_mm": -9, "over": "the chute"}, "homed_flag_bits": {"X": 0, "Y": 1, "Z": 2},
        "raise_clearance_mm": 7.0, "z_home_on_plate": z_on_plate, "finish": None,
    }


def _serve(monkeypatch, docs):
    """Make the bridge answer from *docs* (keyed by verb) and record every ask.

    Also runs the executor's fault watch on a fast clock: the watch sleeps
    a second at a time for ten seconds by default, which is right at a
    machine and wrong in a test.
    """
    import itertools
    import time as _time

    from kiln import _pro_motion_bridge as bridge

    counter = itertools.count(0.0, 0.5)
    monkeypatch.setattr(_time, "monotonic", lambda: next(counter))
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    asks: list[dict] = []

    def _plan_for(adapter, verb, *, axes="XYZ", on_plate_ok=False):
        asks.append({"verb": verb, "axes": axes, "on_plate_ok": on_plate_ok})
        doc = docs.get(verb) if isinstance(docs, dict) else docs
        return doc(on_plate_ok, axes) if callable(doc) else doc

    monkeypatch.setattr(bridge, "plan_for", _plan_for)
    monkeypatch.setattr(bridge, "station_supports", lambda *a: None)
    return asks


class TestBambuWithoutAPlan:
    """The public floor on a Bambu: no plan, no motion, an honest refusal.

    Every Bambu start file raises the head before it homes and never sends
    a bare G28 from an unknown height, so this install never invents one:
    home and park refuse by name, say the plan is served, and point at the
    screen's jog controls -- Z up first, never the Home button over a
    possible part.
    """

    @pytest.mark.parametrize("verb", ["home", "park"])
    def test_home_and_park_refuse_by_name_with_the_served_line(self, no_kiln_pro, bambu, monkeypatch, verb):
        _serve(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.home_axes() if verb == "home" else bambu.park_head()
        text = str(info.value)
        assert f"Kiln will not {verb} bambu_a1" in text and "served one plan at a time" in text
        assert "jog controls" in text and "Z UP first" in text and "Home button descends" in text
        assert _scripts(bambu) == []

    def test_plan_only_refuses_too_because_there_is_nothing_to_describe(self, no_kiln_pro, bambu, monkeypatch):
        _serve(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported):
            bambu.home_axes(plan_only=True)
        assert _scripts(bambu) == []

    def test_a_plan_that_says_no_is_refused_in_its_own_words(self, no_kiln_pro, bambu, monkeypatch):
        doc = {**_fake_home_doc("bambu_x1c"), "ok": False, "steps": [],
               "refusal": {"code": "UNSUPPORTED", "message": "NOT DRIVEN YET. On this model the bed moves in Z."}}
        _serve(monkeypatch, {"home": doc, "park": doc})
        bambu._printer_model = "bambu_x1c"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported, match="bed moves in Z"):
            bambu.park_head()
        ok, why = bambu._station_supports(None, "park")
        assert ok is False and "bed moves in Z" in why

    def test_the_door_surfaces_the_refusal_as_unsupported(self, no_kiln_pro, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import home_axes, park_head

        _serve(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
        for fn in (home_axes, park_head):
            out = fn()
            assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED"
            assert "served one plan at a time" in out["error"]["message"]


class TestBambuRunsAPlan:
    """A plan that answers is run by the public executor: all of it, one step,
    or none -- and the answer is composed from the plan's own words."""

    def test_plan_only_sends_nothing_and_describes_every_step(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        result = bambu.home_axes(plan_only=True)
        assert result.success and result.details["sent"] is False and _scripts(bambu) == []
        assert [s["label"] for s in result.steps] == ["raise", "home X and Y", "home Z", "park"]
        assert result.next_step["number"] == 1 and result.heats_nozzle_to_c == 170.0

    def test_step_n_sends_only_step_n_and_describes_n_plus_one(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        result = bambu.home_axes(step=2)
        assert _scripts(bambu) == ["G28 X"]
        assert result.step_sent == 2 and result.next_step["label"] == "home Z"
        assert result.homed_axes == ["X", "Y"]  # G28 X homes both on this family
        assert "Step 2 of 4 sent (home X and Y)" in result.message and "Next: step 3" in result.message
        with pytest.raises(PrinterError, match="4 steps; step 9"):
            bambu.home_axes(step=9)

    def test_a_full_run_sends_the_concatenation_and_confirms_from_the_flag_bits(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        bambu._last_status["home_flag"] = 0b111
        result = bambu.home_axes()
        assert _scripts(bambu) == ["G91\nG1 Z7 F100\nG90\nG28 X\nG28 Z\nG1 X-9 F100\nG1 Y0 F100"]
        assert result.outcome == "confirmed" and result.homed_axes == ["X", "Y", "Z"]
        assert result.message.startswith("Ran the home the machine's own way.")
        assert result.details["verification_source"] == "homed_flag_bits" and result.resting_position["x_mm"] == -9
        assert bambu.homing_commanded == {"X", "Y", "Z"}

    def test_a_partial_flag_reads_accepted_not_confirmed(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        bambu._last_status["home_flag"] = 0b011
        result = bambu.home_axes()
        assert result.outcome == "accepted" and "not Z" in result.message

    def test_park_runs_the_park_plan_and_never_asks_for_z(self, bambu, monkeypatch):
        asks = _serve(monkeypatch, {"park": _fake_home_doc(verb="park")})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        result = bambu.park_head()
        assert result.success and result.action == "park" and result.homed_axes == ["X", "Y"]
        assert asks == [{"verb": "park", "axes": "XY", "on_plate_ok": False}]
        assert not any("G28 Z" in s for s in _scripts(bambu))

    def test_a_fault_during_the_run_is_reported_in_the_printers_words(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        faults = iter([set(), {("0300_1A00_0002_0001", "hms")}])
        monkeypatch.setattr(bambu, "_snapshot_faults", lambda: next(faults, {("0300_1A00_0002_0001", "hms")}))
        result = bambu.home_axes()
        assert result.success is False and result.outcome == "failed" and result.error_code == "0300_1A00_0002_0001"

    def test_a_cached_plan_says_so(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": {**_fake_home_doc(), "from_cache": True}})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        assert bambu.home_axes(plan_only=True).details["plan_source"] == "cache"


class TestConsentFloor:
    """A Z home that presses the nozzle onto the plate asks the person on
    EVERY call; a recorded "clear" answers the row question and nothing
    more.  The plan says whether the Z step presses the plate; the public
    gate does the asking.
    """

    def _mini(self, bambu, monkeypatch):
        def doc(on_plate_ok, axes):
            if "Z" not in axes:
                return _fake_home_doc("bambu_a1_mini", verb="park")
            if not on_plate_ok:
                return {**_fake_home_doc("bambu_a1_mini", z_on_plate=True), "ok": False, "steps": [],
                        "refusal": {"code": "PLATE_CLEAR_REQUIRED", "message": "homes Z onto the plate"}}
            return _fake_home_doc("bambu_a1_mini", z_on_plate=True)

        asks = _serve(monkeypatch, {"home": doc, "park": _fake_home_doc("bambu_a1_mini", verb="park")})
        bambu._printer_model = "bambu_a1_mini"
        _idle(bambu, monkeypatch)
        return asks

    def test_the_press_asks_and_a_persons_word_on_the_call_runs_it(self, bambu, monkeypatch):
        asks = self._mini(bambu, monkeypatch)
        with pytest.raises(PlateClearRequired) as info:
            bambu.home_axes()
        assert "onto the PLATE" in str(info.value) and "plate_clear=true" in str(info.value)
        assert _scripts(bambu) == []
        result = bambu.home_axes(plate_clear=True)
        assert result.success and "G28 Z" in _scripts(bambu)[-1]
        assert asks[-1]["on_plate_ok"] is True
        ok, why = bambu._station_supports(None, "home_z")
        assert ok is False and "plate_clear" in why
        assert bambu._station_supports(None, "home_z_on_plate")[0] is True

    def test_a_recorded_clear_plate_answers_the_row_but_not_the_press(self, bambu, monkeypatch):
        from kiln.plate_state import mark_clear

        self._mini(bambu, monkeypatch)
        mark_clear(bambu, "human")
        with pytest.raises(PlateClearRequired) as info:
            bambu.home_axes()
        assert "a person said so" in str(info.value) and "every time" in str(info.value)
        assert bambu.park_head().success and bambu.home_axes(axes="XY").success

    def test_a_recorded_part_taller_than_the_raise_refuses_the_travel(self, bambu, monkeypatch):
        from kiln.plate_state import PlateJob, mark_occupied

        self._mini(bambu, monkeypatch)
        mark_occupied(bambu, PlateJob(file="vase.gcode", max_z_mm=60.0))
        with pytest.raises(PlateClearRequired, match="vase.gcode.*60 mm tall.*7 mm"):
            bambu.park_head()
        mark_occupied(bambu, PlateJob(file="coin.gcode", max_z_mm=3.0))
        assert bambu.park_head().success


class TestPlateRecordDoors:
    def test_kiln_plate_clear_records_a_persons_word(self, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import plate_status, run_plate

        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        assert plate_status()["plate"]["status"] == "unknown"
        out = run_plate(action="clear", note="looked")
        assert out["success"] is True and out["plate"]["status"] == "clear" and out["plate"]["source"] == "human"
        assert plate_status()["plate"]["status"] == "clear"


class TestDoctorWithoutAPlan:
    """``kiln doctor`` asks the same gate the tools ask, so with no plan a
    Bambu reads as refused-by-name with the served line, and a generic
    backend reads as the firmware's own routine."""

    def test_a_bambu_reads_as_refused_with_the_served_line(self, no_kiln_pro, bambu, monkeypatch):
        from kiln.cli.main import _doctor_filament_where, _doctor_homing_how

        _serve(monkeypatch, None)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        where, warn = _doctor_filament_where(bambu)
        assert warn is True and "in place" in where and "wipe_nozzle refuses" in where and "bambu_a1" in where
        detail, warn = _doctor_homing_how(bambu)
        assert warn is True and "both refuse" in detail and "jog controls" in detail and "bambu_a1" in detail

    def test_a_bambu_with_a_plan_reads_as_the_machines_own_sequence(self, bambu, monkeypatch):
        from kiln.cli.main import _doctor_homing_how

        _serve(monkeypatch, {"home": _fake_home_doc(), "park": _fake_home_doc(verb="park")})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        detail, warn = _doctor_homing_how(bambu)
        assert warn is False and "own start-sequence homing" in detail

    def test_a_generic_backend_reads_as_the_firmware_routine(self, no_kiln_pro, monkeypatch):
        from kiln.cli.main import _doctor_filament_where, _doctor_homing_how

        adapter = _build("octoprint")
        detail, warn = _doctor_homing_how(adapter)
        assert warn is False and "firmware's own homing routine" in detail and "park_head (kiln park) parks at the firmware's own home" in detail
        where, warn = _doctor_filament_where(adapter)
        assert warn is True and "in place" in where and "wipe_nozzle refuses" in where
