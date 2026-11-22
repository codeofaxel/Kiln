"""A part's outlines at a run of heights, measured from its mesh.

Contract
--------
:func:`outlines_by_height` cuts the mesh with level planes from its lowest
point to its highest and returns, for each plane, the closed outlines the
part has there, each with its section properties: its area, the length of
its edge, its box, its centre, and the smallest and largest second moment
of its area about that centre.  A hole inside an
outline is taken out of it.  A shape lying wholly inside another solid
shape of the same level (two bodies an export left overlapping) is counted
once, as the outer one.

Heights are millimetres up from the part's lowest point, as the file has
it.  Nothing is judged here and nothing is kept.
"""

from __future__ import annotations

import math
from typing import Any

#: The most planes a part is cut with, and the closest together they sit.
MAX_LEVELS = 160
MIN_STEP_MM = 0.5
#: The most outlines one level reports, largest first.
MAX_OUTLINES = 12
#: A shape smaller than this is not reported.
MIN_AREA_MM2 = 0.3
_JOIN_MM = 1e-3


class PartOutlinesError(ValueError):
    """The mesh could not be cut into outlines."""


def _cut(triangles: Any, normals: Any, z: float) -> tuple[Any, Any]:
    """The segments where the plane at *z* cuts *triangles*, each running
    with the solid on its left."""
    import numpy as np

    d = triangles[:, :, 2] - z
    above = d > 0
    count = above.sum(axis=1)
    crossing = (count == 1) | (count == 2)
    if not crossing.any():
        return np.empty((0, 2)), np.empty((0, 2))
    t, d, above, n = triangles[crossing], d[crossing], above[crossing], normals[crossing]
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
    return p1[keep], p2[keep]


def _loops(p1: Any, p2: Any) -> Any:
    """A loop number for each segment: segments that share an end are one loop."""
    import numpy as np

    ends = np.round(np.concatenate([p1, p2]) / _JOIN_MM).astype(np.int64)
    _unique, point = np.unique(ends, axis=0, return_inverse=True)
    point = point.reshape(-1)
    count = len(p1)
    parent = list(range(int(point.max()) + 1))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for first, second in zip(point[:count].tolist(), point[count:].tolist(), strict=True):
        ra, rb = find(first), find(second)
        if ra != rb:
            parent[ra] = rb
    roots = np.fromiter((find(i) for i in point[:count].tolist()), dtype=np.int64, count=count)
    _names, loop = np.unique(roots, return_inverse=True)
    return loop.reshape(-1)


def _winding(point: Any, p1: Any, p2: Any) -> int:
    """How many times the segments wind round *point*."""
    import numpy as np

    px, py = point
    y1, y2 = p1[:, 1], p2[:, 1]
    up = (y1 <= py) & (py < y2)
    down = (y2 <= py) & (py < y1)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_at = p1[:, 0] + (py - y1) * (p2[:, 0] - p1[:, 0]) / (y2 - y1)
    right = x_at > px
    return int((up & right).sum() - (down & right).sum())


def _level(p1: Any, p2: Any) -> tuple[list[dict[str, Any]], int]:
    """``(the level's outlines, largest first; how many more were left off)``."""
    import numpy as np

    if len(p1) == 0:
        return [], 0
    loop = _loops(p1, p2)
    loops = int(loop.max()) + 1
    x1, y1, x2, y2 = p1[:, 0], p1[:, 1], p2[:, 0], p2[:, 1]
    cross = x1 * y2 - x2 * y1

    def total(weights: Any) -> Any:
        return np.bincount(loop, weights=weights, minlength=loops)

    sums = np.stack([
        total(cross) / 2.0,
        total((x1 + x2) * cross) / 6.0,
        total((y1 + y2) * cross) / 6.0,
        total((y1 * y1 + y1 * y2 + y2 * y2) * cross) / 12.0,
        total((x1 * x1 + x1 * x2 + x2 * x2) * cross) / 12.0,
        total((x1 * y2 + 2 * x1 * y1 + 2 * x2 * y2 + x2 * y1) * cross) / 24.0,
        total(np.hypot(x2 - x1, y2 - y1)),
    ], axis=1)
    boxes = np.empty((loops, 4))
    for column, (values, pick) in enumerate((
        (np.minimum(x1, x2), np.minimum), (np.minimum(y1, y2), np.minimum),
        (np.maximum(x1, x2), np.maximum), (np.maximum(y1, y2), np.maximum),
    )):
        start = np.full(loops, np.inf if pick is np.minimum else -np.inf)
        pick.at(start, loop, values)
        boxes[:, column] = start
    solid = [i for i in range(loops) if sums[i, 0] >= MIN_AREA_MM2]
    # A solid shape wound round by the rest of the level lies inside another
    # solid shape: the same plastic, counted once.
    kept = []
    for i in solid:
        own = loop == i
        first = int(np.argmax(own))
        middle = (p1[first] + p2[first]) / 2.0
        if _winding(middle, p1[~own], p2[~own]) <= 0:
            kept.append(i)
    held = {i: sums[i].copy() for i in kept}
    for hole in (i for i in range(loops) if sums[i, 0] < 0):
        around = [
            i for i in kept
            if boxes[i, 0] <= boxes[hole, 0] and boxes[i, 1] <= boxes[hole, 1]
            and boxes[i, 2] >= boxes[hole, 2] and boxes[i, 3] >= boxes[hole, 3]
        ]
        if around:
            held[min(around, key=lambda i: sums[i, 0])] += sums[hole]
    outlines = []
    for i, (area, sx, sy, ixx, iyy, ixy, length) in held.items():
        if area < MIN_AREA_MM2:
            continue
        cx, cy = sx / area, sy / area
        jxx, jyy, jxy = ixx - area * cy * cy, iyy - area * cx * cx, ixy - area * cx * cy
        middle, spread = (jxx + jyy) / 2.0, math.hypot((jxx - jyy) / 2.0, jxy)
        outlines.append({
            "area_mm2": round(float(area), 3),
            "length_mm": round(float(length), 3),
            "box": [round(float(v), 3) for v in boxes[i]],
            "centre": [round(float(cx), 3), round(float(cy), 3)],
            "moment_min_mm4": round(max(float(middle - spread), 0.0), 3),
            "moment_max_mm4": round(max(float(middle + spread), 0.0), 3),
        })
    outlines.sort(key=lambda o: -o["area_mm2"])
    return outlines[:MAX_OUTLINES], max(0, len(outlines) - MAX_OUTLINES)


def outlines_by_height(path: str, *, levels: int = MAX_LEVELS) -> dict[str, Any]:
    """The outlines of the mesh at *path* at up to *levels* heights::

        {"height_mm": 40.0, "levels": [{"z_mm": 0.25, "outlines": [
            {"area_mm2": 900.0, "length_mm": 120.0, "box": [0, 0, 30, 30], "centre": [15, 15],
             "moment_min_mm4": 67500.0, "moment_max_mm4": 67500.0},
        ], "more": 0}, ...]}

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
    low, high = float(triangles[:, :, 2].min()), float(triangles[:, :, 2].max())
    height = high - low
    if height <= 0:
        raise PartOutlinesError(f"{path} has no height.")
    count = int(min(max(2, levels), max(2, math.ceil(height / MIN_STEP_MM))))
    step = height / count
    lowest, highest = triangles[:, :, 2].min(axis=1), triangles[:, :, 2].max(axis=1)
    out = []
    for index in range(count):
        z = low + (index + 0.5) * step
        near = (lowest < z) & (highest > z)
        outlines, more = _level(*_cut(triangles[near], normals[near], z))
        out.append({"z_mm": round(z - low, 3), "outlines": outlines, "more": more})
    return {"height_mm": round(height, 3), "levels": out}


__all__ = ["MAX_LEVELS", "MAX_OUTLINES", "PartOutlinesError", "outlines_by_height"]
