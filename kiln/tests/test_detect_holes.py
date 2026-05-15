"""Tests for ``detect_holes`` in ``kiln.generation.validation``.

Covers:
- Empty / hole-free meshes return an empty list (no false positives).
- A single Z-axis hole is detected with correct diameter, depth, axis.
- Hole oriented along the X axis returns ``axis == "x"``.
- Hole oriented along the Y axis returns ``axis == "y"``.
- Two holes on the same face are both recovered.
- Holes whose diameter is below ``min_diameter_mm`` are filtered out.
- Holes whose depth is below ``min_depth_mm`` are filtered out.
- Holes larger than ``max_diameter_mm`` are filtered out.
- Solid pillars (outward-facing cylindrical surfaces) are NOT reported.
- The returned dicts carry the documented keys: position, diameter_mm,
  depth_mm, axis, triangle_count.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from kiln.generation.validation import detect_holes


# ---------------------------------------------------------------------------
# STL synthesis helpers (binary STL, hand-packed — same style as the
# pockets test file so reviewers can compare the two end-to-end).
# ---------------------------------------------------------------------------


def _write_binary_stl(
    triangles: list[tuple[tuple[float, ...], ...]],
    output_path: str,
) -> None:
    """Write triangles to a binary STL file (zero normals — STL stores
    normals but parsers recompute them from winding)."""
    with open(output_path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", v[0], v[1], v[2]))
            fh.write(struct.pack("<H", 0))


def _hole_side_wall_z(
    cx: float,
    cy: float,
    radius: float,
    z_bottom: float,
    z_top: float,
    segments: int = 24,
) -> list[tuple[tuple[float, ...], ...]]:
    """Cylindrical side-wall triangles for a Z-axis HOLE.

    Winding order is reversed vs. an outward-facing pillar so that the
    face normals point INWARD toward the hole's axis (which is what the
    detector requires).
    """
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        bl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_bottom)
        br = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_bottom)
        tl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_top)
        tr = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_top)
        # Reversed winding -> normal points inward (toward axis at cx,cy).
        tris.append((bl, tr, br))
        tris.append((bl, tl, tr))
    return tris


def _pillar_side_wall_z(
    cx: float,
    cy: float,
    radius: float,
    z_bottom: float,
    z_top: float,
    segments: int = 24,
) -> list[tuple[tuple[float, ...], ...]]:
    """Cylindrical side-wall triangles for a SOLID Z-axis pillar.

    Normals point OUTWARD (away from the central axis) — this is the
    negative control that detect_holes must reject.
    """
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


def _hole_side_wall_x(
    cy: float,
    cz: float,
    radius: float,
    x_left: float,
    x_right: float,
    segments: int = 24,
) -> list[tuple[tuple[float, ...], ...]]:
    """Hole drilled along the X axis — radial profile in the YZ plane."""
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        bl = (x_left, cy + radius * math.cos(a0), cz + radius * math.sin(a0))
        br = (x_left, cy + radius * math.cos(a1), cz + radius * math.sin(a1))
        tl = (x_right, cy + radius * math.cos(a0), cz + radius * math.sin(a0))
        tr = (x_right, cy + radius * math.cos(a1), cz + radius * math.sin(a1))
        # Reversed winding -> normal points inward toward (cy, cz) at every x.
        tris.append((bl, tr, br))
        tris.append((bl, tl, tr))
    return tris


def _hole_side_wall_y(
    cx: float,
    cz: float,
    radius: float,
    y_front: float,
    y_back: float,
    segments: int = 24,
) -> list[tuple[tuple[float, ...], ...]]:
    """Hole drilled along the Y axis — radial profile in the XZ plane.

    Winding chosen so the face normal points INWARD toward the (cx, cz)
    axis line in every (X, Z) cross-section.  Verified by hand: at
    theta=0 the normal lies along -X (toward the hole's axis), so the
    detect_holes inward-normal gate accepts the cluster.
    """
    tris: list[tuple[tuple[float, ...], ...]] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        bl = (cx + radius * math.cos(a0), y_front, cz + radius * math.sin(a0))
        br = (cx + radius * math.cos(a1), y_front, cz + radius * math.sin(a1))
        tl = (cx + radius * math.cos(a0), y_back, cz + radius * math.sin(a0))
        tr = (cx + radius * math.cos(a1), y_back, cz + radius * math.sin(a1))
        # Flipped winding vs. the Z/X helpers — Y axis lives between
        # the +X and +Z right-hand-rule axes, so the inward direction
        # requires the opposite vertex order.
        tris.append((bl, br, tr))
        tris.append((bl, tr, tl))
    return tris


def _solid_cube_triangles(size: float) -> list[tuple[tuple[float, ...], ...]]:
    """Standard axis-aligned cube spanning (0,0,0)→(size,size,size).

    Used as the negative-control mesh: no cylindrical features at all.
    """
    s = size
    p = [
        (0.0, 0.0, 0.0),
        (s, 0.0, 0.0),
        (s, s, 0.0),
        (0.0, s, 0.0),
        (0.0, 0.0, s),
        (s, 0.0, s),
        (s, s, s),
        (0.0, s, s),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),       # bottom -Z
        (4, 5, 6), (4, 6, 7),       # top +Z
        (0, 1, 5), (0, 5, 4),       # front -Y
        (1, 2, 6), (1, 6, 5),       # right +X
        (2, 3, 7), (2, 7, 6),       # back +Y
        (3, 0, 4), (3, 4, 7),       # left -X
    ]
    return [(p[a], p[b], p[c]) for a, b, c in faces]


# ---------------------------------------------------------------------------
# TestDetectHoles
# ---------------------------------------------------------------------------


class TestDetectHoles:
    """Cylindrical-hole detection over hand-built STL meshes."""

    def test_solid_cube_has_no_holes(self, tmp_path: Path) -> None:
        """Cube with all-axis-aligned faces must produce zero holes —
        flat panels are perpendicular to a principal axis but have no
        circular profile, so they get rejected at the radius-fit gate."""
        stl = tmp_path / "cube.stl"
        _write_binary_stl(_solid_cube_triangles(20.0), str(stl))
        holes = detect_holes(str(stl))
        assert holes == []

    def test_empty_mesh_returns_empty_list(self, tmp_path: Path) -> None:
        """A valid STL with zero triangles returns the empty list, not
        an exception."""
        stl = tmp_path / "empty.stl"
        _write_binary_stl([], str(stl))
        holes = detect_holes(str(stl))
        assert holes == []

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            detect_holes(str(tmp_path / "does_not_exist.stl"))

    def test_single_z_axis_hole_detected(self, tmp_path: Path) -> None:
        """One Z-axis hole, diameter 5 mm, depth 10 mm — detector
        recovers each field within tolerance."""
        stl = tmp_path / "single_hole.stl"
        tris = _hole_side_wall_z(
            cx=10.0, cy=10.0, radius=2.5,
            z_bottom=0.0, z_top=10.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert len(holes) == 1
        h = holes[0]
        assert h["axis"] == "z"
        assert h["diameter_mm"] == pytest.approx(5.0, abs=0.2)
        assert h["depth_mm"] == pytest.approx(10.0, abs=0.05)
        assert h["position"]["x_mm"] == pytest.approx(10.0, abs=0.05)
        assert h["position"]["y_mm"] == pytest.approx(10.0, abs=0.05)
        assert h["position"]["z_mm"] == pytest.approx(5.0, abs=0.05)
        assert h["triangle_count"] > 0

    def test_x_axis_hole_detected(self, tmp_path: Path) -> None:
        """Hole oriented along world X — axis label and position
        coordinates flip accordingly."""
        stl = tmp_path / "hole_x.stl"
        tris = _hole_side_wall_x(
            cy=15.0, cz=8.0, radius=3.0,
            x_left=0.0, x_right=20.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert len(holes) == 1
        h = holes[0]
        assert h["axis"] == "x"
        assert h["diameter_mm"] == pytest.approx(6.0, abs=0.2)
        assert h["depth_mm"] == pytest.approx(20.0, abs=0.05)
        assert h["position"]["y_mm"] == pytest.approx(15.0, abs=0.05)
        assert h["position"]["z_mm"] == pytest.approx(8.0, abs=0.05)

    def test_y_axis_hole_detected(self, tmp_path: Path) -> None:
        """Hole oriented along world Y — axis label is ``"y"``."""
        stl = tmp_path / "hole_y.stl"
        tris = _hole_side_wall_y(
            cx=5.0, cz=5.0, radius=1.5,
            y_front=0.0, y_back=12.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert len(holes) == 1
        h = holes[0]
        assert h["axis"] == "y"
        assert h["diameter_mm"] == pytest.approx(3.0, abs=0.2)
        assert h["depth_mm"] == pytest.approx(12.0, abs=0.05)
        assert h["position"]["x_mm"] == pytest.approx(5.0, abs=0.05)
        assert h["position"]["z_mm"] == pytest.approx(5.0, abs=0.05)

    def test_two_holes_on_same_face(self, tmp_path: Path) -> None:
        """Two Z-axis holes far apart — both recovered separately, not
        merged into one large hole."""
        stl = tmp_path / "two_holes.stl"
        tris = []
        tris.extend(_hole_side_wall_z(
            cx=5.0, cy=5.0, radius=1.5,
            z_bottom=0.0, z_top=6.0, segments=24,
        ))
        tris.extend(_hole_side_wall_z(
            cx=25.0, cy=25.0, radius=2.0,
            z_bottom=0.0, z_top=6.0, segments=24,
        ))
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert len(holes) == 2
        diameters = sorted(round(h["diameter_mm"], 1) for h in holes)
        assert diameters[0] == pytest.approx(3.0, abs=0.2)
        assert diameters[1] == pytest.approx(4.0, abs=0.2)

    def test_pillar_is_not_reported_as_hole(self, tmp_path: Path) -> None:
        """A solid cylindrical pillar (outward-facing normals) must NOT
        register — the inward-normal check rejects it."""
        stl = tmp_path / "pillar.stl"
        tris = _pillar_side_wall_z(
            cx=10.0, cy=10.0, radius=2.5,
            z_bottom=0.0, z_top=10.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert holes == []

    def test_hole_below_min_diameter_filtered(self, tmp_path: Path) -> None:
        """A 1 mm-diameter hole is below the default 0.8 mm floor but
        above an elevated ``min_diameter_mm`` of 2 mm — the filter fires."""
        stl = tmp_path / "tiny_hole.stl"
        tris = _hole_side_wall_z(
            cx=5.0, cy=5.0, radius=0.5,  # diameter 1 mm
            z_bottom=0.0, z_top=4.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        # Default floor (0.8) admits this hole.
        holes_default = detect_holes(str(stl))
        assert len(holes_default) == 1
        # Raise the floor to 2 mm — the same hole is now filtered.
        holes_filtered = detect_holes(str(stl), min_diameter_mm=2.0)
        assert holes_filtered == []

    def test_hole_below_min_depth_filtered(self, tmp_path: Path) -> None:
        """A very-shallow hole (0.2 mm deep) is rejected by the default
        ``min_depth_mm`` of 0.5 mm."""
        stl = tmp_path / "shallow_hole.stl"
        tris = _hole_side_wall_z(
            cx=5.0, cy=5.0, radius=1.5,
            z_bottom=0.0, z_top=0.2, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert holes == []
        # Lower the depth gate — the same hole now registers.
        relaxed = detect_holes(
            str(stl), min_diameter_mm=1.0, min_depth_mm=0.1,
        )
        assert len(relaxed) == 1

    def test_hole_above_max_diameter_filtered(self, tmp_path: Path) -> None:
        """A 100 mm-diameter "hole" exceeds the default 50 mm ceiling —
        treated as an outer shell and skipped."""
        stl = tmp_path / "huge_hole.stl"
        tris = _hole_side_wall_z(
            cx=50.0, cy=50.0, radius=50.0,  # diameter 100 mm
            z_bottom=0.0, z_top=10.0, segments=32,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert holes == []

    def test_returned_dict_has_documented_keys(self, tmp_path: Path) -> None:
        """Every returned dict must carry the keys listed in the
        docstring — defensive against accidental shape drift."""
        stl = tmp_path / "key_check.stl"
        tris = _hole_side_wall_z(
            cx=5.0, cy=5.0, radius=2.0,
            z_bottom=0.0, z_top=5.0, segments=24,
        )
        _write_binary_stl(tris, str(stl))
        holes = detect_holes(str(stl), min_diameter_mm=1.0)
        assert len(holes) == 1
        h = holes[0]
        assert set(h.keys()) == {
            "position", "diameter_mm", "depth_mm", "axis", "triangle_count",
        }
        assert set(h["position"].keys()) == {"x_mm", "y_mm", "z_mm"}
        assert h["axis"] in {"x", "y", "z"}
        assert isinstance(h["triangle_count"], int)

    def test_invalid_parameters_raise(self, tmp_path: Path) -> None:
        """min_diameter_mm <= 0 and max <= min are both contract
        violations."""
        stl = tmp_path / "anything.stl"
        _write_binary_stl(_solid_cube_triangles(10.0), str(stl))
        with pytest.raises(ValueError):
            detect_holes(str(stl), min_diameter_mm=0.0)
        with pytest.raises(ValueError):
            detect_holes(str(stl), min_diameter_mm=5.0, max_diameter_mm=4.0)
