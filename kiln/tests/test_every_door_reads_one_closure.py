"""Every door that tells a person whether a surface is closed reads one
closure reading (:mod:`kiln.mesh_closure`) and says it in the same words.

Covers, through the registered tools:

- a closed surface whose sliver triangles sit closer together than the weld
  radius reads closed at every door, never folded into pinched edges and
  loose flaps the file does not have;
- a part that meets itself along a line, written exactly or with its two
  sides a rounding apart, is never called manifold, and every door says it
  is closed with no holes, where it meets itself and what to do, in the same
  words and without sending the person to another program;
- a surface with holes names Kiln's own hole repair, and that repair closes
  it;
- separate pieces that meet only along an edge are said to print as
  separate parts, not to sit on each other.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402


def _call(name: str, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _write(path: Path, triangles) -> str:
    tris = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    corners = tris.reshape(-1, 3)
    trimesh.Trimesh(corners, np.arange(len(corners)).reshape(-1, 3), process=False).export(path)
    return str(path)


def _read(path: str) -> np.ndarray:
    from kiln.generation.validation import read_stl_triangles

    return read_stl_triangles(Path(path)).astype(np.float64)


def _prism(profile: list[tuple[float, float]], height: float) -> list:
    """The closed prism over the counter-clockwise *profile*, its caps fanned
    from the profile's first corner."""
    p = [np.array(xy, dtype=float) for xy in profile]
    tris = []
    for i in range(1, len(p) - 1):
        a, b, c = p[0], p[i], p[i + 1]
        tris.append([(*a, 0.0), (*c, 0.0), (*b, 0.0)])
        tris.append([(*a, height), (*b, height), (*c, height)])
    for i in range(len(p)):
        a, b = p[i], p[(i + 1) % len(p)]
        tris.append([(*a, 0.0), (*b, 0.0), (*b, height)])
        tris.append([(*a, 0.0), (*b, height), (*a, height)])
    return tris


def _cells(filled: list[tuple[int, int, int]], size: float = 10.0) -> list:
    """The outer surface of a set of cubes *size* mm on a grid, wound outward."""
    cells = set(filled)
    tris = []
    for cell in cells:
        for normal in np.vstack([np.eye(3), -np.eye(3)]).astype(int):
            if tuple(np.add(cell, normal)) in cells:
                continue
            u = np.roll(np.abs(normal), 1)
            v = np.cross(normal, u)
            centre = np.add(cell, 0.5) + 0.5 * normal
            q = [tuple((centre + s * 0.5 * u + t * 0.5 * v) * size) for s, t in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
    return tris


#: Cubes in a ring whose two ends touch only along one vertical edge, at
#: x = y = 10 mm: one piece that meets itself along a line.
_RING = [(0, 0, 0), (0, -1, 0), (1, -1, 0), (2, -1, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)]


def _sliver_fan(tmp_path: Path) -> str:
    """A 200 x 144 x 136 mm block whose front edge holds three corners in a
    row 0.11 micrometres apart, the middle one set a fifth of that into the
    part: the caps fan two sliver triangles from the far corner onto them, as
    a CAD export leaves where a rim meets a wall.  Closed, every edge on two
    triangles."""
    gap = 1.1e-4
    profile = [(0.0, 144.0), (0.0, 0.0), (100.0 - gap, 0.0), (100.0, 0.2 * gap),
               (100.0 + gap, 0.0), (200.0, 0.0), (200.0, 144.0)]
    return _write(tmp_path / "sliver_fan.stl", _prism(profile, 136.0))


def _ring(tmp_path: Path, *, apart: float = 0.0) -> str:
    """:data:`_RING`, the end cube's copies of the shared corners moved
    *apart* weld radii off them, so the file writes the two sides of the
    line a rounding apart."""
    from kiln.mesh_bodies import weld_tolerance

    tris = np.asarray(_cells(_RING), dtype=float)
    if apart:
        end = np.all((tris[:, :, 0] >= 10.0) & (tris[:, :, 1] >= 10.0) & (tris[:, :, 0] <= 20.0), axis=1)
        on_line = end[:, None] & np.isclose(tris[:, :, 0], 10.0) & np.isclose(tris[:, :, 1], 10.0)
        shift = apart * weld_tolerance(tris.reshape(-1, 3))
        tris[on_line, :2] += shift
    return _write(tmp_path / f"ring_{apart}.stl", tris)


def _torn(tmp_path: Path) -> str:
    box = trimesh.creation.box((20.0, 20.0, 20.0))
    box.apply_translation((0.0, 0.0, 10.0))
    return _write(tmp_path / "torn.stl", box.triangles[2:])


_OTHER_PROGRAMS = ("PrusaSlicer", "MeshLab", "Meshmixer", "Netfabb", "Blender")


# ---------------------------------------------------------------------------
# Slivers finer than the weld: the file is closed, and every door says so
# ---------------------------------------------------------------------------


def test_slivers_closer_than_the_weld_are_not_folded(tmp_path):
    from kiln.mesh_bodies import _COPY_RADII, read_triangles, weld_tolerance

    tris = _read(_sliver_fan(tmp_path))
    # The case is what it says: closed as written, and two corners the weld
    # radius reaches lie across the side two sliver triangles share.
    assert read_triangles(tris, tolerance=0.0).sealed
    radius = weld_tolerance(tris.reshape(-1, 3))
    corners = {tuple(c) for c in tris.reshape(-1, 3)}
    a, b, c = (np.array(p) for p in sorted(
        (p for p in corners if abs(p[0] - 100.0) < 1e-3 and p[1] < 1e-3 and p[2] == 0.0),
        key=lambda p: p[0],
    ))
    assert np.linalg.norm(a - c) < radius
    assert min(np.linalg.norm(a - b), np.linalg.norm(b - c)) > _COPY_RADII * radius

    reading = read_triangles(tris)

    assert reading.piece_count == 1
    assert (reading.open_edges, reading.pinch_edges, reading.touching_edges) == (0, 0, 0)
    assert reading.sealed


def test_every_door_says_the_slivered_surface_is_closed(tmp_path):
    path = _sliver_fan(tmp_path)

    validated = _call("validate_and_prepare_mesh", file_path=path)
    diagnosed = _call("diagnose_mesh", file_path=path)["report"]
    generated = _call("validate_generated_mesh", file_path=path)
    edges = _call("analyze_non_manifold_edges", file_path=path)

    assert validated["result"]["is_manifold"] is True, validated["message"]
    assert "The surface is closed (watertight)." in validated["message"]
    assert diagnosed["is_watertight"] is True, diagnosed["defects"]
    assert diagnosed["self_intersection_count"] == 0
    assert diagnosed["component_count"] == 1
    assert not any("pinched" in d for d in diagnosed["defects"]), diagnosed["defects"]
    assert diagnosed["closure"] == validated["result"]["closure"]
    assert generated["message"] == "Mesh is valid."
    assert edges["is_watertight"] is True


# ---------------------------------------------------------------------------
# A part that meets itself along a line: never manifold, said one way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("apart", [0.0, 0.3], ids=["written-exactly", "a-rounding-apart"])
def test_a_part_that_meets_itself_is_never_called_manifold(tmp_path, apart):
    path = _ring(tmp_path, apart=apart)

    validated = _call("validate_and_prepare_mesh", file_path=path)
    diagnosed = _call("diagnose_mesh", file_path=path)["report"]
    generated = _call("validate_generated_mesh", file_path=path)

    assert validated["result"]["is_manifold"] is False, validated["message"]
    assert "The surface is closed with no holes" in validated["message"], validated["message"]
    closure = validated["result"]["closure"]
    assert (closure["closed"], closure["manifold"]) == (True, False)
    assert closure["state"].startswith("The surface is closed with no holes, but it is not a clean solid: 1 pinched edge")
    assert "(for example at (10, 10, 5))" in closure["state"]
    assert closure["state"] in validated["message"] and closure["fix"] in validated["message"]
    assert "Ready to print" not in validated["message"]
    assert "not closed" not in validated["message"]

    assert diagnosed["is_watertight"] is False
    assert diagnosed["closure"] == closure
    assert closure["state"] in diagnosed["defects"]
    assert closure["fix"] in diagnosed["recommendations"]
    assert generated["validation"]["closed_solid"] is False
    assert closure["fix"] in generated["message"]

    said = " ".join([validated["message"], *diagnosed["defects"], *diagnosed["recommendations"], generated["message"]])
    assert not [name for name in _OTHER_PROGRAMS if name in said], said
    assert "sink the two sides at least 0.4 mm into each other" in closure["fix"]

    # What the fix says of `repair_mesh` is so: it leaves the line as it is.
    repaired = _call("repair_mesh", file_path=path, output_path=str(tmp_path / "repaired.stl"), close_holes=True)
    assert repaired["is_watertight"] is False, repaired
    assert any(line.startswith("Still 1 pinched edge") for line in repaired["unrepaired"]), repaired["unrepaired"]


# ---------------------------------------------------------------------------
# Holes: Kiln's own repair is named, and it closes them
# ---------------------------------------------------------------------------


def test_a_torn_surface_names_kilns_hole_repair_and_it_closes_it(tmp_path):
    path = _torn(tmp_path)

    generated = _call("validate_generated_mesh", file_path=path)
    diagnosed = _call("diagnose_mesh", file_path=path)["report"]
    assert "`repair_mesh` with `close_holes=True`" in generated["message"], generated["message"]
    assert diagnosed["closure"]["fix"] in diagnosed["recommendations"]
    assert "`repair_mesh` with `close_holes=True`" in diagnosed["closure"]["fix"]
    said = " ".join([generated["message"], *diagnosed["recommendations"]])
    assert not [name for name in _OTHER_PROGRAMS if name in said], said

    repaired = str(tmp_path / "repaired.stl")
    out = _call("repair_mesh", file_path=path, output_path=repaired, close_holes=True)
    assert out["is_watertight"] is True, out
    assert _call("validate_generated_mesh", file_path=repaired)["message"] == "Mesh is valid."


# ---------------------------------------------------------------------------
# Pieces that meet along an edge print as separate parts
# ---------------------------------------------------------------------------


def test_pieces_that_meet_only_along_an_edge_print_as_separate_parts(tmp_path):
    path = _write(tmp_path / "two_cubes.stl", _cells([(0, 0, 0), (1, 1, 0)]))

    validated = _call("validate_and_prepare_mesh", file_path=path)
    diagnosed = _call("diagnose_mesh", file_path=path)["report"]

    said = (
        "1 edge where 2 separate pieces meet only along an edge, with no surface between "
        "them.  Each piece is closed, so the file needs no repair, but they print as 2 "
        "separate parts, joined where they meet by a sliver at most."
    )
    assert validated["result"]["is_manifold"] is True
    assert said in validated["result"]["notes"], validated["result"]["notes"]
    assert said in validated["message"]
    assert "sit on each other" not in validated["message"]
    assert said in diagnosed["notes"], diagnosed["notes"]
