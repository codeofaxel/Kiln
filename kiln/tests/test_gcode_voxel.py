"""Tests for kiln.gcode_voxel.

Exercises the voxelizer against synthetic gcode files where we know
the expected occupancy by construction.  The invariants guarded:

    * A simple line of extrusion produces voxels along that line.
    * Two identical gcodes diff to zero.
    * A "hole" gcode (cylinder minus inner cylinder) diffs against
      the solid gcode with negative deltas inside the hole region.
    * Travel moves (G0 and G1 with no E) don't paint.
    * Both M82 (absolute E) and M83 (relative E) parse correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.gcode_voxel import (
    VoxelGrid,
    diff_voxel_grids,
    gcode_to_voxel_grid,
)

# ---------------------------------------------------------------------------
# Synthetic gcode helpers
# ---------------------------------------------------------------------------


def _write_gcode(path: Path, lines: list[str]) -> None:
    """Write ``lines`` plus a trailing newline."""
    path.write_text("\n".join(lines) + "\n")


def _line_segment_gcode(
    path: Path,
    *,
    x_start: float = 100.0,
    y: float = 100.0,
    z: float = 0.2,
    length_mm: float = 10.0,
    extrude_mm: float = 0.5,
    relative_e: bool = True,
) -> None:
    """Single horizontal extrusion segment along +X."""
    lines = [
        "M83" if relative_e else "M82",
        "G92 E0",
        f"G0 X{x_start:.3f} Y{y:.3f} Z{z:.3f}",
        f"G1 X{x_start + length_mm:.3f} Y{y:.3f} Z{z:.3f} E{extrude_mm:.4f}",
    ]
    _write_gcode(path, lines)


def _filled_square_gcode(
    path: Path,
    *,
    x_min: float = 100.0,
    y_min: float = 100.0,
    side_mm: float = 10.0,
    n_layers: int = 5,
    layer_height_mm: float = 0.2,
    line_spacing_mm: float = 0.4,
    extrude_per_mm: float = 0.05,
    skip_xy_window: tuple[float, float, float, float] | None = None,
) -> None:
    """Fill a square region layer-by-layer with raster lines.

    ``skip_xy_window`` (xmin, ymin, xmax, ymax) lets a caller carve a
    rectangular hole — extrusion moves whose midpoint falls inside
    are emitted as G0 travels instead of G1 extrudes.  This is how we
    construct a "with hole" reference part for diff testing.
    """
    lines: list[str] = ["M83", "G92 E0"]
    n_lines = max(1, int(round(side_mm / line_spacing_mm)))
    for layer in range(1, n_layers + 1):
        z = layer * layer_height_mm
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        for i in range(n_lines):
            y = y_min + i * line_spacing_mm
            x0 = x_min if i % 2 == 0 else x_min + side_mm
            x1 = x_min + side_mm if i % 2 == 0 else x_min
            seg_len = abs(x1 - x0)
            de = seg_len * extrude_per_mm
            mid_x = (x0 + x1) / 2
            inside_skip = (
                skip_xy_window is not None
                and skip_xy_window[0] <= mid_x <= skip_xy_window[2]
                and skip_xy_window[1] <= y <= skip_xy_window[3]
            )
            lines.append(f"G0 X{x0:.3f} Y{y:.3f}")
            if inside_skip:
                lines.append(f"G0 X{x1:.3f} Y{y:.3f}")
            else:
                lines.append(f"G1 X{x1:.3f} Y{y:.3f} E{de:.4f}")
    _write_gcode(path, lines)


# ---------------------------------------------------------------------------
# Voxelizer correctness
# ---------------------------------------------------------------------------


def test_simple_line_segment_voxels_are_along_x_axis(tmp_path: Path) -> None:
    """A single 10mm extrusion along +X should produce voxels at
    consecutive ix values, all at the same iy / iz."""
    gcode = tmp_path / "line.gcode"
    _line_segment_gcode(
        gcode,
        x_start=100.0,
        y=100.0,
        z=0.2,
        length_mm=10.0,
        extrude_mm=0.5,
    )

    grid = gcode_to_voxel_grid(str(gcode))

    # Every voxel should have y_index = floor(100 / 0.4) and z_index =
    # floor(0.2 / 0.2) = 1 (or 0 — boundary case; midpoint sampling
    # puts it solidly in iz=1 because z=0.2 is the floor of layer 1).
    iy_values = {key[1] for key in grid.voxels}
    iz_values = {key[2] for key in grid.voxels}

    assert len(iy_values) == 1, f"expected single y row, got {iy_values}"
    assert len(iz_values) == 1, f"expected single z row, got {iz_values}"

    ix_values = sorted({key[0] for key in grid.voxels})
    # 10mm / 0.4mm voxel ≈ 25 voxels; allow 24-26 because endpoints
    # fall on a boundary.
    assert 24 <= len(ix_values) <= 26
    # And they should be consecutive.
    assert ix_values == list(range(ix_values[0], ix_values[-1] + 1))

    # Total accumulated extrusion equals what we extruded (within
    # floating-point noise from the per-step distribution).
    total = sum(grid.voxels.values())
    assert total == pytest.approx(0.5, abs=1e-6)
    assert grid.total_extruded_mm == pytest.approx(0.5, abs=1e-6)


def test_g0_travel_does_not_paint(tmp_path: Path) -> None:
    """A G0 move (travel) must not create any voxels."""
    gcode = tmp_path / "travel.gcode"
    _write_gcode(gcode, [
        "M83",
        "G0 X100 Y100 Z0.2",
        "G0 X200 Y200 Z0.2",
    ])

    grid = gcode_to_voxel_grid(str(gcode))

    assert len(grid.voxels) == 0
    assert grid.total_extruded_mm == 0.0


def test_g1_with_no_e_does_not_paint(tmp_path: Path) -> None:
    """G1 moves without an E delta are travels — must not paint."""
    gcode = tmp_path / "g1_no_e.gcode"
    _write_gcode(gcode, [
        "M83",
        "G0 X100 Y100 Z0.2",
        "G1 X110 Y100 Z0.2 F600",
    ])

    grid = gcode_to_voxel_grid(str(gcode))

    assert len(grid.voxels) == 0


def test_negative_e_retract_does_not_paint(tmp_path: Path) -> None:
    """Retract moves (negative E delta) are not extrusions."""
    gcode = tmp_path / "retract.gcode"
    _write_gcode(gcode, [
        "M83",
        "G0 X100 Y100 Z0.2",
        "G1 E-2.0 F1800",        # pure retract
        "G0 X200 Y200 Z0.2",
        "G1 E2.0 F1800",         # prime
    ])

    grid = gcode_to_voxel_grid(str(gcode))

    # Retract + prime + travel → no painted voxels.
    assert len(grid.voxels) == 0


def test_absolute_and_relative_e_modes_match(tmp_path: Path) -> None:
    """Same physical print expressed in M82 (abs E) vs M83 (rel E)
    must produce identical voxel grids."""
    rel = tmp_path / "rel.gcode"
    abs_ = tmp_path / "abs.gcode"

    # Relative E version.
    _write_gcode(rel, [
        "M83",
        "G92 E0",
        "G0 X100 Y100 Z0.2",
        "G1 X110 Y100 Z0.2 E0.5",
        "G1 X120 Y100 Z0.2 E0.5",
    ])

    # Absolute E version — same physical extrusion (0.5 + 0.5 = 1.0 mm
    # cumulative).
    _write_gcode(abs_, [
        "M82",
        "G92 E0",
        "G0 X100 Y100 Z0.2",
        "G1 X110 Y100 Z0.2 E0.5",
        "G1 X120 Y100 Z0.2 E1.0",
    ])

    g_rel = gcode_to_voxel_grid(str(rel))
    g_abs = gcode_to_voxel_grid(str(abs_))

    assert g_rel.voxels == g_abs.voxels
    assert g_rel.total_extruded_mm == pytest.approx(g_abs.total_extruded_mm)


# ---------------------------------------------------------------------------
# Diff correctness
# ---------------------------------------------------------------------------


def test_identical_gcodes_diff_to_zero(tmp_path: Path) -> None:
    """Two byte-identical files must diff to an empty delta dict."""
    gcode_a = tmp_path / "a.gcode"
    gcode_b = tmp_path / "b.gcode"
    for path in (gcode_a, gcode_b):
        _filled_square_gcode(path, n_layers=3)

    grid_a = gcode_to_voxel_grid(str(gcode_a))
    grid_b = gcode_to_voxel_grid(str(gcode_b))
    diff = diff_voxel_grids(grid_a, grid_b)

    assert diff.deltas == {}
    assert diff.voxels_added == 0
    assert diff.voxels_removed == 0


def test_solid_vs_hole_diff_shows_negative_in_hole_region(tmp_path: Path) -> None:
    """Solid square A vs square-with-hole B must show negative deltas
    confined to the hole region's XY footprint."""
    solid = tmp_path / "solid.gcode"
    holed = tmp_path / "holed.gcode"

    # 10×10mm filled square, 5 layers tall.
    _filled_square_gcode(
        solid,
        x_min=100.0,
        y_min=100.0,
        side_mm=10.0,
        n_layers=5,
    )

    # Same square but skip extrusion mid-line on layers 4 and 5 only.
    # We split each affected raster line into TWO extrusions with a
    # G0 travel through the skip window — that way the hole is
    # spatially confined to (skip_window_x, skip_window_y) on
    # layers 4-5 instead of erasing entire raster rows.
    lines: list[str] = ["M83", "G92 E0"]
    n_lines = 25  # 10mm / 0.4mm
    skip_window = (102.5, 102.5, 107.5, 107.5)  # 5×5 carve in the middle
    skip_layers = {4, 5}
    for layer in range(1, 6):
        z = layer * 0.2
        lines.append(";LAYER_CHANGE")
        lines.append(f";Z:{z:.3f}")
        lines.append(f"G1 Z{z:.3f} F600")
        for i in range(n_lines):
            y = 100.0 + i * 0.4
            x0 = 100.0 if i % 2 == 0 else 110.0
            x1 = 110.0 if i % 2 == 0 else 100.0
            row_in_skip = (
                layer in skip_layers
                and skip_window[1] <= y <= skip_window[3]
            )
            lines.append(f"G0 X{x0:.3f} Y{y:.3f}")
            if not row_in_skip:
                de = abs(x1 - x0) * 0.05
                lines.append(f"G1 X{x1:.3f} Y{y:.3f} E{de:.4f}")
                continue
            # Split: extrude up to the skip window, travel across,
            # extrude past it.  Direction-dependent.
            if x0 < x1:
                left, right = x0, x1
            else:
                left, right = x1, x0
            # Pre-window extrude.
            de_pre = (skip_window[0] - left) * 0.05
            de_post = (right - skip_window[2]) * 0.05
            if x0 < x1:
                lines.append(f"G1 X{skip_window[0]:.3f} Y{y:.3f} E{de_pre:.4f}")
                lines.append(f"G0 X{skip_window[2]:.3f} Y{y:.3f}")
                lines.append(f"G1 X{x1:.3f} Y{y:.3f} E{de_post:.4f}")
            else:
                lines.append(f"G1 X{skip_window[2]:.3f} Y{y:.3f} E{de_post:.4f}")
                lines.append(f"G0 X{skip_window[0]:.3f} Y{y:.3f}")
                lines.append(f"G1 X{x1:.3f} Y{y:.3f} E{de_pre:.4f}")
    holed.write_text("\n".join(lines) + "\n")

    grid_solid = gcode_to_voxel_grid(str(solid))
    grid_holed = gcode_to_voxel_grid(str(holed))
    diff = diff_voxel_grids(grid_solid, grid_holed)

    # holed has LESS material → diff(b - a) should be negative inside
    # the hole.
    assert diff.voxels_removed > 0, "expected removed material in the hole"
    assert diff.total_removed_mm > 0
    # Nothing added — the hole is purely a removal.
    assert diff.voxels_added == 0

    # All removed-material voxels should land inside the skip window's
    # XY (with one-voxel slack for boundary effects).
    in_window = diff.removed_voxels_in_region(skip_window)
    assert len(in_window) > 0
    # Most (>80%) of removed material should be inside the window —
    # the slop is just border voxels straddling the window edge.
    in_window_amount = sum(-v for v in in_window.values())
    assert in_window_amount / diff.total_removed_mm >= 0.8

    # Z of removed voxels — they should sit at iz corresponding to
    # layers 4 and 5 only (z = 0.8 and z = 1.0).  iz = floor(z / 0.2).
    iz_set = {key[2] for key in diff.deltas if diff.deltas[key] < 0}
    # Layer 4 → z=0.8 → iz=4 (midpoint sampling lands at z=0.8 → iz=4)
    # Layer 5 → z=1.0 → iz=5
    assert iz_set <= {4, 5}, f"unexpected iz values for hole: {iz_set}"


# ---------------------------------------------------------------------------
# Resolution + bounds plumbing
# ---------------------------------------------------------------------------


def test_resolution_mismatch_in_diff_raises(tmp_path: Path) -> None:
    """Diffing two grids with different voxel sizes is a programmer
    error — must raise ValueError."""
    gcode = tmp_path / "x.gcode"
    _line_segment_gcode(gcode, length_mm=5.0)

    g_default = gcode_to_voxel_grid(str(gcode))
    g_fine = gcode_to_voxel_grid(str(gcode), voxel_xy_mm=0.2)

    with pytest.raises(ValueError, match="Grid resolutions differ"):
        diff_voxel_grids(g_default, g_fine)


def test_xy_bounds_clip_grid_to_window(tmp_path: Path) -> None:
    """A bounds clip should drop voxels whose center XY lies outside."""
    gcode = tmp_path / "long.gcode"
    _line_segment_gcode(
        gcode,
        x_start=100.0,
        y=100.0,
        length_mm=20.0,
        extrude_mm=1.0,
    )

    full = gcode_to_voxel_grid(str(gcode))
    clipped = gcode_to_voxel_grid(
        str(gcode),
        bounds=(105.0, 99.0, 115.0, 101.0),
    )

    assert len(full.voxels) > len(clipped.voxels)
    # Every clipped voxel center must be inside the window.
    for key in clipped.voxels:
        cx, cy, _cz = clipped.voxel_to_world(key)
        assert 105.0 <= cx <= 115.0
        assert 99.0 <= cy <= 101.0


def test_invalid_resolution_raises(tmp_path: Path) -> None:
    gcode = tmp_path / "x.gcode"
    _line_segment_gcode(gcode)
    with pytest.raises(ValueError, match="must be positive"):
        gcode_to_voxel_grid(str(gcode), voxel_xy_mm=0.0)
    with pytest.raises(ValueError, match="must be positive"):
        gcode_to_voxel_grid(str(gcode), voxel_z_mm=-0.1)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gcode_to_voxel_grid(str(tmp_path / "nope.gcode"))


def test_slice_by_z_layer_returns_per_layer_totals(tmp_path: Path) -> None:
    """The Z-slice helper should re-bin grid voxels into 1-indexed
    print layers."""
    gcode = tmp_path / "stack.gcode"
    _filled_square_gcode(gcode, n_layers=3, layer_height_mm=0.2)
    grid = gcode_to_voxel_grid(str(gcode))

    per_layer = grid.slice_by_z_layer(layer_height_mm=0.2)

    # 3 layers, all with similar extrusion totals.
    assert set(per_layer.keys()) == {1, 2, 3}
    totals = sorted(per_layer.values())
    assert totals[-1] / totals[0] < 1.1  # within 10% of each other


def test_voxel_grid_round_trip_world_voxel(tmp_path: Path) -> None:
    """world_to_voxel followed by voxel_to_world should return a
    point inside the same voxel."""
    grid = VoxelGrid(voxel_xy_mm=0.4, voxel_z_mm=0.2, origin_x_mm=50.0)
    key = grid.world_to_voxel(101.234, 102.567, 1.234)
    cx, cy, cz = grid.voxel_to_world(key)
    # The center should be within half a voxel of the original point.
    assert abs(cx - 101.234) <= 0.4
    assert abs(cy - 102.567) <= 0.4
    assert abs(cz - 1.234) <= 0.2
