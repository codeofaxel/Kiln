"""Tests for PIL-based colored mesh renderer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiln.colored_renderer import (
    _apply_brightness,
    _compute_brightness,
    _darken,
    _face_normal,
    render_colored_mesh,
    render_colored_mesh_multi_angle,
)
from kiln.threemf_parser import ColoredTriangle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_colored_box() -> list[ColoredTriangle]:
    """A minimal box with 12 triangles, 6 colors (one per face)."""
    # Unit cube: 8 vertices, 12 triangles, 6 faces
    v = [
        (0.0, 0.0, 0.0),  # 0
        (10.0, 0.0, 0.0),  # 1
        (10.0, 10.0, 0.0),  # 2
        (0.0, 10.0, 0.0),  # 3
        (0.0, 0.0, 10.0),  # 4
        (10.0, 0.0, 10.0),  # 5
        (10.0, 10.0, 10.0),  # 6
        (0.0, 10.0, 10.0),  # 7
    ]
    # 6 face colors (red, green, blue, yellow, cyan, magenta)
    face_colors = [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 0),
        (0, 255, 255),
        (255, 0, 255),
    ]
    # Each face = 2 triangles with same color
    faces = [
        # Bottom (Z=0)
        ((0, 1, 2), (0, 2, 3)),
        # Top (Z=10)
        ((4, 6, 5), (4, 7, 6)),
        # Front (Y=0)
        ((0, 5, 1), (0, 4, 5)),
        # Back (Y=10)
        ((2, 7, 3), (2, 6, 7)),
        # Left (X=0)
        ((0, 7, 4), (0, 3, 7)),
        # Right (X=10)
        ((1, 6, 2), (1, 5, 6)),
    ]
    triangles: list[ColoredTriangle] = []
    for fi, (t1_idx, t2_idx) in enumerate(faces):
        color = face_colors[fi]
        triangles.append(ColoredTriangle(
            v0=v[t1_idx[0]], v1=v[t1_idx[1]], v2=v[t1_idx[2]], color=color,
        ))
        triangles.append(ColoredTriangle(
            v0=v[t2_idx[0]], v1=v[t2_idx[1]], v2=v[t2_idx[2]], color=color,
        ))
    return triangles


def _make_single_triangle() -> list[ColoredTriangle]:
    return [
        ColoredTriangle(
            v0=(0.0, 0.0, 0.0),
            v1=(10.0, 0.0, 0.0),
            v2=(5.0, 10.0, 0.0),
            color=(255, 128, 0),
        ),
    ]


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


class TestMathHelpers:
    """Pure-math vector helper functions."""

    def test_face_normal_unit_length(self) -> None:
        n = _face_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
        assert abs(length - 1.0) < 1e-6

    def test_face_normal_z_up(self) -> None:
        n = _face_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(n[2] - 1.0) < 1e-6 or abs(n[2] + 1.0) < 1e-6

    def test_compute_brightness_range(self) -> None:
        # Should always be between ambient and 1.0
        for nx in (-1, 0, 1):
            for ny in (-1, 0, 1):
                for nz in (-1, 0, 1):
                    b = _compute_brightness((nx, ny, nz), (0.3, -0.6, 0.7))
                    assert 0.0 <= b <= 1.0

    def test_apply_brightness_clamps(self) -> None:
        # Full brightness
        assert _apply_brightness((255, 255, 255), 1.0) == (255, 255, 255)
        # Zero brightness — shadow floor preserves color, never full black
        r, g, b = _apply_brightness((255, 255, 255), 0.0)
        assert r == g == b
        assert r > 0  # shadow floor prevents crush to black
        assert r < 128  # but still visibly dark

    def test_darken(self) -> None:
        assert _darken((200, 100, 50), factor=0.5) == (100, 50, 25)


# ---------------------------------------------------------------------------
# render_colored_mesh
# ---------------------------------------------------------------------------


class TestRenderColoredMesh:
    """Single-angle colored mesh rendering."""

    def test_produces_valid_png(self, tmp_path: Path) -> None:
        out = str(tmp_path / "test.png")
        result = render_colored_mesh(
            _make_colored_box(),
            output_path=out,
            width=400,
            height=300,
            supersample=1,
        )
        assert os.path.isfile(out)
        assert os.path.getsize(out) > 0
        assert result.path == out
        assert result.width == 400
        assert result.height == 300
        assert result.triangle_count == 12
        # Back-face culling hides some faces, so not all 6 colors are visible
        assert result.face_colors_used >= 3

    def test_supersample_downscales(self, tmp_path: Path) -> None:
        out = str(tmp_path / "ss.png")
        result = render_colored_mesh(
            _make_single_triangle(),
            output_path=out,
            width=200,
            height=150,
            supersample=2,
        )
        from PIL import Image

        with Image.open(out) as img:
            assert img.size == (200, 150)
        assert result.face_colors_used == 1

    def test_default_output_path(self) -> None:
        result = render_colored_mesh(
            _make_single_triangle(),
            width=100,
            height=100,
            supersample=1,
        )
        assert os.path.isfile(result.path)
        assert result.path.endswith(".png")
        # Clean up
        os.unlink(result.path)

    def test_empty_triangles_raises(self) -> None:
        with pytest.raises(ValueError, match="No triangles"):
            render_colored_mesh([])

    def test_to_dict(self, tmp_path: Path) -> None:
        out = str(tmp_path / "dict.png")
        result = render_colored_mesh(
            _make_single_triangle(),
            output_path=out,
            supersample=1,
        )
        d = result.to_dict()
        assert d["path"] == out
        assert isinstance(d["width"], int)
        assert isinstance(d["face_colors_used"], int)


# ---------------------------------------------------------------------------
# render_colored_mesh_multi_angle
# ---------------------------------------------------------------------------


class TestRenderMultiAngle:
    """Multi-angle colored mesh rendering."""

    def test_all_six_angles(self, tmp_path: Path) -> None:
        views = render_colored_mesh_multi_angle(
            _make_colored_box(),
            output_dir=str(tmp_path),
            width=200,
            height=150,
            supersample=1,
        )
        assert len(views) == 6
        angles = [v["angle"] for v in views]
        assert "isometric" in angles
        assert "front" in angles
        assert "right" in angles
        assert "top" in angles
        assert "bottom" in angles
        assert "back" in angles

        for v in views:
            assert os.path.isfile(v["path"])
            assert v["description"]

    def test_subset_of_angles(self, tmp_path: Path) -> None:
        views = render_colored_mesh_multi_angle(
            _make_colored_box(),
            output_dir=str(tmp_path),
            angles=["isometric", "top"],
            width=200,
            height=150,
            supersample=1,
        )
        assert len(views) == 2
        # Canonical order preserved (isometric first)
        assert views[0]["angle"] == "isometric"
        assert views[1]["angle"] == "top"
        # Quality scores present as metadata
        assert "quality_score" in views[0]

    def test_unknown_angle_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown camera angles"):
            render_colored_mesh_multi_angle(
                _make_single_triangle(),
                angles=["diagonal"],
            )

    def test_empty_triangles_raises(self) -> None:
        with pytest.raises(ValueError, match="No triangles"):
            render_colored_mesh_multi_angle([])
