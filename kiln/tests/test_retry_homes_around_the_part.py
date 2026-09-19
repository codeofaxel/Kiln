"""A same-bed retry on a printer that was power-cycled: home AROUND the part.

The same-bed gate refuses a retry whose machine does not report x, y and z
homed right now (``SAME_BED_RETRY_NOT_HOMED``).  That refusal is correct
for a machine Kiln knows nothing about, and wrong for one where Kiln
already knows exactly what is on the plate and how tall it is: the plate
record names the failed part's footprint and height, and homing around it
is a motion, not a guess.

This is the decision that turns that one refusal into a homing:

  (1) the retry was refused ONLY because the machine is not homed;
  (2) the plate record says ``occupied``, with a footprint AND a height;
  (3) kiln-pro's planner returns a motion plan for this plate;
  (4) every line of that plan clears the part -- nothing crosses the part's
      row below its top, nothing descends over its footprint, and any Z
      reference presses somewhere the part is not;
  (5) the plan runs through the adapter's own ``home_axes`` door, which is
      the plate-aware gate -- never around it;
  (6) the firmware, asked again, reports x, y and z homed.

Anything missing is a refusal that names itself.  Bambu is refused at (4)
on two vendor-cited facts and does not reach the machine.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kiln import plate_state
from kiln.plate_state import PlateJob, mark_clear, mark_occupied
from kiln.printers import home_around_part as hap
from kiln.printers import print_gate as pg
from kiln.printers.base import (
    HomeResult,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
    PrintResult,
)

MACHINE_SERIAL = "SV06BENCH0001"

# The part the failed print left on the plate: 80 x 80 mm at the middle of
# the bed, 40 mm tall.  The SV06's Z probe point (135, 85) sits outside it.
PART_FOOTPRINT = [90.0, 100.0, 170.0, 180.0]
PART_HEIGHT = 40.0

# --- the retry artifact, exactly as kiln-pro bakes it --------------------

_BODY = (
    ";LAYER_CHANGE\n;Z:0.2\n"
    "G1 Z.4 F24000\nG1 X44.658 Y44.939\nG1 Z.2\n"
    ";TYPE:Skirt/Brim\nG1 F3000\nG1 X46.4 Y43.37 E.06961\n"
)


def _header(machine: str) -> str:
    return (
        "\n".join([
            "; --- Kiln same-bed retry safe startup ---",
            "; source_gcode: benchy.gcode",
            "; safe_startup_policy_id: same-bed-safe-prologue-v2:sovol",
            "; safe_startup_policy_version: same-bed-safe-prologue-v2",
            "; safe_startup_motion_contract: no_xyz_motion",
            f"; baked_for_machine: {machine}",
            "G21 ; Kiln same-bed retry units: mm",
            "G90 ; Kiln same-bed retry absolute XY",
            "; --- End Kiln same-bed retry safe startup ---",
        ])
        + "\n"
    )


# --- a real adapter, running the real templates --------------------------


class _Bench(PrinterAdapter):
    """A generic G-code backend: the real ``home_axes`` template, the real
    ``start_print`` template, and every line it was asked to send written
    down.  Nothing is mocked -- the plate gate, the motion gate and the
    pre-print gate all run."""

    def __init__(
        self,
        *,
        model: str = "sovol_sv06",
        serial: str = MACHINE_SERIAL,
        homed: set[str] | None = None,
    ) -> None:
        self.serial = serial
        self._printer_model = model
        self.printer_id = model
        self.homed: set[str] | None = set(homed or set())
        self.sent: list[str] = []
        self.home_impl_calls: list[tuple[str, dict[str, Any]]] = []
        self.started: list[str] = []
        self.homed_reads = 0
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
        # The firmware homes what it was told to home.
        for line in commands:
            head = line.split(";")[0].strip().upper()
            if head.startswith("G28"):
                letters = {c for c in head[3:] if c in "XYZ"}
                self.homed = (self.homed or set()) | {
                    a.lower() for a in (letters or {"X", "Y", "Z"})
                }
        return True

    def _home_axes_impl(self, axes: str, options: dict[str, Any]) -> HomeResult:
        self.home_impl_calls.append((axes, dict(options)))
        return super()._home_axes_impl(axes, options)

    def homed_axes_now(self) -> set[str] | None:
        self.homed_reads += 1
        return None if self.homed is None else set(self.homed)

    def homed_axes_field(self) -> str | None:
        return "toolhead.homed_axes"

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="IMPL")

    def get_tool_position(self) -> dict[str, Any] | None:
        return None


_Bench.__abstractmethods__ = frozenset()


class _Unlatched:
    def get_latch_status(self, printer_id: str) -> dict[str, Any]:
        return {"latched": False, "critical_interlocks_pending": []}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(
        "kiln.emergency.get_emergency_coordinator", lambda: _Unlatched()
    )
    pg._oversize_grants.clear()
    yield
    pg._oversize_grants.clear()


@pytest.fixture
def adapter():
    return _Bench()


@pytest.fixture
def retry_file(tmp_path):
    def _write(name: str = "benchy_same_bed_retry.gcode", machine: str | None = None) -> str:
        path = tmp_path / name
        path.write_text(_header(machine or _machine_of(_Bench())) + _BODY)
        return str(path)

    return _write


def _machine_of(a: Any) -> str:
    return pg.same_bed_machine_id(a)


def _occupy(a: Any, *, footprint: list[float] | None = PART_FOOTPRINT,
            height: float | None = PART_HEIGHT) -> None:
    mark_occupied(
        a,
        PlateJob(file="benchy.gcode", footprint_mm=footprint, max_z_mm=height,
                 printer_id="sovol_sv06"),
    )


# The plan kiln-pro's planner returns for this plate: lift clear of the
# part, home X and Y at that height, cross to the probe point (outside the
# part), and let the firmware find Z there.
GOOD_PLAN = [
    {
        "label": "lift clear of the part",
        "you_will_see": "the nozzle rises 42 mm from wherever it is now",
        "stops_when": "the move completes",
        "gcode": ["G91", "G1 Z42 F600", "G90"],
    },
    {
        "label": "home X and Y at that height",
        "you_will_see": "the head travels to the front-left corner",
        "stops_when": "both endstops trip",
        "gcode": ["G28 X Y"],
    },
    {
        "label": "find Z away from the part",
        "you_will_see": "the head crosses to X135 Y85 and probes there",
        "stops_when": "the probe triggers",
        "gcode": ["G1 X135 Y85 F6000", "G28 Z"],
    },
]


@pytest.fixture
def planner(monkeypatch):
    """kiln-pro's planner, installed.  ``plan`` is what it answers with."""

    class _Planner:
        def __init__(self) -> None:
            self.plan: list[dict[str, Any]] | None = [dict(s) for s in GOOD_PLAN]
            self.asked: list[dict[str, Any]] = []

        def __call__(self, state, station, *, action, clearance_mm):
            self.asked.append({"action": action, "clearance_mm": clearance_mm,
                               "status": state.status})
            if self.plan is None:
                return None
            from kiln.printers.base import HomeStep  # noqa: F401  (shape check)

            return [dict(s) for s in self.plan]

    p = _Planner()
    monkeypatch.setattr(plate_state, "plan_motion_around_plate", p)
    return p


# =========================================================================
# (1) the whole way through: refused for homing, homed around, then started
# =========================================================================


def test_not_homed_with_a_known_plate_and_a_plan_homes_around_the_part_then_starts(
    adapter, retry_file, planner, caplog,
):
    _occupy(adapter)
    path = retry_file(machine=_machine_of(adapter))

    # Before: the same-bed gate refuses it, only for homing.
    refusal = pg.evaluate_same_bed_retry(adapter, path)
    assert refusal["code"] == "SAME_BED_RETRY_NOT_HOMED"
    assert adapter.start_print(path).success is False
    assert adapter.started == []

    with caplog.at_level(logging.INFO, logger="kiln.printers.home_around_part"):
        decision = hap.home_around_part(adapter, refusal=refusal)

    assert decision["homed"] is True, decision["reason"]
    assert decision["code"] == "HOME_AROUND_PART_HOMED"
    assert decision["press_point_mm"] == [135.0, 85.0]
    assert decision["plan_id"]
    # It went through home_axes -- the plate-aware door -- not around it.
    assert [a for a, _ in adapter.home_impl_calls] == ["XYZ"]
    assert all(
        opts.get("plate_clear") is not True for _, opts in adapter.home_impl_calls
    ), "the plate is NOT clear and Kiln must never say it is"
    assert adapter.sent == [
        "G91", "G1 Z42 F600", "G90", "G28 X Y", "G1 X135 Y85 F6000", "G28 Z",
    ]
    # And the machine was asked again afterwards.
    assert decision["homed_evidence"]["axes"] == ["x", "y", "z"]

    audit = [r.getMessage() for r in caplog.records]
    assert audit and "homed-around-part" in audit[-1]
    assert decision["plan_id"] in audit[-1] and "135" in audit[-1]

    # After: the gate accepts, and the retry starts.
    assert pg.run_adapter_gate(adapter, path, {}) is None
    assert adapter.start_print(path).success is True
    assert adapter.started == [path]


def test_the_plate_record_still_says_a_part_is_there_afterwards(adapter, retry_file, planner):
    _occupy(adapter)
    hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    state = plate_state.plate_occupancy(adapter)
    assert state.occupied is True, "homing around a part does not remove the part"
    assert state.job is not None and state.job.max_z_mm == PART_HEIGHT


# =========================================================================
# (1) only that refusal
# =========================================================================


@pytest.mark.parametrize("homed", [{"x", "y", "z"}, None])
def test_any_refusal_that_is_not_the_homing_one_is_left_alone(adapter, retry_file, planner, homed):
    _occupy(adapter)
    adapter.homed = homed
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_NOT_THE_REFUSAL"
    assert adapter.home_impl_calls == [] and adapter.sent == []


def test_a_busy_machine_is_not_homed_around_a_part(adapter, retry_file, planner):
    _occupy(adapter)
    adapter.state = PrinterStatus.PRINTING
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_NOT_THE_REFUSAL"
    assert adapter.sent == []


# =========================================================================
# (2) the plate record
# =========================================================================


def test_an_unknown_plate_is_refused(adapter, retry_file, planner):
    # nothing recorded at all
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_PLATE_UNKNOWN"
    assert "no record" in decision["reason"]
    assert adapter.sent == [], "nothing moved"


def test_a_plate_recorded_clear_is_refused_too(adapter, retry_file, planner):
    mark_clear(adapter, "human")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_PLATE_UNKNOWN"
    assert adapter.sent == []


def test_a_part_of_unknown_height_is_refused(adapter, retry_file, planner):
    _occupy(adapter, height=None)
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_PART_HEIGHT_UNKNOWN"
    assert "how tall" in decision["reason"]
    assert adapter.sent == []


def test_a_part_of_unknown_footprint_is_refused(adapter, retry_file, planner):
    _occupy(adapter, footprint=None)
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_FOOTPRINT_UNKNOWN"
    assert adapter.sent == []


# =========================================================================
# (3) the planner
# =========================================================================


def test_no_planner_at_all_is_refused(adapter, retry_file, monkeypatch):
    _occupy(adapter)
    monkeypatch.setattr(
        plate_state, "plan_motion_around_plate", lambda *a, **k: None
    )
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_NO_PLAN"
    assert adapter.sent == []


def test_the_planner_is_asked_about_this_plate_and_this_action(adapter, retry_file, planner):
    _occupy(adapter)
    hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert planner.asked and planner.asked[0]["action"] == "home"
    assert planner.asked[0]["status"] == "occupied"


# =========================================================================
# (4) every line of the plan is judged against the part
# =========================================================================


def _plan(*lines: str) -> list[dict[str, Any]]:
    return [{
        "label": "the whole plan", "you_will_see": "-", "stops_when": "-",
        "gcode": list(lines),
    }]


def test_a_plan_that_travels_before_it_lifts_is_refused(adapter, retry_file, planner):
    _occupy(adapter)
    planner.plan = _plan("G28 X Y", "G91", "G1 Z42", "G90")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_TRAVELS_LOW"
    assert adapter.sent == []


def test_a_plan_that_lifts_less_than_the_part_is_tall_is_refused(adapter, retry_file, planner):
    _occupy(adapter)
    planner.plan = _plan("G91", "G1 Z30 F600", "G90", "G28 X Y")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_TRAVELS_LOW"
    assert "40" in decision["reason"]
    assert adapter.sent == []


def test_a_plan_that_descends_over_the_part_is_refused(adapter, retry_file, planner):
    _occupy(adapter)
    planner.plan = _plan(
        "G91", "G1 Z42 F600", "G90", "G28 X Y", "G1 X130 Y130 F6000", "G91", "G1 Z-20",
    )
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_DESCENDS_OVER_PART"
    assert adapter.sent == []


def test_a_plan_with_a_command_kiln_cannot_read_is_refused(adapter, retry_file, planner):
    _occupy(adapter)
    planner.plan = _plan("G91", "G1 Z42 F600", "G90", "SAFE_HOME_AROUND PART=1")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_UNREADABLE"
    assert "SAFE_HOME_AROUND" in decision["reason"]
    assert adapter.sent == []


def test_a_plan_that_probes_the_whole_bed_is_refused(adapter, retry_file, planner):
    _occupy(adapter)
    planner.plan = _plan("G91", "G1 Z42 F600", "G90", "G28 X Y", "G29")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_PROBES_THE_BED"
    assert adapter.sent == []


def test_a_z_reference_that_presses_on_the_part_is_refused(adapter, retry_file, planner):
    # The same plan, but the part now covers the machine's probe point.
    _occupy(adapter, footprint=[100.0, 50.0, 180.0, 130.0])  # contains (135, 85)
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    assert decision["code"] == "HOME_AROUND_PART_PRESS_ON_PART"
    assert "135" in decision["reason"] and "85" in decision["reason"]
    assert adapter.sent == []


def test_a_machine_that_cannot_name_its_press_point_is_refused(retry_file, planner, tmp_path):
    # Elegoo Neptune 3: an inductive probe, and the vendor has not said
    # where it lands.
    a = _Bench(model="elegoo_neptune3", serial="NEPTUNE0001")
    _occupy(a)
    path = tmp_path / "r.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["code"] == "HOME_AROUND_PART_NO_SAFE_PRESS_POINT"
    assert a.sent == []


def test_a_machine_with_no_motion_record_is_refused(retry_file, planner, tmp_path):
    a = _Bench(model="", serial="NOMODEL0001")
    _occupy(a)
    path = tmp_path / "n.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["code"] == "HOME_AROUND_PART_NO_MOTION_RECORD"
    assert a.sent == []


def test_a_z_home_that_cannot_touch_the_plate_needs_no_press_point(planner, tmp_path):
    # Voron Trident: Z homes on a pin behind the plate, and its own routine
    # does not travel before Z is known -- so a bare G28 is the whole plan.
    a = _Bench(model="voron_trident", serial="VORON0001")
    _occupy(a)
    planner.plan = _plan("G28")
    path = tmp_path / "v.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["homed"] is True, decision["reason"]
    assert decision["press_point_mm"] is None
    assert a.sent == ["G28"]


def test_a_bare_g28_on_a_machine_that_travels_blind_is_refused(adapter, retry_file, planner):
    # The SV06 can take an explicit plan, but its bare G28 is the firmware's
    # own routine, and Kiln vouches for that only where the vendor has said
    # it does not travel before Z is known.
    _occupy(adapter)
    planner.plan = _plan("G28")
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    # The SV06's vendor DOES say so, so this one is allowed.
    assert decision["homed"] is True, decision["reason"]

    b = _Bench(model="ender3_v2", serial="ENDER0001")
    _occupy(b)
    import pathlib

    p = pathlib.Path(retry_file()).with_name("e.gcode")
    p.write_text(_header(_machine_of(b)) + _BODY)
    decision = hap.home_around_part(b, refusal=pg.evaluate_same_bed_retry(b, str(p)))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_DELEGATES_BLIND_HOME"
    assert b.sent == []


def test_a_machine_that_refuses_unhomed_moves_cannot_be_lifted(planner, tmp_path):
    # QIDI Q1 Pro: the firmware refuses any move until the axes are homed,
    # so the lift that would clear the part cannot be promised.
    a = _Bench(model="qidi_q1_pro", serial="QIDI0001")
    _occupy(a)
    planner.plan = _plan("G91", "G1 Z42 F600", "G90", "G28 X Y")
    path = tmp_path / "q.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["code"] == "HOME_AROUND_PART_UNHOMED_MOVE_NOT_PROMISED"
    assert a.sent == []


# =========================================================================
# (5) Bambu: refused, with the reason, and never touched
# =========================================================================


@pytest.mark.parametrize("model", ["bambu_a1", "bambu_a1_mini", "bambu_p1s", "bambu_x1c"])
def test_a_bambu_is_refused_and_the_reason_names_the_vendor_gap(planner, tmp_path, model):
    a = _Bench(model=model, serial=f"{model}-0001")
    _occupy(a)
    path = tmp_path / f"{model}.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_UNHOMED_MOVE_NOT_PROMISED"
    assert "has not said" in decision["reason"]
    assert a.sent == [], "a Bambu is never moved by this decision"
    assert a.home_impl_calls == []


def test_a_bambu_bare_g28_plan_is_refused_too(planner, tmp_path):
    a = _Bench(model="bambu_a1", serial="A1BARE0001")
    _occupy(a)
    planner.plan = _plan("G28")
    path = tmp_path / "a1bare.gcode"
    path.write_text(_header(_machine_of(a)) + _BODY)
    decision = hap.home_around_part(a, refusal=pg.evaluate_same_bed_retry(a, str(path)))
    assert decision["code"] == "HOME_AROUND_PART_PLAN_DELEGATES_BLIND_HOME"
    assert a.sent == []


# =========================================================================
# (6) the read afterwards is the only proof
# =========================================================================


def test_a_post_home_read_that_is_not_xyz_refuses_and_nothing_starts(
    adapter, retry_file, planner, monkeypatch,
):
    _occupy(adapter)
    path = retry_file(machine=_machine_of(adapter))
    # The plan runs, but the firmware still reports only X and Y.
    original = adapter.send_gcode

    def only_xy(commands: list[str]) -> Any:
        original(commands)
        adapter.homed = {"x", "y"}
        return True

    monkeypatch.setattr(adapter, "send_gcode", only_xy)
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, path))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_NOT_HOMED"
    assert decision["homed_evidence"]["axes"] == ["x", "y"]
    assert adapter.start_print(path).success is False
    assert adapter.started == []


def test_a_post_home_read_the_backend_cannot_answer_refuses(adapter, retry_file, planner, monkeypatch):
    _occupy(adapter)
    path = retry_file(machine=_machine_of(adapter))
    original = adapter.send_gcode

    def blind(commands: list[str]) -> Any:
        original(commands)
        adapter.homed = None
        return True

    monkeypatch.setattr(adapter, "send_gcode", blind)
    decision = hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, path))
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_HOMED_UNKNOWN"
    assert adapter.started == []


def test_a_post_home_read_that_fails_is_a_retry_not_a_gap(adapter, retry_file, planner, monkeypatch):
    _occupy(adapter)
    path = retry_file(machine=_machine_of(adapter))
    refusal = pg.evaluate_same_bed_retry(adapter, path)
    calls = {"n": 0}

    def flaky() -> set[str] | None:
        calls["n"] += 1
        raise PrinterError("timed out")

    monkeypatch.setattr(adapter, "homed_axes_now", flaky)
    decision = hap.home_around_part(adapter, refusal=refusal)
    assert decision["code"] == "HOME_AROUND_PART_HOMED_READ_FAILED"
    assert "timed out" in decision["reason"]


def test_a_home_the_adapter_refuses_is_reported_not_swallowed(adapter, retry_file, planner, monkeypatch):
    _occupy(adapter)
    path = retry_file(machine=_machine_of(adapter))
    refusal = pg.evaluate_same_bed_retry(adapter, path)

    def boom(commands: list[str]) -> Any:
        raise PrinterError("the printer is not listening")

    monkeypatch.setattr(adapter, "send_gcode", boom)
    decision = hap.home_around_part(adapter, refusal=refusal)
    assert decision["homed"] is False
    assert decision["code"] == "HOME_AROUND_PART_HOME_REFUSED"
    assert "not listening" in decision["reason"]
    assert adapter.started == []


# =========================================================================
# evaluate-only: the same answer, with nothing sent
# =========================================================================


def test_the_decision_can_be_asked_for_without_moving_anything(adapter, retry_file, planner):
    _occupy(adapter)
    verdict = hap.evaluate_home_around_part(
        adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file())
    )
    assert verdict["code"] == "HOME_AROUND_PART_READY"
    assert verdict["press_point_mm"] == [135.0, 85.0]
    assert adapter.sent == [] and adapter.home_impl_calls == []


def test_a_refusal_is_logged_with_its_reason(adapter, retry_file, planner, caplog):
    with caplog.at_level(logging.INFO, logger="kiln.printers.home_around_part"):
        hap.home_around_part(adapter, refusal=pg.evaluate_same_bed_retry(adapter, retry_file()))
    lines = [r.getMessage() for r in caplog.records]
    assert lines and "refused" in lines[-1]
    assert "HOME_AROUND_PART_PLATE_UNKNOWN" in lines[-1]
