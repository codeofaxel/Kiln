"""Park and home run only a plan the door itself has read against the part.

Three gaps the planner's author measured in the public doors, closed
here and pinned:

1. **Park asks the planner.**  The generic park refused an occupied plate
   without asking kiln-pro's planner, while the Bambu emitter's park
   already asked.  Now the generic park asks the same way home does; a
   park plan lifts first and never descends over the footprint, and a
   planner that answers ``None`` leaves the refusal exactly as it was.
2. **Every plan is validated in the door.**  The home door ran whatever
   the planner answered; only the same-bed retry decision read the plan
   line by line.  Now the door reads every plan -- home and park, the
   generic template and the Bambu emitter -- against the plate record's
   footprint and height before anything is sent, and a plan that fails
   is refused in the validator's words with nothing moved.  A plan Kiln
   cannot read (no footprint, no height on record) never runs either.
3. **The planner plans for the connected machine.**  The hook passed no
   station, so the planner identified the machine from the record's
   ``job.printer_id`` -- the model declared when the print STARTED.  Now
   the door passes the model the adapter is declared as NOW; the planner
   refuses a mismatch.  And an adapter declared through
   ``set_safety_profile`` (every config.yaml printer that is not a Bambu)
   recorded no model at print start, so the planner had nothing to plan
   for; the record now carries the declared model however it was declared.

Every adapter here is a real :class:`PrinterAdapter` subclass running the
real templates; nothing is a MagicMock.  The planner is the real kiln-pro
one where the test says so (skipped on a public-only install) and a stub
where the test needs a plan the real planner would never give.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from kiln import plate_state
from kiln.plate_state import PlateJob, PlateState, mark_occupied, mark_occupied_by_start, plate_occupancy
from kiln.printers import home_around_part as hap
from kiln.printers.base import (
    HomeResult,
    PlateClearRequired,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
)

from .test_filament_handling import bambu  # noqa: F401  -- the Bambu emitter, MQTT mocked

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name

# An 80 x 80 mm part in the middle of a 220 mm bed, 40 mm tall.  The Ender
# 3's X/Y home corner (0, 0) and the Ender 3 V2's park spot (10, 210) both
# stand well clear of it.
PART = [90.0, 100.0, 170.0, 180.0]
HEIGHT = 40.0


class _Bench(PrinterAdapter):
    """A generic G-code backend with every line it sent written down.

    ``declare`` says how the model reaches the adapter: ``"printer_model"``
    the way the Bambu adapter keeps it, ``"safety_profile"`` the way every
    config.yaml door hands it over (:meth:`PrinterAdapter.set_safety_profile`),
    ``None`` for a machine nobody declared.
    """

    def __init__(self, *, model: str = "ender3", serial: str = "E3BENCH0001", declare: str | None = "printer_model") -> None:
        self.serial = serial
        if declare == "printer_model":
            self._printer_model = model
        elif declare == "safety_profile":
            self.set_safety_profile(model)
        self.sent: list[str] = []
        self.home_impl_calls: list[tuple[str, dict[str, Any]]] = []
        self.state = PrinterStatus.IDLE

    @property
    def name(self) -> str:
        return "bench"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=self.state)

    def send_gcode(self, commands: list[str]) -> Any:
        self.sent.extend(commands)
        return True

    def _home_axes_impl(self, axes: str, options: dict[str, Any]) -> HomeResult:
        self.home_impl_calls.append((axes, dict(options)))
        return super()._home_axes_impl(axes, options)

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        return PrintResult(success=True, message="IMPL")

    def get_tool_position(self) -> dict[str, Any] | None:
        return None

    def homed_axes_now(self) -> set[str] | None:
        return set()  # a power-cycled machine: nothing homed, and it says so

    def homed_axes_field(self) -> str | None:
        return "toolhead.homed_axes"


_Bench.__abstractmethods__ = frozenset()


class _Unlatched:
    def get_latch_status(self, printer_id: str) -> dict[str, Any]:
        return {"latched": False, "critical_interlocks_pending": []}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr("kiln.emergency.get_emergency_coordinator", lambda: _Unlatched())


@pytest.fixture
def real_planner():
    """kiln-pro's own planner, or skip: a public-only install has none."""
    bridge = pytest.importorskip("kiln_pro.bridge", reason="kiln-pro is not installed")
    if not callable(getattr(bridge, "plan_motion_around_plate", None)):
        pytest.skip("the installed kiln-pro predates the around-the-plate planner")
    return bridge


def _occupy(adapter: Any, *, model: str | None = "ender3", footprint: list[float] | None = PART,
            height: float | None = HEIGHT) -> None:
    mark_occupied(adapter, PlateJob(file="benchy.gcode", footprint_mm=footprint, max_z_mm=height, printer_id=model))


def _stub_planner(monkeypatch, plan: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Replace the hook with a planner that answers *plan* and records every ask."""
    asked: list[dict[str, Any]] = []

    def _plan(state, station, *, action, clearance_mm, **kw):
        asked.append({"action": action, "clearance_mm": clearance_mm, "status": state.status,
                      "station": station, **kw})
        return None if plan is None else [dict(s) for s in plan]

    monkeypatch.setattr(plate_state, "plan_motion_around_plate", _plan)
    return asked


def _plan(*lines: str) -> list[dict[str, Any]]:
    return [{"label": "the whole plan", "you_will_see": "-", "stops_when": "-", "gcode": list(lines), "leaves": []}]


# --- reading a plan's lines the way a person watching the head would ------


def _words(line: str) -> tuple[str, dict[str, float]]:
    head = line.split(";")[0].strip().upper()
    parts = head.split()
    return (parts[0] if parts else ""), {w[0]: float(w[1:]) for w in parts[1:] if w[0] in "XYZ"}


def _hits(p: tuple[float, float], q: tuple[float, float], rect: list[float]) -> bool:
    """Whether the axis-aligned segment *p*-*q* meets the closed box *rect*."""
    x0, y0, x1, y1 = rect
    if p[0] == q[0]:
        lo, hi = sorted((p[1], q[1]))
        return x0 <= p[0] <= x1 and not (hi < y0 or lo > y1)
    if p[1] == q[1]:
        lo, hi = sorted((p[0], q[0]))
        return y0 <= p[1] <= y1 and not (hi < x0 or lo > x1)
    raise AssertionError(f"a detour leg is not axis-aligned: {p} -> {q}")


def _assert_lifted_and_never_over_the_part(lines: list[str], *, footprint: list[float], height: float,
                                           home_corner: tuple[float, float]) -> None:
    """The plan lifts by more than the part before anything else, never
    descends, and every travel after the X/Y home misses the footprint plus
    the validator's own margin."""
    assert lines[0] == "G91", lines
    word, z = _words(lines[1])
    assert word == "G1" and set(z) == {"Z"} and z["Z"] >= height + hap.CLEARANCE_MARGIN_MM, lines
    assert lines[2] == "G90", lines
    assert lines[3].upper().startswith("G28 X Y"), lines
    grown = [footprint[0] - hap.PRESS_MARGIN_MM, footprint[1] - hap.PRESS_MARGIN_MM,
             footprint[2] + hap.PRESS_MARGIN_MM, footprint[3] + hap.PRESS_MARGIN_MM]
    x, y = home_corner
    for line in lines[4:]:
        word, coords = _words(line)
        assert not word.startswith("G28"), f"a park plan homes nothing after X and Y: {line}"
        assert "Z" not in coords, f"a park plan never moves Z again: {line}"
        assert word in ("G0", "G1"), line
        nx, ny = coords.get("X", x), coords.get("Y", y)
        assert not _hits((x, y), (nx, ny), grown), (line, (x, y), (nx, ny))
        x, y = nx, ny


# =========================================================================
# (1) park asks the planner, and runs what it validated
# =========================================================================


class TestParkRunsThePlannersDetour:
    @pytest.mark.parametrize("model", ["ender3", "ender3_v2"])
    def test_park_on_an_occupied_plate_runs_a_lifted_detour(self, real_planner, model):
        adapter = _Bench(model=model, serial=f"{model.upper()}BENCH01")
        _occupy(adapter, model=model)

        result = adapter.park_head()

        assert result.success, result.message
        assert result.action == "park"
        assert result.sequence_source == "kiln_pro_motion_plan"
        assert result.details["gcode"] == adapter.sent
        assert result.homed_axes == [], "a detour claims no axis homed; the firmware's read is the only proof"
        _assert_lifted_and_never_over_the_part(adapter.sent, footprint=PART, height=HEIGHT, home_corner=(0.0, 0.0))
        assert adapter.home_impl_calls == [], "the detour replaces the firmware's own X/Y home, it does not wrap it"
        assert plate_occupancy(adapter).occupied, "parking around a part does not remove the part"

    def test_plan_only_and_step_mode_walk_the_same_park_plan(self, real_planner):
        adapter = _Bench()
        _occupy(adapter)
        described = adapter.park_head(plan_only=True)
        assert described.success and adapter.sent == []
        assert described.sequence_source == "kiln_pro_motion_plan" and described.action == "park"
        assert described.steps[0]["label"] == "lift clear of the part"
        first = adapter.park_head(step=1)
        assert first.success and adapter.sent == described.steps[0]["gcode"]
        assert first.next_step["number"] == 2

    def test_park_with_no_plan_is_refused_as_before(self, monkeypatch, tmp_path):
        from kiln import daily_stats

        monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
        adapter = _Bench()
        _occupy(adapter)
        asked = _stub_planner(monkeypatch, None)
        with pytest.raises(PlateClearRequired, match="Refusing to park ender3.*benchy.gcode.*travels sideways"):
            adapter.park_head()
        assert adapter.sent == []
        assert asked and asked[0]["action"] == "park" and asked[0]["status"] == "occupied"
        assert daily_stats.get_daily_stats()["motion_refusals"] == {"ender3|PLATE_CLEAR_REQUIRED|plate_occupied": 1}

    def test_a_person_can_still_vouch_for_the_plate(self, monkeypatch):
        # A person's word, given now: the park is the firmware's own home,
        # as it was before the planner existed -- and the planner is not asked.
        adapter = _Bench()
        _occupy(adapter)
        asked = _stub_planner(monkeypatch, None)
        result = adapter.park_head(plate_clear=True)
        assert result.success and adapter.sent == ["G28"]
        assert asked == [], "a person's word on the call is not a question for the planner"


# =========================================================================
# (2) every plan is read against the part, in the door, before anything moves
# =========================================================================


TRAVELS_LOW = _plan("G28 X Y", "G91", f"G1 Z{HEIGHT + 10:g} F300", "G90")
DESCENDS_OVER_PART = _plan("G91", f"G1 Z{HEIGHT + 10:g} F300", "G90", "G28 X Y", "G1 X130 Y130 F3000", "G91", "G1 Z-20", "G90")
UNREADABLE = _plan("G91", f"G1 Z{HEIGHT + 10:g} F300", "G90", "SAFE_HOME_AROUND PART=1")
GOOD_PARK = _plan("G91", f"G1 Z{HEIGHT + 10:g} F300", "G90", "G28 X Y")
GOOD_HOME = _plan("G91", f"G1 Z{HEIGHT + 10:g} F300", "G90", "G28 X Y", "G1 X0 Y0 F3000", "G28 Z")


def _run(adapter: _Bench, action: str, **options: Any) -> HomeResult:
    return adapter.park_head(**options) if action == "park" else adapter.home_axes(axes="XYZ", **options)


class TestEveryPlanIsValidatedInTheDoor:
    @pytest.mark.parametrize("action", ["home", "park"])
    @pytest.mark.parametrize("bad, words", [
        (TRAVELS_LOW, "before the head is proven"),
        (DESCENDS_OVER_PART, "the head comes down"),
        (UNREADABLE, "cannot read as a motion"),
    ])
    def test_a_plan_that_fails_the_validator_is_refused_and_nothing_is_sent(self, monkeypatch, action, bad, words):
        adapter = _Bench()
        _occupy(adapter)
        _stub_planner(monkeypatch, bad)
        with pytest.raises(PlateClearRequired) as refused:
            _run(adapter, action)
        assert words in str(refused.value), str(refused.value)
        assert "benchy.gcode" in str(refused.value)
        assert adapter.sent == []

    @pytest.mark.parametrize("action", ["home", "park"])
    def test_plan_only_refuses_the_same_plan_it_would_not_run(self, monkeypatch, action):
        adapter = _Bench()
        _occupy(adapter)
        _stub_planner(monkeypatch, TRAVELS_LOW)
        with pytest.raises(PlateClearRequired, match="before the head is proven"):
            _run(adapter, action, plan_only=True)
        assert adapter.sent == []

    @pytest.mark.parametrize("action", ["home", "park"])
    def test_a_plan_for_a_part_of_unknown_footprint_is_not_run(self, monkeypatch, action):
        adapter = _Bench()
        _occupy(adapter, footprint=None)
        _stub_planner(monkeypatch, GOOD_HOME if action == "home" else GOOD_PARK)
        with pytest.raises(PlateClearRequired, match="where on the plate"):
            _run(adapter, action)
        assert adapter.sent == []

    @pytest.mark.parametrize("action", ["home", "park"])
    def test_a_plan_for_a_part_of_unknown_height_is_not_run(self, monkeypatch, action):
        adapter = _Bench()
        _occupy(adapter, height=None)
        _stub_planner(monkeypatch, GOOD_HOME if action == "home" else GOOD_PARK)
        with pytest.raises(PlateClearRequired, match="how tall"):
            _run(adapter, action)
        assert adapter.sent == []

    @pytest.mark.parametrize("action, good", [("home", GOOD_HOME), ("park", GOOD_PARK)])
    def test_a_plan_that_passes_the_validator_runs(self, monkeypatch, action, good):
        adapter = _Bench()
        _occupy(adapter)
        _stub_planner(monkeypatch, good)
        result = _run(adapter, action)
        assert result.success and result.sequence_source == "kiln_pro_motion_plan"
        assert result.action == action
        assert adapter.sent == good[0]["gcode"]

    def test_the_bambu_emitters_doors_read_the_plan_too(self, bambu, monkeypatch):
        from .test_filament_handling import _scripts
        from .test_home_axes import _fake_home_doc, _idle, _serve

        _serve(monkeypatch, {"home": _fake_home_doc(), "park": _fake_home_doc(verb="park")})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(bambu, "_plate_witness", lambda: None)
        mark_occupied(bambu, PlateJob(file="vase.gcode", footprint_mm=[100, 100, 150, 150], max_z_mm=60.0, printer_id="bambu_a1"))
        _stub_planner(monkeypatch, _plan("G28 X", "G91", "G1 Z70 F300", "G90"))
        with pytest.raises(PlateClearRequired, match="before the head is proven"):
            bambu.park_head()
        with pytest.raises(PlateClearRequired, match="before the head is proven"):
            bambu.home_axes(axes="XY")
        assert _scripts(bambu) == []


# =========================================================================
# (3) the model the planner plans for
# =========================================================================


class TestTheModelThePlannerPlansFor:
    def test_the_hook_hands_kiln_pro_the_declared_model_as_the_station_record(self, monkeypatch):
        seen: list[dict[str, Any]] = []

        def _pro(record=None, station=None, *, action="home", clearance_mm=None):
            seen.append({"record": record, "station": station, "action": action, "clearance_mm": clearance_mm})
            return None

        fake_pro = types.ModuleType("kiln_pro")
        fake_bridge = types.ModuleType("kiln_pro.bridge")
        fake_bridge.plan_motion_around_plate = _pro
        fake_pro.bridge = fake_bridge
        monkeypatch.setitem(sys.modules, "kiln_pro", fake_pro)
        monkeypatch.setitem(sys.modules, "kiln_pro.bridge", fake_bridge)
        state = PlateState(machine="M", status="occupied", source="test",
                           job=PlateJob(file="benchy.gcode", footprint_mm=PART, max_z_mm=HEIGHT, printer_id="ender3_v2"))

        assert plate_state.plan_motion_around_plate(state, None, action="park", clearance_mm=None, printer_model="ender3") is None
        assert seen[-1]["station"] == {"printer_id": "ender3"}
        assert seen[-1]["record"]["job"]["printer_id"] == "ender3_v2", "the record is handed over untouched"

        plate_state.plan_motion_around_plate(state, {"raise_before_travel": {"probe_up_mm": 40, "back_down_mm": 15}},
                                             action="home", clearance_mm=25.0, printer_model="ender3")
        assert seen[-1]["station"] == {"raise_before_travel": {"probe_up_mm": 40, "back_down_mm": 15}, "printer_id": "ender3"}

        plate_state.plan_motion_around_plate(state, None, action="home", clearance_mm=None)
        assert seen[-1]["station"] is None, "without a declared model the station is passed as it came"

    @pytest.mark.parametrize("action", ["home", "park"])
    @pytest.mark.parametrize("declare", ["printer_model", "safety_profile"])
    def test_the_door_asks_for_the_model_the_adapter_is_declared_as_now(self, monkeypatch, action, declare):
        adapter = _Bench(model="ender3", declare=declare)
        _occupy(adapter, model="ender3_v2")  # the record remembers the print's model, not today's
        asked = _stub_planner(monkeypatch, None)
        with pytest.raises(PlateClearRequired):
            _run(adapter, action)
        assert asked and asked[0]["printer_model"] == "ender3"
        assert adapter.sent == []

    def test_the_retry_decision_asks_for_the_declared_model_too(self, monkeypatch, tmp_path):
        from .test_retry_homes_around_the_part import _BODY, _header

        adapter = _Bench(model="sovol_sv06", serial="SV06BENCH0001")
        mark_occupied(adapter, PlateJob(file="benchy.gcode", footprint_mm=PART, max_z_mm=HEIGHT, printer_id="ender3"))
        asked = _stub_planner(monkeypatch, None)
        from kiln.printers import print_gate as pg

        path = tmp_path / "retry.gcode"
        path.write_text(_header(pg.same_bed_machine_id(adapter)) + _BODY)
        decision = hap.evaluate_home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, str(path)))
        assert decision["code"] == "HOME_AROUND_PART_NO_PLAN"
        assert asked and asked[0]["printer_model"] == "sovol_sv06"

    @pytest.mark.parametrize("action", ["home", "park"])
    def test_a_record_from_another_model_is_refused_by_the_real_planner(self, real_planner, action):
        # The part was printed when this printer was declared an Ender 3 V2;
        # it has since been re-declared an Ender 3.  A plan for one machine
        # is not run on the other -- and before the model was passed
        # through, the home door ran the V2's plan on the Ender 3.
        adapter = _Bench(model="ender3")
        _occupy(adapter, model="ender3_v2")
        with pytest.raises(PlateClearRequired):
            _run(adapter, action)
        assert adapter.sent == []

    def test_a_record_with_no_model_is_planned_for_the_declared_one(self, real_planner):
        adapter = _Bench(model="ender3")
        _occupy(adapter, model=None)
        result = adapter.park_head()
        assert result.success and result.sequence_source == "kiln_pro_motion_plan"
        _assert_lifted_and_never_over_the_part(adapter.sent, footprint=PART, height=HEIGHT, home_corner=(0.0, 0.0))

    @pytest.mark.parametrize("declare", ["printer_model", "safety_profile"])
    def test_a_started_print_records_the_model_however_it_was_declared(self, declare):
        adapter = _Bench(model="Ender3", declare=declare)
        assert plate_state.job_for_start(adapter, "benchy.gcode").printer_id == "ender3"
        mark_occupied_by_start(adapter, "benchy.gcode")
        state = plate_occupancy(adapter)
        assert state.occupied and state.job is not None and state.job.printer_id == "ender3"

    def test_an_undeclared_printer_records_no_model(self):
        adapter = _Bench(declare=None)
        assert plate_state.job_for_start(adapter, "benchy.gcode").printer_id is None

    def test_an_adapter_declared_through_its_safety_profile_is_parked_around_the_part(self, real_planner):
        adapter = _Bench(model="ender3", declare="safety_profile")
        mark_occupied_by_start(adapter, "benchy.gcode")
        # The file was never local, so the record has no geometry; give it
        # the part's, the way a Kiln-sliced file would have.
        recorded = plate_occupancy(adapter)
        assert recorded.job is not None and recorded.job.printer_id == "ender3"
        _occupy(adapter, model=recorded.job.printer_id)
        result = adapter.park_head()
        assert result.success and result.sequence_source == "kiln_pro_motion_plan"
        _assert_lifted_and_never_over_the_part(adapter.sent, footprint=PART, height=HEIGHT, home_corner=(0.0, 0.0))
