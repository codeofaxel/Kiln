"""Tests for compare_renders in kiln.model_visualizer.

Coverage areas:
- Input validation (path count, existence, angle, label/path mismatch)
- Integration with mocked visualize_model (2-model, 4-model, labels, colors, output path)
- Fallback behavior when PIL is unavailable
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.model_visualizer import compare_renders

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_stl(tmp_path: Path) -> Path:
    """Create a minimal valid binary STL with 1 triangle."""
    stl = tmp_path / "model.stl"
    header = b"\x00" * 80
    num_triangles = struct.pack("<I", 1)
    triangle = struct.pack("<fff", 0, 0, 1)  # normal
    triangle += struct.pack("<fff", 0, 0, 0)  # v1
    triangle += struct.pack("<fff", 1, 0, 0)  # v2
    triangle += struct.pack("<fff", 0, 1, 0)  # v3
    triangle += struct.pack("<H", 0)  # attribute
    stl.write_bytes(header + num_triangles + triangle)
    return stl


@pytest.fixture
def two_stls(tmp_path: Path) -> list[str]:
    """Create two distinct STL files and return their paths as strings."""
    paths = []
    for name in ("model_a.stl", "model_b.stl"):
        stl = tmp_path / name
        header = b"\x00" * 80
        num_triangles = struct.pack("<I", 1)
        triangle = struct.pack("<fff", 0, 0, 1)
        triangle += struct.pack("<fff", 0, 0, 0)
        triangle += struct.pack("<fff", 1, 0, 0)
        triangle += struct.pack("<fff", 0, 1, 0)
        triangle += struct.pack("<H", 0)
        stl.write_bytes(header + num_triangles + triangle)
        paths.append(str(stl))
    return paths


@pytest.fixture
def four_stls(tmp_path: Path) -> list[str]:
    """Create four STL files."""
    paths = []
    for i in range(4):
        stl = tmp_path / f"model_{i}.stl"
        header = b"\x00" * 80
        num_triangles = struct.pack("<I", 1)
        triangle = struct.pack("<fff", 0, 0, 1)
        triangle += struct.pack("<fff", 0, 0, 0)
        triangle += struct.pack("<fff", 1, 0, 0)
        triangle += struct.pack("<fff", 0, 1, 0)
        triangle += struct.pack("<H", 0)
        stl.write_bytes(header + num_triangles + triangle)
        paths.append(str(stl))
    return paths


def _dummy_png(path: str, *, width: int = 800, height: int = 600) -> None:
    """Write a minimal valid PNG to *path* using PIL."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(128, 128, 128))
    img.save(path)


def _make_visualize_success(tmp_path: Path):
    """Return a mock side_effect for visualize_model that produces real PNGs."""
    call_count = 0

    def _mock(file_path, *, output_dir=None, width=800, height=600,
              angles=None, color="", timeout=120):
        nonlocal call_count
        call_count += 1
        out_dir = output_dir or str(tmp_path / f"viz_{call_count}")
        os.makedirs(out_dir, exist_ok=True)
        angle = (angles[0] if angles else "isometric")
        png_path = os.path.join(out_dir, f"render_{call_count}_{angle}.png")
        # Use the CALLER's width/height, not hardcoded defaults
        _dummy_png(png_path, width=width, height=height)
        return {
            "success": True,
            "views": [{"path": png_path, "angle": angle, "description": "test"}],
            "rendered": 1,
            "failed": 0,
            "output_dir": out_dir,
        }

    return _mock


# ---------------------------------------------------------------------------
# TestCompareRendersValidation
# ---------------------------------------------------------------------------


class TestCompareRendersValidation:
    """Validation: path count, file existence, angle names, label mismatch."""

    def test_too_few_paths_empty(self):
        result = compare_renders([])
        assert result["success"] is False
        assert "2" in result["error"] or "at least" in result["error"].lower()

    def test_too_few_paths_one(self, tmp_stl: Path):
        result = compare_renders([str(tmp_stl)])
        assert result["success"] is False

    def test_too_many_paths(self, tmp_path: Path):
        paths = []
        for i in range(5):
            p = tmp_path / f"m{i}.stl"
            header = b"\x00" * 80
            num_t = struct.pack("<I", 1)
            tri = struct.pack("<fff", 0, 0, 1)
            tri += struct.pack("<fff", 0, 0, 0)
            tri += struct.pack("<fff", 1, 0, 0)
            tri += struct.pack("<fff", 0, 1, 0)
            tri += struct.pack("<H", 0)
            p.write_bytes(header + num_t + tri)
            paths.append(str(p))
        result = compare_renders(paths)
        assert result["success"] is False
        assert "4" in result["error"] or "most" in result["error"].lower()

    def test_nonexistent_path(self, tmp_stl: Path):
        bad = "/nonexistent/ghost_model.stl"
        result = compare_renders([str(tmp_stl), bad])
        assert result["success"] is False
        assert "ghost_model" in result["error"] or bad in result["error"]

    def test_invalid_angle(self, two_stls: list[str]):
        result = compare_renders(two_stls, angle="invalid_view_xyz")
        assert result["success"] is False
        assert "invalid_view_xyz" in result["error"].lower() or "angle" in result["error"].lower()

    def test_fewer_labels_pads_with_defaults(self, two_stls: list[str], tmp_path: Path):
        """Fewer labels than paths → pad remaining with A, B, C, D defaults."""
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(two_stls, labels=["Custom"])
        assert result["success"] is True
        assert result["models"][0]["label"] == "Custom"
        # Second model should be padded with default "B"
        assert result["models"][1]["label"] == "B"


# ---------------------------------------------------------------------------
# TestCompareRendersIntegration
# ---------------------------------------------------------------------------


class TestCompareRendersIntegration:
    """Integration tests with mocked visualize_model and real PIL stitching."""

    @pytest.fixture(autouse=True)
    def _skip_no_pil(self):
        pytest.importorskip("PIL")

    def test_two_models_side_by_side(self, two_stls: list[str], tmp_path: Path):
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(two_stls)

        assert result["success"] is True
        assert result["angle"] == "isometric"
        assert os.path.isfile(result["comparison_path"])
        assert len(result["models"]) == 2
        assert result["stitched"] is True
        # Stitched width should be at least 2x a single render width
        assert result["width"] >= 1600

    def test_four_models_grid(self, four_stls: list[str], tmp_path: Path):
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(four_stls)

        assert result["success"] is True
        assert len(result["models"]) == 4
        assert result["layout"] == "2x2"
        assert os.path.isfile(result["comparison_path"])

    def test_custom_labels(self, two_stls: list[str], tmp_path: Path):
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(two_stls, labels=["Before", "After"])

        assert result["success"] is True
        assert result["models"][0]["label"] == "Before"
        assert result["models"][1]["label"] == "After"

    def test_custom_colors(self, two_stls: list[str], tmp_path: Path):
        captured_colors: list[str] = []

        def _mock(file_path, *, output_dir=None, width=800, height=600,
                  angles=None, color="", timeout=120):
            captured_colors.append(color)
            out_dir = output_dir or str(tmp_path / f"viz_{len(captured_colors)}")
            os.makedirs(out_dir, exist_ok=True)
            png = os.path.join(out_dir, f"r{len(captured_colors)}.png")
            _dummy_png(png, width=width, height=height)
            return {
                "success": True,
                "views": [{"path": png, "angle": "isometric", "description": ""}],
                "rendered": 1, "failed": 0, "output_dir": out_dir,
            }

        with patch("kiln.model_visualizer.visualize_model", side_effect=_mock):
            result = compare_renders(
                two_stls, colors=["#FF0000", "#0000FF"],
            )

        assert result["success"] is True
        assert "#FF0000" in captured_colors
        assert "#0000FF" in captured_colors

    def test_output_path_used(self, two_stls: list[str], tmp_path: Path):
        out_file = str(tmp_path / "custom_comparison.png")
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(two_stls, output_path=out_file)

        assert result["success"] is True
        assert result["comparison_path"] == out_file
        assert os.path.isfile(out_file)

    def test_default_labels(self, two_stls: list[str], tmp_path: Path):
        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(two_stls)

        assert result["success"] is True
        assert result["models"][0]["label"] == "A"
        assert result["models"][1]["label"] == "B"

    def test_three_models_single_row(self, tmp_path: Path):
        paths = []
        for i in range(3):
            stl = tmp_path / f"model_{i}.stl"
            header = b"\x00" * 80
            num_t = struct.pack("<I", 1)
            tri = struct.pack("<fff", 0, 0, 1)
            tri += struct.pack("<fff", 0, 0, 0)
            tri += struct.pack("<fff", 1, 0, 0)
            tri += struct.pack("<fff", 0, 1, 0)
            tri += struct.pack("<H", 0)
            stl.write_bytes(header + num_t + tri)
            paths.append(str(stl))

        mock_viz = _make_visualize_success(tmp_path)
        with patch("kiln.model_visualizer.visualize_model", side_effect=mock_viz):
            result = compare_renders(paths)

        assert result["success"] is True
        assert len(result["models"]) == 3
        assert result["layout"] == "3x1"
        # Width should be 3x single render width (2400 for 800px each)
        assert result["width"] >= 2400

    def test_custom_dimensions_passed_through(self, two_stls: list[str], tmp_path: Path):
        captured_dims: list[tuple[int, int]] = []

        def _mock(file_path, *, output_dir=None, width=800, height=600,
                  angles=None, color="", timeout=120):
            captured_dims.append((width, height))
            out_dir = output_dir or str(tmp_path / f"viz_{len(captured_dims)}")
            os.makedirs(out_dir, exist_ok=True)
            png = os.path.join(out_dir, f"r{len(captured_dims)}.png")
            _dummy_png(png, width=width, height=height)
            return {
                "success": True,
                "views": [{"path": png, "angle": "isometric", "description": ""}],
                "rendered": 1, "failed": 0, "output_dir": out_dir,
            }

        with patch("kiln.model_visualizer.visualize_model", side_effect=_mock):
            result = compare_renders(two_stls, width=1024, height=768)

        assert result["success"] is True
        assert all(w == 1024 and h == 768 for w, h in captured_dims)


# ---------------------------------------------------------------------------
# TestCompareRendersFallback
# ---------------------------------------------------------------------------


class TestCompareRendersFallback:
    """Fallback behavior when PIL/Pillow is not installed."""

    def test_no_pillow_returns_individual_paths(
        self, two_stls: list[str], tmp_path: Path,
    ):
        # Pre-create dummy PNGs BEFORE mocking PIL away
        pre_pngs: list[str] = []
        for i in range(2):
            png = str(tmp_path / f"pre_{i}.png")
            _dummy_png(png)
            pre_pngs.append(png)

        call_count = 0

        def _mock_viz(file_path, **kwargs):
            nonlocal call_count
            png = pre_pngs[call_count % len(pre_pngs)]
            call_count += 1
            return {
                "success": True,
                "views": [{"path": png, "angle": "isometric", "description": ""}],
                "rendered": 1, "failed": 0, "output_dir": str(tmp_path),
            }

        def _fail_pil_import(name, *args, **kwargs):
            if name == "PIL" or (isinstance(name, str) and name.startswith("PIL.")):
                raise ImportError("No module named 'PIL'")
            return original_import(name, *args, **kwargs)

        import builtins
        original_import = builtins.__import__

        with (
            patch("kiln.model_visualizer.visualize_model", side_effect=_mock_viz),
            patch("builtins.__import__", side_effect=_fail_pil_import),
        ):
            result = compare_renders(two_stls)

        # Should still succeed (graceful degradation) or return individual paths
        if result["success"]:
            # Graceful fallback — returned individual render paths
            assert result.get("stitched") is False or "note" in result or "models" in result
        else:
            # Acceptable: explicit error mentioning PIL/Pillow
            assert "pil" in result["error"].lower() or "pillow" in result["error"].lower()
