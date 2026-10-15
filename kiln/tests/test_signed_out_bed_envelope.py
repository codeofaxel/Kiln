"""A signed-out install is held to its printer's real edges and the floor's
real ceilings (2026-10-15).

When the per-printer records moved behind sign-in (2026-10-12), a roster
printer whose record was not in hand had NO bed, and every bed door read
"no bed" as "nothing to check": the print-start gate let G-code reaching
X400 on an Ender 3 through, the interception rules for the bed were an empty
list, the make pipeline sized parts against a guessed 200 mm cube, and the
generic floor's chamber (80 C) and speed (10000 mm/min) sat above 29 and
nine real printers.  Measured by running the real doors with kiln-pro
blocked and an empty ``~/.kiln``; each case here is one of those probes,
and each is A/B-pinned by removing the rated bed from the roster row (the
pre-fix shape) and watching the door go quiet again.

Every case runs SIGNED OUT on purpose (``signed_out_install``), against the
synthetic roster printer ``kilntest_served`` (rated bed 200 x 200 x 210, a
number that belongs to no machine).
"""
from __future__ import annotations

import json

import pytest

from kiln import safety_profiles as sp
from kiln.printers import bed_fit

PRINTER = "kilntest_served"
RATED = (200.0, 200.0, 210.0)


@pytest.fixture(autouse=True)
def _signed_out(signed_out_install):
    """The posture under test, requested on purpose."""


@pytest.fixture
def no_rated_bed(monkeypatch):
    """The pre-fix shape: the roster row states no rated bed."""
    roster = json.loads(json.dumps(bed_fit._load_printer_intelligence()))
    roster[PRINTER].pop("rated_build_volume_mm", None)
    monkeypatch.setattr(bed_fit, "_printer_intelligence_cache", roster)


def _off_bed_gcode(tmp_path) -> str:
    path = tmp_path / "far.gcode"
    path.write_text(
        "G28\nG90\nM82\nG1 Z0.3 F600\nG1 X10 Y10 F3000\n"
        "G1 X400 Y10 E10 F1500\nG1 X400 Y400 E20\nG1 X-30 Y10 E30\n",
        encoding="utf-8",
    )
    return str(path)


class TestTheBedGatesHoldTheRatedEdges:
    def test_the_ceilings_are_generic_but_the_bed_is_the_printers(self):
        assert sp.get_profile(PRINTER).id == "default"
        assert bed_fit.get_build_volume(PRINTER) == RATED

    def test_the_print_start_gate_refuses_extrusion_off_the_bed(self, tmp_path):
        from kiln.printers import print_gate

        blocked, code, _message, _detail = print_gate._fit_verdict(_off_bed_gcode(tmp_path), PRINTER)
        assert blocked and code == "EXCEEDS_BED"

    def test_without_the_rated_bed_the_gate_goes_quiet(self, tmp_path, no_rated_bed):
        """The A/B: the incident's shape.  A missing bed is "allow"."""
        from kiln.printers import print_gate

        blocked, _code, _message, detail = print_gate._fit_verdict(_off_bed_gcode(tmp_path), PRINTER)
        assert not blocked and detail["error_code"] == "VOLUME_UNKNOWN"

    def test_the_bed_interception_rules_exist(self):
        from kiln.safety.default_interception_rules import get_default_bed_safety_rules

        assert len(get_default_bed_safety_rules(PRINTER)) >= 4

    def test_without_the_rated_bed_there_are_no_bed_rules(self, no_rated_bed):
        from kiln.safety.default_interception_rules import get_default_bed_safety_rules

        assert get_default_bed_safety_rules(PRINTER) == []

    def test_the_park_planner_can_prove_a_corner(self):
        from kiln.printers import safe_motion

        part = safe_motion.OccupiedRegion(x_min=50, x_max=100, y_min=50, y_max=100, margin_mm=5)
        assert safe_motion.plan_park_point(PRINTER, part).ok

    def test_the_make_pipeline_sizes_against_the_rated_bed_not_a_guess(self):
        from kiln.plugins import _validation_pipeline_internals as pipeline

        resolved = pipeline._resolve_build_volume(PRINTER)
        assert resolved is not None and resolved.dims == RATED

    def test_without_the_rated_bed_the_pipeline_guessed_200(self, no_rated_bed):
        """The second half of the incident: the floor's cube, labelled as a
        printer nobody recognised, stood in for a machine the roster names."""
        from kiln.plugins import _validation_pipeline_internals as pipeline

        resolved = pipeline._resolve_build_volume(PRINTER)
        assert resolved is not None and resolved.dims == (200.0, 200.0, 200.0)
        assert "not recognised" in resolved.provenance

    def test_the_generic_slicer_profile_is_given_the_rated_bed(self):
        from kiln import slicer_profiles as slp

        settings = dict(slp.get_slicer_profile(PRINTER).settings)
        slp._ensure_bed(settings, "default", PRINTER)
        assert settings["bed_shape"] == "0x0,200x0,200x200,0x200"
        assert settings["max_print_height"] == "210"

    def test_the_gcode_bounds_warning_names_the_rated_bed(self):
        from kiln.gcode import validate_gcode_for_printer

        result = validate_gcode_for_printer("G1 X250 Y10 F3000", PRINTER)
        assert result.valid  # bounds are a warning, as they always were
        assert any("rated build volume (X: 0" in w and "200" in w for w in result.warnings), result.warnings
        assert not any("not recognised" in w for w in result.warnings)

    def test_a_printer_the_roster_does_not_list_still_has_no_bed(self):
        assert bed_fit.get_build_volume("a_printer_nobody_makes") is None
        assert bed_fit.get_build_volume("default") is None


class TestTheFloorIsBelowEveryPrinter:
    def test_a_chamber_above_the_floor_is_refused_on_the_floor(self):
        """``M141 S80`` was accepted on an X1C, rated 60, when its record was
        not in hand; the floor is now the lowest catalogued chamber."""
        from kiln.gcode import validate_gcode_for_printer

        assert not validate_gcode_for_printer("M141 S80", PRINTER).valid
        assert validate_gcode_for_printer("M141 S60", PRINTER).valid

    def test_the_floor_speed_is_the_slowest_catalogued(self):
        from kiln.gcode import validate_gcode_for_printer

        floor = sp.get_profile("default")
        assert floor.max_chamber_temp == 60.0 and floor.max_feedrate == 7500.0
        result = validate_gcode_for_printer("G1 X10 F9000", PRINTER)
        assert any("F9000 exceeds" in w for w in result.warnings)

    def test_the_setup_nudge_no_longer_promises_the_bed(self):
        """The bed is held signed out now; the nudge names what sign-in
        still adds (the real temperature limits, the tuned slicer profile)."""
        sp._nudged.clear()
        sp._pending_nudges.clear()
        sentence = sp.warm_profile(PRINTER)
        assert sentence and "signed out" in sentence
        assert "build volume" not in sentence and "tuned profile" in sentence
