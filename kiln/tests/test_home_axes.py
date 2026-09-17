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




class TestBambuWithoutTheServedSequence:
    """The public floor on a Bambu: no served sequence, no motion, an honest refusal.

    Every Bambu start file raises the head before it homes and never sends
    a bare G28 from an unknown height, so this install never invents one:
    home and park refuse by name, say the sequence is served through
    kiln-pro, and point at the screen's jog controls -- Z up first, never
    the Home button over a possible part.
    """

    @pytest.mark.parametrize("verb", ["home", "park"])
    def test_home_and_park_refuse_by_name_with_the_served_line(self, no_kiln_pro, bambu, monkeypatch, verb):
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.home_axes() if verb == "home" else bambu.park_head()
        text = str(info.value)
        assert f"Kiln will not {verb} bambu_a1" in text
        assert "served through Kiln's hosted service" in text
        assert "jog controls" in text and "Z UP first" in text and "Home button descends" in text
        assert _scripts(bambu) == []

    def test_plan_only_refuses_too_because_there_is_no_sequence_to_describe(self, no_kiln_pro, bambu, monkeypatch):
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported):
            bambu.home_axes(plan_only=True)
        assert _scripts(bambu) == []

    def test_the_door_surfaces_the_refusal_as_unsupported(self, no_kiln_pro, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import home_axes, park_head

        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        for fn in (home_axes, park_head):
            monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
            monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
            monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
            out = fn()
            assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED"
            assert "served through Kiln's hosted service" in out["error"]["message"]

    def test_a_served_sequence_comes_back_through_the_bridge_untouched(self, bambu, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        served = HomeResult(success=True, outcome="accepted", message="served", axes="XYZ", homed_axes=["X", "Y", "Z"])
        calls: list = []
        monkeypatch.setattr(bridge, "home_axes_impl", lambda adapter, axes, options: calls.append((adapter, axes, dict(options))) or served)
        result = bambu.home_axes(step=2, wait_seconds=3)
        assert result is served and bambu.homing_commanded == {"X", "Y", "Z"}
        assert calls[0][0] is bambu and calls[0][1] == "XYZ" and calls[0][2]["step"] == 2
        parked = HomeResult(success=True, outcome="accepted", message="served park", axes="XY", homed_axes=["X", "Y"])
        monkeypatch.setattr(bridge, "park_head_impl", lambda adapter, options: parked)
        result = bambu.park_head()
        assert result is parked and result.action == "park"

    def test_homed_flags_are_read_only_where_served(self, no_kiln_pro, bambu):
        bambu._last_status["home_flag"] = 0b111
        assert bambu._read_homed_axes() is None


class TestConsentFloor:
    """A Z home that presses the nozzle onto the plate asks the person on
    EVERY call; a recorded "clear" answers the row question and nothing
    more.  Pinned on the template gate with a served record faked in, so
    the floor holds whether or not the record is served.
    """

    _STATION = {"printer_id": "bambu_a1_mini", "raise_before_travel": {"probe_up_mm": 25, "back_down_mm": 15}}

    def _gate(self, monkeypatch, state=None):
        from kiln import _pro_motion_bridge as bridge
        from kiln.plate_state import PlateState

        adapter = _Stub()
        adapter._printer_model = "bambu_a1_mini"
        monkeypatch.setattr("kiln.plate_state.machine_id", lambda a: "stub-machine")  # the stub has no serial or address
        monkeypatch.setattr(bridge, "plate_occupancy", lambda a: state)
        monkeypatch.setattr(bridge, "plan_motion_around_plate", lambda *a: None)
        return adapter, PlateState

    def test_an_unknown_plate_asks_before_the_press_and_never_before_a_travel(self, no_kiln_pro, monkeypatch):
        adapter, _ = self._gate(monkeypatch)
        with pytest.raises(PlateClearRequired, match="onto the PLATE"):
            adapter._plate_gate({}, station=self._STATION, action="home", touches_plate=True)
        assert adapter._plate_gate({}, station=self._STATION, action="park") is None
        assert adapter._plate_gate({"plate_clear": True}, station=self._STATION, action="home", touches_plate=True) is None

    def test_a_served_clear_record_answers_the_row_but_not_the_press(self, monkeypatch):
        adapter, PlateState = self._gate(monkeypatch)
        clear = PlateState(machine="m", status="clear", source="human", since="2026-09-16T18:12:00")
        monkeypatch.setattr("kiln._pro_motion_bridge.plate_occupancy", lambda a: clear)
        assert adapter._plate_gate({}, station=self._STATION, action="park") is None
        with pytest.raises(PlateClearRequired) as info:
            adapter._plate_gate({}, station=self._STATION, action="home", touches_plate=True)
        assert "a person said so" in str(info.value) and "every time" in str(info.value)

    def test_a_served_part_taller_than_the_raise_refuses_the_travel(self, monkeypatch):
        from kiln.plate_state import PlateJob

        adapter, PlateState = self._gate(monkeypatch)
        tall = PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-16T18:12:00",
                          job=PlateJob(file="vase.gcode", max_z_mm=60.0))
        monkeypatch.setattr("kiln._pro_motion_bridge.plate_occupancy", lambda a: tall)
        with pytest.raises(PlateClearRequired, match="vase.gcode.*60 mm tall.*10 mm"):
            adapter._plate_gate({}, station=self._STATION, action="park")
        short = PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-16T18:12:00",
                           job=PlateJob(file="coin.gcode", max_z_mm=3.0))
        monkeypatch.setattr("kiln._pro_motion_bridge.plate_occupancy", lambda a: short)
        assert adapter._plate_gate({}, station=self._STATION, action="park") is None


class TestPlateRecordPublicFace:
    """Without the served record every plate reads as unknown, nothing is
    written, and every door says so instead of pretending."""

    def test_reads_unknown_and_records_nothing(self, no_kiln_pro, bambu):
        from kiln.plate_state import mark_clear, mark_occupied, mark_occupied_by_start, plate_occupancy

        state = plate_occupancy(bambu)
        assert state.status == "unknown" and "served through Kiln's hosted service" in state.note
        assert mark_clear(bambu, "human") is False
        assert mark_occupied(bambu, None, source="print_ended") is False
        assert mark_occupied_by_start(bambu, "part.3mf") is False

    def test_kiln_plate_clear_says_it_could_not_record(self, no_kiln_pro, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import plate_status, run_plate

        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        out = run_plate(action="clear")
        assert out["success"] is False and out["error"]["code"] == "PLATE_RECORD_FAILED"
        assert "served through Kiln's hosted service" in out["error"]["message"]
        assert plate_status()["plate"]["status"] == "unknown"

    def test_a_served_record_passes_through_the_public_face(self, bambu, monkeypatch):
        from kiln import _pro_motion_bridge as bridge
        from kiln.plate_state import PlateJob, PlateState, plate_occupancy

        served = PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-16T18:12:00",
                            job=PlateJob(file="vase.gcode", max_z_mm=60.0))
        monkeypatch.setattr(bridge, "plate_occupancy", lambda a: served)
        assert plate_occupancy(bambu) is served
        assert "vase.gcode" in served.describe() and "60 mm tall" in served.describe()


class TestDoctorWithoutTheServedSequence:
    """``kiln doctor`` asks the same gate the tools ask, so on this install a
    Bambu reads as refused-by-name with the served line, and a generic
    backend reads as the firmware's own routine."""

    def test_a_bambu_reads_as_refused_with_the_served_line(self, no_kiln_pro, bambu, monkeypatch):
        from kiln.cli.main import _doctor_filament_where, _doctor_homing_how

        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        where, warn = _doctor_filament_where(bambu)
        assert warn is True and "in place" in where and "wipe_nozzle refuses" in where and "bambu_a1" in where
        detail, warn = _doctor_homing_how(bambu)
        assert warn is True and "both refuse" in detail and "jog controls" in detail and "bambu_a1" in detail

    def test_a_generic_backend_reads_as_the_firmware_routine(self, no_kiln_pro, monkeypatch):
        from kiln.cli.main import _doctor_filament_where, _doctor_homing_how

        adapter = _build("octoprint")
        detail, warn = _doctor_homing_how(adapter)
        assert warn is False and "firmware's own homing routine" in detail and "park_head (kiln park) parks at the firmware's own home" in detail
        where, warn = _doctor_filament_where(adapter)
        assert warn is True and "in place" in where and "wipe_nozzle refuses" in where
