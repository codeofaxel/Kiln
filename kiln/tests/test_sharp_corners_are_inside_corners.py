"""A sharp corner is an INSIDE corner: where the material wraps more than 180
degrees around an edge, as at the root of a peg, the inside of an L-bracket or
the base of a rib.  That is where load concentrates and cracks start.

Until 2026-10-23 the structural analysis counted every edge where two faces
met at a steep angle, so it read the outside edges of a box, the rim of a
cylinder and the facets of a cone as "clusters of sharp edges", and called a
cluster critical once it held ten facets: a plain pegboard got 22 warnings,
nearly all outer edges, a cylinder 20 critical ones, and the one corner of an
L-bracket that matters was never found, because it is a single long edge.
Each warning costs a part five points of readiness in the design audit.

The parts here are joined the way a real part is (a boolean union), and each
is read through the tool a person calls, as a plain install answers it.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests import _real_exports as rx  # noqa: E402


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _post(x, y, z0, z1, r, sections=48):
    c = trimesh.creation.cylinder(radius=r, height=z1 - z0, sections=sections)
    c.apply_translation((x, y, (z0 + z1) / 2.0))
    return c


def _union(*parts):
    return trimesh.boolean.union(list(parts), engine="manifold")


#: An L-bracket: a 40 x 60 x 5 base and a 5 mm upright 40 tall.  Its one
#: inside corner runs along y at x = 5, z = 5.
def _l_bracket():
    return _union(_box((0, 0, 0), (40, 60, 5)), _box((0, 0, 0), (5, 60, 40)))


#: Round pegs, 6 mm across, at these centres on a 100 x 100 x 3 plate.
PEGS = [(x, y) for x in (15, 45, 75) for y in (20, 80)]


def _pegboard(sections=48):
    return _union(_box((0, 0, 0), (100, 100, 3)), *[_post(x, y, 3, 18, 3.0, sections) for x, y in PEGS])


def _filleted_l():
    """The L-bracket with its inside corner rounded to a 4 mm radius."""
    block = _box((5, 0, 5), (9, 60, 9))
    cut = trimesh.creation.cylinder(radius=4.0, height=62, sections=64)
    cut.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, (1, 0, 0)))
    cut.apply_translation((9, 30, 9))
    return _union(_l_bracket(), trimesh.boolean.difference([block, cut], engine="manifold"))


def _chamfered_l():
    """The L-bracket with a 4 mm 45-degree chamfer in its inside corner."""
    wedge = trimesh.Trimesh(
        vertices=[(5, 0, 5), (9, 0, 5), (5, 0, 9), (5, 60, 5), (9, 60, 5), (5, 60, 9)],
        faces=[(0, 2, 1), (3, 4, 5), (0, 1, 4), (0, 4, 3), (0, 3, 5), (0, 5, 2), (1, 2, 5), (1, 5, 4)],
    )
    wedge.fix_normals()
    return _union(_l_bracket(), wedge)


def _inside_out_l():
    mesh = _l_bracket()
    mesh.invert()
    return mesh


def _holed_plate():
    return trimesh.boolean.difference([_box((0, 0, 0), (40, 40, 5)), _post(20, 20, -1, 6, 5.0)], engine="manifold")


def _save(mesh, tmp_path, name):
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    return str(path)


def _call(name, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _corners(path):
    reply = _call("analyze_structural_risks", file_path=path)
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "sharp_corner"]


@pytest.mark.parametrize("shape", ["plate", "cylinder", "cone", "holed_plate"])
def test_outside_edges_are_never_a_sharp_corner(shape, tmp_path):
    mesh = {
        "plate": lambda: _box((0, 0, 0), (100, 100, 3)),
        "cylinder": lambda: trimesh.creation.cylinder(radius=10, height=20, sections=128),
        "cone": lambda: trimesh.creation.cone(radius=10, height=20, sections=64),
        "holed_plate": _holed_plate,
    }[shape]()
    assert _corners(_save(mesh, tmp_path, shape)) == []


@pytest.mark.parametrize("build", [_l_bracket, _inside_out_l], ids=["l_bracket", "inside_out"])
def test_the_inside_corner_of_an_l_bracket_is_found_and_its_outside_edges_are_not(build, tmp_path):
    (corner,) = _corners(_save(build(), tmp_path, "l"))
    x, y, z = corner["location_mm"]
    assert (x, z) == pytest.approx((5.0, 5.0), abs=0.01) and y == pytest.approx(30.0, abs=0.5)
    assert corner["region_size_mm"][1] == pytest.approx(60.0, abs=0.01)  # the whole corner, one finding
    assert corner["severity"] == "warning"


@pytest.mark.parametrize("sections", [12, 96])
def test_each_peg_root_is_one_warning_however_finely_it_is_faceted(sections, tmp_path):
    corners = _corners(_save(_pegboard(sections), tmp_path, "pegboard"))
    roots = sorted((round(c["location_mm"][0]), round(c["location_mm"][1]), round(c["location_mm"][2])) for c in corners)
    assert roots == sorted((x, y, 3) for x, y in PEGS)
    assert {c["severity"] for c in corners} == {"warning"}


def test_the_base_of_a_rib_and_the_floor_of_a_pocket_are_inside_corners(tmp_path):
    rib = _union(_box((0, 0, 0), (80, 40, 3)), _box((10, 18, 3), (70, 22, 15)))
    (base,) = _corners(_save(rib, tmp_path, "rib"))
    assert base["location_mm"][2] == pytest.approx(3.0) and base["region_size_mm"][:2] == pytest.approx([60.0, 4.0])

    pocket = trimesh.boolean.difference([_box((0, 0, 0), (40, 40, 10)), _box((10, 10, 4), (30, 30, 11))],
                                        engine="manifold")
    (floor,) = _corners(_save(pocket, tmp_path, "pocket"))
    assert floor["region_size_mm"] == pytest.approx([20.0, 20.0, 6.0])  # the floor's edges and the walls' corners


@pytest.mark.parametrize("build", [_filleted_l, _chamfered_l], ids=["fillet", "chamfer"])
def test_a_rounded_inside_corner_is_not_sharp(build, tmp_path):
    assert _corners(_save(build(), tmp_path, "rounded")) == []


def test_the_doors_name_the_inside_corners(tmp_path):
    path = _save(_pegboard(), tmp_path, "pegboard")
    summary = _call("design_improvement_plan", file_path=path)["summary"]
    assert "6 sharp inside corners" in summary and "clusters of sharp edges" not in summary, summary
    fillets = [r for r in _call("recommend_design_reinforcements", file_path=path)["reinforcements"]
               if r["addresses_risk"] == "sharp_corner"]
    assert len(fillets) == 6, fillets
    assert all(r["description"].startswith("Round the sharp inside corner near (") for r in fillets), fillets


def test_a_real_export_is_read_for_its_inside_corners_alone(tmp_path):
    donut = str(rx.stage("donut_mm.stl", tmp_path / "donut"))
    assert _corners(donut) == []  # a donut has no inside corner

    from kiln.step_import import _ocp_available

    if not _ocp_available():
        pytest.skip("the CAD kernel is not installed; the enclosure is a STEP file")
    enclosure = _corners(str(rx.stage("enclosure_v1.step", tmp_path / "enclosure")))
    assert enclosure and {c["severity"] for c in enclosure} == {"warning"}
    # The inside of the box (its floor and walls meeting) is one corner.
    assert enclosure[0]["region_size_mm"][:2] == pytest.approx([85.2, 59.2], abs=0.1)


@pytest.mark.parametrize("case", ["kilometre_edge", "nan_vertex", "inf_vertex"])
def test_a_hostile_file_is_read_with_bounded_work_and_never_raises(case):
    """Files people upload reach this check on Kiln's servers.  An edge
    kilometres long once made it lay a point every 1.25 mm along it (a 2 km
    edge cost 311 MB; the cost had no ceiling), and one face with no position
    turned every corner of the part inside out."""
    import time
    import tracemalloc

    from kiln.design_reasoning import _find_sharp_corners

    length = 2_000_000.0 if case == "kilometre_edge" else 60.0
    bracket = _union(_box((0, 0, 0), (40, length, 5)), _box((0, 0, 0), (5, length, 40)))
    triangles = [tuple(map(tuple, t)) for t in bracket.triangles.tolist()]
    if case != "kilometre_edge":
        nowhere = float("nan") if case == "nan_vertex" else float("inf")
        triangles.append(((0.0, 0.0, 0.0), (nowhere, 0.0, 0.0), (0.0, 1.0, 0.0)))

    tracemalloc.start()
    try:
        began = time.monotonic()
        (corner,) = _find_sharp_corners(triangles)
        took = time.monotonic() - began
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (corner.location_mm[0], corner.location_mm[2]) == pytest.approx((5.0, 5.0))
    assert peak < 20e6, f"{peak / 1e6:.0f} MB for one L-bracket"
    assert took < 10.0
