from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.mesh_validation_pipeline import (
    ValidationPipelineResult,
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
