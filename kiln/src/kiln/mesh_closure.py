"""Whether a mesh's surface is closed, said once for every door.

:func:`surface_closure` is the one answer every door that tells a person
whether a surface is closed reads (validation, the pre-print pipeline,
diagnosis, repair): from Kiln's piece reading (:mod:`kiln.mesh_bodies`),
whether the surface has holes, whether every edge of each piece is shared by
exactly two of its triangles, and the plain sentences that say so, what it
means for printing and what to do.  A door never words closure itself.

The rest of this module answers two narrower questions for the compile gates,
read from the triangle arrays rather than through a trimesh mesh: whether an
STL is watertight and how many of its closed pieces are inside out.  Those
answers are trimesh's, by its own definitions, so a check that switches here
keeps its verdicts:

- vertices merge when their coordinates agree once scaled by 1e8 and rounded
  (``trimesh.grouping.merge_vertices`` at ``tol.merge``), so a triangle two of
  whose corners merge is a line or a point (:func:`collapsed_triangles`);
- two triangles join one piece across an edge exactly two triangles share
  (``trimesh.graph.face_adjacency``), and a piece of under four triangles is
  no piece (``graph.split``);
- a piece is closed when every edge in it is shared by exactly two of its
  triangles (``graph.is_watertight``);
- a piece's volume is trimesh's surface integral (``triangles.mass_properties``).

Where trimesh would do more than that, the functions here return ``None`` and
the caller asks trimesh: a piece that is not closed (trimesh first tries to
patch its small holes), a coordinate that is not finite (trimesh drops it), or
a file that is not an STL.

The pieces are found with numpy alone (:func:`_pieces`): scipy is not one
of Kiln's dependencies, and trimesh's own split needs scipy or networkx to
find pieces, so a reading that leaned on either would return ``None`` on a
plain ``pip install kiln3d`` and the sealed-cavity check would stand aside
there.

Why: a relief carve's output can be a million triangles, and reading it as
plain arrays answers these two questions without building a full trimesh
mesh.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SurfaceClosure:
    """Whether a mesh's surface is closed, and what a person is told about it.

    ``closed``: no edge has a triangle on one side only, so there is no hole.
    ``manifold``: closed, and every edge of each piece is shared by exactly
    two of its triangles: no piece meets itself along a line.  Separate
    pieces that meet each other are each still manifold; ``contact`` says
    how they print.

    ``state`` is the sentence on the surface, ``status`` its verdict as a
    clause, ``fix`` what to do about it (``""`` when nothing), ``found`` the
    defects alone, as ``state`` names them, and ``contact`` the sentence on
    separate pieces that share edges (``""`` when none do).
    """

    closed: bool
    manifold: bool
    open_edges: int
    pinched_edges: int
    touching_edges: int
    status: str
    state: str
    fix: str
    found: str
    contact: str

    def sentence(self) -> str:
        """The state and what to do, as one paragraph."""
        return closure_sentence(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """What a reply carries: the verdicts and the sentences."""
        return {
            "closed": self.closed,
            "manifold": self.manifold,
            "status": self.status,
            "state": self.state,
            "fix": self.fix,
            "found": self.found,
        }


def closure_sentence(block: dict[str, Any]) -> str:
    """The state and what to do from a reply's closure block
    (:meth:`SurfaceClosure.to_dict`), as one paragraph."""
    return " ".join(part for part in (block.get("state"), block.get("fix")) if part)


#: What a person is told to do about holes.
_HOLES_FIX = (
    "Kiln's `repair_mesh` with `close_holes=True` sews small holes shut; an "
    "opening it cannot close has to be closed where the shape was made."
)
_HOLES_LEFT = (
    "Kiln's hole repair could not close them, so they have to be closed where "
    "the shape was made."
)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _point(p: Any) -> str:
    return "(" + ", ".join(f"{float(f'{float(c):.4g}'):g}" for c in p) + ")"


def pinch_fix() -> str:
    """What a person is told to do about a surface that meets itself along a
    line: what it prints as, and where it is fixed."""
    from kiln.fusion_check import embedment_floor_mm

    return (
        "A slicer joins the two sides of such a line by a sliver at most, and "
        "`repair_mesh` cannot change it, as it is not a hole: where the shape "
        f"was made, sink the two sides at least {embedment_floor_mm():g} mm into "
        "each other if they belong together, or move them apart."
    )


def pinched_found(reading: Any) -> str:
    """The pinched edges of *reading*, and where some of them are, in words."""
    where = ""
    if reading.pinch_at:
        where = " (for example at " + ", ".join(_point(p) for p in reading.pinch_at) + ")"
    return (
        f"{_plural(int(reading.pinch_edges), 'pinched edge')}, where the part meets "
        f"itself along a line with more than two triangles on one edge{where}"
    )


def _contact(reading: Any) -> str:
    """The sentence on separate pieces that share edges: those that print as
    one solid sit on each other; those that do not meet only along an edge,
    and print as separate parts."""
    if not reading.touching_edges:
        return ""
    solid = reading.solid_of or tuple(range(reading.piece_count))
    lines: list[str] = []
    for as_one in (True, False):
        pairs = [(a, b, n) for a, b, n in reading.touching if (solid[a] == solid[b]) == as_one]
        if not pairs:
            continue
        pieces = {i for a, b, _ in pairs for i in (a, b)}
        edges = sum(n for _a, _b, n in pairs)
        if as_one:
            lines.append(
                f"{_plural(edges, 'edge')} where {len(pieces)} separate pieces sit on "
                "each other (separate pieces in contact, which is not a defect of "
                "either piece).  Each piece is closed, so the file needs no repair."
            )
            continue
        parts = len({solid[i] for i in pieces})
        unchecked = (
            f" {_plural(reading.untested_pairs, 'pair')} of pieces were not checked "
            "for more contact and are counted apart."
            if reading.untested_pairs else ""
        )
        lines.append(
            f"{_plural(edges, 'edge')} where {len(pieces)} separate pieces meet only "
            "along an edge, with no surface between them.  Each piece is closed, so "
            f"the file needs no repair, but they print as {parts:,} separate parts, "
            f"joined where they meet by a sliver at most.{unchecked}"
        )
    return "  ".join(lines)


def surface_closure(reading: Any, *, repair_tried: bool = False) -> SurfaceClosure:
    """The closure of the mesh *reading* (:func:`kiln.mesh_bodies.read_bodies`)
    describes, in the words every door says.

    :param repair_tried: Kiln's hole repair already ran on this surface, so
        what is left open is said to be beyond it.
    """
    open_edges, pinched = int(reading.open_edges), int(reading.pinch_edges)
    closed = bool(reading.pieces) and open_edges == 0
    manifold = closed and pinched == 0
    found: list[str] = []
    if open_edges:
        found.append(f"{_plural(open_edges, 'open edge')} (holes in the surface)")
    if pinched:
        found.append(pinched_found(reading))
    what = " and ".join(found)
    if manifold:
        status = "closed (watertight)"
        state = "The surface is closed (watertight)."
    elif not reading.pieces:
        status = "empty"
        state = "The file has no surface to close."
    else:
        status = "closed with no holes, but it is not a clean solid" if closed else "not closed"
        state = f"The surface is {status}: {what}."
    fixes = []
    if open_edges:
        fixes.append(_HOLES_LEFT if repair_tried else _HOLES_FIX)
    if pinched:
        fixes.append(pinch_fix())
    return SurfaceClosure(
        closed=closed,
        manifold=manifold,
        open_edges=open_edges,
        pinched_edges=pinched,
        touching_edges=int(reading.touching_edges),
        status=status,
        state=state,
        fix=" ".join(fixes),
        found=what,
        contact=_contact(reading),
    )


#: trimesh's vertex merge: coordinates scaled by 10**8, rounded (tol.merge).
_MERGE_SCALE = 10**8

#: trimesh's ``split`` drops a connected piece with fewer triangles than this.
_MIN_PIECE_FACES = 4

#: The volume below which ``_sealed_hollows`` has always counted a closed
#: piece as inside out (a hollow).
_INSIDE_OUT_VOLUME = -1e-6


def _triangles(path: str) -> np.ndarray | None:
    """The STL's triangles, or ``None`` for anything this module leaves to trimesh."""
    if not str(path).lower().endswith(".stl"):
        return None
    from kiln.generation.validation import read_stl_triangles
    from kiln.mesh_frame import refuse_an_oversized_mesh

    refuse_an_oversized_mesh(path, "stl")
    triangles = read_stl_triangles(Path(path))
    if not np.isfinite(triangles).all():
        return None
    return triangles


def _merged(triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(faces, coordinates)`` after trimesh's vertex merge: each corner's
    vertex id, and each vertex's coordinates from its first occurrence."""
    corners = triangles.reshape(-1, 3)
    rounded = (corners * _MERGE_SCALE).round().astype(np.int64)
    order = np.lexsort((rounded[:, 2], rounded[:, 1], rounded[:, 0]))
    ranked = rounded[order]
    starts = np.empty(len(order), dtype=bool)
    starts[0] = True
    starts[1:] = (ranked[1:] != ranked[:-1]).any(axis=1)
    del ranked, rounded
    ids = np.empty(len(order), dtype=np.int64)
    ids[order] = np.cumsum(starts) - 1
    # lexsort is stable, so a group's first entry is its first occurrence.
    coordinates = corners[order[starts]]
    return ids.reshape(-1, 3), coordinates


def collapsed_triangles(triangles: np.ndarray) -> np.ndarray:
    """Which of *triangles* ``(N, 3, 3)`` have two corners the merge makes one
    vertex (:func:`_merged`): each is a line or a point."""
    rounded = (np.asarray(triangles, dtype=np.float64) * _MERGE_SCALE).round()
    first, second, third = rounded[:, 0], rounded[:, 1], rounded[:, 2]
    return (first == second).all(axis=1) | (second == third).all(axis=1) | (first == third).all(axis=1)


def _edges(faces: np.ndarray, vertex_count: int) -> tuple[np.ndarray, np.ndarray]:
    """Each triangle's three edges as one sortable key per edge, in trimesh's
    order ((a, b), (b, c), (c, a)), and the triangle each edge belongs to."""
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    edges.sort(axis=1)
    keys = edges[:, 0] * vertex_count + edges[:, 1]
    return keys, np.repeat(np.arange(len(faces), dtype=np.int64), 3)


def _pieces(n_faces: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Each triangle's piece, for triangles joined by the pairs ``(a[i], b[i])``.

    Union-find by arrays: each round every pair whose ends sit in two pieces
    hooks the larger root under the smaller, then pointers jump until every
    triangle points at its root.  A root is always the smallest triangle of
    its piece, so the labels -- pieces numbered by their smallest triangle --
    are the ones scipy's ``connected_components`` gives.  A relief carve's
    half-million triangles settle in a dozen rounds or fewer, shuffled or not.
    """
    parent = np.arange(n_faces, dtype=np.int64)
    while True:
        root_a, root_b = parent[a], parent[b]
        apart = root_a != root_b
        if not apart.any():
            break
        root_a, root_b = root_a[apart], root_b[apart]
        np.minimum.at(parent, np.maximum(root_a, root_b), np.minimum(root_a, root_b))
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    _roots, piece = np.unique(parent, return_inverse=True)
    return piece


def is_watertight(path: str) -> bool | None:
    """trimesh's ``is_watertight`` for the STL at *path*: every edge shared by
    exactly two triangles.  ``None`` when the answer is trimesh's to give."""
    triangles = _triangles(path)
    if triangles is None:
        return None
    if len(triangles) == 0:
        return True
    faces, coordinates = _merged(triangles)
    keys, _owner = _edges(faces, len(coordinates))
    _unique, counts = np.unique(keys, return_counts=True)
    return bool((counts == 2).all())


def inside_out_pieces(path: str) -> int | None:
    """How many closed pieces of the STL at *path* enclose negative volume,
    as ``_sealed_hollows`` counts them through trimesh.  ``None`` when a piece
    is not closed (trimesh would try to patch it first) or the answer is
    otherwise trimesh's to give."""
    triangles = _triangles(path)
    if triangles is None:
        return None
    if len(triangles) == 0:
        return 0
    faces, coordinates = _merged(triangles)
    del triangles
    keys, owner = _edges(faces, len(coordinates))
    n_faces = len(faces)

    # Pieces: triangles joined across an edge exactly two of them share.
    order = np.argsort(keys, kind="stable")
    ranked = keys[order]
    first = np.empty(len(ranked), dtype=bool)
    first[0] = True
    first[1:] = ranked[1:] != ranked[:-1]
    starts = np.nonzero(first)[0]
    counts = np.diff(np.append(starts, len(ranked)))
    pairs = starts[counts == 2]
    a, b = owner[order[pairs]], owner[order[pairs + 1]]
    keep = a != b
    a, b = a[keep], b[keep]
    del ranked, first, starts, counts, pairs, keep
    piece = _pieces(n_faces, a, b)
    del a, b
    size = np.bincount(piece)
    counted = size >= _MIN_PIECE_FACES

    # Closed: inside its own piece, every edge is shared by exactly two.
    edge_piece = piece[owner]
    order = np.lexsort((keys, edge_piece))
    ranked_piece, ranked_key = edge_piece[order], keys[order]
    first = np.empty(len(order), dtype=bool)
    first[0] = True
    first[1:] = (ranked_piece[1:] != ranked_piece[:-1]) | (ranked_key[1:] != ranked_key[:-1])
    starts = np.nonzero(first)[0]
    counts = np.diff(np.append(starts, len(order)))
    open_piece = np.zeros(len(size), dtype=bool)
    open_piece[ranked_piece[starts[counts != 2]]] = True
    del order, ranked_piece, ranked_key, first, starts, counts, edge_piece, keys, owner
    if (open_piece & counted).any():
        return None

    # Volume: trimesh's surface integral, the x term over each triangle.
    corners = coordinates[faces]
    cross_x = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 1])[:, 0]
    volume = np.bincount(piece, weights=cross_x * corners[:, :, 0].sum(axis=1), minlength=len(size)) / 6.0
    return int((counted & (volume < _INSIDE_OUT_VOLUME)).sum())
