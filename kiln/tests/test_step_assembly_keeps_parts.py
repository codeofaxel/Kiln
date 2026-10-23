"""A STEP holding several solids comes out as several parts, on every backend.

A CAD assembly is parts placed in positions: a base and two copies of one
peg.  Every converter has to hand that back as three meshes, each closed on
its own, each where the assembly put it, named the way the file names them,
whatever backend this machine happens to run.  The write plan (fuse into one
STL, keep separate STLs, or one 3MF) is made once from those parts, so a
backend that sees the parts can never be overruled into one anonymous mesh.

Covers:
- the colour-aware door and every converter in the fallback chain, each run
  for real where it is installed and skipped with its reason where not
- part count, distinct names, each part closed, each part's box where the
  assembly placed it
- ``merge_bodies=True`` fuses on request, and touching solids fused are not
  called a conversion fault
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

#: Where the assembly puts each part: (name, box min, box max) in mm.
_BASE = ("base", (0.0, 0.0, 0.0), (40.0, 30.0, 5.0))
_PEG_A = ("peg", (7.0, 12.0, 5.0), (13.0, 18.0, 15.0))
_PEG_B = ("peg", (27.0, 12.0, 5.0), (33.0, 18.0, 15.0))
_ASSEMBLED = (_BASE, _PEG_A, _PEG_B)

#: A cylinder's mesh sits inside its true surface by up to the chord sag, and
#: the coarsest backend (FreeCAD, 0.1 mm linear) sags the most.
_BOX_TOL = 0.15

#: Which backends can read the names a STEP gives its parts.  gmsh's reader
#: is asked for them too, but no machine here has had gmsh to prove it on.
_SEES_NAMES = {"occt-xcaf": True, "occt": True, "freecad": True, "gmsh": False, "cadquery": False}


def _write_assembly_step(path: Path) -> Path:
    """Base plate plus one peg placed twice, as a CAD tool writes an assembly."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    base = named(tool.AddShape(BRepPrimAPI_MakeBox(40.0, 30.0, 5.0).Shape(), False), "base")
    peg = named(tool.AddShape(BRepPrimAPI_MakeCylinder(3.0, 10.0).Shape(), False), "peg")
    assembly = named(tool.NewShape(), "bracket_assembly")
    tool.AddComponent(assembly, base, TopLoc_Location())
    for x in (10.0, 30.0):
        move = gp_Trsf()
        move.SetTranslation(gp_Vec(x, 15.0, 5.0))
        tool.AddComponent(assembly, peg, TopLoc_Location(move))
    tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.Transfer(doc)
    assert writer.Write(str(path)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return path


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


def _assert_assembled(parts: list[tuple[str, list]], *, sees_names: bool) -> None:
    """Three closed parts, each where the assembly put it, named when readable."""
    assert len(parts) == 3, [name for name, _ in parts]
    names = [name for name, _ in parts]
    assert len(set(names)) == 3, f"every part needs its own name: {names}"

    expected = sorted(_ASSEMBLED, key=lambda e: e[1])
    got = sorted(((name, *_box(tris)) for name, tris in parts), key=lambda g: g[1])
    for (want_name, want_lo, want_hi), (name, lo, hi) in zip(expected, got, strict=True):
        assert lo == pytest.approx(want_lo, abs=_BOX_TOL), (name, lo, want_lo)
        assert hi == pytest.approx(want_hi, abs=_BOX_TOL), (name, hi, want_hi)
        if sees_names:
            assert name.startswith(want_name), (name, want_name)
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
        _assert_assembled(_3mf_objects(result.output_path), sees_names=_SEES_NAMES[backend])

    @pytest.mark.parametrize("backend", ["occt", "freecad", "gmsh", "cadquery"])
    def test_stl_split_writes_one_closed_file_per_part(self, monkeypatch, assembly, tmp_path, backend):
        _only(monkeypatch, backend)
        result = step_import.convert_step_to_stl(str(assembly), str(tmp_path / "out"), merge_bodies=False)

        assert result.output_format == "stl"
        assert result.body_count == 3
        assert len(result.output_paths) == 3
        assert len(result.part_names) == 3
        parts = list(zip(result.part_names, map(_stl_triangles, result.output_paths), strict=True))
        _assert_assembled(parts, sees_names=_SEES_NAMES[backend])


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

    def test_merge_with_3mf_is_refused_as_contradictory(self, assembly, tmp_path):
        with pytest.raises(ValueError, match="merge_bodies"):
            step_import.convert_step(str(assembly), str(tmp_path), merge_bodies=True, output_format="3mf")


class TestImportStepFileTool:
    """The door an agent knocks on, on whatever backend this machine prefers."""

    def test_tool_returns_the_parts(self, assembly, tmp_path):
        from unittest.mock import MagicMock

        from kiln.plugins.step_tools import plugin

        tools: dict[str, object] = {}
        mcp = MagicMock()
        mcp.tool = lambda: lambda fn: tools.setdefault(fn.__name__, fn)
        plugin.register(mcp)

        response = tools["import_step_file"](str(assembly), output_dir=str(tmp_path / "out"))

        assert response["status"] == "ok", response
        assert response["output_format"] == "3mf"
        assert response["body_count"] == 3
        assert len(set(response["part_names"])) == 3
        backend = response["conversion"]["backend"]
        sees_names = _SEES_NAMES.get(backend, False)
        _assert_assembled(_3mf_objects(response["output_path"]), sees_names=sees_names)
