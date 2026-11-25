"""A file's separate parts are counted as they print, one count for every reply.

Pieces standing closer than a printer keeps apart print stuck together as
one part, whether the reading of the file's pieces joined them
(:mod:`kiln.mesh_bodies`) or the gap between them was measured
(:func:`kiln.fusion_check.judge_reading`).  The printability report counts
its separate parts by :func:`kiln.fusion_check.printed_parts`, the one
reading a reply's count of parts is taken from, so no reply says "it prints
as 3 separate parts" beside a check that says it prints as one.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402


def _cubes(tmp_path, name: str, xs: list[float]) -> str:
    """10 mm cubes on the bed, the left face of each at *xs*."""
    cubes = []
    for x in xs:
        cube = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        cube.apply_translation((x + 5.0, 5.0, 5.0))
        cubes.append(cube)
    path = tmp_path / name
    trimesh.util.concatenate(cubes).export(path)
    return str(path)


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _printed_count(path: str) -> int:
    """How many parts the file prints as, by the one shared reading."""
    from kiln.fusion_check import judge_reading, printed_parts
    from kiln.mesh_bodies import read_triangles
    from kiln.printability import _parse_mesh

    triangles, _vertices = _parse_mesh(path)
    reading = read_triangles(triangles)
    judged = judge_reading(triangles, reading, clearances=True)
    return len(set(printed_parts(reading, judged.get("clearances"))))


def test_two_pieces_closer_than_a_printer_keeps_apart_print_as_one(tmp_path):
    path = _cubes(tmp_path, "pair.stl", [0.0, 10.04])
    reply = _call("analyze_printability", file_path=path)
    pieces = reply["report"]["pieces"]
    assert "It prints as one solid, though it is drawn as 2 pieces: two of them sit 0.04 mm apart" in reply["message"]
    assert "separate parts" not in reply["message"], reply["message"]
    assert (pieces["piece_count"], pieces["solid_count"]) == (2, 1), pieces
    assert _printed_count(path) == pieces["solid_count"]


def test_the_count_said_is_the_count_printed(tmp_path):
    """Two cubes 0.04 mm apart and a third 10 mm off: two parts, the pair
    said as pieces that print stuck together, and no third part counted."""
    path = _cubes(tmp_path, "three.stl", [0.0, 10.04, 30.04])
    reply = _call("analyze_printability", file_path=path)
    pieces = reply["report"]["pieces"]
    message = reply["message"]
    assert "It prints as 2 separate parts on the plate, not one: the largest is 20 x 10 x 10 mm" in message, message
    assert "Two of its pieces drawn apart sit 0.04 mm apart, closer than a printer keeps apart" in message, message
    assert "3 separate parts" not in message, message
    assert pieces["solid_count"] == 2 and len(pieces["parts_mm"]) == 2, pieces
    assert pieces["parts_mm"][0] == pytest.approx([20.04, 10.0, 10.0], abs=0.01), pieces
    assert _printed_count(path) == pieces["solid_count"]


def test_parts_a_real_gap_apart_are_counted_apart(tmp_path):
    """Control: cubes a clearance apart print as three parts, said as three."""
    path = _cubes(tmp_path, "spaced.stl", [0.0, 10.4, 20.8])
    reply = _call("analyze_printability", file_path=path)
    assert reply["report"]["pieces"]["solid_count"] == 3
    assert "It prints as 3 separate parts on the plate, not one" in reply["message"], reply["message"]


def test_printed_parts_joins_solids_and_touching_pairs():
    """The reading itself: pieces of one solid share a part, and so do
    solids a touching pair names; parts are numbered by their first piece."""
    from types import SimpleNamespace

    from kiln.fusion_check import printed_parts

    reading = SimpleNamespace(piece_count=5, solid_of=(0, 0, 2, 3, 4))
    assert printed_parts(reading) == [0, 0, 1, 2, 3]
    touching = {"touching": [{"bodies": [3, 5], "gap_mm": 0.04}]}
    assert printed_parts(reading, touching) == [0, 0, 1, 2, 1]
    assert printed_parts(reading, {"pairs": [{"bodies": [1, 4], "gap_mm": 0.3}]}) == [0, 0, 1, 2, 3]
