"""Tests for detect_mesh_pockets.

Covers:
- Flat meshes with no pockets
- Top and bottom pocket detection
- Pocket depth accuracy
- Circular vs rectangular shape classification
- min_depth_mm filtering
- Pocket center position accuracy
- Return format validation
- Empty/invalid STL handling
- Multiple pockets on the same face
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from kiln.generation.validation import detect_mesh_pockets

# ---------------------------------------------------------------------------
# STL writing helpers
# ---------------------------------------------------------------------------


def _write_binary_stl(
    triangles: list[tuple[tuple[float, ...], ...]],
    output_path: str,
) -> None:
    """Write triangles to a binary STL file (zero normals)."""
    with open(output_path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", v[0], v[1], v[2]))
            fh.write(struct.pack("<H", 0))


def _make_disc_triangles(
    cx: float,
    cy: float,
    z: float,
    radius: float,
    segments: int = 16,
    *,
    flip: bool = False,
) -> list[tuple[tuple[float, ...], ...]]:
    """Create a fan of triangles forming a disc at a given Z height.

    When *flip* is True the winding order is reversed so the face normal
    points downward (-Z) instead of upward (+Z).
    """
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        p0 = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z)
        p1 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z)
        center = (cx, cy, z)
        if flip:
            tris.append((center, p1, p0))
        else:
            tris.append((center, p0, p1))
    return tris


def _make_annular_ring_triangles(
    cx: float,
    cy: float,
    z: float,
    inner_radius: float,
    outer_radius: float,
    segments: int = 16,
    *,
    flip: bool = False,
) -> list[tuple[tuple[float, ...], ...]]:
    """Create an annular ring (washer shape) at a given Z height.

    Face normal points upward (+Z) by default.  Set *flip* for downward (-Z).
    """
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        inner0 = (cx + inner_radius * math.cos(a0), cy + inner_radius * math.sin(a0), z)
        inner1 = (cx + inner_radius * math.cos(a1), cy + inner_radius * math.sin(a1), z)
        outer0 = (cx + outer_radius * math.cos(a0), cy + outer_radius * math.sin(a0), z)
        outer1 = (cx + outer_radius * math.cos(a1), cy + outer_radius * math.sin(a1), z)
        if flip:
            tris.append((inner0, outer1, outer0))
            tris.append((inner0, inner1, outer1))
        else:
            tris.append((inner0, outer0, outer1))
            tris.append((inner0, outer1, inner1))
    return tris


def _make_rect_triangles(
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    z: float,
    *,
    flip: bool = False,
) -> list[tuple[tuple[float, ...], ...]]:
    """Create two triangles forming a rectangle at a given Z height."""
    p0 = (x_min, y_min, z)
    p1 = (x_max, y_min, z)
    p2 = (x_max, y_max, z)
    p3 = (x_min, y_max, z)
    if flip:
        return [(p0, p2, p1), (p0, p3, p2)]
    return [(p0, p1, p2), (p0, p2, p3)]


def _make_side_wall_triangles(
    cx: float,
    cy: float,
    radius: float,
    z_bottom: float,
    z_top: float,
    segments: int = 16,
) -> list[tuple[tuple[float, ...], ...]]:
    """Create side wall quad strips for a cylinder."""
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        bl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_bottom)
        br = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_bottom)
        tl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_top)
        tr = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_top)
        tris.append((bl, br, tr))
        tris.append((bl, tr, tl))
    return tris


# ---------------------------------------------------------------------------
# TestDetectMeshPockets
# ---------------------------------------------------------------------------


class TestDetectMeshPockets:
    """Pocket detection on STL meshes — Z-cluster analysis, shape, depth."""

    def test_flat_disc_no_pockets(self, tmp_path: Path) -> None:
        stl = tmp_path / "flat_disc.stl"
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Top disc at z=5
        tris.extend(_make_disc_triangles(0, 0, 5.0, 20.0))
        # Bottom disc at z=0
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        # Side walls
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, 5.0))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert result["pockets"] == []

    def test_coaster_with_top_pocket(self, tmp_path: Path) -> None:
        stl = tmp_path / "top_pocket.stl"
        height = 5.0
        pocket_depth = 1.5
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Top annular ring at z=height (main top surface)
        tris.extend(_make_annular_ring_triangles(0, 0, height, 8.0, 20.0))
        # Pocket floor at z=(height - pocket_depth)
        tris.extend(_make_disc_triangles(0, 0, height - pocket_depth, 8.0))
        # Bottom disc at z=0
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        # Side walls
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        pocket = result["pockets"][0]
        assert pocket["face"] == "top"
        assert abs(pocket["depth_mm"] - pocket_depth) < 0.15

    def test_coaster_with_bottom_pocket(self, tmp_path: Path) -> None:
        stl = tmp_path / "bottom_pocket.stl"
        height = 5.0
        pocket_depth = 1.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Top disc at z=height
        tris.extend(_make_disc_triangles(0, 0, height, 20.0))
        # Bottom annular ring at z=0 (main bottom surface, normal down)
        tris.extend(_make_annular_ring_triangles(0, 0, 0.0, 8.0, 20.0, flip=True))
        # Pocket floor (recessed upward) at z=pocket_depth, normal facing down
        tris.extend(_make_disc_triangles(0, 0, pocket_depth, 8.0, flip=True))
        # Side walls
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        bottom_pockets = [p for p in result["pockets"] if p["face"] == "bottom"]
        assert len(bottom_pockets) == 1
        assert abs(bottom_pockets[0]["depth_mm"] - pocket_depth) < 0.15

    def test_both_top_and_bottom_pockets(self, tmp_path: Path) -> None:
        stl = tmp_path / "both_pockets.stl"
        height = 6.0
        top_depth = 1.5
        bottom_depth = 1.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Top annular ring at z=height
        tris.extend(_make_annular_ring_triangles(0, 0, height, 8.0, 20.0))
        # Top pocket floor
        tris.extend(_make_disc_triangles(0, 0, height - top_depth, 8.0))
        # Bottom annular ring at z=0 (normal down)
        tris.extend(_make_annular_ring_triangles(0, 0, 0.0, 8.0, 20.0, flip=True))
        # Bottom pocket floor (recessed upward, normal down)
        tris.extend(_make_disc_triangles(0, 0, bottom_depth, 8.0, flip=True))
        # Side walls
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        top_pockets = [p for p in result["pockets"] if p["face"] == "top"]
        bottom_pockets = [p for p in result["pockets"] if p["face"] == "bottom"]
        assert len(top_pockets) == 1
        assert len(bottom_pockets) == 1
        assert abs(top_pockets[0]["depth_mm"] - top_depth) < 0.15
        assert abs(bottom_pockets[0]["depth_mm"] - bottom_depth) < 0.15

    def test_pocket_depth_accuracy(self, tmp_path: Path) -> None:
        stl = tmp_path / "depth_accuracy.stl"
        height = 10.0
        pocket_depth = 2.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        tris.extend(_make_annular_ring_triangles(0, 0, height, 8.0, 20.0))
        tris.extend(_make_disc_triangles(0, 0, height - pocket_depth, 8.0))
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        assert abs(result["pockets"][0]["depth_mm"] - 2.0) < 0.1

    def test_circular_pocket_detected_as_circular(self, tmp_path: Path) -> None:
        stl = tmp_path / "circular_pocket.stl"
        height = 5.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        tris.extend(_make_annular_ring_triangles(0, 0, height, 10.0, 20.0))
        tris.extend(_make_disc_triangles(0, 0, height - 1.5, 10.0, segments=32))
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        pocket = result["pockets"][0]
        assert pocket["shape"] == "circular"
        assert pocket["radius_mm"] is not None
        assert pocket["radius_mm"] > 0

    def test_rectangular_pocket_detected_as_rectangular(self, tmp_path: Path) -> None:
        stl = tmp_path / "rect_pocket.stl"
        height = 5.0
        pocket_depth = 1.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Main top surface as a rectangle
        tris.extend(_make_rect_triangles(-20, -20, 20, 20, height))
        # Rectangular pocket floor — width != height to trigger rectangular
        tris.extend(_make_rect_triangles(-15, -5, 15, 5, height - pocket_depth))
        # Bottom surface
        tris.extend(_make_rect_triangles(-20, -20, 20, 20, 0.0, flip=True))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        pocket = result["pockets"][0]
        assert pocket["shape"] == "rectangular"
        assert pocket["radius_mm"] is None
        assert pocket["width_mm"] > pocket["height_mm"]

    def test_min_depth_filter(self, tmp_path: Path) -> None:
        stl = tmp_path / "shallow_pocket.stl"
        height = 5.0
        shallow_depth = 0.2  # Below default min_depth_mm of 0.3
        tris: list[tuple[tuple[float, ...], ...]] = []
        tris.extend(_make_annular_ring_triangles(0, 0, height, 8.0, 20.0))
        tris.extend(_make_disc_triangles(0, 0, height - shallow_depth, 8.0))
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        # Default min_depth_mm=0.3 should filter out 0.2mm pocket
        result = detect_mesh_pockets(str(stl))
        assert result["pockets"] == []

        # With lower threshold it should be detected
        result2 = detect_mesh_pockets(str(stl), min_depth_mm=0.1)
        assert len(result2["pockets"]) == 1

    def test_pocket_center_position(self, tmp_path: Path) -> None:
        stl = tmp_path / "offset_pocket.stl"
        height = 5.0
        pocket_depth = 1.5
        offset_x = 5.0
        offset_y = 3.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Large top surface
        tris.extend(_make_rect_triangles(-20, -20, 20, 20, height))
        # Off-center pocket floor
        tris.extend(_make_disc_triangles(offset_x, offset_y, height - pocket_depth, 4.0))
        # Bottom surface
        tris.extend(_make_rect_triangles(-20, -20, 20, 20, 0.0, flip=True))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        pocket = result["pockets"][0]
        assert abs(pocket["center_x"] - offset_x) < 0.5
        assert abs(pocket["center_y"] - offset_y) < 0.5

    def test_return_format_has_expected_keys(self, tmp_path: Path) -> None:
        stl = tmp_path / "format_check.stl"
        height = 5.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        tris.extend(_make_disc_triangles(0, 0, height, 10.0))
        tris.extend(_make_disc_triangles(0, 0, 0.0, 10.0, flip=True))
        tris.extend(_make_side_wall_triangles(0, 0, 10.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert "main_top_z" in result
        assert "main_bottom_z" in result
        assert "overall_height_mm" in result
        assert "bounding_box" in result
        assert "pockets" in result
        assert isinstance(result["pockets"], list)
        assert isinstance(result["bounding_box"], dict)
        assert abs(result["overall_height_mm"] - height) < 0.1
        bbox = result["bounding_box"]
        assert "x_min" in bbox
        assert "x_max" in bbox
        assert "y_min" in bbox
        assert "y_max" in bbox
        assert "z_min" in bbox
        assert "z_max" in bbox

    def test_empty_stl_raises(self, tmp_path: Path) -> None:
        stl = tmp_path / "empty.stl"
        # Write a valid binary STL header with 0 triangles
        with open(stl, "wb") as fh:
            fh.write(b"\x00" * 80)
            fh.write(struct.pack("<I", 0))

        with pytest.raises(ValueError, match="no triangles"):
            detect_mesh_pockets(str(stl))

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            detect_mesh_pockets(str(tmp_path / "nonexistent.stl"))

    def test_multiple_pockets_same_face(self, tmp_path: Path) -> None:
        stl = tmp_path / "multi_pocket.stl"
        height = 5.0
        depth_shallow = 1.0
        depth_deep = 2.5
        tris: list[tuple[tuple[float, ...], ...]] = []
        # Main top surface
        tris.extend(_make_rect_triangles(-30, -30, 30, 30, height))
        # Shallow pocket floor
        tris.extend(_make_disc_triangles(-10, 0, height - depth_shallow, 4.0))
        # Deep pocket floor
        tris.extend(_make_disc_triangles(10, 0, height - depth_deep, 4.0))
        # Bottom surface
        tris.extend(_make_rect_triangles(-30, -30, 30, 30, 0.0, flip=True))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        top_pockets = [p for p in result["pockets"] if p["face"] == "top"]
        assert len(top_pockets) == 2
        depths = sorted(p["depth_mm"] for p in top_pockets)
        assert abs(depths[0] - depth_shallow) < 0.15
        assert abs(depths[1] - depth_deep) < 0.15

    def test_pocket_has_all_expected_fields(self, tmp_path: Path) -> None:
        stl = tmp_path / "pocket_fields.stl"
        height = 5.0
        tris: list[tuple[tuple[float, ...], ...]] = []
        tris.extend(_make_annular_ring_triangles(0, 0, height, 8.0, 20.0))
        tris.extend(_make_disc_triangles(0, 0, height - 1.5, 8.0))
        tris.extend(_make_disc_triangles(0, 0, 0.0, 20.0, flip=True))
        tris.extend(_make_side_wall_triangles(0, 0, 20.0, 0.0, height))
        _write_binary_stl(tris, str(stl))

        result = detect_mesh_pockets(str(stl))
        assert len(result["pockets"]) == 1
        pocket = result["pockets"][0]
        expected_keys = {
            "face", "center_x", "center_y", "floor_z", "depth_mm",
            "shape", "radius_mm", "width_mm", "height_mm", "triangle_count",
        }
        assert set(pocket.keys()) == expected_keys
