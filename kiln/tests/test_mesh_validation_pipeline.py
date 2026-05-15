from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.mesh_validation_pipeline import (
    ValidationPipelineResult,
    _dimensions_from_bbox,
    _mesh_fits_volume,
    run_validation_pipeline,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_mesh_validation_result(
    *,
    valid: bool = True,
    is_manifold: bool = True,
    triangle_count: int = 1000,
    vertex_count: int = 500,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    bounding_box: dict[str, float] | None = None,
) -> MagicMock:
    """Build a mock MeshValidationResult."""
    mock = MagicMock()
    mock.valid = valid
    mock.is_manifold = is_manifold
    mock.triangle_count = triangle_count
    mock.vertex_count = vertex_count
    mock.errors = errors or []
    mock.warnings = warnings or []
    mock.bounding_box = bounding_box or {
        "x_min": 0.0, "x_max": 50.0,
        "y_min": 0.0, "y_max": 50.0,
        "z_min": 0.0, "z_max": 30.0,
    }
    mock.to_dict.return_value = {
        "valid": mock.valid,
        "is_manifold": mock.is_manifold,
        "triangle_count": mock.triangle_count,
        "vertex_count": mock.vertex_count,
        "errors": mock.errors,
        "warnings": mock.warnings,
        "bounding_box": mock.bounding_box,
    }
    return mock


def _make_printability_report(
    *,
    printable: bool = True,
    score: int = 80,
    grade: str = "B",
    recommendations: list[str] | None = None,
) -> MagicMock:
    """Build a mock PrintabilityReport."""
    mock = MagicMock()
    mock.printable = printable
    mock.score = score
    mock.grade = grade
    mock.recommendations = recommendations or [
        "Consider supports for 15% overhang region",
    ]

    # Sub-analysis mocks with to_dict
    for attr in (
        "overhangs", "thin_walls", "bridging", "bed_adhesion",
        "supports", "warping", "thermal_stress", "adhesion_force", "cost",
    ):
        sub = MagicMock()
        sub.to_dict.return_value = {"status": "ok"}
        setattr(mock, attr, sub)

    mock.model_height_mm = 30.0
    mock.estimated_print_time_modifier = 1.0
    mock.to_dict.return_value = {
        "printable": mock.printable,
        "score": mock.score,
        "grade": mock.grade,
        "recommendations": mock.recommendations,
    }
    return mock


def _repair_result(file_path: str) -> dict[str, Any]:
    return {
        "path": file_path,
        "original_triangles": 1005,
        "cleaned_triangles": 1000,
        "degenerate_removed": 5,
        "normals_recomputed": 1000,
    }


def _scale_result(file_path: str, scale: float = 0.5) -> dict[str, Any]:
    return {
        "path": file_path,
        "original_dimensions": {"x": 300.0, "y": 300.0, "z": 200.0},
        "new_dimensions": {
            "x": 300.0 * scale,
            "y": 300.0 * scale,
            "z": 200.0 * scale,
        },
        "scale_factor": scale,
        "scaled": scale < 1.0,
    }


@pytest.fixture
def stl_file(tmp_path):
    """Create a dummy STL file on disk."""
    path = tmp_path / "model.stl"
    path.write_bytes(b"\x00" * 84)  # minimal binary STL header + 0 triangles
    return str(path)


_VALIDATE = "kiln.generation.validation.validate_mesh"
_REPAIR = "kiln.generation.validation.repair_stl"
_REPAIR_ADV = "kiln.generation.validation.repair_stl_advanced"
_PRINTABILITY = "kiln.printability.analyze_printability"
_SCALE = "kiln.generation.validation.scale_to_fit"


# ---------------------------------------------------------------------------
# TestValidationPipelineHappyPath
# ---------------------------------------------------------------------------


class TestValidationPipelineHappyPath:
    """Happy-path scenarios: valid mesh, good printability, fits build volume."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_valid_manifold_mesh_passes(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=80, grade="B")

        result = run_validation_pipeline(stl_file)

        assert result.passed is True
        assert result.was_repaired is False
        assert result.printability_score == 80
        assert result.printability_grade == "B"
        assert result.is_manifold is True
        assert result.triangle_count == 1000
        assert len(result.errors) == 0

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_valid_mesh_with_build_volume_fits(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 0.0, "x_max": 50.0,
                "y_min": 0.0, "y_max": 50.0,
                "z_min": 0.0, "z_max": 30.0,
            },
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(
            stl_file, build_volume=(256.0, 256.0, 256.0),
        )

        assert result.fits_build_volume is True
        assert result.was_scaled is False
        assert result.passed is True

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_dimensions_computed_from_bounding_box(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 10.0, "x_max": 60.0,
                "y_min": 5.0, "y_max": 45.0,
                "z_min": 0.0, "z_max": 30.0,
            },
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.dimensions_mm is not None
        assert result.dimensions_mm["x"] == pytest.approx(50.0, abs=0.1)
        assert result.dimensions_mm["y"] == pytest.approx(40.0, abs=0.1)
        assert result.dimensions_mm["z"] == pytest.approx(30.0, abs=0.1)


# ---------------------------------------------------------------------------
# TestValidationPipelineRepair
# ---------------------------------------------------------------------------


class TestValidationPipelineRepair:
    """Repair flow: basic → advanced → both fail."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR)
    def test_non_manifold_mesh_gets_basic_repair(
        self, mock_repair, mock_validate, mock_printability, stl_file,
    ):
        # First call: not manifold. Second call (after repair): manifold.
        mock_validate.side_effect = [
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=True),
        ]
        mock_repair.return_value = _repair_result(stl_file)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.was_repaired is True
        assert result.is_manifold is True
        assert result.passed is True
        mock_repair.assert_called_once()

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR_ADV)
    @patch(_REPAIR)
    def test_basic_repair_fails_tries_advanced(
        self, mock_repair, mock_repair_adv, mock_validate, mock_printability,
        stl_file,
    ):
        # First call: not manifold. After basic repair: still not manifold.
        # After advanced repair: manifold.
        mock_validate.side_effect = [
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=True),
        ]
        mock_repair.return_value = _repair_result(stl_file)
        mock_repair_adv.return_value = _repair_result(stl_file)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.was_repaired is True
        assert result.is_manifold is True
        assert result.passed is True
        mock_repair.assert_called_once()
        mock_repair_adv.assert_called_once()

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR_ADV)
    @patch(_REPAIR)
    def test_both_repairs_fail_still_returns_result(
        self, mock_repair, mock_repair_adv, mock_validate, mock_printability,
        stl_file,
    ):
        # All validate calls return non-manifold.
        mock_validate.side_effect = [
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=False),
        ]
        mock_repair.return_value = _repair_result(stl_file)
        mock_repair_adv.return_value = _repair_result(stl_file)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.passed is False
        assert result.is_manifold is False
        assert any("manifold" in w.lower() for w in result.warnings)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_auto_repair_disabled_skips_repair(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(is_manifold=False)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, auto_repair=False)

        assert result.was_repaired is False
        assert result.passed is False
        assert result.is_manifold is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR)
    def test_repair_exception_handled_gracefully(
        self, mock_repair, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(is_manifold=False)
        mock_repair.side_effect = OSError("disk full")
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        # Should not crash; error captured.
        assert result.passed is False
        assert any("repair" in e.lower() or "disk full" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# TestValidationPipelinePrintability
# ---------------------------------------------------------------------------


class TestValidationPipelinePrintability:
    """Printability score thresholds, analysis failure, recommendation pass-through."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_low_printability_score_fails(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=30, grade="D")

        result = run_validation_pipeline(stl_file, min_printability_score=40)

        assert result.passed is False
        assert result.printability_score == 30

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_custom_min_score_threshold(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=30, grade="D")

        result = run_validation_pipeline(stl_file, min_printability_score=20)

        assert result.passed is True
        assert result.printability_score == 30

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_printability_analysis_failure_handled(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.side_effect = ValueError("unsupported file format")

        result = run_validation_pipeline(stl_file)

        assert result.passed is False
        assert any("printability" in e.lower() or "unsupported" in e.lower() for e in result.errors)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_recommendations_forwarded(
        self, mock_validate, mock_printability, stl_file,
    ):
        recs = [
            "Consider supports for 15% overhang region",
            "Use brim for better bed adhesion",
        ]
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(recommendations=recs)

        result = run_validation_pipeline(stl_file)

        for rec in recs:
            assert rec in result.recommendations


# ---------------------------------------------------------------------------
# TestValidationPipelineBuildVolume
# ---------------------------------------------------------------------------


class TestValidationPipelineBuildVolume:
    """Build volume checking and auto-scaling."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_mesh_exceeds_build_volume(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 0.0, "x_max": 300.0,
                "y_min": 0.0, "y_max": 300.0,
                "z_min": 0.0, "z_max": 200.0,
            },
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(
            stl_file, build_volume=(256.0, 256.0, 256.0),
        )

        assert result.fits_build_volume is False
        assert result.passed is False

    @patch(_SCALE)
    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_auto_scale_shrinks_to_fit(
        self, mock_validate, mock_printability, mock_scale, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 0.0, "x_max": 300.0,
                "y_min": 0.0, "y_max": 300.0,
                "z_min": 0.0, "z_max": 200.0,
            },
        )
        mock_printability.return_value = _make_printability_report()
        mock_scale.return_value = _scale_result(stl_file, scale=0.85)

        result = run_validation_pipeline(
            stl_file,
            build_volume=(256.0, 256.0, 256.0),
            auto_scale=True,
        )

        assert result.was_scaled is True
        assert result.scale_factor < 1.0
        mock_scale.assert_called_once()

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_auto_scale_disabled_no_scaling(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 0.0, "x_max": 300.0,
                "y_min": 0.0, "y_max": 300.0,
                "z_min": 0.0, "z_max": 200.0,
            },
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(
            stl_file,
            build_volume=(256.0, 256.0, 256.0),
            auto_scale=False,
        )

        assert result.was_scaled is False
        assert result.fits_build_volume is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_no_build_volume_skips_check(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, build_volume=None)

        assert result.fits_build_volume is None
        assert result.was_scaled is False


# ---------------------------------------------------------------------------
# TestValidationPipelineEdgeCases
# ---------------------------------------------------------------------------


class TestValidationPipelineEdgeCases:
    """Edge cases: missing file, empty mesh, summary content, serialization."""

    def test_file_not_found_raises(self):
        with pytest.raises((FileNotFoundError, ValueError), match="(not found|does not exist|No such file)"):
            run_validation_pipeline("/nonexistent/path/model.stl")

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_empty_mesh_handled(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            valid=False,
            triangle_count=0,
            is_manifold=False,
            errors=["Mesh contains no triangles"],
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.passed is False
        assert result.triangle_count == 0
        assert any("triangle" in e.lower() or "empty" in e.lower() for e in result.errors)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_summary_contains_key_info(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=80, grade="B")

        result = run_validation_pipeline(stl_file)

        assert result.summary  # non-empty
        # Summary should mention pass/fail or score
        summary_lower = result.summary.lower()
        assert "pass" in summary_lower or "80" in summary_lower or "b" in summary_lower

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_to_dict_returns_serializable(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)
        result_dict = result.to_dict()

        # Must be JSON-serializable — no dataclass instances, no MagicMock, etc.
        serialized = json.dumps(result_dict)
        assert isinstance(serialized, str)
        assert "passed" in result_dict
        assert "printability_score" in result_dict


# ---------------------------------------------------------------------------
# TestValidationPipelineResult
# ---------------------------------------------------------------------------


class TestValidationPipelineResult:
    """ValidationPipelineResult dataclass structural checks."""

    def test_dataclass_fields(self):
        expected_fields = {
            "passed", "file_path", "original_file_path",
            "is_manifold", "triangle_count",
            "bounding_box", "dimensions_mm",
            "was_repaired", "repair_stats",
            "printability_score", "printability_grade", "printability_details",
            "fits_build_volume", "was_scaled", "scale_factor",
            "errors", "warnings", "recommendations",
            "summary",
        }
        result = ValidationPipelineResult(
            passed=True,
            file_path="/tmp/test.stl",
            original_file_path="/tmp/test.stl",
            is_manifold=True,
            triangle_count=100,
            bounding_box=None,
            dimensions_mm=None,
            was_repaired=False,
            repair_stats=None,
            printability_score=80,
            printability_grade="B",
            printability_details=None,
            fits_build_volume=None,
            was_scaled=False,
            scale_factor=1.0,
            errors=[],
            warnings=[],
            recommendations=[],
            summary="All good",
        )
        actual_fields = {f.name for f in result.__dataclass_fields__.values()}
        assert expected_fields.issubset(actual_fields)

    def test_to_dict_keys(self):
        result = ValidationPipelineResult(
            passed=True,
            file_path="/tmp/test.stl",
            original_file_path="/tmp/test.stl",
            is_manifold=True,
            triangle_count=100,
            bounding_box=None,
            dimensions_mm=None,
            was_repaired=False,
            repair_stats=None,
            printability_score=80,
            printability_grade="B",
            printability_details=None,
            fits_build_volume=None,
            was_scaled=False,
            scale_factor=1.0,
            errors=[],
            warnings=[],
            recommendations=[],
            summary="All good",
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "passed" in d
        assert "file_path" in d
        assert "printability_score" in d
        assert "errors" in d
        assert "summary" in d


# ---------------------------------------------------------------------------
# TestHelperFunctions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Unit tests for internal helper functions."""

    def test_dimensions_from_bbox_normal(self):
        bbox = {"x_min": 10.0, "x_max": 60.0, "y_min": 5.0, "y_max": 45.0, "z_min": 0.0, "z_max": 30.0}
        dims = _dimensions_from_bbox(bbox)
        assert dims == {"x": 50.0, "y": 40.0, "z": 30.0}

    def test_dimensions_from_bbox_none(self):
        assert _dimensions_from_bbox(None) is None

    def test_mesh_fits_volume_fits(self):
        dims = {"x": 50.0, "y": 50.0, "z": 30.0}
        assert _mesh_fits_volume(dims, (256.0, 256.0, 256.0)) is True

    def test_mesh_fits_volume_exceeds(self):
        dims = {"x": 300.0, "y": 50.0, "z": 30.0}
        assert _mesh_fits_volume(dims, (256.0, 256.0, 256.0)) is False

    def test_mesh_fits_volume_none_dims(self):
        assert _mesh_fits_volume(None, (256.0, 256.0, 256.0)) is True

    def test_mesh_fits_volume_exact_boundary(self):
        dims = {"x": 256.0, "y": 256.0, "z": 256.0}
        assert _mesh_fits_volume(dims, (256.0, 256.0, 256.0)) is True

    def test_mesh_fits_volume_exceeds_by_epsilon(self):
        dims = {"x": 256.001, "y": 256.0, "z": 256.0}
        assert _mesh_fits_volume(dims, (256.0, 256.0, 256.0)) is False


# ---------------------------------------------------------------------------
# TestEdgeCasesExpanded
# ---------------------------------------------------------------------------


class TestEdgeCasesExpanded:
    """Extended edge case coverage: boundary values, degenerate inputs, temp files."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_printability_score_exactly_at_threshold_passes(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=40, grade="C")

        result = run_validation_pipeline(stl_file, min_printability_score=40)

        assert result.passed is True
        assert result.printability_score == 40

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_printability_score_one_below_threshold_fails(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=39, grade="D")

        result = run_validation_pipeline(stl_file, min_printability_score=40)

        assert result.passed is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_min_printability_score_zero_always_passes(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=0, grade="F")

        result = run_validation_pipeline(stl_file, min_printability_score=0)

        assert result.passed is True

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_min_printability_score_100_requires_perfect(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=99, grade="A")

        result = run_validation_pipeline(stl_file, min_printability_score=100)

        assert result.passed is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_degenerate_build_volume_zero(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, build_volume=(0.0, 0.0, 0.0))

        assert result.fits_build_volume is False
        assert result.passed is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_empty_material_string(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        # Should not crash with empty material
        result = run_validation_pipeline(stl_file, material="")

        assert result.passed is True

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_unusual_material_name(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, material="CarbonFiber-Nylon")

        assert result.passed is True

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_obj_file_extension(self, mock_validate, mock_printability, tmp_path):
        obj_file = tmp_path / "model.obj"
        obj_file.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")

        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(str(obj_file))

        assert result.passed is True
        assert result.original_file_path == str(obj_file)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR_ADV)
    @patch(_REPAIR)
    def test_was_repaired_false_when_both_repairs_fail(
        self, mock_repair, mock_repair_adv, mock_validate, mock_printability, stl_file,
    ):
        """was_repaired should reflect whether repair actually changed the mesh."""
        mock_validate.side_effect = [
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=False),
        ]
        # Both repairs raise — no modifications were made
        mock_repair.side_effect = RuntimeError("repair engine crashed")
        mock_repair_adv.side_effect = RuntimeError("advanced repair also crashed")
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        # Both repairs threw before modifying anything, so was_repaired should be False
        assert result.was_repaired is False

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR)
    def test_repair_with_runtime_error_handled(
        self, mock_repair, mock_validate, mock_printability, stl_file,
    ):
        """Broad exception types from repair libs should not crash the pipeline."""
        mock_validate.return_value = _make_mesh_validation_result(is_manifold=False)
        mock_repair.side_effect = RuntimeError("numpy internal error")
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.passed is False
        assert any("repair" in e.lower() for e in result.errors)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_printability_import_error_handled(
        self, mock_validate, mock_printability, stl_file,
    ):
        """Missing kiln.printability should degrade gracefully."""
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.side_effect = ImportError("No module named 'kiln.printability'")

        result = run_validation_pipeline(stl_file)

        # ImportError → score stays 0, which is below default threshold → fails
        assert result.passed is False
        assert result.printability_score == 0
        assert any("unavailable" in w.lower() or "printability" in w.lower() for w in result.warnings)

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    @patch(_REPAIR)
    def test_temp_file_created_for_repair(
        self, mock_repair, mock_validate, mock_printability, stl_file,
    ):
        """Repair should work on a temp copy, not the original."""
        mock_validate.side_effect = [
            _make_mesh_validation_result(is_manifold=False),
            _make_mesh_validation_result(is_manifold=True),
        ]
        mock_repair.return_value = _repair_result(stl_file)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert result.was_repaired is True
        # The working file should be a temp copy, not the original
        assert result.file_path != result.original_file_path
        assert "kiln_repair_" in result.file_path

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_summary_mentions_failed_for_failing_mesh(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            is_manifold=False, errors=["non-manifold"],
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, auto_repair=False)

        assert "FAILED" in result.summary

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_summary_mentions_passed_for_passing_mesh(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert "PASSED" in result.summary

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_summary_mentions_triangle_count(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(triangle_count=5432)
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert "5,432" in result.summary

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_validation_errors_propagated_to_result(
        self, mock_validate, mock_printability, stl_file,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            errors=["Degenerate faces detected", "Self-intersecting geometry"],
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file)

        assert "Degenerate faces detected" in result.errors
        assert "Self-intersecting geometry" in result.errors
        assert result.passed is False


# ---------------------------------------------------------------------------
# TestParametrizedScenarios
# ---------------------------------------------------------------------------


class TestParametrizedScenarios:
    """Parametrized tests for material, nozzle, layer height, and threshold combos."""

    @pytest.mark.parametrize("material", ["PLA", "ABS", "PETG", "TPU", "Nylon", ""])
    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_various_materials_accepted(
        self, mock_validate, mock_printability, stl_file, material,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, material=material)

        assert result.passed is True

    @pytest.mark.parametrize(
        "nozzle,layer",
        [(0.2, 0.1), (0.4, 0.2), (0.6, 0.3), (0.8, 0.4), (1.0, 0.5)],
    )
    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_various_nozzle_layer_combos(
        self, mock_validate, mock_printability, stl_file, nozzle, layer,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(
            stl_file, nozzle_diameter=nozzle, layer_height=layer,
        )

        assert result.passed is True

    @pytest.mark.parametrize("threshold", [0, 20, 40, 60, 80, 100])
    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_min_printability_thresholds(
        self, mock_validate, mock_printability, stl_file, threshold,
    ):
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(score=50, grade="C")

        result = run_validation_pipeline(stl_file, min_printability_score=threshold)

        if threshold <= 50:
            assert result.passed is True
        else:
            assert result.passed is False

    @pytest.mark.parametrize(
        "build_vol,should_fit",
        [
            ((256.0, 256.0, 256.0), True),
            ((50.0, 50.0, 30.0), True),   # exact fit
            ((49.0, 50.0, 30.0), False),   # x too small
            ((50.0, 39.0, 30.0), False),   # y too small
            ((50.0, 50.0, 29.0), False),   # z too small
        ],
    )
    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_build_volume_boundary_combos(
        self, mock_validate, mock_printability, stl_file, build_vol, should_fit,
    ):
        mock_validate.return_value = _make_mesh_validation_result(
            bounding_box={
                "x_min": 0.0, "x_max": 50.0,
                "y_min": 0.0, "y_max": 50.0,
                "z_min": 0.0, "z_max": 30.0,
            },
        )
        mock_printability.return_value = _make_printability_report()

        result = run_validation_pipeline(stl_file, build_volume=build_vol)

        assert result.fits_build_volume is should_fit


# ---------------------------------------------------------------------------
# TestMCPToolIntegration
# ---------------------------------------------------------------------------


class TestMCPToolIntegration:
    """Tests for the validate_and_prepare_mesh MCP tool in generation_tools.py."""

    @patch("kiln.mesh_validation_pipeline.run_validation_pipeline")
    def test_validate_and_prepare_mesh_success(self, mock_pipeline, stl_file):
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.summary = "All good"
        mock_result.to_dict.return_value = {"passed": True, "summary": "All good"}
        mock_pipeline.return_value = mock_result

        from kiln.plugins.generation_tools import _GenerationToolsPlugin

        plugin = _GenerationToolsPlugin()
        mcp = MagicMock()

        # Capture registered tools
        registered = {}

        def fake_tool():
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

        mcp.tool = fake_tool
        plugin.register(mcp)

        assert "validate_and_prepare_mesh" in registered

        # Call through with mocked _error_dict
        with patch("kiln.server._error_dict", side_effect=lambda msg, **kw: {"error": msg}):
            result = registered["validate_and_prepare_mesh"](
                file_path=stl_file,
                material="PLA",
            )

        assert result["success"] is True
        assert result["passed"] is True

    @patch("kiln.mesh_validation_pipeline.run_validation_pipeline")
    def test_validate_and_prepare_mesh_pipeline_crash(self, mock_pipeline, stl_file):
        mock_pipeline.side_effect = RuntimeError("segfault in mesh lib")

        from kiln.plugins.generation_tools import _GenerationToolsPlugin

        plugin = _GenerationToolsPlugin()
        mcp = MagicMock()
        registered = {}

        def fake_tool():
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

        mcp.tool = fake_tool
        plugin.register(mcp)

        with patch("kiln.server._error_dict", side_effect=lambda msg, **kw: {"error": msg}):
            result = registered["validate_and_prepare_mesh"](
                file_path=stl_file,
            )

        assert "error" in result
        assert "Unexpected error" in result["error"]

    @patch("kiln.mesh_validation_pipeline.run_validation_pipeline")
    def test_validate_and_prepare_mesh_with_build_volume(self, mock_pipeline, stl_file):
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.summary = "Fits"
        mock_result.to_dict.return_value = {"passed": True}
        mock_pipeline.return_value = mock_result

        from kiln.plugins.generation_tools import _GenerationToolsPlugin

        plugin = _GenerationToolsPlugin()
        mcp = MagicMock()
        registered = {}

        def fake_tool():
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

        mcp.tool = fake_tool
        plugin.register(mcp)

        with patch("kiln.server._error_dict", side_effect=lambda msg, **kw: {"error": msg}):
            registered["validate_and_prepare_mesh"](
                file_path=stl_file,
                build_volume_x=256.0,
                build_volume_y=256.0,
                build_volume_z=256.0,
            )

        # Verify the pipeline was called with the correct build volume tuple
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["build_volume"] == (256.0, 256.0, 256.0)

    @patch("kiln.mesh_validation_pipeline.run_validation_pipeline")
    def test_validate_and_prepare_mesh_partial_build_volume_ignored(
        self, mock_pipeline, stl_file,
    ):
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.summary = "OK"
        mock_result.to_dict.return_value = {"passed": True}
        mock_pipeline.return_value = mock_result

        from kiln.plugins.generation_tools import _GenerationToolsPlugin

        plugin = _GenerationToolsPlugin()
        mcp = MagicMock()
        registered = {}

        def fake_tool():
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

        mcp.tool = fake_tool
        plugin.register(mcp)

        with patch("kiln.server._error_dict", side_effect=lambda msg, **kw: {"error": msg}):
            registered["validate_and_prepare_mesh"](
                file_path=stl_file,
                build_volume_x=256.0,
                # y and z not provided
            )

        # Partial build volume should result in None
        call_kwargs = mock_pipeline.call_args[1]
        assert call_kwargs["build_volume"] is None


# ---------------------------------------------------------------------------
# TestValidationPipelineInspectionBundle
#
# Consumer-proof for the bundle-as-lingua-franca rollout.  When a
# pre-built inspection bundle is provided, the pipeline reads
# printability findings from it instead of re-running
# analyze_printability.  Same answer, half the cost.
# ---------------------------------------------------------------------------


def _make_inspection_bundle(
    *, printability_score: int = 75, printability_grade: str = "B",
    recommendations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a synthetic inspection-bundle dict shaped like what
    ``attach_inspect_bundle`` emits, with just the printability channel
    populated (the only channel the validation pipeline reads)."""
    return {
        "schema_version": "1.0",
        "source_path": "/tmp/synthetic.stl",
        "source_sha": "deadbeef" * 8,
        "bundle_dir": "/tmp/synthetic-bundle",
        "channels_requested": ["printability"],
        "channels_emitted": ["printability"],
        "channels": {
            "printability": {
                "name": "printability",
                "tier": "pro",
                "status": "ok",
                "images": [],
                "findings": {
                    "score": printability_score,
                    "grade": printability_grade,
                    "printable": printability_score >= 50,
                    "recommendations": list(recommendations or []),
                },
                "summary": f"grade {printability_grade}",
                "error": None,
                "elapsed_ms": 0,
            },
        },
        "scene": {},
    }


class TestValidationPipelineInspectionBundle:
    """When ``inspection_bundle`` is provided, the pipeline reads
    printability findings from it rather than re-running analysis."""

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_bundle_skips_analyze_printability_call(
        self, mock_validate, mock_printability, stl_file,
    ):
        """analyze_printability MUST NOT be called when the bundle has
        printability findings — that's the whole point of the
        consumer-proof refactor."""
        mock_validate.return_value = _make_mesh_validation_result()
        # If this gets called, the regression has happened.
        mock_printability.return_value = _make_printability_report(
            score=99, grade="A",
        )

        bundle = _make_inspection_bundle(
            printability_score=72, printability_grade="C",
        )
        result = run_validation_pipeline(stl_file, inspection_bundle=bundle)

        mock_printability.assert_not_called()
        assert result.printability_score == 72
        assert result.printability_grade == "C"

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_bundle_findings_drive_the_score(
        self, mock_validate, mock_printability, stl_file,
    ):
        """The score field on the result reflects what the BUNDLE said,
        not what the (unused) analyze_printability would have said."""
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(
            score=99, grade="A",  # ignored — bundle wins
        )

        bundle = _make_inspection_bundle(
            printability_score=42,
            printability_grade="F",
            recommendations=["increase wall count", "add brim"],
        )
        result = run_validation_pipeline(stl_file, inspection_bundle=bundle)

        assert result.printability_score == 42
        assert result.printability_grade == "F"
        assert "increase wall count" in result.recommendations
        assert "add brim" in result.recommendations

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_no_bundle_legacy_path_unchanged(
        self, mock_validate, mock_printability, stl_file,
    ):
        """When ``inspection_bundle`` is None, behavior is identical to
        before the refactor — analyze_printability runs, its result is
        used."""
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(
            score=80, grade="B",
        )

        result = run_validation_pipeline(stl_file)  # no inspection_bundle

        mock_printability.assert_called_once()
        assert result.printability_score == 80
        assert result.printability_grade == "B"
        assert result.inspection_bundle is None

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_bundle_rides_along_on_result(
        self, mock_validate, mock_printability, stl_file,
    ):
        """The bundle dict appears on the result so downstream
        consumers can read other channels (e.g. rgb evidence PNGs)
        without re-running anything."""
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report()

        bundle = _make_inspection_bundle()
        result = run_validation_pipeline(stl_file, inspection_bundle=bundle)

        assert result.inspection_bundle is bundle

    @patch(_PRINTABILITY)
    @patch(_VALIDATE)
    def test_bundle_without_printability_findings_falls_through(
        self, mock_validate, mock_printability, stl_file,
    ):
        """A bundle with no printability channel (e.g. ``level="quick"``
        which only ran rgb + measurements) falls through to the legacy
        analyze_printability path — graceful degradation."""
        mock_validate.return_value = _make_mesh_validation_result()
        mock_printability.return_value = _make_printability_report(
            score=80, grade="B",
        )

        # Bundle without printability channel — only rgb ran.
        partial_bundle = {
            "schema_version": "1.0",
            "channels": {
                "rgb": {
                    "name": "rgb",
                    "tier": "free",
                    "status": "ok",
                    "findings": {"view_count": 4},
                },
            },
            "channels_emitted": ["rgb"],
        }
        result = run_validation_pipeline(
            stl_file, inspection_bundle=partial_bundle,
        )

        mock_printability.assert_called_once()
        assert result.printability_score == 80
        # Bundle still rides along on the result for downstream readers.
        assert result.inspection_bundle is partial_bundle
