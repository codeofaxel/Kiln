"""The closure and hollow checks read from triangle arrays give trimesh's answers.

``kiln.mesh_closure`` replaces two trimesh loads in the emboss compile (the
runner's closure check and the hollow check) because a relief carve's million
triangles made them the compile's largest memory step.  Each shape here is one
that could split the two apart: merged and unmerged corners, pieces joined only
along an edge, degenerate and lone triangles, inside-out shells, text STL.
"""

from __future__ import annotations

import sys
import tracemalloc

import numpy as np
import pytest
import trimesh

from kiln import mesh_closure


def _write(tmp_path, name, mesh, *, ascii=False):
    path = tmp_path / f"{name}.stl"
    if ascii:
        path.write_text(trimesh.exchange.stl.export_stl_ascii(mesh))
    else:
        path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    return str(path)


def _soup(triangles):
    t = np.asarray(triangles, dtype=np.float64)
    return trimesh.Trimesh(vertices=t.reshape(-1, 3), faces=np.arange(len(t) * 3).reshape(-1, 3), process=False)


def _trimesh_hollows(path):
    """``_sealed_hollows`` as it read before: trimesh's split and volume."""
    mesh = trimesh.load(path, force="mesh")
    return sum(1 for piece in mesh.split(only_watertight=True) if piece.volume < -1e-6)


def _trimesh_open(path):
    """The runner's closure check as it read before."""
    mesh = trimesh.load(path, force="mesh")
    return bool(len(mesh.faces) > 0 and not mesh.is_watertight)


def _box(extents=(20, 20, 20), at=(0, 0, 0), inside_out=False):
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(at)
    if inside_out:
        box.invert()
    return box


def _shapes():
    box = _box()
    tet = np.array(
        [[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 0], [0, 1, 0], [0, 0, 1]],
         [[0, 0, 0], [0, 0, 1], [1, 0, 0]], [[1, 0, 0], [0, 0, 1], [0, 1, 0]]],
        dtype=float,
    )
    jitter = box.triangles.copy()
    jitter[3, 0] += 3e-9  # merges with its neighbour at trimesh's 1e-8
    return {
        "closed box": box,
        "box with a sealed hollow": trimesh.util.concatenate([box, _box((8, 8, 8), inside_out=True)]),
        "two inside-out boxes": trimesh.util.concatenate(
            [_box((5, 5, 5), (40, 0, 0), True), _box((5, 5, 5), (0, 40, 0), True)]
        ),
        "inside-out sphere": _inside_out_sphere(),
        "boxes touching on one edge": trimesh.util.concatenate([box, _box(at=(20, 20, 0))]),
        "box with a degenerate triangle": _soup(np.vstack([box.triangles, [[[0, 0, 0], [0, 0, 0], [1, 0, 0]]]])),
        "box with a lone triangle": _soup(np.vstack([box.triangles, [[[50, 50, 50], [51, 50, 50], [50, 51, 50]]]])),
        "box with a corner off by 3e-9": _soup(jitter),
        "tetrahedron one way": _soup(tet),
        "tetrahedron the other way": _soup(tet[:, ::-1]),
        "flat closed sliver": _soup([[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 0], [0, 1, 0], [1, 0, 0]],
                                    [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 0], [0, 1, 0], [1, 0, 0]]]),
    }


def _inside_out_sphere():
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=10)
    sphere.invert()
    return sphere


@pytest.mark.parametrize("name", sorted(_shapes()))
@pytest.mark.parametrize("ascii", [False, True], ids=["binary", "text"])
def test_the_arrays_answer_as_trimesh_does(tmp_path, name, ascii):
    mesh = _shapes()[name]
    path = _write(tmp_path, "shape", mesh, ascii=ascii)
    watertight = mesh_closure.is_watertight(path)
    assert watertight is not None
    assert (not watertight) == _trimesh_open(path)
    pieces = mesh_closure.inside_out_pieces(path)
    assert pieces is not None, "a shape whose pieces are all closed is the arrays' to answer"
    assert pieces == _trimesh_hollows(path)


def test_a_piece_with_a_hole_is_left_to_trimesh(tmp_path):
    """trimesh patches small holes before it calls a piece closed; the arrays
    do not imitate that, they step aside."""
    box = _box()
    box.update_faces(np.arange(len(box.faces)) > 1)
    path = _write(tmp_path, "open", box)
    assert mesh_closure.inside_out_pieces(path) is None
    assert mesh_closure.is_watertight(path) is False
    assert _trimesh_open(path) is True


def test_without_scipy_the_count_is_left_to_trimesh(tmp_path, monkeypatch):
    path = _write(tmp_path, "box", _box())
    monkeypatch.setitem(sys.modules, "scipy.sparse", None)
    monkeypatch.setitem(sys.modules, "scipy.sparse.csgraph", None)
    assert mesh_closure.inside_out_pieces(path) is None
    assert mesh_closure.is_watertight(path) is True


def test_a_mesh_that_is_not_an_stl_is_left_to_trimesh(tmp_path):
    path = tmp_path / "box.obj"
    path.write_text(trimesh.exchange.obj.export_obj(_box()))
    assert mesh_closure.inside_out_pieces(str(path)) is None
    assert mesh_closure.is_watertight(str(path)) is None


def test_the_hollow_check_of_a_large_shell_costs_a_fraction_of_a_trimesh_load(tmp_path):
    """A 100k-triangle shell with a sealed hollow: trimesh's split held 94 MB
    to count it, the arrays about 30 MB.  A relief carve is ten times this."""
    from kiln.emboss_generator import _sealed_hollows

    outer = trimesh.creation.icosphere(subdivisions=6, radius=30.0)
    inner = trimesh.creation.icosphere(subdivisions=5, radius=10.0)
    inner.invert()
    path = _write(tmp_path, "shell", trimesh.util.concatenate([outer, inner]))
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        hollows = _sealed_hollows(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert hollows == 1
    assert peak < 60 * 1024 * 1024, f"{peak / 1e6:.0f} MB"


def test_a_dirty_mesh_gives_no_nan_anywhere_in_the_carve_checks(tmp_path):
    """Zero-area slivers, a face written twice, a stray triangle and a box
    touching on one edge: the carve checks answer or step aside, and the face
    record's distances are never nan (a nan reads as "not lost" in every
    comparison, which is how the deviation lens read nan for a month)."""
    from kiln.decoration_faces import _min_distance_to_mesh

    box = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    a, b = box.triangles[0][0], box.triangles[0][1]
    touching = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    touching.apply_translation((20.0, 20.0, 0.0))
    dirty = np.vstack([box.triangles, [[a, a, b]], [[a, (a + b) / 2.0, b]], [box.triangles[1]],
                       [[[50, 50, 50], [51, 50, 50], [50, 51, 50]]], touching.triangles])
    path = _write(tmp_path, "dirty", _soup(dirty))
    for answer in (mesh_closure.inside_out_pieces(path), mesh_closure.is_watertight(path)):
        assert answer is None or isinstance(answer, (bool, int))
    distances = _min_distance_to_mesh(dirty.reshape(-1, 3) + 0.01, dirty, reach=1.0)
    assert not np.isnan(distances).any()
