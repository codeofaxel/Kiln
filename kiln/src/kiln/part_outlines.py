"""A part's outlines at a run of heights, measured from its mesh.

Contract
--------
:func:`outlines_by_height` splits the part's height into equal bands, from
its lowest point to its highest, and returns for each band the closed
outlines the part has there, each with its section properties: its area,
the length of its edge, its box, its centre, and the smallest and largest
second moment of its area about that centre.  Each band is cut at three
evenly spaced heights, and each outline is reported as its weakest cut (the
smallest second moment), so a short neck inside a band is not missed.

* A hole inside an outline is taken out of it.  A solid shape lying inside
  or across a larger solid shape of the same height (two bodies an export
  left overlapping) is counted once, as the larger one.
* A triangle repeated exactly (the same corners, wound the same way) is
  counted once.
* The way a file winds its triangles does not decide what is solid: an
  outline nothing surrounds is solid, whichever way its triangles face, and
  a run of triangles facing the other way from the rest of its outline is
  turned to match.  A gap in an outline's edge that two loose ends bound is
  closed straight across.
* ``unread`` is true for a band where the part has plastic that is not in
  ``outlines``: a section too small to report, an edge that could not be
  closed, or an outline whose inside and outside could not be told.  A band
  with no plastic at all is ``unread: false`` with no outlines.
* A band reports at most :data:`MAX_OUTLINES` outlines and the whole part
  at most :data:`MAX_PART_OUTLINES`; a crowded band keeps its thinnest
  outlines (``4 x area / edge length``) and then its largest, and ``more``
  counts the ones left off.
* ``read`` is false, with ``reason``, when no band of the part produced any
  outline.

Heights are millimetres up from the part's lowest point, as the file has
it.  Nothing is judged here and nothing is kept.
"""

from __future__ import annotations

import math
from typing import Any

#: The most bands a part is split into, and the thinnest a band is.
MAX_LEVELS = 160
MIN_STEP_MM = 0.5
#: The most outlines one band reports, and the whole part.
MAX_OUTLINES = 64
MAX_PART_OUTLINES = 4000
#: A shape smaller than this is not reported as an outline.
MIN_AREA_MM2 = 0.3
#: Where in its band each of a band's three cuts sits.
_CUTS = (1.0 / 6.0, 0.5, 5.0 / 6.0)
#: Ends of cut segments closer than this are one point.
_JOIN_MM = 1e-3
#: A loop smaller than this, in area or across, is arithmetic, not plastic.
_SPECK_MM2 = 1e-4
_SPECK_MM = 1e-2
#: A loop whose runs disagree about its direction by more than this share of
#: its edge is turned by where it sits, not by its triangles.
_DOUBT = 0.25
_GRID_SHIFTS = ((0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5))
_CELL_LIMIT = 2**30 - 1
# Columns of an outline row.
_AREA, _LENGTH, _BOX, _CENTRE, _MOMENT_MIN, _MOMENT_MAX = 0, 1, slice(2, 6), slice(6, 8), 8, 9
_ROW = 10


class PartOutlinesError(ValueError):
    """The mesh could not be cut into outlines."""


def _repeats(triangles: Any) -> Any:
    """Which of *triangles* repeat an earlier one exactly: the same corners wound the same way."""
    import numpy as np

    count = len(triangles)
    if count < 2:
        return np.zeros(count, dtype=bool)

    def mix(x: Any) -> Any:
        x = (x ^ (x >> np.uint64(33))) * np.uint64(0xFF51AFD7ED558CCD)
        return x ^ (x >> np.uint64(29))

    bits = np.ascontiguousarray(triangles + 0.0).view(np.uint64)
    with np.errstate(over="ignore"):
        corner = mix(bits[:, :, 0] ^ mix(bits[:, :, 1] ^ mix(bits[:, :, 2])))
        # The same for any corner the triangle is listed from, different if wound the other way.
        key = sum(mix(corner[:, i] * np.uint64(0x9E3779B97F4A7C15) + corner[:, (i + 1) % 3]) for i in range(3))
    order = np.argsort(key)
    same_key = key[order][1:] == key[order][:-1]
    later, earlier = order[1:][same_key], order[:-1][same_key]
    same = np.zeros(len(later), dtype=bool)
    for turn in range(3):
        same |= (triangles[later] == np.roll(triangles[earlier], turn, axis=1)).all(axis=(1, 2))
    repeat = np.zeros(count, dtype=bool)
    repeat[later[same]] = True
    return repeat


def _cut(triangles: Any, normals: Any, heights: Any) -> tuple[Any, Any, Any]:
    """``(p1, p2, plane)``: every segment the level planes at *heights*
    (ascending) cut from *triangles*, each running with the solid on its
    left, grouped by plane."""
    import numpy as np

    lowest, highest = triangles[:, :, 2].min(axis=1), triangles[:, :, 2].max(axis=1)
    first = np.searchsorted(heights, lowest, side="right")
    count = np.maximum(np.searchsorted(heights, highest, side="left") - first, 0)
    owner = np.repeat(np.arange(len(triangles)), count)
    plane = first[owner] + np.arange(len(owner)) - np.repeat(np.cumsum(count) - count, count)
    t, n = triangles[owner], normals[owner]
    d = t[:, :, 2] - heights[plane][:, None]
    above = d > 0
    alone = np.where((above.sum(axis=1) == 1)[:, None], above, ~above).argmax(axis=1)
    rows = np.arange(len(t))
    a, b, c = t[rows, alone], t[rows, (alone + 1) % 3], t[rows, (alone + 2) % 3]
    da, db, dc = d[rows, alone], d[rows, (alone + 1) % 3], d[rows, (alone + 2) % 3]
    with np.errstate(divide="ignore", invalid="ignore"):
        q1 = a + (b - a) * (da / (da - db))[:, None]
        q2 = a + (c - a) * (da / (da - dc))[:, None]
    run = q2[:, :2] - q1[:, :2]
    left = np.stack([-n[:, 1], n[:, 0]], axis=1)
    flip = (run * left).sum(axis=1) < 0
    p1 = np.where(flip[:, None], q2[:, :2], q1[:, :2])
    p2 = np.where(flip[:, None], q1[:, :2], q2[:, :2])
    keep = np.isfinite(p1).all(axis=1) & np.isfinite(p2).all(axis=1) & (np.abs(p2 - p1).sum(axis=1) > 1e-9)
    p1, p2, plane = p1[keep], p2[keep], plane[keep]
    order = np.argsort(plane.astype(np.uint16 if len(heights) < 2**16 else np.int64), kind="stable")
    return p1[order], p2[order], plane[order]


def _components(count: int, a: Any, b: Any) -> Any:
    """A label for each of *count* items, shared by items the pairs
    ``(a[i], b[i])`` join, directly or through others."""
    import numpy as np

    parent = np.arange(count)
    while True:
        ra, rb = parent[a], parent[b]
        apart = ra != rb
        if not apart.any():
            break
        np.minimum.at(parent, np.maximum(ra, rb)[apart], np.minimum(ra, rb)[apart])
        while True:
            up = parent[parent]
            if (up == parent).all():
                break
            parent = up
    root = parent == np.arange(count)
    return (np.cumsum(root) - 1)[parent]


def _points(ends: Any) -> Any:
    """A label for each end: ends at one place share one."""
    import numpy as np

    cells = np.clip(ends / _JOIN_MM, -_CELL_LIMIT, _CELL_LIMIT)

    def grid(shift: tuple[float, float]) -> tuple[Any, Any]:
        cell = np.floor(cells + shift).astype(np.int64) + (_CELL_LIMIT + 1)
        key = cell[:, 0] * 2**31 + cell[:, 1]
        order = np.argsort(key, kind="stable")
        fresh = np.ones(len(key), dtype=bool)
        fresh[1:] = key[order][1:] != key[order][:-1]
        label = np.empty(len(key), dtype=np.int64)
        label[order] = np.cumsum(fresh) - 1
        return order[fresh], label

    first, point = grid(_GRID_SHIFTS[0])
    if not (np.bincount(point) % 2).any():
        return point
    # Four grids half a cell apart: two ends a hair apart share a cell in one.
    joined = [first[point]]
    for shift in _GRID_SHIFTS[1:]:
        first, inverse = grid(shift)
        joined.append(first[inverse])
    return _components(len(ends), np.tile(np.arange(len(ends)), len(joined)), np.concatenate(joined))


def _runs(count: int, a: Any, b: Any, odd: Any) -> tuple[Any, Any, Any]:
    """``(run, turned, clash)`` for *count* segments joined by the pairs
    ``(a[i], b[i])``, where ``odd[i]`` says the pair's two segments run
    opposite ways: each segment's run, whether it runs opposite to its
    run's first segment, and which pairs disagree with that."""
    import numpy as np

    parent = np.arange(count)
    turned = np.zeros(count, dtype=bool)
    while True:
        ra, rb = parent[a], parent[b]
        apart = ra != rb
        if not apart.any():
            break
        high, low = np.maximum(ra, rb)[apart], np.minimum(ra, rb)[apart]
        relation = (turned[a] ^ turned[b] ^ odd)[apart]
        order = np.lexsort((low, high))
        high, low, relation = high[order], low[order], relation[order]
        once = np.ones(len(high), dtype=bool)
        once[1:] = high[1:] != high[:-1]
        parent[high[once]] = low[once]
        turned[high[once]] = relation[once]
        while True:
            up = parent[parent]
            if (up == parent).all():
                break
            turned, parent = turned ^ turned[parent], up
    return parent, turned, turned[a] ^ turned[b] ^ odd


def _sums(p1: Any, p2: Any, loop: Any, loops: int) -> Any:
    """Per loop: signed area, its first moments, its second moments, and its edge length."""
    import numpy as np

    x1, y1, x2, y2 = p1[:, 0], p1[:, 1], p2[:, 0], p2[:, 1]
    cross = x1 * y2 - x2 * y1

    def total(weights: Any) -> Any:
        return np.bincount(loop, weights=weights, minlength=loops)

    return np.stack([
        total(cross) / 2.0,
        total((x1 + x2) * cross) / 6.0,
        total((y1 + y2) * cross) / 6.0,
        total((y1 * y1 + y1 * y2 + y2 * y2) * cross) / 12.0,
        total((x1 * x1 + x1 * x2 + x2 * x2) * cross) / 12.0,
        total((x1 * y2 + 2 * x1 * y1 + 2 * x2 * y2 + x2 * y1) * cross) / 24.0,
        total(np.hypot(x2 - x1, y2 - y1)),
    ], axis=1)


def _boxes(p1: Any, p2: Any, loop: Any, loops: int) -> Any:
    import numpy as np

    boxes = np.empty((loops, 4))
    for column, (values, pick, start) in enumerate((
        (np.minimum(p1[:, 0], p2[:, 0]), np.minimum, np.inf),
        (np.minimum(p1[:, 1], p2[:, 1]), np.minimum, np.inf),
        (np.maximum(p1[:, 0], p2[:, 0]), np.maximum, -np.inf),
        (np.maximum(p1[:, 1], p2[:, 1]), np.maximum, -np.inf),
    )):
        found = np.full(loops, start)
        pick.at(found, loop, values)
        boxes[:, column] = found
    return boxes


def _containers(p1: Any, p2: Any, loop: Any, boxes: Any, area: Any) -> tuple[Any, Any]:
    """``(inner, outer)`` pairs: loop ``outer[i]`` is the larger of the two
    and surrounds a point of the edge of loop ``inner[i]``."""
    import numpy as np

    loops = len(boxes)
    order = np.argsort(loop)
    bounds = np.searchsorted(loop[order], np.arange(loops + 1))
    # Three points on each loop's edge, a third of its segments apart.
    size = bounds[1:] - bounds[:-1]
    picks = order[bounds[:-1, None] + (size[:, None] * np.arange(3)) // 3]
    probes = (p1[picks] + p2[picks]) / 2.0
    rank = np.lexsort((np.arange(loops), np.abs(area)))
    larger = np.empty(loops, dtype=np.int64)
    larger[rank] = np.arange(loops)
    meet = (
        (boxes[:, None, 0] <= boxes[None, :, 2]) & (boxes[None, :, 0] <= boxes[:, None, 2])
        & (boxes[:, None, 1] <= boxes[None, :, 3]) & (boxes[None, :, 1] <= boxes[:, None, 3])
        & (larger[:, None] > larger[None, :])
    )
    inner, outer = [], []
    for j in np.flatnonzero(meet.any(axis=1)).tolist():
        own = order[bounds[j]:bounds[j + 1]]
        a, b = p1[own], p2[own]
        tested = np.flatnonzero(meet[j])
        q = probes[tested].reshape(-1, 2)
        qx, qy = q[:, :1], q[:, 1:]
        y1, y2 = a[None, :, 1], b[None, :, 1]
        straddle = (y1 <= qy) != (y2 <= qy)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = a[None, :, 0] + (qy - y1) * (b[None, :, 0] - a[None, :, 0]) / (y2 - y1)
        inside = (((straddle & (x_at > qx)).sum(axis=1) % 2) == 1).reshape(-1, 3).any(axis=1)
        inner.append(tested[inside])
        outer.append(np.full(int(inside.sum()), j))
    if not inner:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    return np.concatenate(inner), np.concatenate(outer)


def _level(p1: Any, p2: Any) -> tuple[Any, bool]:
    """``(rows, unread)``: the outlines one plane cuts, as rows of
    :data:`_ROW` numbers, and whether plastic at this height went unreported."""
    import numpy as np

    none = np.empty((0, _ROW))
    count = len(p1)
    if count == 0:
        return none, False
    unread = False
    point = _points(np.concatenate([p1, p2]))
    start, end = point[:count], point[count:]
    length = np.hypot(*(p2 - p1).T)
    places = int(point.max()) + 1
    meeting = np.bincount(point, minlength=places)
    # Per point: segments leaving minus segments arriving.
    balance = np.bincount(start, minlength=places) - np.bincount(end, minlength=places)
    disagree, clashing = np.zeros(count), np.empty(0, dtype=np.int64)
    if balance.any():
        # Runs: segments chained end to end through points only two segments meet.
        ends = np.argsort(point)
        at = point[ends]
        pair = np.flatnonzero((meeting[at[:-1]] == 2) & (at[:-1] == at[1:]))
        e1, e2 = ends[pair], ends[pair + 1]
        run, turned, clash = _runs(count, e1 % count, e2 % count, (e1 >= count) == (e2 >= count))
        run_length = np.bincount(run, weights=length, minlength=count)
        turned_length = np.bincount(run, weights=length * turned, minlength=count)
        flip = turned ^ (turned_length > run_length / 2.0)[run]
        with np.errstate(divide="ignore", invalid="ignore"):
            disagree = np.nan_to_num(np.minimum(turned_length, run_length - turned_length) / run_length)[run]
        clashing = e1[clash] % count
        p1, p2 = np.where(flip[:, None], p2, p1), np.where(flip[:, None], p1, p2)
        start, end = np.where(flip, end, start), np.where(flip, start, end)
        balance = np.bincount(start, minlength=places) - np.bincount(end, minlength=places)

    label = _components(places, start, end)
    loop = label[start]
    loops = int(loop.max()) + 1
    doubt = np.zeros(loops)
    np.maximum.at(doubt, loop, disagree)
    edge = np.bincount(loop, weights=length, minlength=loops)
    broken = np.zeros(loops, dtype=bool)
    broken[loop[clashing]] = True
    broken[label[np.flatnonzero((meeting % 2 == 0) & (balance != 0))]] = True
    xy = np.empty((places, 2))
    xy[np.concatenate([start, end])] = np.concatenate([p1, p2])
    chords = []
    loose = np.flatnonzero(meeting % 2 == 1)
    loose_loop = label[loose]
    for i in np.unique(loose_loop).tolist():
        here = loose[loose_loop == i]
        if broken[i] or len(here) != 2 or (meeting[here] != 1).any() or int(balance[here].sum()) != 0:
            broken[i] = True
            continue
        arrive, leave = (here[0], here[1]) if balance[here[0]] < 0 else (here[1], here[0])
        if math.dist(xy[arrive], xy[leave]) > edge[i] / 2.0:
            broken[i] = True
            continue
        chords.append((xy[arrive], xy[leave], i))
    if chords:
        unread = True
        p1 = np.concatenate([p1, [c[0] for c in chords]])
        p2 = np.concatenate([p2, [c[1] for c in chords]])
        loop = np.concatenate([loop, [c[2] for c in chords]])

    sums = _sums(p1, p2, loop, loops)
    boxes = _boxes(p1, p2, loop, loops)
    small = np.hypot(boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]) < _SPECK_MM
    if (broken & ~small).any():
        unread = True
    live = ~broken & ~small & (np.abs(sums[:, 0]) >= _SPECK_MM2)
    if not live.any():
        return none, unread
    # Only whole, measurable loops go on.
    names = np.flatnonzero(live)
    renumber = np.full(loops, -1)
    renumber[names] = np.arange(len(names))
    keep = live[loop]
    p1, p2, loop = p1[keep], p2[keep], renumber[loop[keep]]
    sums, boxes, doubt = sums[names], boxes[names], doubt[names]
    loops = len(names)
    inner, outer = _containers(p1, p2, loop, boxes, sums[:, 0])
    depth = np.bincount(inner, minlength=loops)

    sign = np.sign(sums[:, 0])
    doubtful = doubt > _DOUBT
    # An outline nothing surrounds is solid; when its runs agree, the loops
    # inside it were wound the same way it was.
    outside_in = (depth == 0) & (sign < 0)
    whole_family = outside_in & ~doubtful
    flipped = outside_in.copy()
    flipped[inner[whole_family[outer]]] = True
    sign = np.where(flipped, -sign, sign)
    # A doubtful loop inside another is a hole at odd depth and an island at
    # even depth; an island read that way is not trusted.
    nested_doubt = doubtful & (depth > 0)
    sign = np.where(nested_doubt, np.where(depth % 2 == 0, 1.0, -1.0), sign)
    if (nested_doubt & (depth % 2 == 0)).any():
        unread = True
    sums[:, :6] *= (sign * np.sign(sums[:, 0]))[:, None]

    wound = np.bincount(inner, weights=sign[outer], minlength=loops)
    kept = (sign > 0) & (wound <= 0)
    held = sums.copy()
    for hole in np.flatnonzero(sign < 0).tolist():
        around = outer[(inner == hole) & kept[outer]]
        if len(around):
            held[around[np.argmin(sums[around, 0])]] += sums[hole]
    rows = []
    for i in np.flatnonzero(kept).tolist():
        area, sx, sy, ixx, iyy, ixy, rim = held[i]
        if area < MIN_AREA_MM2:
            unread = True
            continue
        cx, cy = sx / area, sy / area
        jxx, jyy, jxy = ixx - area * cy * cy, iyy - area * cx * cx, ixy - area * cx * cy
        middle, spread = (jxx + jyy) / 2.0, math.hypot((jxx - jyy) / 2.0, jxy)
        rows.append([area, rim, *boxes[i], cx, cy, max(middle - spread, 0.0), max(middle + spread, 0.0)])
    if not rows:
        return none, True
    return np.asarray(rows, dtype=float), unread


def _weakest(cuts: list[Any]) -> Any:
    """The middle cut's outlines, each taken from whichever of the band's
    cuts has it weakest, when that cut has it as one outline."""
    middle = cuts[len(cuts) // 2]
    if len(middle) == 0:
        return middle
    chosen = middle.copy()

    def overlap(a: Any, b: Any) -> Any:
        return (
            (a[:, None, 2] < b[None, :, 4]) & (b[None, :, 2] < a[:, None, 4])
            & (a[:, None, 3] < b[None, :, 5]) & (b[None, :, 3] < a[:, None, 5])
        )

    for other in cuts:
        if other is middle or len(other) == 0:
            continue
        touch = overlap(middle, other)
        single = (touch.sum(axis=1) == 1) & (touch.sum(axis=0)[touch.argmax(axis=1)] == 1)
        partner = other[touch.argmax(axis=1)]
        weaker = single & (partner[:, _MOMENT_MIN] < chosen[:, _MOMENT_MIN])
        chosen[weaker] = partner[weaker]
    return chosen


def _keep(rows: Any, room: int) -> tuple[Any, int]:
    """``(the rows kept, largest first; how many were left off)``: at most
    *room*, the thinnest first and then the largest."""
    import numpy as np

    if len(rows) <= room:
        return rows[np.argsort(-rows[:, _AREA], kind="stable")], 0
    with np.errstate(divide="ignore", invalid="ignore"):
        width = 4.0 * rows[:, _AREA] / rows[:, _LENGTH]
    thin = np.argsort(width, kind="stable")[: max(1, room // 4)]
    rest = np.setdiff1d(np.arange(len(rows)), thin)
    large = rest[np.argsort(-rows[rest, _AREA], kind="stable")][: room - len(thin)]
    kept = rows[np.concatenate([thin, large])]
    return kept[np.argsort(-kept[:, _AREA], kind="stable")], len(rows) - room


def _room(counts: list[int]) -> int:
    """The most outlines any one band may report so the part stays within
    :data:`MAX_PART_OUTLINES`."""
    room = MAX_OUTLINES
    while room > 1 and sum(min(n, room) for n in counts) > MAX_PART_OUTLINES:
        room -= 1
    return room


def _outline(row: Any) -> dict[str, Any]:
    return {
        "area_mm2": round(float(row[_AREA]), 3),
        "length_mm": round(float(row[_LENGTH]), 3),
        "box": [round(float(v), 3) for v in row[_BOX]],
        "centre": [round(float(v), 3) for v in row[_CENTRE]],
        "moment_min_mm4": round(float(row[_MOMENT_MIN]), 3),
        "moment_max_mm4": round(float(row[_MOMENT_MAX]), 3),
    }


def outlines_by_height(path: str, *, levels: int = MAX_LEVELS) -> dict[str, Any]:
    """The outlines of the mesh at *path* in up to *levels* bands::

        {"height_mm": 40.0, "read": True, "levels": [{"z_mm": 0.25, "outlines": [
            {"area_mm2": 900.0, "length_mm": 120.0, "box": [0, 0, 30, 30], "centre": [15, 15],
             "moment_min_mm4": 67500.0, "moment_max_mm4": 67500.0},
        ], "more": 0, "unread": False}, ...]}

    ``z_mm`` is the middle of the band.  When no band produced an outline,
    ``read`` is false and ``reason`` says why in words.

    :raises PartOutlinesError: a file that holds no triangles Kiln can cut.
    """
    import numpy as np

    from kiln.decoration_faces import load_mesh_triangles

    try:
        triangles = np.asarray(load_mesh_triangles(path), dtype=np.float64)
    except Exception as exc:  # noqa: BLE001 -- any unreadable file is one refusal
        raise PartOutlinesError(f"{path} could not be read as a mesh: {exc}") from exc
    if triangles.ndim != 3 or len(triangles) == 0:
        raise PartOutlinesError(f"{path} holds no triangles.")
    with np.errstate(invalid="ignore", over="ignore"):
        normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        size = np.linalg.norm(normals, axis=1)
    # A triangle with a corner that is not a number has no area that is one.
    flat = np.isfinite(size) & (size > 1e-12) & np.isfinite(triangles).all(axis=(1, 2))
    triangles, normals = triangles[flat], normals[flat] / size[flat][:, None]
    if len(triangles) == 0:
        raise PartOutlinesError(f"{path} holds no triangles with any area.")
    once = ~_repeats(triangles)
    triangles, normals = triangles[once], normals[once]
    low, high = float(triangles[:, :, 2].min()), float(triangles[:, :, 2].max())
    height = high - low
    if height <= 0:
        raise PartOutlinesError(f"{path} has no height.")
    count = int(min(max(2, levels), max(2, math.ceil(height / MIN_STEP_MM))))
    step = height / count
    heights = np.array([low + (index + share) * step for index in range(count) for share in _CUTS])
    p1, p2, plane = _cut(triangles, normals, heights)
    bounds = np.searchsorted(plane, np.arange(len(heights) + 1))
    bands = []
    for index in range(count):
        cuts, unread = [], False
        for k in range(index * len(_CUTS), (index + 1) * len(_CUTS)):
            rows, missed = _level(p1[bounds[k]:bounds[k + 1]], p2[bounds[k]:bounds[k + 1]])
            cuts.append(rows)
            unread = unread or missed
        bands.append((_weakest(cuts), unread))
    room = _room([len(rows) for rows, _unread in bands])
    out = []
    for index, (rows, unread) in enumerate(bands):
        kept, more = _keep(rows, room)
        out.append({
            "z_mm": round((index + 0.5) * step, 3), "outlines": [_outline(row) for row in kept],
            "more": more, "unread": bool(unread),
        })
    part: dict[str, Any] = {"height_mm": round(height, 3), "read": any(level["outlines"] for level in out), "levels": out}
    if not part["read"]:
        wide, deep = np.ptp(triangles[:, :, :2].reshape(-1, 2), axis=0)
        part["reason"] = (
            f"nothing in this part is large enough to read: it measures {height:.3g} mm tall and "
            f"{max(wide, deep):.3g} mm across, the size a part has when its file was saved in metres"
            if wide * deep < MIN_AREA_MM2 else
            "no closed outline could be read from this file's surface at any height"
        )
    return part


__all__ = ["MAX_LEVELS", "MAX_OUTLINES", "MAX_PART_OUTLINES", "PartOutlinesError", "outlines_by_height"]
