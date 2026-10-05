"""Rounding and bevelling a part's edges, on its CAD file -- measured against closed forms.

Until 2026-10-04 ``add_mesh_fillet`` and ``add_mesh_chamfer`` never succeeded:
the mesh engines behind them opened the surface of every part tried and made
it bigger, so the door refused every call.  They are rebuilt on the CAD kernel,
and these tests hold the result to numbers geometry gives: a block with all
twelve edges rounded has a volume in closed form, and so do a bevelled block,
a rounded inside corner and the round at the foot of a post.

Three layers, each tested where it lives:

* the kernel child (:mod:`kiln.cad_edge`) -- volumes, holes, what it drops;
* the planner (:mod:`kiln.edge_plan`) -- pure arithmetic on a survey, so most
  of its tests need no kernel at all;
* the door and the registered tools (:mod:`kiln.edge_finish`) -- routes,
  refusals, the CAD handed on to the next edit.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

from kiln.edge_plan import CHAMFER, FILLET, PrintFrame, hanging_band_mm, parse_selector, place_of, plan_edges
from kiln.mesh_edit_check import EDIT_REFUSED, TOO_LARGE_TO_CHECK, measure_mesh

trimesh = pytest.importorskip("trimesh")


def _have_ocp() -> bool:
    try:
        import OCP.BRepFilletAPI  # noqa: F401

        return True
    except ImportError:
        return False


needs_cad_kernel = pytest.mark.skipif(not _have_ocp(), reason="the OpenCascade kernel (OCP) is not installed")

FRAME = PrintFrame(nozzle_mm=0.4, layer_mm=0.2, overhang_deg=45.0)


# ---------------------------------------------------------------------------
# Parts, built on the kernel
# ---------------------------------------------------------------------------


def _write_step(shape, path: Path) -> str:
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


def _box(x, y, z, dx, dy, dz):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _cyl(x, y, z, r, h, axis=(0, 0, 1)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*axis)), r, h).Shape()


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def _fuse(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    op = BRepAlgoAPI_Fuse(a, b)
    op.Build()
    clean = ShapeUpgrade_UnifySameDomain(op.Shape(), True, True, True)
    clean.Build()
    return clean.Shape()


def block(path: Path) -> str:
    """30 x 20 x 10 mm."""
    return _write_step(_box(0, 0, 0, 30, 20, 10), path)


def block_with_hole(path: Path) -> str:
    """30 x 20 x 10 mm, a 3 mm through-hole down its middle."""
    return _write_step(_cut(_box(0, 0, 0, 30, 20, 10), _cyl(15, 10, -2, 1.5, 14)), path)


def bracket(path: Path) -> str:
    """An L: a 40 x 30 x 4 base and a 4 x 30 x 30 upright, one inside corner 30 mm long."""
    return _write_step(_fuse(_box(0, 0, 0, 40, 30, 4), _box(0, 0, 0, 4, 30, 30)), path)


def post_on_plate(path: Path) -> str:
    """A 30 x 30 x 3 plate with a round post, 8 mm across, standing 13 mm above it."""
    return _write_step(_fuse(_box(0, 0, 0, 30, 30, 3), _cyl(15, 15, 1, 4, 15)), path)


def post_with_blind_hole(path: Path) -> str:
    """The post, with a 2.2 mm hole 8 mm down its top."""
    solid = _fuse(_box(0, 0, 0, 30, 30, 3), _cyl(15, 15, 1, 4, 15))
    return _write_step(_cut(solid, _cyl(15, 15, 8, 1.1, 10)), path)


def open_box(path: Path) -> str:
    """A 40 x 30 x 15 open-topped box with 1.6 mm walls and a 2 mm floor."""
    return _write_step(_cut(_box(0, 0, 0, 40, 30, 15), _box(1.6, 1.6, 2, 36.8, 26.8, 20)), path)


def rounded_block_volume(a: float, b: float, c: float, r: float) -> float:
    """A block with all twelve edges rounded at *r*: a smaller block grown by a ball."""
    x, y, z = a - 2 * r, b - 2 * r, c - 2 * r
    return x * y * z + 2 * r * (x * y + y * z + x * z) + math.pi * r * r * (x + y + z) + 4 / 3 * math.pi * r**3


def bevelled_block_volume(a: float, b: float, c: float, d: float) -> float:
    """A block with all twelve edges bevelled at *d*.

    Each edge loses a prism of d*d/2 along its length.  At a corner three
    prisms overlap (each pair shares d^3/3, all three d^3/4), and the kernel
    cuts the point left between the three bevels off flat (d^3/12): 2/3 d^3
    back per corner.
    """
    return a * b * c - 2 * d * d * (a + b + c) + 8 * (2 / 3) * d**3


def _plan(survey, kind=FILLET, size=1.0, **kw):
    return plan_edges(survey, kind=kind, size_mm=size, frame=kw.pop("frame", FRAME), **kw)


# ---------------------------------------------------------------------------
# The kernel: closed-form volumes
# ---------------------------------------------------------------------------


@needs_cad_kernel
@pytest.mark.parametrize("radius", [1.0, 2.0])
def test_a_block_rounded_on_every_edge_has_the_closed_form_volume(tmp_path, radius):
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")
    survey = survey_step(step)
    plan = _plan(survey, FILLET, radius)
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    assert len(done["applied"]) == 12 and not done["dropped"]
    assert done["volume_before_mm3"] == pytest.approx(6000.0, abs=1e-3)
    assert done["volume_after_mm3"] == pytest.approx(rounded_block_volume(30, 20, 10, radius), abs=1e-3)


@needs_cad_kernel
def test_the_two_millimetre_round_is_the_number_the_spec_states(tmp_path):
    """5,804.6961 mm3: the figure the rebuild was specified against."""
    assert rounded_block_volume(30, 20, 10, 2.0) == pytest.approx(5804.6961, abs=1e-4)


@needs_cad_kernel
def test_a_block_bevelled_on_every_edge_has_the_closed_form_volume(tmp_path):
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")
    plan = _plan(survey_step(step), CHAMFER, 2.0)
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    assert len(done["applied"]) == 12
    assert done["volume_after_mm3"] == pytest.approx(bevelled_block_volume(30, 20, 10, 2.0), abs=1e-3)
    assert done["volume_after_mm3"] == pytest.approx(5562.6667, abs=1e-3)


@needs_cad_kernel
def test_a_hole_is_untouched_by_rounding_the_part_around_it(tmp_path):
    """The rounded block's volume less the hole's, and the hole still 3 mm on the CAD."""
    from kiln.cad_edge import finish_step, survey_step

    step = block_with_hole(tmp_path / "block.step")
    survey = survey_step(step)
    assert survey["holes_mm"] == [3.0]
    plan = _plan(survey, FILLET, 2.0)
    rims = [s for s in plan.left_sharp if "rims a 3 mm hole" in s["reason"]]
    assert len(rims) == 2
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    hole = math.pi * 1.5**2 * 10
    assert done["volume_after_mm3"] == pytest.approx(rounded_block_volume(30, 20, 10, 2.0) - hole, abs=1e-3)
    assert done["holes_before_mm"] == done["holes_after_mm"] == [3.0]


@needs_cad_kernel
def test_an_inside_corner_gains_the_closed_form_volume(tmp_path):
    """A round in a square inside corner adds (1 - pi/4) r^2 per mm of corner."""
    from kiln.cad_edge import finish_step, survey_step

    step = bracket(tmp_path / "bracket.step")
    survey = survey_step(step)
    inside = [e for e in survey["edges"] if e["corner"] == "inside"]
    assert len(inside) == 1 and inside[0]["length_mm"] == pytest.approx(30.0)
    plan = _plan(survey, FILLET, 1.5, edges="inside")
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    gained = done["volume_after_mm3"] - done["volume_before_mm3"]
    assert gained == pytest.approx((1 - math.pi / 4) * 1.5**2 * 30.0, abs=1e-3)


@needs_cad_kernel
def test_the_round_at_the_foot_of_a_post_has_the_closed_form_volume(tmp_path):
    """Pappus: the corner's cross-section, (1 - pi/4) r^2, swept around the
    post at the radius of its centroid, R + r (10 - 3 pi) / (3 (4 - pi))."""
    from kiln.cad_edge import finish_step, survey_step

    step = post_on_plate(tmp_path / "post.step")
    survey = survey_step(step)
    foot = [e for e in survey["edges"] if e["corner"] == "inside"]
    assert len(foot) == 1 and foot[0]["post_mm"] == 8.0 and foot[0]["curve"] == "circle"
    r, post_radius = 1.0, 4.0
    plan = _plan(survey, FILLET, r, edges="inside")
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    area = (1 - math.pi / 4) * r * r
    centroid = post_radius + r * (10 - 3 * math.pi) / (3 * (4 - math.pi))
    assert done["volume_after_mm3"] - done["volume_before_mm3"] == pytest.approx(2 * math.pi * centroid * area, abs=1e-3)


# ---------------------------------------------------------------------------
# The kernel: what it reads, what it drops, what it keeps
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_the_survey_says_where_every_edge_of_a_block_sits(tmp_path):
    from kiln.cad_edge import survey_step

    survey = survey_step(block(tmp_path / "block.step"))
    assert survey["box"] == [0.0, 0.0, 0.0, 30.0, 20.0, 10.0]
    places = sorted(place_of(e, 0.0) for e in survey["edges"])
    assert places == ["bottom"] * 4 + ["top"] * 4 + ["vertical"] * 4
    assert {e["corner"] for e in survey["edges"]} == {"outside"}
    assert {e["turn_deg"] for e in survey["edges"]} == {90.0}
    # Each edge of a block has the two faces beside it as room: a 10 mm
    # vertical edge sits between a 30 mm and a 20 mm face.
    upright = next(e for e in survey["edges"] if place_of(e, 0.0) == "vertical")
    assert sorted(upright["room_mm"]) == [20.0, 30.0]


@needs_cad_kernel
def test_an_edge_the_kernel_cannot_build_is_dropped_and_named_and_the_rest_are_done(tmp_path):
    """A 6 mm round on the 10 mm edges of a 30 x 20 x 10 block cannot exist
    beside a 6 mm round on its other edges; the plan would never ask for it,
    so the kernel is handed it directly."""
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")
    survey = survey_step(step)
    by_place = {}
    for e in survey["edges"]:
        by_place.setdefault(place_of(e, 0.0), []).append(e)
    fine = [{"edges": [e["id"]], "mids": [e["mid"]], "kind": FILLET, "size_mm": 1.0} for e in by_place["vertical"]]
    # 12 mm on a top edge of a 10 mm tall block: there is no such round.
    bad = by_place["top"][0]
    impossible = {"edges": [bad["id"]], "mids": [bad["mid"]], "kind": FILLET, "size_mm": 12.0}
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=[*fine, impossible])
    assert [d["edges"] for d in done["dropped"]] == [[bad["id"]]]
    assert done["dropped"][0]["reason"]
    assert len(done["applied"]) == 4
    quarter = (1 - math.pi / 4) * 1.0 * 10.0
    assert done["volume_before_mm3"] - done["volume_after_mm3"] == pytest.approx(4 * quarter, abs=1e-3)


@needs_cad_kernel
def test_a_plan_made_for_another_file_is_refused(tmp_path):
    from kiln.cad_edge import CadEdgeError, finish_step, survey_step

    plan = _plan(survey_step(block(tmp_path / "block.step")))
    other = bracket(tmp_path / "bracket.step")
    with pytest.raises(CadEdgeError, match="plan the edges again"):
        finish_step(other, str(tmp_path / "out.stl"), treatments=plan.treatments)


@needs_cad_kernel
def test_a_bevel_on_the_bed_follows_the_rounded_corners_above_it(tmp_path):
    """Rounds on the upright edges, a bevel around the bottom: one build, all
    eight chains, and the bevel smaller than the corners it runs through."""
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")

    def bevel_the_bed(chains, _frame):
        return {c["chain"]: (CHAMFER, 1.5, "on the bed") for c in chains if c["place"] == "bottom"}

    plan = _plan(survey_step(step), FILLET, 1.5, edges="vertical,bottom", choose=bevel_the_bed)
    bevels = [t for t in plan.treatments if t["kind"] == CHAMFER]
    assert len(bevels) == 4
    # 1.5 mm corners, less the 0.2 mm a 0.4 mm nozzle turns in.
    assert {t["size_mm"] for t in bevels} == {1.3}
    assert "follows corners rounded at 1.5 mm" in bevels[0]["note"]
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan.treatments)
    assert len(done["applied"]) == 8 and not done["dropped"]


@needs_cad_kernel
def test_a_hole_rim_is_bevelled_after_the_rim_around_it_was_rounded(tmp_path):
    """Rounding the post's outer rim rebuilds its top face, and the kernel's
    record then calls the hole's untouched rim deleted (2026-10-04: every
    lead-in beside a rounded rim was dropped).  The rim is found by where it
    is."""
    from kiln.cad_edge import finish_step, survey_step

    step = post_with_blind_hole(tmp_path / "post.step")
    survey = survey_step(step)
    rim = next(e for e in survey["edges"] if e.get("hole_mm") == 2.2 and e["hole_edge"] == "mouth")
    outer = next(e for e in survey["edges"] if e.get("post_mm") == 8.0 and e["corner"] == "outside")
    plan = [
        {"edges": [outer["id"]], "mids": [outer["mid"]], "kind": FILLET, "size_mm": 1.0},
        {"edges": [rim["id"]], "mids": [rim["mid"]], "kind": CHAMFER, "size_mm": 0.2},
    ]
    done = finish_step(step, str(tmp_path / "out.stl"), treatments=plan)
    assert len(done["applied"]) == 2 and not done["dropped"]
    assert done["holes_after_mm"] == [2.2]


# ---------------------------------------------------------------------------
# The mesh the kernel writes is closed
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_the_rounded_parts_mesh_is_a_closed_surface_of_the_same_volume(tmp_path):
    """The kernel's mesher leaves a facet with no area at the pole of every
    ball-rounded corner -- eight on this block -- and a mesh carrying them
    reads as an open surface with no volume."""
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")
    out = tmp_path / "out.stl"
    finish_step(step, str(out), treatments=_plan(survey_step(step), FILLET, 2.0).treatments)
    measured = measure_mesh(str(out))
    assert measured.watertight is True
    assert measured.volume_mm3 == pytest.approx(rounded_block_volume(30, 20, 10, 2.0), rel=1e-3)
    assert measured.extents_mm == pytest.approx((30.0, 20.0, 10.0), abs=1e-6)


@needs_cad_kernel
def test_a_cad_part_with_rounded_corners_converts_to_a_closed_mesh_on_the_kernel_alone(tmp_path, monkeypatch):
    """Kiln's own STEP conversion, with the kernel as its only backend (what
    a server without FreeCAD runs): a part with ball-rounded corners read as
    "not closed" and of no volume until 2026-10-04.  FreeCAD and gmsh are
    hidden so a developer's machine takes the same path."""
    import kiln.step_import as si
    from kiln.cad_edge import finish_step, survey_step

    step = block(tmp_path / "block.step")
    rounded = tmp_path / "rounded.step"
    finish_step(
        step, str(tmp_path / "scratch.stl"), treatments=_plan(survey_step(step), FILLET, 2.0).treatments,
        output_step=str(rounded),
    )
    monkeypatch.setattr(si, "_find_freecad_cmd", lambda: None)
    monkeypatch.setattr(si, "_find_gmsh_cmd", lambda: None)
    mesh_path, _note, record = si.ensure_mesh_path(str(rounded), output_dir=str(tmp_path / "converted"), with_record=True)
    assert record.backend == "occt"
    measured = measure_mesh(mesh_path)
    assert measured.watertight is True
    assert measured.volume_mm3 == pytest.approx(rounded_block_volume(30, 20, 10, 2.0), rel=1e-3)


def _facet(a, b, c) -> bytes:
    import struct

    return struct.pack("<12fH", 0.0, 0.0, 0.0, *a, *b, *c, 0)


def _stl(path: Path, facets: list[bytes]) -> str:
    path.write_bytes(b"test".ljust(80, b"\x00") + len(facets).to_bytes(4, "little") + b"".join(facets))
    return str(path)


def test_collapsed_facets_are_dropped_and_real_ones_kept(tmp_path):
    from kiln.cad_kernel import drop_collapsed_facets

    o, x, y, z = (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)
    tetrahedron = [_facet(o, y, x), _facet(o, x, z), _facet(o, z, y), _facet(x, y, z)]
    path = _stl(tmp_path / "t.stl", [*tetrahedron[:2], _facet(x, x, z), *tetrahedron[2:], _facet(o, y, y)])
    assert trimesh.load(path, force="mesh").is_watertight is False
    assert drop_collapsed_facets(path) == 2
    cleaned = trimesh.load(path, force="mesh")
    assert len(cleaned.faces) == 4 and cleaned.is_watertight is True
    assert Path(path).read_bytes()[:4] == b"test"  # the header's own text survives
    assert drop_collapsed_facets(path) == 0


def test_a_file_that_is_not_a_binary_stl_is_left_as_it_is(tmp_path):
    from kiln.cad_kernel import drop_collapsed_facets

    text = tmp_path / "ascii.stl"
    text.write_text("solid x\nendsolid x\n" * 20)
    before = text.read_bytes()
    assert drop_collapsed_facets(str(text)) == 0
    assert text.read_bytes() == before
    assert drop_collapsed_facets(str(tmp_path / "missing.stl")) == 0


# ---------------------------------------------------------------------------
# The planner: pure arithmetic on a survey
# ---------------------------------------------------------------------------


def _edge(edge_id, *, z=(5.0, 5.0), tangent=(1.0, 0.0, 0.0), normals=((0, 0, 1), (0, -1, 0)), corner="outside",
          room=(20.0, 20.0), across=(None, None), ends=None, turn=90.0, curve="line", chain=None, **extra):
    pair = [list(normals[0]), list(normals[1])]
    return {
        "id": edge_id, "chain": chain or edge_id, "curve": curve, "length_mm": 10.0, "mid": [0.0, 0.0, z[0]],
        "tangent": list(tangent), "z": list(z), "turn_deg": turn, "corner": corner, "faces": [1, 2],
        "normals": [pair, pair, pair], "room_mm": list(room), "across": list(across),
        "ends": ends or [edge_id * 10, edge_id * 10 + 1], **extra,
    }


def _survey(*edges, bed_z=0.0):
    return {"box": [0.0, 0.0, bed_z, 50.0, 50.0, 20.0], "volume_mm3": 1.0, "holes_mm": [], "edges": list(edges)}


TOP = {"z": (20.0, 20.0), "normals": ((0, 0, 1), (0, -1, 0))}
BED = {"z": (0.0, 0.0), "normals": ((0, 0, -1), (0, -1, 0))}
UPRIGHT = {"z": (0.0, 20.0), "tangent": (0.0, 0.0, 1.0), "normals": ((1, 0, 0), (0, -1, 0))}
UNDER = {"z": (12.0, 12.0), "normals": ((0, 0, -1), (0, -1, 0))}
FLOOR_CORNER = {"z": (2.0, 2.0), "normals": ((0, 0, 1), (0, 1, 0)), "corner": "inside"}


@pytest.mark.parametrize(
    ("shape", "place"),
    [(TOP, "top"), (BED, "bottom"), (UPRIGHT, "vertical"), (UNDER, "under"), (FLOOR_CORNER, "top"),
     ({"z": (3.0, 9.0), "tangent": (0.7, 0.0, 0.7)}, "sloped")],
)
def test_where_an_edge_sits_when_the_part_prints(shape, place):
    assert place_of(_edge(1, **shape), 0.0) == place


def test_a_selection_reads_words_and_edge_ids():
    assert parse_selector(None) == (set(), set())
    assert parse_selector("all") == (set(), set())
    assert parse_selector("top, outside") == ({"top", "outside"}, set())
    assert parse_selector("e12,E7") == (set(), {7, 12})
    assert parse_selector(["bottom", "e3"]) == ({"bottom"}, {3})
    with pytest.raises(ValueError, match="'sideways' is not an edge selection"):
        parse_selector("top,sideways")


def test_words_of_one_kind_widen_and_words_of_two_kinds_narrow():
    survey = _survey(_edge(1, **TOP), _edge(2, **BED), _edge(3, **UPRIGHT), _edge(4, **FLOOR_CORNER))

    def chosen(edges):
        return sorted(i for t in _plan(survey, edges=edges).treatments for i in t["edges"])

    assert chosen(None) == [1, 2, 3, 4]
    assert chosen("top") == [1, 4]
    assert chosen("top,bottom") == [1, 2, 4]
    assert chosen("top,outside") == [1]
    assert chosen("inside") == [4]
    assert chosen("e3") == [3]
    assert chosen("e3,bottom") == [2, 3]
    assert _plan(survey, edges="top").not_selected == 2


def test_an_edge_that_turns_less_than_the_threshold_is_not_sharp():
    survey = _survey(_edge(1, **TOP), _edge(2, **{**TOP, "turn": 45.0}))
    assert [t["edges"] for t in _plan(survey, min_turn_deg=60.0).treatments] == [[1]]
    assert len(_plan(survey, min_turn_deg=30.0).treatments) == 2


def test_an_edge_no_file_has_and_a_size_of_nothing_are_errors():
    survey = _survey(_edge(1, **TOP))
    with pytest.raises(ValueError, match="no edge e9"):
        _plan(survey, edges="e9")
    with pytest.raises(ValueError, match="above 0 mm"):
        _plan(survey, size=0.0)


def test_a_named_edge_the_surface_runs_smoothly_across_says_so():
    plan = _plan(_survey(_edge(1, **{**TOP, "corner": "smooth"})), edges="e1")
    assert not plan.treatments
    assert "no corner here to finish" in plan.left_sharp[0]["reason"]


@pytest.mark.parametrize(("nozzle", "expected"), [(0.4, 0.6), (0.6, 0.5), (0.8, 0.4)])
def test_two_rounds_share_a_narrow_face_and_leave_one_nozzle_width_of_flat(nozzle, expected):
    """The top of a 1.6 mm wall: two edges, 1.6 mm of room between them.  What
    each may take is set by the nozzle, so it changes when the nozzle does."""
    wall_top = _survey(
        _edge(1, **{**TOP, "room": (1.6, 20.0), "across": (2, None)}),
        _edge(2, **{**TOP, "room": (1.6, 20.0), "across": (1, None)}),
    )
    plan = _plan(wall_top, size=1.0, frame=PrintFrame(nozzle, 0.2, 45.0))
    assert [t["size_mm"] for t in plan.treatments] == [pytest.approx(expected)] * 2
    assert f"keeps one {nozzle:g} mm nozzle width of flat" in plan.treatments[0]["note"]
    assert sum(t["size_mm"] for t in plan.treatments) == pytest.approx(1.6 - nozzle)


def test_a_round_beside_an_unfinished_edge_has_the_whole_face_less_the_flat():
    plan = _plan(_survey(_edge(1, **{**TOP, "room": (1.6, 20.0), "across": (2, None)}), _edge(2, **BED)), edges="e1")
    assert plan.treatments[0]["size_mm"] == pytest.approx(1.0)  # 1.2 mm of room, 1.0 asked
    plan = _plan(_survey(_edge(1, **{**TOP, "room": (1.6, 20.0), "across": (2, None)}), _edge(2, **BED)), edges="e1", size=3.0)
    assert plan.treatments[0]["size_mm"] == pytest.approx(1.2)


def test_what_one_edge_does_not_use_of_a_shared_face_the_other_may():
    """Edge 2's own other face holds it to 0.3 mm; edge 1 gets the rest of
    the 2.0 mm they share, and together they never pass it."""
    survey = _survey(
        _edge(1, **{**TOP, "room": (2.0, 20.0), "across": (2, None)}),
        _edge(2, **{**TOP, "room": (2.0, 0.7), "across": (1, None)}),
    )
    sizes = {t["edges"][0]: t["size_mm"] for t in _plan(survey, size=1.5).treatments}
    assert sizes[2] == pytest.approx(0.3)
    assert sizes[1] == pytest.approx(1.3)
    assert sizes[1] + sizes[2] <= 2.0 - FRAME.nozzle_mm + 1e-9


def test_an_edge_with_no_room_for_a_finish_that_shows_stays_sharp_and_says_why():
    """The top of a 0.5 mm wall: 0.05 mm each once a nozzle width is kept."""
    fin = _survey(
        _edge(1, **{**TOP, "room": (0.5, 20.0), "across": (2, None)}),
        _edge(2, **{**TOP, "room": (0.5, 20.0), "across": (1, None)}),
    )
    plan = _plan(fin)
    assert not plan.treatments
    assert len(plan.left_sharp) == 2
    assert "0.5 mm wide" in plan.left_sharp[0]["reason"] and "does not show" in plan.left_sharp[0]["reason"]


def test_a_round_reaches_less_far_across_a_shallow_corner():
    """A round touches each face r x tan(turn / 2) from the edge: half as far
    on a 53 degree corner as on a square one, so the same face fits twice the radius."""
    shallow = 2 * math.degrees(math.atan(0.5))
    square = _plan(_survey(_edge(1, **{**TOP, "room": (1.0, 20.0)})), size=5.0)
    gentle = _plan(_survey(_edge(1, **{**TOP, "room": (1.0, 20.0), "turn": shallow})), size=5.0, min_turn_deg=30.0)
    assert square.treatments[0]["size_mm"] == pytest.approx(0.6)
    assert gentle.treatments[0]["size_mm"] == pytest.approx(1.2)


@pytest.mark.parametrize(("limit_deg", "radius"), [(45.0, 1.0), (60.0, 1.5), (30.0, 0.8)])
def test_a_round_on_the_bed_hangs_by_the_closed_form(limit_deg, radius):
    """r x (1 - sin(limit)): the part of the curve between level and the steepest slope that prints."""
    frame = PrintFrame(0.4, 0.2, limit_deg)
    band = hanging_band_mm(FILLET, radius, _edge(1, **BED), frame)
    assert band == pytest.approx(radius * (1 - math.sin(math.radians(limit_deg))), abs=1e-9)


def test_a_bevel_on_the_bed_hangs_only_past_the_limit_and_then_all_of_it_does():
    assert hanging_band_mm(CHAMFER, 1.0, _edge(1, **BED), PrintFrame(0.4, 0.2, 45.0)) == 0.0
    assert hanging_band_mm(CHAMFER, 1.0, _edge(1, **BED), PrintFrame(0.4, 0.2, 40.0)) == pytest.approx(1.0)


@pytest.mark.parametrize("shape", [TOP, UPRIGHT, FLOOR_CORNER])
def test_nothing_hangs_on_an_edge_that_faces_up_or_sideways(shape):
    assert hanging_band_mm(FILLET, 2.0, _edge(1, **shape), FRAME) == 0.0
    assert hanging_band_mm(CHAMFER, 2.0, _edge(1, **shape), FRAME) == 0.0


def test_a_round_on_the_bed_is_made_and_said_to_droop():
    plan = _plan(_survey(_edge(1, **BED)), size=1.0)
    assert plan.treatments[0]["kind"] == FILLET and plan.treatments[0]["size_mm"] == 1.0
    caution = plan.cautions[0]
    assert caution["kind"] == "on_the_bed" and caution["edges"] == [1]
    assert "0.29 mm" in caution["message"] and "45 degrees" in caution["message"]


def test_a_round_facing_down_off_the_bed_is_said_to_need_support():
    plan = _plan(_survey(_edge(1, **UNDER)), size=1.0)
    assert [c["kind"] for c in plan.cautions] == ["overhang"]


@pytest.mark.parametrize(("layer", "cautioned"), [(0.2, True), (0.3, False)])
def test_a_hanging_band_thinner_than_a_layer_is_not_a_caution(layer, cautioned):
    """A 1 mm round on the bed hangs 0.29 mm: more than a 0.2 mm layer, less than a 0.3 mm one."""
    plan = _plan(_survey(_edge(1, **BED)), size=1.0, frame=PrintFrame(0.4, layer, 45.0))
    assert bool(plan.cautions) is cautioned


def test_a_size_below_what_the_printer_shows_is_made_and_said_not_to_show():
    plan = _plan(_survey(_edge(1, **TOP), _edge(2, **UPRIGHT)), size=0.15)
    assert [t["size_mm"] for t in plan.treatments] == [0.15, 0.15]
    assert sorted(c["edges"][0] for c in plan.cautions if c["kind"] == "too_small_to_show") == [1, 2]
    # Half a nozzle on an upright edge, one layer on a level one.
    assert FRAME.smallest_shown_mm("vertical") == 0.2 and FRAME.smallest_shown_mm("top") == 0.2
    wide = PrintFrame(0.8, 0.3, 45.0)
    assert wide.smallest_shown_mm("vertical") == 0.4 and wide.smallest_shown_mm("top") == 0.3


def test_a_hole_rim_stays_sharp_unless_it_is_selected_or_named():
    survey = _survey(_edge(1, **TOP), _edge(2, **{**TOP, "curve": "circle", "hole_mm": 3.0}))
    plain = _plan(survey)
    assert [t["edges"] for t in plain.treatments] == [[1]]
    assert plain.left_sharp[0]["edges"] == [2] and "rims a 3 mm hole" in plain.left_sharp[0]["reason"]
    assert [t["edges"] for t in _plan(survey, edges="holes").treatments] == [[2]]
    assert [t["edges"] for t in _plan(survey, edges="e2").treatments] == [[2]]
    assert sorted(t["edges"][0] for t in _plan(survey, edges="top,holes").treatments) == [1, 2]


def test_the_floor_of_a_blind_hole_is_not_its_rim():
    """2026-10-04: both ends of a blind hole were "its rim", so selecting
    holes -- or a lead-in for a named screw -- bevelled the bottom of the
    hole as well as its mouth."""
    mouth = _edge(1, **{**TOP, "curve": "circle", "hole_mm": 2.2, "hole_edge": "mouth"})
    floor = _edge(2, **{**FLOOR_CORNER, "curve": "circle", "hole_mm": 2.2, "hole_edge": "floor"})
    survey = _survey(mouth, floor)
    assert [t["edges"] for t in _plan(survey, edges="holes").treatments] == [[1]]
    everything = _plan(survey)
    assert not everything.treatments
    assert sorted(s["reason"].split(";")[0] for s in everything.left_sharp) == [
        "it is the floor of a 2.2 mm hole", "it rims a 2.2 mm hole",
    ]
    assert [t["edges"] for t in _plan(survey, edges="e2").treatments] == [[2]]
    offered = {}
    _plan(survey, choose=lambda chains, frame: offered.update({c["chain"]: c["held"] for c in chains}))
    assert offered == {1: "hole_rim", 2: "hole_floor"}


def test_edges_that_run_on_into_each_other_are_one_choice():
    """A chain is taken whole or left whole: one member the selection misses leaves it."""
    survey = _survey(_edge(1, chain=1, **TOP), _edge(2, chain=1, **UPRIGHT), _edge(3, **TOP))
    assert [t["edges"] for t in _plan(survey).treatments] == [[1, 2], [3]]
    assert [t["edges"] for t in _plan(survey, edges="top").treatments] == [[3]]
    assert [t["edges"] for t in _plan(survey, edges="e2").treatments] == [[1, 2]]


def test_a_chooser_may_change_a_finish_leave_an_edge_or_finish_a_held_rim():
    survey = _survey(
        _edge(1, **BED), _edge(2, **TOP), _edge(3, **UNDER),
        _edge(4, **{**TOP, "curve": "circle", "hole_mm": 2.5}),
    )
    seen = {}

    def choose(chains, frame):
        seen.update({c["chain"]: c for c in chains})
        assert frame is FRAME
        return {1: (CHAMFER, 0.8, "bevelled"), 3: (None, 0.0, "left: it would hang"), 4: (CHAMFER, 0.4, "lead-in")}

    plan = _plan(survey, size=1.0, choose=choose)
    by_edge = {t["edges"][0]: t for t in plan.treatments}
    assert (by_edge[1]["kind"], by_edge[1]["size_mm"], by_edge[1]["note"]) == (CHAMFER, 0.8, "bevelled")
    assert (by_edge[2]["kind"], by_edge[2]["size_mm"]) == (FILLET, 1.0)
    assert (by_edge[4]["kind"], by_edge[4]["size_mm"]) == (CHAMFER, 0.4)
    assert [(s["edges"], s["reason"]) for s in plan.left_sharp] == [([3], "left: it would hang")]
    # What it was handed: the facts a choice turns on, and the rim marked as held.
    assert seen[1]["place"] == "bottom" and seen[1]["hangs_mm"][FILLET] == pytest.approx(1 - math.sin(math.pi / 4))
    assert seen[1]["hangs_mm"][CHAMFER] == 0.0
    assert seen[4]["held"] == "hole_rim" and seen[4]["hole_mm"] == 2.5
    assert "held" not in seen[2]


def test_a_held_rim_the_chooser_says_nothing_about_stays_sharp():
    survey = _survey(_edge(1, **{**TOP, "curve": "circle", "hole_mm": 2.5}))
    plan = _plan(survey, choose=lambda chains, frame: {})
    assert not plan.treatments and "rims a 2.5 mm hole" in plan.left_sharp[0]["reason"]


def test_a_bevel_stays_inside_the_rounded_corners_it_meets():
    """Edges 1 (a bevel) and 2 (a round) share corner 7."""
    survey = _survey(_edge(1, ends=[7, 8], **BED), _edge(2, ends=[7, 9], **UPRIGHT))

    def bevel_one(chains, _frame):
        return {1: (CHAMFER, 2.0, "")}

    plan = _plan(survey, size=1.5, choose=bevel_one)
    bevel = next(t for t in plan.treatments if t["kind"] == CHAMFER)
    assert bevel["size_mm"] == pytest.approx(1.5 - FRAME.nozzle_mm / 2)
    # No shared corner, no limit.
    apart = _survey(_edge(1, ends=[7, 8], **BED), _edge(2, ends=[5, 9], **UPRIGHT))
    assert next(t for t in _plan(apart, size=1.5, choose=bevel_one).treatments if t["kind"] == CHAMFER)["size_mm"] == 2.0


def test_the_plan_reads_back_in_the_words_a_reply_carries():
    said = _plan(_survey(_edge(1, **BED), _edge(2, **{**TOP, "curve": "circle", "hole_mm": 3.0}))).to_dict()
    assert said["finished"] == [
        {"edges": ["e1"], "finish": "fillet", "size_mm": 1.0, "asked_mm": 1.0, "place": "bottom", "corner": "outside"},
    ]
    assert said["left_sharp"][0]["edges"] == ["e2"]
    assert said["cautions"][0]["edges"] == ["e1"] and said["not_selected"] == 0


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_a_step_part_comes_back_rounded_as_a_mesh_and_as_cad(tmp_path):
    from kiln.cad_kernel import step_made_by_kiln
    from kiln.edge_finish import fillet_part

    step = block(tmp_path / "block.step")
    reply = fillet_part(step, radius_mm=2.0, nozzle_mm=0.4, layer_height_mm=0.2)
    assert reply["success"] is True, reply
    assert reply["method"] == "cad" and reply["cad_source"] == step
    assert reply["path"] == str(tmp_path / "block_filleted.stl")
    assert reply["step_path"] == str(tmp_path / "block_filleted.step") and step_made_by_kiln(reply["step_path"])
    assert reply["volume_mm3"] == {"before": 6000.0, "after": pytest.approx(rounded_block_volume(30, 20, 10, 2.0), abs=1e-3)}
    measured = reply["measured"]
    assert measured["ok"] and measured["after"]["closed_surface"] is True
    assert measured["after"]["size_mm"] == [30.0, 20.0, 10.0]
    assert len(reply["edges"]["finished"]) == 12
    assert [c["kind"] for c in reply["edges"]["cautions"]] == ["on_the_bed"] * 4
    assert reply["nozzle"]["diameter_mm"] == 0.4 and reply["nozzle"]["source"] == "stated"
    assert reply["sized_for"]["layer_height_mm"] == 0.2 and reply["sized_for"]["overhang_limit_deg"] == 45.0
    assert "12 edge chains rounded" in reply["note"] and "block_filleted.step" in reply["note"]


@needs_cad_kernel
def test_the_next_edit_of_the_finished_mesh_starts_from_its_cad(tmp_path):
    """The mesh and the CAD beside it are a pair: a bevel asked of the mesh
    is made on the CAD the round produced."""
    from kiln.edge_finish import chamfer_part, fillet_part

    first = fillet_part(block(tmp_path / "block.step"), radius_mm=1.0, edges="vertical")
    assert first["success"] is True, first
    second = chamfer_part(first["path"], distance_mm=0.5, edges="top")
    assert second["success"] is True, second
    assert second["method"] == "cad" and second["cad_source"] == first["step_path"]
    quarter = (1 - math.pi / 4) * 1.0 * 10.0
    assert first["volume_mm3"]["after"] == pytest.approx(6000.0 - 4 * quarter, abs=1e-3)
    assert second["volume_mm3"]["before"] == pytest.approx(first["volume_mm3"]["after"], abs=1e-3)
    assert second["volume_mm3"]["after"] < second["volume_mm3"]["before"]


@needs_cad_kernel
def test_the_callers_own_cad_is_never_written_over(tmp_path):
    """An output named after the input would put the finished CAD on top of it."""
    from kiln.edge_finish import fillet_part

    step = block(tmp_path / "block.step")
    original = Path(step).read_bytes()
    reply = fillet_part(step, radius_mm=1.0, output_path=str(tmp_path / "block.stl"))
    assert reply["success"] is True, reply
    assert Path(step).read_bytes() == original
    assert reply["step_path"] == str(tmp_path / "block.kiln.step")
    assert "block.step was already there" in reply["note"]


@needs_cad_kernel
def test_plan_only_builds_nothing_and_lists_every_edge(tmp_path):
    from kiln.edge_finish import fillet_part

    reply = fillet_part(block_with_hole(tmp_path / "block.step"), plan_only=True, edges="top")
    assert reply["success"] is True and reply["plan_only"] is True
    assert not (tmp_path / "block_filleted.stl").exists()
    assert len(reply["edges"]["finished"]) == 4
    listed = reply["all_edges"]
    assert len(listed) == 14 and {e["id"][0] for e in listed} == {"e"}
    assert sum(1 for e in listed if e.get("hole_mm") == 3.0) == 2


@needs_cad_kernel
def test_a_selection_that_finishes_nothing_is_refused_with_the_reasons(tmp_path):
    from kiln.edge_finish import NOTHING_FINISHED, fillet_part

    reply = fillet_part(block(tmp_path / "block.step"), edges="inside")
    assert (reply["success"], reply["code"]) == (False, NOTHING_FINISHED)
    assert "no sharp edge matched the selection" in reply["message"]
    assert not (tmp_path / "block_filleted.stl").exists()


@needs_cad_kernel
def test_a_result_that_cost_the_part_a_hole_is_refused(tmp_path, monkeypatch):
    """Holes are judged on the CAD.  A build that came back without one is
    refused, whatever the mesh looks like."""
    import kiln.cad_edge as cad_edge
    from kiln.edge_finish import fillet_part

    real = cad_edge.finish_step

    def lost_its_hole(*args, **kwargs):
        return {**real(*args, **kwargs), "holes_after_mm": []}

    monkeypatch.setattr(cad_edge, "finish_step", lost_its_hole)
    step = block_with_hole(tmp_path / "block.step")
    reply = fillet_part(step, radius_mm=1.0)
    assert (reply["success"], reply["code"]) == (False, EDIT_REFUSED)
    assert "lost or resized the 3 mm hole" in reply["message"]
    assert not (tmp_path / "block_filleted.stl").exists() and not (tmp_path / "block_filleted.step").exists()


@needs_cad_kernel
def test_a_bevelled_hole_rim_keeps_its_hole_on_the_cad(tmp_path):
    from kiln.edge_finish import chamfer_part

    reply = chamfer_part(post_with_blind_hole(tmp_path / "post.step"), distance_mm=0.2, edges="holes")
    assert reply["success"] is True, reply
    assert reply["holes_mm"] == {"before": [2.2], "after": [2.2]}
    assert len(reply["edges"]["finished"]) == 1


@needs_cad_kernel
def test_a_result_too_big_to_check_is_not_handed_back(tmp_path, monkeypatch):
    from kiln.edge_finish import fillet_part

    monkeypatch.setenv("KILN_EDIT_CHECK_MAX_TRIANGLES", "5000")
    reply = fillet_part(block(tmp_path / "block.step"), radius_mm=2.0)
    assert (reply["success"], reply["code"]) == (False, TOO_LARGE_TO_CHECK)
    assert "reads up to 5,000" in reply["message"] and "Select fewer edges" in reply["message"]
    assert not (tmp_path / "block_filleted.stl").exists()


def test_a_mesh_with_no_cad_behind_it_is_refused_in_a_sentence(tmp_path):
    """Measured 2026-10-01: the mesh engines opened a cube's surface and made
    it bigger (the round by 1.41 mm, the bevel by 1.0).  They are gone, and a
    mesh is told where edges are finished."""
    from kiln.edge_finish import NEEDS_CAD, chamfer_part, fillet_part

    cube = tmp_path / "cube.stl"
    trimesh.creation.box((20.0, 20.0, 20.0)).export(str(cube))
    before = cube.read_bytes()
    for finish, verb in ((fillet_part, "round"), (chamfer_part, "bevel")):
        reply = finish(str(cube))
        assert (reply["success"], reply["code"]) == (False, NEEDS_CAD)
        assert f"Kiln did not {verb} the edges" in reply["message"] and "STEP file" in reply["message"]
    assert cube.read_bytes() == before
    assert list(tmp_path.iterdir()) == [cube]


def test_the_mesh_engines_are_gone():
    import kiln.generation.validation as validation

    assert not hasattr(validation, "add_fillet") and not hasattr(validation, "add_chamfer")


def test_an_openscad_file_is_finished_in_its_script(tmp_path):
    from kiln.edge_finish import IN_THE_SCRIPT, fillet_part

    scad = tmp_path / "bracket.scad"
    scad.write_text("cube([10, 10, 10]);")
    reply = fillet_part(str(scad), radius_mm=1.5)
    assert (reply["success"], reply["code"]) == (False, IN_THE_SCRIPT)
    assert "bracket.scad" in reply["message"] and "1.5 mm round" in reply["message"]
    assert reply["script"] == {"scad_path": str(scad), "recipe": None}
    assert scad.read_text() == "cube([10, 10, 10]);"


def test_a_mesh_whose_recipe_keeps_its_openscad_source_is_finished_in_its_script(tmp_path):
    from kiln.design_recipe import create_recipe, save_recipe
    from kiln.edge_finish import IN_THE_SCRIPT, NEEDS_CAD, chamfer_part, script_behind

    part = tmp_path / "body.stl"
    trimesh.creation.box((10.0, 10.0, 10.0)).export(str(part))
    stray = tmp_path / "stray.stl"
    trimesh.creation.box((5.0, 5.0, 5.0)).export(str(stray))
    recipe = create_recipe(
        "box", parts=[{"name": "body", "role": "structural", "stl_path": "body.stl", "color": "white"}],
        source_scad="cube([10, 10, 10]);",
    )
    recipe_file = save_recipe(recipe, str(tmp_path))
    assert script_behind(str(part)) == {"scad_path": None, "recipe": recipe_file}
    reply = chamfer_part(str(part))
    assert (reply["success"], reply["code"]) == (False, IN_THE_SCRIPT)
    assert "design recipe" in reply["message"]
    # A mesh the recipe does not list is not the script's.
    assert script_behind(str(stray)) is None
    assert chamfer_part(str(stray))["code"] == NEEDS_CAD


def test_the_printer_a_plan_is_sized_for_says_where_each_figure_came_from():
    from kiln.edge_finish import print_frame

    frame, said = print_frame(nozzle_mm=0.6, layer_height_mm=0.3, material="PETG")
    assert (frame.nozzle_mm, frame.layer_mm) == (0.6, 0.3)
    assert said["nozzle"]["source"] == "stated" and said["layer_height_from"] == "the layer height it was given"
    assert "0.6 mm nozzle" in said["note"] and "in PETG" in said["note"]
    # Nothing stated, no printer named: the default profile's layer, said as that.
    frame, said = print_frame(nozzle_mm=0.4)
    assert frame.layer_mm == 0.2 and said["layer_height_from"] == "Kiln's default slicing profile"
    # A layer taller than the nozzle is not a layer height.
    assert print_frame(nozzle_mm=0.4, layer_height_mm=0.9)[0].layer_mm == 0.2


# ---------------------------------------------------------------------------
# Thickening hands its CAD on too
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_a_thickened_cad_part_can_be_rounded_from_its_cad(tmp_path):
    pytest.importorskip("manifold3d")
    from kiln.edge_finish import fillet_part
    from kiln.wall_thicken import thicken_part

    thick = thicken_part(block_with_hole(tmp_path / "block.step"), amount_mm=0.4, output_path=str(tmp_path / "thick.stl"))
    assert thick["success"] is True and thick["method"] == "cad", thick
    assert thick["step_path"] == str(tmp_path / "thick.step") and "thick.step" in thick["note"]
    rounded = fillet_part(thick["path"], radius_mm=1.0, edges="vertical")
    assert rounded["success"] is True, rounded
    assert rounded["cad_source"] == thick["step_path"]
    assert rounded["measured"]["before"]["size_mm"] == [30.8, 20.8, 10.8]
    assert rounded["holes_mm"] == {"before": [3.0], "after": [3.0]}


# ---------------------------------------------------------------------------
# Through the registered tools
# ---------------------------------------------------------------------------


@pytest.fixture
def tools(monkeypatch):
    """The mesh and design-reasoning tools as registered, with the inspect bundle recorded rather than rendered."""
    import types

    import kiln.server
    from kiln.plugins.design_reasoning_tools import plugin as reasoning
    from kiln.plugins.mesh_tools import plugin as mesh

    registered: dict = {}

    class _Mcp:
        def tool(self, *_a, **_kw):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    bundle: dict = {}

    def attach_inspect_bundle(reply, **kw):
        bundle.update(kw)
        return reply

    fake = types.ModuleType("kiln_pro.plugins.git_render_tools")
    fake.attach_inspect_bundle = attach_inspect_bundle
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", fake)
    monkeypatch.setattr(kiln.server, "_check_auth", lambda *_a, **_k: None)
    mesh.register(_Mcp())
    reasoning.register(_Mcp())
    registered["_bundle"] = bundle
    return registered


@needs_cad_kernel
@pytest.mark.parametrize(
    ("tool", "size", "volume"),
    [("add_mesh_fillet", {"radius_mm": 2.0}, rounded_block_volume(30, 20, 10, 2.0)),
     ("add_mesh_chamfer", {"distance_mm": 2.0}, bevelled_block_volume(30, 20, 10, 2.0))],
)
def test_the_tool_finishes_a_step_part_and_grades_it_against_its_mesh(tools, tmp_path, tool, size, volume):
    step = block(tmp_path / "block.step")
    reply = tools[tool](file_path=step, output_path=str(tmp_path / "out.stl"), **size)
    assert reply["success"] is True, reply
    assert reply["volume_mm3"]["after"] == pytest.approx(volume, abs=1e-3)
    assert os.path.isfile(reply["path"]) and os.path.isfile(reply["step_path"])
    assert reply["measured"]["after"]["closed_surface"] is True
    before = tools["_bundle"]["self_check_before"]
    assert before.endswith(".stl")
    assert measure_mesh(before).extents_mm == pytest.approx((30.0, 20.0, 10.0), abs=0.01)
    assert tools["_bundle"]["stl_keys"] == ("path",)


@needs_cad_kernel
def test_the_tool_selects_edges_and_sizes_for_the_nozzle_it_is_told(tools, tmp_path):
    reply = tools["add_mesh_fillet"](
        file_path=open_box(tmp_path / "box.step"), radius_mm=1.0, edges="top,outside", nozzle_mm=0.6,
        layer_height_mm=0.3, output_path=str(tmp_path / "out.stl"),
    )
    assert reply["success"] is True, reply
    assert reply["nozzle"]["diameter_mm"] == 0.6
    # The wall top is 1.6 mm wide: two rounds and one 0.6 mm nozzle width of flat.
    assert {f["size_mm"] for f in reply["edges"]["finished"]} == {0.5}
    assert {f["place"] for f in reply["edges"]["finished"]} == {"top"}
    assert reply["measured"]["after"]["thinnest_wall_mm"] >= reply["measured"]["before"]["thinnest_wall_mm"] - 0.05


@needs_cad_kernel
def test_the_tools_plan_only_lists_edge_ids_and_attaches_no_picture(tools, tmp_path):
    reply = tools["add_mesh_chamfer"](file_path=block(tmp_path / "block.step"), plan_only=True)
    assert reply["plan_only"] is True and len(reply["all_edges"]) == 12
    assert tools["_bundle"] == {}


def test_the_tool_refuses_a_mesh_as_an_error_with_its_code(tools, tmp_path):
    from kiln.edge_finish import NEEDS_CAD

    cube = tmp_path / "cube.stl"
    trimesh.creation.box((20.0, 20.0, 20.0)).export(str(cube))
    for tool in ("add_mesh_fillet", "add_mesh_chamfer"):
        reply = tools[tool](file_path=str(cube))
        assert reply["success"] is False
        assert reply["error"]["code"] == NEEDS_CAD
        assert "sized_for" not in reply  # nothing was planned, so nothing was sized


def test_the_tool_says_what_it_cannot_read_is_invalid(tools, tmp_path):
    missing = tools["add_mesh_fillet"](file_path=str(tmp_path / "missing.step"))
    assert missing["error"]["code"] == "INVALID_ARGS" and "File not found" in missing["error"]["message"]
    cube = tmp_path / "cube.stl"
    trimesh.creation.box((20.0, 20.0, 20.0)).export(str(cube))
    for bad in ({"radius_mm": 0.0}, {"angle_threshold_deg": 180.0}):
        assert tools["add_mesh_fillet"](file_path=str(cube), **bad)["error"]["code"] == "INVALID_ARGS"


@needs_cad_kernel
def test_the_tool_says_a_selection_it_cannot_read_is_invalid(tools, tmp_path):
    reply = tools["add_mesh_fillet"](file_path=block(tmp_path / "block.step"), edges="sideways")
    assert reply["error"]["code"] == "INVALID_ARGS"
    assert "'sideways' is not an edge selection" in reply["error"]["message"]


# ---------------------------------------------------------------------------
# The reinforcement step
# ---------------------------------------------------------------------------


def _needs_rounding(tmp_path: Path) -> str:
    """The open box: its plan asks for its sharp corners to be rounded."""
    return open_box(tmp_path / "box.step")


@needs_cad_kernel
def test_reinforcing_a_cad_part_rounds_it_on_the_cad_and_says_what_prints_badly(tmp_path, monkeypatch):
    """Without a chooser: the radius asked, on every sharp edge, with the
    edges that will droop listed and one sentence saying who chooses for you."""
    import kiln.design_reasoning as dr

    monkeypatch.setattr(dr, "_edge_policy", lambda *_a, **_k: None)
    result = dr.apply_reinforcements(_needs_rounding(tmp_path), output_path=str(tmp_path / "out.stl"), fillet_radius_mm=1.0)
    rounded = next(e for e in result.applied if e["type"] == "fillet")
    finished = rounded["edges"]["finished"]
    assert {f["finish"] for f in finished} == {"fillet"}
    assert sum(1 for f in finished if f["place"] == "bottom") == 4
    assert [c["kind"] for c in rounded["edges"]["cautions"]].count("on_the_bed") == 4
    assert "4 of the finished edges will print badly as asked" in result.print_aware
    assert "kiln3d.com/pricing?src=agent&tool=apply_design_reinforcements" in result.print_aware
    assert measure_mesh(result.output_path).watertight is True


@needs_cad_kernel
def test_reinforcing_hands_the_edges_to_a_chooser_and_the_cad_to_the_result(tmp_path, monkeypatch):
    import kiln.design_reasoning as dr

    handed = {}

    def policy(material, fastener):
        handed.update(material=material, fastener=fastener)

        def choose(chains, frame):
            handed["frame"] = frame
            return {c["chain"]: (CHAMFER, c["asked_mm"], "bevelled on the bed") for c in chains if c["place"] == "bottom"}

        return choose

    monkeypatch.setattr(dr, "_edge_policy", policy)
    # No base plate or gusset here: every edit applied was a CAD one.
    monkeypatch.setattr(dr, "_apply_base", lambda *_a, **_k: None)
    monkeypatch.setattr(dr, "_apply_gusset", lambda *_a, **_k: None)
    result = dr.apply_reinforcements(
        _needs_rounding(tmp_path), output_path=str(tmp_path / "out.stl"), fillet_radius_mm=1.0,
        nozzle_mm=0.4, layer_height_mm=0.2, material="PETG", fastener="M3",
    )
    assert handed["material"] == "PETG" and handed["fastener"] == "M3"
    assert (handed["frame"].nozzle_mm, handed["frame"].layer_mm) == (0.4, 0.2)
    rounded = next(e for e in result.applied if e["type"] == "fillet")
    on_bed = [f for f in rounded["edges"]["finished"] if f["place"] == "bottom"]
    assert len(on_bed) == 4 and {f["finish"] for f in on_bed} == {"chamfer"}
    assert not rounded["edges"]["cautions"]
    assert result.print_aware == "Each edge's finish was chosen for how it prints on this printer."
    assert result.step_path == str(tmp_path / "out.step") and os.path.isfile(result.step_path)
    assert result.to_dict()["step_path"] == result.step_path


def test_reinforcing_a_mesh_skips_the_rounding_and_says_why(tmp_path, monkeypatch):
    manifold3d = pytest.importorskip("manifold3d")
    import numpy as np

    import kiln.design_reasoning as dr

    cube = manifold3d.Manifold.cube
    flat = (cube([40.0, 30.0, 15.0]) - cube([38.0, 28.0, 15.0]).translate([1.0, 1.0, 1.0])).to_mesh()
    stl = tmp_path / "box.stl"
    trimesh.Trimesh(np.asarray(flat.vert_properties)[:, :3], np.asarray(flat.tri_verts), process=False).export(str(stl))
    result = dr.apply_reinforcements(str(stl), output_path=str(tmp_path / "out.stl"))
    skipped = next(e for e in result.skipped if e["type"] == "fillet")
    assert "mesh with no CAD file behind it" in skipped["reason"]
    assert result.step_path is None


# ---------------------------------------------------------------------------
# Kernel names, on either kernel version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [("Face_s", "Edge_s"), ("Face", "Edge")])
def test_a_shape_is_narrowed_under_whichever_name_the_kernel_has(monkeypatch, names):
    """Kernel 7.9 spells it ``TopoDS.Face_s``, kernel 8.0 ``TopoDS.Face``."""
    import types

    from kiln import cad_kernel

    face_name, edge_name = names
    topods = type("TopoDS", (), {
        face_name: staticmethod(lambda shape: ("face", shape)),
        edge_name: staticmethod(lambda shape: ("edge", shape)),
    })
    module = types.ModuleType("OCP.TopoDS")
    module.TopoDS = topods
    monkeypatch.setitem(sys.modules, "OCP", types.ModuleType("OCP"))
    monkeypatch.setitem(sys.modules, "OCP.TopoDS", module)
    assert cad_kernel.as_face("s") == ("face", "s")
    assert cad_kernel.as_edge("s") == ("edge", "s")


#: Spellings only one supported kernel version has.  ``kiln.cad_kernel`` is the
#: one module that may name them, as the first thing it tries.
_ONE_VERSION_ONLY = (
    "TopoDS.Solid_s", "TopoDS.Face_s", "TopoDS.Edge_s", "TopoDS.Vertex_s", "TopoDS.Shell_s", "TopoDS.Wire_s",
    "TopTools_IndexedMapOfShape", "TopTools_IndexedDataMapOfShapeListOfShape", "TopTools_ListOfShape",
)


def _one_version_spellings(source: str) -> list[str]:
    return [name for name in _ONE_VERSION_ONLY if name in source]


def test_no_kernel_child_names_a_call_only_one_kernel_version_has():
    """2026-10-04: the thickening child, written on kernel 7.9, named three
    things kernel 8.0 renamed, and stopped at its first import on every
    machine with the newer kernel -- the server included.  It reported the
    failure as a note and fell back to the mesh route, so nothing looked
    broken.  A child is read here, without a kernel, because a machine on 7.9
    runs the old spellings happily."""
    src = Path(__file__).resolve().parents[1] / "src" / "kiln"
    children = [src / "cad_offset.py", src / "cad_edge.py"]
    assert all(p.is_file() for p in children)
    found = {p.name: _one_version_spellings(p.read_text(encoding="utf-8")) for p in children}
    assert found == {"cad_offset.py": [], "cad_edge.py": []}


def test_the_scan_sees_each_spelling_it_is_looking_for():
    old_child = "edges = TopTools_IndexedMapOfShape()\nface = TopoDS.Face_s(found.FindKey(i))\n"
    assert _one_version_spellings(old_child) == ["TopoDS.Face_s", "TopTools_IndexedMapOfShape"]


# ---------------------------------------------------------------------------
# Memory, against the smallest machine Kiln runs on
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_the_kernel_child_stays_far_inside_a_two_gigabyte_machine(tmp_path):
    """Measured 2026-10-04: rounding every edge of an enclosure peaks at
    0.4 GB in the child, and a full-bed plate of 144 posts at 0.9 GB.  The
    bound is loose (1 GB for a small part) so it never flakes; what it
    catches is an order of magnitude, not a margin."""
    import json
    import resource
    import subprocess

    import kiln
    from kiln.cad_edge import survey_step

    step = open_box(tmp_path / "box.step")
    plan = _plan(survey_step(step), FILLET, 1.0)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "do": "finish", "step": step, "out": str(tmp_path / "out.stl"), "out_step": None,
        "treatments": plan.treatments, "linear": 5e-3, "angular": 0.1, "search_s": 60,
    }))
    child = Path(kiln.__file__).with_name("cad_edge.py")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    run = subprocess.run(
        [sys.executable, str(child), str(request)], capture_output=True, text=True, timeout=180,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )
    assert run.returncode == 0, run.stderr[-1000:]
    assert json.loads(run.stdout.strip().splitlines()[-1])["applied"]
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    assert peak >= before
    assert peak_mb < 1000, f"the kernel child peaked at {peak_mb:.0f} MB"


def test_a_mesh_past_the_check_limit_is_refused_before_it_is_read(tmp_path, monkeypatch):
    from kiln.mesh_edit_check import MAX_CHECK_TRIANGLES, MeshTooLarge, guarded_edit, max_check_triangles

    assert max_check_triangles() == MAX_CHECK_TRIANGLES == 250_000
    cube = tmp_path / "cube.stl"
    trimesh.creation.box((20.0, 20.0, 20.0)).export(str(cube))
    monkeypatch.setenv("KILN_EDIT_CHECK_MAX_TRIANGLES", "8")
    with pytest.raises(MeshTooLarge) as said:
        measure_mesh(str(cube))
    assert (said.value.triangles, said.value.limit) == (12, 8)
    ran = []
    reply = guarded_edit(str(cube), str(tmp_path / "out.stl"), lambda scratch: ran.append(scratch), edit="round the edges", instead="")
    assert (reply["success"], reply["code"]) == (False, TOO_LARGE_TO_CHECK)
    assert "12 triangles" in reply["message"] and not ran
