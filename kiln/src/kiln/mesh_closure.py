"""Whether an STL's surface is closed, and how many of its closed pieces are
inside out, read from the triangle arrays rather than through a trimesh mesh.

The answers are trimesh's, by its own definitions, so a check that switches
here keeps its verdicts:

- vertices merge when their coordinates agree once scaled by 1e8 and rounded
  (``trimesh.grouping.merge_vertices`` at ``tol.merge``);
- two triangles join one piece across an edge exactly two triangles share
  (``trimesh.graph.face_adjacency``), and a piece of under four triangles is
  no piece (``graph.split``);
- a piece is closed when every edge in it is shared by exactly two of its
  triangles (``graph.is_watertight``);
- a piece's volume is trimesh's surface integral (``triangles.mass_properties``).

Where trimesh would do more than that, the functions here return ``None`` and
the caller asks trimesh: a piece that is not closed (trimesh first tries to
patch its small holes), a coordinate that is not finite (trimesh drops it),
a file that is not an STL, or no scipy to find the pieces.

Why (2026-10-30): a relief carve's output is a million triangles, and loading
it as a trimesh mesh to answer these two questions was the largest memory
step of the carve, 1.15 GB for a 90 mm coaster's hollow check.  The arrays
answer it in about a third of that.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

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


def _edges(faces: np.ndarray, vertex_count: int) -> tuple[np.ndarray, np.ndarray]:
    """Each triangle's three edges as one sortable key per edge, in trimesh's
    order ((a, b), (b, c), (c, a)), and the triangle each edge belongs to."""
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    edges.sort(axis=1)
    keys = edges[:, 0] * vertex_count + edges[:, 1]
    return keys, np.repeat(np.arange(len(faces), dtype=np.int64), 3)


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
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
    except ImportError:
        return None
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
    graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n_faces, n_faces)).tocsr()
    _n, piece = connected_components(graph, directed=False)
    del graph, a, b
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
