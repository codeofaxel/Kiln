"""The whole-array readings the printability analysis takes give, to the bit,
what reading the same mesh one face at a time gives.

Each reading here has a face-by-face twin, either kept beside it (for a mesh
the arrays cannot read) or written out below from the helpers the analysis
uses for one face.  The meshes are 32-bit corners, as an STL holds them,
with the cases that sit on a boundary: faces on the plate, faces exactly at
the overhang limit, faces with no area, and negative zeros.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from kiln import mesh_bodies, printability
from kiln.generation import validation


def _meshes(seed: int, count: int = 60) -> list[list[tuple[tuple[float, ...], ...]]]:
    rng = np.random.default_rng(seed)
    out = []
    for trial in range(count):
        n = int(rng.integers(1, 600))
        v = rng.normal(scale=float(rng.choice([0.05, 2.0, 40.0])), size=(n, 3, 3))
        v = v.astype(np.float32).astype(np.float64)
        if trial % 3 == 0:
            v[: n // 3, :, 2] = v[: n // 3, :, 2].min()  # faces on the plate
        if trial % 4 == 0:
            v[: n // 5, 2] = v[: n // 5, 1]  # faces with no area
        if trial % 5 == 0:
            k = max(1, n // 4)  # faces drawn exactly at 45 degrees
            v[:k, 0] = (0.0, 0.0, 5.0)
            v[:k, 1] = (1.0, 0.0, 5.0)
            v[:k, 2] = (0.0, 1.0, 4.0)
        if trial % 7 == 0:
            v[: n // 6, 0, 0] = -0.0
        out.append([tuple(tuple(corner) for corner in tri) for tri in v.tolist()])
    return out


def _bits(values: object) -> str:
    """*values* as their exact spelling, negative zeros included."""
    return repr(values)


def test_volume_area_and_overhangs_agree():
    for triangles in _meshes(1):
        bed = min(corner[2] for tri in triangles for corner in tri)
        corners = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
        assert _bits(validation._surface_sums(corners, bed)) == _bits(
            validation._surface_sums_by_face(triangles, bed)
        )


@pytest.mark.parametrize("layer", [0.1, 0.2, 0.28])
def test_wall_area_by_layer_agrees(layer):
    for triangles in _meshes(2):
        z = [corner[2] for tri in triangles for corner in tri]
        layers = max(2, int((max(z) - min(z)) / layer))
        assert _bits(printability._wall_area_by_layer(triangles, min(z), layer, layers)) == _bits(
            printability._wall_area_by_layer_by_face(triangles, min(z), layer, layers)
        )


def _overhangs_one_by_one(triangles, limit, z_min, layer):
    picked, leans = [], []
    for index, tri in enumerate(triangles):
        if z_min is not None and validation._is_bed_supported_triangle(tri, z_min, layer):
            continue
        nz = printability._normalize(printability._triangle_normal(tri[0], tri[1], tri[2]))[2]
        if nz >= 0:
            continue
        lean = max(0.0, 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, -nz)))))
        if printability._at_overhang_limit(lean, limit):
            picked.append(index)
            leans.append(lean)
    return picked, leans


@pytest.mark.parametrize("limit", [45.0, 50.0, 30.0])
def test_overhanging_faces_agree(limit):
    for triangles in _meshes(3):
        z_min = min(corner[2] for tri in triangles for corner in tri)
        for on_plate in (z_min, None):
            got = printability._overhang_faces(triangles, limit, z_min=on_plate, layer_height=0.2)
            assert _bits(got) == _bits(_overhangs_one_by_one(triangles, limit, on_plate, 0.2))


def test_steepest_free_air_lean_agrees():
    for triangles in _meshes(4):
        z_min = min(corner[2] for tri in triangles for corner in tri)
        exempt = set(range(0, len(triangles), 3))
        want = 0.0
        for index, tri in enumerate(triangles):
            if index in exempt or validation._is_bed_supported_triangle(tri, z_min, 0.2):
                continue
            nz = printability._normalize(printability._triangle_normal(tri[0], tri[1], tri[2]))[2]
            if nz >= 0 or nz <= printability._BRIDGE_FLAT_NZ_LIMIT:
                continue
            want = max(want, max(0.0, 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, -nz))))))
        got = printability._steepest_free_air_lean(triangles, None, z_min, 0.2, exempt)
        assert _bits(got) == _bits(want)


def _chords_one_by_one(points, pa, d_seg, supported):
    out = []
    for cx, cy in points.tolist():
        best = math.inf
        for k in range(printability._BRIDGE_CHORD_DIRECTIONS):
            theta = math.pi * k / printability._BRIDGE_CHORD_DIRECTIONS
            ux, uy = math.cos(theta), math.sin(theta)
            denom = ux * d_seg[:, 1] - uy * d_seg[:, 0]
            with np.errstate(divide="ignore", invalid="ignore"):
                s = ((pa[:, 0] - cx) * uy - (pa[:, 1] - cy) * ux) / denom
                t = np.where(
                    abs(ux) > abs(uy),
                    (pa[:, 0] + s * d_seg[:, 0] - cx) / ux,
                    (pa[:, 1] + s * d_seg[:, 1] - cy) / uy,
                )
            hit = (np.abs(denom) > 1e-12) & (s >= -1e-9) & (s <= 1.0 + 1e-9)
            fwd, bwd = hit & (t > 1e-9), hit & (t < -1e-9)
            if not fwd.any() or not bwd.any():
                continue
            t_f, t_b = np.where(fwd, t, np.inf), np.where(bwd, -t, np.inf)
            i_f, i_b = int(np.argmin(t_f)), int(np.argmin(t_b))
            if supported[i_f] and supported[i_b]:
                best = min(best, float(t_f[i_f] + t_b[i_b]))
        out.append(best)
    return out


def test_bridge_chords_agree():
    rng = np.random.default_rng(5)
    for _ in range(40):
        segments = int(rng.integers(1, 40))
        pa = rng.normal(scale=8.0, size=(segments, 2))
        d_seg = rng.normal(scale=6.0, size=(segments, 2))
        d_seg[: segments // 4, 1] = 0.0  # segments along an axis the probes run on
        supported = rng.random(segments) < 0.7
        points = rng.normal(scale=6.0, size=(int(rng.integers(1, 30)), 2))
        got = printability._supported_chords(points, pa, d_seg, supported).tolist()
        assert _bits(got) == _bits(_chords_one_by_one(points, pa, d_seg, supported))


def test_corner_ids_number_as_numpy_unique_does():
    rng = np.random.default_rng(6)
    for _ in range(40):
        corners = rng.integers(-3, 4, size=(int(rng.integers(1, 300)), 3)).astype(np.float64)
        corners[rng.random(len(corners)) < 0.2, 0] = -0.0
        want = np.unique(corners, axis=0, return_inverse=True)[1].reshape(-1)
        assert np.array_equal(validation._exact_corner_ids(corners), want)


def test_a_long_chain_settles_in_a_few_passes(monkeypatch):
    # A chain of links met in no particular order: labelling it by passing
    # the lowest label one link along per pass takes a pass per link.
    count = 4000
    chain = np.random.default_rng(7).permutation(count)
    passes = []
    real = mesh_bodies._settle

    def counted(label):
        passes.append(1)
        return real(label)

    monkeypatch.setattr(mesh_bodies, "_settle", counted)
    label = mesh_bodies._components(count, chain[:-1], chain[1:])
    assert (label == 0).all()
    assert len(passes) <= 40, f"{len(passes)} passes over the links"
