"""A STEP holding several solids comes out as several parts, on every backend.

A CAD assembly is parts placed in positions: a base, a peg, and a second
copy of the peg inside a sub-assembly that is itself turned and moved.
Every converter has to hand that back as three meshes, each closed on its
own, each where the assembly put it, named the way the file names them,
coloured where the backend reads colour, whatever backend this machine
happens to run.  The write plan (fuse into one
STL, keep separate STLs, or one 3MF) is made once from those parts, so a
backend that sees the parts can never be overruled into one anonymous mesh.

Covers:
- the colour-aware door and every converter in the fallback chain, each run
  for real where it is installed and skipped with its reason where not
- part count, distinct names, each part closed, each part's box where the
  assembly placed it (a turned sub-assembly catches placements composed in
  the wrong order), each part's colour with an instance's own colour first
- ``merge_bodies=True`` fuses on request, even a single coloured part, and
  touching solids fused are not called a conversion fault
- surfaces a part carries beside its solid are kept, named, and never
  called a conversion fault
- a part name too long for a file name
- a file with nothing to mesh is refused in words, not handed on empty
- the ``import_step_file`` tool door
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from kiln import step_import

# Captured before any test patches them: these name what is really installed.
_HAS = {
    "freecad": step_import._find_freecad_cmd() is not None,
    "gmsh": step_import._find_gmsh_cmd() is not None,
    "occt": step_import._ocp_available(),
    "cadquery": step_import._cadquery_available(),
}
_REAL_OCP_AVAILABLE = step_import._ocp_available

#: Where the assembly puts each part, and its colour: (name, box min, box
#: max, colour) in mm.  The second peg sits 2 mm along its sub-assembly's X,
#: and the sub-assembly is turned 90 degrees then moved to (30, 15, 5), so it
#: lands at (30, 17, 5).  Composing the two placements the other way round
#: would put it at (32, 15, 5).  It is painted green on its instance, which
#: wins over the blue the peg part carries.
_BASE = ("base", (0.0, 0.0, 0.0), (40.0, 30.0, 5.0), "#D92626")
_PEG_A = ("peg", (7.0, 12.0, 5.0), (13.0, 18.0, 15.0), "#1A59D9")
_PEG_B = ("peg", (27.0, 14.0, 5.0), (33.0, 20.0, 15.0), "#1AB233")
_ASSEMBLED = (_BASE, _PEG_A, _PEG_B)

#: A cylinder's mesh sits inside its true surface by up to the chord sag, and
#: the coarsest backend (FreeCAD, 0.1 mm linear) sags the most.
_BOX_TOL = 0.15

#: Which backends can read the names a STEP gives its parts, and which its colours.
_SEES_NAMES = {"occt-xcaf": True, "occt": True, "freecad": True, "gmsh": True, "cadquery": False}
_SEES_COLOURS = {"occt-xcaf": True, "occt": True, "freecad": False, "gmsh": False, "cadquery": False}


def _xcaf_document():
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    return doc, XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()), XCAFDoc_DocumentTool.ColorTool_s(doc.Main())


def _named(label, name):
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    return label


def _paint(colors, label, rgb):
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.XCAFDoc import XCAFDoc_ColorType

    colors.SetColor(label, Quantity_Color(*rgb, Quantity_TOC_RGB), XCAFDoc_ColorType.XCAFDoc_ColorGen)


def _placed(x, y, z, turn_deg=0.0):
    """Move to (x, y, z) after turning ``turn_deg`` about Z."""
    import math

    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    move = gp_Trsf()
    move.SetTranslation(gp_Vec(x, y, z))
    if turn_deg:
        turn = gp_Trsf()
        turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(turn_deg))
        move.Multiply(turn)
    return TopLoc_Location(move)


def _write_document(doc, path: Path) -> Path:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Writer

    writer = STEPCAFControl_Writer()
    writer.Transfer(doc)
    assert writer.Write(str(path)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return path


def _write_assembly_step(path: Path) -> Path:
    """Base, peg, and a turned sub-assembly holding the peg again (see _ASSEMBLED)."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.TopLoc import TopLoc_Location

    doc, tool, colors = _xcaf_document()
    base = _named(tool.AddShape(BRepPrimAPI_MakeBox(40.0, 30.0, 5.0).Shape(), False), "base")
    _paint(colors, base, (0.85, 0.15, 0.15))
    peg = _named(tool.AddShape(BRepPrimAPI_MakeCylinder(3.0, 10.0).Shape(), False), "peg")
    _paint(colors, peg, (0.10, 0.35, 0.85))
    mount = _named(tool.NewShape(), "peg_mount")
    _paint(colors, tool.AddComponent(mount, peg, _placed(2.0, 0.0, 0.0)), (0.10, 0.70, 0.20))
    assembly = _named(tool.NewShape(), "bracket_assembly")
    tool.AddComponent(assembly, base, TopLoc_Location())
    tool.AddComponent(assembly, peg, _placed(10.0, 15.0, 5.0))
    tool.AddComponent(assembly, mount, _placed(30.0, 15.0, 5.0, 90.0))
    tool.UpdateAssemblies()
    return _write_document(doc, path)


def _write_solid_with_surfaces_step(path: Path) -> Path:
    """One named part: a box, plus a loose face beside it, as CAD exports carry."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    plane = gp_Pln(gp_Pnt(0.0, 0.0, 20.0), gp_Dir(0.0, 0.0, 1.0))
    builder.Add(compound, BRepBuilderAPI_MakeFace(plane, 0.0, 10.0, 0.0, 10.0).Face())
    doc, tool, _colors = _xcaf_document()
    _named(tool.AddShape(compound, False), "lid")
    return _write_document(doc, path)


def _write_parts_step(path: Path, names: list[str], *, rgb=None) -> Path:
    """Side-by-side boxes, one free part per name, optionally all one colour."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    doc, tool, colors = _xcaf_document()
    for i, name in enumerate(names):
        label = _named(tool.AddShape(BRepPrimAPI_MakeBox(gp_Pnt(20.0 * i, 0, 0), 10.0, 10.0, 10.0).Shape(), False), name)
        if rgb:
            _paint(colors, label, rgb)
    return _write_document(doc, path)


def _write_two_solid_compound_step(path: Path) -> Path:
    """Two touching boxes in ONE compound: several solids, no assembly at all."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(10.0, 0.0, 0.0), 10.0, 10.0, 10.0).Shape())
    writer = STEPControl_Writer()
    writer.Transfer(compound, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return path


# ---------------------------------------------------------------------------
# Reading the results back without Kiln's own readers
# ---------------------------------------------------------------------------


def _stl_triangles(path: str) -> list[tuple[tuple[float, float, float], ...]]:
    import struct

    data = Path(path).read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count, f"{path} is not a binary STL"
    out = []
    for i in range(count):
        v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
        out.append((v[0:3], v[3:6], v[6:9]))
    return out


def _closed(triangles) -> bool:
    """Every edge shared by exactly two triangles, corners matched exactly."""
    from collections import Counter

    ids: dict[tuple[float, float, float], int] = {}
    edges: Counter[tuple[int, int]] = Counter()
    for tri in triangles:
        corner = [ids.setdefault(tuple(v), len(ids)) for v in tri]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edges[tuple(sorted((corner[a], corner[b])))] += 1
    return bool(edges) and all(n == 2 for n in edges.values())


def _box(triangles) -> tuple[tuple[float, ...], tuple[float, ...]]:
    pts = [v for tri in triangles for v in tri]
    return (
        tuple(min(p[i] for p in pts) for i in range(3)),
        tuple(max(p[i] for p in pts) for i in range(3)),
    )


def _3mf_objects(path: str) -> list[tuple[str, list]]:
    """(name, triangles) per object, read straight from the 3MF's XML."""
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
    objects = []
    for obj in root.iterfind(".//m:object", ns):
        verts = [
            (float(v.get("x")), float(v.get("y")), float(v.get("z")))
            for v in obj.iterfind(".//m:vertex", ns)
        ]
        tris = [
            (verts[int(t.get("v1"))], verts[int(t.get("v2"))], verts[int(t.get("v3"))])
            for t in obj.iterfind(".//m:triangle", ns)
        ]
        objects.append((obj.get("name"), tris))
    return objects


def _3mf_colours(path: str) -> dict[str, str]:
    """{object name: display colour}, read straight from the 3MF's XML."""
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
    bases = [b.get("displaycolor") for b in root.iterfind(".//m:basematerials/m:base", ns)]
    return {
        obj.get("name"): bases[int(obj.get("pindex"))]
        for obj in root.iterfind(".//m:object", ns)
        if obj.get("pid") is not None
    }


def _assert_assembled(parts, *, sees_names: bool, colors=None) -> None:
    """Three closed parts, each where the assembly put it, named and coloured when readable.

    ``parts`` is ``(name, triangles)`` per part; ``colors``, when given, is
    the reported colour per part in the same order.
    """
    assert len(parts) == 3, [name for name, _ in parts]
    names = [name for name, _ in parts]
    assert len(set(names)) == 3, f"every part needs its own name: {names}"

    reported = colors if colors is not None else [None] * len(parts)
    expected = sorted(_ASSEMBLED, key=lambda e: e[1])
    got = sorted(
        ((name, *_box(tris), color) for (name, tris), color in zip(parts, reported, strict=True)),
        key=lambda g: g[1],
    )
    for (want_name, want_lo, want_hi, want_color), (name, lo, hi, color) in zip(expected, got, strict=True):
        assert lo == pytest.approx(want_lo, abs=_BOX_TOL), (name, lo, want_lo)
        assert hi == pytest.approx(want_hi, abs=_BOX_TOL), (name, hi, want_hi)
        if sees_names:
            assert name.startswith(want_name), (name, want_name)
        if colors is not None:
            assert color == want_color, (name, color, want_color)
    for name, tris in parts:
        assert _closed(tris), f"part {name!r} is not a closed mesh on its own"


# ---------------------------------------------------------------------------
# Picking one backend for real
# ---------------------------------------------------------------------------


def _only(monkeypatch, backend: str) -> None:
    """Make *backend* the one that runs, or skip saying why it cannot."""
    installed = "occt" if backend == "occt-xcaf" else backend
    if not _HAS[installed]:
        pytest.skip(f"{installed} is not installed on this machine")
    monkeypatch.setattr(step_import, "_ocp_available", _REAL_OCP_AVAILABLE if installed == "occt" else lambda: False)
    if installed != "freecad":
        monkeypatch.setattr(step_import, "_find_freecad_cmd", lambda: None)
    if installed != "gmsh":
        monkeypatch.setattr(step_import, "_find_gmsh_cmd", lambda: None)
    if installed != "cadquery":
        monkeypatch.setattr(step_import, "_cadquery_available", lambda: False)


@pytest.fixture
def assembly(tmp_path) -> Path:
    if not _HAS["occt"]:
        pytest.skip("the OCCT kernel is needed to author the assembly STEP these tests read")
    return _write_assembly_step(tmp_path / "bracket.step")


# ---------------------------------------------------------------------------
# The parts survive
# ---------------------------------------------------------------------------


class TestAssemblyKeepsItsParts:
    """auto: one 3MF object per solid; stl + split: one STL per solid."""

    @pytest.mark.parametrize("backend", ["occt-xcaf", "freecad", "gmsh", "cadquery"])
    def test_auto_writes_one_named_object_per_part(self, monkeypatch, assembly, tmp_path, backend):
        _only(monkeypatch, backend)
        result = step_import.convert_step(str(assembly), str(tmp_path / "out"), output_format="auto")

        assert result.output_format == "3mf"
        assert result.body_count == 3
        assert len(result.part_names) == 3
        assert not any("not a closed surface" in w for w in result.warnings), result.warnings
        objects = _3mf_objects(result.output_path)
        assert [name for name, _ in objects] == result.part_names
        colors = result.part_colors if _SEES_COLOURS[backend] else None
        _assert_assembled(objects, sees_names=_SEES_NAMES[backend], colors=colors)
        if colors is not None:
            assert _3mf_colours(result.output_path) == dict(zip(result.part_names, colors, strict=True))

    @pytest.mark.parametrize("backend", ["occt", "freecad", "gmsh", "cadquery"])
    def test_stl_split_writes_one_closed_file_per_part(self, monkeypatch, assembly, tmp_path, backend):
        _only(monkeypatch, backend)
        result = step_import.convert_step_to_stl(str(assembly), str(tmp_path / "out"), merge_bodies=False)

        assert result.output_format == "stl"
        assert result.body_count == 3
        assert len(result.output_paths) == 3
        assert len(result.part_names) == 3
        parts = list(zip(result.part_names, map(_stl_triangles, result.output_paths), strict=True))
        colors = result.part_colors if _SEES_COLOURS[backend] else None
        _assert_assembled(parts, sees_names=_SEES_NAMES[backend], colors=colors)


class TestMergeIsAnExplicitChoice:
    """merge_bodies=True fuses into one STL; a fused assembly is not a fault."""

    def test_merge_fuses_every_part_into_one_stl(self, monkeypatch, assembly, tmp_path):
        _only(monkeypatch, "occt-xcaf")
        result = step_import.convert_step(str(assembly), str(tmp_path / "out"), merge_bodies=True)

        assert result.output_format == "stl"
        assert len(result.output_paths) == 1
        assert result.body_count == 3
        tris = _stl_triangles(result.output_path)
        assert _box(tris)[0] == pytest.approx((0.0, 0.0, 0.0), abs=_BOX_TOL)
        assert _box(tris)[1] == pytest.approx((40.0, 30.0, 15.0), abs=_BOX_TOL)

    def test_touching_solids_fused_are_not_called_a_conversion_fault(self, monkeypatch, tmp_path):
        if not _HAS["occt"]:
            pytest.skip("the OCCT kernel is needed to author the STEP this test reads")
        step = _write_two_solid_compound_step(tmp_path / "two_boxes.step")
        _only(monkeypatch, "occt")
        result = step_import.convert_step_to_stl(str(step), str(tmp_path / "out"), merge_bodies=True)

        assert result.body_count == 2
        # Two boxes sharing a face share its edges once fused, so the fused
        # mesh is not one closed surface.  Each box is, which is what a
        # conversion owes: saying otherwise blames Kiln's converter for the
        # assembly being an assembly.
        assert not any("not a closed surface" in w for w in result.warnings), result.warnings
        assert _box(_stl_triangles(result.output_path))[1] == pytest.approx((20.0, 10.0, 10.0), abs=1e-3)

    def test_merge_of_one_coloured_part_is_still_an_stl(self, monkeypatch, tmp_path):
        if not _HAS["occt"]:
            pytest.skip("the OCCT kernel is needed to author the STEP this test reads")
        step = _write_parts_step(tmp_path / "plate.step", ["plate"], rgb=(0.85, 0.15, 0.15))
        _only(monkeypatch, "occt-xcaf")
        result = step_import.convert_step(str(step), str(tmp_path / "out"), merge_bodies=True)

        assert result.output_format == "stl"
        assert result.output_path.endswith(".stl")

    def test_merge_with_3mf_is_refused_as_contradictory(self, assembly, tmp_path):
        with pytest.raises(ValueError, match="merge_bodies"):
            step_import.convert_step(str(assembly), str(tmp_path), merge_bodies=True, output_format="3mf")


class TestWhatAPartCarries:
    """Surfaces beside a solid, and names no file system will take whole."""

    @pytest.mark.parametrize("backend", ["occt", "freecad"])
    def test_surfaces_beside_a_solid_are_kept_and_not_a_fault(self, monkeypatch, tmp_path, backend):
        if not _HAS["occt"]:
            pytest.skip("the OCCT kernel is needed to author the STEP this test reads")
        step = _write_solid_with_surfaces_step(tmp_path / "lid.step")
        _only(monkeypatch, backend)
        result = step_import.convert_step_to_stl(str(step), str(tmp_path / "out"), merge_bodies=False)

        assert result.body_count == 2
        assert any(name.endswith("surfaces") for name in result.part_names), result.part_names
        # The face was never a solid, so nothing promised it closed.
        assert not any("not a closed surface" in w for w in result.warnings), result.warnings
        boxes = sorted(_box(_stl_triangles(p)) for p in result.output_paths)
        assert boxes[0][1] == pytest.approx((10.0, 10.0, 10.0), abs=1e-3)
        assert boxes[1][0][2] == pytest.approx(20.0, abs=1e-3)

    def test_a_name_too_long_for_a_file_keeps_its_part(self, monkeypatch, tmp_path):
        if not _HAS["occt"]:
            pytest.skip("the OCCT kernel is needed to author the STEP this test reads")
        names = ["bracket " * 40, "bracket " * 40 + "rev b"]
        step = _write_parts_step(tmp_path / "long.step", names)
        _only(monkeypatch, "occt")
        result = step_import.convert_step_to_stl(str(step), str(tmp_path / "out"), merge_bodies=False)

        assert result.body_count == 2
        assert [n.strip() for n in result.part_names] == [n.strip() for n in names]
        assert all(len(Path(p).name.encode()) <= 255 for p in result.output_paths)


class TestNothingToMesh:
    """A file with no surfaces is refused in words, on every backend."""

    @pytest.mark.parametrize("backend", ["occt", "freecad", "gmsh"])
    def test_an_empty_assembly_is_refused_plainly(self, monkeypatch, tmp_path, backend):
        if not _HAS["occt"]:
            pytest.skip("the OCCT kernel is needed to author the STEP this test reads")
        doc, tool, _colors = _xcaf_document()
        _named(tool.NewShape(), "nothing_here")
        tool.UpdateAssemblies()
        step = _write_document(doc, tmp_path / "empty.step")
        _only(monkeypatch, backend)

        with pytest.raises(step_import.StepImportError, match="gave no surfaces to mesh") as caught:
            step_import.convert_step_to_stl(str(step), str(tmp_path / "out"), merge_bodies=False)
        assert "kiln_step_work" not in str(caught.value), "no scratch paths in what a person reads"


class TestImportStepFileTool:
    """The door an agent knocks on, on whatever backend this machine prefers."""

    def test_tool_returns_the_parts(self, assembly, tmp_path):
        from unittest.mock import MagicMock

        from kiln.plugins.step_tools import plugin

        tools: dict[str, object] = {}
        mcp = MagicMock()
        mcp.tool = lambda **_kw: lambda fn: tools.setdefault(fn.__name__, fn)
        plugin.register(mcp)

        response = tools["import_step_file"](str(assembly), output_dir=str(tmp_path / "out"))

        assert response["status"] == "ok", response
        assert response["output_format"] == "3mf"
        assert response["body_count"] == 3
        assert len(set(response["part_names"])) == 3
        backend = response["conversion"]["backend"]
        colors = response["part_colors"] if _SEES_COLOURS.get(backend) else None
        _assert_assembled(
            _3mf_objects(response["output_path"]), sees_names=_SEES_NAMES.get(backend, False), colors=colors
        )
