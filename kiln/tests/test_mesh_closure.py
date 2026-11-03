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


def _a_plain_install(monkeypatch):
    """What ``pip install kiln3d`` has: neither scipy nor networkx (both only
    in the dev extra).  trimesh imports both when it is first imported and,
    finding neither, keeps stand-ins that raise, so its split finds no pieces
    either ("no graph engines available!").  Both halves are reproduced: the
    modules hidden from anything importing them now, and trimesh's own
    references swapped for the stand-ins it would have kept."""
    from trimesh import exceptions
    from trimesh import graph as trimesh_graph

    for name in ("scipy", "scipy.sparse", "scipy.sparse.csgraph", "networkx"):
        monkeypatch.setitem(sys.modules, name, None)
    missing = exceptions.ExceptionWrapper(ImportError("not installed on a plain install"))
    for name in ("coo_matrix", "csgraph", "nx"):
        monkeypatch.setattr(trimesh_graph, name, missing, raising=False)


def test_without_scipy_the_arrays_still_count(tmp_path, monkeypatch):
    """Until 2026-11-02 the pieces were scipy's, and with no scipy this stood
    aside (``None``).  A/B: fails on public ac3ada47, passes after."""
    path = _write(tmp_path, "hollow", trimesh.util.concatenate([_box(), _box((8, 8, 8), inside_out=True)]))
    _a_plain_install(monkeypatch)
    assert mesh_closure.inside_out_pieces(path) == 1
    assert mesh_closure.is_watertight(path) is True


def test_the_sealed_cavity_check_runs_on_a_plain_install(tmp_path, monkeypatch):
    """The check itself, the way the emboss compile asks it.  On a plain
    install it read nothing (``None``) and every sealed hollow a compile
    added went unsaid.  A/B: fails on public ac3ada47, passes after."""
    from kiln.emboss_generator import _sealed_hollows

    path = _write(tmp_path, "hollow", trimesh.util.concatenate([_box(), _box((8, 8, 8), inside_out=True)]))
    _a_plain_install(monkeypatch)
    assert _sealed_hollows(path) == 1


@pytest.mark.parametrize("name", sorted(_shapes()))
def test_the_pieces_are_the_ones_scipy_finds(tmp_path, name):
    """Where scipy is installed (the server, the dev extra), the numpy count
    labels every triangle exactly as scipy's ``connected_components`` did."""
    sparse = pytest.importorskip("scipy.sparse")
    csgraph = pytest.importorskip("scipy.sparse.csgraph")
    mesh = trimesh.load(_write(tmp_path, "shape", _shapes()[name]), force="mesh", process=False)
    adjacency = mesh.face_adjacency
    a, b = adjacency[:, 0].astype(np.int64), adjacency[:, 1].astype(np.int64)
    n = len(mesh.faces)
    graph = sparse.coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n)).tocsr()
    _count, theirs = csgraph.connected_components(graph, directed=False)
    np.testing.assert_array_equal(mesh_closure._pieces(n, a, b), theirs)


def test_a_shuffled_large_mesh_gets_scipys_pieces():
    """Face order decides how many rounds the union-find takes; a shuffled
    shell of half a million triangles still settles, to scipy's labels."""
    csgraph = pytest.importorskip("scipy.sparse.csgraph")
    sparse = pytest.importorskip("scipy.sparse")
    shells = [trimesh.creation.icosphere(subdivisions=6, radius=30.0)]
    for i in range(3):
        small = trimesh.creation.icosphere(subdivisions=4, radius=5.0)
        small.apply_translation((100.0 + 20.0 * i, 0.0, 0.0))
        shells.append(small)
    mesh = trimesh.util.concatenate(shells)
    order = np.random.default_rng(7).permutation(len(mesh.faces))
    mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[order], process=False)
    adjacency = mesh.face_adjacency
    a, b = adjacency[:, 0].astype(np.int64), adjacency[:, 1].astype(np.int64)
    n = len(mesh.faces)
    graph = sparse.coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n)).tocsr()
    count, theirs = csgraph.connected_components(graph, directed=False)
    ours = mesh_closure._pieces(n, a, b)
    assert count == 4
    np.testing.assert_array_equal(ours, theirs)


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


def _dirty_mesh():
    """Zero-area slivers, a face written twice, a stray triangle and a box
    touching another on one edge."""
    box = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    a, b = box.triangles[0][0], box.triangles[0][1]
    touching = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    touching.apply_translation((20.0, 20.0, 0.0))
    return np.vstack([box.triangles, [[a, a, b]], [[a, (a + b) / 2.0, b]], [box.triangles[1]],
                      [[[50, 50, 50], [51, 50, 50], [50, 51, 50]]], touching.triangles])


def test_a_dirty_mesh_gets_trimeshs_own_answers(tmp_path):
    """Where the arrays answer a dirty mesh at all, they give trimesh's own
    answer, read the way the checks' trimesh path reads it."""
    from kiln.mesh_frame import load_mesh

    path = _write(tmp_path, "dirty", _soup(_dirty_mesh()))
    mesh = load_mesh(path, force="mesh")
    closed = mesh_closure.is_watertight(path)
    if closed is not None:
        assert closed is bool(mesh.is_watertight)
    hollows = mesh_closure.inside_out_pieces(path)
    if hollows is not None:
        assert hollows == sum(1 for piece in mesh.split(only_watertight=True) if piece.volume < -1e-6)
    assert (closed, hollows) != (None, None), "the arrays answered neither question"


def test_a_zero_area_sliver_is_measured_not_dropped():
    """A sliver whose corners lie on one line has no plane, so no plane
    filter can judge it: the face record measures it as the segment it is.
    Dropped instead, a point beside it read as farther than the reach, the
    way a nan reads "not lost" in every comparison."""
    from kiln.decoration_faces import _min_distance_to_mesh

    sliver = np.array([[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [5.0, 0.0, 0.0]]])
    points = np.array([[5.0, 0.05, 0.0], [2.0, 0.0, 0.03], [9.0, -0.02, 0.0]])
    distances = _min_distance_to_mesh(points, sliver, reach=0.1)
    np.testing.assert_allclose(distances, [0.05, 0.03, 0.02], atol=1e-12)
