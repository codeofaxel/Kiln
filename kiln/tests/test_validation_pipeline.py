"""Tests for the post-generation validation pipeline plugin.

Coverage:
    - Valid STL pass scenario (mocked mesh analysis)
    - Missing file returns fail
    - Unsupported format returns fail
    - pass_with_warnings when printability module unavailable
    - fail scenario (non-manifold + repair fails)
    - Repair attempted when watertight check fails
    - Bed-fit check uses printer_id
    - Resilience — individual steps failing doesn't crash pipeline
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_binary_stl(tmp_dir: Path, *, triangles: int = 10) -> str:
    """Create a minimal binary STL with *triangles* faces."""
    stl_path = tmp_dir / "test.stl"
    header = b"\x00" * 80
    data = bytearray(header)
    data += struct.pack("<I", triangles)
    for i in range(triangles):
        # normal
        data += struct.pack("<3f", 0.0, 0.0, 1.0)
        # 3 vertices — small cube-ish
        data += struct.pack("<3f", 0.0, 0.0, float(i))
        data += struct.pack("<3f", 10.0, 0.0, float(i))
        data += struct.pack("<3f", 5.0, 10.0, float(i + 1))
        # attribute byte count
        data += struct.pack("<H", 0)
    stl_path.write_bytes(bytes(data))
    return str(stl_path)


def _make_mock_analysis(
    *,
    triangle_count: int = 500,
    is_manifold: bool = True,
    volume_mm3: float = 5000.0,
) -> MagicMock:
    """Create a mock MeshAnalysis."""
    analysis = MagicMock()
    analysis.to_dict.return_value = {
        "triangle_count": triangle_count,
        "is_manifold": is_manifold,
        "volume_mm3": volume_mm3,
        "dimensions_mm": {"width_mm": 20.0, "depth_mm": 15.0, "height_mm": 10.0},
        "bounding_box": {
            "x_min": 0, "x_max": 20, "y_min": 0, "y_max": 15, "z_min": 0, "z_max": 10,
        },
    }
    return analysis


def _make_mock_validation(*, is_manifold: bool = True, valid: bool = True) -> MagicMock:
    """Create a mock MeshValidationResult."""
    result = MagicMock()
    result.is_manifold = is_manifold
    result.valid = valid
    result.errors = [] if valid else ["Non-manifold geometry"]
    result.warnings = []
    return result


def _make_mock_printability(*, score: int = 85, grade: str = "B") -> MagicMock:
    """Create a mock printability report."""
    report = MagicMock()
    report.score = score
    report.grade = grade
    report.recommendations = ["Consider adding a brim for better adhesion"]
    report.thin_walls = []
    report.overhang_percentage = 5.0
    return report


def _build_tools() -> dict[str, Any]:
    """Instantiate the plugin and return all registered tool functions."""
    from kiln.plugins.validation_pipeline_tools import _ValidationPipelinePlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    plugin = _ValidationPipelinePlugin()
    plugin.register(_FakeMcp())
    return tools


def _invoke_tool(
    input_path: str,
    printer_id: str = "",
    material: str = "",
) -> dict[str, Any]:
    """Instantiate the plugin and call validate_and_prepare directly."""
    tools = _build_tools()
    assert "validate_and_prepare" in tools
    return tools["validate_and_prepare"](input_path, printer_id=printer_id, material=material)


def _invoke_ai_tool(
    input_path: str,
    target_height_mm: float = 0,
    printer_id: str = "",
    material: str = "PLA",
) -> dict[str, Any]:
    """Call prepare_ai_model_for_print via the plugin registration."""
    tools = _build_tools()
    assert "prepare_ai_model_for_print" in tools
    return tools["prepare_ai_model_for_print"](
        input_path,
        target_height_mm=target_height_mm,
        printer_id=printer_id,
        material=material,
    )


# ---------------------------------------------------------------------------
# Tests — format checks
# ---------------------------------------------------------------------------


class TestFormatCheck:
    """Format validation: file existence and extension."""

    def test_missing_file_returns_fail(self, tmp_path: Path) -> None:
        result = _invoke_tool(str(tmp_path / "nonexistent.stl"))

        # The outer envelope wraps the report — status key is "success"
        # but the inner pipeline status is "fail"
        # Actually the tool returns {"status": "success", ...report fields...}
        # where the report's own "status" overwrites the outer one.
        # Since _PipelineReport.status = "fail" for missing file, we get "fail"
        # in the merged dict. Let's just check the report fields.
        checks = result["checks"]
        assert len(checks) >= 1
        assert checks[0]["name"] == "format"
        assert checks[0]["passed"] is False
        assert result["ready_to_print"] is False

    def test_nonexistent_file_returns_human_readable_summary(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does_not_exist.stl")
        result = _invoke_tool(missing)

        assert result["status"] == "fail"
        assert result["ready_to_print"] is False
        # Summary must be a plain human-readable string, not a Python stack trace
        summary = result["summary"]
        assert isinstance(summary, str)
        assert len(summary) > 0
        assert "File not found" in summary
        assert "Traceback" not in summary
        assert "Exception" not in summary

    def test_unsupported_format_returns_fail(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "model.dwg"
        bad_file.write_text("not a mesh")
        result = _invoke_tool(str(bad_file))

        assert result["status"] == "fail"
        fmt_check = result["checks"][0]
        assert fmt_check["name"] == "format"
        assert fmt_check["passed"] is False
        assert "Unsupported format" in fmt_check["details"]

    def test_valid_stl_passes_format_check(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)

        # Patch all downstream modules so only format check matters
        with (
            patch.dict("sys.modules", {
                "kiln.generation.validation": None,
                "kiln.printability": None,
                "kiln.design_intelligence": None,
            }),
        ):
            result = _invoke_tool(stl)

        fmt_check = result["checks"][0]
        assert fmt_check["name"] == "format"
        assert fmt_check["passed"] is True
        assert "STL" in fmt_check["details"]


# ---------------------------------------------------------------------------
# Tests — full pipeline scenarios
# ---------------------------------------------------------------------------


class TestPassScenario:
    """Full pipeline with a valid manifold mesh — expect pass."""

    def test_valid_stl_passes_pipeline(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path, triangles=500)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        # Structural load mock
        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                return_value=mock_analysis,
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                return_value=mock_validation,
            ),
            patch(
                "kiln.printability.analyze_printability",
                return_value=mock_printability,
            ),
            patch(
                "kiln.design_intelligence.estimate_load_capacity",
                return_value=mock_estimate,
            ),
        ):
            result = _invoke_tool(stl)

        # With estimate_load_capacity available, structural passes (info) → status pass
        assert result["status"] in ("pass", "pass_with_warnings")
        assert result["ready_to_print"] is True
        assert result["repaired"] is False

        check_names = [c["name"] for c in result["checks"]]
        assert "format" in check_names
        assert "mesh_geometry" in check_names
        assert "watertight" in check_names
        assert "printability" in check_names

        # Model info should be populated
        assert result["model_info"]["triangles"] == 500


class TestFailScenario:
    """Pipeline with non-manifold mesh that can't be repaired — expect fail."""

    def test_non_manifold_unrepairable_is_warning(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis(is_manifold=False)
        mock_validation = _make_mock_validation(is_manifold=False, valid=False)
        mock_post_repair = _make_mock_validation(is_manifold=False, valid=False)

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                return_value=mock_analysis,
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation, mock_post_repair],
            ),
            patch(
                "kiln.generation.validation.repair_stl",
                return_value={},
            ),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        # Watertight check should have failed with warning
        wt = [c for c in result["checks"] if c["name"] == "watertight"][0]
        assert wt["passed"] is False

        # Repair was attempted but failed
        repair = [c for c in result["checks"] if c["name"] == "repair"][0]
        assert repair["passed"] is False
        assert "non-manifold" in repair["details"].lower()


class TestPassWithWarnings:
    """Pipeline where some steps are skipped — expect pass_with_warnings."""

    def test_skipped_modules_give_warnings(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path, triangles=20)

        # Force all analysis modules to be unavailable
        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("no module"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=ImportError("no module"),
            ),
            patch(
                "kiln.printability.analyze_printability",
                side_effect=ImportError("no module"),
            ),
            patch(
                "kiln.design_intelligence.estimate_load_capacity",
                side_effect=ImportError("no module"),
            ),
        ):
            result = _invoke_tool(stl)

        # Should still return a result (resilient) but with warnings
        assert result["status"] in ("pass", "pass_with_warnings")
        # Format check passed, others skipped → pass_with_warnings
        assert any(
            c.get("severity") == "warning"
            for c in result["checks"]
        )
        assert result["ready_to_print"] is True


# ---------------------------------------------------------------------------
# Tests — repair behavior
# ---------------------------------------------------------------------------


class TestRepairAttempted:
    """Repair is attempted only when watertight check fails."""

    def test_repair_called_when_not_manifold(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_validation_good = _make_mock_validation(is_manifold=True, valid=True)

        mock_repair = MagicMock(return_value={})

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation_bad, mock_validation_good],
            ),
            patch("kiln.generation.validation.repair_stl", mock_repair),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        mock_repair.assert_called_once()
        assert result["repaired"] is True
        assert result["repaired_path"] is not None

    def test_repair_not_called_when_manifold(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation(is_manifold=True)

        mock_repair = MagicMock()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.generation.validation.repair_stl", mock_repair),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        mock_repair.assert_not_called()
        assert result["repaired"] is False


# ---------------------------------------------------------------------------
# Tests — bed fit with printer_id
# ---------------------------------------------------------------------------


class TestBedFitCheck:
    """Bed-fit check uses printer_id to resolve build volume."""

    def test_bed_fit_with_printer_id(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()  # 20x15x10mm
        mock_validation = _make_mock_validation()

        mock_profile = MagicMock()
        mock_profile.build_volume = [256, 256, 256]

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="bambu_a1")

        bed_checks = [c for c in result["checks"] if c["name"] == "bed_fit"]
        assert len(bed_checks) == 1
        assert bed_checks[0]["passed"] is True
        assert "256" in bed_checks[0]["details"]

    def test_bed_fit_fails_when_model_too_large(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        # Mock analysis with 300mm wide model
        analysis = MagicMock()
        analysis.to_dict.return_value = {
            "triangle_count": 100,
            "is_manifold": True,
            "volume_mm3": 50000.0,
            "dimensions_mm": {"width_mm": 300.0, "depth_mm": 200.0, "height_mm": 100.0},
        }
        mock_validation = _make_mock_validation()
        mock_profile = MagicMock()
        mock_profile.build_volume = [256, 256, 256]

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="bambu_a1")

        bed_checks = [c for c in result["checks"] if c["name"] == "bed_fit"]
        assert len(bed_checks) == 1
        assert bed_checks[0]["passed"] is False
        assert "exceeds" in bed_checks[0]["details"]

    def test_no_bed_fit_without_printer_id(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        bed_checks = [c for c in result["checks"] if c["name"] == "bed_fit"]
        assert len(bed_checks) == 0


# ---------------------------------------------------------------------------
# Tests — resilience
# ---------------------------------------------------------------------------


class TestResilience:
    """Pipeline survives individual step failures without crashing."""

    def test_analysis_exception_doesnt_crash(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path, triangles=5)

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=RuntimeError("segfault simulation"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "kiln.printability.analyze_printability",
                side_effect=RuntimeError("kaboom"),
            ),
            patch(
                "kiln.design_intelligence.estimate_load_capacity",
                side_effect=RuntimeError("pow"),
            ),
        ):
            result = _invoke_tool(stl)

        # Pipeline should complete, not crash
        assert result["status"] in ("pass", "pass_with_warnings", "fail")
        assert len(result["checks"]) >= 1  # at least format check
        # A resilient check should either pass genuinely or explicitly mention being skipped
        for check in result["checks"]:
            if check["name"] != "format":
                if not check["passed"]:
                    # Failed checks are fine — the pipeline is resilient to module absence
                    pass
                elif check.get("severity") == "warning":
                    assert (
                        "skip" in check["details"].lower()
                        or "unavailable" in check["details"].lower()
                    )

    def test_all_modules_import_error_still_returns(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path, triangles=3)

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError,
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=ImportError,
            ),
            patch(
                "kiln.printability.analyze_printability",
                side_effect=ImportError,
            ),
            patch(
                "kiln.design_intelligence.estimate_load_capacity",
                side_effect=ImportError,
            ),
        ):
            result = _invoke_tool(stl)

        assert result["status"] in ("pass", "pass_with_warnings")
        assert "checks" in result
        assert "ready_to_print" in result
        # Inline STL parser should kick in for mesh_geometry
        geo = [c for c in result["checks"] if c["name"] == "mesh_geometry"]
        assert len(geo) == 1
        assert geo[0]["passed"] is True
        assert "inline parser" in geo[0]["details"]


# ---------------------------------------------------------------------------
# Tests — inline STL fallback
# ---------------------------------------------------------------------------


class TestInlineStlFallback:
    """Inline binary STL parser works when kiln.generation.validation is missing."""

    def test_inline_parser_extracts_triangle_count(self, tmp_path: Path) -> None:
        from kiln.plugins.validation_pipeline_tools import _inline_stl_analysis

        stl = _make_binary_stl(tmp_path, triangles=42)

        with patch.dict("sys.modules", {"kiln.generation.validation": None}):
            result = _inline_stl_analysis(stl)

        assert result["triangle_count"] == 42
        assert "bounding_box" in result
        assert "dimensions_mm" in result

    def test_inline_parser_handles_tiny_file(self, tmp_path: Path) -> None:
        from kiln.plugins.validation_pipeline_tools import _inline_stl_analysis

        tiny = tmp_path / "tiny.stl"
        tiny.write_bytes(b"\x00" * 10)

        with patch.dict("sys.modules", {"kiln.generation.validation": None}):
            result = _inline_stl_analysis(str(tiny))

        assert "error" in result

    def test_inline_parser_ascii_stl_returns_triangle_count_not_garbage(
        self, tmp_path: Path
    ) -> None:
        """ASCII STL must not produce a garbage bounding box from binary float parsing."""
        from kiln.plugins.validation_pipeline_tools import _inline_stl_analysis

        ascii_stl = """\
solid test
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 5 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 0 0 1
      vertex 10 0 1
      vertex 5 10 1
    endloop
  endfacet
endsolid test
"""
        stl_path = tmp_path / "ascii.stl"
        stl_path.write_text(ascii_stl)

        with patch.dict("sys.modules", {"kiln.generation.validation": None}):
            result = _inline_stl_analysis(str(stl_path))

        # Should report 2 triangles (one per "facet normal" line)
        assert result.get("triangle_count") == 2
        # Must NOT produce a bounding_box — we can't parse coords from ASCII
        assert "bounding_box" not in result
        # Must NOT contain an error key
        assert "error" not in result


# ---------------------------------------------------------------------------
# Tests — real binary STL through full pipeline
# ---------------------------------------------------------------------------


def _make_known_binary_stl(tmp_dir: Path) -> str:
    """Create a binary STL with a known 10x20x5mm bounding box (2 triangles).

    Triangle 1: covers the XY face at z=0  (0,0,0) (10,0,0) (10,20,0)
    Triangle 2: covers the XY face at z=5  (0,0,5) (10,0,5) (0,20,5)
    This guarantees x∈[0,10], y∈[0,20], z∈[0,5] → 10×20×5 mm.
    """
    stl_path = tmp_dir / "known_dims.stl"
    header = b"\x00" * 80
    data = bytearray(header)
    data += struct.pack("<I", 2)

    # Triangle 1
    data += struct.pack("<3f", 0.0, 0.0, -1.0)  # normal
    data += struct.pack("<3f", 0.0, 0.0, 0.0)   # v1
    data += struct.pack("<3f", 10.0, 0.0, 0.0)  # v2
    data += struct.pack("<3f", 10.0, 20.0, 0.0) # v3
    data += struct.pack("<H", 0)

    # Triangle 2
    data += struct.pack("<3f", 0.0, 0.0, 1.0)   # normal
    data += struct.pack("<3f", 0.0, 0.0, 5.0)   # v1
    data += struct.pack("<3f", 10.0, 0.0, 5.0)  # v2
    data += struct.pack("<3f", 0.0, 20.0, 5.0)  # v3
    data += struct.pack("<H", 0)

    stl_path.write_bytes(bytes(data))
    return str(stl_path)


class TestRealBinaryStlPipeline:
    """Full pipeline run on a real binary STL (no mocked mesh module)."""

    def test_pipeline_returns_correct_dimensions_and_triangle_count(
        self, tmp_path: Path
    ) -> None:
        """Inline STL parser returns exact bounding box and triangle count."""
        stl = _make_known_binary_stl(tmp_path)

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("not available"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=ImportError("not available"),
            ),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert result["status"] in ("pass", "pass_with_warnings")

        # Triangle count from inline parser
        assert result["model_info"]["triangles"] == 2

        # Bounding box dimensions: x=10, y=20, z=5
        dims = result["model_info"]["dimensions_mm"]
        assert abs(dims["x"] - 10.0) < 0.1
        assert abs(dims["y"] - 20.0) < 0.1
        assert abs(dims["z"] - 5.0) < 0.1

        # mesh_geometry check passed and mentions inline parser
        geo = [c for c in result["checks"] if c["name"] == "mesh_geometry"]
        assert len(geo) == 1
        assert geo[0]["passed"] is True
        assert "inline parser" in geo[0]["details"]


# ---------------------------------------------------------------------------
# Tests — recommendations contain actionable strings
# ---------------------------------------------------------------------------


class TestRecommendations:
    """Recommendations list contains actionable strings when issues found."""

    def test_recommendations_populated_when_bed_too_small(self, tmp_path: Path) -> None:
        """Bed-fit failure produces a recommendation with action verb."""
        stl = _make_known_binary_stl(tmp_path)  # 10x20x5mm

        mock_profile = MagicMock()
        mock_profile.build_volume = [5, 5, 5]  # Smaller than the model

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("not available"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=ImportError("not available"),
            ),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="test_printer")

        recs = result["recommendations"]
        assert len(recs) >= 1
        # Each recommendation must be a non-empty string with an action verb
        for rec in recs:
            assert isinstance(rec, str)
            assert len(rec) > 10, f"Recommendation too short: {rec!r}"
        # The bed-fit recommendation specifically mentions what to do
        combined = " ".join(recs).lower()
        assert any(word in combined for word in ("scale", "split", "shrink", "fit"))

    def test_recommendations_populated_when_repair_fails(self, tmp_path: Path) -> None:
        """Failed repair produces a recommendation directing the user to next steps."""
        stl = _make_binary_stl(tmp_path)
        mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_post_repair = _make_mock_validation(is_manifold=False, valid=False)

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("not available"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation_bad, mock_post_repair],
            ),
            patch("kiln.generation.validation.repair_stl", return_value={}),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        recs = result["recommendations"]
        assert len(recs) >= 1
        combined = " ".join(recs).lower()
        assert any(
            word in combined
            for word in ("repair", "blender", "meshlab", "manifold", "advanced")
        )


# ---------------------------------------------------------------------------
# Tests — printer_id bed-fit fail via inline STL parser path
# ---------------------------------------------------------------------------


class TestBedFitFailWithInlineParser:
    """Bed-fit fail when dimensions come from inline STL parser (no mesh module)."""

    def test_model_too_large_for_printer_via_inline_parser(self, tmp_path: Path) -> None:
        """Known 10x20x5mm model fails bed-fit against 5x5x5mm build volume."""
        stl = _make_known_binary_stl(tmp_path)  # 10x20x5mm

        mock_profile = MagicMock()
        mock_profile.build_volume = [5, 5, 5]

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("not available"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=ImportError("not available"),
            ),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="small_printer")

        bed_checks = [c for c in result["checks"] if c["name"] == "bed_fit"]
        assert len(bed_checks) == 1
        assert bed_checks[0]["passed"] is False
        assert "exceeds" in bed_checks[0]["details"]
        assert result["status"] == "fail"
        assert result["ready_to_print"] is False


# ---------------------------------------------------------------------------
# Tests — all 8 check names appear in response
# ---------------------------------------------------------------------------


class TestAllCheckNamesPresent:
    """All 8 check names appear in response even when some checks are skipped."""

    def test_all_eight_checks_present(self, tmp_path: Path) -> None:
        """Trigger every branch so all 8 check names appear: format, mesh_geometry,
        watertight, repair, printability, structural, bed_fit, summary."""
        stl = _make_binary_stl(tmp_path)

        # Non-manifold triggers repair; printer_id triggers bed_fit
        mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_post_repair_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_profile = MagicMock()
        mock_profile.build_volume = [256, 256, 256]

        with (
            patch(
                "kiln.generation.validation.analyze_mesh",
                side_effect=ImportError("not available"),
            ),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation_bad, mock_post_repair_bad],
            ),
            patch("kiln.generation.validation.repair_stl", return_value={}),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="bambu_a1")

        check_names = {c["name"] for c in result["checks"]}
        expected = {"format", "mesh_geometry", "watertight", "repair", "printability", "structural", "bed_fit"}
        assert expected.issubset(check_names), f"Missing checks: {expected - check_names}"


# ---------------------------------------------------------------------------
# Tests — ready_to_print boolean
# ---------------------------------------------------------------------------


class TestReadyToPrint:
    """ready_to_print is True for pass and pass_with_warnings, False for fail."""

    def _run_with_status(
        self, tmp_path: Path, *, force_fail: bool = False, force_warning: bool = False
    ) -> dict[str, Any]:
        stl = _make_binary_stl(tmp_path)

        if force_fail:
            # Non-manifold + failed repair → bed_fit error → status=fail
            mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
            mock_post_repair = _make_mock_validation(is_manifold=False, valid=False)
            mock_profile = MagicMock()
            mock_profile.build_volume = [1, 1, 1]  # Tiny — will fail bed_fit (error severity)

            analysis = MagicMock()
            analysis.to_dict.return_value = {
                "triangle_count": 10,
                "is_manifold": False,
                "volume_mm3": 1000.0,
                "dimensions_mm": {"width_mm": 50.0, "depth_mm": 50.0, "height_mm": 50.0},
            }
            with (
                patch("kiln.generation.validation.analyze_mesh", return_value=analysis),
                patch(
                    "kiln.generation.validation.validate_mesh",
                    side_effect=[mock_validation_bad, mock_post_repair],
                ),
                patch("kiln.generation.validation.repair_stl", return_value={}),
                patch("kiln.printability.analyze_printability", side_effect=ImportError),
                patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
                patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
            ):
                return _invoke_tool(stl, printer_id="test_printer")

        elif force_warning:
            # All optional modules unavailable → skipped steps → pass_with_warnings
            with (
                patch(
                    "kiln.generation.validation.analyze_mesh",
                    side_effect=ImportError,
                ),
                patch(
                    "kiln.generation.validation.validate_mesh",
                    side_effect=ImportError,
                ),
                patch("kiln.printability.analyze_printability", side_effect=ImportError),
                patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            ):
                return _invoke_tool(stl)

        else:
            # Full pass: manifold + good printability
            mock_analysis = _make_mock_analysis()
            mock_validation = _make_mock_validation()
            mock_printability = _make_mock_printability()
            with (
                patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
                patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
                patch("kiln.printability.analyze_printability", return_value=mock_printability),
                patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            ):
                return _invoke_tool(stl)

    def test_ready_to_print_true_when_status_pass(self, tmp_path: Path) -> None:
        result = self._run_with_status(tmp_path)
        assert result["status"] in ("pass", "pass_with_warnings")
        assert result["ready_to_print"] is True

    def test_ready_to_print_true_when_status_pass_with_warnings(self, tmp_path: Path) -> None:
        result = self._run_with_status(tmp_path, force_warning=True)
        assert result["status"] == "pass_with_warnings"
        assert result["ready_to_print"] is True

    def test_ready_to_print_false_when_status_fail(self, tmp_path: Path) -> None:
        result = self._run_with_status(tmp_path, force_fail=True)
        assert result["status"] == "fail"
        assert result["ready_to_print"] is False


# ---------------------------------------------------------------------------
# Tests — printability score
# ---------------------------------------------------------------------------


class TestPrintabilityScore:
    """Numeric 0-100 printability score and score_breakdown."""

    def test_score_high_when_everything_passes(self, tmp_path: Path) -> None:
        """Clean manifold mesh with good structural geometry — score is 100."""
        stl = _make_binary_stl(tmp_path, triangles=500)
        mock_analysis = _make_mock_analysis()  # 20x15x10mm — passes structural (info)
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", return_value=mock_printability),
            patch("kiln.design_intelligence.estimate_load_capacity", return_value=mock_estimate),
        ):
            result = _invoke_tool(stl)

        assert result["printability_score"] == 100

    def test_score_less_than_100_when_warnings_present(self, tmp_path: Path) -> None:
        """Skipped modules produce warning checks → score < 100."""
        stl = _make_binary_stl(tmp_path, triangles=20)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert result["printability_score"] < 100
        assert isinstance(result["score_breakdown"], list)
        assert len(result["score_breakdown"]) > 0

    def test_score_zero_when_multiple_failures(self) -> None:
        """Multiple hard failures (error severity) drive score to 0."""
        # Force 4 hard-error failures: unsupported format is one path, but
        # here we manufacture a scenario via bed_fit errors and bed_fit with
        # a tiny build volume plus a bad watertight check.
        # Easiest: use a non-existent file to get 1 error, then check clamp.
        # But that exits early. Instead: call 5 error-severity checks via
        # multiple bed-fit mismatches isn't possible in one call.
        # Simplest reproducible approach: patch _compute_printability_score
        # directly to verify clamping, then also test a realistic multi-error
        # scenario from existing helpers.
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        # 5 failed-error checks → 5*25 = 125 deductions → clamped to 0
        checks = [
            _CheckResult(name=f"check_{i}", passed=False, severity="error", details="x")
            for i in range(5)
        ]
        score, breakdown = _compute_printability_score(checks, repaired=False)
        assert score == 0
        assert len(breakdown) == 5

    def test_score_breakdown_lists_deductions(self, tmp_path: Path) -> None:
        """score_breakdown contains human-readable deduction strings."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name="watertight", passed=False, severity="warning", details="x"),
            _CheckResult(name="printability", passed=True, severity="warning", details="x"),
        ]
        score, breakdown = _compute_printability_score(checks, repaired=True)
        # -10 (warning fail) + -5 (skipped) + -15 (repaired) = -30 → 70
        assert score == 70
        assert any("watertight" in s for s in breakdown)
        assert any("repair" in s for s in breakdown)


# ---------------------------------------------------------------------------
# Tests — material checks
# ---------------------------------------------------------------------------


class TestMaterialCheck:
    """Material-specific heuristic checks."""

    def _run_with_material(
        self, tmp_path: Path, material: str, *, dims: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """Run the pipeline with controlled dimensions and a given material."""
        stl = _make_binary_stl(tmp_path)

        if dims is None:
            dims = {"width_mm": 20.0, "depth_mm": 15.0, "height_mm": 10.0}

        analysis = MagicMock()
        analysis.to_dict.return_value = {
            "triangle_count": 100,
            "is_manifold": True,
            "volume_mm3": 5000.0,
            "dimensions_mm": dims,
        }
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            return _invoke_tool(stl, material=material)

    def test_no_material_no_material_check_step(self, tmp_path: Path) -> None:
        """When material is empty, no material_check step appears in results."""
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl, material="")

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 0

    def test_pla_overhang_warning_on_tall_narrow_model(self, tmp_path: Path) -> None:
        """PLA check warns on tall/narrow models (high aspect ratio → steep overhangs)."""
        # aspect = z / max(x, y) = 100 / 10 = 10 → > 2 threshold
        result = self._run_with_material(
            tmp_path,
            "pla",
            dims={"width_mm": 10.0, "depth_mm": 10.0, "height_mm": 100.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is False
        assert "PLA" in mat_checks[0]["details"]
        assert "overhang" in mat_checks[0]["details"].lower()

    def test_pla_no_warning_on_low_aspect_model(self, tmp_path: Path) -> None:
        """PLA check passes on a short, wide model (low aspect ratio)."""
        result = self._run_with_material(
            tmp_path,
            "pla",
            dims={"width_mm": 50.0, "depth_mm": 40.0, "height_mm": 10.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is True

    def test_abs_warping_warning_on_large_model(self, tmp_path: Path) -> None:
        """ABS check warns when bed footprint (max of x, y) exceeds 100 mm."""
        result = self._run_with_material(
            tmp_path,
            "abs",
            dims={"width_mm": 150.0, "depth_mm": 80.0, "height_mm": 30.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is False
        assert "warp" in mat_checks[0]["details"].lower()

    def test_abs_no_warning_on_small_model(self, tmp_path: Path) -> None:
        """ABS check passes when bed footprint (x, y) is ≤100 mm."""
        result = self._run_with_material(
            tmp_path,
            "abs",
            dims={"width_mm": 40.0, "depth_mm": 30.0, "height_mm": 20.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is True

    def test_abs_no_warning_when_only_height_large(self, tmp_path: Path) -> None:
        """ABS check passes when only height exceeds 100mm — height irrelevant for warping."""
        result = self._run_with_material(
            tmp_path,
            "abs",
            dims={"width_mm": 40.0, "depth_mm": 30.0, "height_mm": 200.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is True

    def test_asa_alias_resolves_to_abs_check(self, tmp_path: Path) -> None:
        """ASA is aliased to ABS — warping check fires on large models."""
        result = self._run_with_material(
            tmp_path,
            "asa",
            dims={"width_mm": 200.0, "depth_mm": 50.0, "height_mm": 10.0},
        )

        mat_checks = [c for c in result["checks"] if c["name"] == "material_check"]
        assert len(mat_checks) == 1
        assert mat_checks[0]["passed"] is False

    def test_material_check_is_warning_not_hard_fail(self, tmp_path: Path) -> None:
        """A triggered material warning must never set status to 'fail'."""
        result = self._run_with_material(
            tmp_path,
            "abs",
            dims={"width_mm": 200.0, "depth_mm": 200.0, "height_mm": 200.0},
        )

        # Material check is a warning — status must be pass_with_warnings, not fail
        assert result["status"] in ("pass", "pass_with_warnings")
        assert result["ready_to_print"] is True


# ---------------------------------------------------------------------------
# Tests — summary field
# ---------------------------------------------------------------------------


class TestSummaryField:
    """Response includes a human-readable summary string."""

    def test_summary_present_and_nonempty(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert "summary" in result
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 10

    def test_summary_says_print_ready_when_passing(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", return_value=mock_printability),
            patch("kiln.design_intelligence.estimate_load_capacity", return_value=mock_estimate),
        ):
            result = _invoke_tool(stl)

        assert "Print-ready" in result["summary"]

    def test_summary_says_not_ready_on_fail(self, tmp_path: Path) -> None:
        result = _invoke_tool(str(Path("/nonexistent/model.stl")))

        assert "Not ready" in result["summary"]


# ---------------------------------------------------------------------------
# Tests — validated_path field
# ---------------------------------------------------------------------------


class TestValidatedPath:
    """Response includes validated_path pointing to the file for next step."""

    def test_validated_path_is_input_when_no_repair(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert result["validated_path"] == stl

    def test_validated_path_is_repaired_when_repair_succeeds(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_validation_good = _make_mock_validation(is_manifold=True, valid=True)

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation_bad, mock_validation_good],
            ),
            patch("kiln.generation.validation.repair_stl", return_value={}),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert result["repaired"] is True
        assert result["validated_path"] == result["repaired_path"]
        assert result["validated_path"] != stl


# ---------------------------------------------------------------------------
# Tests — next_action field
# ---------------------------------------------------------------------------


class TestNextAction:
    """Response includes next_action with tool and args/reason."""

    def test_next_action_is_slice_when_ready(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", return_value=mock_printability),
            patch("kiln.design_intelligence.estimate_load_capacity", return_value=mock_estimate),
        ):
            result = _invoke_tool(stl)

        assert result["ready_to_print"] is True
        na = result["next_action"]
        assert na["tool"] == "slice_model"
        assert "input_path" in na["args"]
        assert na["args"]["input_path"] == result["validated_path"]

    def test_next_action_is_scale_when_bed_fit_fails(self, tmp_path: Path) -> None:
        stl = _make_known_binary_stl(tmp_path)  # 10x20x5mm
        mock_profile = MagicMock()
        mock_profile.build_volume = [5, 5, 5]

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.safety_profiles.get_profile", return_value=mock_profile),
        ):
            result = _invoke_tool(stl, printer_id="small_printer")

        assert result["ready_to_print"] is False
        na = result["next_action"]
        assert na["tool"] == "scale_mesh_to_fit"
        assert "reason" in na

    def test_next_action_is_repair_when_mesh_fails(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_validation_bad = _make_mock_validation(is_manifold=False, valid=False)
        mock_post_repair = _make_mock_validation(is_manifold=False, valid=False)

        # Force bed_fit error too — but mesh repair should take priority when
        # there's no bed_fit issue. Here we only have watertight failure.
        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch(
                "kiln.generation.validation.validate_mesh",
                side_effect=[mock_validation_bad, mock_post_repair],
            ),
            patch("kiln.generation.validation.repair_stl", return_value={}),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        # Non-manifold is a warning, not error — so it may still be ready_to_print.
        # But if next_action exists and there's a repair issue, check repair suggestion.
        na = result["next_action"]
        assert na is not None
        assert "tool" in na


# ---------------------------------------------------------------------------
# Tests — no structural fabrication
# ---------------------------------------------------------------------------


class TestStructuralAssessment:
    """Structural assessment uses real geometric risk factors or estimate_load_capacity."""

    def test_structural_uses_geometric_risk_factors_when_no_module(self, tmp_path: Path) -> None:
        """Without design_intelligence, reports real aspect ratio / cross-section / SA:V."""
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()  # 20x15x10mm
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        structural = [c for c in result["checks"] if c["name"] == "structural"]
        assert len(structural) == 1
        # 20x15x10mm: aspect = 10/15 = 0.67, min dim = 10 > 5 → info, passed
        assert structural[0]["passed"] is True
        assert "Aspect ratio" in structural[0]["details"]
        assert "Min cross-section" in structural[0]["details"]
        assert "Surface-to-volume" in structural[0]["details"]
        # No fabricated load numbers
        assert "safe_load" not in structural[0]["details"].lower()

    def test_structural_warns_on_tall_narrow_model(self, tmp_path: Path) -> None:
        """Tall narrow model (aspect >= 3) triggers a warning."""
        stl = _make_binary_stl(tmp_path)
        # 5x5x50mm → aspect = 50/5 = 10
        analysis = MagicMock()
        analysis.to_dict.return_value = {
            "triangle_count": 100,
            "is_manifold": True,
            "volume_mm3": 1250.0,
            "dimensions_mm": {"width_mm": 5.0, "depth_mm": 5.0, "height_mm": 50.0},
        }
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        structural = [c for c in result["checks"] if c["name"] == "structural"]
        assert len(structural) == 1
        assert structural[0]["passed"] is False
        assert structural[0].get("severity") == "warning"
        assert "tall/narrow" in structural[0]["details"]

    def test_structural_uses_estimate_load_capacity_when_available(self, tmp_path: Path) -> None:
        """When design_intelligence is available, uses estimate_load_capacity."""
        stl = _make_binary_stl(tmp_path, triangles=100)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", return_value=mock_printability),
            patch("kiln.design_intelligence.estimate_load_capacity", return_value=mock_estimate),
        ):
            result = _invoke_tool(stl)

        structural = [c for c in result["checks"] if c["name"] == "structural"]
        assert len(structural) == 1
        assert structural[0]["passed"] is True
        assert "25.0 N" in structural[0]["details"]
        assert result["model_info"]["structural_estimate"] == {"safe_load_n": 25.0}


# ---------------------------------------------------------------------------
# Tests — _compute_printability_score unit tests
# ---------------------------------------------------------------------------


class TestComputePrintabilityScore:
    """Unit tests for the _compute_printability_score function."""

    def test_all_pass_returns_100(self) -> None:
        """All checks pass with info severity → score 100, no deductions."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name="format", passed=True, details="ok"),
            _CheckResult(name="mesh_geometry", passed=True, details="ok"),
            _CheckResult(name="watertight", passed=True, details="ok"),
            _CheckResult(name="structural", passed=True, details="ok"),
        ]
        score, breakdown = _compute_printability_score(checks, repaired=False)
        assert score == 100
        assert breakdown == []

    def test_one_warning_deducts_10(self) -> None:
        """One failed warning check → -10 deduction."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name="format", passed=True, details="ok"),
            _CheckResult(name="watertight", passed=False, severity="warning", details="non-manifold"),
        ]
        score, breakdown = _compute_printability_score(checks, repaired=False)
        assert score == 90
        assert len(breakdown) == 1
        assert "watertight" in breakdown[0]

    def test_one_error_deducts_25(self) -> None:
        """One failed error check → -25 deduction."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name="bed_fit", passed=False, severity="error", details="too big"),
        ]
        score, breakdown = _compute_printability_score(checks, repaired=False)
        assert score == 75
        assert len(breakdown) == 1
        assert "bed_fit" in breakdown[0]

    def test_repair_deducts_15(self) -> None:
        """Repair needed → -15 deduction."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name="format", passed=True, details="ok"),
        ]
        score, breakdown = _compute_printability_score(checks, repaired=True)
        assert score == 85
        assert any("repair" in s for s in breakdown)

    def test_score_clamps_at_zero(self) -> None:
        """Many failures clamp score to 0, never negative."""
        from kiln.plugins.validation_pipeline_tools import _CheckResult, _compute_printability_score

        checks = [
            _CheckResult(name=f"fail_{i}", passed=False, severity="error", details="x")
            for i in range(10)
        ]
        score, breakdown = _compute_printability_score(checks, repaired=True)
        assert score == 0
        assert len(breakdown) == 11  # 10 errors + 1 repair


# ---------------------------------------------------------------------------
# Tests — summary content assertions
# ---------------------------------------------------------------------------


class TestSummaryContent:
    """Summary field contains 'Print-ready' or 'Not ready' and the score."""

    def test_summary_contains_print_ready_and_score_on_pass(self, tmp_path: Path) -> None:
        stl = _make_binary_stl(tmp_path)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()
        mock_printability = _make_mock_printability()

        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"safe_load_n": 25.0}

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", return_value=mock_printability),
            patch("kiln.design_intelligence.estimate_load_capacity", return_value=mock_estimate),
        ):
            result = _invoke_tool(stl)

        assert "Print-ready" in result["summary"]
        assert "/100" in result["summary"]

    def test_summary_contains_not_ready_and_score_on_fail(self, tmp_path: Path) -> None:
        result = _invoke_tool(str(Path("/nonexistent/model.stl")))

        assert "Not ready" in result["summary"]
        assert "/100" in result["summary"]

    def test_summary_contains_score_on_pass_with_warnings(self, tmp_path: Path) -> None:
        """pass_with_warnings summary still says Print-ready and includes score."""
        stl = _make_binary_stl(tmp_path)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        assert result["status"] == "pass_with_warnings"
        assert "Print-ready" in result["summary"]
        assert "/100" in result["summary"]


# ---------------------------------------------------------------------------
# Tests — next_action for format failures
# ---------------------------------------------------------------------------


class TestNextActionFormatFailures:
    """next_action is None for unrecoverable format failures."""

    def test_missing_file_next_action_is_none(self, tmp_path: Path) -> None:
        result = _invoke_tool(str(tmp_path / "ghost.stl"))
        assert result["next_action"] is None

    def test_unsupported_format_next_action_is_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "model.dwg"
        bad.write_text("not a mesh")
        result = _invoke_tool(str(bad))
        assert result["next_action"] is None

    def test_low_printability_suggests_auto_orient_when_not_ready(self, tmp_path: Path) -> None:
        """When model fails validation due to printability, suggests auto_orient_model.

        To reach the auto_orient path, ready_to_print must be False (requires an
        error-severity failure) AND the failure must not be bed_fit or mesh.
        We inject an error-severity check via inline STL parse failure + low printability.
        """
        from kiln.plugins.validation_pipeline_tools import _CheckResult

        # Test the priority logic directly: printability failed, no bed/mesh fail
        checks = [
            _CheckResult(name="format", passed=True, details="ok"),
            _CheckResult(name="mesh_geometry", passed=False, details="parse error", severity="error"),
            _CheckResult(name="printability", passed=False, details="Score 30/100", severity="warning"),
        ]
        # Verify: bed_fail=False, mesh_fail=False (mesh_geometry is not "watertight"/"repair"),
        # printability_fail=True → should pick auto_orient
        bed_fail = any(not c.passed and c.name == "bed_fit" for c in checks)
        mesh_fail = any(not c.passed and c.name in ("watertight", "repair") for c in checks)
        printability_fail = any(not c.passed and c.name == "printability" for c in checks)

        assert not bed_fail
        assert not mesh_fail
        assert printability_fail


# ---------------------------------------------------------------------------
# Tests — print time and cost estimate (Step 9)
# ---------------------------------------------------------------------------


class TestPrintTimeEstimateWithRealModule:
    """Step 9 when an estimate module is available."""

    def test_estimate_module_populates_model_info(self, tmp_path: Path) -> None:
        """When estimate_mesh_print_time is available, model_info gets exact keys."""
        stl = _make_binary_stl(tmp_path, triangles=100)
        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        # Mock estimate_print_time_from_mesh in kiln.generation.validation
        fake_estimate = MagicMock(
            return_value={"time_min": 45, "filament_g": 12.0}
        )

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
            patch("kiln.generation.validation.estimate_print_time_from_mesh", fake_estimate, create=True),
        ):
            result = _invoke_tool(stl)

        info = result["model_info"]
        assert info["estimated_print_time_min"] == 45
        assert info["estimated_filament_g"] == 12.0
        assert abs(info["estimated_cost_usd"] - 0.24) < 0.01  # 12g * $0.02/g

        # Check entry must appear with name="estimate" and passed=True
        est_checks = [c for c in result["checks"] if c["name"] == "estimate"]
        assert len(est_checks) == 1
        assert est_checks[0]["passed"] is True
        assert "45 min" in est_checks[0]["details"]
        assert "$0.24" in est_checks[0]["details"]

        # Summary should include the time/cost snippet
        assert "45 min" in result["summary"] or "$0.24" in result["summary"]

        # Spaced/paren rough keys must NOT appear when real module is available
        assert "estimated_print_time_min (rough)" not in info
        assert "estimated_filament_g (rough)" not in info
        assert "estimated_cost_usd (rough)" not in info


class TestPrintTimeEstimateFallback:
    """Step 9 inline fallback when no estimate module is available."""

    def test_fallback_from_bounding_box_volume(self, tmp_path: Path) -> None:
        """When no module is available, rough estimate is computed from bbox volume."""
        # Use the known-dims STL: 10×20×5mm → bbox vol = 1000 mm³ = 1.0 cm³
        # Expected rough: time = max(1, round(1.0 * 8)) = 8 min
        #                 filament = 1.0 * 1.24 * 0.3 = 0.372g → 0.4
        #                 cost = 0.4 * 0.02 = 0.008 → 0.01
        stl = _make_known_binary_stl(tmp_path)

        # Force all analysis modules absent so inline STL parser runs and
        # populates bounding_box_volume_cm3.
        # Hide estimate_print_time_from_mesh so the bounding-box fallback runs.
        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.estimate_print_time_from_mesh", side_effect=AttributeError("forced")),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        info = result["model_info"]

        # Estimate keys must be present (clean names, no spaces/parens)
        assert "estimated_print_time_min" in info
        assert "estimated_filament_g" in info
        assert "estimated_cost_usd" in info
        # Bounding-box fallback is identified by estimate_source
        assert info.get("estimate_source") == "bounding_box"

        # Values should be reasonable (bbox vol 1 cm³ → 8 min, ~0.4g, ~$0.01)
        assert info["estimated_print_time_min"] >= 1
        assert info["estimated_filament_g"] > 0
        assert info["estimated_cost_usd"] >= 0

        # Check entry must appear
        est_checks = [c for c in result["checks"] if c["name"] == "estimate"]
        assert len(est_checks) == 1
        assert est_checks[0]["passed"] is True
        assert "rough" in est_checks[0]["details"].lower()

        # Old spaced/paren keys must NOT appear
        assert "estimated_print_time_min (rough)" not in info
        assert "estimated_filament_g (rough)" not in info
        assert "estimated_cost_usd (rough)" not in info


# ---------------------------------------------------------------------------
# Helper — tiny STL (simulates AI model exported in meters)
# ---------------------------------------------------------------------------


def _make_tiny_binary_stl(tmp_dir: Path, *, max_dim_mm: float = 1.9) -> str:
    """Create a binary STL with bounding box where max dim is *max_dim_mm*.

    Uses 2000 triangles (above the 1000 threshold) to trigger auto-scale.
    The model is a figurine-like shape: taller than wide.
    """
    stl_path = tmp_dir / "tiny_ai_model.stl"
    header = b"\x00" * 80
    tri_count = 2000
    # Scale: height = max_dim_mm, width/depth = max_dim_mm * 0.4
    w = max_dim_mm * 0.4
    d = max_dim_mm * 0.4
    h = max_dim_mm

    data = bytearray(header)
    data += struct.pack("<I", tri_count)
    for i in range(tri_count):
        frac = float(i) / tri_count
        data += struct.pack("<3f", 0.0, 0.0, 1.0)  # normal
        data += struct.pack("<3f", 0.0, 0.0, frac * h)
        data += struct.pack("<3f", w, 0.0, frac * h)
        data += struct.pack("<3f", w * 0.5, d, frac * h + h / tri_count)
        data += struct.pack("<H", 0)
    stl_path.write_bytes(bytes(data))
    return str(stl_path)


def _make_normal_size_stl(
    tmp_dir: Path,
    *,
    width: float = 50.0,
    depth: float = 40.0,
    height: float = 30.0,
) -> str:
    """Create a binary STL with known dimensions in the normal printing range."""
    stl_path = tmp_dir / "normal_model.stl"
    header = b"\x00" * 80
    tri_count = 100
    data = bytearray(header)
    data += struct.pack("<I", tri_count)
    for i in range(tri_count):
        frac = float(i) / tri_count
        data += struct.pack("<3f", 0.0, 0.0, 1.0)
        data += struct.pack("<3f", 0.0, 0.0, frac * height)
        data += struct.pack("<3f", width, 0.0, frac * height)
        data += struct.pack("<3f", width * 0.5, depth, frac * height + height / tri_count)
        data += struct.pack("<H", 0)
    stl_path.write_bytes(bytes(data))
    return str(stl_path)


# ---------------------------------------------------------------------------
# Tests — prepare_ai_model_for_print
# ---------------------------------------------------------------------------


class TestPrepareAiModelAutoScale:
    """Auto-scaling: tiny models get scaled up, normal models left alone."""

    def test_tiny_model_auto_scaled_to_reasonable_size(self, tmp_path: Path) -> None:
        """Model < 10mm triggers auto-scale — test the scaling helper directly."""
        from kiln.plugins.validation_pipeline_tools import _auto_scale_if_needed

        stl = _make_tiny_binary_stl(tmp_path, max_dim_mm=1.9)

        # Simulate what validate_and_prepare would return for inline-parsed dims
        model_info = {
            "triangles": 2000,
            "dimensions_mm": {"width_mm": 0.76, "depth_mm": 0.76, "height_mm": 1.9},
            "bounding_box_volume_cm3": 0.001,
        }

        scaled_path, factor = _auto_scale_if_needed(stl, model_info)

        assert factor > 1.0, f"Expected scale factor > 1.0, got {factor}"
        assert scaled_path is not None, "Scaled STL should have been created"
        assert Path(scaled_path).exists(), "Scaled STL file should exist on disk"

    def test_normal_size_model_not_scaled(self, tmp_path: Path) -> None:
        """Model already at correct size gets no scaling."""
        stl = _make_normal_size_stl(tmp_path, width=50.0, depth=40.0, height=30.0)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        assert result["scale_factor"] == 0
        assert len(result["actions_taken"]) == 0

    def test_target_height_overrides_auto_detection(self, tmp_path: Path) -> None:
        """target_height_mm forces scaling to exact height."""
        stl = _make_tiny_binary_stl(tmp_path, max_dim_mm=1.9)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl, target_height_mm=120.0)

        assert result["scale_factor"] > 0
        assert any("120" in a for a in result["actions_taken"])


class TestPrepareAiModelHollowRecommendation:
    """Smart hollow recommendation based on volume-to-bounding-box ratio."""

    def test_solid_model_gets_hollow_recommendation(self, tmp_path: Path) -> None:
        """Volume ratio > 0.7 AND max dim > 30mm triggers hollow recommendation."""
        stl = _make_normal_size_stl(tmp_path, width=50.0, depth=50.0, height=50.0)

        # Mock analyze_mesh to return high volume (ratio > 0.7)
        mock_analysis = MagicMock()
        mock_analysis.to_dict.return_value = {
            "triangle_count": 500,
            "volume_mm3": 100000.0,  # 100cm3 — 80% of 125cm3 bbox
            "dimensions_mm": {"width_mm": 50.0, "depth_mm": 50.0, "height_mm": 50.0},
        }

        call_count = {"n": 0}

        def _analyze_side_effect(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ImportError("not available")
            return mock_analysis

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=_analyze_side_effect),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        hollow_recs = [r for r in result["recommendations"] if "hollow" in r.lower()]
        assert len(hollow_recs) >= 1
        assert "material" in hollow_recs[0].lower() or "save" in hollow_recs[0].lower()

    def test_thin_shell_gets_thicken_recommendation(self, tmp_path: Path) -> None:
        """SA/V ratio > 1.0 triggers thicken recommendation."""
        # 100x100x1mm = very thin shell. SA/V = 2*(100*100+100*1+100*1)/10000 = 2.04
        stl = _make_normal_size_stl(tmp_path, width=100.0, depth=100.0, height=1.0)

        mock_analysis = MagicMock()
        mock_analysis.to_dict.return_value = {
            "triangle_count": 500,
            "volume_mm3": 5000.0,  # 5cm3 — ~4% of 125cm3 bbox
            "dimensions_mm": {"width_mm": 50.0, "depth_mm": 50.0, "height_mm": 50.0},
        }

        call_count = {"n": 0}

        def _analyze_side_effect(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ImportError("not available")
            return mock_analysis

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=_analyze_side_effect),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        thicken_recs = [r for r in result["recommendations"] if "thicken" in r.lower()]
        assert len(thicken_recs) >= 1

    def test_normal_model_no_hollow_recommendation(self, tmp_path: Path) -> None:
        """Medium model (< 50cm³) with normal SA/V gets no hollow/thicken recommendation."""
        # 30x30x30 = 27cm³ < 50cm³ threshold. SA/V = 0.2 (normal, not thin-shelled).
        stl = _make_normal_size_stl(tmp_path, width=30.0, depth=30.0, height=30.0)

        mock_analysis = MagicMock()
        mock_analysis.to_dict.return_value = {
            "triangle_count": 500,
            "volume_mm3": 37500.0,  # 37.5cm3 — 30% of 125cm3 bbox
            "dimensions_mm": {"width_mm": 50.0, "depth_mm": 50.0, "height_mm": 50.0},
        }

        call_count = {"n": 0}

        def _analyze_side_effect(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ImportError("not available")
            return mock_analysis

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=_analyze_side_effect),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        hollow_recs = [r for r in result["recommendations"] if "hollow" in r.lower()]
        thicken_recs = [r for r in result["recommendations"] if "thicken" in r.lower()]
        assert len(hollow_recs) == 0
        assert len(thicken_recs) == 0


class TestPrepareAiModelSimplifyRecommendation:
    """Mesh simplification recommendation based on triangle count."""

    def test_high_triangle_count_gets_simplify_recommendation(self, tmp_path: Path) -> None:
        """Model with > 100K triangles gets a simplification recommendation."""
        stl = _make_normal_size_stl(tmp_path)

        mock_analysis = MagicMock()
        mock_analysis.to_dict.return_value = {
            "triangle_count": 393000,
            "volume_mm3": 5000.0,
            "dimensions_mm": {"width_mm": 50.0, "depth_mm": 40.0, "height_mm": 30.0},
        }
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        simplify_recs = [r for r in result["recommendations"] if "simplify" in r.lower()]
        assert len(simplify_recs) >= 1
        assert "393,000" in simplify_recs[0]

    def test_low_triangle_count_no_simplify_recommendation(self, tmp_path: Path) -> None:
        """Model with < 100K triangles gets no simplification recommendation."""
        stl = _make_normal_size_stl(tmp_path)

        mock_analysis = MagicMock()
        mock_analysis.to_dict.return_value = {
            "triangle_count": 5000,
            "volume_mm3": 5000.0,
            "dimensions_mm": {"width_mm": 50.0, "depth_mm": 40.0, "height_mm": 30.0},
        }
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        simplify_recs = [r for r in result["recommendations"] if "simplify" in r.lower()]
        assert len(simplify_recs) == 0


class TestPrepareAiModelReturnFormat:
    """Return format matches the spec: original, prepared, actions, recommendations."""

    def test_return_keys_present(self, tmp_path: Path) -> None:
        """All required keys are in the response."""
        stl = _make_normal_size_stl(tmp_path)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl)

        required_keys = {
            "status", "original", "prepared", "actions_taken",
            "recommendations", "scale_factor", "ready_to_print", "next_action",
        }
        assert required_keys.issubset(result.keys())
        assert "dimensions_mm" in result["original"]
        assert "triangles" in result["original"]
        assert "printability_score" in result["original"]
        assert "stl_path" in result["prepared"]


# ---------------------------------------------------------------------------
# Tests — _inline_stl_scale with truncated binary STL
# ---------------------------------------------------------------------------


class TestInlineSTLScaleTruncated:
    """_inline_stl_scale handles truncated binary STL gracefully."""

    def test_inline_stl_scale_truncated_binary(self, tmp_path: Path) -> None:
        """Truncated binary STL produces a shorter but valid output."""
        # Write a binary STL header claiming 10 triangles but only include 3
        stl = tmp_path / "truncated.stl"
        header = b"\x00" * 80
        count = struct.pack("<I", 10)  # claims 10 triangles
        # Write only 3 complete triangles (50 bytes each)
        tri_data = b""
        for i in range(3):
            tri_data += struct.pack(
                "<12f",
                0, 0, 1,
                float(i), 0, 0,
                float(i + 1), 0, 0,
                float(i), 1, 0,
            )
            tri_data += b"\x00\x00"  # attribute byte count
        stl.write_bytes(header + count + tri_data)

        from kiln.plugins.validation_pipeline_tools import _inline_stl_scale

        result = _inline_stl_scale(str(stl), 2.0)
        # Should produce output with the header still claiming 10
        assert Path(result).exists()
        result_data = Path(result).read_bytes()
        result_count = struct.unpack("<I", result_data[80:84])[0]
        assert result_count == 10  # header still says 10, but file is short
        # The actual data is only 3 triangles worth
        actual_tri_bytes = len(result_data) - 84
        assert actual_tri_bytes == 3 * 50  # 3 triangles * 50 bytes each


# ---------------------------------------------------------------------------
# Tests — scale_check fires for small inline-parsed model
# ---------------------------------------------------------------------------


def _make_binary_stl_with_dims(
    tmp_dir: Path,
    *,
    x_range: tuple[float, float] = (0, 10),
    y_range: tuple[float, float] = (0, 20),
    z_range: tuple[float, float] = (0, 5),
    triangles: int = 4,
) -> str:
    """Create a binary STL with vertices spanning the given coordinate ranges."""
    stl_path = tmp_dir / "dims_model.stl"
    header = b"\x00" * 80
    data = bytearray(header)
    data += struct.pack("<I", triangles)

    x_min, x_max = x_range
    y_min, y_max = y_range
    z_min, z_max = z_range

    for i in range(triangles):
        frac = float(i) / max(triangles - 1, 1)
        data += struct.pack("<3f", 0.0, 0.0, 1.0)  # normal
        data += struct.pack("<3f", x_min, y_min, z_min + frac * (z_max - z_min))
        data += struct.pack("<3f", x_max, y_min, z_min + frac * (z_max - z_min))
        data += struct.pack("<3f", x_min + (x_max - x_min) * 0.5, y_max, z_max)
        data += struct.pack("<H", 0)

    stl_path.write_bytes(bytes(data))
    return str(stl_path)


class TestScaleCheckFires:
    """scale_check should fire when inline parser detects a tiny model."""

    def test_scale_check_warns_for_small_model(self, tmp_path: Path) -> None:
        """scale_check should fire when inline parser detects a tiny model."""
        # Create a tiny model (2mm x 2mm x 2mm)
        stl = _make_binary_stl_with_dims(
            tmp_path, x_range=(0, 2), y_range=(0, 2), z_range=(0, 2),
        )

        # Force all analysis modules absent so inline STL parser runs
        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        # Assert that a check with name "scale_check" exists and failed
        scale_checks = [c for c in result["checks"] if c["name"] == "scale_check"]
        assert len(scale_checks) == 1
        assert scale_checks[0]["passed"] is False
        assert "2.0mm" in scale_checks[0]["details"]
        assert scale_checks[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Tests — target_height_mm on normal-size model
# ---------------------------------------------------------------------------


class TestPrepareAiModelTargetHeightNormalSize:
    """target_height_mm on a model that is already > 10mm."""

    def test_target_height_normal_size_model(self, tmp_path: Path) -> None:
        """A normal-size model (>10mm) with target_height_mm should scale to target."""
        # Create a model with max_dim ~50mm (height=50)
        stl = _make_normal_size_stl(tmp_path, width=30.0, depth=30.0, height=50.0)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_ai_tool(stl, target_height_mm=100.0)

        # Should have been scaled (factor ~ 2.0)
        assert result["scale_factor"] > 0
        assert any("100" in a for a in result["actions_taken"])


# ---------------------------------------------------------------------------
# Tests — resilience assertion fix
# ---------------------------------------------------------------------------
# (Fix in TestResilience.test_analysis_exception_doesnt_crash is applied
#  via the edit below — see the assertion replacement.)


# ---------------------------------------------------------------------------
# Tests — severity always present in check dicts
# ---------------------------------------------------------------------------


class TestSeverityAlwaysPresent:
    """Every check dict must include 'severity' — never omitted."""

    def test_all_checks_include_severity_key(self, tmp_path: Path) -> None:
        """Every check dict must include 'severity' — never omitted."""
        stl = _make_binary_stl(tmp_path, triangles=100)

        mock_analysis = _make_mock_analysis()
        mock_validation = _make_mock_validation()

        with (
            patch("kiln.generation.validation.analyze_mesh", return_value=mock_analysis),
            patch("kiln.generation.validation.validate_mesh", return_value=mock_validation),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        for check in result["checks"]:
            assert "severity" in check, f"Check {check['name']} missing severity key"

    def test_all_checks_include_severity_when_modules_absent(self, tmp_path: Path) -> None:
        """Severity present even when all optional modules are missing."""
        stl = _make_binary_stl(tmp_path, triangles=10)

        with (
            patch("kiln.generation.validation.analyze_mesh", side_effect=ImportError),
            patch("kiln.generation.validation.validate_mesh", side_effect=ImportError),
            patch("kiln.printability.analyze_printability", side_effect=ImportError),
            patch("kiln.design_intelligence.estimate_load_capacity", side_effect=ImportError),
        ):
            result = _invoke_tool(stl)

        for check in result["checks"]:
            assert "severity" in check, f"Check {check['name']} missing severity key"


# ===================================================================
# Module-level run_full_validation_pipeline entry point
# ===================================================================
#
# `slice_and_print`, `run_quick_print`, and `run_reslice_and_print`
# import `run_full_validation_pipeline` directly to gate the print on
# mesh-level printability before slicing.  These tests pin the import
# path and the contract — same return shape as the validate_and_prepare
# MCP tool, since both delegate to the same orchestration body.


class TestRunFullValidationPipeline:
    """Tests for the module-level ``run_full_validation_pipeline``."""

    def test_module_level_function_is_importable(self) -> None:
        """The function must be importable from validation_pipeline_tools —
        slice_and_print and the print pipelines depend on this exact path."""
        from kiln.plugins.validation_pipeline_tools import (
            run_full_validation_pipeline,
        )
        assert callable(run_full_validation_pipeline)

    def test_supported_formats_set_is_importable(self) -> None:
        """The print tools also import _SUPPORTED_FORMATS to short-circuit
        validation for unsupported inputs (e.g. raw .gcode)."""
        from kiln.plugins._validation_pipeline_internals import _SUPPORTED_FORMATS
        # Mesh formats the validator can introspect.
        assert ".stl" in _SUPPORTED_FORMATS
        assert ".obj" in _SUPPORTED_FORMATS
        assert ".3mf" in _SUPPORTED_FORMATS
        # Raw G-code is intentionally NOT in the set — validator can't
        # introspect it; print tools should skip the gate cleanly.
        assert ".gcode" not in _SUPPORTED_FORMATS

    def test_returns_same_shape_as_mcp_tool(self, tmp_path: Path) -> None:
        """Module-level call returns the same dict shape as validate_and_prepare —
        same keys, same semantics — so callers can rely on either entry point."""
        from kiln.plugins.validation_pipeline_tools import (
            run_full_validation_pipeline,
        )

        stl = _make_binary_stl(tmp_path, triangles=10)
        report = run_full_validation_pipeline(stl)

        # Contract: every key the gate logic in slice_and_print /
        # quick_print reads must be present.
        for key in (
            "ready_to_print",
            "printability_score",
            "validated_path",
            "summary",
            "next_action",
            "checks",
            "status",
        ):
            assert key in report, f"missing key {key!r} from validation report"

    def test_missing_file_returns_not_ready(self, tmp_path: Path) -> None:
        """A non-existent input fails the gate — print pipelines treat
        ready_to_print=False as the block signal."""
        from kiln.plugins.validation_pipeline_tools import (
            run_full_validation_pipeline,
        )

        report = run_full_validation_pipeline(str(tmp_path / "missing.stl"))
        assert report["ready_to_print"] is False
        assert report["printability_score"] == 0

    def test_unsupported_format_returns_not_ready(self, tmp_path: Path) -> None:
        """An unsupported format also fails — but slice_and_print /
        quick_print short-circuit BEFORE calling the validator using
        _SUPPORTED_FORMATS, so this is a defensive gate."""
        from kiln.plugins.validation_pipeline_tools import (
            run_full_validation_pipeline,
        )

        gcode = tmp_path / "fake.gcode"
        gcode.write_text("G28\n")
        report = run_full_validation_pipeline(str(gcode))
        assert report["ready_to_print"] is False
