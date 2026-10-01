"""The array STL reader and the tuple parser describe a file the same way.

``read_stl_triangles`` is the numpy twin of ``_parse_stl`` for callers that
need only coordinates, and ``_parse_stl`` keeps one tuple per distinct
corner.  Both exist to cut memory; neither may change what a file says.
"""

from __future__ import annotations

import struct
import tracemalloc

import numpy as np
import pytest

from kiln.generation.validation import _parse_stl, analyze_mesh, read_stl_triangles

_RECORD = [("normal", "<f4", (3,)), ("corners", "<f4", (3, 3)), ("attr", "<u2")]


def _binary_stl(path, corners, header=b"\0" * 80):
    record = np.zeros(len(corners), dtype=_RECORD)
    record["corners"] = corners
    with open(path, "wb") as fh:
        fh.write(header.ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(record)))
        record.tofile(fh)
    return path


def _ascii_stl(path, corners):
    facets = "".join(
        "facet normal 0 0 0\nouter loop\n"
        + "".join(f"vertex {x!r} {y!r} {z!r}\n" for (x, y, z) in tri)
        + "endloop\nendfacet\n"
        for tri in corners.tolist()
    )
    path.write_text(f"solid part\n{facets}endsolid part\n")
    return path


def _sphere_stl(path):
    import trimesh

    trimesh.creation.icosphere(subdivisions=6, radius=30.0).export(path)
    return path


_CORNERS = np.random.default_rng(5).uniform(-40.0, 40.0, size=(50, 3, 3)).astype("<f4")


class TestReadStlTriangles:
    def test_binary_matches_the_tuple_parser(self, tmp_path):
        path = _binary_stl(tmp_path / "part.stl", _CORNERS)
        triangles, _vertices = _parse_stl(path, [])
        np.testing.assert_array_equal(read_stl_triangles(path), np.array(triangles))

    def test_ascii_matches_the_tuple_parser(self, tmp_path):
        path = _ascii_stl(tmp_path / "part.stl", _CORNERS.astype(float))
        triangles, _vertices = _parse_stl(path, [])
        np.testing.assert_array_equal(read_stl_triangles(path), np.array(triangles))

    def test_binary_file_whose_header_says_solid(self, tmp_path):
        """A binary header may start with "solid"; the size still says binary."""
        path = _binary_stl(tmp_path / "part.stl", _CORNERS, header=b"solid exported by a CAD tool")
        triangles, _vertices = _parse_stl(path, [])
        np.testing.assert_array_equal(read_stl_triangles(path), np.array(triangles))

    def test_an_ascii_file_with_no_facets_is_no_triangles_quietly(self, tmp_path, recwarn):
        path = tmp_path / "empty.stl"
        path.write_text("solid empty\nendsolid empty\n")
        assert read_stl_triangles(path).shape == (0, 3, 3)
        assert not recwarn.list

    def test_truncated_file_raises_the_parsers_own_reason(self, tmp_path):
        path = _binary_stl(tmp_path / "part.stl", _CORNERS)
        path.write_bytes(path.read_bytes()[:-30])
        errors: list[str] = []
        _parse_stl(path, errors)
        with pytest.raises(ValueError) as caught:
            read_stl_triangles(path)
        assert [str(caught.value)] == errors


class TestTupleParserSharesCorners:
    def test_each_distinct_corner_is_one_tuple(self, tmp_path):
        triangles, vertices = _parse_stl(_sphere_stl(tmp_path / "sphere.stl"), [])
        first_seen: dict[tuple[float, ...], tuple[float, ...]] = {}
        for tri in triangles:
            for corner in tri:
                assert first_seen.setdefault(corner, corner) is corner
        assert len(first_seen) == len(vertices)

    def test_vertex_list_keeps_the_order_callers_have_seen(self, tmp_path):
        """The unique-corner list is the corners collected into a set in file
        order, exactly as before the tuples were shared."""
        triangles, vertices = _parse_stl(_sphere_stl(tmp_path / "sphere.stl"), [])
        collected: set[tuple[float, ...]] = set()
        for tri in triangles:
            collected.update(tri)
        assert vertices == list(collected)

    def test_parsing_a_closed_mesh_holds_a_third_of_the_memory(self, tmp_path):
        """82k-triangle sphere: a tuple per use held 44 MB, one per corner 16 MB."""
        path = _sphere_stl(tmp_path / "sphere.stl")
        tracemalloc.start()
        try:
            _parse_stl(path, [])
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 28 * 1024 * 1024


def test_analyzing_a_closed_mesh_holds_half_the_memory(tmp_path):
    """The analysis every mesh tool's inspection runs: 59 MB on an
    82k-triangle sphere before the corners were shared, 29 MB after, and
    the same measurements either way."""
    path = _sphere_stl(tmp_path / "sphere.stl")
    tracemalloc.start()
    try:
        analysis = analyze_mesh(str(path))
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (analysis.triangle_count, analysis.vertex_count) == (81_920, 40_962)
    assert analysis.is_manifold and analysis.connected_components == 1
    assert peak < 42 * 1024 * 1024
