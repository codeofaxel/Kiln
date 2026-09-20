"""``motion_outcomes`` -- what a served motion plan did on a real machine.

A sequence derived from a vendor's start file earns its bench on machines
Kiln never sees: the walk happens on the owner's own printer and no
server is in the loop.  So the executor every plan runs through counts
the outcome -- how far a step walk got, whether the whole walk ran clean,
which fault code stopped it, why a run was refused -- as classes in the
daily stats, and the heartbeat carries them like every other counter.

Pinned here:

* the key is three tokens and nothing else can be spelled in it -- no
  coordinate, serial or line of G-code survives the shape;
* ``completed_all_steps`` means every step in order with none skipped and
  none faulted, tracked in local-only bookkeeping that never ships;
* every executor door records, a detour and a plan-only call record
  nothing, and a counter never blocks a motion;
* the map survives midnight and leaves the machine.
"""

from __future__ import annotations

import time

import pytest

from kiln import daily_stats
from kiln.printers.base import FilamentHandlingUnsupported, HomingUnsupported

from .test_filament_handling import (  # noqa: F401
    _fake_wipe_doc,
    _hot,
    _scripts,
    _serve_plans,
    bambu,
)
from .test_home_axes import _fake_home_doc, _idle, _serve
from .test_wipe_steps import _steps_doc

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name


@pytest.fixture(autouse=True)
def _own_stats_file(tmp_path, monkeypatch):
    """Record into a private file: a custom path is never suppressed, and
    nothing here can reach the developer's real counters."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))


def _outcomes() -> dict[str, int]:
    return dict(daily_stats.get_daily_stats().get("motion_outcomes", {}))


def _walks() -> dict:
    return dict(daily_stats._read().get("motion_walks", {}))


class TestTheKey:
    def test_three_tokens_and_nothing_else(self):
        daily_stats.record_motion_outcome("Bambu Lab A1 mini", "wipe", "step_sent", 3)
        daily_stats.record_motion_outcome("bambu_a1", "home", "full_run")
        daily_stats.record_motion_outcome("bambu_a1", "wipe", "fault", "0300_0D00-0001 0001")
        daily_stats.record_motion_outcome("bambu_a1", "wipe", "refused", "step mode only")
        daily_stats.record_motion_outcome("bambu_a1", "wipe", "refused")
        daily_stats.record_motion_outcome("bambu_a1", "wipe", "completed_all_steps")
        assert _outcomes() == {
            "bambu_lab_a1_mini|wipe|step_sent_3": 1,
            "bambu_a1|home|full_run": 1,
            "bambu_a1|wipe|fault_0300_0d00_0001_0001": 1,
            "bambu_a1|wipe|refused_step_mode_only": 1,
            "bambu_a1|wipe|refused": 1,
            "bambu_a1|wipe|completed_all_steps": 1,
        }

    @pytest.mark.parametrize("model, verb, kind, detail", [
        ("bambu_a1", "jog", "full_run", None),          # not a verb
        ("bambu_a1", "wipe", "landed", None),           # not an outcome
        ("bambu_a1", "wipe", "step_sent", 0),           # steps start at 1
        ("bambu_a1", "wipe", "step_sent", 1000),        # and stop before four digits
        ("bambu_a1", "wipe", "step_sent", "3"),         # a number, not text
        ("bambu_a1", "wipe", "step_sent", True),        # a bool is not a step
        ("bambu_a1", "wipe", "full_run", "G1 X-13.5"),  # a detail on a kind that takes none is ignored, not spelled
    ])
    def test_a_key_that_is_not_of_the_shape_is_dropped_or_folded(self, model, verb, kind, detail):
        daily_stats.record_motion_outcome(model, verb, kind, detail)
        out = _outcomes()
        assert all("|" in k and k.count("|") == 2 for k in out)
        assert not any("X-13.5" in k or " " in k or "." in k for k in out)
        assert out in ({}, {"bambu_a1|wipe|full_run": 1})

    def test_a_coordinate_a_serial_or_a_line_of_gcode_cannot_be_spelled(self):
        daily_stats.record_motion_outcome("bambu_a1", "wipe", "fault", "G1 X-13.5 F3000 ; serial 01P00A000000001")
        (key,) = _outcomes()
        assert key == "bambu_a1|wipe|fault_g1_x_13_5_f3000_serial_01p00a000"  # folded and cut at 32
        assert daily_stats._MOTION_KEY_RE.match(key)
        assert not daily_stats._MOTION_KEY_RE.match("bambu_a1|wipe|G1 X-13.5")
        assert not daily_stats._MOTION_KEY_RE.match("bambu_a1|wipe|step_sent_3|extra")

    def test_split_reads_what_the_token_says(self):
        assert daily_stats.split_motion_outcome("step_sent_12") == ("step_sent", "12")
        assert daily_stats.split_motion_outcome("fault_0300_0d00") == ("fault", "0300_0d00")
        assert daily_stats.split_motion_outcome("refused") == ("refused", None)
        assert daily_stats.split_motion_outcome("refused_no_plan") == ("refused", "no_plan")
        assert daily_stats.split_motion_outcome("completed_all_steps") == ("completed_all_steps", None)
        assert daily_stats.split_motion_outcome("full_run") == ("full_run", None)
        assert daily_stats.split_motion_outcome("step_sent") is None
        assert daily_stats.split_motion_outcome("bogus") is None and daily_stats.split_motion_outcome(3) is None
        for kind in daily_stats.MOTION_OUTCOMES:
            token = daily_stats.motion_outcome_token(kind, 2 if kind == "step_sent" else "x")
            assert token and daily_stats.split_motion_outcome(token)[0] == kind

    def test_distinct_keys_are_capped(self, monkeypatch):
        monkeypatch.setattr(daily_stats, "_MOTION_OUTCOMES_MAX_DISTINCT", 3)
        for step in range(1, 6):
            daily_stats.record_motion_outcome("bambu_a1", "home", "step_sent", step)
        daily_stats.record_motion_outcome("bambu_a1", "home", "step_sent", 1)
        out = _outcomes()
        assert len(out) == 3 and out["bambu_a1|home|step_sent_1"] == 2

    def test_the_map_survives_midnight_and_is_returned_without_the_walks(self, monkeypatch):
        daily_stats.record_motion_step("bambu_a1", "home", 1, 3)
        stats = daily_stats.get_daily_stats()
        assert stats["motion_outcomes"] == {"bambu_a1|home|step_sent_1": 1}
        assert "motion_walks" not in stats and _walks() == {"bambu_a1|home": _walks()["bambu_a1|home"]}
        fresh = daily_stats._archive_completed_day(daily_stats._read())
        assert fresh["previous"]["motion_outcomes"] == {"bambu_a1|home|step_sent_1": 1}
        assert fresh["motion_walks"]["bambu_a1|home"]["next"] == 2  # a walk spans midnight
        assert "motion_outcomes" in daily_stats._ROLLOVER_MAPS


class TestTheWalk:
    """``completed_all_steps`` is every step, in order, none skipped, none faulted."""

    def test_a_walk_in_order_completes(self):
        for step in (1, 2, 3):
            daily_stats.record_motion_step("bambu_a1_mini", "wipe", step, 3)
        assert _outcomes() == {
            "bambu_a1_mini|wipe|step_sent_1": 1, "bambu_a1_mini|wipe|step_sent_2": 1,
            "bambu_a1_mini|wipe|step_sent_3": 1, "bambu_a1_mini|wipe|completed_all_steps": 1,
        }
        assert _walks() == {}  # the walk is over

    def test_a_skipped_step_is_not_a_walk(self):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 3, 3)
        assert "bambu_a1_mini|wipe|completed_all_steps" not in _outcomes()
        assert _outcomes()["bambu_a1_mini|wipe|step_sent_3"] == 1

    def test_the_last_step_alone_is_not_a_walk(self):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 3, 3)
        assert _outcomes() == {"bambu_a1_mini|wipe|step_sent_3": 1}

    def test_a_fault_ends_the_walk(self):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 2, 3)
        daily_stats.record_motion_fault("bambu_a1_mini", "wipe", "0300_1A00_0002_0001")
        assert _walks() == {}
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 3, 3)
        out = _outcomes()
        assert "bambu_a1_mini|wipe|completed_all_steps" not in out
        assert out["bambu_a1_mini|wipe|fault_0300_1a00_0002_0001"] == 1

    def test_starting_over_restarts_the_walk(self):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 2, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 3)  # again from the top
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 2, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 3, 3)
        assert _outcomes()["bambu_a1_mini|wipe|completed_all_steps"] == 1

    def test_a_walk_abandoned_for_hours_is_over(self, monkeypatch):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 3)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 2, 3)
        later = time.time() + daily_stats._MOTION_WALK_TTL_S + 1
        monkeypatch.setattr(time, "time", lambda: later)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 3, 3)
        assert "bambu_a1_mini|wipe|completed_all_steps" not in _outcomes()

    def test_walks_are_per_model_and_verb(self):
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 2)
        daily_stats.record_motion_step("bambu_a1", "wipe", 1, 2)
        daily_stats.record_motion_step("bambu_a1_mini", "home", 1, 2)
        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 2, 2)
        out = _outcomes()
        assert out["bambu_a1_mini|wipe|completed_all_steps"] == 1
        assert "bambu_a1|wipe|completed_all_steps" not in out and "bambu_a1_mini|home|completed_all_steps" not in out
        assert set(_walks()) == {"bambu_a1|wipe", "bambu_a1_mini|home"}

    def test_a_one_step_plan_completes_on_its_step(self):
        daily_stats.record_motion_step("bambu_a1", "park", 1, 1)
        assert _outcomes() == {"bambu_a1|park|step_sent_1": 1, "bambu_a1|park|completed_all_steps": 1}

    def test_a_step_outside_the_plan_records_nothing(self):
        daily_stats.record_motion_step("bambu_a1", "park", 4, 3)
        daily_stats.record_motion_step("bambu_a1", "park", 0, 3)
        daily_stats.record_motion_step("bambu_a1", "park", True, 3)
        assert _outcomes() == {} and _walks() == {}

    def test_the_walk_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(daily_stats, "_MOTION_WALKS_MAX", 2)
        for model in ("m1", "m2", "m3"):
            daily_stats.record_motion_step(model, "home", 1, 3)
        assert len(_walks()) == 2 and "m1|home" not in _walks()


class TestTheDoors:
    """Every executor door records; a detour and a plan-only call do not."""

    def test_a_home_walked_step_by_step_records_the_walk(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        assert bambu.home_axes(plan_only=True).success and _outcomes() == {}
        for step in (1, 2, 3, 4):
            assert bambu.home_axes(step=step).success
        assert _outcomes() == {
            "bambu_a1|home|step_sent_1": 1, "bambu_a1|home|step_sent_2": 1,
            "bambu_a1|home|step_sent_3": 1, "bambu_a1|home|step_sent_4": 1,
            "bambu_a1|home|completed_all_steps": 1,
        }

    def test_a_home_run_whole_is_a_full_run_and_a_park_is_its_own_verb(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc(), "park": _fake_home_doc(verb="park")})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        assert bambu.home_axes().success and bambu.park_head().success
        assert _outcomes() == {"bambu_a1|home|full_run": 1, "bambu_a1|park|full_run": 1}

    def test_a_fault_records_its_code_and_ends_the_walk(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        assert bambu.home_axes(step=1).success
        faults = iter([set(), {("0300_1A00_0002_0001", "hms")}])
        monkeypatch.setattr(bambu, "_snapshot_faults", lambda: next(faults, {("0300_1A00_0002_0001", "hms")}))
        assert bambu.home_axes(step=2).success is False
        out = _outcomes()
        assert out["bambu_a1|home|fault_0300_1a00_0002_0001"] == 1 and "bambu_a1|home|step_sent_2" not in out
        assert _walks() == {}

    def test_a_script_the_printer_refuses_is_a_rejected_fault(self, bambu, monkeypatch):
        from kiln.printers.command_verdict import CommandVerdict

        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(bambu, "send_gcode", lambda cmds: CommandVerdict(state="failed", message="no"))
        assert bambu.home_axes().success is False
        assert _outcomes() == {"bambu_a1|home|fault_rejected": 1}

    def test_a_detour_around_a_part_records_nothing(self, bambu, monkeypatch):
        from kiln.plate_state import PlateJob, mark_occupied

        from .test_motion_gate import _facts

        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        mark_occupied(bambu, PlateJob(file="vase.gcode", footprint_mm=[100, 100, 150, 150], max_z_mm=60.0))
        # The door reads every detour against the part before it runs one,
        # and no Bambu record states what an unhomed move does -- so this
        # bench stands a record in that does, and offers a plan that lifts
        # clear of the 60 mm part before it travels.
        monkeypatch.setattr(bambu, "motion_facts", lambda: _facts(
            printer_id="bambu_a1", z_home_method="nozzle_contact_plate", z_home_xy_mm=(128.0, 254.0),
            unhomed_move_policy="clamped", z_travel_limit_mm=256.0,
        ))
        detour = [{"label": "around", "you_will_see": "x", "stops_when": "y",
                   "gcode": ["G91", "G1 Z62 F300", "G90", "G28 X"], "leaves": []}]
        monkeypatch.setattr("kiln.plate_state.plan_motion_around_plate", lambda *a, **k: detour)
        result = bambu.home_axes(axes="XY")
        assert result.success and result.sequence_source == "kiln_pro_motion_plan"
        assert _scripts(bambu) == ["G91\nG1 Z62 F300\nG90\nG28 X"]
        assert _outcomes() == {}

    def test_no_plan_is_a_refusal_by_reason(self, bambu, monkeypatch):
        _serve(monkeypatch, None)
        bambu._printer_model = "bambu_h2s"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported):
            bambu.home_axes()
        with pytest.raises(HomingUnsupported):
            bambu.park_head()
        assert _outcomes() == {"bambu_h2s|home|refused_no_plan": 1, "bambu_h2s|park|refused_no_plan": 1}

    def test_a_wipe_walked_step_by_step_records_the_walk_and_its_refusals(self, bambu, monkeypatch):
        doc = _steps_doc(unbenched=True)
        _serve_plans(monkeypatch, {"wipe": doc})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch, cold=110.0)
        with pytest.raises(FilamentHandlingUnsupported):
            bambu.wipe_nozzle()
        assert bambu.wipe_nozzle(plan_only=True).success
        assert bambu.wipe_nozzle(step=3).success is False  # cold: the heat step was skipped
        for step in range(1, 7):
            assert bambu.wipe_nozzle(step=step).success, step
        out = _outcomes()
        assert out["bambu_a1|wipe|refused_step_mode_only"] == 1 and out["bambu_a1|wipe|refused_cold_extruder"] == 1
        assert [out[f"bambu_a1|wipe|step_sent_{n}"] for n in range(1, 7)] == [1] * 6
        assert out["bambu_a1|wipe|completed_all_steps"] == 1
        assert "bambu_a1|wipe|full_run" not in out

    def test_a_wipe_run_whole_is_a_full_run_and_a_fault_is_its_code(self, bambu, monkeypatch):
        _serve_plans(monkeypatch, {"wipe": _fake_wipe_doc()})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch, cold=110.0)
        assert bambu.wipe_nozzle().success
        bambu._mqtt_client.publish.reset_mock()
        faults = iter([set(), {("0300_1A00_0002_0001", "hms")}])
        monkeypatch.setattr(bambu, "_snapshot_faults", lambda: next(faults, {("0300_1A00_0002_0001", "hms")}))
        assert bambu.wipe_nozzle().success is False
        assert _outcomes() == {"bambu_a1|wipe|full_run": 1, "bambu_a1|wipe|fault_0300_1a00_0002_0001": 1}

    def test_a_wipe_with_no_plan_is_a_refusal(self, bambu, monkeypatch):
        _serve_plans(monkeypatch, {})
        bambu._printer_model = "bambu_h2s"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        with pytest.raises(FilamentHandlingUnsupported):
            bambu.wipe_nozzle()
        assert _outcomes() == {"bambu_h2s|wipe|refused_no_plan": 1}

    def test_a_purge_that_parked_is_a_full_run_and_one_in_place_is_not(self, bambu, monkeypatch):
        from .test_filament_handling import _fake_purge_doc

        _serve_plans(monkeypatch, {"purge": _fake_purge_doc()})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        assert bambu.purge_filament(length_mm=10).details["purge_station"]["status"] == "parked"
        assert _outcomes() == {"bambu_a1|purge|full_run": 1}
        _serve_plans(monkeypatch, {})
        bambu._plan_memo = {}
        bambu._mqtt_client.publish.reset_mock()
        assert bambu.purge_filament(length_mm=10).details["purge_station"]["status"] == "in_place"
        assert _outcomes() == {"bambu_a1|purge|full_run": 1}

    def test_a_counter_that_fails_never_blocks_the_motion(self, bambu, monkeypatch):
        _serve(monkeypatch, {"home": _fake_home_doc()})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(daily_stats, "record_motion_outcome", _boom)
        monkeypatch.setattr(daily_stats, "record_motion_step", _boom)
        assert bambu.home_axes().success and bambu.home_axes(step=1).success

    def test_the_heartbeat_carries_it(self, monkeypatch):
        from kiln import heartbeat

        daily_stats.record_motion_step("bambu_a1_mini", "wipe", 1, 1)
        shipped = heartbeat._top_n(daily_stats.get_daily_stats()["motion_outcomes"], 100)
        assert shipped == {"bambu_a1_mini|wipe|step_sent_1": 1, "bambu_a1_mini|wipe|completed_all_steps": 1}
