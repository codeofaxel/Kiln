"""How many pieces a mesh is, where they touch, and whether each is sealed.

One reading, asked by every door that counts pieces or judges whether a part
is closed: ``diagnose_mesh``, the printability report, the validation and
repair passes, and the doors that adopt a scene's named parts.

Two things a file does not say have to be settled first.

* **Which corners are the same point.**  An STL repeats every corner once per
  triangle, and a glTF or OBJ splits a corner wherever its texture or normals
  change, so corners at one position are one corner, and so are copies of
  one point an exporter wrote a rounding apart: corners within a radius that
  is a fixed fraction of the part's own size (:data:`WELD_FRACTION`), never
  a fixed length, since the same model written in meters and in millimeters
  must weld the same way, or the count depends on the unit it was exported
  in.  Two corners one triangle holds stay apart unless they are copies
  too, a few roundings apart.
* **Which triangles belong together where more than two meet on one edge.**
  Two pieces laid on each other (a coat modelled on a body's surface) share
  their corners along the contact, so after the weld four triangles meet on
  each of those edges.  Each piece's own two triangles there are paired by
  going round the edge: a triangle closes the solid that the triangle before
  it opened.  Two triangles lying exactly on top of each other face away
  from each other, so neither is taken as the other's partner.

What is left is three kinds of edge a person can be told about in plain
words: **open** (one triangle: a hole), **pinched** (a piece meets itself
along a line), and **touching** (two separate pieces share the edge).

Pieces are then grouped into the **solids** a slicer prints.  Two pieces
that share a surface, or where one sinks into the other, are one solid: a
slicer cuts every triangle of the file into one outline per layer, so the
contact lies inside the outline.  Two pieces that meet only along a line or
at a point stay two shells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: Corners closer than this fraction of the part's diagonal are copies of one
#: corner, unless a triangle holds them both (:data:`_COPY_RADII`).  A millionth of the part's size
#: is far below anything a printer can make and well above float32 rounding
#: at any size, so it joins corners an exporter wrote twice and never two a
#: designer meant apart.  The repair pass's weld reads the same fraction
#: (``_auto_weld_tolerance``).
WELD_FRACTION = 1e-6

#: How many of its neighbours in a weld bucket each corner is measured against
#: (:func:`_join_near`): the copies of one point an exporter wrote are two, or
#: a handful where many triangles meet, so the work stays a few passes over
#: the corners whatever a file holds.
_NEAR_SPAN = 8

#: Two corners one triangle holds are two copies of one point only when they
#: are closer than this many weld radii, a few float32 roundings at the
#: part's own size.  A side longer than this is the triangle's own, and the
#: weld never closes it.
_COPY_RADII = 0.25

#: How far outside a piece's own surface, as a fraction of its diagonal, the
#: points sit that ask whether another piece is right there: far below any
#: gap a printer resolves and far above the weld, so a designed clearance
#: (a print-in-place hinge) never reads as contact.
_PROBE_DEPTH_FRACTION = 1e-3

#: Probe points per pair, spread evenly over the smaller piece's triangles.
_PROBES = 96

#: Two pieces are in contact when at least this many of the smaller one's
#: probes, and this share of them, land inside the other: one stray ray is
#: not contact.
_CONTACT_MIN_PROBES = 2
_CONTACT_MIN_SHARE = 0.05

#: Contact tests run per reading.  A pair whose pieces are already in one
#: solid is not tested, so a lattice of thousands of cells costs about one
#: test a cell; past this a pair reads as apart, the cautious answer, and
#: the reading says how many went untested.
_MAX_CONTACT_PAIRS = 8192

#: The most work one reading spends looking for contact, so a file built to
#: stall it (a server reads files people upload) cannot: pieces the sweep
#: looks at, and triangles the contact tests scan plus probe-triangle pairs
#: they cross.  Past either, what is left reads as apart and is counted as
#: untested.  A Blender donut of 62 pieces and 300,000 triangles uses about
#: a twentieth of the second (measured 2026-10-23).
_MAX_SWEEP_LOOKS = 4_000_000
_MAX_CONTACT_WORK = 200_000_000
#: Triangles held at once in the per-host material the tests reuse; a host
#: past it is read for its test and let go.
_MAX_HELD_MATERIAL = 4_000_000

@dataclass(frozen=True)
class Piece:
    """One separate piece: its triangle count, where it is, and whether it is
    a sealed solid (no open edge and no pinched edge of its own)."""

    faces: int
    sealed: bool
    center: tuple[float, float, float]
    size: tuple[float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "faces": self.faces,
            "sealed": self.sealed,
            "center": [_rounded(v) for v in self.center],
            "size": [_rounded(v) for v in self.size],
        }


@dataclass(frozen=True)
class BodyReading:
    """The pieces of one mesh and the edges that are not plain, two-triangle
    surface.  Pieces are largest first; ``touching`` names pairs of pieces by
    their index in ``pieces`` with the number of edges they share."""

    pieces: tuple[Piece, ...]
    open_edges: int
    pinch_edges: int
    touching_edges: int
    touching: tuple[tuple[int, int, int], ...]
    weld_tolerance: float
    #: Piece index per input triangle, ``-1`` for a triangle the weld
    #: collapsed to a line or a point.
    face_piece: np.ndarray = field(repr=False, compare=False)
    #: A few points on the pinched edges, so a person can find them.
    pinch_at: tuple[tuple[float, float, float], ...] = ()
    #: Distinct edges of the welded surface, every kind counted once.
    edge_count: int = 0
    #: Pairs of pieces that print as one solid, by index in ``pieces``, with
    #: how: ``"surface"`` (they share triangles) or ``"contact"`` (one lies
    #: flush against the other or sinks into it).
    joined: tuple[tuple[int, int, str], ...] = ()
    #: The solid each piece prints in, by index in ``pieces``: the smallest
    #: index of the pieces joined to it.  Empty for a reading of no pieces.
    solid_of: tuple[int, ...] = ()
    #: Pairs of pieces whose boxes meet that went untested for contact (past
    #: :data:`_MAX_CONTACT_PAIRS`); each reads as apart.
    untested_pairs: int = 0

    @property
    def piece_count(self) -> int:
        return len(self.pieces)

    @property
    def solid_count(self) -> int:
        """How many separate solids the pieces print as."""
        return len(set(self.solid_of)) if self.solid_of else self.piece_count

    @property
    def sealed(self) -> bool:
        """Every piece is a closed solid.  Pieces that only touch each other
        are still each sealed."""
        return bool(self.pieces) and all(p.sealed for p in self.pieces)

    @property
    def unsealed_count(self) -> int:
        return sum(1 for p in self.pieces if not p.sealed)

    def describe(self) -> list[str]:
        """What is wrong with the edges, one plain sentence per kind, worst
        first.  Empty when every edge is ordinary surface."""
        lines: list[str] = []
        if self.pinch_edges:
            where = ""
            if self.pinch_at:
                where = " (for example at " + "; ".join(
                    _point(p) for p in self.pinch_at
                ) + ")"
            lines.append(
                f"{self.pinch_edges:,} pinched edge(s): a piece meets itself along "
                f"a line, with 3 or more triangles on one edge{where}.  That is "
                "no hole to sew; the surface has to be rebuilt where it was made."
            )
        if self.open_edges:
            lines.append(
                f"{self.open_edges:,} open edge(s): the surface has holes there."
            )
        if self.touching_edges:
            pairs = "; ".join(
                f"the {_piece_words(self.pieces[a])} and the "
                f"{_piece_words(self.pieces[b])} share {n:,} edge(s)"
                for a, b, n in self.touching[:3]
            )
            more = (
                f" (and {len(self.touching) - 3} more pair(s))"
                if len(self.touching) > 3 else ""
            )
            lines.append(
                f"{_count_pairs(self.touching)} separate pieces sit on each other: "
                f"{pairs}{more}.  They are separate pieces in contact, which is "
                "not a defect of either piece."
            )
        return lines

    def solids_sentence(self) -> str:
        """How many solids the pieces print as, in one plain sentence, or
        ``""`` for a single piece or pieces that all stand apart."""
        n, k = self.piece_count, self.solid_count
        unchecked = (
            f"  {self.untested_pairs:,} pairs of pieces were not checked and are "
            "counted apart." if self.untested_pairs else ""
        )
        if n < 2 or not (self.joined or unchecked):
            return ""
        if not self.joined:
            return unchecked.strip()
        if k == 1:
            return (
                f"Its {n:,} pieces print as one solid: they share surfaces or sink "
                "into one another, so a slicer prints them as one part." + unchecked
            )
        return (
            f"Its {n:,} pieces print as {k:,} separate solids: pieces that share a "
            "surface or sink into one another print as one, and the rest stand on "
            "their own." + unchecked
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "piece_count": self.piece_count,
            "solid_count": self.solid_count,
            "sealed": self.sealed,
            "open_edges": self.open_edges,
            "pinch_edges": self.pinch_edges,
            "touching_edges": self.touching_edges,
            "touching": [list(t) for t in self.touching],
            "pinch_at": [list(p) for p in self.pinch_at],
            "pieces": [p.to_dict() for p in self.pieces[:20]],
            "joined": [list(j) for j in self.joined[:50]],
            "untested_pairs": self.untested_pairs,
            "weld_tolerance": self.weld_tolerance,
        }


def _rounded(value: float) -> float:
    """*value* to the figures a report prints, whatever the unit."""
    return float(f"{value:.4g}")


def _point(p: tuple[float, float, float]) -> str:
    return "(" + ", ".join(f"{_rounded(v):g}" for v in p) + ")"


def _piece_words(piece: Piece) -> str:
    return f"piece of {piece.faces:,} triangles around {_point(piece.center)}"


def _count_pairs(touching: tuple[tuple[int, int, int], ...]) -> int:
    return len({i for a, b, _ in touching for i in (a, b)})


def weld_tolerance(vertices: np.ndarray) -> float:
    """The weld radius for corners spread like *vertices*: a millionth of
    their bounding-box diagonal, or ``0`` for a mesh with no extent."""
    if len(vertices) == 0:
        return 0.0
    span = np.ptp(np.asarray(vertices, dtype=np.float64), axis=0)
    return float(math.sqrt(float((span * span).sum())) * WELD_FRACTION)


def weld(
    vertices: np.ndarray, faces: np.ndarray, *, tolerance: float | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Join the corners a file wrote more than once.

    Corners at exactly one point are one corner.  Then corners within
    *tolerance* (default :func:`weld_tolerance`) of each other join, the
    copies of one point an exporter wrote a rounding apart, except where a
    triangle holds both and they are further apart than a few roundings
    (:data:`_COPY_RADII`), even through a chain of others: that side is the
    triangle's own.  So geometry finer than the radius reads as the file
    wrote it.  A *tolerance* of ``0`` joins exact duplicates only.

    :returns: ``(vertices, faces, tolerance)`` with *faces* re-indexed.
    """
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(faces).reshape(-1, 3)
    tol = weld_tolerance(v) if tolerance is None else max(0.0, float(tolerance))
    uniq, inverse = _gather(v)
    if tol > 0.0 and len(uniq) > 1:
        label = _join_near(uniq, inverse[f], tol)
        keep, label = np.unique(label, return_inverse=True)
        uniq = uniq[keep]
        inverse = label.reshape(-1)[inverse]
    index = np.int32 if len(uniq) < np.iinfo(np.int32).max else np.int64
    return uniq, inverse.astype(index)[f], tol


def _gather(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One representative per exact corner (``-0.0`` reads as ``0.0``), and
    each corner's representative.  The corners are sorted by index, not
    copied, to hold little memory."""
    if len(v) == 0:
        return v, np.zeros(0, dtype=np.int64)
    order = np.lexsort((v[:, 2], v[:, 1], v[:, 0]))
    new = np.ones(len(v), dtype=bool)
    for axis in range(3):
        column = v[order, axis]
        new[1:] &= column[1:] == column[:-1]
    new[1:] = ~new[1:]
    group = np.cumsum(new) - 1
    inverse = np.empty(len(v), dtype=np.int64)
    inverse[order] = group
    return v[order[new]], inverse


def _join_near(points: np.ndarray, faces: np.ndarray, tol: float) -> np.ndarray:
    """For each point, the smallest index of the points joined to it: two
    join when they lie within *tol* of each other and are not the ends of a
    side of *faces* longer than :data:`_COPY_RADII` radii, and joins chain.
    A chain that would close such a side is left unjoined, all of it.

    Each of eight grids of cell ``2 * tol``, offset by ``tol`` on each axis
    in turn, buckets the points; two points within *tol* on every axis share
    a bucket in at least one of them (:func:`_near_pairs`, which also says
    how a crowded bucket is joined).  One grid's pairs are joined before the
    next is laid, so the work and memory stay a few passes over the points
    whatever a file holds.
    """
    n = len(points)
    label = np.arange(n, dtype=np.int64)
    origin = points.min(axis=0)
    scaled = (points - origin) / (2.0 * tol)
    sides = None
    for dx in (0.0, 0.5):
        for dy in (0.0, 0.5):
            for dz in (0.0, 0.5):
                a, b = _near_pairs(points, scaled, (dx, dy, dz), tol)
                if not len(a):
                    continue
                if sides is None:
                    sides = _sides(points, faces, _COPY_RADII * tol)
                pair = np.minimum(a, b) * n + np.maximum(a, b)
                at = np.minimum(np.searchsorted(sides, pair), max(len(sides) - 1, 0))
                own = (sides[at] == pair) if len(sides) else np.zeros(len(pair), dtype=bool)
                a, b = label[a[~own]], label[b[~own]]
                if len(a):
                    label = _components(n, a, b)[label]
    if sides is None or not len(sides):
        return label
    lo, hi = sides // n, sides % n
    closed = label[lo[label[lo] == label[hi]]]
    if len(closed):
        shut = np.zeros(n, dtype=bool)
        shut[closed] = True
        undo = shut[label]
        label[undo] = np.flatnonzero(undo)
    return label


def _near_pairs(
    points: np.ndarray, scaled: np.ndarray, offset: tuple[float, float, float], tol: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The pairs of *points* that share a bucket of the grid laid on *scaled*
    (the points in bucket units, from 0) moved by *offset*, and lie within
    *tol* of each other.

    Each point is measured against its bucket's first point, which joins the
    copies of one point in one pass however many a file wrote.  A bucket
    with a point that first did not reach is measured in full, pair by pair,
    when it holds at most :data:`_NEAR_SPAN` + 1 points.  One holding more is
    geometry finer than any printer resolves, or a file built to crowd one:
    its points left over join the first unmeasured, and the side check that
    follows still keeps every triangle's corners apart.
    """
    reach = np.floor(scaled.max(axis=0) + offset).astype(np.int64) + 1
    if float(np.prod(reach.astype(np.float64))) < 2.0**62:
        bucket = np.zeros(len(scaled), dtype=np.int64)  # one axis at a time, for memory
        for axis in range(3):
            bucket *= reach[axis]
            bucket += np.floor(scaled[:, axis] + offset[axis]).astype(np.int64)
    else:  # a tolerance far finer than the part: one integer cannot name every cell
        cells = np.floor(scaled + offset).astype(np.int64)
        bucket = np.unique(cells, axis=0, return_inverse=True)[1].reshape(-1)
        del cells
    order = np.argsort(bucket, kind="stable")
    in_order = bucket[order]
    del bucket
    starts = np.flatnonzero(np.r_[True, in_order[1:] != in_order[:-1]])
    size = np.diff(np.r_[starts, len(in_order)])
    which = np.repeat(np.arange(len(starts)), size)  # each sorted point's bucket, from 0
    lead = order[starts[which]]
    gap = np.zeros(len(order))
    for axis in range(3):
        gap += (points[order, axis] - points[lead, axis]) ** 2
    is_first = order == lead
    near = ~is_first & (gap <= tol * tol)
    pairs_a, pairs_b = [lead[near]], [order[near]]
    left = ~is_first & ~near
    if left.any():
        unreached = np.zeros(len(starts), dtype=bool)
        unreached[which[left]] = True
        crowded = size > _NEAR_SPAN + 1
        wholesale = left & crowded[which]
        pairs_a.append(lead[wholesale])
        pairs_b.append(order[wholesale])
        full = np.flatnonzero(unreached[which] & ~crowded[which])
        o, b_of = order[full], which[full]
        for step in range(1, _NEAR_SPAN + 1):
            shared = b_of[step:] == b_of[:-step]
            if not shared.any():
                break  # no bucket measured in full holds more than *step* points
            a, b = o[:-step][shared], o[step:][shared]
            gap = np.zeros(len(a))
            for axis in range(3):
                gap += (points[a, axis] - points[b, axis]) ** 2
            close = gap <= tol * tol
            pairs_a.append(a[close])
            pairs_b.append(b[close])
    return np.concatenate(pairs_a), np.concatenate(pairs_b)


def _sides(points: np.ndarray, faces: np.ndarray, shortest: float) -> np.ndarray:
    """Every triangle side of *faces* longer than *shortest* once, keyed
    ``low * n + high`` by its two ends (``n`` the number of *points*),
    ascending.  A shorter side joins two copies of one point."""
    n = len(points)
    starts = faces.reshape(-1).astype(np.int64)
    ends = np.roll(faces, -1, axis=1).reshape(-1).astype(np.int64)
    length = np.zeros(len(starts))
    for axis in range(3):
        length += (points[starts, axis] - points[ends, axis]) ** 2
    side = length > shortest * shortest
    return np.unique(np.minimum(starts, ends)[side] * n + np.maximum(starts, ends)[side])


def _settle(label: np.ndarray) -> np.ndarray:
    """Follow each label to the label it points at until nothing moves."""
    while True:
        nxt = label[label]
        if np.array_equal(nxt, label):
            return label
        label = nxt


def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Component label per node for the undirected links ``a[i]-b[i]``."""
    label = np.arange(n, dtype=np.int64)
    if len(a) == 0:
        return label
    while True:
        low = np.minimum(label[a], label[b])
        before = label.copy()
        np.minimum.at(label, a, low)
        np.minimum.at(label, b, low)
        label = _settle(label)
        if np.array_equal(label, before):
            return label


def _pair_round_edge(
    faces_on_edge: list[int],
    forward: list[bool],
    angle: list[float],
) -> list[tuple[int, int]]:
    """Pair the triangles on one edge the way the solids they bound pair.

    A triangle that runs along the edge in its sorted direction (*forward*)
    closes, going round the edge, the solid that the nearest backward
    triangle before it opened.  Triangles at the same angle put the forward
    one first, so two triangles lying back to back on each other (two
    pieces in contact) are never taken as one solid of zero thickness.
    """
    order = sorted(range(len(faces_on_edge)), key=lambda i: (angle[i], 0 if forward[i] else 1))
    n = len(order)
    start = next((k for k in range(n) if forward[order[k]]), None)
    if start is None:
        return []
    pairs: list[tuple[int, int]] = []
    open_: list[int] = []
    waiting: list[int] = []
    for step in range(1, n + 1):
        i = order[(start + step) % n]
        if forward[i]:
            if open_:
                pairs.append((faces_on_edge[open_.pop()], faces_on_edge[i]))
            else:
                waiting.append(i)
        else:
            open_.append(i)
    while open_ and waiting:
        pairs.append((faces_on_edge[open_.pop()], faces_on_edge[waiting.pop(0)]))
    return pairs


def read_bodies(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    tolerance: float | None = None,
    max_points: int = 3,
) -> BodyReading:
    """Read the pieces of the mesh *vertices* / *faces* (any indexing: an
    STL's one-copy-per-corner, a glTF split at its seams, or already welded).

    :param tolerance: weld radius; defaults to :func:`weld_tolerance`.
    :param max_points: how many pinched-edge points to keep for the report.
    """
    v, f, tol = weld(vertices, faces, tolerance=tolerance)
    return _read_welded(v, f, tol, max_points=max_points)


def _read_welded(
    v: np.ndarray, f: np.ndarray, tol: float, *, max_points: int = 3,
) -> BodyReading:
    """:func:`read_bodies` on corners already welded."""
    n_faces = len(f)
    face_piece = np.full(n_faces, -1, dtype=np.int64)
    if n_faces == 0:
        return BodyReading((), 0, 0, 0, (), tol, face_piece)

    live = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    live_idx = np.nonzero(live)[0]
    lf = f[live]
    # Triangle side s runs from corner s % 3 of triangle s // 3 to the next
    # corner in winding order; each side is keyed by its two ends, sorted.
    starts = lf.reshape(-1)
    ends = np.roll(lf, -1, axis=1).reshape(-1)
    n_v = np.int64(len(v))
    key = np.minimum(starts, ends).astype(np.int64)
    key *= n_v
    key += np.maximum(starts, ends)
    del ends
    order = np.argsort(key, kind="stable")
    key = key[order]
    new_edge = np.ones(len(key), dtype=bool)
    new_edge[1:] = key[1:] != key[:-1]
    edge_start = np.nonzero(new_edge)[0]
    del new_edge
    edge_len = np.diff(np.append(edge_start, len(key)))
    owner_s = (order // 3).astype(np.int64)

    links_a: list[np.ndarray] = []
    links_b: list[np.ndarray] = []
    two = edge_start[edge_len == 2]
    links_a.append(owner_s[two])
    links_b.append(owner_s[two + 1])

    crowded = np.nonzero(edge_len > 2)[0]
    crowded_pairs: list[tuple[int, int]] = []
    first = bounds = owners = np.zeros(0, dtype=np.int64)
    if len(crowded):
        # Every triangle side on a crowded edge, measured at once: the angle
        # of the triangle's third corner round the edge's axis.
        rows = np.concatenate([np.arange(edge_start[e], edge_start[e] + edge_len[e]) for e in crowded])
        edge_of_row = np.repeat(np.arange(len(crowded)), edge_len[crowded])
        a_row, b_row = key[rows] // n_v, key[rows] % n_v
        slot = order[rows]
        owners = owner_s[rows]
        thirds = lf[owners, (slot % 3 + 2) % 3]
        axis = v[b_row] - v[a_row]
        axis /= np.maximum(np.linalg.norm(axis, axis=1), 1e-300)[:, None]
        d = v[thirds] - v[a_row]
        d -= (d * axis).sum(axis=1)[:, None] * axis
        first = np.searchsorted(edge_of_row, np.arange(len(crowded)))
        ref = d[first][edge_of_row]
        ref /= np.maximum(np.linalg.norm(ref, axis=1), 1e-300)[:, None]
        side = np.cross(axis, ref)
        angle = np.arctan2((d * side).sum(axis=1), (d * ref).sum(axis=1))
        fwd = starts[slot] == a_row
        bounds = np.append(first, len(rows))
        angle_l, fwd_l, owners_l = angle.tolist(), fwd.tolist(), owners.tolist()
        for k in range(len(crowded)):
            s0, s1 = int(bounds[k]), int(bounds[k + 1])
            crowded_pairs.extend(
                _pair_round_edge(owners_l[s0:s1], fwd_l[s0:s1], angle_l[s0:s1])
            )
    if crowded_pairs:
        cp = np.asarray(crowded_pairs, dtype=np.int64)
        links_a.append(cp[:, 0])
        links_b.append(cp[:, 1])

    label = _components(len(lf), np.concatenate(links_a), np.concatenate(links_b))
    roots, piece_of = np.unique(label, return_inverse=True)
    piece_of = piece_of.reshape(-1)
    del label

    # Classify every edge that is not ordinary two-triangle surface.
    open_by_piece = np.zeros(len(roots), dtype=np.int64)
    one = edge_start[edge_len == 1]
    np.add.at(open_by_piece, piece_of[owner_s[one]], 1)
    pinch_by_piece = np.zeros(len(roots), dtype=np.int64)
    touch: dict[tuple[int, int], int] = {}
    pinch_points: list[tuple[float, float, float]] = []
    pinch_total = 0
    touch_total = 0
    if len(crowded):
        pieces_on = piece_of[owners]
        low_p = np.minimum.reduceat(pieces_on, first)
        high_p = np.maximum.reduceat(pieces_on, first)
        within = low_p == high_p
        pinch_total = int(within.sum())
        touch_total = int((~within).sum())
        np.add.at(pinch_by_piece, low_p[within], 1)
        for k in np.nonzero(within)[0][:max_points]:
            edge_key = key[int(edge_start[crowded[k]])]
            mid = (v[int(edge_key // n_v)] + v[int(edge_key % n_v)]) / 2.0
            pinch_points.append(tuple(_rounded(float(x)) for x in mid))
        for k in np.nonzero(~within)[0]:
            on = sorted(set(pieces_on[int(bounds[k]):int(bounds[k + 1])].tolist()))
            for i in range(len(on)):
                for j in range(i + 1, len(on)):
                    pair = (on[i], on[j])
                    touch[pair] = touch.get(pair, 0) + 1

    # Pieces, largest first, with where each sits.
    counts = np.bincount(piece_of, minlength=len(roots))
    lows = np.full((len(roots), 3), np.inf)
    highs = np.full((len(roots), 3), -np.inf)
    for corner in range(3):
        points = v[lf[:, corner]]
        np.minimum.at(lows, piece_of, points)
        np.maximum.at(highs, piece_of, points)
    rank = np.argsort(-counts, kind="stable")
    new_index = np.empty_like(rank)
    new_index[rank] = np.arange(len(rank))
    pieces = tuple(
        Piece(
            faces=int(counts[p]),
            sealed=bool(open_by_piece[p] == 0 and pinch_by_piece[p] == 0),
            center=tuple(float(x) for x in (lows[p] + highs[p]) / 2.0),
            size=tuple(float(x) for x in highs[p] - lows[p]),
        )
        for p in rank
    )
    touching = tuple(
        (int(min(new_index[a], new_index[b])), int(max(new_index[a], new_index[b])), int(n))
        for (a, b), n in sorted(touch.items(), key=lambda item: -item[1])
    )
    ranked = new_index[piece_of]
    face_piece[live_idx] = ranked
    joined, solid_of, untested = _solids(v, lf, ranked, pieces, lows[rank], highs[rank])
    return BodyReading(
        pieces=pieces,
        open_edges=int(len(one)),
        pinch_edges=pinch_total,
        touching_edges=touch_total,
        touching=touching,
        weld_tolerance=tol,
        face_piece=face_piece,
        pinch_at=tuple(pinch_points),
        edge_count=int(len(edge_start)),
        joined=joined,
        solid_of=solid_of,
        untested_pairs=untested,
    )


def _solids(
    v: np.ndarray,
    faces: np.ndarray,
    piece: np.ndarray,
    pieces: tuple[Piece, ...],
    lows: np.ndarray,
    highs: np.ndarray,
) -> tuple[tuple[tuple[int, int, str], ...], tuple[int, ...], int]:
    """Which pieces print as one solid (see the module docstring).

    *faces* are the welded live triangles, *piece* the piece of each, and
    *lows* / *highs* each piece's box, all in ``pieces`` order.

    :returns: ``(joined, solid_of, untested_pairs)`` for :class:`BodyReading`.
    """
    n = len(pieces)
    if n < 2:
        return (), tuple(range(n)), 0
    joined: dict[tuple[int, int], str] = {}

    # Shared surface: a triangle of one piece lies on a triangle of another,
    # the same three corners once they are welded.
    corners = np.sort(faces, axis=1)
    _, same, count = np.unique(corners, axis=0, return_inverse=True, return_counts=True)
    same = same.reshape(-1)
    doubled = np.nonzero(count[same] > 1)[0]
    if len(doubled):
        doubled = doubled[np.argsort(same[doubled], kind="stable")]
        group = same[doubled]
        starts = np.nonzero(np.r_[True, group[1:] != group[:-1]])[0]
        lo = np.minimum.reduceat(piece[doubled], starts)
        hi = np.maximum.reduceat(piece[doubled], starts)
        for a, b in set(zip(lo[lo != hi].tolist(), hi[lo != hi].tolist(), strict=True)):
            joined[(a, b)] = "surface"
        # Three or more pieces on one triangle are rare; each joins the first,
        # which groups them all at a cost of one pair a piece.
        ends = np.r_[starts[1:], len(doubled)]
        for k in np.nonzero(ends - starts > 2)[0].tolist():
            on = sorted(set(piece[doubled[starts[k]:ends[k]]].tolist()))
            for b in on[1:]:
                joined.setdefault((on[0], b), "surface")

    # Contact: points just outside one sealed piece's own surface that lie
    # inside another sealed piece.  Only pairs whose boxes meet can touch.
    sealed = np.array([p.sealed for p in pieces])
    tri = v[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    volume = np.zeros(n)
    np.add.at(volume, piece, np.einsum("ij,ij->i", tri[:, 0], cross) / 6.0)
    diagonal = np.linalg.norm(highs - lows, axis=1)
    slack = diagonal.max() * WELD_FRACTION
    # A sweep along the axis the pieces are most spread out on: each sealed
    # piece meets only those that start before it ends, so a scene of
    # thousands of pieces costs little more than its sort (along an axis
    # every piece spans, thirty thousand rods would meet each other).  Two
    # boxes that meet only along an edge or at a corner hold pieces that
    # cannot share a face or overlap, so only boxes that share an area or a
    # volume are paired.
    spread = np.ptp((lows + highs) / 2.0, axis=0) / np.maximum((highs - lows).mean(axis=0), 1e-300)
    axis = int(np.argmax(spread))
    by_axis = np.argsort(lows[:, axis], kind="stable")
    begins = lows[by_axis, axis]
    pairs: list[tuple[int, int]] = []
    unswept = 0
    looked = 0
    for k, a in enumerate(by_axis.tolist()):
        if not sealed[a]:
            continue
        stop = int(np.searchsorted(begins, highs[a, axis] + slack, side="right"))
        looked += stop - k - 1
        if len(pairs) > 16 * _MAX_CONTACT_PAIRS or looked > _MAX_SWEEP_LOOKS:
            # A scene this crowded is past the budget anyway: every piece
            # left unswept counts as at least one untested pair.
            unswept += 1
            continue
        near = by_axis[k + 1 : stop]
        near = near[sealed[near]]
        reach = np.minimum(highs[near], highs[a]) - np.maximum(lows[near], lows[a])
        meet = np.all(reach >= -slack, axis=1) & ((reach > slack).sum(axis=1) >= 2)
        for b in near[meet].tolist():
            pair = (min(a, b), max(a, b))
            if pair not in joined:
                pairs.append(pair)

    def faces_of(i: int) -> np.ndarray:
        return order[bounds[i]:bounds[i + 1]]

    order = np.argsort(piece, kind="stable")
    bounds = np.searchsorted(piece[order], np.arange(n + 1))
    # Pieces already joined, so a pair inside one solid is never tested.
    root = list(range(n))

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    for a, b in joined:
        root[find(a)] = find(b)

    inside_out = np.array([i for i in range(n) if sealed[i] and volume[i] < 0.0], dtype=np.int64)
    held: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    held_faces = 0

    def walls_of(host: int) -> np.ndarray:
        """The host's own inner walls (inside-out pieces within its box), so
        a hollow part's cavity reads as the air it is."""
        if not len(inside_out):
            return inside_out
        return inside_out[
            np.all(lows[inside_out] >= lows[host] - slack, axis=1)
            & np.all(highs[inside_out] <= highs[host] + slack, axis=1)
        ]

    def material_size(host: int, walls: np.ndarray) -> int:
        return int(bounds[host + 1] - bounds[host]) + int(sum(bounds[j + 1] - bounds[j] for j in walls))

    def material_of(host: int, walls: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The host's triangles with its inner walls, and each one's box in
        plan; kept for the next test of the same host while the held total
        stays under :data:`_MAX_HELD_MATERIAL`."""
        nonlocal held_faces
        if host in held:
            return held[host]
        faces = np.concatenate([faces_of(host), *(faces_of(int(j)) for j in walls)])
        t = tri[faces]
        built = (faces, t[:, :, :2].min(axis=1), t[:, :, :2].max(axis=1), walls)
        if held_faces + len(faces) <= _MAX_HELD_MATERIAL:
            held[host] = built
            held_faces += len(faces)
        return built

    tests = 0
    work = 0
    untested = unswept
    for a, b in pairs:
        if find(a) == find(b):
            continue
        small, host = (a, b) if diagonal[a] <= diagonal[b] else (b, a)
        if volume[host] <= 0.0:
            # An inside-out piece is the inner wall of a hollow part; its
            # material is the part's, which the outer wall's pair asks about.
            continue
        if tests >= _MAX_CONTACT_PAIRS or work >= _MAX_CONTACT_WORK:
            untested += 1
            continue
        # Reading a host's material is work too, charged before it is done:
        # once, while it is held; every time, when it is not.
        if host not in held:
            walls = walls_of(host)
            size = material_size(host, walls)
            if work + size > _MAX_CONTACT_WORK:
                untested += 1
                continue
            work += size
        else:
            walls = held[host][3]
        faces, plan_lo, plan_hi, walls = material_of(host, walls)
        if small in walls:
            faces = faces[piece[faces] != small]
            plan_lo, plan_hi = tri[faces, :, :2].min(axis=1), tri[faces, :, :2].max(axis=1)
            work += len(faces)
        met, cost = _in_contact(
            tri, cross, faces_of(small), volume[small], diagonal[small],
            faces, plan_lo, plan_hi, budget=_MAX_CONTACT_WORK - work,
        )
        # Every test costs at least its probes, so none is free to repeat.
        work += max(cost, _PROBES)
        if met is None:
            untested += 1
            continue
        tests += 1
        if met:
            joined[(a, b)] = "contact"
            root[find(a)] = find(b)

    label = _components(n, np.array([a for a, _ in joined], dtype=np.int64),
                        np.array([b for _, b in joined], dtype=np.int64))
    return (
        tuple((a, b, how) for (a, b), how in sorted(joined.items())),
        tuple(int(x) for x in label),
        untested,
    )


def _in_contact(
    tri: np.ndarray,
    cross: np.ndarray,
    own: np.ndarray,
    own_volume: float,
    own_diagonal: float,
    host: np.ndarray,
    plan_lo: np.ndarray,
    plan_hi: np.ndarray,
    *,
    budget: int,
) -> tuple[bool | None, int]:
    """Whether the piece of triangles *own* lies flush against, or sinks
    into, the material bounded by the triangles *host* (with each one's box
    in plan, *plan_lo* / *plan_hi*), and the work the answer cost.

    ``None`` when answering would cost more than *budget*: the pair is left
    untested rather than read wrong.

    Probes sit just outside *own*'s surface (outward by its winding, which
    its signed volume orients), and each casts a ray straight up through
    *host*, adding one for every surface it leaves through and taking one
    for every surface it enters: a positive count is inside material, and
    a hollow part's cavity counts nothing because the ray enters its inner
    wall before it leaves the outer one.  Material right outside *own* is a
    shared face or an overlap, and either way the two print as one solid;
    two pieces meeting along a line or at a point keep every probe in air.
    """
    picked = own[np.unique(np.linspace(0, len(own) - 1, min(len(own), _PROBES)).astype(np.int64))]
    area = np.linalg.norm(cross[picked], axis=1)
    picked, area = picked[area > 0.0], area[area > 0.0]
    if len(picked) == 0 or own_volume == 0.0:
        return False, 0
    depth = _PROBE_DEPTH_FRACTION * own_diagonal
    outward = np.sign(own_volume) * cross[picked] / area[:, None]
    probes = tri[picked].mean(axis=1) + depth * outward
    # Off every edge and corner of the host in plan, by a hair.
    probes[:, 0] += depth * 0.0137
    probes[:, 1] += depth * 0.0071

    # Only a triangle over a probe in plan can cross its ray.
    lo, hi = probes[:, :2].min(axis=0), probes[:, :2].max(axis=0)
    reach = np.all((plan_lo <= hi) & (plan_hi >= lo), axis=1)
    over = host[reach]
    cost = len(host) + len(probes) * len(over)
    if cost > budget:
        return None, len(host)
    t, facing = tri[over], np.sign(cross[over, 2])
    t, facing = t[facing != 0.0], facing[facing != 0.0]
    if len(t) == 0:
        return False, cost
    count = np.zeros(len(probes))
    for s in range(0, len(t), 8192):
        c, up = t[s : s + 8192], facing[s : s + 8192]
        e1, e2 = c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]
        den = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        dx = probes[:, None, 0] - c[None, :, 0, 0]
        dy = probes[:, None, 1] - c[None, :, 0, 1]
        u = (dx * e2[None, :, 1] - dy * e2[None, :, 0]) / den[None, :]
        w = (e1[None, :, 0] * dy - e1[None, :, 1] * dx) / den[None, :]
        z = c[None, :, 0, 2] + u * e1[None, :, 2] + w * e2[None, :, 2]
        crossed = (u > 0.0) & (w > 0.0) & (u + w < 1.0) & (z > probes[:, None, 2])
        count += (crossed * up[None, :]).sum(axis=1)
    inside = int((count > 0.5).sum())
    return inside >= _CONTACT_MIN_PROBES and inside >= _CONTACT_MIN_SHARE * len(probes), cost


def read_triangles(triangles: Any, *, tolerance: float | None = None, max_points: int = 3) -> BodyReading:
    """:func:`read_bodies` for a triangle soup, shape ``(T, 3, 3)`` or a list
    of three-corner tuples, as Kiln's STL reader returns.  The soup is let go
    once its corners are welded, so the reading holds little beside it."""
    flat = np.asarray(triangles, dtype=np.float64).reshape(-1, 3)
    corners = np.arange(len(flat), dtype=np.int64).reshape(-1, 3)
    v, f, tol = weld(flat, corners, tolerance=tolerance)
    del flat, corners
    return _read_welded(v, f, tol, max_points=max_points)


def read_mesh(mesh: Any, **kwargs: Any) -> BodyReading:
    """:func:`read_bodies` for a loaded ``trimesh.Trimesh``."""
    return read_bodies(np.asarray(mesh.vertices), np.asarray(mesh.faces), **kwargs)


def is_sealed(mesh: Any) -> bool:
    """Whether every piece of *mesh* is a closed solid once corners the file
    wrote twice are joined (a glTF splits corners along its texture seams,
    which a plain edge check reads as holes)."""
    if len(getattr(mesh, "faces", ())) == 0:
        return False
    return read_mesh(mesh).sealed
