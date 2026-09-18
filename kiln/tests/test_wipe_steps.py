"""``wipe_nozzle`` in steps, with a person's word for the plate.

A served wipe plan may describe itself as steps.  The doors then offer
three things the full run does not: ``plan_only`` (the steps, nothing
sent), ``step=N`` (one step sent, the next described, the finish held
until the last), and ``plate_clear`` -- a PERSON's statement, read on
every call by a wipe whose plan presses the plate, whatever the plate
record says.  A plan no one has run on a real machine says so
(``step_mode_only``) and runs only in steps.

Every document here is made up: the shape, never a sequence.  The real
plans, their figures and the reasons they press the plate are served, and
pinned where they live.
"""

from __future__ import annotations

import json

import pytest

from kiln.printers.base import (
    FilamentHandlingUnsupported,
    PlateClearRequired,
    PrinterError,
    PrinterState,
    PrinterStatus,
)

from .test_filament_handling import (  # noqa: F401
    _fake_purge_doc,
    _fake_wipe_doc,
    _hot,
    _scripts,
    _serve_plans,
    bambu,
    door,
)

# ruff: noqa: F811  -- `bambu` and `door` are fixtures, re-used by name


@pytest.fixture(autouse=True)
def _fresh_plate_record(tmp_path, monkeypatch):
    """Every test starts with no plate record (it is written to disk on purpose)."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))


def _steps_doc(printer_id: str = "bambu_a1", *, presses_plate: bool = False, unbenched: bool = False) -> dict:
    """A wipe plan described as steps, with made-up lines: park, heat, snap, two passes, park."""
    doc = _fake_wipe_doc(printer_id)
    passes = doc["post_gcode"]
    doc["steps"] = [
        {"number": 1, "label": "park while cold", "you_will_see": "the head lifts and goes left",
         "stops_when": "the moves end", "gcode": list(doc["pre_gcode"]), "leaves": ["limits pushed"]},
        {"number": 2, "label": "heat", "you_will_see": "no motion; the nozzle heats", "stops_when": "it reaches temperature",
         "gcode": [], "leaves": ["heater ON"], "heats": True},
        {"number": 3, "label": "snap the tail", "you_will_see": "no motion of the head; the extruder pulls back",
         "stops_when": "the retract ends", "gcode": ["M83", "G1 E-1 F500", "M82"], "leaves": ["heater ON"]},
        {"number": 4, "label": "cool and take the datum", "you_will_see": "the head goes to the pad and touches down",
         "stops_when": "the probe stops", "gcode": passes[:6], "leaves": ["heater ON"], "touches_plate": presses_plate},
        {"number": 5, "label": "the passes", "you_will_see": "the head scrubs", "stops_when": "the strokes end",
         "gcode": passes[6:8], "leaves": ["heater ON"]},
        {"number": 6, "label": "wait and park", "you_will_see": "the head lifts and goes to the chute",
         "stops_when": "the fan stops", "gcode": passes[8:], "leaves": ["heater at the hand-off, switched off by the answer"]},
    ]
    if presses_plate:
        doc["z_home_on_plate"] = True
        doc["details"]["needs_plate_clear"] = "takes its Z datum on the plate and crosses it to reach the pad"
    if unbenched:
        doc["details"]["bench"] = {"status": "unbenched", "note": "derived from a file; no machine on the bench"}
        doc["step_mode_only"] = {"reason": "the wipe is derived from the vendor's start file and has not been run on a real machine. Bench it: plan_only=true (kiln filament wipe --plan), then step=1, step=2, ... (--step N)"}
    return doc


def _ready(bambu, monkeypatch, doc, **more):
    _serve_plans(monkeypatch, {"wipe": doc, **more})
    bambu._printer_model = doc["printer_id"]
    bambu._last_status["ams"]["tray_now"] = "0"
    _hot(bambu, monkeypatch, cold=110.0)  # the fake plan's hand-off is 120
    return bambu


class TestPlanOnly:
    def test_describes_the_steps_and_sends_nothing(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        result = bambu.wipe_nozzle(plan_only=True)
        assert result.success and result.details["sent"] is False and _scripts(bambu) == []
        assert [s["number"] for s in result.steps] == [1, 2, 3, 4, 5, 6] and result.step_sent is None
        assert result.next_step["number"] == 1 and result.next_step["label"] == "park while cold"
        assert result.details["heater"] == "untouched: nothing was sent" and bambu._mqtt_client.publish.call_count == 0
        assert "6 steps" in result.message and "step=1 … step=6" in result.message and "all at once with no step named" in result.message
        assert result.details["wipe_c"] == 150 and "resting_position" not in result.details

    def test_a_plan_without_steps_refuses_step_mode_by_name(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _fake_wipe_doc())
        for options in ({"plan_only": True}, {"step": 1}):
            with pytest.raises(PrinterError, match="describes no steps"):
                bambu.wipe_nozzle(**options)
        assert _scripts(bambu) == []
        assert bambu.wipe_nozzle().success  # the full run is untouched


class TestStepMode:
    def test_one_step_is_sent_and_the_next_described_with_the_finish_held(self, bambu, monkeypatch):
        doc = _ready(bambu, monkeypatch, _steps_doc()) and _steps_doc()
        result = bambu.wipe_nozzle(step=1)
        assert result.success and result.step_sent == 1 and result.next_step["number"] == 2
        assert _scripts(bambu) == ["\n".join(doc["pre_gcode"])]
        assert result.leaves == ["limits pushed"] and "Left armed: limits pushed." in result.message
        assert result.details["heater"] == "as the step left it -- see leaves; the finish runs after the last step"
        assert "Next: step 2 -- heat" in result.message and "M104 S0" not in _scripts(bambu)
        assert "end_retract_mm" not in result.details and "finish" not in result.details

    def test_the_heat_step_sets_the_gated_temperature_and_waits_on_the_thermistor(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        result = bambu.wipe_nozzle(step=2, material="PLA")
        assert result.success and result.details["hotend_reading"] == 210 and result.details["gcode"] == []
        assert _scripts(bambu) == ["M104 S210"]
        assert result.temperature == 210 and result.leaves == ["heater ON"]

    def test_the_snap_on_a_cold_nozzle_refuses_and_names_the_heat_step(self, bambu, monkeypatch):
        """A person can call step=3 without step=2; the extruder move still
        needs the thermistor at temperature, read once, no wait."""
        _ready(bambu, monkeypatch, _steps_doc())
        result = bambu.wipe_nozzle(step=3)
        assert result.success is False and result.verification_source == "thermistor"
        assert "moves the extruder" in result.message and "run step 2 (heat) first" in result.message
        assert "Nothing was sent" in result.message and _scripts(bambu) == []
        assert result.details["last_hotend_reading"] == 25.0 and result.step_sent == 3

    def test_the_snap_after_the_heat_sends_the_plans_own_retract(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        assert bambu.wipe_nozzle(step=2).success
        result = bambu.wipe_nozzle(step=3)
        assert result.success and _scripts(bambu)[-1] == "M83\nG1 E-1 F500\nM82"

    def test_the_last_step_runs_the_finish_once_and_reports_the_rest(self, bambu, monkeypatch):
        doc = _steps_doc()
        _ready(bambu, monkeypatch, doc)
        assert bambu.wipe_nozzle(step=2).success
        result = bambu.wipe_nozzle(step=6)
        assert result.success and result.next_step is None and result.step_sent == 6
        scripts = _scripts(bambu)
        assert scripts[-2:] == ["M104 S0", "M106 S255"]  # heater off, then the plan's cool-down begins
        assert "\n".join(doc["post_gcode"][8:]) in scripts
        assert not any("G1 E-0.8 F1800" in s for s in scripts)  # the snap step already pulled back
        assert result.details["end_retract_mm"] == 1.0 and result.details["heater"] == "off"
        assert result.details["cooldown"]["handoff_c"] == 120 and result.details["resting_position"] == {"over": "the chute"}
        # ... and finishes from a thread the request cannot take with it
        from kiln.printers import routine_ledger

        assert routine_ledger.wait_settled(5.0) and _scripts(bambu)[-1] == "M106 S0"
        assert result.details["plate_clear_given"] is False  # the plan never asked
        assert "That was the last step" in result.message

    def test_a_step_the_firmware_refuses_says_which(self, bambu, monkeypatch):
        from kiln.printers.command_verdict import CommandVerdict

        _ready(bambu, monkeypatch, _steps_doc())
        monkeypatch.setattr(bambu, "send_gcode", lambda cmds: CommandVerdict(state="failed", message="unknown command"))
        result = bambu.wipe_nozzle(step=1)
        assert result.success is False and result.verification_source == "firmware_rejected_move"
        assert "refused step 1 (park while cold)" in result.message and result.details["verdict"]["accepted"] is False

    @pytest.mark.parametrize("step", [0, -1, "2", 2.5, True])
    def test_a_step_that_is_not_a_whole_number_from_one_is_refused_before_anything(self, bambu, monkeypatch, step):
        _ready(bambu, monkeypatch, _steps_doc())
        with pytest.raises(PrinterError, match="whole number from 1"):
            bambu.wipe_nozzle(step=step)
        assert bambu._mqtt_client.publish.call_count == 0

    def test_a_step_past_the_end_is_refused_by_count(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        with pytest.raises(PrinterError, match="has 6 steps; step 7"):
            bambu.wipe_nozzle(step=7)
        assert _scripts(bambu) == []

    def test_the_full_run_of_a_step_plan_carries_the_steps_beside_the_answer(self, bambu, monkeypatch):
        doc = _steps_doc()
        _ready(bambu, monkeypatch, doc)
        result = bambu.wipe_nozzle()
        assert result.success and len(result.steps) == 6 and result.step_sent is None and result.next_step is None
        assert any(s == "M83\nG1 E-1 F500\n" + "\n".join(doc["post_gcode"]) + "\nM82" for s in _scripts(bambu))

    def test_a_malformed_step_is_refused_before_anything(self, bambu, monkeypatch):
        doc = _steps_doc()
        doc["steps"][3]["gcode"] = []  # empty, and it does not heat
        _ready(bambu, monkeypatch, doc)
        with pytest.raises(PrinterError, match="empty step"):
            bambu.wipe_nozzle(plan_only=True)
        for bad in ("G28", ["G28", "  "], ["G28", 7]):
            doc["steps"][3]["gcode"] = bad
            with pytest.raises(PrinterError, match="no G-code list"):
                bambu.wipe_nozzle(step=1)
        assert _scripts(bambu) == []


class TestStepModeOnly:
    """A plan derived from a file that no one has run: the full run refuses
    in the plan's own words; plan_only and step=N are how it is benched."""

    def test_the_full_run_refuses_and_names_step_mode(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc(unbenched=True))
        with pytest.raises(FilamentHandlingUnsupported) as info:
            bambu.wipe_nozzle()
        text = str(info.value)
        assert text.startswith("Kiln will not run the wipe on bambu_a1 in one go: the wipe is derived")
        assert "kiln filament wipe --plan" in text and "--step N" in text
        assert "wipe from the printer's own screen" in text and "start a print" in text
        assert _scripts(bambu) == []

    def test_plan_only_and_a_step_still_run(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc(unbenched=True))
        plan = bambu.wipe_nozzle(plan_only=True)
        assert plan.success and "step mode only" in plan.message and "all at once" not in plan.message
        assert plan.details["bench"]["status"] == "unbenched"
        assert bambu.wipe_nozzle(step=1).success and _scripts(bambu) != []

    def test_a_benched_plan_says_nothing_of_the_kind(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        assert bambu.wipe_nozzle().success


class TestPlateClear:
    """A wipe whose plan presses the plate asks a person on every call."""

    def test_asks_before_a_full_run_a_step_or_a_plan(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc(presses_plate=True))
        for options in ({}, {"step": 1}, {"plan_only": True}):
            with pytest.raises(PlateClearRequired) as info:
                bambu.wipe_nozzle(**options)
            text = str(info.value)
            assert text.startswith("bambu_a1 takes its Z datum on the plate and crosses it to reach the pad.")
            assert "plate_clear=true on wipe_nozzle (kiln filament wipe --plate-clear)" in text
            assert "wipe from the printer's own screen" in text and "start a print" in text
            assert "home_axes" not in text and "axes=" not in text and "park_head" not in text
        assert bambu._mqtt_client.publish.call_count == 0

    def test_a_recorded_clear_plate_does_not_stand_in_for_the_press(self, bambu, monkeypatch):
        from kiln.plate_state import mark_clear

        _ready(bambu, monkeypatch, _steps_doc(presses_plate=True))
        mark_clear(bambu, "human")
        with pytest.raises(PlateClearRequired) as info:
            bambu.wipe_nozzle(step=1)
        text = str(info.value)
        assert "cannot see a print started from the printer's own screen" in text and "every time" in text
        assert "plate_clear=true on wipe_nozzle (kiln filament wipe --plate-clear)" in text
        assert "Without asking, wipe from the printer's own screen" in text

    def test_a_recorded_part_is_named_and_a_persons_word_given_now_outranks_the_record(self, bambu, monkeypatch):
        from kiln.plate_state import PlateJob, mark_occupied

        _ready(bambu, monkeypatch, _steps_doc(presses_plate=True))
        mark_occupied(bambu, PlateJob(file="vase.gcode", max_z_mm=3.0))
        with pytest.raises(PlateClearRequired, match="vase.gcode"):
            bambu.wipe_nozzle()
        assert bambu.wipe_nozzle(plate_clear=True).success  # the person looked, now; the record is written down

    def test_the_word_runs_it_and_the_answer_says_it_was_given(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc(presses_plate=True))
        plan = bambu.wipe_nozzle(plan_only=True, plate_clear=True)
        assert plan.success and plan.details["needs_plate_clear"].startswith("takes its Z datum")
        result = bambu.wipe_nozzle(plate_clear=True)
        assert result.success and result.details["plate_clear_given"] is True
        bambu._mqtt_client.publish.reset_mock()  # the finish switched the heater off; a fresh op heats again
        assert bambu.wipe_nozzle(step=2, plate_clear=True).success
        last = bambu.wipe_nozzle(step=6, plate_clear=True)
        assert last.success and last.details["plate_clear_given"] is True

    def test_a_step_that_touches_the_plate_asks_even_without_the_flag(self, bambu, monkeypatch):
        doc = _steps_doc(presses_plate=True)
        del doc["z_home_on_plate"]
        del doc["details"]["needs_plate_clear"]  # the default contact sentence, the homing one
        _ready(bambu, monkeypatch, doc)
        with pytest.raises(PlateClearRequired) as info:
            bambu.wipe_nozzle()
        assert "pressing the nozzle onto the PLATE" in str(info.value) and "wipe_nozzle" in str(info.value)

    def test_a_plan_that_never_touches_the_plate_never_asks(self, bambu, monkeypatch):
        _ready(bambu, monkeypatch, _steps_doc())
        result = bambu.wipe_nozzle()
        assert result.success and result.details["plate_clear_given"] is False


class TestTheDoors:
    """The MCP tool, the CLI and the doctor, all through the one door."""

    def test_the_tool_passes_the_three_words_to_the_adapter(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        door._wipe_nozzle_impl = door._record
        out = wipe_nozzle(temperature=200, plate_clear=True, step=2)
        assert out["success"] is True
        assert door.plans[0].options["plate_clear"] is True and door.plans[0].options["step"] == 2
        assert "plan_only" not in door.plans[0].options
        assert out["safety"]  # a step moves the machine hot: the same warning as the full run

    def test_plan_only_is_gated_as_a_read_and_carries_no_burn_warning(self, door, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        scopes: list[str] = []
        monkeypatch.setattr(srv, "_check_auth", lambda scope: scopes.append(scope))
        monkeypatch.setattr(srv, "_check_rate_limit", lambda tool: (_ for _ in ()).throw(AssertionError("rate-limited a read")))
        door._wipe_nozzle_impl = door._record
        out = wipe_nozzle(temperature=200, plan_only=True)
        assert out["success"] is True and scopes == ["read"]
        assert door.plans[0].options["plan_only"] is True and "safety" not in out  # nothing moved, nothing is hot

    def test_a_bad_step_is_an_error_envelope_not_a_crash(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        out = wipe_nozzle(temperature=200, step="2")
        assert out["success"] is False and "whole number from 1" in out["error"]["message"]

    def test_the_plate_refusal_is_its_own_code_with_the_frame(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        def _refuse(plan):
            raise PlateClearRequired("stub presses the plate", snapshot_path="/tmp/plate.jpg")

        door._wipe_nozzle_impl = _refuse
        out = wipe_nozzle(temperature=200)
        assert out["success"] is False and out["error"]["code"] == "PLATE_CLEAR_REQUIRED"
        assert out["plate_clear_required"] is True and out["snapshot_path"] == "/tmp/plate.jpg"
        assert out["outcome"] == "failed" and out["error"]["retryable"] is False

    def test_the_cli_carries_the_flags_and_the_code(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        door._wipe_nozzle_impl = door._record
        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200", "--plate-clear", "--step", "3", "--json"])
        assert result.exit_code == 0, result.output
        assert door.plans[-1].options["plate_clear"] is True and door.plans[-1].options["step"] == 3
        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200", "--plan", "--json"])
        assert result.exit_code == 0, result.output
        assert door.plans[-1].options["plan_only"] is True

        def _refuse(plan):
            raise PlateClearRequired("stub presses the plate", snapshot_path=None)

        door._wipe_nozzle_impl = _refuse
        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "PLATE_CLEAR_REQUIRED"
        result = CliRunner().invoke(cli, ["filament", "wipe", "--temp", "200"])
        assert result.exit_code == 1 and "stub presses the plate" in result.output

    def test_the_wipe_command_names_the_three_options(self):
        from kiln.cli.main import cli

        params = {p.name for p in cli.commands["filament"].commands["wipe"].params}
        assert {"plate_clear", "step", "plan_only"} <= params

    def test_doctor_says_what_the_wipe_costs(self, bambu, monkeypatch):
        from kiln.cli.main import _doctor_filament_where

        monkeypatch.setattr(bambu, "get_state", lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=25.0))
        bambu._printer_model = "bambu_a1"
        cases = [
            (_steps_doc(), False, False),
            (_steps_doc(presses_plate=True), True, False),
            (_steps_doc(unbenched=True), False, True),
            (_steps_doc(presses_plate=True, unbenched=True), True, True),
        ]
        for doc, consent, unbenched in cases:
            bambu._plan_memo = {}
            _serve_plans(monkeypatch, {"purge": _fake_purge_doc(), "wipe": doc})
            where, warn = _doctor_filament_where(bambu)
            assert warn is False and where.startswith("purge and load park over bambu_a1's own purge chute; wipe_nozzle")
            assert ("--plate-clear" in where and "every time: bambu_a1 takes its Z datum" in where) is consent, where
            assert ("in step mode only (kiln filament wipe --plan, then --step N)" in where and "a full run refuses" in where) is unbenched, where
        # purge refused, wipe ok: the wipe line still reads in full
        bambu._plan_memo = {}
        _serve_plans(monkeypatch, {"wipe": _steps_doc(presses_plate=True)})
        where, warn = _doctor_filament_where(bambu)
        assert warn is False and where.startswith("purge and load extrude in place on bambu_a1 and say so (")
        assert "--plate-clear" in where and "park over" not in where
        # nothing at all: the wipe's own reason, not the purge's
        bambu._plan_memo = {}
        _serve_plans(monkeypatch, {"purge": None, "wipe": {"schema": "motion_plan/1", "printer_id": "bambu_a1", "verb": "wipe", "ok": False,
                                                            "refusal": {"code": "UNSUPPORTED", "message": "the wipe is a firmware macro here"}}})
        where, warn = _doctor_filament_where(bambu)
        assert warn is True and "wipe_nozzle refuses (the wipe is a firmware macro here)" in where

    def test_the_envelope_carries_the_steps_and_the_next_one(self, door):
        from kiln.plugins.filament_handling_tools import wipe_nozzle
        from kiln.printers.base import FilamentOpResult

        steps = _steps_doc()["steps"]
        door._wipe_nozzle_impl = lambda plan: FilamentOpResult(
            success=True, action="wipe", message="step 1 sent", steps=steps, step_sent=1, next_step=steps[1], leaves=["limits pushed"],
        )
        out = wipe_nozzle(temperature=200, step=1)
        assert out["success"] is True and out["step_sent"] == 1 and out["next_step"]["number"] == 2
        assert [s["number"] for s in out["steps"]] == [1, 2, 3, 4, 5, 6] and out["leaves"] == ["limits pushed"]
        json.dumps(out)
