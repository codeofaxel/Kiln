"""Thickening a part's walls makes them thicker -- measured, not assumed.

Found 2026-10-01 on a clean CAD enclosure (80 x 55 x 28 mm, walls 1.2-2.0 mm,
eight screw holes): ``thicken_mesh_walls(0.4)`` left its thinnest wall at
0.096 mm, lost all eight holes, creased the floor and grew the part 0.8 mm --
and reported success.  Its seven tests checked that a file was written, that
the triangle count was unchanged and that a negative amount raised; none
measured a wall.  ``add_mesh_fillet`` and ``add_mesh_chamfer``, from the same
commit, opened the surface of a plain cube and made it BIGGER (their tests
are test_edge_finish.py's now).

So these tests build real parts -- a block with a through-hole as CAD and as a
mesh, a thin open box with a hole in its floor -- run the real engines, and
read the thinnest wall, the holes, the outside size and whether the surface is
closed, with the same analyzer the rest of Kiln reports through.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiln.mesh_edit_check import (
    EDIT_REFUSED,
    MeshMeasure,
    judge_edit,
    measure_mesh,
)

manifold3d = pytest.importorskip("manifold3d")
trimesh = pytest.importorskip("trimesh")


def _have_ocp() -> bool:
    try:
        import OCP.BRepOffset  # noqa: F401

        return True
    except ImportError:
        return False


needs_cad_kernel = pytest.mark.skipif(not _have_ocp(), reason="the OpenCascade kernel (OCP) is not installed")


def _write_mesh(manifold, path: Path) -> str:
    mesh = manifold.to_mesh()
    import numpy as np

    trimesh.Trimesh(
        vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=False,
    ).export(str(path))
    return str(path)


def _block_with_hole(path: Path) -> str:
    """30 x 20 x 10 mm, a 3 mm through-hole down its middle."""
    m = manifold3d.Manifold
    block = m.cube([30.0, 20.0, 10.0])
    bore = m.cylinder(14.0, 1.5, 1.5, 64).translate([15.0, 10.0, -2.0])
    return _write_mesh(block - bore, path)


def _thin_box_with_hole(path: Path) -> str:
    """An open-topped 40 x 30 x 15 mm box, 1 mm walls, a 3 mm hole in its floor."""
    m = manifold3d.Manifold
    outer = m.cube([40.0, 30.0, 15.0])
    inner = m.cube([38.0, 28.0, 15.0]).translate([1.0, 1.0, 1.0])
    bore = m.cylinder(4.0, 1.5, 1.5, 64).translate([20.0, 15.0, -1.0])
    return _write_mesh(outer - inner - bore, path)


def _step_block_with_hole(path: Path) -> str:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    block = BRepPrimAPI_MakeBox(30.0, 20.0, 10.0).Shape()
    bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(15.0, 10.0, -2.0), gp_Dir(0, 0, 1)), 1.5, 14.0).Shape()
    cut = BRepAlgoAPI_Cut(block, bore)
    cut.Build()
    writer = STEPControl_Writer()
    writer.Transfer(cut.Shape(), STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


def _thicker_and_intact(reply: dict, amount: float) -> None:
    """The promise, read off the measurements the reply carries."""
    assert reply["success"] is True, reply
    before, after = reply["measured"]["before"], reply["measured"]["after"]
    assert after["closed_surface"] is True
    assert after["thinnest_wall_mm"] >= before["thinnest_wall_mm"] + 0.9 * amount
    for b, a in zip(before["size_mm"], after["size_mm"], strict=True):
        assert a == pytest.approx(b + 2 * amount, abs=0.05)
    assert len(after["holes_mm"]) == len(before["holes_mm"])
    for b, a in zip(sorted(before["holes_mm"]), sorted(after["holes_mm"]), strict=True):
        assert a == pytest.approx(b, abs=0.1)


# ---------------------------------------------------------------------------
# The two routes
# ---------------------------------------------------------------------------


@needs_cad_kernel
def test_a_step_part_grows_every_surface_and_keeps_its_hole(tmp_path):
    from kiln.wall_thicken import thicken_part

    step = _step_block_with_hole(tmp_path / "block.step")
    reply = thicken_part(step, amount_mm=0.4, output_path=str(tmp_path / "out.stl"))
    assert reply["method"] == "cad"
    _thicker_and_intact(reply, 0.4)
    assert "kept their size" in reply["note"]


@needs_cad_kernel
def test_a_mesh_beside_its_step_is_thickened_from_the_cad(tmp_path):
    """The mesh Kiln converted from a STEP still sits beside it, under its
    name: the walls are moved in the CAD, not on the triangles."""
    from kiln.step_import import convert_step_to_stl
    from kiln.wall_thicken import thicken_part

    step = _step_block_with_hole(tmp_path / "bracket.step")
    converted = convert_step_to_stl(step, output_dir=str(tmp_path))
    stl = str(tmp_path / "bracket.stl")
    assert os.path.isfile(stl), converted
    reply = thicken_part(stl, amount_mm=0.4, output_path=str(tmp_path / "out.stl"))
    assert reply["method"] == "cad"
    assert reply["cad_source"] == step
    _thicker_and_intact(reply, 0.4)


def test_a_lone_mesh_gets_a_true_offset_with_its_hole_kept(tmp_path):
    """No CAD file: the part is grown by a ball and the hole cut back to size.
    The old vertex push on this box lost the hole and pushed the 1 mm walls
    through each other."""
    from kiln.wall_thicken import thicken_part

    mesh = _thin_box_with_hole(tmp_path / "box.stl")
    reply = thicken_part(mesh, amount_mm=0.4, output_path=str(tmp_path / "out.stl"))
    assert reply["method"] == "mesh"
    _thicker_and_intact(reply, 0.4)


@needs_cad_kernel
def test_holes_may_close_in_when_asked_but_never_vanish(tmp_path):
    from kiln.wall_thicken import thicken_part

    step = _step_block_with_hole(tmp_path / "block.step")
    reply = thicken_part(step, amount_mm=0.4, output_path=str(tmp_path / "out.stl"), keep_hole_size=False)
    assert reply["success"] is True, reply
    (hole,) = reply["measured"]["after"]["holes_mm"]
    assert hole == pytest.approx(3.0 - 2 * 0.4, abs=0.1)
    assert "narrower, as asked" in reply["note"]


def test_a_worse_result_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    """Whatever the engine hands back is measured before anyone is told it worked."""
    import kiln.mesh_offset as mo
    from kiln.wall_thicken import thicken_part

    mesh = _thin_box_with_hole(tmp_path / "box.stl")

    def open_surface(src, out, **_kw):
        broken = trimesh.load(src, force="mesh")
        broken.update_faces(list(range(len(broken.faces) - 4)))  # four triangles gone
        broken.export(out)
        return {}

    monkeypatch.setattr(mo, "offset_mesh", open_surface)
    out = tmp_path / "out.stl"
    reply = thicken_part(mesh, amount_mm=0.4, output_path=str(out))
    assert reply["success"] is False
    assert reply["code"] == EDIT_REFUSED
    assert "no longer closed" in reply["message"]
    assert "Your file is unchanged" in reply["message"]
    assert not out.exists()


def test_an_amount_out_of_range_is_an_error(tmp_path):
    from kiln.wall_thicken import thicken_part

    mesh = _block_with_hole(tmp_path / "b.stl")
    for amount in (0, -1, 6):
        with pytest.raises(ValueError):
            thicken_part(mesh, amount_mm=amount)


def test_the_reinforcement_step_says_why_it_did_not_thicken(tmp_path, monkeypatch):
    """apply_design_reinforcements reaches the same door, and a refusal
    reaches its reply instead of "no thin walls detected"."""
    import kiln.wall_thicken as wt
    from kiln.design_reasoning import _apply_thicken

    monkeypatch.setattr(
        wt, "thicken_part",
        lambda *a, **k: {"success": False, "code": EDIT_REFUSED, "message": "Kiln did not thicken the walls: x"},
    )
    reply = _apply_thicken(str(tmp_path / "p.stl"), str(tmp_path), 0.4)
    assert reply["success"] is False
    assert reply["message"] == "Kiln did not thicken the walls: x"


# ---------------------------------------------------------------------------
# The measured check
# ---------------------------------------------------------------------------


def _m(**kw) -> MeshMeasure:
    base = dict(extents_mm=(30.0, 20.0, 10.0), watertight=True, volume_mm3=5900.0, min_wall_mm=1.2,
                hole_diameters_mm=(3.0,))
    base.update(kw)
    return MeshMeasure(**base)


@pytest.mark.parametrize(
    ("after", "kwargs", "says"),
    [
        (_m(watertight=False), {}, "no longer closed"),
        (_m(extents_mm=(31.4, 21.4, 11.4)), {}, "only ever takes material away"),
        (_m(extents_mm=(32.0, 20.8, 10.8)), {"grows_by_mm": 0.4}, "grew 2.00 mm across"),
        (_m(hole_diameters_mm=()), {}, "3.0 mm hole is gone"),
        (_m(hole_diameters_mm=(2.2,)), {}, "3.0 mm hole is gone or no longer 3.0 mm"),
        (_m(min_wall_mm=0.096), {}, "went from 1.20 mm to 0.10 mm"),
        (_m(min_wall_mm=1.3, extents_mm=(30.8, 20.8, 10.8)), {"grows_by_mm": 0.4, "wall_grows_by_mm": 0.4},
         "should have gained at least 0.4 mm"),
    ],
)
def test_judge_edit_names_each_way_a_result_is_worse(after, kwargs, says):
    verdict = judge_edit(_m(), after, **kwargs)
    assert verdict.ok is False
    assert any(says in p for p in verdict.problems), verdict.problems


def test_judge_edit_passes_an_honest_thickening():
    after = _m(extents_mm=(30.8, 20.8, 10.8), min_wall_mm=2.0)
    assert judge_edit(_m(), after, grows_by_mm=0.4, wall_grows_by_mm=0.4).ok


def test_judge_edit_lets_held_off_holes_shrink_but_not_vanish():
    shrunk = _m(extents_mm=(30.8, 20.8, 10.8), min_wall_mm=2.0, hole_diameters_mm=(2.2,))
    assert judge_edit(_m(), shrunk, grows_by_mm=0.4, wall_grows_by_mm=0.4, holes_keep_size=False).ok
    gone = _m(extents_mm=(30.8, 20.8, 10.8), min_wall_mm=2.0, hole_diameters_mm=())
    assert not judge_edit(_m(), gone, grows_by_mm=0.4, wall_grows_by_mm=0.4, holes_keep_size=False).ok


def test_measure_mesh_reads_a_real_part(tmp_path):
    measured = measure_mesh(_block_with_hole(tmp_path / "b.stl"))
    assert measured.watertight is True
    assert measured.extents_mm == pytest.approx((30.0, 20.0, 10.0), abs=0.01)
    assert measured.hole_diameters_mm == pytest.approx((3.0,), abs=0.05)


# ---------------------------------------------------------------------------
# Through the registered tools
# ---------------------------------------------------------------------------


@pytest.fixture
def tools(monkeypatch):
    """The mesh tools as registered, with the inspect bundle recorded rather than rendered."""
    import sys
    import types

    import kiln.server
    from kiln.plugins.mesh_tools import plugin

    registered: dict = {}

    class _Mcp:
        def tool(self, **_kw):
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
    plugin.register(_Mcp())
    registered["_bundle"] = bundle
    return registered


@needs_cad_kernel
def test_the_tool_thickens_a_step_part_and_grades_it_against_its_mesh(tools, tmp_path):
    step = _step_block_with_hole(tmp_path / "block.step")
    reply = tools["thicken_mesh_walls"](file_path=step, amount_mm=0.4, output_path=str(tmp_path / "out.stl"))
    assert reply["success"] is True, reply
    assert reply["method"] == "cad"
    assert os.path.isfile(reply["path"])
    before = tools["_bundle"]["self_check_before"]
    assert before.endswith(".stl")
    assert measure_mesh(before).extents_mm == pytest.approx((30.0, 20.0, 10.0), abs=0.01)
