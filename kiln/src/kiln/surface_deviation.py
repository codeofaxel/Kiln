"""How far one triangle surface lies from another, measured at sampled points.

:func:`surface_deviation` lays points over each surface (area-weighted random
points plus the surface's own corners, from a fixed seed, so the same two
meshes always give the same answer) and measures each point's distance to the
nearest point on the OTHER surface's triangles: the true closest point on a
triangle, which may lie inside it or on an edge, never a corner or a centre
standing in for it.  The answer is the largest of those distances in either
direction.  It is sampled: a spike narrower than the spacing of the points can
be larger than the answer.

:func:`nearest_surface_distances` is exact for every point it is given, and
needs numpy alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

#: Surface area each random sample stands for: one point per this many mm2.
_SAMPLE_AREA_MM2 = 0.0625
#: Random points laid over one surface, at least and at most.
_MIN_AREA_SAMPLES = 1_000
_MAX_AREA_SAMPLES = 20_000
#: Most corners of one surface measured; past it a seeded subset is.
_MAX_CORNER_SAMPLES = 200_000
#: Most pieces the grid may cut big triangles into, at least.
_MIN_PIECE_BUDGET = 250_000
#: Most (point, triangle) pairs held at once while measuring.
_PAIRS_PER_BATCH = 500_000
#: Triangles read at a time where a whole-mesh array would be large.
_CHUNK = 1 << 18
#: Triangle radii, as percentiles, a grid may be built for.
_LIMIT_PERCENTILES = (50, 60, 70, 80, 90, 95, 99)
#: Most grid cells along one axis, so a cell key stays inside int64.
_MAX_CELLS_PER_AXIS = 1 << 20
#: Cells searched out from a point's own cell, in turn, before a coarser
#: grid takes over the points still undecided.
_REACHES = (1, 2, 4)
#: How much wider each coarser grid's cells are.
_COARSER = 8.0
#: Seed of the sample points.
_SEED = 20240613


@dataclass(frozen=True)
class SurfaceDeviation:
    """The largest distance between two surfaces at the points measured."""

    max_mm: float
    #: Points measured, both directions together.
    samples: int

    def to_dict(self) -> dict[str, Any]:
        return {"max_mm": self.max_mm, "samples": self.samples}


def surface_deviation(
    vertices_a: np.ndarray,
    faces_a: np.ndarray,
    vertices_b: np.ndarray,
    faces_b: np.ndarray,
) -> SurfaceDeviation:
    """The largest distance from a sampled point of either surface to the other surface."""
    rng = np.random.default_rng(_SEED)
    points_a = sample_surface(vertices_a, faces_a, rng)
    points_b = sample_surface(vertices_b, faces_b, rng)
    a_to_b = nearest_surface_distances(points_a, vertices_b, faces_b)
    b_to_a = nearest_surface_distances(points_b, vertices_a, faces_a)
    largest = max(float(a_to_b.max(initial=0.0)), float(b_to_a.max(initial=0.0)))
    return SurfaceDeviation(max_mm=largest, samples=len(points_a) + len(points_b))


def sample_surface(vertices: np.ndarray, faces: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Points on the triangles ``vertices[faces]``: random points, one per
    :data:`_SAMPLE_AREA_MM2` of area within the sample limits, and the
    corners the faces use (a seeded subset past :data:`_MAX_CORNER_SAMPLES`)."""
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) == 0:
        return np.empty((0, 3))
    area = np.empty(len(faces))
    for start in range(0, len(faces), _CHUNK):
        tri = vertices[faces[start:start + _CHUNK]]
        area[start:start + len(tri)] = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    total = float(area.sum())
    used = np.unique(faces)
    if len(used) > _MAX_CORNER_SAMPLES:
        used = rng.choice(used, size=_MAX_CORNER_SAMPLES, replace=False)
    corners = vertices[used]
    if not total > 0.0:
        return corners
    count = int(min(_MAX_AREA_SAMPLES, max(_MIN_AREA_SAMPLES, total / _SAMPLE_AREA_MM2)))
    tri = vertices[faces[rng.choice(len(faces), size=count, p=area / total)]]
    root = np.sqrt(rng.random(count))
    w = rng.random(count)
    inside = (
        (1.0 - root)[:, None] * tri[:, 0]
        + (root * (1.0 - w))[:, None] * tri[:, 1]
        + (root * w)[:, None] * tri[:, 2]
    )
    return np.concatenate([inside, corners])


def nearest_surface_distances(points: np.ndarray, vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Distance from each point to the nearest point on the triangles
    ``vertices[faces]``, exact; ``inf`` for every point when there are no
    triangles.  A triangle with two corners at one place has no surface and
    is left out."""
    points = np.asarray(points, dtype=np.float64)
    out = np.full(len(points), np.inf)
    triangles = _Triangles(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64).reshape(-1, 3))
    if len(points) == 0 or len(triangles.faces) == 0:
        return out
    pending = np.arange(len(points))
    floor = 0.0
    while len(pending):
        grid = _TriangleGrid(triangles, points, floor)
        pending = grid.search(points, pending, out)
        floor = grid.limit * _COARSER
    return out


class _Triangles:
    """Triangles as ``vertices[faces]``, each with its centre and the
    distance from it to its farthest corner."""

    def __init__(self, vertices: np.ndarray, faces: np.ndarray) -> None:
        centre = np.empty((len(faces), 3))
        radius = np.empty(len(faces))
        flat = np.zeros(len(faces), dtype=bool)
        for start in range(0, len(faces), _CHUNK):
            tri = vertices[faces[start:start + _CHUNK]]
            span = slice(start, start + len(tri))
            centre[span] = tri.mean(axis=1)
            radius[span] = _radii(tri, centre[span])
            a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
            flat[span] = (a == b).all(axis=1) | (b == c).all(axis=1) | (c == a).all(axis=1)
        kept = ~flat
        self.vertices = vertices
        self.faces = faces[kept] if flat.any() else faces
        self.centre = centre[kept] if flat.any() else centre
        self.radius = radius[kept] if flat.any() else radius


def _shell_offsets(inner: int, outer: int) -> np.ndarray:
    """Cell offsets at most *outer* cells away on every axis and more than
    *inner* on some axis (``inner=-1``: the point's own cell too)."""
    span = np.arange(-outer, outer + 1)
    grid = np.stack(np.meshgrid(span, span, span, indexing="ij"), axis=-1).reshape(-1, 3)
    return grid[np.abs(grid).max(axis=1) > inner]


class _TriangleGrid:
    """Triangles filed by their centres, in cells twice as wide as the
    farthest any triangle's corner lies from its centre (:attr:`limit`).  A
    triangle wider than that is filed as the pieces of its edge-midpoint
    split, which cover the same surface.

    A triangle within ``d`` of a point has its centre within ``d +``
    :attr:`limit` of it, so once the cells searched reach past that, the
    nearest triangle has been seen.
    """

    def __init__(self, triangles: _Triangles, points: np.ndarray, floor: float) -> None:
        radius = triangles.radius
        lo = np.minimum(triangles.vertices.min(axis=0), points.min(axis=0))
        hi = np.maximum(triangles.vertices.max(axis=0), points.max(axis=0))
        extent = float(max((hi - lo).max(), 1e-9))
        limit = _cell_limit(radius, max(extent / _MAX_CELLS_PER_AXIS, floor, 1e-9))
        big = radius > limit
        if big.any():
            pieces = _split_until(triangles.vertices[triangles.faces[big]], limit)
            piece_centre = pieces.mean(axis=1)
            self.faces = triangles.faces[~big]
            self.centres = np.concatenate([triangles.centre[~big], piece_centre])
            self.radii = np.concatenate([radius[~big], _radii(pieces, piece_centre)])
        else:
            pieces = np.empty((0, 3, 3))
            self.faces, self.centres, self.radii = triangles.faces, triangles.centre, radius
        self.vertices = triangles.vertices
        self.pieces = pieces
        self.small = len(self.faces)
        self.limit = limit
        self.cell = 2.0 * limit
        self.lo = lo - self.cell
        self.dims = np.floor((hi - self.lo) / self.cell).astype(np.int64) + 2
        keys = self._key(self._cells(self.centres))
        self.order = np.argsort(keys, kind="stable")
        self.cell_keys, self.cell_first, self.cell_count = np.unique(
            keys[self.order], return_index=True, return_counts=True,
        )

    def _cells(self, xyz: np.ndarray) -> np.ndarray:
        return np.floor((xyz - self.lo) / self.cell).astype(np.int64)

    def _key(self, cells: np.ndarray) -> np.ndarray:
        return (cells[..., 0] * self.dims[1] + cells[..., 1]) * self.dims[2] + cells[..., 2]

    def search(self, points: np.ndarray, pending: np.ndarray, out: np.ndarray) -> np.ndarray:
        """Lower ``out`` over the cells around each pending point, ever wider;
        return the points whose nearest triangle may lie farther out."""
        pending = pending[np.argsort(self._key(self._cells(points[pending])), kind="stable")]
        inner = -1
        for reach in _REACHES:
            shell = _shell_offsets(inner, reach)
            step = max(1, _PAIRS_PER_BATCH // len(shell))
            for start in range(0, len(pending), step):
                self._measure(points, pending[start:start + step], shell, out)
            if reach + 1 >= int(self.dims.max()):
                return pending[:0]
            pending = pending[out[pending] + self.limit >= reach * self.cell]
            if not len(pending):
                return pending
            inner = reach
        return pending

    def _corners(self, ids: np.ndarray) -> np.ndarray:
        out = np.empty((len(ids), 3, 3))
        small = ids < self.small
        out[small] = self.vertices[self.faces[ids[small]]]
        out[~small] = self.pieces[ids[~small] - self.small]
        return out

    def _measure(self, points: np.ndarray, batch: np.ndarray, shell: np.ndarray, out: np.ndarray) -> None:
        """Lower ``out[batch]`` to the distance to each triangle filed in the
        *shell* cells around each point, :data:`_PAIRS_PER_BATCH` pairs at a time."""
        cells = self._cells(points[batch])[:, None, :] + shell[None, :, :]
        valid = ((cells >= 0) & (cells < self.dims)).all(axis=2)
        owner = np.broadcast_to(batch[:, None], valid.shape)[valid]
        keys = self._key(cells[valid])
        at = np.minimum(np.searchsorted(self.cell_keys, keys), len(self.cell_keys) - 1)
        filled = self.cell_keys[at] == keys
        if not filled.any():
            return
        owner, at = owner[filled], at[filled]
        first, count = self.cell_first[at], self.cell_count[at]
        ends = np.cumsum(count)
        cuts = np.searchsorted(ends, np.arange(_PAIRS_PER_BATCH, int(ends[-1]), _PAIRS_PER_BATCH), side="left")
        for lo, hi in zip(np.r_[0, cuts + 1], np.r_[cuts + 1, len(count)], strict=True):
            if hi > lo:
                self._measure_cells(points, owner[lo:hi], first[lo:hi], count[lo:hi], out)

    def _measure_cells(
        self, points: np.ndarray, owner: np.ndarray, first: np.ndarray, count: np.ndarray, out: np.ndarray,
    ) -> None:
        """Lower ``out`` for each ``owner`` point to its distance from the
        triangles at sorted positions ``first .. first + count``."""
        from kiln.decoration_faces import _expand_ranges, _point_triangle_distances

        tri = self.order[_expand_ranges(first, count)]
        owner = np.repeat(owner, count)
        to_centre = np.linalg.norm(points[owner] - self.centres[tri], axis=1)
        # A triangle's centre lies on it, so the nearest centre bounds each
        # point's distance; a triangle whose every point is farther is skipped.
        starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
        bound = np.minimum(np.minimum.reduceat(to_centre, starts), out[owner[starts]])
        near = to_centre - self.radii[tri] <= np.repeat(bound, np.diff(np.r_[starts, len(owner)]))
        owner, tri = owner[near], tri[near]
        if len(tri) == 0:
            return
        distance = _point_triangle_distances(points[owner], self._corners(tri))
        starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
        heads = owner[starts]
        out[heads] = np.minimum(out[heads], np.minimum.reduceat(distance, starts))


def _radii(triangles: np.ndarray, centre: np.ndarray) -> np.ndarray:
    """How far each triangle's farthest corner lies from *centre*."""
    return np.sqrt(((triangles - centre[:, None, :]) ** 2).sum(axis=2).max(axis=1))


def _cell_limit(radius: np.ndarray, floor: float) -> float:
    """The triangle radius a grid is built for: of the candidate radii, the
    one that files the fewest triangles in a point's neighbouring cells,
    counting the pieces a wider triangle is split into, within the piece
    budget; never under *floor*."""
    budget = max(_MIN_PIECE_BUDGET, len(radius))
    best, best_cost = None, np.inf
    for limit in np.unique(np.maximum(np.percentile(radius, _LIMIT_PERCENTILES), floor)):
        limit = float(limit)
        pieces = _pieces_needed(radius, limit)
        if pieces > budget:
            continue
        cost = limit * limit * (np.count_nonzero(radius <= limit) + pieces)
        if cost < best_cost:
            best, best_cost = limit, cost
    if best is not None:
        return best
    limit = max(float(radius.max()), floor)
    while _pieces_needed(radius, limit / 2.0) <= budget and limit / 2.0 >= floor:
        limit /= 2.0
    return limit


def _pieces_needed(radius: np.ndarray, limit: float) -> int:
    """How many pieces the triangles wider than *limit* split into."""
    big = radius[radius > limit]
    if not len(big):
        return 0
    return int(np.sum(np.power(4.0, np.ceil(np.log2(big / limit)))))


def _split_until(triangles: np.ndarray, limit: float) -> np.ndarray:
    """*triangles* split at their edge midpoints until every piece's corners
    lie within *limit* of its centre; the pieces cover the same surface."""
    kept = [np.empty((0, 3, 3))]
    while len(triangles):
        a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
        ab, bc, ca = (a + b) / 2.0, (b + c) / 2.0, (c + a) / 2.0
        pieces = np.concatenate([
            np.stack([a, ab, ca], axis=1), np.stack([ab, b, bc], axis=1),
            np.stack([ca, bc, c], axis=1), np.stack([ab, bc, ca], axis=1),
        ])
        wide = _radii(pieces, pieces.mean(axis=1)) > limit
        kept.append(pieces[~wide])
        triangles = pieces[wide]
    return np.concatenate(kept)
