"""The printability searches read the triangles that can answer, not the whole part.

Three searches asked every triangle of the part every question: whether part
lies under an overhang (once per sample of every downward region, so on a
detailed part the work grew with the square of the triangle count), the wall
and gap ray casts, and which loose pieces float (one pass over every face per
piece).  On a 1M-triangle figure the first alone took most of the hosted
server's 15-minute job limit.  These tests count the work, not the time: how
many triangles each search reads, against how many the whole part holds.
"""

from __future__ import annotations

import dataclasses
from unittest import mock

import numpy as np
import pytest

import kiln.printability as printability
from kiln.fusion_check import judge_reading
from kiln.mesh_bodies import read_triangles

trimesh = pytest.importorskip("trimesh")


def _as_list(tris: np.ndarray) -> list[tuple[tuple[float, ...], ...]]:
    return [tuple(map(tuple, t)) for t in tris.tolist()]


def _blocks_over_a_slab(n: int = 20) -> np.ndarray:
    """A slab with an n x n field of small blocks hovering over it: n * n
    downward regions, each with part below it."""
    parts = [trimesh.creation.box(extents=(4.0 * n + 4.0, 4.0 * n + 4.0, 2.0))]
    for i in range(n):
        for j in range(n):
            block = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
            block.apply_translation((4.0 * i - 2.0 * n + 2.0, 4.0 * j - 2.0 * n + 2.0, 4.0))
            parts.append(block)
    mesh = trimesh.util.concatenate(parts)
    mesh.apply_translation((0.0, 0.0, -mesh.bounds[0][2]))
    return np.asarray(mesh.triangles, dtype=np.float64)


class _GatheredFaces(np.ndarray):
    """Faces that count every row a search gathers from them."""

    gathered = 0

    def __getitem__(self, key):
        out = super().__getitem__(key)
        if isinstance(key, np.ndarray) and out.ndim == 3:
            _GatheredFaces.gathered += out.shape[0]
            return out.view(np.ndarray)
        return out


class _ComparedPieces(np.ndarray):
    """Piece labels that count every label compared."""

    compared = 0

    def __eq__(self, other):
        _ComparedPieces.compared += self.size
        return super().__eq__(other)


class TestThePartBelowSearch:
    def test_reads_fewer_faces_than_the_part_holds(self):
        tris = _blocks_over_a_slab()
        triangles = _as_list(tris)
        z_min = float(tris[:, :, 2].min())
        regions = printability._analyze_downward_regions(triangles, z_min, layer_height=0.2, tris=tris)
        assert len(regions) >= 400
        _GatheredFaces.gathered = 0
        summaries = printability._summarize_regions(
            triangles, regions, z_min, layer_height=0.2, tris=tris.view(_GatheredFaces),
        )
        assert sum(region.over_model for region in summaries) >= 400
        # Reading the whole part for every sample read it hundreds of times over.
        assert _GatheredFaces.gathered <= len(tris)


class TestTheWallCast:
    def test_tests_a_small_share_of_ray_triangle_pairs(self):
        sphere = trimesh.creation.icosphere(subdivisions=5, radius=20.0)
        tris = np.asarray(sphere.triangles, dtype=np.float64)
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        normals = np.cross(e1, e2)
        lengths = np.linalg.norm(normals, axis=1)
        comps = np.zeros(len(tris), dtype=np.int64)
        pick = np.random.default_rng(0).choice(len(tris), 500, replace=False)
        inward = -normals[pick] / lengths[pick, None]
        origins = tris[pick].mean(axis=1) + 1e-4 * inward

        pairs = 0
        real = printability._raycast_min_distances

        def counting(origins, directions, v0, *args, **kwargs):
            nonlocal pairs
            if kwargs.get("boxes") is None:
                pairs += origins.shape[0] * v0.shape[0]
            return real(origins, directions, v0, *args, **kwargs)

        with mock.patch.object(printability, "_raycast_min_distances", counting):
            dist = printability._cast_within_components(
                origins, inward, comps[pick], v0, e1, e2, lengths / 2.0, comps,
            )
        # Every inward ray crosses the sphere and leaves through its far side.
        assert np.isfinite(dist).all()
        assert pairs <= 0.05 * len(origins) * len(tris)


class TestWhichPiecesFloat:
    def test_reads_the_piece_labels_once_not_once_per_piece(self):
        parts = []
        for i in range(200):
            cube = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
            cube.apply_translation((3.0 * i, 0.0, 0.5 + (2.0 if i % 2 else 0.0)))
            parts.append(cube)
        triangles = _as_list(np.asarray(trimesh.util.concatenate(parts).triangles, dtype=np.float64))
        reading = read_triangles(triangles)
        assert reading.piece_count == 200
        _ComparedPieces.compared = 0
        counted = dataclasses.replace(reading, face_piece=reading.face_piece.view(_ComparedPieces))
        judge_reading(triangles, counted)
        # One pass over every face per piece compared 200 times the faces.
        assert _ComparedPieces.compared <= len(triangles)
