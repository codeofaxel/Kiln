"""Tests for kiln.emboss_generator module."""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Tests: MATERIAL_DEPTHS and get_default_depth
# ---------------------------------------------------------------------------

class TestMaterialDepths:
    def test_expected_keys(self):
        from kiln.emboss_generator import MATERIAL_DEPTHS

        for key in ("PLA", "PETG", "ABS", "TPU", "Nylon", "Resin"):
            assert key in MATERIAL_DEPTHS, f"Missing material: {key}"

    def test_values_are_positive_floats(self):
        from kiln.emboss_generator import MATERIAL_DEPTHS

        for _mat, depth in MATERIAL_DEPTHS.items():
            assert isinstance(depth, float)
            assert depth > 0


class TestGetDefaultDepth:
    def test_known_materials(self):
        from kiln.emboss_generator import get_default_depth

        assert get_default_depth("PLA") == 0.6
        assert get_default_depth("TPU") == 1.2
        assert get_default_depth("Resin") == 0.3

    def test_unknown_material_fallback(self):
        from kiln.emboss_generator import get_default_depth

        assert get_default_depth("UnknownMaterial") == 0.8


# ---------------------------------------------------------------------------
# Tests: generate_emboss_scad
# ---------------------------------------------------------------------------

def _make_face(face_name: str = "top") -> dict:
    """Return a minimal face dict suitable for generate_emboss_scad."""
    return {
        "normal": [0.0, 0.0, 1.0],
        "center": [5.0, 5.0, 10.0],
        "width_mm": 10.0,
        "height_mm": 10.0,
        "face_name": face_name,
    }


class TestGenerateEmbossScad:
    def test_svg_content(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        # Create a dummy SVG file for the path reference
        svg_file = tmp_path / "logo.svg"
        svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

        content_info = {
            "type": "svg",
            "svg_path": str(svg_file),
            "width": 100,
            "height": 100,
            "aspect_ratio": 1.0,
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
        )

        assert "scad_path" in result
        assert os.path.isfile(result["scad_path"])

        with open(result["scad_path"]) as f:
            scad_code = f.read()
        assert "difference()" in scad_code  # default mode is deboss
        assert "import(" in scad_code
        assert "linear_extrude" in scad_code

    def test_text_content(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        content_info = {
            "type": "openscad_text",
            "text": "KILN",
            "font_size": 12,
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "model.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
            mode="emboss",
        )

        assert os.path.isfile(result["scad_path"])

        with open(result["scad_path"]) as f:
            scad_code = f.read()
        assert "union()" in scad_code  # emboss mode
        assert "text(" in scad_code
        assert "KILN" in scad_code

    def test_invalid_mode_raises(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        with pytest.raises(ValueError, match="mode must be"):
            generate_emboss_scad(
                model_path=str(tmp_path / "model.stl"),
                content_info={"type": "svg", "svg_path": "/tmp/x.svg"},
                face=_make_face(),
                output_dir=str(tmp_path / "out"),
                mode="invalid",
            )

    def test_invalid_content_type_raises(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        with pytest.raises(ValueError, match="content_info"):
            generate_emboss_scad(
                model_path=str(tmp_path / "model.stl"),
                content_info={"type": "unsupported"},
                face=_make_face(),
                output_dir=str(tmp_path / "out"),
            )

    def test_output_paths(self, tmp_path):
        from kiln.emboss_generator import generate_emboss_scad

        content_info = {
            "type": "openscad_text",
            "text": "test",
        }

        result = generate_emboss_scad(
            model_path=str(tmp_path / "widget.stl"),
            content_info=content_info,
            face=_make_face(),
            output_dir=str(tmp_path / "out"),
        )

        assert result["scad_path"].endswith(".scad")
        assert result["output_stl_path"].endswith(".stl")
        assert "openscad" in result["openscad_command"]
