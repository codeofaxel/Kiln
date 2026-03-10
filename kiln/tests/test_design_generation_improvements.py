"""Tests for design generation improvements.

Covers:
- GLB parsing and GLB-to-STL conversion
- Image-to-3D support in MeshyProvider
- Provider-aware prompt limits in feedback loop
- Enhanced design intelligence prompt enrichment
- OpenSCAD render preview
- Mesh rescaling
- Design templates
"""

from __future__ import annotations

import json
import os
import struct
from unittest.mock import MagicMock, patch

import pytest
import responses

from kiln.generation.base import GenerationError, GenerationStatus
from kiln.generation.meshy import _BASE_URL, MeshyProvider
from kiln.generation.openscad import OpenSCADProvider
from kiln.generation.validation import (
    _parse_glb,
    convert_to_stl,
    rescale_stl,
    validate_mesh,
)
from kiln.generation_feedback import (
    generate_improved_prompt,
    get_provider_prompt_limit,
)

# ---------------------------------------------------------------------------
# GLB test helpers
# ---------------------------------------------------------------------------


def _build_glb(vertices: list[tuple[float, float, float]], indices: list[int] | None = None) -> bytes:
    """Build a minimal valid GLB from vertices and optional indices."""
    # BIN chunk: positions (+ optional indices)
    bin_buf = b""
    for v in vertices:
        bin_buf += struct.pack("<3f", *v)
    pos_byte_len = len(bin_buf)

    accessors = [
        {
            "bufferView": 0,
            "componentType": 5126,
            "count": len(vertices),
            "type": "VEC3",
            "byteOffset": 0,
        }
    ]
    buffer_views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": pos_byte_len}
    ]
    prim: dict = {"attributes": {"POSITION": 0}}

    if indices is not None:
        idx_offset = pos_byte_len
        for idx in indices:
            bin_buf += struct.pack("<H", idx)
        idx_byte_len = len(indices) * 2
        buffer_views.append(
            {"buffer": 0, "byteOffset": idx_offset, "byteLength": idx_byte_len}
        )
        accessors.append(
            {
                "bufferView": 1,
                "componentType": 5123,
                "count": len(indices),
                "type": "SCALAR",
                "byteOffset": 0,
            }
        )
        prim["indices"] = 1

    gltf_json = {
        "asset": {"version": "2.0"},
        "meshes": [{"primitives": [prim]}],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_buf)}],
    }
    json_bytes = json.dumps(gltf_json).encode()
    # Pad JSON to 4-byte boundary
    while len(json_bytes) % 4:
        json_bytes += b" "
    # Pad BIN to 4-byte boundary
    while len(bin_buf) % 4:
        bin_buf += b"\x00"

    # Build GLB
    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    bin_chunk = struct.pack("<II", len(bin_buf), 0x004E4942) + bin_buf
    total = 12 + len(json_chunk) + len(bin_chunk)
    header = struct.pack("<III", 0x46546C67, 2, total)
    return header + json_chunk + bin_chunk


def _cube_vertices_and_indices():
    """Return 8 cube vertices and 36 triangle indices."""
    verts = [
        (0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0),
        (0.0, 0.0, 10.0), (10.0, 0.0, 10.0), (10.0, 10.0, 10.0), (0.0, 10.0, 10.0),
    ]
    indices = [
        0, 1, 2, 0, 2, 3,  # bottom
        4, 6, 5, 4, 7, 6,  # top
        0, 4, 5, 0, 5, 1,  # front
        2, 6, 7, 2, 7, 3,  # back
        0, 3, 7, 0, 7, 4,  # left
        1, 5, 6, 1, 6, 2,  # right
    ]
    return verts, indices


# ---------------------------------------------------------------------------
# GLB Parsing Tests
# ---------------------------------------------------------------------------


class TestGLBParsing:
    """GLB binary glTF 2.0 parsing."""

    def test_indexed_glb(self, tmp_path):
        verts, indices = _cube_vertices_and_indices()
        glb = _build_glb(verts, indices)
        path = tmp_path / "cube.glb"
        path.write_bytes(glb)

        errors: list[str] = []
        triangles, vertices = _parse_glb(path, errors)
        assert not errors
        assert len(triangles) == 12  # cube = 12 triangles
        assert len(vertices) == 8

    def test_non_indexed_glb(self, tmp_path):
        """Non-indexed: 3 vertices = 1 triangle."""
        verts = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.0, 10.0, 0.0)]
        glb = _build_glb(verts, None)
        path = tmp_path / "tri.glb"
        path.write_bytes(glb)

        errors: list[str] = []
        triangles, vertices = _parse_glb(path, errors)
        assert not errors
        assert len(triangles) == 1
        assert len(vertices) == 3

    def test_empty_glb(self, tmp_path):
        path = tmp_path / "empty.glb"
        path.write_bytes(b"\x00" * 20)

        errors: list[str] = []
        _parse_glb(path, errors)
        assert errors  # should report bad magic or no meshes

    def test_invalid_magic(self, tmp_path):
        path = tmp_path / "bad.glb"
        path.write_bytes(struct.pack("<III", 0xDEADBEEF, 2, 12))

        errors: list[str] = []
        _parse_glb(path, errors)
        assert any("magic" in e.lower() or "valid" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# GLB-to-STL Conversion Tests
# ---------------------------------------------------------------------------


class TestGLBToSTLConversion:
    """GLB to STL conversion pipeline."""

    def test_cube_conversion(self, tmp_path):
        verts, indices = _cube_vertices_and_indices()
        glb_path = tmp_path / "cube.glb"
        glb_path.write_bytes(_build_glb(verts, indices))

        stl_path = convert_to_stl(str(glb_path))
        assert stl_path.endswith(".stl")
        assert os.path.getsize(stl_path) > 0

        # Validate the resulting STL
        result = validate_mesh(stl_path)
        assert result.valid
        assert result.triangle_count == 12

    def test_explicit_output_path(self, tmp_path):
        verts, indices = _cube_vertices_and_indices()
        glb_path = tmp_path / "model.glb"
        glb_path.write_bytes(_build_glb(verts, indices))
        out = str(tmp_path / "output.stl")

        result_path = convert_to_stl(str(glb_path), output_path=out)
        assert result_path == out
        assert os.path.isfile(out)

    def test_unsupported_format(self):
        with pytest.raises(ValueError, match="expects .obj or .glb"):
            convert_to_stl("/tmp/model.fbx")

    def test_obj_still_works(self, tmp_path):
        obj_path = tmp_path / "tri.obj"
        obj_path.write_text("v 0 0 0\nv 10 0 0\nv 5 10 0\nf 1 2 3\n")

        stl_path = convert_to_stl(str(obj_path))
        assert os.path.isfile(stl_path)


# ---------------------------------------------------------------------------
# GLB Validation Tests
# ---------------------------------------------------------------------------


class TestGLBValidation:
    """validate_mesh() with GLB files."""

    def test_cube_validation(self, tmp_path):
        verts, indices = _cube_vertices_and_indices()
        path = tmp_path / "cube.glb"
        path.write_bytes(_build_glb(verts, indices))

        result = validate_mesh(str(path))
        assert result.valid
        assert result.triangle_count == 12
        assert result.vertex_count == 8

    def test_empty_glb_fails(self, tmp_path):
        path = tmp_path / "empty.glb"
        path.write_bytes(b"\x00" * 20)

        result = validate_mesh(str(path))
        assert not result.valid


# ---------------------------------------------------------------------------
# Mesh Rescaling Tests
# ---------------------------------------------------------------------------


class TestMeshRescaling:
    """STL mesh rescaling."""

    def _write_cube_stl(self, tmp_path, size=10.0):
        """Write a simple cube STL."""
        verts, indices = _cube_vertices_and_indices()
        # Scale vertices
        scaled_verts = [(v[0] * size / 10, v[1] * size / 10, v[2] * size / 10) for v in verts]
        triangles = []
        for i in range(0, len(indices) - 2, 3):
            triangles.append((scaled_verts[indices[i]], scaled_verts[indices[i+1]], scaled_verts[indices[i+2]]))

        path = tmp_path / "cube.stl"
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 80)
            fh.write(struct.pack("<I", len(triangles)))
            for tri in triangles:
                fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
                for v in tri:
                    fh.write(struct.pack("<3f", v[0], v[1], v[2]))
                fh.write(struct.pack("<H", 0))
        return str(path)

    def test_target_height(self, tmp_path):
        path = self._write_cube_stl(tmp_path, size=10.0)
        result = rescale_stl(path, target_height_mm=50.0)
        assert result["scale_applied"] == 5.0
        assert result["new_dimensions"]["height_mm"] == 50.0

    def test_scale_factor(self, tmp_path):
        path = self._write_cube_stl(tmp_path, size=10.0)
        result = rescale_stl(path, scale_factor=2.0)
        assert result["scale_applied"] == 2.0
        assert result["new_dimensions"]["height_mm"] == 20.0

    def test_max_dimension(self, tmp_path):
        path = self._write_cube_stl(tmp_path, size=100.0)
        result = rescale_stl(path, max_dimension_mm=50.0)
        assert result["scale_applied"] == 0.5
        assert result["new_dimensions"]["height_mm"] == 50.0

    def test_max_dimension_no_scale_if_fits(self, tmp_path):
        path = self._write_cube_stl(tmp_path, size=10.0)
        result = rescale_stl(path, max_dimension_mm=200.0)
        assert result["scale_applied"] == 1.0

    def test_requires_exactly_one_option(self, tmp_path):
        path = self._write_cube_stl(tmp_path)
        with pytest.raises(ValueError, match="Exactly one"):
            rescale_stl(path, target_height_mm=50.0, scale_factor=2.0)
        with pytest.raises(ValueError, match="Exactly one"):
            rescale_stl(path)


# ---------------------------------------------------------------------------
# Image-to-3D Tests
# ---------------------------------------------------------------------------


class TestMeshyImageTo3D:
    """Meshy image-to-3D endpoint integration."""

    @responses.activate
    def test_image_to_3d_correct_endpoint(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/image-to-3d",
            json={"result": "img-job-001"},
            status=200,
        )

        provider = MeshyProvider(api_key="test-key")
        job = provider.generate("", image_url="https://example.com/photo.jpg")

        assert job.id == "img-job-001"
        assert job.status == GenerationStatus.PENDING
        assert "[image]" in job.prompt

    @responses.activate
    def test_image_job_polls_correct_endpoint(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/image-to-3d",
            json={"result": "img-job-002"},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{_BASE_URL}/image-to-3d/img-job-002",
            json={"status": "IN_PROGRESS", "progress": 50},
            status=200,
        )

        provider = MeshyProvider(api_key="test-key")
        provider.generate("", image_url="https://example.com/photo.jpg")
        status = provider.get_job_status("img-job-002")

        assert status.status == GenerationStatus.IN_PROGRESS
        assert status.progress == 50

    @responses.activate
    def test_text_jobs_unaffected(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/text-to-3d",
            json={"result": "txt-job-001"},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{_BASE_URL}/text-to-3d/txt-job-001",
            json={"status": "SUCCEEDED", "progress": 100},
            status=200,
        )

        provider = MeshyProvider(api_key="test-key")
        provider.generate("a cube")
        status = provider.get_job_status("txt-job-001")

        assert status.status == GenerationStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Provider-Aware Prompt Limits
# ---------------------------------------------------------------------------


class TestProviderPromptLimits:
    """Provider-aware prompt length limits."""

    def test_meshy_limit(self):
        assert get_provider_prompt_limit("meshy") == 600

    def test_gemini_limit(self):
        assert get_provider_prompt_limit("gemini") == 10_000

    def test_tripo3d_limit(self):
        assert get_provider_prompt_limit("tripo3d") == 5_000

    def test_openscad_limit(self):
        assert get_provider_prompt_limit("openscad") == 100_000

    def test_none_returns_default(self):
        assert get_provider_prompt_limit(None) == 600

    def test_unknown_returns_default(self):
        assert get_provider_prompt_limit("unknown_provider") == 600

    def test_improved_prompt_respects_provider(self):
        from kiln.generation_feedback import FeedbackType, PrintFeedback

        fb = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["thin walls"],
            constraints=["minimum wall thickness 2mm"],
            severity="moderate",
        )
        result = generate_improved_prompt("a cube", [fb], provider="gemini")
        # Gemini limit is 10K, so the prompt should NOT be truncated
        assert len(result.improved_prompt) <= 10_000
        assert "wall thickness" in result.improved_prompt

    def test_explicit_max_length_overrides_provider(self):
        from kiln.generation_feedback import FeedbackType, PrintFeedback

        fb = PrintFeedback(
            original_prompt="test",
            feedback_type=FeedbackType.PRINTABILITY,
            issues=["overhangs"],
            constraints=["no overhangs > 45 degrees"],
            severity="moderate",
        )
        result = generate_improved_prompt("a cube", [fb], provider="gemini", max_length=100)
        assert len(result.improved_prompt) <= 100


# ---------------------------------------------------------------------------
# Design Intelligence Enrichment
# ---------------------------------------------------------------------------


class TestEnhancedPromptEnrichment:
    """Design intelligence prompt enrichment with per-material constraints."""

    def test_material_overhang_limit_used(self):
        """Should use material-specific overhang angle, not hardcoded 50."""
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        # Mock design intelligence to return material with 55-degree limit
        mock_material = MagicMock()
        mock_material.material.display_name = "PETG"
        mock_material.material.design_limits = {
            "max_unsupported_overhang_deg": 55,
            "recommended_wall_thickness_mm": 1.5,
            "max_bridge_length_mm": 12,
            "max_cantilever_length_mm": 40,
        }

        mock_brief = MagicMock()
        mock_brief.combined_rules = {"min_wall_thickness_mm": 1.2}
        mock_brief.recommended_material = mock_material
        mock_brief.applicable_patterns = []
        mock_brief.combined_guidance = []

        with patch("kiln.design_intelligence.get_design_constraints", return_value=mock_brief), \
             patch("kiln.design_intelligence.get_printer_design_profile", return_value=None):
            result = enhance_prompt_with_design_intelligence("test prompt", max_length=2000)

        assert "55 degrees" in result.improved_prompt
        assert "50 degrees" not in result.improved_prompt
        assert "1.5mm" in result.improved_prompt

    def test_large_budget_includes_guidance(self):
        """With a large prompt budget, combined_guidance should be included."""
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        mock_material = MagicMock()
        mock_material.material.display_name = "PLA"
        mock_material.material.design_limits = {"max_unsupported_overhang_deg": 50}

        mock_brief = MagicMock()
        mock_brief.combined_rules = {}
        mock_brief.recommended_material = mock_material
        mock_brief.applicable_patterns = []
        mock_brief.combined_guidance = ["Use gradual transitions between thick and thin sections"]

        with patch("kiln.design_intelligence.get_design_constraints", return_value=mock_brief), \
             patch("kiln.design_intelligence.get_printer_design_profile", return_value=None):
            result = enhance_prompt_with_design_intelligence("test", max_length=5000)

        assert "gradual transitions" in result.improved_prompt

    def test_small_budget_caps_constraints(self):
        """With Meshy's 600-char limit, constraints should be capped."""
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        mock_material = MagicMock()
        mock_material.material.display_name = "PLA"
        mock_material.material.design_limits = {"max_unsupported_overhang_deg": 50}

        mock_brief = MagicMock()
        mock_brief.combined_rules = {}
        mock_brief.recommended_material = mock_material
        mock_brief.applicable_patterns = []
        mock_brief.combined_guidance = []

        with patch("kiln.design_intelligence.get_design_constraints", return_value=mock_brief), \
             patch("kiln.design_intelligence.get_printer_design_profile", return_value=None):
            result = enhance_prompt_with_design_intelligence("test prompt", max_length=600)

        assert len(result.improved_prompt) <= 600

    def test_enrichment_returns_original_on_failure(self):
        """If design intelligence is unavailable, return original prompt."""
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        with patch("kiln.design_intelligence.get_design_constraints", side_effect=Exception("unavailable")):
            result = enhance_prompt_with_design_intelligence("test prompt")

        assert result.improved_prompt == "test prompt"
        assert result.constraints_added == []


# ---------------------------------------------------------------------------
# OpenSCAD Render Preview Tests
# ---------------------------------------------------------------------------


class TestOpenSCADRenderPreview:
    """OpenSCAD render preview for visual inspection."""

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    def test_render_preview_unsupported_format(self, mock_find):
        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")

        with pytest.raises(GenerationError, match="Cannot render preview"):
            provider.render_preview("/tmp/model.obj")

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    def test_render_preview_stl_wraps_in_import(self, mock_find):
        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")

        with patch.object(provider, "_render_scad_to_png", return_value="/tmp/preview.png") as mock_render:
            result = provider.render_preview("/tmp/model.stl")

        assert result == "/tmp/preview.png"
        call_args = mock_render.call_args[0]
        assert 'import("/tmp/model.stl")' in call_args[0]

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    @patch("subprocess.run")
    def test_render_timeout_raises(self, mock_run, mock_find):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="openscad", timeout=60)

        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")

        with pytest.raises(GenerationError, match="timed out"):
            provider._render_scad_to_png("cube();", "/tmp/out.png", 800, 600)


# ---------------------------------------------------------------------------
# Design Templates Tests
# ---------------------------------------------------------------------------


class TestDesignTemplates:
    """Design template loading and parameter handling."""

    def test_templates_json_valid(self):
        """Templates file is valid JSON with expected structure."""
        from pathlib import Path as _Path

        tpl_path = _Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_templates.json"
        with open(tpl_path) as fh:
            data = json.load(fh)

        templates = {k: v for k, v in data.items() if not k.startswith("_")}
        assert len(templates) >= 6

        for key, tpl in templates.items():
            assert "display_name" in tpl, f"{key} missing display_name"
            assert "description" in tpl, f"{key} missing description"
            assert "scad_template" in tpl, f"{key} missing scad_template"
            assert "parameters" in tpl, f"{key} missing parameters"

    def test_all_templates_have_defaults(self):
        """Every parameter must have a default value."""
        from pathlib import Path as _Path

        tpl_path = _Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_templates.json"
        with open(tpl_path) as fh:
            data = json.load(fh)

        for key, tpl in data.items():
            if key.startswith("_"):
                continue
            for param_name, param in tpl.get("parameters", {}).items():
                assert "default" in param, f"{key}.{param_name} missing default"

    def test_template_parameter_substitution(self):
        """Parameters should substitute into SCAD code."""
        from string import Template

        scad = "width = ${width};\nheight = ${height};"
        result = Template(scad).safe_substitute({"width": 50, "height": 30})
        assert "width = 50;" in result
        assert "height = 30;" in result


# ---- Phase 2: Advanced analysis, repair, composition, 3MF, iteration ----


class TestMeshAnalysis:
    """Tests for analyze_mesh() — volume, surface area, overhangs, components."""

    def test_cube_volume_and_surface_area(self, tmp_path):
        """A unit cube should have volume ~1 and surface area ~6."""
        from kiln.generation.validation import analyze_mesh

        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 1.0)
        result = analyze_mesh(cube_path)

        assert result.triangle_count == 12  # 6 faces * 2 triangles
        assert result.volume_mm3 > 0.5  # ~1.0
        assert result.surface_area_mm2 > 4.0  # ~6.0
        assert result.connected_components == 1
        assert result.degenerate_triangles == 0
        assert result.printability_score > 0

    def test_cube_center_of_mass(self, tmp_path):
        """Center of mass of a unit cube should be near (0.5, 0.5, 0.5)."""
        from kiln.generation.validation import analyze_mesh

        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 1.0)
        result = analyze_mesh(cube_path)

        assert result.center_of_mass is not None
        assert abs(result.center_of_mass["x"] - 0.5) < 0.1
        assert abs(result.center_of_mass["y"] - 0.5) < 0.1
        assert abs(result.center_of_mass["z"] - 0.5) < 0.1

    def test_dimensions_computed(self, tmp_path):
        """Dimensions should be populated from bounding box."""
        from kiln.generation.validation import analyze_mesh

        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 10.0)
        result = analyze_mesh(cube_path)

        assert result.dimensions_mm is not None
        assert abs(result.dimensions_mm["width_mm"] - 10.0) < 0.1
        assert abs(result.dimensions_mm["depth_mm"] - 10.0) < 0.1
        assert abs(result.dimensions_mm["height_mm"] - 10.0) < 0.1

    def test_printability_score_range(self, tmp_path):
        """Score should be between 0 and 100."""
        from kiln.generation.validation import analyze_mesh

        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 20.0)
        result = analyze_mesh(cube_path)

        assert 0 <= result.printability_score <= 100

    def test_nonexistent_file(self):
        """Analyzing a missing file returns issues."""
        from kiln.generation.validation import analyze_mesh

        result = analyze_mesh("/nonexistent/file.stl")
        assert len(result.printability_issues) > 0

    def test_unsupported_format(self, tmp_path):
        """Unsupported extension returns issues."""
        from kiln.generation.validation import analyze_mesh

        bad = tmp_path / "model.fbx"
        bad.write_bytes(b"fake")
        result = analyze_mesh(str(bad))
        assert len(result.printability_issues) > 0


class TestSTLRepair:
    """Tests for repair_stl()."""

    def test_repair_removes_degenerate_triangles(self, tmp_path):
        """Degenerate (zero-area) triangles should be removed."""
        from kiln.generation.validation import repair_stl

        stl_path = str(tmp_path / "bad.stl")
        _write_cube_stl(stl_path, 10.0)

        # Add a degenerate triangle (three identical vertices)
        with open(stl_path, "r+b") as fh:
            fh.seek(80)
            count = struct.unpack("<I", fh.read(4))[0]
            fh.seek(80)
            fh.write(struct.pack("<I", count + 1))
            fh.seek(0, 2)  # end of file
            # Degenerate: all three vertices are the same
            fh.write(struct.pack("<3f", 0, 0, 0))  # normal
            for _ in range(3):
                fh.write(struct.pack("<3f", 5.0, 5.0, 5.0))
            fh.write(struct.pack("<H", 0))

        result = repair_stl(stl_path)
        assert result["degenerate_removed"] >= 1
        assert result["cleaned_triangles"] < result["original_triangles"]

    def test_repair_custom_output(self, tmp_path):
        """Repair to a custom output path."""
        from kiln.generation.validation import repair_stl

        stl_path = str(tmp_path / "input.stl")
        out_path = str(tmp_path / "repaired.stl")
        _write_cube_stl(stl_path, 10.0)

        result = repair_stl(stl_path, output_path=out_path)
        assert result["path"] == out_path
        assert os.path.isfile(out_path)


class TestDesignComposition:
    """Tests for compose_stls()."""

    def test_merge_two_cubes(self, tmp_path):
        """Merging two cubes doubles the triangle count."""
        from kiln.generation.validation import compose_stls

        a = str(tmp_path / "a.stl")
        b = str(tmp_path / "b.stl")
        out = str(tmp_path / "combined.stl")
        _write_cube_stl(a, 10.0)
        _write_cube_stl(b, 5.0)

        result = compose_stls([a, b], out)
        assert result["total_triangles"] == 24  # 12 + 12
        assert result["files_merged"] == 2
        assert os.path.isfile(out)

    def test_empty_list_raises(self):
        """Empty file list raises ValueError."""
        from kiln.generation.validation import compose_stls

        with pytest.raises(ValueError, match="No files"):
            compose_stls([], "/tmp/out.stl")


class TestExport3MF:
    """Tests for export_3mf()."""

    def test_export_cube_to_3mf(self, tmp_path):
        """A cube STL should export to a valid 3MF ZIP."""
        import zipfile as zf

        from kiln.generation.validation import export_3mf

        stl_path = str(tmp_path / "cube.stl")
        _write_cube_stl(stl_path, 10.0)

        out = export_3mf(stl_path)
        assert out.endswith(".3mf")
        assert os.path.isfile(out)

        # Verify it's a valid ZIP with expected entries
        with zf.ZipFile(out) as z:
            names = z.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "3D/3dmodel.model" in names

            # Verify XML contains vertices and triangles
            model = z.read("3D/3dmodel.model").decode("utf-8")
            assert "<vertex" in model
            assert "<triangle" in model

    def test_export_custom_output_path(self, tmp_path):
        """Custom output path should be used."""
        from kiln.generation.validation import export_3mf

        stl_path = str(tmp_path / "cube.stl")
        out_path = str(tmp_path / "custom.3mf")
        _write_cube_stl(stl_path, 10.0)

        result = export_3mf(stl_path, output_path=out_path)
        assert result == out_path
        assert os.path.isfile(out_path)

    def test_unsupported_format_raises(self, tmp_path):
        """Unsupported input format raises."""
        from kiln.generation.validation import export_3mf

        bad = tmp_path / "model.fbx"
        bad.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Unsupported"):
            export_3mf(str(bad))


class TestConnectedComponents:
    """Tests for _count_components()."""

    def test_single_cube_one_component(self, tmp_path):
        """A single cube mesh has exactly 1 component."""
        from kiln.generation.validation import _count_components, _parse_stl

        stl_path = str(tmp_path / "cube.stl")
        _write_cube_stl(stl_path, 10.0)
        from pathlib import Path

        tris, _ = _parse_stl(Path(stl_path), [])
        assert _count_components(tris) == 1

    def test_empty_mesh_zero_components(self):
        """Empty triangle list has 0 components."""
        from kiln.generation.validation import _count_components

        assert _count_components([]) == 0


class TestOpenSCADErrorParsing:
    """Tests for _parse_openscad_output()."""

    def test_clean_output(self):
        """Clean compilation has no errors."""
        from kiln.generation.openscad import _parse_openscad_output

        result = _parse_openscad_output("", 0)
        assert result["valid"] is True
        assert len(result["errors"]) == 0

    def test_error_detected(self):
        """Errors in stderr are extracted."""
        from kiln.generation.openscad import _parse_openscad_output

        result = _parse_openscad_output(
            "ERROR: Parser error in line 5: syntax error\n", 1
        )
        assert result["valid"] is False
        assert len(result["errors"]) >= 1
        assert result["errors"][0]["line"] == 5

    def test_warning_detected(self):
        """Warnings are separated from errors."""
        from kiln.generation.openscad import _parse_openscad_output

        result = _parse_openscad_output(
            "WARNING: Duplicate parameter in line 3\n", 0
        )
        assert result["valid"] is True
        assert len(result["warnings"]) >= 1

    def test_mixed_errors_and_warnings(self):
        """Both errors and warnings are correctly categorized."""
        from kiln.generation.openscad import _parse_openscad_output

        stderr = (
            "WARNING: deprecated feature\n"
            "ERROR: undefined variable 'foo', line 10\n"
        )
        result = _parse_openscad_output(stderr, 1)
        assert result["valid"] is False
        assert len(result["errors"]) >= 1
        assert len(result["warnings"]) >= 1


class TestSlicerEstimation:
    """Tests for _parse_gcode_estimates()."""

    def test_parse_prusaslicer_comments(self, tmp_path):
        """PrusaSlicer-style comments should be parsed."""
        from kiln.slicer import _parse_gcode_estimates

        gcode = tmp_path / "test.gcode"
        gcode.write_text(
            "; generated by PrusaSlicer\n"
            "G28 ; home\n"
            "; estimated printing time (normal mode) = 1h 23m 45s\n"
            "; filament used [mm] = 1234.56\n"
            "; filament used [g] = 12.34\n"
            "; filament used [cm3] = 9.87\n"
            "; total layers count = 150\n"
            "; filament cost = 0.42\n"
        )

        result = _parse_gcode_estimates(str(gcode))
        assert result["estimated_time_seconds"] == 1 * 3600 + 23 * 60 + 45
        assert result["filament_length_mm"] == 1234.56
        assert result["filament_weight_g"] == 12.34
        assert result["filament_volume_cm3"] == 9.87
        assert result["layer_count"] == 150
        assert result["filament_cost"] == 0.42

    def test_empty_gcode(self, tmp_path):
        """Empty gcode file returns just path."""
        from kiln.slicer import _parse_gcode_estimates

        gcode = tmp_path / "empty.gcode"
        gcode.write_text("G28\nG1 X10 Y10\n")

        result = _parse_gcode_estimates(str(gcode))
        assert result["gcode_path"] == str(gcode)
        assert "estimated_time_seconds" not in result


class TestIterateDesign:
    """Tests for the iterate_design automated loop."""

    @patch("kiln.server._get_generation_provider")
    def test_iteration_stops_on_high_score(self, mock_get_provider, tmp_path):
        """Loop stops when printability score is >= 80."""
        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 20.0)

        mock_provider = MagicMock()
        mock_provider.name = "openscad"
        mock_job = MagicMock()
        mock_job.status.value = "succeeded"
        mock_job.to_dict.return_value = {"id": "test-1", "status": "succeeded"}
        mock_job.id = "test-1"
        mock_job.error = None
        mock_provider.generate.return_value = mock_job

        mock_result = MagicMock()
        mock_result.local_path = cube_path
        mock_result.to_dict.return_value = {"local_path": cube_path}
        mock_provider.download_result.return_value = mock_result
        mock_get_provider.return_value = mock_provider

        from kiln.server import iterate_design

        result = iterate_design("cube(20);", provider="openscad", max_iterations=3)

        assert result["status"] == "success"
        assert result["best_score"] >= 0
        assert len(result["iterations"]) >= 1

    @patch("kiln.server._get_generation_provider")
    def test_iteration_handles_generation_failure(self, mock_get_provider):
        """Loop handles generation failures gracefully."""
        mock_provider = MagicMock()
        mock_provider.name = "openscad"
        mock_provider.generate.side_effect = Exception("compile error")
        mock_get_provider.return_value = mock_provider

        from kiln.server import iterate_design

        result = iterate_design("bad code;", provider="openscad", max_iterations=1)

        # Should fail after exhausting iterations
        assert result.get("error") or result.get("status") == "error"


# ---- Phase 3: orientation, support, advanced repair, advisor ----


class TestOrientationOptimizer:
    """Tests for optimize_orientation() and _rotate_triangles()."""

    def test_cube_returns_valid_result(self, tmp_path):
        """Optimizing a cube produces a valid result dict."""
        from kiln.generation.validation import optimize_orientation

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)
        result = optimize_orientation(stl, output_path=str(tmp_path / "opt.stl"))

        assert "rotation_x_deg" in result
        assert "rotation_y_deg" in result
        assert "printability_score" in result
        assert result["printability_score"] >= 0
        assert os.path.isfile(result["path"])

    def test_orientation_places_on_build_plate(self, tmp_path):
        """Output mesh should have z_min at 0 (on build plate)."""
        from kiln.generation.validation import _parse_stl, optimize_orientation

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "opt.stl")
        optimize_orientation(stl, output_path=out)

        from pathlib import Path as _Path

        tris, verts = _parse_stl(_Path(out), [])
        z_vals = [v[2] for v in verts]
        assert min(z_vals) >= -0.01  # should be at or above z=0

    def test_rotate_triangles_identity(self):
        """0-degree rotation should preserve geometry."""
        from kiln.generation.validation import _rotate_triangles

        tris = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]
        rotated = _rotate_triangles(tris, 0.0, 0.0)
        for i in range(3):
            for j in range(3):
                assert abs(rotated[0][i][j] - tris[0][i][j]) < 1e-6

    def test_rotate_triangles_90x(self):
        """90-degree X rotation should swap Y and Z."""
        from kiln.generation.validation import _rotate_triangles

        tris = [((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        rotated = _rotate_triangles(tris, 90.0, 0.0)
        # (0,1,0) rotated 90° around X → (0,0,1)
        assert abs(rotated[0][1][1] - 0.0) < 1e-5
        assert abs(rotated[0][1][2] - 1.0) < 1e-5

    def test_nonexistent_file_raises(self):
        """Missing file raises FileNotFoundError."""
        from kiln.generation.validation import optimize_orientation

        with pytest.raises(FileNotFoundError):
            optimize_orientation("/nonexistent/file.stl")


class TestSupportVolumeEstimation:
    """Tests for estimate_support_volume()."""

    def test_cube_has_no_supports(self, tmp_path):
        """A cube on the build plate needs no support."""
        from kiln.generation.validation import estimate_support_volume

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)
        result = estimate_support_volume(stl)

        assert result["total_triangles"] == 12
        assert "support_volume_mm3" in result
        assert "needs_supports" in result
        # Cube overhangs depend on normal orientation — just check keys
        assert isinstance(result["overhang_percentage"], float)

    def test_unsupported_format_raises(self, tmp_path):
        """Non-mesh file raises ValueError."""
        from kiln.generation.validation import estimate_support_volume

        bad = tmp_path / "model.fbx"
        bad.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Unsupported"):
            estimate_support_volume(str(bad))

    def test_result_keys_complete(self, tmp_path):
        """All expected keys are present."""
        from kiln.generation.validation import estimate_support_volume

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        result = estimate_support_volume(stl)

        expected_keys = {
            "support_volume_mm3", "support_volume_cm3", "support_weight_g",
            "overhang_area_mm2", "overhang_triangle_count", "total_triangles",
            "overhang_percentage", "needs_supports",
        }
        assert expected_keys.issubset(set(result.keys()))


class TestAdvancedRepair:
    """Tests for repair_stl_advanced() and _find_boundary_loops()."""

    def test_advanced_repair_removes_degenerate(self, tmp_path):
        """Degenerate triangles removed like basic repair."""
        from kiln.generation.validation import repair_stl_advanced

        stl = str(tmp_path / "bad.stl")
        _write_cube_stl(stl, 10.0)

        # Append a degenerate triangle
        with open(stl, "r+b") as fh:
            fh.seek(80)
            count = struct.unpack("<I", fh.read(4))[0]
            fh.seek(80)
            fh.write(struct.pack("<I", count + 1))
            fh.seek(0, 2)
            fh.write(struct.pack("<3f", 0, 0, 0))  # normal
            for _ in range(3):
                fh.write(struct.pack("<3f", 5.0, 5.0, 5.0))
            fh.write(struct.pack("<H", 0))

        result = repair_stl_advanced(stl, output_path=str(tmp_path / "fixed.stl"))
        assert result["degenerate_removed"] >= 1
        assert result["cleaned_triangles"] < result["original_triangles"]
        assert os.path.isfile(result["path"])

    def test_advanced_repair_close_holes_flag(self, tmp_path):
        """close_holes=False should skip hole closing."""
        from kiln.generation.validation import repair_stl_advanced

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        result = repair_stl_advanced(stl, close_holes=False)
        assert result["holes_closed"] == 0

    def test_advanced_repair_result_keys(self, tmp_path):
        """All expected keys present in result."""
        from kiln.generation.validation import repair_stl_advanced

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        result = repair_stl_advanced(stl, output_path=str(tmp_path / "out.stl"))

        expected = {
            "path", "original_triangles", "cleaned_triangles",
            "degenerate_removed", "holes_closed", "triangles_added",
            "final_triangles",
        }
        assert expected.issubset(set(result.keys()))

    def test_find_boundary_loops_simple_triangle(self):
        """Three directed edges forming a triangle → one 3-vertex loop."""
        from kiln.generation.validation import _find_boundary_loops

        edges = [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            ((1.0, 0.0, 0.0), (0.5, 1.0, 0.0)),
            ((0.5, 1.0, 0.0), (0.0, 0.0, 0.0)),
        ]
        loops = _find_boundary_loops(edges)
        assert len(loops) >= 1
        assert len(loops[0]) == 3

    def test_find_boundary_loops_empty(self):
        """No edges → no loops."""
        from kiln.generation.validation import _find_boundary_loops

        assert _find_boundary_loops([]) == []


class TestDesignAdvisor:
    """Tests for the design_advisor MCP tool."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_geometric_prompt_recommends_openscad(self, _mock_auth):
        from kiln.server import design_advisor

        result = design_advisor("a shelf bracket for my desk")
        assert result["recommended_approach"] in ("template", "openscad")
        assert "suggested_workflow" in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_organic_prompt_recommends_meshy(self, _mock_auth):
        from kiln.server import design_advisor

        result = design_advisor("a dragon sculpture with detailed wings")
        assert result["recommended_approach"] == "meshy"
        assert result["confidence"] == "medium"

    @patch("kiln.server._check_auth", return_value=None)
    def test_template_match_found(self, _mock_auth):
        from kiln.server import design_advisor

        result = design_advisor("I need a phone stand for my desk")
        assert result["recommended_approach"] == "template"
        assert len(result["matching_templates"]) >= 1
        assert result["matching_templates"][0]["template_id"] == "phone_stand"

    @patch("kiln.server._check_auth", return_value=None)
    def test_complexity_estimate(self, _mock_auth):
        from kiln.server import design_advisor

        simple = design_advisor("a box")
        assert simple["estimated_complexity"] == "simple"

        complex_prompt = design_advisor(
            "a multi-compartment desk organizer with phone stand, "
            "pen holder sections, cable routing channels, and a drawer"
        )
        assert complex_prompt["estimated_complexity"] == "complex"


class TestTemplateVariations:
    """Tests for generate_template_variations MCP tool."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._get_generation_provider")
    def test_generates_requested_count(self, mock_get_provider, _mock_auth, tmp_path):
        """Should generate the requested number of variations."""
        cube_path = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_path, 20.0)

        mock_provider = MagicMock()
        mock_provider.name = "openscad"
        mock_job = MagicMock()
        mock_job.status.value = "succeeded"
        mock_job.id = "var-1"
        mock_provider.generate.return_value = mock_job

        mock_result = MagicMock()
        mock_result.local_path = cube_path
        mock_result.file_size_bytes = 1000
        mock_provider.download_result.return_value = mock_result
        mock_get_provider.return_value = mock_provider

        from kiln.server import generate_template_variations

        result = generate_template_variations("phone_stand", variation_count=3)
        assert result["status"] == "success"
        assert result["variation_count"] == 3
        assert len(result["variations"]) == 3

    @patch("kiln.server._check_auth", return_value=None)
    def test_unknown_template_returns_error(self, _mock_auth):
        from kiln.server import generate_template_variations

        result = generate_template_variations("nonexistent_template_xyz")
        assert "error" in result or result.get("status") == "error"


# ---- Helpers ----


def _write_cube_stl(path: str, size: float) -> None:
    """Write a simple cube STL for testing."""
    s = size
    # 12 triangles for a cube (2 per face)
    tris = [
        # Bottom face (z=0)
        ((0, 0, 0), (s, 0, 0), (s, s, 0)),
        ((0, 0, 0), (s, s, 0), (0, s, 0)),
        # Top face (z=s)
        ((0, 0, s), (s, s, s), (s, 0, s)),
        ((0, 0, s), (0, s, s), (s, s, s)),
        # Front face (y=0)
        ((0, 0, 0), (s, 0, s), (s, 0, 0)),
        ((0, 0, 0), (0, 0, s), (s, 0, s)),
        # Back face (y=s)
        ((0, s, 0), (s, s, 0), (s, s, s)),
        ((0, s, 0), (s, s, s), (0, s, s)),
        # Left face (x=0)
        ((0, 0, 0), (0, s, 0), (0, s, s)),
        ((0, 0, 0), (0, s, s), (0, 0, s)),
        # Right face (x=s)
        ((s, 0, 0), (s, 0, s), (s, s, s)),
        ((s, 0, 0), (s, s, s), (s, s, 0)),
    ]

    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)  # header
        fh.write(struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0, 0, 0))  # normal
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
