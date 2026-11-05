"""Spatial indexes over a mesh's triangles, for searches that ask about a few
places at a time.

Each index answers with a *superset* of the triangles that can answer a query:
it tests a triangle's bounding box, padded, and never the triangle itself.  A
search that runs its own test on the candidates therefore finds exactly what
it would have found by testing every triangle; the index only skips the
triangles that could not have answered.  Memory is one entry per triangle,
whatever the triangles' shapes.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np

#: How far every box is grown, relative to the largest coordinate it holds:
#: far wider than the rounding in any search's own test, so a triangle that a
#: search would count is never left out by a box drawn a hair too tight.
_PAD_RELATIVE = 1e-6
_PAD_ABSOLUTE = 1e-9

#: Cells along a side of :class:`XYIndex`'s finest level, at most.
_MAX_CELLS = 2**20

#: Triangles per box in :class:`RayBoxes`.
_RUN = 32

#: Working memory one batch of :meth:`RayBoxes.candidates` may hold for its
#: ray-box tests.
_RAY_BATCH_BYTES = 64 * 1024 * 1024


def _pad(coords: np.ndarray) -> float:
    return _PAD_RELATIVE * max(1.0, float(np.abs(coords).max(initial=0.0))) + _PAD_ABSOLUTE


def _ranges(left: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Every position ``left[i] .. left[i] + counts[i] - 1``, in order."""
    total = int(counts.sum())
    starts = np.cumsum(counts) - counts
    return np.repeat(left - starts, counts) + np.arange(total)


class XYIndex:
    """Triangles by the XY box they cover.

    A multi-level grid: each triangle is filed once, at the finest level whose
    cells are at least as wide as its box, under the cell that holds its box's
    centre.  A box that overlaps a query box has its centre within half a cell
    of that query box at its own level, so each level is searched over the
    query box grown by half a cell.
    """

    def __init__(self, tris: np.ndarray) -> None:
        """*tris* is ``(T, 3, 3)``."""
        xy = tris[:, :, :2]
        pad = _pad(xy)
        self.lo = xy.min(axis=1) - pad
        self.hi = xy.max(axis=1) + pad
        self._levels: list[tuple[float, int, int, np.ndarray, np.ndarray]] = []
        if len(tris) == 0:
            self._origin = np.zeros(2)
            return
        size = (self.hi - self.lo).max(axis=1)
        centre = (self.lo + self.hi) / 2.0
        self._origin = self.lo.min(axis=0)
        extent = float((self.hi.max(axis=0) - self._origin).max())
        # The finest cells: about a typical triangle, but never so fine that a
        # side holds more than _MAX_CELLS of them (a mesh of mostly zero-size
        # triangles would otherwise number its cells past what an int64 holds).
        base = max(float(np.median(size)), extent / _MAX_CELLS, 1e-9)
        top = max(0, math.ceil(math.log2(max(extent / base, 1.0))))
        level = np.ceil(np.log2(np.maximum(size / base, 1.0))).astype(np.int64)
        # Rounding in the logarithm must never file a box at a level whose
        # cells are narrower than the box.
        level = np.where(base * np.exp2(level) < size, level + 1, level)
        level = np.clip(level, 0, top + 1)
        for lv in np.unique(level):
            ids = np.flatnonzero(level == lv)
            cell = base * 2.0 ** int(lv)
            cx = np.floor((centre[ids, 0] - self._origin[0]) / cell).astype(np.int64)
            cy = np.floor((centre[ids, 1] - self._origin[1]) / cell).astype(np.int64)
            nx, ny = int(cx.max()) + 1, int(cy.max()) + 1
            key = cx * ny + cy
            order = np.argsort(key, kind="stable")
            self._levels.append((cell, nx, ny, key[order], ids[order]))

    @property
    def entries(self) -> int:
        """How many triangle entries the grid holds (one per triangle)."""
        return sum(ids.size for *_, ids in self._levels)

    def in_box(self, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
        """The triangles, ascending, whose padded box overlaps the box."""
        parts = []
        for cell, nx, ny, keys, ids in self._levels:
            half = cell / 2.0
            cx0 = max(0, math.floor((x0 - half - self._origin[0]) / cell))
            cx1 = min(nx - 1, math.floor((x1 + half - self._origin[0]) / cell))
            cy0 = max(0, math.floor((y0 - half - self._origin[1]) / cell))
            cy1 = min(ny - 1, math.floor((y1 + half - self._origin[1]) / cell))
            if cx0 > cx1 or cy0 > cy1:
                continue
            rows = np.arange(cx0, cx1 + 1, dtype=np.int64) * ny
            left = np.searchsorted(keys, rows + cy0, "left")
            right = np.searchsorted(keys, rows + cy1, "right")
            parts.append(ids[_ranges(left, right - left)])
        if not parts:
            return np.empty(0, dtype=np.int64)
        cand = np.concatenate(parts)
        lo, hi = self.lo[cand], self.hi[cand]
        keep = (lo[:, 0] <= x1) & (hi[:, 0] >= x0) & (lo[:, 1] <= y1) & (hi[:, 1] >= y0)
        return np.sort(cand[keep])

    def at_points(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(point, triangle)`` pairs whose padded box holds the point, for
        every row of *xy* ``(P, 2)``: grouped by point, triangles ascending
        within a point."""
        points, found = [], []
        for cell, nx, ny, keys, ids in self._levels:
            half = cell / 2.0
            c0 = np.floor((xy - half - self._origin) / cell).astype(np.int64)
            c1 = np.floor((xy + half - self._origin) / cell).astype(np.int64)
            # The grown point spans one cell's width, so at most two cells a side.
            for dx in (0, 1):
                for dy in (0, 1):
                    cx, cy = c0[:, 0] + dx, c0[:, 1] + dy
                    ok = (cx <= c1[:, 0]) & (cy <= c1[:, 1]) & (cx >= 0) & (cy >= 0) & (cx < nx) & (cy < ny)
                    if not ok.any():
                        continue
                    k = cx[ok] * ny + cy[ok]
                    left = np.searchsorted(keys, k, "left")
                    counts = np.searchsorted(keys, k, "right") - left
                    points.append(np.repeat(np.flatnonzero(ok), counts))
                    found.append(ids[_ranges(left, counts)])
        if not points:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty
        point, tri = np.concatenate(points), np.concatenate(found)
        p, lo, hi = xy[point], self.lo[tri], self.hi[tri]
        keep = (lo[:, 0] <= p[:, 0]) & (hi[:, 0] >= p[:, 0]) & (lo[:, 1] <= p[:, 1]) & (hi[:, 1] >= p[:, 1])
        point, tri = point[keep], tri[keep]
        order = np.lexsort((tri, point))
        return point[order], tri[order]


class RayBoxes:
    """Triangles in runs of :data:`_RUN`, neighbours together (Morton order),
    each run behind one padded box.  A ray that misses a run's box misses
    every triangle in the run."""

    def __init__(self, v0: np.ndarray, edge1: np.ndarray, edge2: np.ndarray) -> None:
        """The triangles as a corner and two edges, each ``(T, 3)``."""
        p1, p2 = v0 + edge1, v0 + edge2
        lo = np.minimum(np.minimum(v0, p1), p2)
        hi = np.maximum(np.maximum(v0, p1), p2)
        n = v0.shape[0]
        self._lo = np.empty((0, 3))
        self._hi = np.empty((0, 3))
        self._members = np.empty((0, _RUN), dtype=np.int64)
        if n == 0:
            return
        centre = (lo + hi) / 2.0
        span = centre.max(axis=0) - centre.min(axis=0)
        span[span == 0.0] = 1.0
        q = ((centre - centre.min(axis=0)) / span * 1023.0).astype(np.int64)
        order = np.argsort(_morton(q), kind="stable")
        runs = -(-n // _RUN)
        filled = np.concatenate([order, np.full(runs * _RUN - n, order[-1])])
        pad = _pad(np.concatenate([lo, hi]))
        self._lo = lo[filled].reshape(runs, _RUN, 3).min(axis=1) - pad
        self._hi = hi[filled].reshape(runs, _RUN, 3).max(axis=1) + pad
        self._members = filled.reshape(runs, _RUN)

    def candidates(self, origins: np.ndarray, directions: np.ndarray) -> Iterator[np.ndarray]:
        """For each ray ``origin + t * direction``, ``t >= 0``, in order: the
        triangles, ascending, behind every box it meets."""
        runs = self._lo.shape[0]
        batch = max(1, _RAY_BATCH_BYTES // (8 * 3 * 6 * max(1, runs)))
        for start in range(0, origins.shape[0], batch):
            met = self._met(origins[start:start + batch], directions[start:start + batch])
            for row in met:
                hit = np.flatnonzero(row)
                yield np.unique(self._members[hit]) if hit.size else np.empty(0, dtype=np.int64)

    def _met(self, o: np.ndarray, d: np.ndarray) -> np.ndarray:
        """``(R, runs)``: which boxes each ray meets (slab test)."""
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / d
            t1 = (self._lo[None, :, :] - o[:, None, :]) * inv[:, None, :]
            t2 = (self._hi[None, :, :] - o[:, None, :]) * inv[:, None, :]
        near, far = np.minimum(t1, t2), np.maximum(t1, t2)
        flat = d == 0.0
        if flat.any():
            # A ray parallel to a slab is in it for every t, or never.
            inside = (o[:, None, :] >= self._lo[None, :, :]) & (o[:, None, :] <= self._hi[None, :, :])
            axis = np.broadcast_to(flat[:, None, :], near.shape)
            near = np.where(axis, np.where(inside, -np.inf, np.inf), near)
            far = np.where(axis, np.where(inside, np.inf, -np.inf), far)
        # NaN (zero times infinity) is skipped, which can only widen the test.
        enter = np.nanmax(near, axis=2)
        leave = np.nanmin(far, axis=2)
        return leave >= np.maximum(enter, 0.0)


def _morton(q: np.ndarray) -> np.ndarray:
    """Interleaved bits of three 10-bit coordinates ``(T, 3)``."""

    def spread(x: np.ndarray) -> np.ndarray:
        x = x & 0x3FF
        x = (x | (x << 16)) & 0x030000FF
        x = (x | (x << 8)) & 0x0300F00F
        x = (x | (x << 4)) & 0x030C30C3
        return (x | (x << 2)) & 0x09249249

    return spread(q[:, 0]) | (spread(q[:, 1]) << 1) | (spread(q[:, 2]) << 2)
