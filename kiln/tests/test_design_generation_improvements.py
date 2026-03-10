"""Tests for design generation improvements.

Covers:
- GLB parsing and GLB-to-STL conversion
- Image-to-3D support in MeshyProvider
- Provider-aware prompt limits in feedback loop
- Enhanced design intelligence prompt enrichment
- OpenSCAD render preview
- Mesh rescaling
- Design templates
- Phase 3: orientation optimizer, support estimation, advanced repair, design advisor
- Phase 4: mesh comparison, failure prediction, simplification, scorecard, cost, floating regions
- Phase 5: mesh mirroring, hollow shell, center on bed, non-manifold edge analysis
- Phase 7: 3MF model extraction (3MF → STL)
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
        assert "[image-to-3d]" in job.prompt

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
        # The stash version writes a temp .scad file with an import()
        # statement and passes the file path to _render_scad_to_png.
        scad_path = mock_render.call_args[0][0]
        assert scad_path.endswith(".scad")

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    @patch("subprocess.run")
    def test_render_timeout_raises(self, mock_run, mock_find):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="openscad", timeout=60)

        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")

        with pytest.raises(GenerationError, match="timed out"):
            provider._render_scad_to_png("/tmp/cube.scad", output_path="/tmp/out.png", width=800, height=600)


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
        assert len(templates) >= 20

        for key, tpl in templates.items():
            assert "display_name" in tpl, f"{key} missing display_name"
            assert "description" in tpl, f"{key} missing description"
            assert "scad_template" in tpl, f"{key} missing scad_template"
            assert "parameters" in tpl, f"{key} missing parameters"
            assert "category" in tpl, f"{key} missing category"

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
        """Optimizing a cube produces a valid result with non-negative score."""
        from kiln.generation.validation import optimize_orientation

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)
        result = optimize_orientation(stl, output_path=str(tmp_path / "opt.stl"))

        assert isinstance(result["rotation_x_deg"], (int, float))
        assert isinstance(result["rotation_y_deg"], (int, float))
        assert result["printability_score"] >= 0
        assert result["printability_score"] <= 100
        assert os.path.isfile(result["path"])
        # Output file should have the same triangle count.
        vr = validate_mesh(result["path"])
        assert vr.triangle_count == 12

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
        assert isinstance(result["overhang_percentage"], float)
        # Cube faces are axis-aligned — overhang % should be low.
        assert result["overhang_percentage"] < 50.0
        assert result["support_volume_mm3"] >= 0.0

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


# ---- Phase 4: comparison, failure prediction, simplification, scorecard, cost,
#      floating regions, print readiness gate ----


class TestMeshComparison:
    """Tests for compare_meshes()."""

    def test_identical_meshes(self, tmp_path):
        from kiln.generation.validation import compare_meshes

        a = str(tmp_path / "a.stl")
        b = str(tmp_path / "b.stl")
        _write_cube_stl(a, 20.0)
        _write_cube_stl(b, 20.0)
        result = compare_meshes(a, b)

        assert result["meshes_identical"] is True
        assert result["volume_delta_mm3"] == 0.0
        assert result["triangle_count_delta"] == 0

    def test_different_sizes_detected(self, tmp_path):
        from kiln.generation.validation import compare_meshes

        a = str(tmp_path / "small.stl")
        b = str(tmp_path / "big.stl")
        _write_cube_stl(a, 10.0)
        _write_cube_stl(b, 20.0)
        result = compare_meshes(a, b)

        assert result["meshes_identical"] is False
        assert result["volume_delta_mm3"] > 0
        assert result["volume_change_pct"] > 0
        assert "hausdorff_distance_mm" in result

    def test_hausdorff_zero_for_same(self, tmp_path):
        from kiln.generation.validation import compare_meshes

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 15.0)
        result = compare_meshes(f, f)
        assert result["hausdorff_distance_mm"] == 0.0

    def test_dimension_deltas(self, tmp_path):
        from kiln.generation.validation import compare_meshes

        a = str(tmp_path / "a.stl")
        b = str(tmp_path / "b.stl")
        _write_cube_stl(a, 10.0)
        _write_cube_stl(b, 20.0)
        result = compare_meshes(a, b)

        assert "dimensions_delta_mm" in result
        assert result["dimensions_delta_mm"]["width_mm"] == 10.0


class TestFailurePrediction:
    """Tests for predict_print_failures()."""

    def test_cube_not_high_risk(self, tmp_path):
        from kiln.generation.validation import predict_print_failures

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = predict_print_failures(f)

        assert result["verdict"] != "high_risk"
        assert result["risk_score"] < 50
        assert "failures" in result

    def test_result_has_required_keys(self, tmp_path):
        from kiln.generation.validation import predict_print_failures

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = predict_print_failures(f)

        for key in ("verdict", "risk_score", "failure_count", "failures",
                     "dimensions_mm", "triangle_count", "printability_score"):
            assert key in result

    def test_unsupported_format_raises(self, tmp_path):
        from kiln.generation.validation import predict_print_failures

        bad = tmp_path / "model.fbx"
        bad.write_bytes(b"fake")
        with pytest.raises(ValueError):
            predict_print_failures(str(bad))

    def test_custom_thresholds(self, tmp_path):
        from kiln.generation.validation import predict_print_failures

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        # Very strict thresholds — should still work without error
        result = predict_print_failures(
            f, min_wall_mm=5.0, max_bridge_mm=1.0, max_overhang_deg=10.0
        )
        assert isinstance(result["risk_score"], int)


class TestMeshSimplification:
    """Tests for simplify_mesh()."""

    def test_simplify_reduces_triangles(self, tmp_path):
        from kiln.generation.validation import simplify_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = simplify_mesh(f, target_ratio=0.5, output_path=str(tmp_path / "simple.stl"))

        assert result["simplified_triangles"] <= result["original_triangles"]
        assert result["reduction_pct"] >= 0.0
        assert os.path.isfile(result["path"])

    def test_ratio_1_no_change(self, tmp_path):
        from kiln.generation.validation import simplify_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = simplify_mesh(f, target_ratio=1.0, output_path=str(tmp_path / "same.stl"))

        assert result["simplified_triangles"] == result["original_triangles"]
        assert result["reduction_pct"] == 0.0

    def test_extreme_simplification(self, tmp_path):
        from kiln.generation.validation import simplify_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = simplify_mesh(f, target_ratio=0.01, output_path=str(tmp_path / "tiny.stl"))

        # Even extreme simplification should produce a valid file
        assert os.path.isfile(result["path"])
        assert result["simplified_triangles"] <= result["original_triangles"]

    def test_default_output_path(self, tmp_path):
        from kiln.generation.validation import simplify_mesh

        f = str(tmp_path / "model.stl")
        _write_cube_stl(f, 10.0)
        result = simplify_mesh(f, target_ratio=0.5)

        assert "_simplified" in result["path"]


class TestDesignScorecard:
    """Tests for design_scorecard()."""

    def test_cube_gets_good_grade(self, tmp_path):
        from kiln.generation.validation import design_scorecard

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = design_scorecard(f)

        assert result["grade"] in ("A", "B", "C")
        assert 0 <= result["overall_score"] <= 100
        assert "printability" in result
        assert "structural" in result
        assert "efficiency" in result
        assert "quality" in result

    def test_scorecard_factors_have_scores(self, tmp_path):
        from kiln.generation.validation import design_scorecard

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = design_scorecard(f)

        for factor in ("printability", "structural", "efficiency", "quality"):
            assert "score" in result[factor]
            assert "notes" in result[factor]
            assert 0 <= result[factor]["score"] <= 100

    def test_unparseable_raises(self, tmp_path):
        from kiln.generation.validation import design_scorecard

        bad = tmp_path / "bad.stl"
        bad.write_bytes(b"not an stl")
        with pytest.raises(ValueError):
            design_scorecard(str(bad))


class TestMaterialCost:
    """Tests for estimate_material_cost()."""

    def test_pla_cost_positive(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = estimate_material_cost(f)

        assert result["material"] == "pla"
        assert result["weight_g"] > 0
        assert result["filament_length_m"] > 0
        assert result["estimated_cost_usd"] > 0

    def test_different_materials(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        pla = estimate_material_cost(f, material="pla")
        petg = estimate_material_cost(f, material="petg")
        tpu = estimate_material_cost(f, material="tpu")

        # Different materials should have different costs
        assert pla["density_g_cm3"] != tpu["density_g_cm3"]
        assert petg["cost_per_kg_usd"] != tpu["cost_per_kg_usd"]

    def test_infill_affects_cost(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        low = estimate_material_cost(f, infill_pct=10.0)
        high = estimate_material_cost(f, infill_pct=80.0)

        assert high["weight_g"] > low["weight_g"]
        assert high["estimated_cost_usd"] > low["estimated_cost_usd"]

    def test_custom_cost_override(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        result = estimate_material_cost(f, cost_per_kg=100.0)
        assert result["cost_per_kg_usd"] == 100.0

    def test_unknown_material_defaults_to_pla(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        result = estimate_material_cost(f, material="unobtanium")
        assert result["density_g_cm3"] == 1.24  # PLA density


class TestFloatingRegionRemoval:
    """Tests for remove_floating_regions()."""

    def test_single_component_unchanged(self, tmp_path):
        from kiln.generation.validation import remove_floating_regions

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = remove_floating_regions(f, output_path=str(tmp_path / "out.stl"))

        assert result["removed_components"] == 0
        assert result["kept_triangles"] == result["original_triangles"]

    def test_removes_small_component(self, tmp_path):
        from kiln.generation.validation import remove_floating_regions

        f = str(tmp_path / "multi.stl")
        _write_two_component_stl(f)  # big cube + tiny cube
        result = remove_floating_regions(f, output_path=str(tmp_path / "clean.stl"))

        assert result["original_components"] == 2
        assert result["removed_components"] == 1
        assert result["kept_triangles"] < result["original_triangles"]

    def test_keep_all_above_threshold(self, tmp_path):
        from kiln.generation.validation import remove_floating_regions

        f = str(tmp_path / "multi.stl")
        _write_two_component_stl(f)
        # Set threshold very low so both components are kept
        result = remove_floating_regions(
            f, keep_largest=False, min_triangle_pct=0.1,
            output_path=str(tmp_path / "all.stl"),
        )
        assert result["kept_components"] == 2


class TestPrintReadinessGate:
    """Tests for can_print_now()."""

    def test_clean_cube_can_print(self, tmp_path):
        from kiln.generation.validation import can_print_now

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = can_print_now(f)

        # Both fields must be consistent: can_print=True when verdict is printable.
        assert result["can_print"] is True
        assert result["verdict"] in ("ready_to_print", "printable_with_supports")
        assert isinstance(result["issues"], list)

    def test_oversized_fails(self, tmp_path):
        from kiln.generation.validation import can_print_now

        f = str(tmp_path / "huge.stl")
        _write_cube_stl(f, 300.0)
        result = can_print_now(f, printer_bed_mm=(200.0, 200.0, 200.0))

        assert result["can_print"] is False
        assert any(i["type"] == "too_large" for i in result["issues"])

    def test_auto_fix_returns_actions(self, tmp_path):
        from kiln.generation.validation import can_print_now

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        out = str(tmp_path / "fixed.stl")
        result = can_print_now(f, auto_fix=True, output_path=out)

        assert "actions_taken" in result
        assert isinstance(result["actions_taken"], list)

    def test_bad_file_returns_unprintable(self, tmp_path):
        from kiln.generation.validation import can_print_now

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"not valid")
        result = can_print_now(f)

        assert result["can_print"] is False
        assert result["verdict"] == "unprintable"


# ---- Helpers ----


def _write_two_component_stl(path: str) -> None:
    """Write an STL with two disconnected cubes (big + tiny)."""
    s = 20.0  # big cube
    t = 2.0   # tiny cube offset far away
    ox, oy, oz = 50.0, 50.0, 50.0  # offset for tiny cube

    big_tris = [
        ((0, 0, 0), (s, 0, 0), (s, s, 0)),
        ((0, 0, 0), (s, s, 0), (0, s, 0)),
        ((0, 0, s), (s, s, s), (s, 0, s)),
        ((0, 0, s), (0, s, s), (s, s, s)),
        ((0, 0, 0), (s, 0, s), (s, 0, 0)),
        ((0, 0, 0), (0, 0, s), (s, 0, s)),
        ((0, s, 0), (s, s, 0), (s, s, s)),
        ((0, s, 0), (s, s, s), (0, s, s)),
        ((0, 0, 0), (0, s, 0), (0, s, s)),
        ((0, 0, 0), (0, s, s), (0, 0, s)),
        ((s, 0, 0), (s, 0, s), (s, s, s)),
        ((s, 0, 0), (s, s, s), (s, s, 0)),
    ]
    tiny_tris = [
        ((ox, oy, oz), (ox + t, oy, oz), (ox + t, oy + t, oz)),
        ((ox, oy, oz), (ox + t, oy + t, oz), (ox, oy + t, oz)),
        ((ox, oy, oz + t), (ox + t, oy + t, oz + t), (ox + t, oy, oz + t)),
        ((ox, oy, oz + t), (ox, oy + t, oz + t), (ox + t, oy + t, oz + t)),
        ((ox, oy, oz), (ox + t, oy, oz + t), (ox + t, oy, oz)),
        ((ox, oy, oz), (ox, oy, oz + t), (ox + t, oy, oz + t)),
        ((ox, oy + t, oz), (ox + t, oy + t, oz), (ox + t, oy + t, oz + t)),
        ((ox, oy + t, oz), (ox + t, oy + t, oz + t), (ox, oy + t, oz + t)),
        ((ox, oy, oz), (ox, oy + t, oz), (ox, oy + t, oz + t)),
        ((ox, oy, oz), (ox, oy + t, oz + t), (ox, oy, oz + t)),
        ((ox + t, oy, oz), (ox + t, oy, oz + t), (ox + t, oy + t, oz + t)),
        ((ox + t, oy, oz), (ox + t, oy + t, oz + t), (ox + t, oy + t, oz)),
    ]
    all_tris = big_tris + tiny_tris

    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(all_tris)))
        for tri in all_tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))


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


def _write_offset_cube_stl(path: str, size: float, offset_x: float, offset_y: float, offset_z: float) -> None:
    """Write a cube STL offset from origin."""
    s = size
    ox, oy, oz = offset_x, offset_y, offset_z
    tris = [
        ((ox, oy, oz), (ox + s, oy, oz), (ox + s, oy + s, oz)),
        ((ox, oy, oz), (ox + s, oy + s, oz), (ox, oy + s, oz)),
        ((ox, oy, oz + s), (ox + s, oy + s, oz + s), (ox + s, oy, oz + s)),
        ((ox, oy, oz + s), (ox, oy + s, oz + s), (ox + s, oy + s, oz + s)),
        ((ox, oy, oz), (ox + s, oy, oz + s), (ox + s, oy, oz)),
        ((ox, oy, oz), (ox, oy, oz + s), (ox + s, oy, oz + s)),
        ((ox, oy + s, oz), (ox + s, oy + s, oz), (ox + s, oy + s, oz + s)),
        ((ox, oy + s, oz), (ox + s, oy + s, oz + s), (ox, oy + s, oz + s)),
        ((ox, oy, oz), (ox, oy + s, oz), (ox, oy + s, oz + s)),
        ((ox, oy, oz), (ox, oy + s, oz + s), (ox, oy, oz + s)),
        ((ox + s, oy, oz), (ox + s, oy, oz + s), (ox + s, oy + s, oz + s)),
        ((ox + s, oy, oz), (ox + s, oy + s, oz + s), (ox + s, oy + s, oz)),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))


# ===========================================================================
# Phase 5 Tests: Mirror, Hollow, Center, Non-Manifold Edge Analysis
# ===========================================================================


class TestMirrorMesh:
    """Tests for mirror_mesh() — reflect mesh along an axis with winding reversal."""

    def test_mirror_x_negates_x_coords(self, tmp_path):
        from kiln.generation.validation import _parse_stl, mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        out = str(tmp_path / "mirrored.stl")
        result = mirror_mesh(f, axis="x", output_path=out)

        assert result["axis"] == "x"
        assert result["triangle_count"] == 12
        assert os.path.isfile(out)

        # Verify mirrored geometry: all x coords should be <= 0
        from pathlib import Path
        errors = []
        tris, verts = _parse_stl(Path(out), errors)
        assert not errors
        xs = [v[0] for v in verts]
        assert max(xs) <= 0.001  # original was 0..20, mirrored should be -20..0

    def test_mirror_y_negates_y_coords(self, tmp_path):
        from kiln.generation.validation import _parse_stl, mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 15.0)
        out = str(tmp_path / "mirrored_y.stl")
        result = mirror_mesh(f, axis="y", output_path=out)

        assert result["axis"] == "y"
        from pathlib import Path
        errors = []
        _, verts = _parse_stl(Path(out), errors)
        ys = [v[1] for v in verts]
        assert max(ys) <= 0.001

    def test_mirror_z_negates_z_coords(self, tmp_path):
        from kiln.generation.validation import _parse_stl, mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 10.0)
        out = str(tmp_path / "mirrored_z.stl")
        result = mirror_mesh(f, axis="z", output_path=out)

        assert result["axis"] == "z"
        from pathlib import Path
        errors = []
        _, verts = _parse_stl(Path(out), errors)
        zs = [v[2] for v in verts]
        assert max(zs) <= 0.001

    def test_invalid_axis_raises(self, tmp_path):
        from kiln.generation.validation import mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 10.0)
        with pytest.raises(ValueError, match="axis must be"):
            mirror_mesh(f, axis="q")

    def test_preserves_triangle_count(self, tmp_path):
        from kiln.generation.validation import mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = mirror_mesh(f, axis="x", output_path=str(tmp_path / "m.stl"))
        assert result["triangle_count"] == 12

    def test_double_mirror_roundtrip(self, tmp_path):
        from pathlib import Path

        from kiln.generation.validation import _parse_stl, mirror_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        # Read original vertices
        errors = []
        _, orig_verts = _parse_stl(Path(f), errors)
        orig_xs = sorted(v[0] for v in orig_verts)

        # Mirror twice should restore original coordinates
        m1 = str(tmp_path / "m1.stl")
        mirror_mesh(f, axis="x", output_path=m1)
        m2 = str(tmp_path / "m2.stl")
        mirror_mesh(m1, axis="x", output_path=m2)

        errors2 = []
        _, round_verts = _parse_stl(Path(m2), errors2)
        round_xs = sorted(v[0] for v in round_verts)

        assert len(orig_xs) == len(round_xs)
        for a, b in zip(orig_xs, round_xs, strict=True):
            assert abs(a - b) < 0.01

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import mirror_mesh

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"garbage")
        with pytest.raises(ValueError):
            mirror_mesh(f)


class TestHollowMesh:
    """Tests for hollow_mesh() — create inner offset shell for material savings."""

    def test_hollow_doubles_triangles(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 30.0)
        out = str(tmp_path / "hollow.stl")
        result = hollow_mesh(f, wall_thickness_mm=2.0, output_path=out)

        assert result["original_triangles"] == 12
        assert result["total_triangles"] == 24  # outer + inner shells
        assert os.path.isfile(out)

    def test_material_savings_reported(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 40.0)
        result = hollow_mesh(f, wall_thickness_mm=3.0, output_path=str(tmp_path / "h.stl"))

        assert result["estimated_volume_saved_mm3"] > 0
        assert 0 < result["estimated_material_saved_pct"] < 100
        assert result["scale_factor"] > 0
        assert result["scale_factor"] < 1.0

    def test_thin_wall_increases_savings(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 40.0)

        thin = hollow_mesh(f, wall_thickness_mm=1.0, output_path=str(tmp_path / "thin.stl"))
        thick = hollow_mesh(f, wall_thickness_mm=5.0, output_path=str(tmp_path / "thick.stl"))

        # Thinner walls = more material saved
        assert thin["estimated_material_saved_pct"] > thick["estimated_material_saved_pct"]

    def test_too_thick_raises(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 10.0)
        with pytest.raises(ValueError, match="too small"):
            hollow_mesh(f, wall_thickness_mm=6.0)

    def test_extremely_thick_wall_raises(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        with pytest.raises(ValueError):
            hollow_mesh(f, wall_thickness_mm=9.6)  # scale < 0.05

    def test_default_output_path_suffix(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "model.stl")
        _write_cube_stl(f, 30.0)
        result = hollow_mesh(f)

        assert "_hollow" in result["path"]
        assert os.path.isfile(result["path"])

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"not valid")
        with pytest.raises(ValueError):
            hollow_mesh(f)


class TestCenterOnBed:
    """Tests for center_on_bed() — center model on build plate at z=0."""

    def test_offset_cube_gets_centered(self, tmp_path):
        from pathlib import Path

        from kiln.generation.validation import _parse_stl, center_on_bed

        f = str(tmp_path / "offset.stl")
        _write_offset_cube_stl(f, 20.0, 100.0, 100.0, 50.0)
        out = str(tmp_path / "centered.stl")
        result = center_on_bed(f, bed_x_mm=200.0, bed_y_mm=200.0, output_path=out)

        assert result["already_centered"] is False
        assert "translation_mm" in result
        assert os.path.isfile(out)

        # Verify the mesh is now centered on bed
        errors = []
        _, verts = _parse_stl(Path(out), errors)
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]

        center_x = (min(xs) + max(xs)) / 2.0
        center_y = (min(ys) + max(ys)) / 2.0
        assert abs(center_x - 100.0) < 0.1  # centered on bed_x/2
        assert abs(center_y - 100.0) < 0.1  # centered on bed_y/2
        assert min(zs) >= -0.01  # z_min at ~0

    def test_floating_cube_drops_to_z0(self, tmp_path):
        from pathlib import Path

        from kiln.generation.validation import _parse_stl, center_on_bed

        f = str(tmp_path / "floating.stl")
        _write_offset_cube_stl(f, 10.0, 0.0, 0.0, 30.0)  # z starts at 30
        out = str(tmp_path / "grounded.stl")
        result = center_on_bed(f, output_path=out)

        assert result["translation_mm"]["z"] == -30.0
        errors = []
        _, verts = _parse_stl(Path(out), errors)
        assert min(v[2] for v in verts) >= -0.01

    def test_already_centered_returns_flag(self, tmp_path):
        from kiln.generation.validation import center_on_bed

        f = str(tmp_path / "centered.stl")
        # Write cube at bed center: bed 256x256, cube 20x20 centered at (128, 128, 0)
        _write_offset_cube_stl(f, 20.0, 118.0, 118.0, 0.0)
        result = center_on_bed(f, bed_x_mm=256.0, bed_y_mm=256.0)

        assert result["already_centered"] is True

    def test_custom_bed_size(self, tmp_path):
        from kiln.generation.validation import center_on_bed

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = center_on_bed(f, bed_x_mm=300.0, bed_y_mm=300.0, output_path=str(tmp_path / "c.stl"))

        assert result["new_center_mm"]["x"] == 150.0
        assert result["new_center_mm"]["y"] == 150.0

    def test_preserves_triangle_count(self, tmp_path):
        from pathlib import Path

        from kiln.generation.validation import _parse_stl, center_on_bed

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        out = str(tmp_path / "c.stl")
        center_on_bed(f, output_path=out)

        errors = []
        tris, _ = _parse_stl(Path(out), errors)
        assert len(tris) == 12

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import center_on_bed

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"nope")
        with pytest.raises(ValueError):
            center_on_bed(f)


class TestNonManifoldEdges:
    """Tests for count_non_manifold_edges() — boundary/manifold/T-junction classification."""

    def test_cube_mostly_manifold(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = count_non_manifold_edges(f)

        assert result["total_edges"] > 0
        assert result["manifold_edges"] >= 0
        assert result["boundary_edges"] >= 0
        assert result["t_junction_edges"] >= 0
        assert result["total_edges"] == (
            result["manifold_edges"] + result["boundary_edges"] + result["t_junction_edges"]
        )

    def test_manifold_pct_in_range(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = count_non_manifold_edges(f)

        assert 0 <= result["manifold_pct"] <= 100

    def test_two_component_has_more_edges(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        single = str(tmp_path / "single.stl")
        _write_cube_stl(single, 20.0)
        multi = str(tmp_path / "multi.stl")
        _write_two_component_stl(multi)

        r_single = count_non_manifold_edges(single)
        r_multi = count_non_manifold_edges(multi)

        assert r_multi["total_edges"] > r_single["total_edges"]

    def test_is_watertight_field(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = count_non_manifold_edges(f)

        assert isinstance(result["is_watertight"], bool)
        assert result["is_watertight"] == (result["non_manifold_edges"] == 0)

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"garbage")
        with pytest.raises(ValueError):
            count_non_manifold_edges(f)

    def test_non_manifold_sum(self, tmp_path):
        from kiln.generation.validation import count_non_manifold_edges

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = count_non_manifold_edges(f)

        assert result["non_manifold_edges"] == result["boundary_edges"] + result["t_junction_edges"]


# ===========================================================================
# Phase 6 Tests: Scale-to-Fit, Merge, Split, Print Time Estimation
# ===========================================================================


class TestScaleToFit:
    """Tests for scale_to_fit() — auto-scale mesh to fit build volume."""

    def test_oversized_cube_scaled_down(self, tmp_path):
        from kiln.generation.validation import scale_to_fit

        f = str(tmp_path / "big.stl")
        _write_cube_stl(f, 300.0)
        out = str(tmp_path / "fitted.stl")
        result = scale_to_fit(f, max_x_mm=200.0, max_y_mm=200.0, max_z_mm=200.0, output_path=out)

        assert result["scale_factor"] < 1.0
        assert result["new_dimensions"]["x"] <= 200.1
        assert result["new_dimensions"]["y"] <= 200.1
        assert result["new_dimensions"]["z"] <= 200.1
        assert os.path.isfile(out)

    def test_small_cube_already_fits(self, tmp_path):
        from kiln.generation.validation import scale_to_fit

        f = str(tmp_path / "small.stl")
        _write_cube_stl(f, 20.0)
        out = str(tmp_path / "same.stl")
        result = scale_to_fit(f, max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0, output_path=out)

        assert result["already_fits"] is True
        assert result["scale_factor"] == 1.0

    def test_maintains_aspect_ratio(self, tmp_path):
        from pathlib import Path

        from kiln.generation.validation import _parse_stl, scale_to_fit

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 100.0)
        out = str(tmp_path / "scaled.stl")
        scale_to_fit(f, max_x_mm=50.0, max_y_mm=50.0, max_z_mm=50.0, output_path=out)

        errors: list[str] = []
        _, verts = _parse_stl(Path(out), errors)
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        w = max(xs) - min(xs)
        d = max(ys) - min(ys)
        h = max(zs) - min(zs)
        assert abs(w - d) < 0.1
        assert abs(w - h) < 0.1

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import scale_to_fit

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"nope")
        with pytest.raises(ValueError):
            scale_to_fit(f)

    def test_negative_volume_raises(self, tmp_path):
        from kiln.generation.validation import scale_to_fit

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        with pytest.raises(ValueError, match="positive"):
            scale_to_fit(f, max_x_mm=-1.0)


class TestMergeSTLFiles:
    """Tests for merge_stl_files() — combine multiple STLs."""

    def test_merge_two_cubes(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        f1 = str(tmp_path / "c1.stl")
        f2 = str(tmp_path / "c2.stl")
        _write_cube_stl(f1, 10.0)
        _write_cube_stl(f2, 20.0)
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([f1, f2], output_path=out)

        assert result["file_count"] == 2
        assert result["total_triangles"] == 24
        assert os.path.isfile(out)

    def test_merge_single_file(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 10.0)
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([f], output_path=out)
        assert result["total_triangles"] == 12

    def test_empty_list_raises(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        with pytest.raises(ValueError, match="empty"):
            merge_stl_files([], output_path=str(tmp_path / "out.stl"))

    def test_missing_file_raises(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        with pytest.raises(ValueError, match="not found"):
            merge_stl_files(["/nonexistent.stl"], output_path=str(tmp_path / "out.stl"))


class TestSplitByComponent:
    """Tests for split_by_component() — split multi-body mesh."""

    def test_single_component_one_file(self, tmp_path):
        from kiln.generation.validation import split_by_component

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = split_by_component(f, output_dir=str(tmp_path / "parts"))

        assert result["component_count"] >= 1
        assert len(result["file_paths"]) >= 1
        for p in result["file_paths"]:
            assert os.path.isfile(p)

    def test_two_components_two_files(self, tmp_path):
        from kiln.generation.validation import split_by_component

        f = str(tmp_path / "multi.stl")
        _write_two_component_stl(f)
        result = split_by_component(f, output_dir=str(tmp_path / "split"))

        assert result["component_count"] == 2
        assert len(result["file_paths"]) == 2
        # Largest component first
        assert result["triangles_per_component"][0] >= result["triangles_per_component"][1]

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import split_by_component

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"bad")
        with pytest.raises(ValueError):
            split_by_component(f)


class TestPrintTimeEstimate:
    """Tests for estimate_print_time_from_mesh() — rough time estimate."""

    def test_cube_time_positive(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        result = estimate_print_time_from_mesh(f)

        assert result["estimated_time_seconds"] > 0
        assert result["estimated_time_human"]
        assert result["layers"] > 0
        assert result["material"] == "pla"

    def test_bigger_takes_longer(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        small = str(tmp_path / "small.stl")
        big = str(tmp_path / "big.stl")
        _write_cube_stl(small, 10.0)
        _write_cube_stl(big, 50.0)

        r_small = estimate_print_time_from_mesh(small)
        r_big = estimate_print_time_from_mesh(big)

        assert r_big["estimated_time_seconds"] > r_small["estimated_time_seconds"]

    def test_slower_speed_takes_longer(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        fast = estimate_print_time_from_mesh(f, print_speed_mm_s=100.0)
        slow = estimate_print_time_from_mesh(f, print_speed_mm_s=30.0)

        assert slow["estimated_time_seconds"] > fast["estimated_time_seconds"]

    def test_high_temp_material_overhead(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)

        pla = estimate_print_time_from_mesh(f, material="pla")
        abs_ = estimate_print_time_from_mesh(f, material="abs")

        # ABS has higher per-layer overhead
        assert abs_["estimated_time_seconds"] > pla["estimated_time_seconds"]

    def test_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        f = str(tmp_path / "bad.stl")
        (tmp_path / "bad.stl").write_bytes(b"garbage")
        with pytest.raises(ValueError):
            estimate_print_time_from_mesh(f)

    def test_zero_layer_height_raises(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        f = str(tmp_path / "cube.stl")
        _write_cube_stl(f, 20.0)
        with pytest.raises(ValueError, match="positive"):
            estimate_print_time_from_mesh(f, layer_height_mm=0)


# ===========================================================================
# Cost Estimate Helper Tests
# ===========================================================================


class TestEstimatePrintCost:
    """Tests for material cost estimation integration."""

    def test_cost_increases_with_size(self, tmp_path):
        from kiln.generation.validation import estimate_material_cost

        small = str(tmp_path / "small.stl")
        big = str(tmp_path / "big.stl")
        _write_cube_stl(small, 10.0)
        _write_cube_stl(big, 40.0)

        r_small = estimate_material_cost(small)
        r_big = estimate_material_cost(big)

        assert r_big["estimated_cost_usd"] > r_small["estimated_cost_usd"]


# ---------------------------------------------------------------------------
# Phase 7: 3MF model extraction tests
# ---------------------------------------------------------------------------


def _write_3mf_with_cube(path: str, size: float) -> None:
    """Write a valid 3MF file containing a cube mesh."""
    import zipfile as _zf

    half = size / 2.0
    verts = [
        (-half, -half, -half), (half, -half, -half),
        (half, half, -half), (-half, half, -half),
        (-half, -half, half), (half, -half, half),
        (half, half, half), (-half, half, half),
    ]
    tris = [
        (0, 1, 2), (0, 2, 3),  # bottom
        (4, 6, 5), (4, 7, 6),  # top
        (0, 4, 5), (0, 5, 1),  # front
        (2, 6, 7), (2, 7, 3),  # back
        (0, 3, 7), (0, 7, 4),  # left
        (1, 5, 6), (1, 6, 2),  # right
    ]
    vert_lines = "\n".join(
        f'        <vertex x="{v[0]}" y="{v[1]}" z="{v[2]}" />'
        for v in verts
    )
    tri_lines = "\n".join(
        f'        <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}" />'
        for t in tris
    )
    model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
{vert_lines}
        </vertices>
        <triangles>
{tri_lines}
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1" />
  </build>
</model>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" />
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
                 Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />
</Relationships>"""
    with _zf.ZipFile(path, "w", _zf.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model_xml)


class TestExtractModelFrom3MF:
    """Tests for 3MF → STL extraction.

    Covers:
    - Round-trip: STL → 3MF → extracted STL preserves geometry
    - Direct 3MF extraction with correct triangle/vertex counts
    - .gcode.3mf compound extension handling
    - Multi-object 3MF merging
    - Custom output path
    - Missing model file raises ValueError
    - Non-ZIP file raises ValueError
    - File not found raises FileNotFoundError
    - Extracted STL is valid binary STL
    """

    def test_basic_extraction(self, tmp_path):
        """Extract a cube 3MF and verify triangle/vertex counts."""
        from kiln.generation.validation import extract_model_from_3mf

        threemf = str(tmp_path / "cube.3mf")
        _write_3mf_with_cube(threemf, 20.0)

        result = extract_model_from_3mf(threemf)

        assert result["triangle_count"] == 12  # cube = 12 tris
        assert result["vertex_count"] == 8
        assert os.path.exists(result["output_path"])
        assert result["output_path"].endswith(".stl")

    def test_extracted_stl_is_valid_binary(self, tmp_path):
        """Verify the extracted STL can be re-parsed by validate_mesh."""
        from kiln.generation.validation import extract_model_from_3mf

        threemf = str(tmp_path / "cube.3mf")
        _write_3mf_with_cube(threemf, 20.0)

        result = extract_model_from_3mf(threemf)
        out = result["output_path"]

        # Parse the output STL — should be a valid binary STL.
        vr = validate_mesh(out)
        assert vr.valid
        assert vr.triangle_count == 12

    def test_dimensions_match_cube_size(self, tmp_path):
        """Extracted dimensions should match the cube size."""
        from kiln.generation.validation import extract_model_from_3mf

        threemf = str(tmp_path / "cube.3mf")
        _write_3mf_with_cube(threemf, 30.0)

        result = extract_model_from_3mf(threemf)
        dims = result["dimensions"]

        assert abs(dims["x_mm"] - 30.0) < 0.01
        assert abs(dims["y_mm"] - 30.0) < 0.01
        assert abs(dims["z_mm"] - 30.0) < 0.01

    def test_roundtrip_stl_to_3mf_to_stl(self, tmp_path):
        """STL → export_3mf → extract back → same triangle count."""
        from kiln.generation.validation import (
            export_3mf,
            extract_model_from_3mf,
        )

        # Create original STL cube.
        stl_orig = str(tmp_path / "orig.stl")
        _write_cube_stl(stl_orig, 20.0)

        # Export to 3MF.
        threemf = export_3mf(stl_orig, output_path=str(tmp_path / "rt.3mf"))

        # Extract back.
        result = extract_model_from_3mf(threemf, output_path=str(tmp_path / "extracted.stl"))

        assert result["triangle_count"] == 12  # cube = 12 tris
        assert os.path.exists(result["output_path"])

        # Validate the extracted STL.
        vr = validate_mesh(result["output_path"])
        assert vr.valid

    def test_gcode_3mf_extension_handling(self, tmp_path):
        """Files named .gcode.3mf should strip .gcode from the output stem."""
        from kiln.generation.validation import extract_model_from_3mf

        # Create a .gcode.3mf (same format, different extension).
        gcode_3mf = str(tmp_path / "model.gcode.3mf")
        _write_3mf_with_cube(gcode_3mf, 10.0)

        result = extract_model_from_3mf(gcode_3mf)

        # Should produce "model.stl", not "model.gcode.stl".
        assert result["output_path"].endswith("model.stl")
        assert ".gcode." not in os.path.basename(result["output_path"])
        assert os.path.exists(result["output_path"])

    def test_custom_output_path(self, tmp_path):
        """Custom output_path is respected."""
        from kiln.generation.validation import extract_model_from_3mf

        threemf = str(tmp_path / "cube.3mf")
        _write_3mf_with_cube(threemf, 15.0)
        custom_out = str(tmp_path / "custom_output.stl")

        result = extract_model_from_3mf(threemf, output_path=custom_out)
        assert result["output_path"] == custom_out
        assert os.path.exists(custom_out)

    def test_multi_object_3mf(self, tmp_path):
        """3MF with two mesh objects should merge them into one STL."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        # Build a 3MF with two separate cube objects.
        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="10" y="0" z="0" />
          <vertex x="0" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="2" type="model">
      <mesh>
        <vertices>
          <vertex x="20" y="0" z="0" />
          <vertex x="30" y="0" z="0" />
          <vertex x="20" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1" />
    <item objectid="2" />
  </build>
</model>"""

        threemf = str(tmp_path / "multi.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')

        result = extract_model_from_3mf(threemf)

        # Two objects × 1 triangle each = 2 triangles total.
        assert result["triangle_count"] == 2
        assert result["vertex_count"] == 6

    def test_no_model_file_raises(self, tmp_path):
        """3MF ZIP with no .model file should raise ValueError."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        bad_3mf = str(tmp_path / "empty.3mf")
        with _zf.ZipFile(bad_3mf, "w") as zf:
            zf.writestr("readme.txt", "not a model")

        with pytest.raises(ValueError, match="No 3D model found"):
            extract_model_from_3mf(bad_3mf)

    def test_not_a_zip_raises(self, tmp_path):
        """Non-ZIP file should raise ValueError."""
        from kiln.generation.validation import extract_model_from_3mf

        bad = str(tmp_path / "notzip.3mf")
        with open(bad, "w") as fh:
            fh.write("this is not a zip file")

        with pytest.raises(ValueError, match="Not a valid ZIP"):
            extract_model_from_3mf(bad)

    def test_file_not_found_raises(self, tmp_path):
        """Missing file should raise FileNotFoundError."""
        from kiln.generation.validation import extract_model_from_3mf

        with pytest.raises(FileNotFoundError):
            extract_model_from_3mf(str(tmp_path / "nope.3mf"))

    def test_empty_mesh_raises(self, tmp_path):
        """3MF with object but no triangles should raise ValueError."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices></vertices>
        <triangles></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

        threemf = str(tmp_path / "empty_mesh.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')

        with pytest.raises(ValueError, match="no mesh geometry"):
            extract_model_from_3mf(threemf)

    def test_roundtrip_volume_preserved(self, tmp_path):
        """Round-trip STL → 3MF → STL preserves mesh volume exactly."""
        from kiln.generation.validation import (
            analyze_mesh,
            export_3mf,
            extract_model_from_3mf,
        )

        stl_orig = str(tmp_path / "orig.stl")
        _write_cube_stl(stl_orig, 25.0)
        vol_orig = analyze_mesh(stl_orig).volume_mm3

        threemf = export_3mf(stl_orig, output_path=str(tmp_path / "rt.3mf"))
        result = extract_model_from_3mf(threemf, output_path=str(tmp_path / "rt.stl"))
        vol_extracted = analyze_mesh(result["output_path"]).volume_mm3

        # Volume must match within rounding tolerance.
        assert abs(vol_orig - vol_extracted) < 0.1, (
            f"Volume changed: {vol_orig} → {vol_extracted}"
        )

    def test_invalid_triangle_indices_skipped(self, tmp_path):
        """Triangles referencing out-of-bounds vertices are silently skipped."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="10" y="0" z="0" />
          <vertex x="0" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="0" v2="1" v3="99" />
          <triangle v1="-1" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

        threemf = str(tmp_path / "bad_idx.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')

        result = extract_model_from_3mf(threemf)
        # Only the first triangle (valid indices) should survive.
        assert result["triangle_count"] == 1

    def test_case_insensitive_model_path(self, tmp_path):
        """3MF with lowercase '3d/' directory should still be found."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="5" y="0" z="0" />
          <vertex x="0" y="5" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

        threemf = str(tmp_path / "lower.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            # Use lowercase path
            zf.writestr("3d/3dmodel.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')

        result = extract_model_from_3mf(threemf)
        assert result["triangle_count"] == 1

    def test_nonstandard_model_path_fallback(self, tmp_path):
        """3MF with .model file at a non-standard path should still be found."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="8" y="0" z="0" />
          <vertex x="0" y="8" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

        threemf = str(tmp_path / "custom_path.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            # Non-standard path — broader .model search should find it.
            zf.writestr("Models/custom.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')

        result = extract_model_from_3mf(threemf)
        assert result["triangle_count"] == 1

    def test_component_only_object_skipped(self, tmp_path):
        """Objects with only components (no mesh) should be skipped gracefully."""
        import zipfile as _zf

        from kiln.generation.validation import extract_model_from_3mf

        # Object 1 has components only (no mesh), object 2 has actual mesh.
        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <components>
        <component objectid="2" />
      </components>
    </object>
    <object id="2" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="10" y="0" z="0" />
          <vertex x="0" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

        threemf = str(tmp_path / "components.3mf")
        with _zf.ZipFile(threemf, "w") as zf:
            zf.writestr("3D/3dmodel.model", model_xml)
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')

        result = extract_model_from_3mf(threemf)
        # Should extract only from object 2 (the one with mesh).
        assert result["triangle_count"] == 1
        assert result["vertex_count"] == 3


# ---------------------------------------------------------------------------
# Geometry helpers for non-ideal mesh tests
# ---------------------------------------------------------------------------


def _write_thin_wall_stl(path: str, width: float, height: float, thickness: float) -> None:
    """Write a thin rectangular slab (wall) as binary STL.

    width along X, thickness along Y, height along Z.
    """
    w, t, h = width, thickness, height
    tris = [
        # Front (y=0)
        ((0, 0, 0), (w, 0, h), (w, 0, 0)),
        ((0, 0, 0), (0, 0, h), (w, 0, h)),
        # Back (y=t)
        ((0, t, 0), (w, t, 0), (w, t, h)),
        ((0, t, 0), (w, t, h), (0, t, h)),
        # Bottom (z=0)
        ((0, 0, 0), (w, 0, 0), (w, t, 0)),
        ((0, 0, 0), (w, t, 0), (0, t, 0)),
        # Top (z=h)
        ((0, 0, h), (w, t, h), (w, 0, h)),
        ((0, 0, h), (0, t, h), (w, t, h)),
        # Left (x=0)
        ((0, 0, 0), (0, t, 0), (0, t, h)),
        ((0, 0, 0), (0, t, h), (0, 0, h)),
        # Right (x=w)
        ((w, 0, 0), (w, 0, h), (w, t, h)),
        ((w, 0, 0), (w, t, h), (w, t, 0)),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))


def _write_single_triangle_stl(path: str) -> None:
    """Write a degenerate STL with a single open triangle (non-manifold)."""
    tris = [((0, 0, 0), (10, 0, 0), (5, 10, 0))]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", 1))
        fh.write(struct.pack("<3f", 0, 0, 0))
        for v in tris[0]:
            fh.write(struct.pack("<3f", *v))
        fh.write(struct.pack("<H", 0))


# ---------------------------------------------------------------------------
# Audit-driven edge case tests
# ---------------------------------------------------------------------------


class TestFailurePredictionEdgeCases:
    """Tests that failure prediction actually detects problems.

    Covers:
    - Thin wall detection with geometry below threshold
    - Top-heavy model detection
    - Risk score is non-zero for problematic geometry
    """

    def test_thin_wall_detected(self, tmp_path):
        """A 0.3mm thick wall should trigger thin-wall failure prediction."""
        from kiln.generation.validation import predict_print_failures

        stl = str(tmp_path / "thin.stl")
        _write_thin_wall_stl(stl, width=20.0, height=30.0, thickness=0.3)

        result = predict_print_failures(stl, min_wall_mm=0.8)
        failures = result.get("failures", [])
        types = [f["type"] for f in failures]

        assert "thin_walls" in types, f"Expected thin_walls detection, got: {types}"
        assert result["risk_score"] > 0

    def test_tall_thin_model_high_risk(self, tmp_path):
        """A very tall thin slab should score high risk (top-heavy)."""
        from kiln.generation.validation import predict_print_failures

        stl = str(tmp_path / "tall.stl")
        _write_thin_wall_stl(stl, width=5.0, height=100.0, thickness=2.0)

        result = predict_print_failures(stl)
        assert result["risk_score"] >= 20, (
            f"Tall thin model should have elevated risk, got {result['risk_score']}"
        )

    def test_cube_low_risk(self, tmp_path):
        """A standard cube should have low risk score."""
        from kiln.generation.validation import predict_print_failures

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        result = predict_print_failures(stl)
        assert result["risk_score"] < 50


class TestScorecardGrades:
    """Tests that scorecard assigns meaningful grades.

    Covers:
    - Clean cube gets A or B
    - Single triangle (non-manifold, zero volume) gets D or F
    """

    def test_cube_gets_good_grade(self, tmp_path):
        """A well-formed cube should get A or B."""
        from kiln.generation.validation import design_scorecard

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)
        result = design_scorecard(stl)

        assert result["grade"] in ("A", "B"), f"Cube should be A/B, got {result['grade']}"
        assert result["overall_score"] >= 60

    def test_single_triangle_scores_lower_than_cube(self, tmp_path):
        """A single open triangle should score strictly lower than a cube."""
        from kiln.generation.validation import design_scorecard

        cube_stl = str(tmp_path / "cube.stl")
        _write_cube_stl(cube_stl, 20.0)
        cube_score = design_scorecard(cube_stl)["overall_score"]

        single_stl = str(tmp_path / "single.stl")
        _write_single_triangle_stl(single_stl)
        single_score = design_scorecard(single_stl)["overall_score"]

        assert single_score < cube_score, (
            f"Single triangle ({single_score}) should score lower than cube ({cube_score})"
        )


class TestRepairErrorPaths:
    """Tests for repair function error paths.

    Covers:
    - repair_stl raises on unparseable file
    - export_3mf raises on empty geometry
    """

    def test_repair_bad_file_raises(self, tmp_path):
        from kiln.generation.validation import repair_stl

        bad = str(tmp_path / "garbage.stl")
        with open(bad, "wb") as fh:
            fh.write(b"not a valid stl at all")

        with pytest.raises(ValueError, match="Failed to parse"):
            repair_stl(bad)

    def test_export_3mf_empty_geometry_raises(self, tmp_path):
        from kiln.generation.validation import export_3mf

        # Create a file that parses as STL but has 0 triangles.
        empty = str(tmp_path / "empty.stl")
        with open(empty, "wb") as fh:
            fh.write(b"\x00" * 80)
            fh.write(struct.pack("<I", 0))

        with pytest.raises(ValueError, match="no geometry"):
            export_3mf(empty)


class TestHollowMeshValidation:
    """Tests for hollow_mesh input validation.

    Covers:
    - Zero wall thickness raises
    - Negative wall thickness raises
    """

    def test_zero_wall_raises(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        with pytest.raises(ValueError, match="must be positive"):
            hollow_mesh(stl, wall_thickness_mm=0)

    def test_negative_wall_raises(self, tmp_path):
        from kiln.generation.validation import hollow_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        with pytest.raises(ValueError, match="must be positive"):
            hollow_mesh(stl, wall_thickness_mm=-1.0)


class TestRescaleEdgeCases:
    """Tests for rescale_stl boundary conditions."""

    def test_zero_height_raises(self, tmp_path):
        """A flat (zero-height) model should raise ValueError."""
        from kiln.generation.validation import rescale_stl

        flat = str(tmp_path / "flat.stl")
        # A flat triangle at z=0
        _write_single_triangle_stl(flat)

        with pytest.raises(ValueError, match="near-zero height"):
            rescale_stl(flat, target_height_mm=50.0)


class TestSimplifyMeshClamping:
    """Tests for simplify_mesh target_ratio clamping."""

    def test_zero_ratio_clamps_to_min(self, tmp_path):
        """target_ratio=0 should be clamped, not crash."""
        from kiln.generation.validation import simplify_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        result = simplify_mesh(stl, target_ratio=0.0, output_path=str(tmp_path / "out.stl"))
        # Should not crash, and should produce fewer triangles.
        assert result["simplified_triangles"] <= result["original_triangles"]

    def test_ratio_above_one_clamps(self, tmp_path):
        """target_ratio > 1.0 should be clamped to 1.0 (no simplification)."""
        from kiln.generation.validation import simplify_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        result = simplify_mesh(stl, target_ratio=5.0, output_path=str(tmp_path / "out.stl"))
        assert result["simplified_triangles"] == result["original_triangles"]


class TestEstimateMaterialCostEdgeCases:
    """Tests for material cost estimation edge cases."""

    def test_single_triangle_zero_volume(self, tmp_path):
        """A flat triangle has zero volume — should raise ValueError."""
        from kiln.generation.validation import estimate_material_cost

        stl = str(tmp_path / "flat.stl")
        _write_single_triangle_stl(stl)

        with pytest.raises(ValueError, match="no volume"):
            estimate_material_cost(stl)


class TestNonManifoldDetection:
    """Tests for manifold detection with known non-manifold geometry."""

    def test_single_triangle_is_non_manifold(self, tmp_path):
        """A single open triangle should be detected as non-manifold."""
        from kiln.generation.validation import analyze_mesh

        stl = str(tmp_path / "open.stl")
        _write_single_triangle_stl(stl)

        result = analyze_mesh(stl)
        assert result.is_manifold is False

    def test_cube_is_manifold(self, tmp_path):
        """A closed cube should be detected as manifold."""
        from kiln.generation.validation import analyze_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 15.0)

        result = analyze_mesh(stl)
        assert result.is_manifold is True


class TestMergeSTLEdgeCases:
    """Tests for merge_stl_files edge cases."""

    def test_empty_output_path_raises(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        stl = str(tmp_path / "a.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="output_path"):
            merge_stl_files([stl], output_path="")

    def test_no_files_raises(self, tmp_path):
        from kiln.generation.validation import merge_stl_files

        with pytest.raises(ValueError, match="must not be empty"):
            merge_stl_files([], output_path=str(tmp_path / "out.stl"))


class TestEstimatePrintTimeEdgeCases:
    """Tests for print time estimation edge cases."""

    def test_zero_speed_raises(self, tmp_path):
        from kiln.generation.validation import estimate_print_time_from_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 20.0)

        with pytest.raises(ValueError, match="[Ss]peed"):
            estimate_print_time_from_mesh(stl, print_speed_mm_s=0.0)


# ---------------------------------------------------------------------------
# Geometry-level mesh repair: thicken, fillet, chamfer
# ---------------------------------------------------------------------------


class TestThickenWalls:
    """Tests for thicken_walls() — geometry-level thin-wall fix."""

    def test_basic_thickening(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "thickened.stl")

        result = thicken_walls(stl, amount_mm=0.5, output_path=out)

        assert result["path"] == out
        assert result["amount_mm"] == 0.5
        assert result["triangle_count"] == 12
        assert os.path.isfile(out)

    def test_thickened_file_is_valid_stl(self, tmp_path):
        from kiln.generation.validation import thicken_walls, validate_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "thickened.stl")

        thicken_walls(stl, amount_mm=0.5, output_path=out)
        val = validate_mesh(out)
        assert val.valid
        assert val.triangle_count == 12

    def test_default_output_path(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "part.stl")
        _write_cube_stl(stl, 10.0)

        result = thicken_walls(stl, amount_mm=0.3)
        assert result["path"].endswith("_thickened.stl")
        assert os.path.isfile(result["path"])

    def test_thin_wall_detection(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "thin.stl")
        # A very thin slab (1mm thick) — should detect thin walls
        _write_thin_wall_stl(stl, width=20.0, height=20.0, thickness=1.0)

        result = thicken_walls(stl, amount_mm=0.5)
        # Should have modified some vertices
        assert result["vertices_modified"] > 0

    def test_zero_amount_raises(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="positive"):
            thicken_walls(stl, amount_mm=0)

    def test_negative_amount_raises(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="positive"):
            thicken_walls(stl, amount_mm=-1.0)

    def test_empty_stl_raises(self, tmp_path):
        from kiln.generation.validation import thicken_walls

        stl = str(tmp_path / "empty.stl")
        with open(stl, "wb") as fh:
            fh.write(b"\x00" * 80)
            fh.write(struct.pack("<I", 0))

        with pytest.raises(ValueError, match="no geometry"):
            thicken_walls(stl, amount_mm=0.5)


class TestAddFillet:
    """Tests for add_fillet() — round sharp edges."""

    def test_basic_fillet(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "filleted.stl")

        result = add_fillet(stl, radius_mm=1.0, output_path=out)

        assert result["path"] == out
        assert result["radius_mm"] == 1.0
        assert result["angle_threshold_deg"] == 60.0
        assert os.path.isfile(out)

    def test_cube_has_sharp_edges(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        result = add_fillet(stl, radius_mm=0.5)
        # A cube has 12 edges, all at 90 degrees — should find sharp edges
        assert result["sharp_edges_found"] > 0
        assert result["fillet_triangles_added"] > 0
        assert result["triangle_count"] > 12  # Original + fillet tris

    def test_filleted_file_is_valid_stl(self, tmp_path):
        from kiln.generation.validation import add_fillet, validate_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "filleted.stl")

        add_fillet(stl, radius_mm=0.5, output_path=out)
        val = validate_mesh(out)
        assert val.valid
        assert val.triangle_count > 12

    def test_default_output_path(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "bracket.stl")
        _write_cube_stl(stl, 10.0)

        result = add_fillet(stl, radius_mm=1.0)
        assert result["path"].endswith("_filleted.stl")

    def test_zero_radius_raises(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="positive"):
            add_fillet(stl, radius_mm=0)

    def test_invalid_angle_raises(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="between 0 and 180"):
            add_fillet(stl, angle_threshold_deg=0)

        with pytest.raises(ValueError, match="between 0 and 180"):
            add_fillet(stl, angle_threshold_deg=180)

    def test_high_threshold_finds_no_edges(self, tmp_path):
        from kiln.generation.validation import add_fillet

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        # 170 degrees — almost flat, should find no sharp edges on a cube
        result = add_fillet(stl, angle_threshold_deg=170.0)
        # Cube edges are at 90 degrees (cos=0), only edges sharper than
        # 170 degrees (cos(170)≈-0.985) would be detected — cube doesn't have those
        assert result["triangle_count"] == 12  # No fillets added


class TestAddChamfer:
    """Tests for add_chamfer() — flat bevel at sharp edges."""

    def test_basic_chamfer(self, tmp_path):
        from kiln.generation.validation import add_chamfer

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "chamfered.stl")

        result = add_chamfer(stl, distance_mm=0.5, output_path=out)

        assert result["path"] == out
        assert result["distance_mm"] == 0.5
        assert result["angle_threshold_deg"] == 60.0
        assert os.path.isfile(out)

    def test_cube_has_sharp_edges(self, tmp_path):
        from kiln.generation.validation import add_chamfer

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        result = add_chamfer(stl, distance_mm=0.3)
        assert result["sharp_edges_found"] > 0
        assert result["chamfer_triangles_added"] > 0
        assert result["triangle_count"] > 12

    def test_chamfered_file_is_valid_stl(self, tmp_path):
        from kiln.generation.validation import add_chamfer, validate_mesh

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)
        out = str(tmp_path / "chamfered.stl")

        add_chamfer(stl, distance_mm=0.5, output_path=out)
        val = validate_mesh(out)
        assert val.valid
        assert val.triangle_count > 12

    def test_default_output_path(self, tmp_path):
        from kiln.generation.validation import add_chamfer

        stl = str(tmp_path / "part.stl")
        _write_cube_stl(stl, 10.0)

        result = add_chamfer(stl, distance_mm=0.5)
        assert result["path"].endswith("_chamfered.stl")

    def test_zero_distance_raises(self, tmp_path):
        from kiln.generation.validation import add_chamfer

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="positive"):
            add_chamfer(stl, distance_mm=0)

    def test_chamfer_adds_two_tris_per_edge(self, tmp_path):
        from kiln.generation.validation import add_chamfer

        stl = str(tmp_path / "cube.stl")
        _write_cube_stl(stl, 10.0)

        result = add_chamfer(stl, distance_mm=0.3)
        # Each sharp edge gets 2 chamfer triangles
        assert result["chamfer_triangles_added"] == result["sharp_edges_found"] * 2


# ---------------------------------------------------------------------------
# Boolean mesh operations (via OpenSCAD)
# ---------------------------------------------------------------------------


class TestBooleanMeshOperation:
    """Tests for boolean_mesh_operation() — union, difference, intersection."""

    def test_invalid_operation_raises(self, tmp_path):
        from kiln.generation.openscad import boolean_mesh_operation

        stl1 = str(tmp_path / "a.stl")
        stl2 = str(tmp_path / "b.stl")
        _write_cube_stl(stl1, 10.0)
        _write_cube_stl(stl2, 10.0)

        with pytest.raises(ValueError, match="union.*difference.*intersection"):
            boolean_mesh_operation("subtract", [stl1, stl2])

    def test_fewer_than_two_files_raises(self, tmp_path):
        from kiln.generation.openscad import boolean_mesh_operation

        stl = str(tmp_path / "a.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(ValueError, match="at least 2"):
            boolean_mesh_operation("union", [stl])

    def test_missing_file_raises(self, tmp_path):
        from kiln.generation.openscad import boolean_mesh_operation

        stl = str(tmp_path / "a.stl")
        _write_cube_stl(stl, 10.0)

        with pytest.raises(FileNotFoundError):
            boolean_mesh_operation("union", [stl, "/nonexistent.stl"])

    @patch("kiln.generation.openscad._find_openscad", return_value=None)
    def test_no_openscad_raises(self, mock_find, tmp_path):
        from kiln.generation.openscad import boolean_mesh_operation

        stl1 = str(tmp_path / "a.stl")
        stl2 = str(tmp_path / "b.stl")
        _write_cube_stl(stl1, 10.0)
        _write_cube_stl(stl2, 10.0)

        with pytest.raises(GenerationError, match="not found"):
            boolean_mesh_operation("union", [stl1, stl2])

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    def test_provider_boolean_timeout(self, mock_find, tmp_path):
        import subprocess as _sp

        stl1 = str(tmp_path / "a.stl")
        stl2 = str(tmp_path / "b.stl")
        _write_cube_stl(stl1, 10.0)
        _write_cube_stl(stl2, 10.0)

        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("openscad", 120)), \
                pytest.raises(GenerationError, match="timed out"):
            provider.boolean_operation("union", [stl1, stl2])

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    def test_provider_boolean_nonzero_exit(self, mock_find, tmp_path):
        stl1 = str(tmp_path / "a.stl")
        stl2 = str(tmp_path / "b.stl")
        _write_cube_stl(stl1, 10.0)
        _write_cube_stl(stl2, 10.0)

        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error: something went wrong"
        with patch("subprocess.run", return_value=mock_result), \
                pytest.raises(GenerationError, match="failed"):
            provider.boolean_operation("difference", [stl1, stl2])

    @patch("kiln.generation.openscad._find_openscad", return_value="/usr/bin/openscad")
    def test_all_three_operations_accepted(self, mock_find, tmp_path):
        """All three boolean operations should pass validation."""
        from kiln.generation.openscad import OpenSCADProvider

        stl1 = str(tmp_path / "a.stl")
        stl2 = str(tmp_path / "b.stl")
        _write_cube_stl(stl1, 10.0)
        _write_cube_stl(stl2, 5.0)

        provider = OpenSCADProvider(binary_path="/usr/bin/openscad")

        # All three should pass argument validation (may fail at OpenSCAD
        # execution, but should NOT raise ValueError)
        for op in ("union", "difference", "intersection"):
            mock_result = MagicMock()
            mock_result.returncode = 0
            out = str(tmp_path / f"{op}_result.stl")
            _write_cube_stl(out, 10.0)

            with patch("subprocess.run", return_value=mock_result):
                result = provider.boolean_operation(op, [stl1, stl2], output_path=out)
            assert result == out
