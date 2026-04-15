"""Voxelize a gcode file's extrusion path into a sparse 3D occupancy grid.

The primitive answers a single physical question: *how much filament does
this gcode deposit at every point in space?*  Once you have that,
comparing two gcodes for "same physical part" reduces to comparing two
voxel grids — which is robust to slicer-noise (different mesh
triangulations, retract tweaks, travel reordering) that would defeat a
naive line-by-line gcode diff.

Why this lives in public Kiln (not kiln-pro):

    The voxel primitive is broadly useful: failure-recovery resume
    validation ("did we lay down the same physical material as the
    original plan?"), multi-material print verification ("is colour A
    where it should be?"), regression checks across slicer upgrades,
    and pro's mid-print decoration arbiter all need the same building
    block.  Keeping the primitive in public Kiln means each of those
    consumers reads the same numbers — and any free-tier user can
    inspect their own gcode without paying for a tier they don't need.

    Pro's mid-print arbiter (:mod:`kiln_pro.recovery.gcode_diff_arbiter`)
    is what wraps this with decoration-specific intelligence (logo XY
    bounds, bridge-layer reasoning, plan reconciliation).  The split
    matches the public/pro pattern of public shipping primitives and
    pro wrapping them with intelligence.

Algorithm:

    Stream the gcode line-by-line, tracking absolute XYZE state plus
    M82/M83 mode for relative E.  For every G1 move with positive
    extrusion (``dE > 0``), the segment from previous (X, Y, Z) to
    new (X, Y, Z) is "painted" into the voxel grid: each voxel the
    segment passes through accumulates the share of total extruded
    filament length proportional to the segment fraction inside it.

    Travel moves (G0, or G1 without a positive E delta) are excluded.
    Negative E (retracts) is excluded.  Z-only moves don't paint.

    The grid is sparse — only voxels with at least one extrusion get
    a key.  This keeps memory bounded: a typical 100mm part at 0.4mm
    XY × 0.2mm Z resolution is ~80k voxels even when fully solid,
    and most parts are mostly hollow.

The output of :func:`gcode_to_voxel_grid` is a :class:`VoxelGrid`
instance whose only required fields are ``voxels`` (the sparse dict)
and the resolution it was binned at.  All consumers — pro arbiter,
multi-material verifier, anything else — read those fields.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)


# Regex.  Compiled once, used hot in :func:`gcode_to_voxel_grid`.  The
# patterns are intentionally tolerant of leading sign + integer-only
# coords (some hand-written gcode dispenses with decimals on round
# numbers like ``X10``).
_X_RE = re.compile(r"\bX(-?\d*\.?\d+)", re.IGNORECASE)
_Y_RE = re.compile(r"\bY(-?\d*\.?\d+)", re.IGNORECASE)
_Z_RE = re.compile(r"\bZ(-?\d*\.?\d+)", re.IGNORECASE)
_E_RE = re.compile(r"\bE(-?\d*\.?\d+)", re.IGNORECASE)
_G0_RE = re.compile(r"^\s*G0\b", re.IGNORECASE)
_G1_RE = re.compile(r"^\s*G1\b", re.IGNORECASE)
_G92_RE = re.compile(r"^\s*G92\b", re.IGNORECASE)
_M82_RE = re.compile(r"^\s*M82\b", re.IGNORECASE)
_M83_RE = re.compile(r"^\s*M83\b", re.IGNORECASE)


# Default resolution targets the "typical FDM" case: 0.4mm nozzle laying
# 0.2mm layers.  Callers slicing finer (0.1mm layers, 0.2mm nozzle) can
# tighten the grid; callers analysing chunky industrial prints can
# loosen it.  Either way the algorithm is O(extrusion_length /
# voxel_size) per move so resolution scales linearly.
_DEFAULT_VOXEL_XY_MM = 0.4
_DEFAULT_VOXEL_Z_MM = 0.2

# Below this segment length we degenerate-skip the rasterizer — the
# entire deposited filament lands in the source voxel and there's no
# point in computing a single-voxel walk.  Threshold matches the
# precision-printing minimum of 1µm.
_MIN_SEGMENT_LEN_MM = 1e-3

# Float-rounding epsilon for voxel-boundary classification.  Without
# this, a point at exactly z = N * voxel_z (e.g. z=0.6, voxel_z=0.2)
# computes z/voxel_z = 2.9999... and lands in voxel iz=2 instead of
# iz=3.  1e-9 mm is safely below any print-meaningful resolution
# (millionth of a micron) but big enough to dominate IEEE 754 noise.
_BOUNDARY_EPSILON = 1e-9


@dataclass
class VoxelGrid:
    """Sparse 3D occupancy grid keyed by (ix, iy, iz) voxel indices.

    The grid stores accumulated extrusion length (mm of filament
    deposited) per voxel.  This is the natural unit for "how much
    material was laid down here" — it commutes cleanly under
    splitting (a longer move passing through more voxels distributes
    its total extrusion over them) and is comparable across gcodes
    even when their slicer settings differ.
    """

    # (ix, iy, iz) -> total extruded filament length (mm) in this voxel.
    voxels: dict[tuple[int, int, int], float] = field(default_factory=dict)

    # Resolution the grid was binned at.  Two grids with different
    # resolutions cannot be diffed meaningfully; :func:`diff_voxel_grids`
    # checks this.
    voxel_xy_mm: float = _DEFAULT_VOXEL_XY_MM
    voxel_z_mm: float = _DEFAULT_VOXEL_Z_MM

    # Origin (XY) of voxel index (0, 0).  Z origin is always 0 (build
    # plate).  Keeps absolute world coordinates recoverable for
    # downstream rendering / overlay.
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0

    # Total extruded filament across the whole file — sanity-check
    # number consumers can use to detect "did the file extrude
    # anything at all" without iterating the dict.
    total_extruded_mm: float = 0.0

    def voxel_to_world(self, key: tuple[int, int, int]) -> tuple[float, float, float]:
        """Center XYZ (mm) of the voxel at ``key``."""
        ix, iy, iz = key
        x = self.origin_x_mm + (ix + 0.5) * self.voxel_xy_mm
        y = self.origin_y_mm + (iy + 0.5) * self.voxel_xy_mm
        z = (iz + 0.5) * self.voxel_z_mm
        return x, y, z

    def world_to_voxel(self, x: float, y: float, z: float) -> tuple[int, int, int]:
        """Voxel index containing world point ``(x, y, z)``.

        Adds a 1e-9 mm epsilon before flooring so a point landing
        exactly on a voxel boundary (e.g. z = 0.6 with 0.2mm voxels,
        which floats to 2.9999...) lands in the upper voxel rather
        than the lower one.  Without this the rasterizer drops a
        full layer's worth of extrusion into the layer below it.
        """
        ix = int(math.floor((x - self.origin_x_mm) / self.voxel_xy_mm + _BOUNDARY_EPSILON))
        iy = int(math.floor((y - self.origin_y_mm) / self.voxel_xy_mm + _BOUNDARY_EPSILON))
        iz = int(math.floor(z / self.voxel_z_mm + _BOUNDARY_EPSILON))
        return ix, iy, iz

    def slice_by_z_layer(self, layer_height_mm: float) -> dict[int, float]:
        """Return total extrusion per 1-indexed print layer.

        Useful for "how much material is at each layer" queries that
        don't need the full XY breakdown.  The Z axis index stays in
        the grid's own ``voxel_z_mm`` units; this helper re-bins to
        the caller's print layer height.

        Slicer convention: layer N extrudes AT z = N * layer_height
        (the nozzle parks at the *top* of the layer it's about to
        deposit).  In voxel terms the extrusion at z = N*h lands
        in voxel iz = floor(N*h / voxel_z); for the canonical case
        where ``layer_height_mm == voxel_z_mm`` this gives iz = N.

        We ascribe a voxel's contribution to slicer layer
        ``ceil((iz_lo + ε) / (layer_height/voxel_z))`` where iz_lo
        is the voxel's lower Z edge — equivalent to "the smallest
        layer N such that N * h >= the voxel's lower Z edge."
        """
        if layer_height_mm <= 0:
            raise ValueError(f"layer_height_mm must be positive (got {layer_height_mm})")
        out: dict[int, float] = {}
        for (_ix, _iy, iz), amount in self.voxels.items():
            # The slicer parks at z = N*h.  In voxel terms (assuming
            # ``world_to_voxel``'s boundary epsilon) the extrusion
            # at z = N*h lands in iz = N (when voxel_z == h).  We
            # therefore map iz directly to layer N for the canonical
            # case, and round for the case where voxel_z divides h
            # unevenly.  ``max(1, ...)`` clamps the rare iz=0
            # extrusion (sub-mm undershoot) to layer 1.
            z_floor = iz * self.voxel_z_mm
            layer = max(1, int(round(z_floor / layer_height_mm + _BOUNDARY_EPSILON)))
            out[layer] = out.get(layer, 0.0) + amount
        return out


@dataclass
class VoxelDiff:
    """Per-voxel difference (b_extrude - a_extrude) between two grids.

    A positive delta means grid B has MORE material in that voxel than
    grid A (e.g. an added decoration); a negative delta means grid B
    has LESS material (e.g. a carved-out region).  Voxels present in
    only one grid show up with the full one-sided delta.
    """

    # (ix, iy, iz) -> b - a (mm of filament).
    deltas: dict[tuple[int, int, int], float] = field(default_factory=dict)

    voxel_xy_mm: float = _DEFAULT_VOXEL_XY_MM
    voxel_z_mm: float = _DEFAULT_VOXEL_Z_MM
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0

    @property
    def voxels_added(self) -> int:
        """Voxel count where grid B has strictly more material."""
        return sum(1 for d in self.deltas.values() if d > 0)

    @property
    def voxels_removed(self) -> int:
        """Voxel count where grid B has strictly less material."""
        return sum(1 for d in self.deltas.values() if d < 0)

    @property
    def total_added_mm(self) -> float:
        """Sum of positive deltas — extrusion added by B vs A."""
        return sum(d for d in self.deltas.values() if d > 0)

    @property
    def total_removed_mm(self) -> float:
        """Sum of |negative deltas| — extrusion removed by B vs A."""
        return -sum(d for d in self.deltas.values() if d < 0)

    def removed_voxels_in_region(
        self,
        xy_bounds: tuple[float, float, float, float],
        *,
        epsilon_mm: float = 0.005,
    ) -> dict[tuple[int, int, int], float]:
        """Return removed-material voxels whose center XY lies inside
        ``xy_bounds`` ``(xmin, ymin, xmax, ymax)``.

        ``epsilon_mm`` filters out vanishingly small deltas that come
        from floating-point noise in the rasterizer (a segment whose
        endpoint sits on a voxel boundary will get split with rounding
        error far below print-meaningful scales).  Default 0.005mm of
        filament — well below any single extrusion's per-voxel share
        on a typical FDM print, but high enough to suppress pure
        rounding noise.
        """
        xmin, ymin, xmax, ymax = xy_bounds
        out: dict[tuple[int, int, int], float] = {}
        for key, delta in self.deltas.items():
            if delta >= -epsilon_mm:
                continue
            ix, iy, _iz = key
            cx = self.origin_x_mm + (ix + 0.5) * self.voxel_xy_mm
            cy = self.origin_y_mm + (iy + 0.5) * self.voxel_xy_mm
            if xmin <= cx <= xmax and ymin <= cy <= ymax:
                out[key] = delta
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gcode_to_voxel_grid(
    gcode_path: str,
    voxel_xy_mm: float = _DEFAULT_VOXEL_XY_MM,
    voxel_z_mm: float = _DEFAULT_VOXEL_Z_MM,
    bounds: tuple[float, float, float, float] | None = None,
) -> VoxelGrid:
    """Walk ``gcode_path`` and accumulate extrusion into a sparse voxel grid.

    :param gcode_path: Path to a sliced gcode file.  Must exist.
    :param voxel_xy_mm: XY edge length of one voxel (mm).  Default
        0.4mm matches a typical FDM nozzle.
    :param voxel_z_mm: Z edge length of one voxel (mm).  Default
        0.2mm matches the PLA baseline layer height.
    :param bounds: Optional ``(xmin, ymin, xmax, ymax)`` clip window.
        Voxels outside this XY window are dropped from the grid.
        Useful when you only care about a specific feature (a logo,
        a hole, a colour swap) — keeps the grid small and the diff
        focused.  When ``None`` the grid covers the full XY extent
        of the gcode's extrusion moves.

    :returns: :class:`VoxelGrid` keyed by (ix, iy, iz).

    :raises FileNotFoundError: if ``gcode_path`` doesn't exist.
    :raises ValueError: if ``voxel_xy_mm`` or ``voxel_z_mm`` <= 0.
    """
    if not os.path.isfile(gcode_path):
        raise FileNotFoundError(f"gcode not found: {gcode_path}")
    if voxel_xy_mm <= 0 or voxel_z_mm <= 0:
        raise ValueError(
            f"voxel resolution must be positive (got xy={voxel_xy_mm}, z={voxel_z_mm})",
        )

    # Origin = bounds.min when supplied, else fall back to (0, 0).
    # Using world (0, 0) keeps voxel indices comparable across gcodes
    # that share a build plate origin, which is the common case for
    # before/after diffs of the same part.
    origin_x = float(bounds[0]) if bounds else 0.0
    origin_y = float(bounds[1]) if bounds else 0.0

    grid = VoxelGrid(
        voxel_xy_mm=voxel_xy_mm,
        voxel_z_mm=voxel_z_mm,
        origin_x_mm=origin_x,
        origin_y_mm=origin_y,
    )

    # Position state.  Defaults track a "homed but not yet moved"
    # printer — the first absolute XY in the file establishes the real
    # starting point.  E starts at 0 so the first ``G92 E0`` (common
    # in slicer output) is a no-op.
    cur_x: float = 0.0
    cur_y: float = 0.0
    cur_z: float = 0.0
    cur_e: float = 0.0

    # Mode flags.  ``e_relative`` flips on M83 / off on M82.  Most
    # PrusaSlicer / Bambu output is M83 (relative E) — but we honour
    # both since absolute-E gcode is still common from older slicers.
    e_relative: bool = False

    with open(gcode_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith(";"):
                continue

            # Strip inline comments BEFORE regex extraction so
            # "G1 X10 ; foo" doesn't confuse the X parser.
            if ";" in line:
                line = line.split(";", 1)[0].rstrip()
                if not line:
                    continue

            # Mode commands.  M82 = absolute E, M83 = relative E.
            if _M83_RE.match(line):
                e_relative = True
                continue
            if _M82_RE.match(line):
                e_relative = False
                continue

            # G92 sets axis position without movement.  Only E matters
            # for our purposes (XY/Z resets are rare and don't occur
            # in normal slicer output between layers).
            if _G92_RE.match(line):
                e_match = _E_RE.search(line)
                if e_match:
                    cur_e = float(e_match.group(1))
                continue

            is_g0 = _G0_RE.match(line) is not None
            is_g1 = _G1_RE.match(line) is not None
            if not (is_g0 or is_g1):
                continue

            x_match = _X_RE.search(line)
            y_match = _Y_RE.search(line)
            z_match = _Z_RE.search(line)
            e_match = _E_RE.search(line)

            new_x = float(x_match.group(1)) if x_match else cur_x
            new_y = float(y_match.group(1)) if y_match else cur_y
            new_z = float(z_match.group(1)) if z_match else cur_z

            # Compute extrusion delta.  In relative-E mode the line's E
            # value IS the delta; in absolute-E mode we subtract from
            # the running total.  G0 NEVER extrudes (some slicers emit
            # "G0 E1" for retract-on-travel, but that's a retract, not
            # a print move — treated as zero extrusion).
            if is_g0 or e_match is None:
                de = 0.0
                if e_match is not None and not is_g0:
                    # G1 with E but no XYZ change is a retract/prime —
                    # update the absolute E tracker but don't paint.
                    if e_relative:
                        cur_e += float(e_match.group(1))
                    else:
                        cur_e = float(e_match.group(1))
            else:
                e_val = float(e_match.group(1))
                if e_relative:
                    de = e_val
                    cur_e += e_val
                else:
                    de = e_val - cur_e
                    cur_e = e_val

            # Only paint when this is a positive-extrusion move with
            # actual XYZ travel.  A pure prime (G1 E+ with no XYZ
            # change, restoring filament after a retract) is not a
            # paint — it's a tool-state operation, no material laid
            # down at the head's standing position.  Excluding it
            # matters: the diff between two re-sliced gcodes would
            # otherwise pick up arbitrary prime placement as fake
            # "added material."
            moved = (
                new_x != cur_x or new_y != cur_y or new_z != cur_z
            )
            paint = is_g1 and de > 0 and moved

            if paint:
                _paint_segment(
                    grid,
                    cur_x, cur_y, cur_z,
                    new_x, new_y, new_z,
                    de,
                    bounds=bounds,
                )

            cur_x, cur_y, cur_z = new_x, new_y, new_z

    return grid


def diff_voxel_grids(grid_a: VoxelGrid, grid_b: VoxelGrid) -> VoxelDiff:
    """Return per-voxel ``b - a`` deltas as a :class:`VoxelDiff`.

    The two grids MUST share resolution and origin — otherwise the
    voxel indices don't refer to the same physical regions and the
    diff is meaningless.  We raise ``ValueError`` rather than silently
    returning a garbage diff.

    Voxels present in only one grid are treated as if the other grid
    has 0 in that cell — the natural behaviour for a sparse grid.
    """
    if (
        grid_a.voxel_xy_mm != grid_b.voxel_xy_mm
        or grid_a.voxel_z_mm != grid_b.voxel_z_mm
    ):
        raise ValueError(
            f"Grid resolutions differ: A={grid_a.voxel_xy_mm}/"
            f"{grid_a.voxel_z_mm} mm, B={grid_b.voxel_xy_mm}/"
            f"{grid_b.voxel_z_mm} mm — diff is meaningless.",
        )
    if (
        grid_a.origin_x_mm != grid_b.origin_x_mm
        or grid_a.origin_y_mm != grid_b.origin_y_mm
    ):
        raise ValueError(
            f"Grid origins differ: A=({grid_a.origin_x_mm}, "
            f"{grid_a.origin_y_mm}), B=({grid_b.origin_x_mm}, "
            f"{grid_b.origin_y_mm}) — voxel indices are not comparable.",
        )

    diff = VoxelDiff(
        voxel_xy_mm=grid_a.voxel_xy_mm,
        voxel_z_mm=grid_a.voxel_z_mm,
        origin_x_mm=grid_a.origin_x_mm,
        origin_y_mm=grid_a.origin_y_mm,
    )

    keys = set(grid_a.voxels) | set(grid_b.voxels)
    for key in keys:
        a = grid_a.voxels.get(key, 0.0)
        b = grid_b.voxels.get(key, 0.0)
        delta = b - a
        if delta != 0.0:
            diff.deltas[key] = delta

    return diff


# ---------------------------------------------------------------------------
# Internals — the rasterizer
# ---------------------------------------------------------------------------


def _paint_segment(
    grid: VoxelGrid,
    x0: float, y0: float, z0: float,
    x1: float, y1: float, z1: float,
    extrude_mm: float,
    *,
    bounds: tuple[float, float, float, float] | None,
) -> None:
    """Distribute ``extrude_mm`` of filament along the 3D segment.

    Walks the segment in fixed steps of ``min(voxel_xy, voxel_z) / 2``
    and accumulates extrusion proportional to the step's length share.
    The half-voxel stride guarantees every voxel the segment crosses
    receives at least one sample — the classic Amanatides-Woo DDA is
    overkill for a primitive whose typical input segment is shorter
    than 5 voxels.

    For segments shorter than ``_MIN_SEGMENT_LEN_MM`` we drop the full
    extrusion into the source voxel — segment fraction math would
    divide by zero and the geometric result is the same.
    """
    seg_len = math.sqrt(
        (x1 - x0) * (x1 - x0)
        + (y1 - y0) * (y1 - y0)
        + (z1 - z0) * (z1 - z0)
    )

    grid.total_extruded_mm += extrude_mm

    if seg_len < _MIN_SEGMENT_LEN_MM:
        key = grid.world_to_voxel(x0, y0, z0)
        if _in_bounds(grid, key, bounds):
            grid.voxels[key] = grid.voxels.get(key, 0.0) + extrude_mm
        return

    # Step size = half the smaller voxel edge.  Smaller steps mean
    # finer accounting at the cost of more dict touches; half-voxel
    # is the empirical sweet spot for FDM-scale segments (typically
    # 0.5-30mm).
    stride = min(grid.voxel_xy_mm, grid.voxel_z_mm) * 0.5
    n_steps = max(1, int(math.ceil(seg_len / stride)))
    per_step_extrude = extrude_mm / n_steps

    for i in range(n_steps):
        # Sample at the midpoint of each step.  Midpoint sampling
        # avoids the "endpoint hits the next voxel" off-by-one that
        # endpoint-sampling causes when the segment lands exactly on
        # a voxel boundary.
        t = (i + 0.5) / n_steps
        sx = x0 + (x1 - x0) * t
        sy = y0 + (y1 - y0) * t
        sz = z0 + (z1 - z0) * t
        key = grid.world_to_voxel(sx, sy, sz)
        if _in_bounds(grid, key, bounds):
            grid.voxels[key] = grid.voxels.get(key, 0.0) + per_step_extrude


def _in_bounds(
    grid: VoxelGrid,
    key: tuple[int, int, int],
    bounds: tuple[float, float, float, float] | None,
) -> bool:
    """Voxel-center XY containment check against the optional clip box.

    Z is unbounded — bounds are XY-only because mid-print operations
    typically know the logo's footprint but not its Z extent (which
    is what they're trying to discover via the diff).
    """
    if bounds is None:
        return True
    ix, iy, _iz = key
    cx = grid.origin_x_mm + (ix + 0.5) * grid.voxel_xy_mm
    cy = grid.origin_y_mm + (iy + 0.5) * grid.voxel_xy_mm
    xmin, ymin, xmax, ymax = bounds
    return xmin <= cx <= xmax and ymin <= cy <= ymax


__all__ = [
    "VoxelDiff",
    "VoxelGrid",
    "diff_voxel_grids",
    "gcode_to_voxel_grid",
]
