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
