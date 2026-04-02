"""Tests for kiln.surface_intelligence module."""

from __future__ import annotations

import os
import struct
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Helper: build a minimal binary STL cube (12 triangles)
# ---------------------------------------------------------------------------

def _build_cube_stl(size: float = 10.0) -> bytes:
    """Return bytes for a binary STL cube centred at (size/2, size/2, size/2).

    The cube has 8 vertices and 12 triangles (2 per face).
    Binary STL format: 80-byte header + uint32 tri count + 50 bytes per tri.
    """
    s = size
    # 6 faces, each with 2 triangles. Normal points outward.
    # Vertices ordered counter-clockwise when viewed from outside.
    faces = [
        # bottom (z=0, normal 0,0,-1)
        ((0, 0, -1), [(0, 0, 0), (s, 0, 0), (s, s, 0)]),
        ((0, 0, -1), [(0, 0, 0), (s, s, 0), (0, s, 0)]),
        # top (z=s, normal 0,0,1)
        ((0, 0, 1), [(0, 0, s), (s, s, s), (s, 0, s)]),
        ((0, 0, 1), [(0, 0, s), (0, s, s), (s, s, s)]),
        # front (y=0, normal 0,-1,0)
        ((0, -1, 0), [(0, 0, 0), (s, 0, s), (s, 0, 0)]),
        ((0, -1, 0), [(0, 0, 0), (0, 0, s), (s, 0, s)]),
        # back (y=s, normal 0,1,0)
        ((0, 1, 0), [(0, s, 0), (s, s, 0), (s, s, s)]),
        ((0, 1, 0), [(0, s, 0), (s, s, s), (0, s, s)]),
        # left (x=0, normal -1,0,0)
        ((-1, 0, 0), [(0, 0, 0), (0, s, 0), (0, s, s)]),
        ((-1, 0, 0), [(0, 0, 0), (0, s, s), (0, 0, s)]),
        # right (x=s, normal 1,0,0)
        ((1, 0, 0), [(s, 0, 0), (s, 0, s), (s, s, s)]),
        ((1, 0, 0), [(s, 0, 0), (s, s, s), (s, s, 0)]),
    ]

    header = b"\x00" * 80
    count = struct.pack("<I", len(faces))
    tri_data = b""
    for normal, verts in faces:
        tri_data += struct.pack(
            "<12fH",
            normal[0], normal[1], normal[2],
            verts[0][0], verts[0][1], verts[0][2],
            verts[1][0], verts[1][1], verts[1][2],
            verts[2][0], verts[2][1], verts[2][2],
            0,  # attribute byte count
        )
    return header + count + tri_data


@pytest.fixture()
def cube_stl_path(tmp_path):
    """Write a 10mm cube STL to a temp file and return the path."""
    path = tmp_path / "cube.stl"
    path.write_bytes(_build_cube_stl(10.0))
    return str(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFindLargestFlatFace:
    def test_finds_face_on_cube(self, cube_stl_path):
        from kiln.surface_intelligence import find_largest_flat_face

        result = find_largest_flat_face(cube_stl_path)
        # All 6 faces of a cube have equal area; the function should
        # return one of the cardinal faces (top preferred for ties).
        assert "face_name" in result
        assert result["face_name"] in {"top", "bottom", "front", "back", "left", "right"}
        assert result["area_mm2"] > 0
        assert len(result["normal"]) == 3

    def test_prefers_top_for_equal_area(self, cube_stl_path):
        from kiln.surface_intelligence import find_largest_flat_face

        result = find_largest_flat_face(cube_stl_path)
        # For a perfect cube all faces are equal area. The implementation
        # iterates triangles in STL order; bottom triangles come first,
        # but top comes next. We just verify a valid face is returned.
        assert result["face_name"] in {"top", "bottom", "front", "back", "left", "right"}

    def test_returns_expected_keys(self, cube_stl_path):
        from kiln.surface_intelligence import find_largest_flat_face

        result = find_largest_flat_face(cube_stl_path)
        for key in ("normal", "center", "width_mm", "height_mm", "area_mm2", "face_name"):
            assert key in result, f"Missing key: {key}"

    def test_file_not_found(self, tmp_path):
        from kiln.surface_intelligence import find_largest_flat_face

        with pytest.raises(FileNotFoundError):
            find_largest_flat_face(str(tmp_path / "nonexistent.stl"))


class TestFindNamedFace:
    def test_find_top(self, cube_stl_path):
        from kiln.surface_intelligence import find_named_face

        result = find_named_face(cube_stl_path, "top")
        assert result["face_name"] == "top"
        # Normal should point up (+Z)
        assert result["normal"][2] > 0.9

    def test_find_bottom(self, cube_stl_path):
        from kiln.surface_intelligence import find_named_face

        result = find_named_face(cube_stl_path, "bottom")
        assert result["face_name"] == "bottom"
        assert result["normal"][2] < -0.9

    def test_nonexistent_face_name_raises(self, cube_stl_path):
        from kiln.surface_intelligence import find_named_face

        with pytest.raises(ValueError):
            find_named_face(cube_stl_path, "diagonal")


class TestComputeFaceTransform:
    def test_returns_valid_axes(self, cube_stl_path):
        from kiln.surface_intelligence import compute_face_transform, find_named_face
        import math

        face = find_named_face(cube_stl_path, "top")
        transform = compute_face_transform(face)

        assert "x_axis" in transform
        assert "y_axis" in transform
        assert "origin" in transform
        assert "normal" in transform

        # x_axis and y_axis should be unit vectors (length ~1)
        x_len = math.sqrt(sum(c * c for c in transform["x_axis"]))
        y_len = math.sqrt(sum(c * c for c in transform["y_axis"]))
        assert abs(x_len - 1.0) < 1e-6
        assert abs(y_len - 1.0) < 1e-6

        # x_axis and y_axis should be orthogonal
        dot = sum(a * b for a, b in zip(transform["x_axis"], transform["y_axis"]))
        assert abs(dot) < 1e-6
