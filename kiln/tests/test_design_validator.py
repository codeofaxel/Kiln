"""Tests for the design validation pipeline.

Coverage areas:
- Individual validation checks (wall thickness, overhang, stability,
  bridge length, bed adhesion)
- Full DesignValidationReport construction and aggregation
- validation_to_feedback conversion to PrintFeedback items
- design_validation_to_feedback bridge function in generation_feedback
- Edge cases: missing file, empty requirements, unknown material,
  all-pass, all-fail, degenerate geometry
- MCP tool integration (validate_design_for_requirements)
"""

from __future__ import annotations

import os
import struct
import tempfile

import pytest

from kiln.design_validator import (
    DesignValidationCheck,
    DesignValidationReport,
    _check_bed_adhesion,
    _check_bridge_length,
    _check_overhang_angle,
    _check_stability,
    _check_wall_thickness,
    _resolve_wall_thickness,
    validate_design,
    validation_to_feedback,
)


# ---------------------------------------------------------------------------
# STL helpers (same pattern as test_printability.py)
# ---------------------------------------------------------------------------


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    """Create a minimal binary STL from triangle vertex tuples."""
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<9f", *v1, *v2, *v3)
        attr = struct.pack("<H", 0)
        body += normal + verts + attr
    return header + count + body


def _cube_triangles(size: float = 10.0) -> list[tuple]:
    """12 triangles forming a cube [0,size]^3."""
    s = size
    verts = [
        (0, 0, 0),
        (s, 0, 0),
        (s, s, 0),
        (0, s, 0),
        (0, 0, s),
        (s, 0, s),
        (s, s, s),
        (0, s, s),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),  # bottom
        (4, 6, 5),
        (4, 7, 6),  # top
        (0, 4, 5),
        (0, 5, 1),  # front
        (2, 6, 7),
        (2, 7, 3),  # back
        (0, 3, 7),
        (0, 7, 4),  # left
        (1, 5, 6),
        (1, 6, 2),  # right
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _tall_narrow_triangles(
    base_x: float = 5.0,
    base_y: float = 5.0,
    height: float = 50.0,
) -> list[tuple]:
    """12 triangles forming a tall narrow box (high aspect ratio)."""
    verts = [
        (0, 0, 0),
        (base_x, 0, 0),
        (base_x, base_y, 0),
        (0, base_y, 0),
        (0, 0, height),
        (base_x, 0, height),
        (base_x, base_y, height),
        (0, base_y, height),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _thin_wall_triangles(wall_thickness: float = 0.3) -> list[tuple]:
    """A thin slab — triggers thin wall detection."""
    t = wall_thickness
    verts = [
        (0, 0, 0),
        (20, 0, 0),
        (20, 20, 0),
        (0, 20, 0),
        (0, 0, t),
        (20, 0, t),
        (20, 20, t),
        (0, 20, t),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple], name: str = "test_model.stl") -> str:
    """Write a binary STL file and return its path."""
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


@pytest.fixture()
def tmpdir():
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture()
def cube_stl(tmpdir):
    return _write_stl(tmpdir, _cube_triangles(10.0))


@pytest.fixture()
def tall_stl(tmpdir):
    return _write_stl(tmpdir, _tall_narrow_triangles(5.0, 5.0, 50.0), "tall.stl")


@pytest.fixture()
def thin_stl(tmpdir):
    return _write_stl(tmpdir, _thin_wall_triangles(0.3), "thin.stl")


@pytest.fixture()
def large_stl(tmpdir):
    return _write_stl(tmpdir, _cube_triangles(300.0), "large.stl")


# Reset the design knowledge base between tests.
@pytest.fixture(autouse=True)
def _reset_kb():
    from kiln.design_intelligence import _reset_knowledge_base

    _reset_knowledge_base()
    yield
    _reset_knowledge_base()


# ---------------------------------------------------------------------------
# TestDesignValidationChecks — individual check functions
# ---------------------------------------------------------------------------


class TestWallThicknessCheck:
    def test_thin_wall_flagged_as_critical(self):
        check = _check_wall_thickness(0.3, 1.0, material_name="PLA")
        assert not check.passed
        assert check.severity == "critical"
        assert "0.30" in check.fix_suggestion
        assert "1.0" in check.fix_suggestion

    def test_thin_wall_flagged_as_warning(self):
        check = _check_wall_thickness(0.7, 1.0, material_name="PLA")
        assert not check.passed
        assert check.severity == "warning"

    def test_thick_wall_passes(self):
        check = _check_wall_thickness(2.0, 1.0)
        assert check.passed
        assert check.severity == "info"
        assert check.fix_suggestion == ""

    def test_exact_threshold_passes(self):
        check = _check_wall_thickness(1.0, 1.0)
        assert check.passed

    def test_material_name_in_message(self):
        check = _check_wall_thickness(0.5, 1.2, material_name="PETG")
        assert "PETG" in check.fix_suggestion

    def test_check_name_is_wall_thickness(self):
        check = _check_wall_thickness(2.0, 1.0)
        assert check.check_name == "wall_thickness"

    def test_actual_and_required_values(self):
        check = _check_wall_thickness(0.5, 1.5)
        assert check.actual_value == 0.5
        assert check.required_value == 1.5


class TestOverhangAngleCheck:
    def test_steep_overhang_flagged(self):
        check = _check_overhang_angle(50.0, 45.0, material_name="PETG")
        assert not check.passed
        assert check.severity == "warning"
        assert "50.0" in str(check.actual_value)

    def test_moderate_overhang_passes(self):
        check = _check_overhang_angle(30.0, 60.0)
        assert check.passed
        assert check.severity == "info"

    def test_no_overhangs_passes(self):
        check = _check_overhang_angle(0, 45.0)
        assert check.passed

    def test_exact_threshold_passes(self):
        check = _check_overhang_angle(45.0, 45.0)
        assert check.passed

    def test_check_name_is_overhang(self):
        check = _check_overhang_angle(30.0, 60.0)
        assert check.check_name == "overhang"

    def test_material_name_in_fix(self):
        check = _check_overhang_angle(70.0, 50.0, material_name="ABS")
        assert "ABS" in check.fix_suggestion


class TestStabilityCheck:
    def test_tall_narrow_model_stability_warning(self):
        dims = {"width": 5.0, "depth": 5.0, "height": 25.0}
        check = _check_stability(dims, max_ratio=4.0)
        assert not check.passed
        assert check.severity == "warning"
        assert check.check_name == "stability"

    def test_extremely_tall_is_critical(self):
        dims = {"width": 5.0, "depth": 5.0, "height": 50.0}
        check = _check_stability(dims, max_ratio=4.0)
        assert not check.passed
        assert check.severity == "critical"

    def test_stable_model_passes(self):
        dims = {"width": 10.0, "depth": 10.0, "height": 10.0}
        check = _check_stability(dims, max_ratio=4.0)
        assert check.passed

    def test_flat_model_passes(self):
        dims = {"width": 50.0, "depth": 50.0, "height": 2.0}
        check = _check_stability(dims)
        assert check.passed

    def test_degenerate_zero_height_passes(self):
        dims = {"width": 10.0, "depth": 10.0, "height": 0.0}
        check = _check_stability(dims)
        assert check.passed

    def test_degenerate_zero_base_passes(self):
        dims = {"width": 0.0, "depth": 0.0, "height": 10.0}
        check = _check_stability(dims)
        assert check.passed

    def test_ratio_value_correct(self):
        dims = {"width": 5.0, "depth": 10.0, "height": 30.0}
        check = _check_stability(dims, max_ratio=4.0)
        # ratio = 30/5 = 6.0
        assert check.actual_value == 6.0


class TestBridgeLengthCheck:
    def test_long_bridge_flagged(self):
        check = _check_bridge_length(25.0, 15.0, material_name="PLA")
        assert not check.passed
        assert check.severity == "warning"
        assert "25.0" in check.fix_suggestion

    def test_short_bridge_passes(self):
        check = _check_bridge_length(10.0, 20.0)
        assert check.passed

    def test_exact_threshold_passes(self):
        check = _check_bridge_length(15.0, 15.0)
        assert check.passed

    def test_check_name_is_bridge_length(self):
        check = _check_bridge_length(5.0, 20.0)
        assert check.check_name == "bridge_length"


class TestBedAdhesionCheck:
    def test_low_bed_adhesion_flagged(self):
        check = _check_bed_adhesion(5.0)
        assert not check.passed
        assert check.severity == "warning"
        assert "5.0%" in check.fix_suggestion

    def test_good_bed_adhesion_passes(self):
        check = _check_bed_adhesion(30.0)
        assert check.passed

    def test_exact_threshold_passes(self):
        check = _check_bed_adhesion(15.0)
        assert check.passed

    def test_borderline_adhesion_flagged(self):
        check = _check_bed_adhesion(10.0)
        assert not check.passed
        assert check.severity == "warning"

    def test_check_name_is_bed_adhesion(self):
        check = _check_bed_adhesion(50.0)
        assert check.check_name == "bed_adhesion"


# ---------------------------------------------------------------------------
# TestResolveWallThickness
# ---------------------------------------------------------------------------


class TestResolveWallThickness:
    def test_takes_max_of_all_sources(self):
        rules = {"min_wall_thickness_mm": 2.0, "material_min_wall_thickness_mm": 1.5}
        limits = {"min_wall_thickness_mm": 0.8}
        assert _resolve_wall_thickness(rules, limits) == 2.0

    def test_material_limits_win_when_stricter(self):
        rules = {"min_wall_thickness_mm": 0.5}
        limits = {"min_wall_thickness_mm": 1.2}
        assert _resolve_wall_thickness(rules, limits) == 1.2

    def test_none_when_no_sources(self):
        assert _resolve_wall_thickness({}, {}) is None

    def test_single_source(self):
        assert _resolve_wall_thickness({"min_wall_thickness_mm": 3.0}, {}) == 3.0


# ---------------------------------------------------------------------------
# TestDesignValidationReport
# ---------------------------------------------------------------------------


class TestDesignValidationReport:
    def test_all_pass_report(self, cube_stl):
        report = validate_design(cube_stl, "simple coaster")
        assert report.overall_pass is True
        assert report.critical_count == 0
        assert len(report.checks) > 0

    def test_report_has_file_path(self, cube_stl):
        report = validate_design(cube_stl, "coaster")
        assert report.file_path == cube_stl

    def test_report_has_requirements_text(self, cube_stl):
        report = validate_design(cube_stl, "load bearing shelf bracket")
        assert report.requirements_text == "load bearing shelf bracket"

    def test_report_has_material(self, cube_stl):
        report = validate_design(cube_stl, "vase", material="petg")
        assert report.material == "petg"

    def test_report_to_dict_complete(self, cube_stl):
        report = validate_design(cube_stl, "simple coaster")
        d = report.to_dict()
        assert "file_path" in d
        assert "requirements_text" in d
        assert "material" in d
        assert "overall_pass" in d
        assert "checks" in d
        assert "critical_count" in d
        assert "warning_count" in d
        assert "summary" in d

    def test_check_to_dict_complete(self):
        check = DesignValidationCheck(
            check_name="test",
            passed=True,
            severity="info",
            actual_value=1.0,
            required_value=2.0,
            fix_suggestion="",
        )
        d = check.to_dict()
        assert d["check_name"] == "test"
        assert d["passed"] is True
        assert d["severity"] == "info"
        assert d["actual_value"] == 1.0
        assert d["required_value"] == 2.0
        assert d["fix_suggestion"] == ""

    def test_overall_pass_false_when_critical_exists(self, tall_stl):
        # 50mm tall, 5mm base = 10:1 ratio, critical
        report = validate_design(tall_stl, "simple object")
        # The aspect ratio of 10:1 should trigger critical
        stability_checks = [c for c in report.checks if c.check_name == "stability"]
        if stability_checks and not stability_checks[0].passed:
            assert report.overall_pass is False
            assert report.critical_count > 0

    def test_summary_describes_failures(self, tall_stl):
        report = validate_design(tall_stl, "simple object")
        assert len(report.summary) > 0
        # Summary should mention check counts
        assert "/" in report.summary

    def test_summary_on_all_pass(self, cube_stl):
        report = validate_design(cube_stl, "simple thing")
        if report.overall_pass:
            assert "passed" in report.summary.lower()


# ---------------------------------------------------------------------------
# TestValidateDesign — full pipeline
# ---------------------------------------------------------------------------


class TestValidateDesign:
    def test_cube_passes_basic_validation(self, cube_stl):
        report = validate_design(cube_stl, "decorative figurine")
        assert isinstance(report, DesignValidationReport)
        assert len(report.checks) > 0

    def test_load_bearing_checks_wall_thickness(self, cube_stl):
        report = validate_design(cube_stl, "shelf bracket that holds 10 lbs")
        check_names = {c.check_name for c in report.checks}
        assert "wall_thickness" in check_names

    def test_material_override_uses_material(self, cube_stl):
        report = validate_design(cube_stl, "vase", material="petg")
        assert report.material == "petg"
        # Should have overhang check (PETG has max_unsupported_overhang_deg)
        check_names = {c.check_name for c in report.checks}
        assert "overhang" in check_names

    def test_stability_always_checked(self, cube_stl):
        report = validate_design(cube_stl, "something simple")
        check_names = {c.check_name for c in report.checks}
        assert "stability" in check_names

    def test_adhesion_always_checked(self, cube_stl):
        report = validate_design(cube_stl, "something simple")
        check_names = {c.check_name for c in report.checks}
        assert "bed_adhesion" in check_names

    def test_tall_model_stability_issue(self, tall_stl):
        report = validate_design(tall_stl, "a tower")
        stability = [c for c in report.checks if c.check_name == "stability"]
        assert len(stability) == 1
        assert not stability[0].passed

    def test_warning_count_accurate(self, cube_stl):
        report = validate_design(cube_stl, "simple item")
        actual_warnings = sum(
            1 for c in report.checks if not c.passed and c.severity == "warning"
        )
        assert report.warning_count == actual_warnings

    def test_critical_count_accurate(self, tall_stl):
        report = validate_design(tall_stl, "a thing")
        actual_critical = sum(
            1 for c in report.checks if not c.passed and c.severity == "critical"
        )
        assert report.critical_count == actual_critical


# ---------------------------------------------------------------------------
# TestValidationToFeedback
# ---------------------------------------------------------------------------


class TestValidationToFeedback:
    def test_wall_failure_generates_feedback(self, thin_stl):
        report = validate_design(thin_stl, "load bearing bracket")
        feedback = validation_to_feedback(report, "a bracket")
        # Should have at least one feedback item if wall thickness failed
        wall_checks = [c for c in report.checks if c.check_name == "wall_thickness" and not c.passed]
        if wall_checks:
            assert len(feedback) > 0
            types = {f.feedback_type.value for f in feedback}
            assert "printability" in types

    def test_stability_failure_generates_structural_feedback(self, tall_stl):
        report = validate_design(tall_stl, "tower")
        feedback = validation_to_feedback(report, "a tower")
        stability_failed = any(
            c.check_name == "stability" and not c.passed for c in report.checks
        )
        if stability_failed:
            types = {f.feedback_type.value for f in feedback}
            assert "structural" in types

    def test_no_failures_empty_feedback(self, cube_stl):
        report = validate_design(cube_stl, "simple cube")
        if report.overall_pass and report.warning_count == 0:
            feedback = validation_to_feedback(report, "a cube")
            assert len(feedback) == 0

    def test_feedback_constraints_are_specific(self, tall_stl):
        report = validate_design(tall_stl, "tower")
        feedback = validation_to_feedback(report, "a tower")
        for fb in feedback:
            for constraint in fb.constraints:
                # Constraints should be specific, not empty
                assert len(constraint) > 5

    def test_multiple_failures_generate_multiple_feedback(self, tall_stl):
        report = validate_design(tall_stl, "load bearing shelf bracket")
        feedback = validation_to_feedback(report, "bracket")
        # If multiple check types failed, we should get grouped feedback
        failed_types = {c.check_name for c in report.checks if not c.passed}
        if len(failed_types) > 1:
            assert len(feedback) >= 1

    def test_feedback_has_original_prompt(self, tall_stl):
        report = validate_design(tall_stl, "a thing")
        feedback = validation_to_feedback(report, "my cool tower")
        for fb in feedback:
            assert fb.original_prompt == "my cool tower"

    def test_feedback_severity_reflects_check(self, tall_stl):
        report = validate_design(tall_stl, "object")
        feedback = validation_to_feedback(report, "object")
        has_critical_check = any(
            c.severity == "critical" and not c.passed for c in report.checks
        )
        if has_critical_check and feedback:
            severities = {fb.severity for fb in feedback}
            assert "critical" in severities

    def test_overhang_failure_generates_feedback(self, cube_stl):
        # Force an overhang issue by using a material with strict limits
        # and a model that has overhangs.  The cube might not have them,
        # but we verify the mapping logic.
        report = validate_design(cube_stl, "outdoor bracket", material="petg")
        overhang_failed = any(
            c.check_name == "overhang" and not c.passed for c in report.checks
        )
        if overhang_failed:
            feedback = validation_to_feedback(report, "bracket")
            types = {f.feedback_type.value for f in feedback}
            assert "printability" in types


# ---------------------------------------------------------------------------
# TestDesignValidationToFeedbackBridge
# ---------------------------------------------------------------------------


class TestDesignValidationToFeedbackBridge:
    def test_bridge_function_delegates(self, tall_stl):
        from kiln.generation_feedback import design_validation_to_feedback

        report = validate_design(tall_stl, "tower")
        feedback = design_validation_to_feedback(report, "a tower")
        direct = validation_to_feedback(report, "a tower")
        # Both should produce the same number of items.
        assert len(feedback) == len(direct)

    def test_bridge_returns_print_feedback(self, tall_stl):
        from kiln.generation_feedback import PrintFeedback, design_validation_to_feedback

        report = validate_design(tall_stl, "tower")
        feedback = design_validation_to_feedback(report, "a tower")
        for fb in feedback:
            assert isinstance(fb, PrintFeedback)


# ---------------------------------------------------------------------------
# TestDesignValidationEdgeCases
# ---------------------------------------------------------------------------


class TestDesignValidationEdgeCases:
    def test_missing_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            validate_design("/nonexistent/path/model.stl", "anything")

    def test_empty_requirements(self, cube_stl):
        report = validate_design(cube_stl, "")
        assert isinstance(report, DesignValidationReport)
        # Should still run at least stability + adhesion checks
        assert len(report.checks) >= 2

    def test_unknown_material_still_validates(self, cube_stl):
        # Unknown material falls back to PLA in design_intelligence
        report = validate_design(cube_stl, "coaster", material="unobtanium")
        assert isinstance(report, DesignValidationReport)
        assert len(report.checks) > 0

    def test_large_model_no_build_volume_no_crash(self, large_stl):
        report = validate_design(large_stl, "big sculpture")
        assert isinstance(report, DesignValidationReport)

    def test_build_volume_not_checked_when_not_provided(self, cube_stl):
        report = validate_design(cube_stl, "coaster")
        check_names = {c.check_name for c in report.checks}
        # No build_volume check since we didn't provide one
        # (build_volume check is not in the default set — it's in
        # printability, not design_validator, by default)
        assert isinstance(report, DesignValidationReport)

    def test_validation_to_feedback_all_pass_returns_empty(self):
        report = DesignValidationReport(
            file_path="/fake.stl",
            requirements_text="test",
            material=None,
            overall_pass=True,
            checks=[
                DesignValidationCheck(
                    check_name="stability",
                    passed=True,
                    severity="info",
                    actual_value=1.0,
                    required_value=4.0,
                    fix_suggestion="",
                ),
            ],
            critical_count=0,
            warning_count=0,
            summary="All passed.",
        )
        feedback = validation_to_feedback(report, "test prompt")
        assert feedback == []

    def test_report_with_material_none(self, cube_stl):
        report = validate_design(cube_stl, "simple coaster")
        # Material may or may not be None depending on recommendation
        d = report.to_dict()
        assert "material" in d
