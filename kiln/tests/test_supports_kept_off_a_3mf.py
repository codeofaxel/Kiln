"""A 3MF gets the same support blockers a mesh gets, in its own copy.

A slice that turns supports on for one overhang keeps them off the faces the
verdict said print on their own, by painting those faces as support blockers
in the file the slicer is handed.  That reached STL and OBJ inputs only.
Measured 2026-11-05 on a threaded rod beside a shelf: sliced from an STL, no
support in the thread; the same part as a 3MF (a plain one, a PrusaSlicer
project, a Bambu Studio project, a Kiln colour-painted file), 43,000 to 50,000
support samples packed into its grooves on every slicer.

A 3MF is somebody's file: colours painted on its faces, several objects, a
project's settings.  So the copy handed to the slicer is that file with two
attributes added to the triangles kept off, and nothing else moved.  These
tests write 3MFs by hand in the layouts slicers write, then read the copy.
The last ones slice, and skip where no slicer is installed.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh", reason="needs trimesh to build the parts")
np = pytest.importorskip("numpy")

from kiln.slicer_supports import SliceSupports, decide_supports, kept_off_input  # noqa: E402

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships'
          '+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
          "</Types>")
_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
         'relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/'
         '3dmanufacturing/2013/01/3dmodel"/></Relationships>')
_IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"


@pytest.fixture(autouse=True)
def _public_floor(monkeypatch):
    """Judge on public Kiln's own 45-degree floor, whatever is installed."""
    import kiln.design_intelligence as design_intelligence

    monkeypatch.setattr(design_intelligence, "load_pro_overlay_or_empty", lambda kind: {})


def _mesh_xml(mesh, attrs: dict[int, str] | None = None) -> str:
    """One ``<mesh>``; *attrs* adds text to a triangle's element by its place."""
    attrs = attrs or {}
    vertices = "".join(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in mesh.vertices.tolist())
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"{attrs.get(i, "")}/>' for i, (a, b, c) in enumerate(mesh.faces.tolist())
    )
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def _model(mesh, *, attrs=None, items=(_IDENTITY,), extra_ns: str = "") -> str:
    build = "".join(f'<item objectid="1" transform="{t}"/>' for t in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}"{extra_ns}>'
            f'<resources><object id="1" type="model">{_mesh_xml(mesh, attrs)}</object></resources>'
            f"<build>{build}</build></model>")


def _archive(path: Path, members: dict[str, str | bytes]) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _TYPES)
        zf.writestr("_rels/.rels", _RELS)
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def _box(at=(100.0, 100.0, 5.0)):
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    box.apply_translation(at)
    return box


def _judged(path: str) -> list:
    """The triangles in the order the verdict indexes them."""
    from kiln.printability import _parse_mesh, _prepare_parsed

    facets, vertices = _parse_mesh(path)
    return _prepare_parsed(list(facets), vertices)[0]


def _key(triangle) -> tuple:
    return tuple(sorted(tuple(round(float(c), 6) for c in v) for v in triangle))


def _keeping(path: str, wanted) -> SliceSupports:
    """A decision that keeps supports off the judged triangles *wanted* picks."""
    kept = tuple(i for i, tri in enumerate(_judged(path)) if wanted(tri))
    assert kept, "the test picked no triangle"
    return SliceSupports(enabled=True, decided_by="analysis", reason="test", kept_off=kept)


def _rows(model: str) -> list[str]:
    return re.findall(r"<triangle\b[^>]*/>", model)


def _painted(row: str) -> bool:
    return 'slic3rpe:custom_supports="8"' in row and 'paint_supports="8"' in row


def test_the_copy_is_the_same_file_with_the_blockers_added(tmp_path: Path) -> None:
    box = _box()
    colour = {i: ' paint_color="4"' for i in range(6)}
    members = {
        "3D/3dmodel.model": _model(box, attrs=colour),
        "Metadata/model_settings.config": "<config><object id=\"1\"/></config>",
        "Metadata/plate_1.png": bytes(range(256)),
    }
    source = _archive(tmp_path / "part.3mf", members)
    top = float(box.bounds[1][2])
    decision = _keeping(source, lambda tri: all(abs(v[2] - top) < 1e-9 for v in tri))  # the two top faces

    path, painted = kept_off_input(source, decision, str(tmp_path / "out"), bed_centre=None) if (
        tmp_path / "out").mkdir() is None else (None, 0)

    assert painted == 2 and path != source and os.path.basename(path) == "part.3mf"
    before, after = zipfile.ZipFile(source), zipfile.ZipFile(path)
    assert after.namelist() == before.namelist()
    for name in before.namelist():
        if name != "3D/3dmodel.model":
            assert after.read(name) == before.read(name), name
    old, new = _rows(before.read("3D/3dmodel.model").decode()), _rows(after.read("3D/3dmodel.model").decode())
    assert len(new) == len(old) == len(box.faces)
    # Each triangle is its old self, with the two attributes added or not at all.
    for was, now in zip(old, new, strict=True):
        assert now in (was, was[:-2] + ' slic3rpe:custom_supports="8" paint_supports="8"/>')
    assert [i for i, row in enumerate(new) if 'paint_color="4"' in row] == list(range(6))
    # The painted triangles are the ones kept off, by where they are.
    root = ET.fromstring(after.read("3D/3dmodel.model"))  # and the copy is well-formed
    points = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in root.iter(f"{{{_CORE}}}vertex")]
    for row in (r for r in new if _painted(r)):
        corners = [points[int(n)] for n in re.findall(r'v[123]="(\d+)"', row)]
        assert all(abs(z - top) < 1e-9 for _x, _y, z in corners)
    assert 'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"' in after.read("3D/3dmodel.model").decode()


def test_support_paint_the_person_put_there_is_left_alone(tmp_path: Path) -> None:
    box = _box()
    theirs = {0: ' paint_supports="4"', 1: ' slic3rpe:custom_supports="4"'}
    extra_ns = ' xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"'
    source = _archive(tmp_path / "part.3mf", {"3D/3dmodel.model": _model(box, attrs=theirs, extra_ns=extra_ns)})
    decision = _keeping(source, lambda tri: True)  # every face

    (tmp_path / "out").mkdir()
    path, painted = kept_off_input(source, decision, str(tmp_path / "out"), bed_centre=None)

    model = zipfile.ZipFile(path).read("3D/3dmodel.model").decode()
    rows = _rows(model)
    assert painted == len(box.faces) - 2
    assert rows[0].endswith(' paint_supports="4"/>') and rows[1].endswith(' slic3rpe:custom_supports="4"/>')
    assert all(_painted(row) for row in rows[2:])
    assert model.count("xmlns:slic3rpe=") == 1


def test_an_object_placed_twice_is_painted_only_where_both_copies_agree(tmp_path: Path) -> None:
    """The second copy stands 100 mm along X.  A blocker under a face one
    copy needs supported would take the support from it."""
    box = _box()
    source = _archive(tmp_path / "two.3mf", {
        "3D/3dmodel.model": _model(box, items=(_IDENTITY, "1 0 0 0 1 0 0 0 1 100 0 0")),
    })
    assert len(_judged(source)) == 2 * len(box.faces)
    top = float(box.bounds[1][2])
    on_top = lambda tri: all(abs(v[2] - top) < 1e-9 for v in tri)  # noqa: E731
    first = lambda tri: max(v[0] for v in tri) < 150.0  # noqa: E731
    (tmp_path / "out").mkdir()

    both = kept_off_input(source, _keeping(source, on_top), str(tmp_path / "out"), bed_centre=None)
    assert both is not None and both[1] == 2  # two triangles in the file, kept off in both copies

    one = kept_off_input(source, _keeping(source, lambda tri: on_top(tri) and first(tri)),
                         str(tmp_path / "out"), bed_centre=None)
    assert one is None


def test_a_mesh_kept_in_its_own_model_part_is_painted_there(tmp_path: Path) -> None:
    """The layout Bambu Studio writes: the plate's model names an object in
    another part of the archive, and the mesh is in that part."""
    box = _box()
    plate = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}" '
             f'xmlns:p="{_PRODUCTION}"><resources><object id="2" type="model"><components>'
             f'<component p:path="/3D/Objects/object_1.model" objectid="1" transform="{_IDENTITY}"/>'
             f'</components></object></resources><build><item objectid="2" transform="{_IDENTITY}"/></build></model>')
    source = _archive(tmp_path / "project.3mf", {
        "3D/3dmodel.model": plate,
        "3D/Objects/object_1.model": _model(box, items=()),
        "Metadata/project_settings.config": '{"layer_height": "0.2"}',
    })
    decision = _keeping(source, lambda tri: True)
    (tmp_path / "out").mkdir()

    path, painted = kept_off_input(source, decision, str(tmp_path / "out"), bed_centre=None)

    after = zipfile.ZipFile(path)
    assert painted == len(box.faces)
    assert after.read("3D/3dmodel.model").decode() == plate
    assert after.read("Metadata/project_settings.config") == b'{"layer_height": "0.2"}'
    assert all(_painted(row) for row in _rows(after.read("3D/Objects/object_1.model").decode()))


def test_a_model_written_with_a_prefix_is_handed_over_as_it_is(tmp_path: Path) -> None:
    """The copy is made by adding to the file's own text; a model whose
    elements carry a prefix is one this does not rewrite, so it is left."""
    box = _box()
    plain = _model(box)
    prefixed = re.sub(r"<(/?)(model|resources|object|mesh|vertices|vertex|triangles|triangle|build|item)\b",
                      r"<\1m:\2", plain).replace(f'xmlns="{_CORE}"', f'xmlns:m="{_CORE}"')
    source = _archive(tmp_path / "prefixed.3mf", {"3D/3dmodel.model": prefixed})
    decision = _keeping(source, lambda tri: True)  # Kiln reads the file; it just does not rewrite it
    (tmp_path / "out").mkdir()

    assert kept_off_input(source, decision, str(tmp_path / "out"), bed_centre=None) is None
    assert not list((tmp_path / "out").iterdir())


# ---------------------------------------------------------------------------
# What the slicer lays down
# ---------------------------------------------------------------------------


def _slicers() -> dict[str, str]:
    found = {}
    for name, commands, mac in (
        ("PrusaSlicer", ("prusa-slicer", "PrusaSlicer", "prusaslicer"),
         "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"),
        ("Bambu Studio", ("bambu-studio",), "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        ("OrcaSlicer", ("orca-slicer",), "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
    ):
        exe = next((shutil.which(c) for c in commands if shutil.which(c)), None) or (
            mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None)
        if exe:
            found[name] = exe
    return found


_SLICERS = _slicers()
#: A 10 mm post through a wide shelf, which needs supports, with a small lip
#: above the shelf, which prints on its own: (radius, height) corners of the
#: profile.  Support for the lip would stand on the shelf's top.
_LIP_REACH = 1.5
_LIP_RISE = _LIP_REACH / math.tan(math.radians(60.0))
_SHELF_TOP, _LIP_FROM = 11.0, 16.0
_POST_AND_CAP = [(0, 0), (5, 0), (5, 8), (20, 8), (20, _SHELF_TOP), (5, _SHELF_TOP), (5, _LIP_FROM),
                 (5 + _LIP_REACH, _LIP_FROM + _LIP_RISE), (5 + _LIP_REACH, 18), (5, 18), (5, 23), (0, 23)]


#: The same post and lip with no shelf: nothing on it needs supports.
_POST_AND_LIP = [(0, 0), (5, 0), (5, _LIP_FROM), (5 + _LIP_REACH, _LIP_FROM + _LIP_RISE), (5 + _LIP_REACH, 18),
                 (5, 18), (5, 23), (0, 23)]


def _support_samples(gcode: str) -> np.ndarray:
    """Every support extrusion, sampled every 0.1 mm: rows of (x, y, z)."""
    from kiln.gcode import feature_label

    samples, x, y, z, support = [], 0.0, 0.0, 0.0, False
    number = re.compile(r"([XYE])(-?\d*\.?\d+)")
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                label = feature_label(line)
                if label is not None:
                    support = label.lower().startswith("support")
                elif line.startswith(";Z:"):
                    z = float(line[3:])
                elif line.startswith("; Z_HEIGHT:"):
                    z = float(line.split(":")[1])
            elif line.startswith("G1"):
                words = dict(number.findall(line.split(";")[0]))
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                if support and float(words.get("E", 0)) > 0 and (nx, ny) != (x, y):
                    n = max(2, int(math.hypot(nx - x, ny - y) / 0.1) + 1)
                    t = np.linspace(0.0, 1.0, n)
                    samples.append(np.stack([x + (nx - x) * t, y + (ny - y) * t, np.full(n, z)], axis=1))
                x, y = nx, ny
    return np.concatenate(samples) if samples else np.zeros((0, 3))


def _sliced_through_kiln(source: str, slicer: str, centre, tmp_path, monkeypatch):
    """Slice *source* through Kiln's own slicing step and judge the file:
    supports under the shelf, none under the lip.  *centre* is where the
    post stands on the bed."""
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    assert decide_supports(source, material="pla").kept_off, "the verdict kept no face off: the lip is not judged"

    result = slice_file(source, output_dir=str(tmp_path / "out"), profile=resolve_slicer_profile("bambu_a1"),
                        slicer_path=_SLICERS[slicer], timeout=900)

    samples = _support_samples(result.output_path)
    radius = np.hypot(samples[:, 0] - centre[0], samples[:, 1] - centre[1])
    # Between the shelf's top and the lip: only support for the lip stands there.
    under_lip = int(((samples[:, 2] > _SHELF_TOP) & (samples[:, 2] < _LIP_FROM + _LIP_RISE)).sum())
    under_cap = int(((radius > 8.0) & (radius < 20.0) & (samples[:, 2] < 8.0)).sum())
    assert result.supports.enabled and result.supports.kept_off_painted > 0, result.supports.note
    assert under_cap > 1000, f"{slicer}: almost no support under the shelf ({under_cap} samples)"
    assert under_lip == 0, f"{slicer}: {under_lip} support samples under the lip that prints on its own"
    assert "parts that print on their own" in result.supports.note
    return result


@pytest.mark.skipif(not _SLICERS, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_a_3mf_is_sliced_with_supports_under_the_shelf_and_none_under_the_lip(tmp_path, monkeypatch, slicer):
    centre = (128.0, 128.0)
    part = trimesh.creation.revolve(np.asarray(_POST_AND_CAP, dtype=float), sections=96)
    part.apply_translation((centre[0], centre[1], 0.0))
    source = _archive(tmp_path / "part.3mf", {"3D/3dmodel.model": _model(part)})

    _sliced_through_kiln(source, slicer, centre, tmp_path, monkeypatch)


@pytest.mark.skipif("Bambu Studio" not in _SLICERS, reason="Bambu Studio is not installed")
@pytest.mark.parametrize("slicer", [name for name in ("Bambu Studio", "PrusaSlicer") if name in _SLICERS] or ["none"])
def test_a_project_bambu_studio_saved_is_sliced_the_same_way(tmp_path, monkeypatch, slicer):
    """The file a Bambu user has: a project Bambu Studio wrote itself, with a
    printer chosen, its mesh in a part of its own and its settings beside it,
    supports switched on.  The supports are the project's own, and the lip
    Kiln measured as printing on its own is still kept clear of them."""
    import subprocess

    from kiln.slicer_orca import ini_to_settings, write_orca_presets
    from kiln.slicer_profiles import resolve_slicer_profile

    stl = tmp_path / "part.stl"
    trimesh.creation.revolve(np.asarray(_POST_AND_CAP, dtype=float), sections=96).export(str(stl))
    theirs = {**ini_to_settings(resolve_slicer_profile("bambu_a1")), "support_material": "1"}
    presets = write_orca_presets(theirs, str(tmp_path), name="a1")
    project = tmp_path / "project.3mf"
    saved = subprocess.run(
        [_SLICERS["Bambu Studio"], "--load-settings", f"{presets.machine_path};{presets.process_path}",
         "--load-filaments", ";".join(presets.filament_paths), "--arrange", "1", "--export-3mf", str(project), str(stl)],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
    )
    assert project.is_file(), f"Bambu Studio saved no project (exit {saved.returncode})"
    names = zipfile.ZipFile(project).namelist()
    assert "Metadata/project_settings.config" in names and any(n.startswith("3D/Objects/") for n in names), names
    corners = np.array([v for tri in _judged(str(project)) for v in tri])
    centre = ((corners[:, 0].min() + corners[:, 0].max()) / 2.0, (corners[:, 1].min() + corners[:, 1].max()) / 2.0)

    from kiln.project_plates import saved_supports

    assert (saved_supports(str(project)) or {}).get("support_material") == "1", "the project did not save supports on"

    result = _sliced_through_kiln(str(project), slicer, centre, tmp_path, monkeypatch)

    assert result.supports.decided_by == "project"
    assert result.supports.note == "supports on, as the project saved them; none on the parts that print on their own"



@pytest.mark.skipif("Bambu Studio" not in _SLICERS, reason="Bambu Studio is not installed")
@pytest.mark.parametrize("slicer", [name for name in ("Bambu Studio", "PrusaSlicer") if name in _SLICERS] or ["none"])
def test_a_project_with_supports_on_and_nothing_that_needs_them_is_kept_clear(tmp_path, monkeypatch, slicer):
    """A project Bambu Studio saved with supports on, holding a post and a
    lip that prints on its own and nothing else.  With supports on, a slicer
    left alone stands support under the lip; through Kiln none is laid, and
    the reply says whose doing that is."""
    import subprocess

    import kiln.slicer as slicer_module
    from kiln import monitor_twin
    from kiln.project_plates import saved_supports
    from kiln.slicer import slice_file
    from kiln.slicer_orca import ini_to_settings, write_orca_presets
    from kiln.slicer_profiles import resolve_slicer_profile

    stl = tmp_path / "part.stl"
    trimesh.creation.revolve(np.asarray(_POST_AND_LIP, dtype=float), sections=96).export(str(stl))
    theirs = {**ini_to_settings(resolve_slicer_profile("bambu_a1")), "support_material": "1"}
    presets = write_orca_presets(theirs, str(tmp_path), name="a1")
    project = tmp_path / "project.3mf"
    subprocess.run(
        [_SLICERS["Bambu Studio"], "--load-settings", f"{presets.machine_path};{presets.process_path}",
         "--load-filaments", ";".join(presets.filament_paths), "--arrange", "1", "--export-3mf", str(project), str(stl)],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
    )
    assert project.is_file() and (saved_supports(str(project)) or {}).get("support_material") == "1"
    assert not decide_supports(str(project), material="pla").enabled, "the part is read as needing supports"
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")

    def sliced(out: str):
        return slice_file(str(project), output_dir=str(tmp_path / out), profile=resolve_slicer_profile("bambu_a1"),
                          slicer_path=_SLICERS[slicer], timeout=900)

    result = sliced("kept_clear")

    assert len(_support_samples(result.output_path)) == 0
    assert result.supports.decided_by == "project" and result.supports.kept_off_painted > 0
    assert result.supports.note == (
        "no supports placed: the project has them on, and Kiln kept them off the faces it measured as printing "
        "on their own"
    )

    # The same project with nothing kept clear: the slicer stands support under the lip.
    monkeypatch.setattr(slicer_module, "kept_off_input", lambda *_a, **_k: None)
    assert len(_support_samples(sliced("as_saved").output_path)) > 500
