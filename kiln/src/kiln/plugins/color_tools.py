"""Procedural color assignment plugin — geometry-based multicolor without cloud APIs.

Splits a 3D model into color zones based on geometry (Z-height bands,
face normal clustering, or random assignment) and produces separate STL
files for each zone.  Optionally composes them into a multicolor 3MF
for AMS/MMU printers.

Zero cloud dependencies.  No Meshy, no Gemini, no GPU required.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import math
import os
import random
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80
_STL_TRIANGLE_SIZE = 50  # 12 (normal) + 36 (3 vertices) + 2 (attr)

_DEFAULT_PALETTE = ["#FFFFFF", "#F72323", "#161616", "#898989"]

_PLA_DENSITY_G_PER_CM3 = 1.24
_DEFAULT_INFILL_FACTOR = 0.30

# Rough FDM print-time estimate: weight * this factor (minutes per gram at
# standard speed / 0.2 mm layers / 20 % infill).
_PRINT_TIME_MIN_PER_G = 20.0

# Human-readable zone labels for the "normal" method.
_NORMAL_ZONE_LABELS: dict[int, str] = {
    0: "top",
    1: "bottom",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _Triangle:
    """A single STL triangle with normal, vertices, and attribute byte."""

    normal: tuple[float, float, float]
    v0: tuple[float, float, float]
    v1: tuple[float, float, float]
    v2: tuple[float, float, float]
    attr: int = 0

    @property
    def centroid_z(self) -> float:
        return (self.v0[2] + self.v1[2] + self.v2[2]) / 3.0

    @property
    def centroid(self) -> tuple[float, float, float]:
        return (
            (self.v0[0] + self.v1[0] + self.v2[0]) / 3.0,
            (self.v0[1] + self.v1[1] + self.v2[1]) / 3.0,
            (self.v0[2] + self.v1[2] + self.v2[2]) / 3.0,
        )

    @property
    def area(self) -> float:
        """Triangle area via cross product (mm^2)."""
        ux = self.v1[0] - self.v0[0]
        uy = self.v1[1] - self.v0[1]
        uz = self.v1[2] - self.v0[2]
        vx = self.v2[0] - self.v0[0]
        vy = self.v2[1] - self.v0[1]
        vz = self.v2[2] - self.v0[2]
        cx = uy * vz - uz * vy
        cy = uz * vx - ux * vz
        cz = ux * vy - uy * vx
        return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)

    def raw_bytes(self) -> bytes:
        """Serialize to 50-byte binary STL triangle record."""
        return struct.pack(
            "<12fH",
            *self.normal,
            *self.v0,
            *self.v1,
            *self.v2,
            self.attr,
        )


@dataclass
class _ColorZone:
    """Accumulates triangles assigned to one color zone.

    ``watertight`` is judged only when capping ran (the z_height method):
    ``True`` means the zone is a closed, consistently wound solid;
    ``None`` means the question was never asked (per-facet methods).
    """

    index: int
    color: str
    triangles: list[_Triangle] = field(default_factory=list)
    watertight: bool | None = None

    @property
    def face_count(self) -> int:
        return len(self.triangles)

    @property
    def total_area_mm2(self) -> float:
        return sum(t.area for t in self.triangles)


# ---------------------------------------------------------------------------
# STL parsing / writing (binary + ASCII, stdlib)
# ---------------------------------------------------------------------------


def _parse_ascii_stl(file_path: str) -> list[_Triangle]:
    """Parse an ASCII STL file into a list of triangles.

    Handles both ``solid name`` and bare ``solid`` headers.  Each facet block
    must contain exactly three ``vertex`` lines.

    :raises ValueError: If the file is malformed or no triangles are found.
    """
    triangles: list[_Triangle] = []
    normal: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vertices: list[tuple[float, float, float]] = []

    with open(file_path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("facet normal"):
                parts = line.split()
                # "facet normal nx ny nz"
                normal = (float(parts[2]), float(parts[3]), float(parts[4]))
                vertices = []
            elif line.startswith("vertex"):
                parts = line.split()
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("endfacet"):
                if len(vertices) == 3:
                    triangles.append(
                        _Triangle(
                            normal=normal,
                            v0=vertices[0],
                            v1=vertices[1],
                            v2=vertices[2],
                        )
                    )
                vertices = []

    return triangles


def _parse_binary_stl(file_path: str) -> list[_Triangle]:
    """Parse a binary or ASCII STL file into a list of triangles.

    Tries binary parsing first.  If the header starts with ``solid`` and the
    file size does not match the expected binary layout, falls back to
    :func:`_parse_ascii_stl` automatically.

    :raises ValueError: If the file is too small or truncated.
    """
    path = Path(file_path)
    size = path.stat().st_size

    if size < _STL_HEADER_SIZE + 4:
        raise ValueError(f"File too small for binary STL: {size} bytes")

    triangles: list[_Triangle] = []
    with open(path, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)

        count_bytes = fh.read(4)
        tri_count = struct.unpack("<I", count_bytes)[0]

        expected = _STL_HEADER_SIZE + 4 + tri_count * _STL_TRIANGLE_SIZE
        if header[:5] == b"solid" and expected != size:
            # ASCII STL — delegate to the text parser
            _logger.debug("ASCII STL detected, switching to ASCII parser: %s", file_path)
            return _parse_ascii_stl(file_path)

        if size < expected:
            raise ValueError(
                f"Truncated STL: expected {expected} bytes, got {size}"
            )

        for _ in range(tri_count):
            data = fh.read(_STL_TRIANGLE_SIZE)
            if len(data) < _STL_TRIANGLE_SIZE:
                break
            floats = struct.unpack_from("<12f", data, 0)
            attr = struct.unpack_from("<H", data, 48)[0]
            triangles.append(
                _Triangle(
                    normal=(floats[0], floats[1], floats[2]),
                    v0=(floats[3], floats[4], floats[5]),
                    v1=(floats[6], floats[7], floats[8]),
                    v2=(floats[9], floats[10], floats[11]),
                    attr=attr,
                )
            )

    return triangles


def _write_binary_stl(triangles: list[_Triangle], output_path: str) -> None:
    """Write triangles to a binary STL file."""
    with open(output_path, "wb") as fh:
        # 80-byte header
        fh.write(b"Kiln color_tools" + b"\0" * (_STL_HEADER_SIZE - 16))
        # Triangle count
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(tri.raw_bytes())


# ---------------------------------------------------------------------------
# Color assignment strategies
# ---------------------------------------------------------------------------


def _z_extent(triangles: list[_Triangle]) -> tuple[float, float]:
    """The true vertex Z extent — the model's real bottom and top."""
    zs = [v[2] for t in triangles for v in (t.v0, t.v1, t.v2)]
    return min(zs), max(zs)


def _assign_z_height(
    triangles: list[_Triangle],
    num_colors: int,
    *,
    z_extent: tuple[float, float] | None = None,
) -> list[int]:
    """Assign each triangle to a color zone by Z-height band.

    ``z_extent`` is the (min, max) height the bands divide — defaulting to
    the triangles' VERTEX extent, the model's real bottom and top.  (It
    used to be the centroid extent, which shifted every band edge inward
    on any mesh whose top or bottom faces lean.)  The plane-split path
    passes the extent it cut at, so each piece is judged against the same
    edges it was cut to.
    """
    if not triangles:
        return []

    z_min, z_max = z_extent if z_extent is not None else _z_extent(triangles)
    z_range = z_max - z_min

    if z_range < 1e-9:
        return [0] * len(triangles)

    band_size = z_range / num_colors
    assignments: list[int] = []
    for tri in triangles:
        band = int((tri.centroid_z - z_min) / band_size)
        # Clamp both edges (top-edge exactness; callers may pass a
        # narrower extent than the triangles cover)
        assignments.append(min(max(band, 0), num_colors - 1))
    return assignments


def _assign_normal(
    triangles: list[_Triangle],
    num_colors: int,
) -> list[int]:
    """Assign each triangle to a color zone by face normal direction.

    Strategy:
      - Zone 0: top-facing (normal Z > threshold)
      - Zone 1: bottom-facing (normal Z < -threshold)
      - Remaining zones: side faces divided by azimuth angle
    """
    if not triangles:
        return []

    threshold = 0.5  # ~60 deg from horizontal
    side_zones = max(1, num_colors - 2)
    assignments: list[int] = []

    for tri in triangles:
        nz = tri.normal[2]
        if nz > threshold:
            assignments.append(0)  # top
        elif nz < -threshold:
            assignments.append(min(1, num_colors - 1))  # bottom
        else:
            # Side face — divide by azimuth
            angle = math.atan2(tri.normal[1], tri.normal[0])  # -pi..pi
            normalized = (angle + math.pi) / (2 * math.pi)  # 0..1
            zone = int(normalized * side_zones)
            if zone >= side_zones:
                zone = side_zones - 1
            # Offset by 2 (top=0, bottom=1, sides=2..)
            zone_idx = zone + 2
            if zone_idx >= num_colors:
                zone_idx = num_colors - 1
            assignments.append(zone_idx)

    return assignments


def _assign_random(
    triangles: list[_Triangle],
    num_colors: int,
    *,
    seed: int = 42,
) -> list[int]:
    """Random face assignment — artistic/abstract prints."""
    rng = random.Random(seed)
    return [rng.randint(0, num_colors - 1) for _ in triangles]


# ---------------------------------------------------------------------------
# Band-plane splitting — crisp color boundaries for the z_height method
# ---------------------------------------------------------------------------
#
# Assigning whole triangles to bands makes the color boundary follow the
# triangulation: a face whose centroid sits just above a band edge drags its
# lower corners up into the wrong color, so the printed line where two
# colors meet comes out as a sawtooth — and on a coarse mesh whose faces
# are TALLER than the bands, centroid assignment degenerates into stripes.
# Cutting every crossing triangle exactly at the band heights first makes
# centroid assignment exact: each piece lies wholly inside one band, and
# the boundary is the straight horizontal line the band math names.
#
# The normal/random methods need no analog: a facet has one orientation,
# so per-facet assignment IS exact for them by construction.

#: A vertex this close to a cut plane counts as ON it — touching is not
#: crossing, and near-plane cuts would only mint sliver debris.
_PLANE_EPS = 1e-9

#: Pieces below this area are cut debris, not geometry (mm^2).
_DEGENERATE_AREA_MM2 = 1e-12


def _band_planes(z_min: float, z_max: float, num_colors: int) -> list[float]:
    """The ``num_colors - 1`` interior cut heights dividing [z_min, z_max]."""
    band = (z_max - z_min) / num_colors
    return [z_min + band * i for i in range(1, num_colors)]


def _split_triangle_at_plane(tri: _Triangle, z: float) -> list[_Triangle]:
    """Cut one triangle at the horizontal plane ``z``.

    Returns pieces that exactly cover the input (winding, normal, and
    attribute preserved), or ``[tri]`` untouched when it does not truly
    cross the plane.  Both halves are triangulated from one shared pair of
    intersection points, so the cut edge is watertight by construction —
    and adjacent triangles derive the bit-identical cut point from their
    shared edge: the interpolation always runs from the lexicographically
    smaller endpoint, because the two faces traverse the edge in opposite
    directions and ``a + t*(b - a)`` rounds differently from each end.
    """
    verts = (tri.v0, tri.v1, tri.v2)
    sides = tuple(
        0.0 if abs(v[2] - z) <= _PLANE_EPS else v[2] - z for v in verts
    )
    if all(s >= 0.0 for s in sides) or all(s <= 0.0 for s in sides):
        return [tri]

    below: list[tuple[float, float, float]] = []
    above: list[tuple[float, float, float]] = []
    for i in range(3):
        a, sa = verts[i], sides[i]
        b, sb = verts[(i + 1) % 3], sides[(i + 1) % 3]
        if sa <= 0.0:
            below.append(a)
        if sa >= 0.0:
            above.append(a)
        if (sa < 0.0 < sb) or (sb < 0.0 < sa):
            lo, hi = (a, b) if a <= b else (b, a)
            t = (z - lo[2]) / (hi[2] - lo[2])
            cut = (
                lo[0] + t * (hi[0] - lo[0]),
                lo[1] + t * (hi[1] - lo[1]),
                z,
            )
            below.append(cut)
            above.append(cut)

    pieces: list[_Triangle] = []
    for poly in (below, above):
        for k in range(1, len(poly) - 1):
            piece = _Triangle(
                normal=tri.normal,
                v0=poly[0],
                v1=poly[k],
                v2=poly[k + 1],
                attr=tri.attr,
            )
            if piece.area > _DEGENERATE_AREA_MM2:
                pieces.append(piece)
    # All pieces degenerate (a triangle hugging the plane): keep the
    # original — never trade real area for a cleaner cut.
    return pieces or [tri]


def _split_triangles_at_planes(
    triangles: list[_Triangle], planes: list[float],
) -> list[_Triangle]:
    """Cut triangles at every plane so no output triangle spans one."""
    for z in planes:
        triangles = [
            piece for tri in triangles for piece in _split_triangle_at_plane(tri, z)
        ]
    return triangles


def _band_by_z_height(
    triangles: list[_Triangle], num_colors: int,
) -> tuple[list[_Triangle], list[int], float, list[float]]:
    """The z_height method, whole: split at the band planes, then assign.

    The ONE door both color tools route the method through, so the band
    edges, the cutting, and the assignment can never disagree.  Returns
    ``(triangles, assignments, z_range, cap_planes)`` — the triangles are
    the plane-split set (counts grow at the boundaries), every one of
    them wholly inside its band, ``z_range`` is the vertex-true height
    the bands divide (what the band-height warning judges), and
    ``cap_planes`` is the exact list of heights the faces were cut at
    (empty when nothing was cut) so the zones' cut rings can be capped
    after bucketing at the same heights they were opened at.
    """
    if not triangles:
        return [], [], 0.0, []
    z_min, z_max = _z_extent(triangles)
    z_range = z_max - z_min
    planes: list[float] = []
    if num_colors >= 2 and z_range >= 1e-9:
        planes = _band_planes(z_min, z_max, num_colors)
        triangles = _split_triangles_at_planes(triangles, planes)
    assignments = _assign_z_height(
        triangles, num_colors, z_extent=(z_min, z_max),
    )
    return triangles, assignments, z_range, planes


# ---------------------------------------------------------------------------
# Capping the cut loops — a zone of a closed model is itself a closed solid
# ---------------------------------------------------------------------------
#
# Splitting at the band planes leaves every zone an OPEN shell: its cut
# rings are boundary loops lying exactly at the plane heights, and
# slicers refuse open shells (``manifold = no``).  Each zone is closed by
# chaining its boundary edges at each cut plane into loops and
# triangulating the enclosed region.  Holes stay holes — a hollow part's
# ring cap is an annulus, never a filled disk.  Orientation comes from
# the mesh, not a guess: the shell's winding makes each boundary loop's
# REVERSAL the cap's traversal, so every directed edge of a capped zone
# is used exactly once each way.  Loops that do not close (an input that
# was open to begin with, or a non-manifold seam) are left alone —
# capping never invents geometry the input did not imply.
#
# Loop vertices are compared exactly: the plane split derives identical
# cut points on both sides of every cut, and the two zones meeting at a
# plane share the same loop coordinates, so the caps facing each other
# across a plane cover geometrically identical regions.


def _boundary_edges(
    triangles: list[_Triangle],
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Directed edges used by exactly one face.

    An interior edge of a consistently wound surface appears once as
    ``(a, b)`` and once as ``(b, a)``; a boundary edge has no reverse.
    Edges with any other multiplicity are non-manifold seams — excluded,
    so the loops through them fail to close and stay uncapped.
    """
    counts: dict[tuple, int] = {}
    for t in triangles:
        for a, b in ((t.v0, t.v1), (t.v1, t.v2), (t.v2, t.v0)):
            if a != b:
                counts[(a, b)] = counts.get((a, b), 0) + 1
    return [
        (a, b)
        for (a, b), n in counts.items()
        if n == 1 and (b, a) not in counts
    ]


def _chain_closed_loops(
    edges: list[tuple[tuple[float, float, float], tuple[float, float, float]]],
) -> list[list[tuple[float, float, float]]]:
    """Chain directed edges into closed vertex loops.

    Chains that dead-end are dropped — only a ring the geometry actually
    closed is a cap candidate.  Vertices are matched by exact equality:
    the plane split guarantees shared cut points bit-for-bit, so fuzzy
    merging is neither needed nor wanted.
    """
    successors: dict[tuple, list[tuple]] = {}
    for a, b in edges:
        successors.setdefault(a, []).append(b)

    loops: list[list[tuple[float, float, float]]] = []
    while successors:
        start = next(iter(successors))
        loop: list[tuple[float, float, float]] | None = [start]
        cur = start
        while True:
            nxts = successors.get(cur)
            if not nxts:
                loop = None  # open chain — consumed edges stay dropped
                break
            nxt = nxts.pop()
            if not nxts:
                del successors[cur]
            if nxt == start:
                break
            loop.append(nxt)
            cur = nxt
        if loop is not None and len(loop) >= 3:
            loops.append(loop)
    return loops


def _shoelace(loop: list[tuple[float, float, float]]) -> float:
    """Signed area of the loop projected onto the XY plane."""
    s = 0.0
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i][0], loop[i][1]
        x1, y1 = loop[(i + 1) % n][0], loop[(i + 1) % n][1]
        s += x0 * y1 - x1 * y0
    return 0.5 * s


def _point_in_loop(x: float, y: float, loop: list) -> bool:
    """Even-odd ray-crossing test in XY; loop entries index [0]=x, [1]=y."""
    inside = False
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i][0], loop[i][1]
        x1, y1 = loop[(i + 1) % n][0], loop[(i + 1) % n][1]
        if (y0 > y) != (y1 > y) and (
            x0 + (y - y0) * (x1 - x0) / (y1 - y0) > x
        ):
            inside = not inside
    return inside


def _nest_loops(
    loops: list[list[tuple[float, float, float]]],
) -> list[tuple[list, list[list]]]:
    """Group coplanar loops into ``(outer, holes)`` regions by containment.

    Even containment depth is a region outer, odd depth is a hole in its
    enclosing region — so an annulus caps as a ring, and a ring inside a
    ring's hole becomes its own region (any nesting depth).
    """
    depths: list[int] = []
    for i, lp in enumerate(loops):
        px, py = lp[0][0], lp[0][1]
        depths.append(
            sum(
                1
                for j, other in enumerate(loops)
                if j != i and _point_in_loop(px, py, other)
            )
        )
    regions: list[tuple[list, list[list]]] = []
    for i, lp in enumerate(loops):
        if depths[i] % 2 != 0:
            continue
        holes = [
            loops[j]
            for j in range(len(loops))
            if depths[j] == depths[i] + 1
            and _point_in_loop(loops[j][0][0], loops[j][0][1], lp)
        ]
        regions.append((lp, holes))
    return regions


def _tri_sides(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, px: float, py: float,
) -> tuple[float, float, float]:
    """Signed edge tests of (px, py) against triangle abc."""
    d1 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    d2 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
    d3 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
    return d1, d2, d3


def _strictly_inside_tri(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, px: float, py: float,
) -> bool:
    """True when (px, py) is strictly inside triangle abc (either winding)."""
    d1, d2, d3 = _tri_sides(ax, ay, bx, by, cx, cy, px, py)
    return (d1 > 0 and d2 > 0 and d3 > 0) or (d1 < 0 and d2 < 0 and d3 < 0)


def _inside_tri_closed(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, px: float, py: float,
) -> bool:
    """True when (px, py) is inside or on triangle abc (either winding)."""
    d1, d2, d3 = _tri_sides(ax, ay, bx, by, cx, cy, px, py)
    return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)


def _merge_hole(outer: list, hole: list) -> list | None:
    """Bridge one hole into the outer, returning one weakly simple polygon.

    Working frame: ``outer`` counter-clockwise, ``hole`` clockwise,
    entries ``(wx, wy, original_vertex)``.  The bridge runs from the
    hole's rightmost vertex M to a mutually visible outer vertex found by
    casting a +x ray from M; the bridge edges appear once in each
    direction in the merged polygon, so they cancel in the final surface.
    Returns ``None`` when no visible vertex exists (degenerate input).
    """
    mi = max(range(len(hole)), key=lambda i: hole[i][0])
    mx, my = hole[mi][0], hole[mi][1]

    n = len(outer)
    best: tuple[float, int] | None = None
    for i in range(n):
        x0, y0 = outer[i][0], outer[i][1]
        x1, y1 = outer[(i + 1) % n][0], outer[(i + 1) % n][1]
        if (y0 > my) != (y1 > my):
            xi = x0 + (my - y0) * (x1 - x0) / (y1 - y0)
            if xi >= mx and (best is None or xi < best[0]):
                best = (xi, i)
    if best is None:
        return None
    xi, i = best
    x0, y0 = outer[i][0], outer[i][1]
    x1, y1 = outer[(i + 1) % n][0], outer[(i + 1) % n][1]

    if xi == x0 and my == y0:
        vis = i
    elif xi == x1 and my == y1:
        vis = (i + 1) % n
    else:
        # Candidate: the crossed edge's endpoint with the larger x.  A
        # reflex outer vertex on or inside triangle (M, I, candidate)
        # would block the bridge — among those, the one closest in angle
        # to the ray (then closest to M) is guaranteed mutually visible.
        vis = i if x0 > x1 else (i + 1) % n
        px, py = outer[vis][0], outer[vis][1]
        best_key: tuple[float, float] | None = None
        for j in range(n):
            if j == vis:
                continue
            jx, jy = outer[j][0], outer[j][1]
            if (jx, jy) == (px, py):
                continue  # a bridge duplicate of the candidate itself
            prev = outer[(j - 1) % n]
            nxt = outer[(j + 1) % n]
            reflex = (
                (jx - prev[0]) * (nxt[1] - jy) - (jy - prev[1]) * (nxt[0] - jx)
            ) < 0
            if not reflex:
                continue
            if not _inside_tri_closed(mx, my, xi, my, px, py, jx, jy):
                continue
            dx, dy = jx - mx, jy - my
            dist = math.hypot(dx, dy)
            if dist <= 0.0:
                continue
            key = (abs(dy) / dist, dist)  # angle off the +x ray, then range
            if best_key is None or key < best_key:
                best_key = key
                vis = j
                px, py = jx, jy

    if (outer[vis][0], outer[vis][1]) == (mx, my):
        return None

    rotated = hole[mi:] + hole[:mi]
    return (
        outer[: vis + 1]
        + rotated
        + [rotated[0], outer[vis]]
        + outer[vis + 1 :]
    )


def _ear_clip(poly: list) -> list[tuple] | None:
    """Triangulate a weakly simple CCW polygon by ear clipping.

    Entries are ``(wx, wy, original_vertex)``; emitted triangles are
    ``(v_a, v_b, v_c)`` original vertices in working-frame CCW order.
    Only strictly convex empty ears are clipped, so collinear loop
    vertices (mid-edge cut points) are absorbed into their neighbours'
    ears and every polygon edge is consumed exactly once — the edge
    balance the capped zone's manifoldness rests on.  Returns ``None``
    when no ear exists (self-intersecting or inconsistently wound
    input): a missing cap degrades to an open zone, a wrong cap would
    corrupt the solid.
    """
    verts = list(poly)
    tris: list[tuple] = []
    min_cross = 2.0 * _DEGENERATE_AREA_MM2  # cross == twice the area
    search_from = 0
    while len(verts) > 3:
        n = len(verts)
        clipped = False
        for k in range(n):
            ib = (search_from + k) % n
            ia = (ib - 1) % n
            ic = (ib + 1) % n
            ax, ay = verts[ia][0], verts[ia][1]
            bx, by = verts[ib][0], verts[ib][1]
            cx, cy = verts[ic][0], verts[ic][1]
            cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if cross <= min_cross:
                continue
            blocked = False
            for j in range(n):
                if j in (ia, ib, ic):
                    continue
                px, py = verts[j][0], verts[j][1]
                if (px, py) in ((ax, ay), (bx, by), (cx, cy)):
                    continue  # a bridge duplicate sitting ON a corner
                if _strictly_inside_tri(ax, ay, bx, by, cx, cy, px, py):
                    blocked = True
                    break
                # A vertex exactly ON the clip's new chord (a collinear
                # mid-edge cut point) would be stranded behind it as a
                # T-junction that breaks the zone's edge balance — steer
                # the clip around it instead.
                if (
                    (cx - ax) * (py - ay) - (cy - ay) * (px - ax) == 0.0
                    and min(ax, cx) <= px <= max(ax, cx)
                    and min(ay, cy) <= py <= max(ay, cy)
                ):
                    blocked = True
                    break
            if blocked:
                continue
            tris.append((verts[ia][2], verts[ib][2], verts[ic][2]))
            del verts[ib]
            search_from = ia
            clipped = True
            break
        if not clipped:
            _logger.debug(
                "cap triangulation found no ear (%d vertices left) — "
                "leaving this region uncapped",
                len(verts),
            )
            return None
    a, b, c = verts
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if cross <= min_cross:
        # A degenerate closing triangle means some boundary edge would
        # go unpaired — an open zone is honest, a cracked cap is not.
        _logger.debug(
            "cap triangulation closed on a degenerate triangle — "
            "leaving this region uncapped",
        )
        return None
    tris.append((a[2], b[2], c[2]))
    return tris


def _cap_ring_loops(
    loops: list[list[tuple[float, float, float]]],
) -> list[_Triangle]:
    """Triangulate the coplanar region bounded by ``loops`` into caps.

    The loops arrive in the shell's boundary direction; the cap consumes
    them REVERSED, which lands the standard orientation for free: region
    outers counter-clockwise and holes clockwise when the cap faces +z
    (zone below the plane), the mirror when it faces -z.  The cap normal
    follows the winding by the right-hand rule.
    """
    reversed_loops = [lp[::-1] for lp in loops]
    regions = _nest_loops(reversed_loops)
    caps: list[_Triangle] = []
    for outer, holes in regions:
        area = _shoelace(outer)
        if abs(area) <= _DEGENERATE_AREA_MM2:
            _logger.debug("cap outer loop has no area — skipped")
            continue
        direction = 1.0 if area > 0.0 else -1.0
        # Working frame mirrors y when the cap faces -z, so the
        # triangulation always sees a CCW outer; emitting the mirrored
        # order flips the real winding back to match the -z normal.
        poly = [(v[0], direction * v[1], v) for v in outer]
        consistent = True
        pending_holes: list[list] = []
        for h in holes:
            h_area = _shoelace(h) * direction
            if abs(h_area) <= _DEGENERATE_AREA_MM2:
                continue  # a slit, not a hole
            if h_area > 0.0:
                _logger.debug(
                    "cap hole wound like its outer — inconsistent shell, "
                    "region left uncapped",
                )
                consistent = False
                break
            pending_holes.append([(v[0], direction * v[1], v) for v in h])
        if not consistent:
            continue
        merged: list | None = poly
        for h in sorted(
            pending_holes, key=lambda hp: -max(p[0] for p in hp)
        ):
            merged = _merge_hole(merged, h)
            if merged is None:
                _logger.debug(
                    "cap hole bridge failed — region left uncapped",
                )
                break
        if merged is None:
            continue
        emitted = _ear_clip(merged)
        if emitted is None:
            continue
        normal = (0.0, 0.0, direction)
        caps.extend(
            _Triangle(normal=normal, v0=a, v1=b, v2=c)
            for a, b, c in emitted
        )
    return caps


def _cap_zone_at_planes(
    triangles: list[_Triangle], planes: list[float],
) -> list[_Triangle]:
    """Cap triangles closing a zone's cut rings at the band planes.

    Only boundary edges whose endpoints both lie within ``_PLANE_EPS``
    of a cut plane are candidates; a boundary anywhere else belongs to
    the input's own geometry and is honestly left open.
    """
    if not triangles or not planes:
        return []
    boundary = _boundary_edges(triangles)
    if not boundary:
        return []
    caps: list[_Triangle] = []
    for z in planes:
        ring = [
            e
            for e in boundary
            if abs(e[0][2] - z) <= _PLANE_EPS
            and abs(e[1][2] - z) <= _PLANE_EPS
        ]
        if not ring:
            continue
        loops = _chain_closed_loops(ring)
        if not loops:
            continue
        caps.extend(_cap_ring_loops(loops))
    return [c for c in caps if c.area > _DEGENERATE_AREA_MM2]


def _is_edge_manifold(triangles: list[_Triangle]) -> bool:
    """True when every directed edge is used exactly once each way —
    a closed, consistently wound surface a slicer accepts."""
    if not triangles:
        return False
    counts: dict[tuple, int] = {}
    for t in triangles:
        for a, b in ((t.v0, t.v1), (t.v1, t.v2), (t.v2, t.v0)):
            if a == b:
                return False
            counts[(a, b)] = counts.get((a, b), 0) + 1
    return all(
        n == 1 and counts.get((b, a), 0) == 1
        for (a, b), n in counts.items()
    )


# ---------------------------------------------------------------------------
# Weight estimation
# ---------------------------------------------------------------------------


def _estimate_weight_g(triangles: list[_Triangle]) -> float:
    """Estimate filament weight (grams) for a set of triangles.

    Uses surface-area-based estimate: each triangle contributes
    ``area * layer_height`` of material volume.  This works for open
    shells (color zones are rarely watertight) and is proportional to
    the actual printed material.

    :param triangles: Triangles in the zone.
    :returns: Estimated weight in grams (PLA, 0.2 mm layer height).
    """
    _LAYER_HEIGHT_MM = 0.2
    total_area_mm2 = sum(t.area for t in triangles)
    volume_mm3 = total_area_mm2 * _LAYER_HEIGHT_MM
    volume_cm3 = volume_mm3 / 1000.0
    return round(volume_cm3 * _PLA_DENSITY_G_PER_CM3, 2)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


_MIN_BAND_HEIGHT_MM = 3.0  # < 15 layers at 0.2 mm — warn below this threshold
_LAYER_HEIGHT_REFERENCE_MM = 0.2


def _band_height_warning(z_range: float, num_colors: int) -> str | None:
    """Return a warning string if any Z-height band is too thin for FDM.

    At 0.2 mm layer height, 15 layers = 3 mm.  Bands thinner than 3 mm
    produce only a few colour layers and may not show a visible colour
    change on the finished print.

    :param z_range: Total Z extent of the model in mm.
    :param num_colors: Number of requested colour bands.
    :returns: Warning string, or ``None`` if bands are tall enough.
    """
    if num_colors < 2:
        return None
    band_height = z_range / num_colors
    if band_height < _MIN_BAND_HEIGHT_MM:
        layers = round(band_height / _LAYER_HEIGHT_REFERENCE_MM)
        return (
            f"Band height is {band_height:.1f} mm (~{layers} layers at "
            f"{_LAYER_HEIGHT_REFERENCE_MM} mm).  Bands thinner than "
            f"{_MIN_BAND_HEIGHT_MM} mm may not show a visible colour "
            "change on the finished print.  Consider using fewer colours "
            "or a taller model."
        )
    return None


def _split_and_write(
    triangles: list[_Triangle],
    assignments: list[int],
    num_colors: int,
    palette: list[str],
    output_dir: str,
    base_name: str,
    *,
    cap_planes: list[float] | None = None,
) -> list[_ColorZone]:
    """Bucket triangles by assignment, cap cut rings, write per-zone STLs.

    ``cap_planes`` — the exact heights the z_height method cut at, from
    :func:`_band_by_z_height`.  Each zone's cut rings at those planes
    are closed with flat caps so a zone of a closed input is itself a
    closed solid, and the zone's ``watertight`` verdict is recorded.
    ``None`` (the normal/random methods) writes the buckets as-is:
    those methods produce non-planar zone boundaries no flat cap could
    close.
    """
    zones: list[_ColorZone] = []
    for i in range(num_colors):
        color = palette[i] if i < len(palette) else palette[i % len(palette)]
        zones.append(_ColorZone(index=i, color=color))

    for tri, zone_idx in zip(triangles, assignments, strict=True):
        zones[zone_idx].triangles.append(tri)

    for zone in zones:
        if cap_planes is not None and zone.triangles:
            zone.triangles.extend(
                _cap_zone_at_planes(zone.triangles, cap_planes)
            )
            zone.watertight = _is_edge_manifold(zone.triangles)
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        _write_binary_stl(zone.triangles, stl_path)

    return zones


def _try_compose_3mf(
    zones: list[_ColorZone],
    output_dir: str,
    base_name: str,
    printer_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Attempt to compose a multicolor 3MF.

    :returns: ``(path, error)`` — path on success, error message on failure.
    """
    try:
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
    except ImportError:
        _logger.debug("multicolor_3mf not available — skipping 3MF composition")
        return None, None

    parts: list[ColorPart] = []
    for zone in zones:
        if zone.face_count == 0:
            continue
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        parts.append(
            ColorPart(
                stl_path=stl_path,
                extruder=zone.index + 1,
                name=f"zone_{zone.index}",
                color=zone.color,
            )
        )

    if not parts:
        return None, None

    out_3mf = os.path.join(output_dir, f"{base_name}_multicolor.3mf")
    try:
        result = compose_multicolor_3mf(
            parts, output_path=out_3mf, printer_id=printer_id or None,
        )
        return result.get("output_path", out_3mf), None
    except (ImportError, OSError, ValueError, TypeError) as exc:
        _logger.exception("Failed to compose multicolor 3MF")
        return None, str(exc)


def _try_compose_painted_3mf(
    triangles: list[_Triangle],
    assignments: list[int],
    palette: list[str],
    output_dir: str,
    base_name: str,
    printer_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Compose the painted single-object 3MF for surface-following colorings.

    The normal and random methods color the SURFACE, not stackable bodies —
    splitting the shell along their zones yields zero-thickness sheets no
    slicer accepts (measured: exit 206, "unable to create convex hull").
    The printable form keeps the mesh whole and paints per-triangle color
    references into it; slicers' color-import flows map each color to a
    filament.

    :returns: ``(path, error)`` — path on success, error message on failure.
    """
    try:
        from kiln.multicolor_3mf import compose_painted_3mf
    except ImportError:
        _logger.debug("multicolor_3mf not available — skipping 3MF composition")
        return None, None

    out_3mf = os.path.join(output_dir, f"{base_name}_multicolor.3mf")
    result = compose_painted_3mf(
        [(t.v0, t.v1, t.v2) for t in triangles],
        [palette[a] if a < len(palette) else palette[a % len(palette)]
         for a in assignments],
        output_path=out_3mf,
        name=base_name,
        printer_id=printer_id or None,
    )
    if not result.get("success"):
        return None, str(result.get("error"))
    return result.get("output_path", out_3mf), None


def _hex_to_color_name(hex_color: str) -> str:
    """Return a human-readable color name for common hex values.

    Falls back to the raw hex string for unrecognized colors.
    """
    _COLOR_MAP: dict[str, str] = {
        "#ffffff": "white",
        "#f72323": "red",
        "#161616": "black",
        "#898989": "grey",
        "#000000": "black",
        "#ff0000": "red",
        "#00ff00": "green",
        "#0000ff": "blue",
        "#ffff00": "yellow",
        "#ff8800": "orange",
        "#ff00ff": "magenta",
        "#00ffff": "cyan",
        "#808080": "gray",
        "#c0c0c0": "silver",
        "#ffa500": "orange",
        "#800000": "maroon",
        "#008000": "dark green",
        "#000080": "navy",
        "#800080": "purple",
        "#ffc0cb": "pink",
        "#a52a2a": "brown",
    }
    return _COLOR_MAP.get(hex_color.lower(), hex_color)


def _zone_label(zone_index: int, method: str) -> str:
    """Return a human-readable label for a zone based on method and index."""
    if method == "normal":
        return _NORMAL_ZONE_LABELS.get(zone_index, f"side{zone_index - 1}")
    return f"zone{zone_index}"


def _build_summary(
    active_zones: list[_ColorZone],
    zone_weights: list[float],
    total_weight: float,
    method: str,
) -> str:
    """Build a one-line summary of the color assignment result.

    Example: "4 color zones: top (white, 30%), sides (red, 40%), ..."
    """
    if not active_zones or total_weight < 1e-9:
        return f"{len(active_zones)} color zones"

    parts: list[str] = []
    for zone, weight in zip(active_zones, zone_weights, strict=True):
        label = _zone_label(zone.index, method)
        color_name = _hex_to_color_name(zone.color)
        pct = round(weight / total_weight * 100)
        parts.append(f"{label} ({color_name}, {pct}%)")

    count = len(active_zones)
    return f"{count} color zone{'s' if count != 1 else ''}: {', '.join(parts)}"


def _build_result(
    zones: list[_ColorZone],
    output_dir: str,
    base_name: str,
    method: str,
    total_faces: int,
    threemf_path: str | None,
    *,
    compose_3mf_error: str | None = None,
    band_warning: str | None = None,
) -> dict[str, Any]:
    """Build the standard return dict."""
    # Only include zones that have actual geometry — skip empties.
    active_zones = [z for z in zones if z.face_count > 0]

    zone_weights = [_estimate_weight_g(z.triangles) for z in active_zones]
    total_weight_g = round(sum(zone_weights), 2)
    print_time_estimate_min = round(total_weight_g * _PRINT_TIME_MIN_PER_G, 1)

    zone_details: list[dict[str, Any]] = []
    for slot_num, (zone, weight) in enumerate(
        zip(active_zones, zone_weights, strict=True), start=1
    ):
        stl_path = os.path.join(output_dir, f"{base_name}_zone{zone.index}.stl")
        detail: dict[str, Any] = {
            "zone": zone.index,
            "color": zone.color,
            "face_count": zone.face_count,
            "stl_path": stl_path,
            "ams_slot": slot_num,
            "estimated_weight_g": weight,
        }
        if zone.watertight is not None:
            detail["watertight"] = zone.watertight
        zone_details.append(detail)

    result: dict[str, Any] = {
        "success": True,
        "method": method,
        "total_faces": total_faces,
        "num_colors": len(active_zones),
        "total_weight_g": total_weight_g,
        "print_time_estimate_min": print_time_estimate_min,
        "summary": _build_summary(active_zones, zone_weights, total_weight_g, method),
        "zones": zone_details,
        "ams_mapping": {
            f"slot_{i + 1}": z.color
            for i, z in enumerate(active_zones)
        },
    }

    if threemf_path:
        result["multicolor_3mf"] = threemf_path
        result["next_step"] = (
            f"Multicolor 3MF is ready at {threemf_path}.  "
            "Call slice_model(input_path=<multicolor_3mf>) to slice it, "
            "then upload_file() + start_print() to send to your AMS printer."
        )
        result["next_action"] = {
            "tool": "slice_model",
            "args": {"input_path": threemf_path},
        }
    else:
        result["next_step"] = (
            "Call compose_multicolor_3mf with the zone STL paths to build a "
            "multicolor 3MF, then slice_model() to slice, then "
            "upload_file() + start_print() to print."
        )
        result["next_action"] = {
            "hint": "Use compose_multicolor_3mf to combine zone STLs, "
            "or slice each zone separately.",
        }

    if band_warning:
        result["warning"] = band_warning

    if compose_3mf_error:
        result["compose_3mf_error"] = compose_3mf_error

    return result


# ---------------------------------------------------------------------------
# OBJ / MTL parsing + texture sampling
# ---------------------------------------------------------------------------


@dataclass
class _ObjFace:
    """A parsed OBJ face with vertex indices, UV indices, and material name."""

    vertex_indices: list[int]
    uv_indices: list[int]
    material: str


def _parse_obj(obj_path: str) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float]],
    list[_ObjFace],
]:
    """Parse an OBJ file into vertices, UV coords, and faces.

    :param obj_path: Path to the .obj file.
    :returns: ``(vertices, uvs, faces)`` where vertices are 3-tuples,
        uvs are 2-tuples, and faces carry vertex/UV indices (0-based)
        plus the active material name.
    :raises ValueError: If the file cannot be read or is empty.
    """
    vertices: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    faces: list[_ObjFace] = []
    current_material = ""

    try:
        with open(obj_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split()
                tag = parts[0]

                if tag == "v" and len(parts) >= 4:
                    vertices.append(
                        (float(parts[1]), float(parts[2]), float(parts[3]))
                    )
                elif tag == "vt" and len(parts) >= 3:
                    uvs.append((float(parts[1]), float(parts[2])))
                elif tag == "usemtl":
                    current_material = parts[1] if len(parts) > 1 else ""
                elif tag == "f":
                    v_idx: list[int] = []
                    uv_idx: list[int] = []
                    for token in parts[1:]:
                        # Formats: v, v/vt, v/vt/vn, v//vn
                        components = token.split("/")
                        v_idx.append(int(components[0]) - 1)  # OBJ is 1-based
                        if len(components) >= 2 and components[1]:
                            uv_idx.append(int(components[1]) - 1)
                    faces.append(
                        _ObjFace(
                            vertex_indices=v_idx,
                            uv_indices=uv_idx,
                            material=current_material,
                        )
                    )
    except OSError as exc:
        raise ValueError(f"Cannot read OBJ file: {exc}") from exc

    return vertices, uvs, faces


def _parse_mtl(mtl_path: str) -> dict[str, str]:
    """Parse an MTL file to extract texture image paths per material.

    Looks for ``map_Kd`` (diffuse texture map) directives.

    :param mtl_path: Path to the .mtl file.
    :returns: ``{material_name: texture_path}`` — paths are relative
        to the MTL file's directory.
    """
    textures: dict[str, str] = {}
    current_name = ""

    try:
        with open(mtl_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("newmtl "):
                    current_name = line.split(None, 1)[1]
                elif line.startswith("map_Kd ") and current_name:
                    tex_file = line.split(None, 1)[1]
                    textures[current_name] = tex_file
    except OSError:
        _logger.debug("Cannot read MTL file: %s", mtl_path)

    return textures


def _find_mtl_path(obj_path: str) -> str | None:
    """Discover the MTL file referenced by an OBJ.

    Checks the ``mtllib`` directive inside the OBJ first, then falls
    back to ``<basename>.mtl`` in the same directory.
    """
    obj_dir = os.path.dirname(obj_path)

    try:
        with open(obj_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("mtllib "):
                    mtl_name = line.split(None, 1)[1]
                    candidate = os.path.join(obj_dir, mtl_name)
                    if os.path.isfile(candidate):
                        return candidate
    except OSError:
        pass

    # Fallback: same stem with .mtl extension
    stem = Path(obj_path).stem
    fallback = os.path.join(obj_dir, f"{stem}.mtl")
    if os.path.isfile(fallback):
        return fallback

    return None


def _sample_face_color(
    face: _ObjFace,
    uvs: list[tuple[float, float]],
    img: Any,
    img_width: int,
    img_height: int,
) -> tuple[int, int, int]:
    """Sample the texture at the centroid UV of a face.

    :returns: ``(r, g, b)`` tuple in 0-255 range.
    """
    if not face.uv_indices or not uvs:
        return (128, 128, 128)  # neutral grey fallback

    # Average UV coordinates of the face
    u_sum = 0.0
    v_sum = 0.0
    count = 0
    for idx in face.uv_indices:
        if 0 <= idx < len(uvs):
            u_sum += uvs[idx][0]
            v_sum += uvs[idx][1]
            count += 1

    if count == 0:
        return (128, 128, 128)

    u = u_sum / count
    v = v_sum / count

    # Wrap to [0, 1] via modulo (standard UV tiling behavior)
    u = u % 1.0
    v = v % 1.0

    # OBJ UV: (0,0) is bottom-left; image pixels: (0,0) is top-left
    px = int(u * (img_width - 1))
    py = int((1.0 - v) * (img_height - 1))
    px = max(0, min(img_width - 1, px))
    py = max(0, min(img_height - 1, py))

    pixel = img.getpixel((px, py))
    if isinstance(pixel, (tuple, list)):
        return (pixel[0], pixel[1], pixel[2])
    # Greyscale
    return (pixel, pixel, pixel)


def _quantize_colors(
    face_colors: list[tuple[int, int, int]],
    num_colors: int,
) -> tuple[list[int], list[tuple[int, int, int]]]:
    """Assign each face to one of ``num_colors`` dominant color clusters.

    Uses a simple iterative k-means on RGB values.  No external
    dependencies beyond stdlib + the face color list.

    :returns: ``(assignments, centroids)`` where assignments[i] is the
        cluster index for face i, and centroids are the final RGB centers.
    """
    if not face_colors:
        return [], []

    num_colors = min(num_colors, len(face_colors))

    # Seed centroids: pick evenly spaced samples from the face list
    step = max(1, len(face_colors) // num_colors)
    centroids = [face_colors[i * step] for i in range(num_colors)]

    # Deduplicate centroids if step caused collisions
    seen: set[tuple[int, int, int]] = set()
    unique_centroids: list[tuple[int, int, int]] = []
    for c in centroids:
        if c not in seen:
            seen.add(c)
            unique_centroids.append(c)
    # If fewer unique colors than requested, clamp — don't pad with
    # synthetic grey values that would steal faces from real clusters.
    num_colors = len(unique_centroids)
    centroids = unique_centroids

    assignments = [0] * len(face_colors)

    for _iteration in range(20):
        # Assign each face to the nearest centroid
        changed = False
        for i, color in enumerate(face_colors):
            best_dist = float("inf")
            best_idx = 0
            for j, cent in enumerate(centroids):
                dr = color[0] - cent[0]
                dg = color[1] - cent[1]
                db = color[2] - cent[2]
                dist = dr * dr + dg * dg + db * db
                if dist < best_dist:
                    best_dist = dist
                    best_idx = j
            if assignments[i] != best_idx:
                changed = True
                assignments[i] = best_idx

        if not changed:
            break

        # Update centroids
        sums = [[0, 0, 0] for _ in range(num_colors)]
        counts = [0] * num_colors
        for i, color in enumerate(face_colors):
            ci = assignments[i]
            sums[ci][0] += color[0]
            sums[ci][1] += color[1]
            sums[ci][2] += color[2]
            counts[ci] += 1

        for j in range(num_colors):
            if counts[j] > 0:
                centroids[j] = (
                    sums[j][0] // counts[j],
                    sums[j][1] // counts[j],
                    sums[j][2] // counts[j],
                )

    return assignments, centroids


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert an (R, G, B) tuple to a ``#RRGGBB`` hex string."""
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _obj_face_to_triangle(
    face: _ObjFace,
    vertices: list[tuple[float, float, float]],
) -> list[_Triangle]:
    """Convert an OBJ face (possibly a quad+) into triangles via fan triangulation.

    :returns: List of triangles. Empty if face has fewer than 3 vertices.
    """
    idx = face.vertex_indices
    if len(idx) < 3:
        return []

    v = [vertices[i] for i in idx if 0 <= i < len(vertices)]
    if len(v) < 3:
        return []

    tris: list[_Triangle] = []
    for i in range(1, len(v) - 1):
        # Compute face normal via cross product
        ux = v[i][0] - v[0][0]
        uy = v[i][1] - v[0][1]
        uz = v[i][2] - v[0][2]
        vx = v[i + 1][0] - v[0][0]
        vy = v[i + 1][1] - v[0][1]
        vz = v[i + 1][2] - v[0][2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length > 1e-12:
            nx /= length
            ny /= length
            nz /= length

        tris.append(
            _Triangle(
                normal=(nx, ny, nz),
                v0=v[0],
                v1=v[i],
                v2=v[i + 1],
            )
        )
    return tris


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _ColorToolsPlugin:
    """Procedural color assignment — geometry-based multicolor for AMS/MMU.

    Tools:
        - auto_color_by_height
        - auto_color_by_region
    """

    @property
    def name(self) -> str:
        return "color_tools"

    @property
    def description(self) -> str:
        return "Procedural color assignment — geometry-based multicolor without cloud APIs"

    def register(self, mcp: Any) -> None:
        """Register color assignment tools with the MCP server."""

        @mcp.tool()
        def auto_color_by_height(
            input_path: str,
            num_colors: int = 4,
            color_palette: list[str] | None = None,
            printer_id: str = "",
        ) -> dict:
            """Split a 3D model into horizontal color zones by Z-height.

            Divides the model's height into N equal bands.  Faces that
            cross a band edge are cut exactly at it, so the line where two
            colors meet is the straight horizontal line the bands name —
            never a sawtooth of whole faces, on any tessellation.  The
            cut faces are capped at the band planes, so each zone of a
            closed model is itself a closed solid ready to slice.
            Produces separate STL files per zone and (if available) a
            multicolor 3MF ready for AMS/MMU printers.

            Zero cloud dependencies — pure geometry.

            :param input_path: Path to a binary STL file.
            :param num_colors: Number of color zones (default 4).
            :param color_palette: List of hex colors (e.g.
                ``["#FF0000", "#00FF00"]``).  Defaults to white/red/black/grey.
            :param printer_id: Optional supported printer model id.  Names
                the bed the composed 3MF is placed on, so a model sitting
                off the plate is centred on the machine's real bed rather
                than an assumed 256mm one.
            :returns: Dict with zone STL paths, hex colors, face counts
                (boundary faces are cut and capped, so counts can exceed
                the input's), per-zone ``watertight`` verdicts, AMS slot
                mapping, weight estimates, and optional 3MF path.
            """
            path = Path(input_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {input_path}"}

            if num_colors < 1:
                return {"success": False, "error": "num_colors must be >= 1"}

            palette = color_palette or _DEFAULT_PALETTE
            if len(palette) < num_colors:
                # Cycle palette to fill
                palette = [palette[i % len(palette)] for i in range(num_colors)]

            try:
                triangles = _parse_binary_stl(input_path)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not triangles:
                return {"success": False, "error": "No triangles found in STL"}

            output_dir = tempfile.mkdtemp(prefix="kiln_color_")
            base_name = path.stem

            triangles, assignments, z_range, cap_planes = _band_by_z_height(
                triangles, num_colors,
            )
            zones = _split_and_write(
                triangles, assignments, num_colors, palette, output_dir,
                base_name, cap_planes=cap_planes,
            )
            threemf_path, compose_err = _try_compose_3mf(
                zones, output_dir, base_name, printer_id=printer_id,
            )

            warn = _band_height_warning(z_range, num_colors)

            response = _build_result(
                zones, output_dir, base_name, "z_height",
                len(triangles), threemf_path,
                compose_3mf_error=compose_err,
                band_warning=warn,
            )
            try:
                from kiln_pro.plugins.git_render_tools import (
                    attach_inspect_bundle,
                )

                return attach_inspect_bundle(
                    response, level="quick", stl_keys=("multicolor_3mf",),
                )
            except ImportError:
                return response

        @mcp.tool()
        def auto_color_by_region(
            input_path: str,
            num_colors: int = 4,
            method: str = "z_height",
            color_palette: list[str] | None = None,
            printer_id: str = "",
        ) -> dict:
            """Split a 3D model into color zones by geometric region.

            Supports multiple assignment methods:
              - ``"z_height"``: horizontal bands by Z-height (default) —
                faces crossing a band edge are cut exactly at it and the
                cuts are capped, so the color boundary is a straight line
                on any tessellation and each zone of a closed model is
                itself a closed solid ready to slice
              - ``"normal"``: group by face normal direction
                (top / bottom / sides)
              - ``"random"``: random face assignment for artistic prints

            The 3MF takes whichever form actually prints: z_height bands
            become one closed solid per color; normal/random colorings
            follow the surface, so the mesh stays ONE watertight object
            with the colors painted per triangle — slicers that support
            color import (BambuStudio, OrcaSlicer) offer to map each
            color to a filament on open.

            Zero cloud dependencies — pure geometry.

            :param input_path: Path to a binary STL file.
            :param num_colors: Number of color zones (default 4).
            :param method: Assignment method — ``"z_height"``,
                ``"normal"``, or ``"random"``.
            :param color_palette: List of hex colors.  Defaults to
                white/red/black/grey.
            :param printer_id: Optional supported printer model id.  Names
                the bed the composed 3MF is placed on, so a model sitting
                off the plate is centred on the machine's real bed rather
                than an assumed 256mm one.
            :returns: Dict with zone STL paths, hex colors, face counts,
                AMS slot mapping, weight estimates, and optional 3MF path.
            """
            valid_methods = {"z_height", "normal", "random"}
            if method not in valid_methods:
                return {
                    "success": False,
                    "error": f"Unknown method '{method}'. Choose from: {sorted(valid_methods)}",
                }

            path = Path(input_path)
            if not path.exists():
                return {"success": False, "error": f"File not found: {input_path}"}

            if num_colors < 1:
                return {"success": False, "error": "num_colors must be >= 1"}

            palette = color_palette or _DEFAULT_PALETTE
            if len(palette) < num_colors:
                palette = [palette[i % len(palette)] for i in range(num_colors)]

            try:
                triangles = _parse_binary_stl(input_path)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not triangles:
                return {"success": False, "error": "No triangles found in STL"}

            output_dir = tempfile.mkdtemp(prefix="kiln_color_")
            base_name = path.stem

            z_range: float | None = None
            cap_planes: list[float] | None = None
            if method == "z_height":
                triangles, assignments, z_range, cap_planes = _band_by_z_height(
                    triangles, num_colors,
                )
            elif method == "normal":
                # Per-facet by construction — a facet has ONE orientation,
                # so there is no plane to cut at and nothing to split.
                assignments = _assign_normal(triangles, num_colors)
            else:
                assignments = _assign_random(triangles, num_colors)

            zones = _split_and_write(
                triangles, assignments, num_colors, palette, output_dir,
                base_name, cap_planes=cap_planes,
            )
            if method == "z_height":
                # Stacked bands stand as closed solids — one object per color.
                threemf_path, compose_err = _try_compose_3mf(
                    zones, output_dir, base_name, printer_id=printer_id,
                )
            else:
                # Surface-following colorings have no solid per color; the
                # printable form is the whole mesh painted per triangle.
                threemf_path, compose_err = _try_compose_painted_3mf(
                    triangles, assignments, palette, output_dir, base_name,
                    printer_id=printer_id,
                )

            warn: str | None = None
            if z_range is not None:
                warn = _band_height_warning(z_range, num_colors)

            response = _build_result(
                zones, output_dir, base_name, method,
                len(triangles), threemf_path,
                compose_3mf_error=compose_err,
                band_warning=warn,
            )
            try:
                from kiln_pro.plugins.git_render_tools import (
                    attach_inspect_bundle,
                )

                return attach_inspect_bundle(
                    response, level="quick", stl_keys=("multicolor_3mf",),
                )
            except ImportError:
                return response

        _logger.debug("Registered color tools")


plugin = _ColorToolsPlugin()
