"""A CAD part stays a CAD part through a resize, a turn, a mirror and a move.

The part's STEP file sits beside the mesh Kiln converted from it.  A door
that resizes, turns, mirrors or moves that mesh makes the same change to the
exact solid and leaves the changed CAD beside the changed mesh, so the next
edit that works on CAD finds it.  Every case here goes through the tool as
registered and then reads the CAD that came back with the kernel: its box,
its volume, its hole, and whether its faces are still flat and round.

Also pinned: a mesh with no CAD gets no CAD; a stretch along one axis never
keeps the CAD and says so; and a changed solid that does not match the
changed mesh is refused, never kept.
"""

from __future__ import annotations

import os
import shutil
import sys
import types
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")


def _have_ocp() -> bool:
    try:
        import OCP.BRepBuilderAPI  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _have_ocp(), reason="the OpenCascade kernel (OCP) is not installed")

_REAL_EXPORT = Path(__file__).parent / "real_exports" / "enclosure_v1.step"


def _step_block_with_hole(path: Path) -> str:
    """30 x 20 x 10 mm with a 3 mm through-hole, as exact CAD."""
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


def _cad_part(folder: Path, name: str = "bracket", source: Path | None = None) -> tuple[str, str]:
    """A STEP file and the mesh Kiln converts from it, side by side: ``(step, stl)``."""
    from kiln.step_import import convert_step_to_stl

    step = folder / f"{name}.step"
    if source is None:
        _step_block_with_hole(step)
    else:
        shutil.copyfile(source, step)
    convert_step_to_stl(str(step), output_dir=str(folder))
    stl = folder / f"{name}.stl"
    assert stl.is_file()
    return str(step), str(stl)


def _read(step_path: str) -> dict:
    """What the kernel says a STEP file holds."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepCheck import BRepCheck_Analyzer

    from kiln import cad_kernel as k

    solid, why_not = k.read_one_solid(step_path, verb="reads")
    assert solid is not None, why_not
    kinds: dict[str, int] = {}
    for face in k.subshapes(solid, "face"):
        kind = str(BRepAdaptor_Surface(k.as_face(face)).GetType()).rsplit("_", 1)[-1]
        kinds[kind] = kinds.get(kind, 0) + 1
    box = k.box(solid)
    return {
        "size": tuple(round(box[i + 3] - box[i], 4) for i in range(3)),
        "box": box,
        "volume": k.volume(solid),
        "holes": sorted(round(2 * h["radius"], 4) for h in k.full_cylinders(solid, concave=True)),
        "kinds": kinds,
        "valid": bool(BRepCheck_Analyzer(solid).IsValid()),
    }


def _mesh_box(path: str) -> tuple[float, ...]:
    from kiln.mesh_frame import load_mesh

    low, high = load_mesh(path, force="mesh").bounds
    return (*low, *high)


@pytest.fixture
def tools(monkeypatch):
    """The mesh tools as registered, with the inspect bundle left out."""
    import kiln.server
    from kiln.plugins.mesh_tools import plugin

    registered: dict = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    fake = types.ModuleType("kiln_pro.plugins.git_render_tools")
    fake.attach_inspect_bundle = lambda reply, **_kw: reply
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", fake)
    monkeypatch.setattr(kiln.server, "_check_auth", lambda *_a, **_k: None)
    plugin.register(_Mcp())
    registered["rotate_model"] = kiln.server.rotate_model
    return registered


def _followed(reply: dict) -> dict:
    assert reply["success"] is True, reply
    assert reply.get("cad_kept") is True, reply
    assert os.path.isfile(reply["step_path"])
    return _read(reply["step_path"])


# ---------------------------------------------------------------------------
# Each door
# ---------------------------------------------------------------------------


def test_a_resize_to_fit_the_bed_keeps_the_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    before = _read(step)
    reply = tools["scale_mesh_to_fit"](file_path=stl, max_x_mm=15.0, max_y_mm=15.0, max_z_mm=15.0)
    after = _followed(reply)

    assert after["valid"]
    assert after["size"] == pytest.approx((15.0, 10.0, 5.0), abs=1e-3)
    assert after["volume"] == pytest.approx(before["volume"] * 0.5**3, rel=1e-6)
    assert after["holes"] == pytest.approx([1.5], abs=1e-3)  # the 3 mm hole, at half size, still a hole
    assert after["kinds"] == before["kinds"]  # flat stays flat, round stays round
    assert after["box"] == pytest.approx(_mesh_box(reply["path"]), abs=0.05)
    assert "not a file Kiln made" not in reply["cad_note"]  # the file left alone is the part's own CAD
    # The caller's own STEP is as it was; the resized CAD is a file beside it.
    assert _read(step)["size"] == before["size"]
    assert os.path.realpath(reply["step_path"]) != os.path.realpath(step)


def test_the_next_cad_edit_finds_the_resized_cad(tools, tmp_path):
    from kiln.step_import import step_converted_from

    _step, stl = _cad_part(tmp_path)
    reply = tools["scale_mesh_to_fit"](file_path=stl, max_x_mm=15.0, max_y_mm=15.0, max_z_mm=15.0)
    assert step_converted_from(reply["path"]) == reply["step_path"]

    thicker = tools["thicken_mesh_walls"](
        file_path=reply["path"], amount_mm=0.4, output_path=str(tmp_path / "thicker.stl"),
    )
    assert thicker["success"] is True, thicker
    assert thicker["method"] == "cad"
    assert _read(thicker["step_path"])["size"] == pytest.approx((15.8, 10.8, 5.8), abs=1e-2)


def test_a_part_that_already_fits_is_copied_with_its_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    out = str(tmp_path / "copy" / "bracket.stl")
    os.makedirs(os.path.dirname(out))
    reply = tools["scale_mesh_to_fit"](file_path=stl, output_path=out)
    assert reply["already_fits"] is True
    assert _followed(reply)["size"] == _read(step)["size"]

    in_place = tools["scale_mesh_to_fit"](file_path=stl)
    assert in_place["already_fits"] is True
    assert "cad_kept" not in in_place and "step_path" not in in_place  # nothing changed, nothing written


def test_an_even_rescale_keeps_the_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    before = _read(step)
    reply = tools["rescale_model"](file_path=stl, scale_factor=2.0)
    after = _followed(reply)
    assert after["size"] == pytest.approx((60.0, 40.0, 20.0), abs=1e-3)
    assert after["holes"] == pytest.approx([6.0], abs=1e-3)
    assert after["volume"] == pytest.approx(before["volume"] * 8.0, rel=1e-6)
    assert after["kinds"] == before["kinds"]


def test_a_stretch_along_one_axis_never_keeps_the_cad(tools, tmp_path):
    _step, stl = _cad_part(tmp_path)
    reply = tools["rescale_model"](file_path=stl, scale_x=2.0)
    assert reply["success"] is True, reply
    assert reply["cad_kept"] is False
    assert "step_path" not in reply
    assert "stretched" in reply["cad_note"] and "even resize" in reply["cad_note"]
    assert sorted(p.name for p in tmp_path.glob("*.step")) == ["bracket.step"]


def test_a_turn_keeps_the_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    before = _read(step)
    reply = tools["rotate_model"](input_path=stl, rotation_z=90.0, output_path=str(tmp_path / "turned.stl"))
    after = _followed(reply)
    assert after["size"] == pytest.approx((20.0, 30.0, 10.0), abs=1e-3)
    assert after["volume"] == pytest.approx(before["volume"], rel=1e-6)
    assert after["holes"] == before["holes"] and after["kinds"] == before["kinds"]
    assert after["box"] == pytest.approx(_mesh_box(reply["output_path"]), abs=0.05)


def test_the_reply_says_what_the_change_was_as_one_map(tools, tmp_path):
    """Every corner of the part's CAD as it was, sent through the reply's
    map, lands on the CAD as it is: for a resize, a turn and a mirror."""
    import hashlib
    import itertools

    for door, kwargs, key in (
        ("scale_mesh_to_fit", {"max_x_mm": 15.0, "max_y_mm": 15.0, "max_z_mm": 15.0, "output_path": "o.stl"}, "path"),
        ("rotate_model", {"rotation_z": 90.0, "rotation_x": 30.0, "output_path": "o.stl"}, "output_path"),
        ("mirror_mesh_model", {"axis": "y", "output_path": "o.stl"}, "path"),
    ):
        folder = tmp_path / door
        folder.mkdir()
        step, stl = _cad_part(folder)
        kwargs = {**kwargs, "output_path": str(folder / kwargs["output_path"])}
        name = "input_path" if door == "rotate_model" else "file_path"
        reply = tools[door](**{name: stl}, **kwargs)
        after = _followed(reply)
        change = reply["cad_change"]
        assert change["kind"] == "placed"
        assert change["mirrored"] is (door == "mirror_mesh_model")
        assert change["from_sha256"] == hashlib.sha256(Path(step).read_bytes()).hexdigest()
        m = [change["matrix"][i * 4:i * 4 + 4] for i in range(4)]
        assert m[3] == [0.0, 0.0, 0.0, 1.0]
        was = _read(step)["box"]
        corners = [
            [sum(m[i][j] * c[j] for j in range(3)) + m[i][3] for i in range(3)]
            for c in itertools.product((was[0], was[3]), (was[1], was[4]), (was[2], was[5]))
        ]
        lows = [min(c[i] for c in corners) for i in range(3)]
        highs = [max(c[i] for c in corners) for i in range(3)]
        if door != "rotate_model":  # a tilted box's corners bound it loosely
            assert (*lows, *highs) == pytest.approx(after["box"], abs=1e-3)
        det = (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )
        assert det == pytest.approx(after["volume"] / _read(step)["volume"] * (-1 if change["mirrored"] else 1), rel=1e-6)


def test_a_turn_onto_its_side_keeps_the_cad(tools, tmp_path):
    _step, stl = _cad_part(tmp_path)
    reply = tools["rotate_model"](
        input_path=stl, rotation_x=90.0, rotation_z=30.0, output_path=str(tmp_path / "turned.stl"),
    )
    after = _followed(reply)
    assert after["holes"] == pytest.approx([3.0], abs=1e-3)
    assert after["box"] == pytest.approx(_mesh_box(reply["output_path"]), abs=0.05)


def test_a_mirror_keeps_the_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    before = _read(step)
    reply = tools["mirror_mesh_model"](file_path=stl, axis="x", output_path=str(tmp_path / "mirrored.stl"))
    after = _followed(reply)
    assert after["valid"]
    assert after["size"] == before["size"]
    assert after["volume"] == pytest.approx(before["volume"], rel=1e-6)
    assert after["box"] == pytest.approx(_mesh_box(reply["path"]), abs=0.05)


def test_a_move_to_the_middle_of_the_bed_keeps_the_cad(tools, tmp_path):
    step, stl = _cad_part(tmp_path)
    reply = tools["center_model_on_bed"](file_path=stl, bed_x_mm=200.0, bed_y_mm=200.0)
    after = _followed(reply)
    assert after["size"] == _read(step)["size"]
    assert after["box"] == pytest.approx(_mesh_box(reply["path"]), abs=0.05)
    assert after["box"][0] == pytest.approx(85.0, abs=0.05)


def test_a_part_stood_the_way_it_prints_best_keeps_the_cad(tmp_path, monkeypatch):
    """The orientation door, given the STEP file itself, writes a turned mesh
    and the turned CAD beside it."""
    import kiln.server
    from kiln.plugins.printability_tools import plugin

    registered: dict = {}

    class _Mcp:
        def tool(self, **_kw):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    monkeypatch.setattr(kiln.server, "_check_auth", lambda *_a, **_k: None)
    plugin.register(_Mcp())

    step = _step_block_with_hole(tmp_path / "bracket.step")
    before = _read(step)
    out = str(tmp_path / "stood.stl")
    reply = registered["auto_orient_model"](file_path=step, apply=True, output_path=out)
    assert reply["success"] is True, reply
    assert reply.get("cad_kept") is True, reply
    after = _read(reply["step_path"])
    assert after["volume"] == pytest.approx(before["volume"], rel=1e-6)
    assert after["holes"] == before["holes"] and after["kinds"] == before["kinds"]
    assert after["box"] == pytest.approx(_mesh_box(out), abs=0.05)


def test_a_real_enclosure_resized_keeps_every_face_exact(tools, tmp_path):
    step, stl = _cad_part(tmp_path, "enclosure", _REAL_EXPORT)
    before = _read(step)
    reply = tools["scale_mesh_to_fit"](file_path=stl, max_x_mm=45.0, max_y_mm=45.0, max_z_mm=45.0)
    after = _followed(reply)
    assert after["valid"]
    assert after["size"] == pytest.approx(tuple(v / 2 for v in before["size"]), abs=1e-3)
    assert after["kinds"] == before["kinds"]
    assert after["holes"] == pytest.approx([d / 2 for d in before["holes"]], abs=1e-3)
    assert after["volume"] == pytest.approx(before["volume"] / 8, rel=1e-6)


# ---------------------------------------------------------------------------
# What must never happen
# ---------------------------------------------------------------------------


def test_a_mesh_with_no_cad_gets_no_cad(tools, tmp_path):
    lone = tmp_path / "lone.stl"
    trimesh.creation.box(extents=(30.0, 20.0, 10.0)).export(str(lone))
    reply = tools["scale_mesh_to_fit"](file_path=str(lone), max_x_mm=15.0, max_y_mm=15.0, max_z_mm=15.0)
    assert reply["success"] is True, reply
    assert not {"cad_kept", "step_path", "cad_note"} & set(reply)
    assert list(tmp_path.glob("*.step")) == []


def test_a_solid_that_does_not_match_the_mesh_is_never_kept(tmp_path):
    """The door says it only moved the part; the mesh was in fact turned."""
    from kiln.auto_orient import rotate_stl_file
    from kiln.cad_follow import watch

    _step, stl = _cad_part(tmp_path)
    watched = watch(stl)
    assert watched.source is not None
    turned = str(tmp_path / "turned.stl")
    rotate_stl_file(stl, turned, rotation_z=90.0)

    reply = watched.follow(turned)
    assert reply["cad_kept"] is False
    assert "step_path" not in reply
    assert "away from the changed mesh" in reply["cad_note"]
    assert sorted(p.name for p in tmp_path.glob("*.step")) == ["bracket.step"]
