"""Reinforcing a part rounds its sharp INSIDE corners and leaves its outside edges as they are.

The structural analysis counts a sharp corner only where the material wraps
more than 180 degrees around an edge (``test_sharp_corners_are_inside_corners``):
a rounded inside corner spreads the load a sharp one concentrates, and rounding
an outside edge changes how the part looks and fits for no strength.  Until
2026-10-23 the reinforcement that answers those corners rounded EVERY sharp
edge.  Measured on the parts below: the L-bracket got its one inside corner and
17 outside edges rounded, so it came back 265 mm3 lighter though the fillet it
was asked for adds 29; the pegboard's pegs came back domed and its plate's
edges soft; and the Fusion enclosure's 215 edge runs came to 369,768
triangles, more than the check every edit is graded by reads, so the edit was
refused and its 27 inside corners were never rounded at all.

Each part here is a CAD file, built on the kernel the edge step rounds on,
in the shapes the sharp-corner tests use, and is reinforced through the tool a
person calls, as a plain install answers it.  The base plate is a union on
the mesh made after the edge step, with tests of its own; it is held off here
so the volume measured is the edge step's alone.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402
from tests.test_edge_finish import _box, _cyl, _fuse, _write_step, needs_cad_kernel  # noqa: E402

pytestmark = needs_cad_kernel

RADIUS = 1.5

#: The L-bracket of the sharp-corner tests: a 40 x 60 x 5 base and a 5 mm
#: upright 40 tall.  Its one inside corner runs along y at x = 5, z = 5.
L_CORNER_XZ = (5.0, 5.0)
L_LENGTH = 60.0

#: Round pegs 6 mm across, standing 15 mm on a 100 x 100 x 3 plate.
PEGS = [(x, y) for x in (15, 45, 75) for y in (20, 80)]
PEG_R, PLATE_TOP = 3.0, 3.0


def _l_bracket(tmp_path):
    return _write_step(_fuse(_box(0, 0, 0, 40, 60, 5), _box(0, 0, 0, 5, 60, 40)), tmp_path / "l_bracket.step")


def _pegboard(tmp_path):
    plate = _box(0, 0, 0, 100, 100, 3)
    for x, y in PEGS:
        plate = _fuse(plate, _cyl(x, y, 1, PEG_R, 17))
    return _write_step(plate, tmp_path / "pegboard.step")


def _reinforce(path, tmp_path, monkeypatch):
    """The reply of ``apply_design_reinforcements``, with the base plate held off."""
    import kiln.design_reasoning as dr
    from kiln import server

    monkeypatch.setattr(dr, "_apply_base", lambda *_a, **_k: None)
    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, "apply_design_reinforcements", {
            "file_path": str(path), "output_path": str(tmp_path / "reinforced.stl"), "fillet_radius_mm": RADIUS,
        })
    assert reply.get("success"), reply
    return reply


def _rounding(reply):
    rounded = [a for a in reply["applied"] if a["type"] == "fillet"]
    assert len(rounded) == 1, reply["skipped"]
    return rounded[0]["edges"]


def _meshes(path, reply):
    from kiln.step_import import ensure_mesh_path

    return trimesh.load(ensure_mesh_path(str(path))[0], force="mesh"), trimesh.load(reply["output_path"], force="mesh")


def _nearest(points, vertices, chunk=256):
    """Each point's distance to the nearest of *vertices*."""
    out = np.empty(len(points))
    for i in range(0, len(points), chunk):
        block = points[i:i + chunk]
        out[i:i + chunk] = np.sqrt(((block[:, None, :] - vertices[None, :, :]) ** 2).sum(axis=2).min(axis=1))
    return out


def _away_from_inside_edges(mesh, clearance):
    """The mesh's vertices farther than *clearance* from every sharp inside
    edge, as the structural analysis reads them."""
    from kiln.design_reasoning import _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC, _sharp_inside_edges

    edges, verts, _turns = _sharp_inside_edges(mesh.triangles, _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC)
    a, b = verts[edges[:, 0]], verts[edges[:, 1]]
    ab = b - a
    keep = []
    for p in mesh.vertices:
        t = np.clip(((p - a) * ab).sum(axis=1) / (ab * ab).sum(axis=1), 0.0, 1.0)
        keep.append(np.linalg.norm(p - (a + t[:, None] * ab), axis=1).min() > clearance)
    return mesh.vertices[np.asarray(keep)]


def _assert_outside_untouched(before, after):
    """The box is the box it was, and every point of the part away from an
    inside corner -- its outside corners and outside edges -- is where it was."""
    assert after.bounds == pytest.approx(before.bounds, abs=1e-6)
    kept = _away_from_inside_edges(before, RADIUS + 0.05)
    assert len(kept) >= 8
    moved = _nearest(kept, after.vertices)
    assert moved.max() < 1e-6, f"{int((moved >= 1e-6).sum())} points away from the inside corners moved, by up to {moved.max():.3f} mm"


def _corners(path):
    from kiln import server

    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, "analyze_structural_risks", {"file_path": str(path)})
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "sharp_corner"]


def test_the_inside_corner_of_an_l_bracket_is_rounded_and_its_outside_edges_are_not(tmp_path, monkeypatch):
    step = _l_bracket(tmp_path)
    reply = _reinforce(step, tmp_path, monkeypatch)
    before, after = _meshes(step, reply)
    _assert_outside_untouched(before, after)
    # A round in a square inside corner adds (1 - pi/4) r^2 per mm of corner,
    # and nothing else changed.
    assert after.volume - before.volume == pytest.approx((1 - math.pi / 4) * RADIUS**2 * L_LENGTH, rel=5e-3)
    # The corner is an arc of the asked radius, centred r in from both faces.
    cx, cz = L_CORNER_XZ[0] + RADIUS, L_CORNER_XZ[1] + RADIUS
    x, z = after.vertices[:, 0], after.vertices[:, 2]
    in_corner = (x > L_CORNER_XZ[0] + 1e-6) & (x < cx - 1e-6) & (z > L_CORNER_XZ[1] + 1e-6) & (z < cz - 1e-6)
    assert in_corner.sum() >= 4
    assert np.hypot(x[in_corner] - cx, z[in_corner] - cz) == pytest.approx(RADIUS, abs=1e-3)
    assert _corners(reply["output_path"]) == []
    # And the reply says so: one edge rounded, an inside one.
    assert [(f["corner"], f["size_mm"]) for f in _rounding(reply)["finished"]] == [("inside", RADIUS)]


def test_each_peg_root_is_rounded_and_the_pegs_and_plate_keep_their_edges(tmp_path, monkeypatch):
    step = _pegboard(tmp_path)
    reply = _reinforce(step, tmp_path, monkeypatch)
    before, after = _meshes(step, reply)
    _assert_outside_untouched(before, after)
    # Pappus: the corner's cross-section, (1 - pi/4) r^2, swept around each
    # peg at the radius of its centroid.
    area = (1 - math.pi / 4) * RADIUS**2
    centroid = PEG_R + RADIUS * (10 - 3 * math.pi) / (3 * (4 - math.pi))
    assert after.volume - before.volume == pytest.approx(len(PEGS) * 2 * math.pi * centroid * area, rel=5e-3)
    # Each root is a quarter-round of the asked radius between the peg and the plate.
    centre_rho, centre_z = PEG_R + RADIUS, PLATE_TOP + RADIUS
    for px, py in PEGS:
        v = after.vertices
        rho, z = np.hypot(v[:, 0] - px, v[:, 1] - py), v[:, 2]
        root = (rho > PEG_R + 1e-6) & (rho < centre_rho - 1e-6) & (z > PLATE_TOP + 1e-6) & (z < centre_z - 1e-6)
        assert root.sum() >= 8, (px, py)
        assert np.hypot(rho[root] - centre_rho, z[root] - centre_z) == pytest.approx(RADIUS, abs=1e-3)
    assert _corners(reply["output_path"]) == []
    finished = _rounding(reply)["finished"]
    assert sorted((f["corner"], f["size_mm"]) for f in finished) == [("inside", RADIUS)] * len(PEGS)


def test_a_part_with_no_inside_corner_is_left_as_it_is_and_says_so(tmp_path, monkeypatch):
    import kiln.design_reasoning as dr
    from kiln.edge_finish import NOTHING_FINISHED

    cube = _write_step(_box(0, 0, 0, 20, 20, 20), tmp_path / "cube.step")
    work = tmp_path / "work"
    work.mkdir()
    printer = {"printer_id": None, "nozzle_mm": None, "layer_height_mm": None, "material": None}
    step = dr._apply_edge_finish(cube, str(work), "fillet", RADIUS, dr._SHARP_ANGLE_THRESHOLD_DEG_PUBLIC, printer, None)
    assert (step["success"], step.get("code")) == (False, NOTHING_FINISHED), step.get("note")
    assert step["message"] == (
        "Kiln did not round any edge: the part has no sharp inside corner. Its outside edges are left as they are."
    )
    assert step["edges"]["finished"] == [] and step["edges"]["not_selected"] == 12
    assert list(work.iterdir()) == []

    # Through the tool: nothing to reinforce, and the part comes back as it was.
    with rx.without_kiln_pro():
        from kiln import server

        reply = rx.call(server.mcp, "apply_design_reinforcements", {
            "file_path": cube, "output_path": str(tmp_path / "reinforced.stl"),
        })
    assert reply.get("success") and reply["applied"] == [], reply
    before, after = _meshes(cube, reply)
    assert after.volume == pytest.approx(before.volume) and after.bounds == pytest.approx(before.bounds)


def test_a_real_cad_export_gets_its_inside_corners_rounded_and_keeps_its_outside(tmp_path, monkeypatch):
    """Autodesk Fusion's enclosure: an open box with screw posts."""
    step = rx.stage("enclosure_v1.step", tmp_path / "enclosure")
    corners_before = _corners(step)
    reply = _reinforce(step, tmp_path, monkeypatch)
    before, after = _meshes(step, reply)
    assert after.bounds == pytest.approx(before.bounds, abs=1e-6)
    assert _nearest(before.convex_hull.vertices, after.vertices).max() < 1e-6
    assert after.volume > before.volume  # material went into the corners, none came off the outside
    assert after.is_watertight
    assert len(_corners(reply["output_path"])) < len(corners_before)

    edges = _rounding(reply)
    assert edges["finished"] and {f["corner"] for f in edges["finished"]} == {"inside"}
    # The floor of a screw hole is part of the hole, and stays as it is.
    floors = [s for s in edges["left_sharp"] if "floor of a 4 mm hole" in s["reason"]]
    assert len(floors) == 4 and len(edges["left_sharp"]) == 4
    # The corners still counted are ones rounding did not reach: the next
    # step is not to round them again.
    assert "First, round" not in reply["summary"], reply["summary"]
    assert "It left 4 edges sharp on purpose." in reply["summary"], reply["summary"]
