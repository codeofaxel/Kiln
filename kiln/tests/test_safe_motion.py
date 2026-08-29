"""Tests for kiln.printers.safe_motion — motion planning over an occupied bed.

Coverage areas:
- G28 classification (which commands home Z)
- Sequence invariant checker (no Z home, lift before homing)
- Canonical sequence builders (lift+home, resume preamble, abort,
  firmware-resume positioning)
- Occupied-region extraction from real sliced gcode and a gcode-3MF
- Park-point planning against real catalogue bed geometries
  (corner-origin bedslinger, CoreXY, enclosed) including refusal paths
- Klipper homing-config analysis (safe_z_home / probe / endstop)
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kiln.printers.bed_fit import get_build_volume
from kiln.printers.safe_motion import (
    HomingBehavior,
    OccupiedRegion,
    _rect,
    _segment_enters_rect,
    analyze_homing_config,
    build_firmware_resume_positioning,
    build_lift_and_home_xy,
    build_resume_preamble,
    build_safe_abort_sequence,
    check_mid_print_sequence,
    homes_z,
    occupied_region_for_job,
    occupied_regions_for_job,
    plan_park_point,
    plan_travel,
)

# ---------------------------------------------------------------------------
# G28 classification
# ---------------------------------------------------------------------------


class TestHomesZ:
    @pytest.mark.parametrize(
        "command",
        ["G28", "g28", "G28 Z", "G28 X Z", "G28 Z0", "  G28  ", "G28 W"],
    )
    def test_z_homing_commands(self, command: str):
        assert homes_z(command) is True

    @pytest.mark.parametrize(
        "command",
        ["G28 X Y", "G28 X", "G28 Y", "g28 x y", "M104 S200", "G1 Z5 F600", "G280"],
    )
    def test_non_z_homing_commands(self, command: str):
        assert homes_z(command) is False


# ---------------------------------------------------------------------------
# Sequence invariants
# ---------------------------------------------------------------------------


class TestCheckMidPrintSequence:
    def test_flags_the_pre_planner_resume_sequence(self):
        # The exact sequence RESUME_FROM_LAYER emitted before the shared
        # planner existed: bare G28 first, before any lift or heat.
        legacy = [
            "; Recovery: resume from layer 42",
            "G28",
            "M104 S210",
            "M140 S60",
            "M109 S210",
            "M190 S60",
            "G1 Z13.4 F1000",
            "G1 E5 F300",
        ]
        violations = check_mid_print_sequence(legacy)
        assert violations, "legacy sequence must be flagged"
        assert any("homes Z" in v for v in violations)

    def test_accepts_every_builder_output(self):
        sequences = [
            build_lift_and_home_xy(),
            build_resume_preamble(hotend_temp=210, bed_temp=60, resume_z_mm=8.4),
            build_safe_abort_sequence(),
            build_firmware_resume_positioning(
                z_height_mm=22.4,
                hotend_temp_c=210.0,
                bed_temp_c=60.0,
                fan_pwm=255,
                flow_rate_pct=100.0,
                prime_length_mm=5.0,
                z_clearance_mm=2.0,
            ),
        ]
        for seq in sequences:
            assert check_mid_print_sequence(seq) == [], seq

    def test_flags_homing_without_prior_lift(self):
        violations = check_mid_print_sequence(["G28 X Y", "G91", "G1 Z5 F600"])
        assert any("before any Z lift" in v for v in violations)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_lift_and_home_never_touches_z_home(self):
        seq = build_lift_and_home_xy(lift_mm=7.5)
        assert "G28 X Y" in seq
        assert not any(homes_z(c) for c in seq)
        # Lift is relative and precedes the home.
        assert seq.index("G91") < seq.index("G28 X Y")
        lift = next(c for c in seq if c.startswith("G1 Z"))
        assert seq.index(lift) < seq.index("G28 X Y")

    def test_resume_preamble_shape(self):
        seq = build_resume_preamble(
            hotend_temp=210,
            bed_temp=60,
            resume_z_mm=12.4,
            header_comment="resume test",
        )
        joined = "\n".join(seq)
        # heat (non-blocking) -> lift -> home X/Y -> wait -> travel -> prime
        assert joined.index("M104 S210") < joined.index("G28 X Y")
        assert joined.index("G1 Z5") < joined.index("G28 X Y")
        assert joined.index("G28 X Y") < joined.index("M109 S210")
        # Travel Z is resume Z + lift, never at or below the resume layer.
        assert "G1 Z17.4" in joined
        assert not any(homes_z(c) for c in seq)

    def test_firmware_resume_teaches_lifted_z(self):
        seq = build_firmware_resume_positioning(
            z_height_mm=22.4,
            hotend_temp_c=210.0,
            bed_temp_c=60.0,
            fan_pwm=204,
            flow_rate_pct=100.0,
            prime_length_mm=5.0,
            z_clearance_mm=2.0,
        )
        # Lift happens physically at part-top Z, so G92 must teach the
        # LIFTED height or every later absolute Z is off by the clearance.
        assert "G92 Z24.4" in seq
        lift_idx = next(i for i, c in enumerate(seq) if c.startswith("G1 Z"))
        home_idx = seq.index("G28 X Y")
        assert lift_idx < home_idx
        assert not any(homes_z(c) for c in seq)

    def test_abort_lifts_before_parking(self):
        seq = build_safe_abort_sequence()
        assert seq[0] == "M104 S0"
        lift_idx = next(i for i, c in enumerate(seq) if c.startswith("G1 Z"))
        assert lift_idx < seq.index("G28 X Y")
        assert seq[-1] == "M84"


# ---------------------------------------------------------------------------
# Occupied region from real job artifacts
# ---------------------------------------------------------------------------

_SLICED_GCODE = """\
; generated by PrusaSlicer
M104 S210
G28 ; home (start gcode — bed is clear here)
G1 X-30 Y2 E5 F1200 ; purge line outside the plate, pre-print
;LAYER_CHANGE
;Z:0.2
G1 X80 Y90 F7200
G1 X120.5 Y90 E1.2
G1 X120.5 Y140 E2.4
G1 X80 Y140 E3.6
G1 X-48 F18000 ; travel to wiper — no E, must not widen the bbox
;LAYER_CHANGE
;Z:0.4
G1 X81 Y91 E4.0
"""


class TestOccupiedRegion:
    def test_bbox_from_sliced_gcode(self, tmp_path: Path):
        gcode = tmp_path / "job.gcode"
        gcode.write_text(_SLICED_GCODE)
        region = occupied_region_for_job(str(gcode), margin_mm=10.0)
        assert region is not None
        assert region.x_min == pytest.approx(80.0)
        assert region.x_max == pytest.approx(120.5)
        assert region.y_min == pytest.approx(90.0)
        assert region.y_max == pytest.approx(140.0)
        # Margin included in containment: 71,95 is 9mm off the bbox.
        assert region.contains(71.0, 95.0) is True
        assert region.contains(60.0, 95.0) is False
        # The pre-print purge at X=-30 and the travel to X=-48 are
        # start-gcode / travel moves — they must NOT count as occupied.
        assert region.contains(-30.0, 2.0) is False

    def test_bbox_from_bambu_gcode_3mf(self, tmp_path: Path):
        threemf = tmp_path / "job.gcode.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", _SLICED_GCODE)
        region = occupied_region_for_job(str(threemf))
        assert region is not None
        assert region.x_max == pytest.approx(120.5)
        assert region.source == "3mf_gcode_bbox"

    def test_missing_file_is_unknown_not_clear(self):
        assert occupied_region_for_job("/nonexistent/job.gcode") is None

    def test_unparseable_file_is_unknown_not_clear(self, tmp_path: Path):
        junk = tmp_path / "job.gcode"
        junk.write_text("; a file with no print moves at all\nM104 S200\n")
        assert occupied_region_for_job(str(junk)) is None


# ---------------------------------------------------------------------------
# Park planning against real catalogue geometries
# ---------------------------------------------------------------------------


def _region(x0: float, x1: float, y0: float, y1: float, margin: float = 10.0) -> OccupiedRegion:
    return OccupiedRegion(
        x_min=x0, x_max=x1, y_min=y0, y_max=y1, margin_mm=margin, source="test"
    )


class TestParkPlanning:
    def test_catalogue_geometries_resolve(self):
        # The geometries these tests rely on, verified against the catalogue.
        assert get_build_volume("ender3") == (220.0, 220.0, 250.0)
        assert get_build_volume("bambu_x1c") == (256.0, 256.0, 256.0)
        assert get_build_volume("voron_2_4_350") == (350.0, 350.0, 350.0)

    def test_bedslinger_centre_part_parks_clear(self):
        # Ender 3 (corner-origin bedslinger), 60x60 part centred on the bed.
        park = plan_park_point("ender3", _region(80, 140, 80, 140))
        assert park.ok is True
        x, y = park.xy
        assert 0 <= x <= 220 and 0 <= y <= 220
        assert not _region(80, 140, 80, 140).contains(x, y)

    def test_corner_part_parks_in_opposite_corner(self):
        # Part hugging the origin corner: the only sensible park is the
        # far corner, and near-origin candidates must be rejected.
        region = _region(0, 80, 0, 80)
        park = plan_park_point("ender3", region)
        assert park.ok is True
        assert park.xy == (215.0, 215.0)

    def test_enclosed_corexy_parks_clear(self):
        # X1C (enclosed CoreXY, 256mm bed), part in the +X half.
        region = _region(150, 250, 60, 200)
        park = plan_park_point("bambu_x1c", region)
        assert park.ok is True
        x, y = park.xy
        assert not region.contains(x, y)
        assert x <= 10.0  # provably in the free -X strip

    def test_large_corexy_parks_clear(self):
        region = _region(100, 250, 100, 250)
        park = plan_park_point("voron_2_4_350", region)
        assert park.ok is True
        assert not region.contains(*park.xy)

    # -- refusal paths --------------------------------------------------

    def test_unknown_printer_refuses(self):
        park = plan_park_point("mystery_printer_9000", _region(80, 140, 80, 140))
        assert park.ok is False
        assert park.xy is None
        assert "bed geometry unknown" in park.reason

    def test_unknown_occupancy_refuses(self):
        park = plan_park_point("ender3", None)
        assert park.ok is False
        assert park.xy is None
        assert "occupied region unknown" in park.reason

    def test_bed_filling_part_refuses(self):
        # A plate-filling job: no corner is provably clear -> refuse,
        # never guess.
        park = plan_park_point("ender3", _region(0, 220, 0, 220))
        assert park.ok is False
        assert park.xy is None
        assert "no on-bed point is provably clear" in park.reason


# ---------------------------------------------------------------------------
# Travel planning
# ---------------------------------------------------------------------------


def _assert_route_proven_clear(plan, regions) -> None:
    """Independent geometric re-verification of a returned route: every
    segment must miss the open interior of every inflated keep-out box."""
    rects = [_rect(r) for r in regions]
    assert plan.ok, plan.reason
    assert len(plan.waypoints) >= 2
    for a, b in zip(plan.waypoints, plan.waypoints[1:], strict=False):
        for rect in rects:
            assert not _segment_enters_rect(a, b, rect), (
                f"segment {a}->{b} enters keep-out {rect}: {plan}"
            )


class TestPlanTravel:
    def test_direct_when_clear(self):
        region = _region(150, 200, 150, 200)
        plan = plan_travel("ender3", (10, 10), (100, 10), region)
        assert plan.ok and plan.strategy == "direct"
        assert plan.waypoints == [(10, 10), (100, 10)]

    def test_routes_around_a_blocking_part(self):
        # Part square in the middle of the straight line.
        region = _region(90, 130, 90, 130)
        plan = plan_travel("ender3", (10, 110), (210, 110), region)
        assert plan.strategy == "route_around"
        _assert_route_proven_clear(plan, [region])
        assert plan.waypoints[0] == (10, 110)
        assert plan.waypoints[-1] == (210, 110)

    def test_routes_through_the_gap_between_two_parts(self):
        # Two parts with a clear corridor between them (margins 10:
        # keep-outs are y=[40,110] and y=[130,200] — corridor y in
        # (110,130)).
        low = _region(80, 140, 50, 100)
        high = _region(80, 140, 140, 190)
        plan = plan_travel("ender3", (10, 120), (210, 120), [low, high])
        _assert_route_proven_clear(plan, [low, high])

    def test_wall_across_the_bed_refuses(self):
        # Keep-out spanning the full bed width: no route can exist.
        wall = _region(0, 220, 100, 120)
        plan = plan_travel("ender3", (110, 10), (110, 210), wall)
        assert plan.ok is False
        assert plan.waypoints == []
        assert "no clear route" in plan.reason

    def test_fly_over_when_every_height_is_known(self):
        region = OccupiedRegion(
            x_min=90, x_max=130, y_min=90, y_max=130,
            margin_mm=10.0, z_top_mm=12.0, source="test",
        )
        plan = plan_travel(
            "ender3", (10, 110), (210, 110), region, travel_z_mm=20.0
        )
        assert plan.ok and plan.strategy == "fly_over"
        assert plan.waypoints == [(10, 110), (210, 110)]

    def test_unknown_height_sinks_fly_over(self):
        # z_top unknown -> cannot prove fly-over; must route around.
        region = _region(90, 130, 90, 130)  # z_top_mm=None
        plan = plan_travel(
            "ender3", (10, 110), (210, 110), region, travel_z_mm=20.0
        )
        assert plan.strategy == "route_around"
        _assert_route_proven_clear(plan, [region])

    def test_goal_inside_keep_out_refuses_unless_ignored(self):
        target = OccupiedRegion(
            x_min=90, x_max=130, y_min=90, y_max=130,
            margin_mm=10.0, source="exclude_object", name="coaster_2",
        )
        other = OccupiedRegion(
            x_min=30, x_max=60, y_min=30, y_max=60,
            margin_mm=10.0, source="exclude_object", name="coaster_1",
        )
        blocked = plan_travel("ender3", (5, 110), (110, 110), [target, other])
        assert blocked.ok is False
        assert "coaster_2" in blocked.reason
        # Aiming AT the target (mid-print decoration) exempts only it.
        aimed = plan_travel(
            "ender3", (5, 110), (110, 110), [target, other],
            ignore_names={"coaster_2"},
        )
        _assert_route_proven_clear(aimed, [other])

    def test_off_bed_endpoint_refuses(self):
        plan = plan_travel("ender3", (10, 10), (400, 10), _region(90, 130, 90, 130))
        assert plan.ok is False
        assert "outside" in plan.reason

    def test_unknown_printer_refuses(self):
        plan = plan_travel(
            "mystery_printer_9000", (10, 10), (50, 10), _region(90, 130, 90, 130)
        )
        assert plan.ok is False
        assert "bed geometry unknown" in plan.reason

    def test_unknown_occupancy_refuses(self):
        plan = plan_travel("ender3", (10, 10), (50, 10), None)
        assert plan.ok is False
        assert "occupied regions unknown" in plan.reason

    def test_corner_part_hugging_route(self):
        # Part in the origin corner; route from one adjacent edge to the
        # other must go around the outside, never through.
        region = _region(0, 60, 0, 60)
        plan = plan_travel("ender3", (5, 90), (90, 5), region)
        _assert_route_proven_clear(plan, [region])


# ---------------------------------------------------------------------------
# Per-object occupancy (exclude-object declarations)
# ---------------------------------------------------------------------------

_MULTI_OBJECT_GCODE = """\
; generated by OrcaSlicer
EXCLUDE_OBJECT_DEFINE NAME=coaster_1 CENTER=45,45 POLYGON=[[30,30],[60,30],[60,60],[30,60]]
EXCLUDE_OBJECT_DEFINE NAME=coaster_2 CENTER=110,110 POLYGON=[[90,90],[130,90],[130,130],[90,130]]
M104 S210
;LAYER_CHANGE
G1 X30 Y30 E1.0
G1 X130 Y130 E2.0
"""


class TestOccupiedRegionsForJob:
    def test_per_object_regions_parsed(self, tmp_path: Path):
        gcode = tmp_path / "plate.gcode"
        gcode.write_text(_MULTI_OBJECT_GCODE)
        regions = occupied_regions_for_job(str(gcode))
        assert regions is not None and len(regions) == 2
        by_name = {r.name: r for r in regions}
        assert by_name["coaster_1"].x_min == pytest.approx(30.0)
        assert by_name["coaster_1"].y_max == pytest.approx(60.0)
        assert by_name["coaster_2"].x_max == pytest.approx(130.0)
        assert all(r.source == "exclude_object" for r in regions)

    def test_falls_back_to_single_bbox(self, tmp_path: Path):
        gcode = tmp_path / "plain.gcode"
        gcode.write_text(_SLICED_GCODE)
        regions = occupied_regions_for_job(str(gcode))
        assert regions is not None and len(regions) == 1
        assert regions[0].source == "gcode_bbox"

    def test_missing_file_is_none(self):
        assert occupied_regions_for_job("/nonexistent/plate.gcode") is None

    def test_park_clears_all_objects(self, tmp_path: Path):
        gcode = tmp_path / "plate.gcode"
        gcode.write_text(_MULTI_OBJECT_GCODE)
        regions = occupied_regions_for_job(str(gcode))
        park = plan_park_point("ender3", regions)
        assert park.ok is True
        assert not any(r.contains(*park.xy) for r in regions)


# ---------------------------------------------------------------------------
# Homing behaviour from the machine's own config
# ---------------------------------------------------------------------------


class TestAnalyzeHomingConfig:
    def test_safe_z_home_with_position(self):
        behavior = analyze_homing_config(
            {"safe_z_home": {"home_xy_position": "128, 128", "z_hop": "10"}}
        )
        assert behavior.style == "safe_z_home"
        assert behavior.home_xy == (128.0, 128.0)

    def test_probe_as_virtual_endstop(self):
        behavior = analyze_homing_config(
            {"stepper_z": {"endstop_pin": "probe:z_virtual_endstop"}}
        )
        assert behavior.style == "probe_in_place"

    def test_physical_endstop(self):
        behavior = analyze_homing_config(
            {"stepper_z": {"endstop_pin": "PC2", "position_endstop": "0"}}
        )
        assert behavior.style == "endstop"

    def test_no_config_is_unknown(self):
        assert analyze_homing_config(None).style == "unknown"
        assert analyze_homing_config({}).style == "unknown"

    def test_default_dataclass_is_unknown(self):
        assert HomingBehavior().style == "unknown"
