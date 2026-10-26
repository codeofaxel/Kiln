"""The footprint's outline is chained in time linear in its edges, whatever the file.

An automated security review of public main c9ddfcf3 (2026-10-25) found the
one place the corner reading was still quadratic.  Joining the footprint's
outline edges into rings, an edge arriving at a point takes the sharpest left
turn there; where the outline runs over itself, two arrivals can be handed the
same leaving edge, and the second scanned that point's leaving edges from the
pick until it found a free one, every time.  A file whose bed contact is the
same triangle stacked N times (a bad export, or a hostile upload) has N
arrivals and N leaving edges at every corner, so N scans of up to N each:
measured 4,000 copies at 2.4 s, and about 100,000 is tens of minutes on one
analysis call.  The duplicates were also each read as a ring of their own, so
one triangle stacked N times had 3N "sharp corners".

The rule now: a point's free leaving edges are found through a skip list that
jumps over the used ones, so each edge is passed over at most a few times in
the whole walk, and the edge taken is the one the scan took.  A triangle the
file states twice is one triangle of contact.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from kiln.printability import _corner_ids, _footprint_sharp_corners, _outline_rings, _patch_outline

_ONE = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0))

#: Far above what the bounded walk takes (about 0.1 s here) and far below
#: what the scan took (5 s or more at these sizes), so a slow machine does
#: not fail it and the scan cannot pass it.
_BUDGET_S = 1.5


def _timed(triangles) -> tuple[int, float]:
    started = time.perf_counter()
    corners = _footprint_sharp_corners(triangles, 0.0)
    return corners, time.perf_counter() - started


def test_a_triangle_stacked_thousands_of_times_is_one_triangle_read_quickly() -> None:
    once, _ = _timed([_ONE])
    stacked, took = _timed([_ONE] * 6_000)

    assert once == 3
    assert stacked == once, "a triangle the file states twice is one triangle of contact"
    assert took < _BUDGET_S, f"{took:.2f} s for one triangle stated 6,000 times"


def test_a_stack_of_slightly_different_triangles_is_read_in_bounded_time() -> None:
    """Not one triangle repeated, so nothing can be dropped as a copy: 6,000
    triangles share two corners and differ at the third by a micrometre each,
    so every arrival at the shared corners is handed the same leaving edge."""
    stack = [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0 + 0.001 * i, 0.0)) for i in range(6_000)]

    corners, took = _timed(stack)

    assert corners >= 3
    assert took < _BUDGET_S, f"{took:.2f} s for 6,000 overlapping triangles"


# ---------------------------------------------------------------------------
# The bound changes how the free edge is found, never which edge it is
# ---------------------------------------------------------------------------


def _rings_by_scan(edges: np.ndarray, ids: np.ndarray) -> list[list[tuple[float, float]]]:
    """The rings as the chainer read them before 2026-10-25, scan and all:
    the oracle the bounded walk must agree with, edge for edge."""
    run = edges[:, 1] - edges[:, 0]
    heading = np.arctan2(run[:, 1], run[:, 0])
    back = np.where(heading > 0.0, heading - np.pi, heading + np.pi)
    key = ids[:, 0] * 8.0 + (heading + np.pi)
    order = np.argsort(key, kind="stable")
    key = key[order]
    at = ids[:, 1] * 8.0
    lo = np.searchsorted(key, at)
    hi = np.searchsorted(key, at + 8.0)
    pick = np.searchsorted(key, at + (back + np.pi), side="right") - 1
    pick = np.where(pick >= lo, pick, hi - 1)
    after = np.where(lo < hi, order[np.clip(pick, 0, len(order) - 1)], -1).tolist()
    used = [False] * len(edges)
    rings = []
    for start in range(len(edges)):
        if used[start]:
            continue
        walk, k = [], start
        while k >= 0 and not used[k]:
            used[k] = True
            walk.append(k)
            k = after[k]
            if k >= 0 and used[k] and k != start:
                lo_k, hi_k, j = int(lo[walk[-1]]), int(hi[walk[-1]]), int(pick[walk[-1]])
                free = (int(order[lo_k + (j - lo_k - q) % (hi_k - lo_k)]) for q in range(hi_k - lo_k))
                k = next((e for e in free if not used[e]), -1)
        if len(walk) >= 3:
            rings.append([tuple(p) for p in edges[walk, 0].tolist()])
    return rings


def _soup(rng: np.random.Generator, count: int) -> np.ndarray:
    """*count* triangles on a coarse grid, so they overlap, share corners and
    run over one another's edges: every case the free-edge search meets."""
    tris = rng.integers(0, 4, size=(count, 3, 2)).astype(np.float64)
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    return tris[np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) > 0]


@pytest.mark.parametrize("seed", range(40))
def test_the_bounded_walk_takes_the_edge_the_scan_took(seed: int) -> None:
    rng = np.random.default_rng(seed)
    tris = _soup(rng, int(rng.integers(2, 40)))
    ids = _corner_ids(tris.reshape(-1, 2)).reshape(-1, 3)
    edges, edge_ids = _patch_outline(tris, ids)

    got = [[tuple(p) for p in ring.tolist()] for ring in _outline_rings(edges, edge_ids)]

    assert got == _rings_by_scan(edges, edge_ids)
