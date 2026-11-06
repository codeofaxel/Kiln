"""A project's support enforcers, blockers, modifiers and negative parts are
not the model.

A project saved by Bambu Studio or OrcaSlicer keeps those volumes as meshes
beside the part they act on, and says which is which in its settings.  They
shape the slice and are never laid down as plastic, and a person is free to
let one hang below the plate.  Kiln measured every mesh as the model, so a
real project (measured 2026-11-05: a support enforcer reaching 8 mm below the
plate under a part standing on it) was refused as "falls outside the
printer's bed", and the enforcer's box was judged for overhangs as though it
would print.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kiln.generation.validation import _placed_3mf_meshes
from kiln.printers.bed_fit import compute_3mf_geometry_bbox
from kiln.project_plates import printed_parts_only

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_FACES = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
          (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _box(low: tuple[float, float, float], high: tuple[float, float, float]) -> str:
    (x0, y0, z0), (x1, y1, z1) = low, high
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    vertices = "".join(f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in corners)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in _FACES)
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def _project(path: Path, other_kind: str | None, *, as_saved: bool = False) -> str:
    """One object at (100, 100): a 20 mm cube standing on the plate (part 1)
    and a box from 8 mm below the plate to 5 mm above it (part 2), which the
    project's settings call *other_kind* -- or do not mention at all.
    *as_saved* writes part 2 the way Bambu Studio saves a volume that never
    prints: an object of type ``other``."""
    root = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}" '
            f'xmlns:p="{_PRODUCTION}" requiredextensions="p">\n <resources>\n  <object id="3" type="model">\n'
            f'   <components>\n    <component p:path="/3D/Objects/object_1.model" objectid="1"/>\n'
            f'    <component p:path="/3D/Objects/object_1.model" objectid="2"/>\n   </components>\n  </object>\n'
            f' </resources>\n <build>\n  <item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>\n </build>\n'
            f"</model>\n")
    parts = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}">\n <resources>\n'
             f'  <object id="1" type="model">{_box((0, 0, 0), (20, 20, 20))}</object>\n'
             f'  <object id="2" type="{"other" if as_saved else "model"}">{_box((0, 0, -8), (30, 30, 5))}</object>\n'
             f" </resources>\n <build/>\n"
             f"</model>\n")
    second = "" if other_kind is None else f'    <part id="2" subtype="{other_kind}"/>\n'
    settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <object id="3">\n'
                '    <metadata key="name" value="bracket"/>\n    <part id="1" subtype="normal_part"/>\n'
                f"{second}  </object>\n</config>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if as_saved:
            relationship = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
            zf.writestr("[Content_Types].xml", (
                '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.'
                '3dmanufacturing-3dmodel+xml"/></Types>'))
            for name, target in (("_rels/.rels", "/3D/3dmodel.model"),
                                 ("3D/_rels/3dmodel.model.rels", "/3D/Objects/object_1.model")):
                zf.writestr(name, (
                    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                    f'package/2006/relationships"><Relationship Target="{target}" Id="rel-1" Type="{relationship}"/>'
                    "</Relationships>"))
        zf.writestr("3D/3dmodel.model", root)
        zf.writestr("3D/Objects/object_1.model", parts)
        zf.writestr("Metadata/model_settings.config", settings)
    return str(path)


@pytest.mark.parametrize("kind", ["support_enforcer", "support_blocker", "modifier_part", "negative_part"])
def test_a_volume_that_never_prints_is_not_measured_as_the_model(tmp_path: Path, kind: str) -> None:
    project = _project(tmp_path / "project.3mf", kind)

    assert compute_3mf_geometry_bbox(project) == {
        "x_min": 100.0, "x_max": 120.0, "y_min": 100.0, "y_max": 120.0, "z_min": 0.0, "z_max": 20.0,
    }
    errors: list[str] = []
    meshes = _placed_3mf_meshes(Path(project), errors)
    assert [placed.object_id for placed, *_ in meshes] == [1] and not errors


@pytest.mark.parametrize("kind", ["normal_part", None])
def test_a_second_part_that_prints_is_still_the_model(tmp_path: Path, kind: str | None) -> None:
    """Counted unless the project says otherwise: a part it calls plastic, and
    one it says nothing about."""
    project = _project(tmp_path / "project.3mf", kind)

    box = compute_3mf_geometry_bbox(project)
    assert box is not None and box["z_min"] == -8.0 and box["x_max"] == 130.0
    assert [placed.object_id for placed, *_ in _placed_3mf_meshes(Path(project), [])] == [1, 2]


def test_settings_that_cannot_be_read_count_every_part(tmp_path: Path) -> None:
    """The safe way round for a bed check: measure more, never less."""
    project = _project(tmp_path / "project.3mf", "support_enforcer")
    rewritten = tmp_path / "broken.3mf"
    with zipfile.ZipFile(project) as zin, zipfile.ZipFile(rewritten, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            zout.writestr(info, b"<config><object id=" if info.filename.endswith(".config") else body)

    box = compute_3mf_geometry_bbox(str(rewritten))
    assert box is not None and box["z_min"] == -8.0


# ---------------------------------------------------------------------------
# PrusaSlicer is handed the parts that print and no others
#
# Measured 2026-11-05 on PrusaSlicer 2.9.4 and a real plate of nine objects,
# one of which carries a support enforcer: "Found component with invalid
# object id", and the whole plate refused.  With that one volume left out the
# plate loads and slices.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["support_enforcer", "support_blocker", "modifier_part", "negative_part"])
def test_the_volumes_that_never_print_are_left_out_and_counted(tmp_path: Path, kind: str) -> None:
    project = _project(tmp_path / "project.3mf", kind, as_saved=True)

    only, left_out = printed_parts_only(project, out_dir=str(tmp_path / "out"))

    assert left_out == {kind: 1} and Path(only).name == "project.3mf"
    before, after = zipfile.ZipFile(project), zipfile.ZipFile(only)
    root = after.read("3D/3dmodel.model").decode()
    assert 'objectid="1"' in root and 'objectid="2"' not in root and root.count("<component ") == 1
    assert '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>' in root
    assert after.namelist() == before.namelist()
    for name in after.namelist():
        if name != "3D/3dmodel.model":
            assert after.read(name) == before.read(name), name


@pytest.mark.parametrize("kind", ["normal_part", None])
def test_a_project_whose_parts_all_print_is_handed_on_as_it_is(tmp_path: Path, kind: str | None) -> None:
    assert printed_parts_only(_project(tmp_path / "project.3mf", kind), out_dir=str(tmp_path / "out")) is None


def test_a_file_that_is_no_project_is_handed_on_as_it_is(tmp_path: Path) -> None:
    mesh = tmp_path / "part.stl"
    mesh.write_bytes(b"\0" * 84)
    broken = tmp_path / "broken.3mf"
    broken.write_bytes(b"not a zip")
    assert printed_parts_only(str(mesh)) is None and printed_parts_only(str(broken)) is None
    assert printed_parts_only(str(tmp_path / "missing.3mf")) is None


def test_an_object_that_is_nothing_but_such_a_volume_is_left_as_written(tmp_path: Path) -> None:
    """Nothing would be left of it: the file is handed on untouched."""
    project = _project(tmp_path / "project.3mf", "support_enforcer")
    rewritten = tmp_path / "only.3mf"
    with zipfile.ZipFile(project) as zin, zipfile.ZipFile(rewritten, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename).decode()
            if info.filename == "3D/3dmodel.model":
                body = body.replace('    <component p:path="/3D/Objects/object_1.model" objectid="1"/>\n', "")
            zout.writestr(info, body)
    assert printed_parts_only(str(rewritten), out_dir=str(tmp_path / "out")) is None


def _sliced_by(tmp_path: Path, monkeypatch, slicer_name: str, kind: str):
    import kiln.slicer as slicer

    handed: dict[str, str] = {}

    def fake(_slicer, input_abs, sliced_input, out_file, **_kwargs):
        handed.update(input=input_abs, sliced=sliced_input)
        return slicer.SliceResult(success=True, output_path=out_file, slicer="fake", message="Sliced it")

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(slicer, "find_slicer", lambda *a, **k: slicer.SlicerInfo(path="/bin/true", name=slicer_name))
    monkeypatch.setattr(slicer, "_slice_decided", fake)
    project = _project(tmp_path / "project.3mf", kind, as_saved=True)
    return project, handed, lambda: slicer.slice_file(project, output_dir=str(tmp_path / "out"))


def test_prusaslicer_is_handed_the_printed_parts_and_the_reply_says_what_was_left_out(tmp_path, monkeypatch) -> None:
    project, handed, run = _sliced_by(tmp_path, monkeypatch, "PrusaSlicer", "support_enforcer")

    result = run()

    assert handed["input"] != project and Path(handed["input"]).name == "project.3mf"
    assert 'objectid="2"' not in zipfile.ZipFile(handed["sliced"]).read("3D/3dmodel.model").decode()
    assert result.message == (
        "Sliced it. Left out, because PrusaSlicer reads only the parts of a project that print: 1 support enforcer."
    )


def test_a_slicer_that_reads_the_project_is_handed_all_of_it(tmp_path, monkeypatch) -> None:
    project, handed, run = _sliced_by(tmp_path, monkeypatch, "OrcaSlicer", "support_enforcer")

    result = run()

    assert handed["input"] == project and "Left out" not in result.message


def test_a_cut_prusaslicer_cannot_read_is_refused_not_dropped(tmp_path, monkeypatch) -> None:
    """Left out, the cut would not be made and the print would be another part."""
    import kiln.slicer as slicer

    _project_path, handed, run = _sliced_by(tmp_path, monkeypatch, "PrusaSlicer", "negative_part")

    with pytest.raises(slicer.SlicerError, match="cuts a shape out of a part"):
        run()
    assert not handed


_PRUSA = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"


@pytest.mark.skipif(not Path(_PRUSA).is_file(), reason="PrusaSlicer is not installed")
def test_prusaslicer_slices_a_project_that_carries_a_support_enforcer(tmp_path, monkeypatch) -> None:
    """The real slicer on the file as Bambu Studio saves it: it prints the
    20 mm cube, and nothing of the 30 mm volume beside it."""
    import re

    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    project = _project(tmp_path / "project.3mf", "support_enforcer", as_saved=True)

    result = slice_file(project, output_dir=str(tmp_path / "out"), profile=resolve_slicer_profile("bambu_a1"),
                        slicer_path=_PRUSA, timeout=600)

    assert result.success and "1 support enforcer" in result.message
    walls: list[float] = []
    feature, x = "", 0.0
    for line in Path(result.output_path).read_text(errors="replace").splitlines():
        if line.startswith(";TYPE:"):
            feature = line[6:]
        elif line.startswith("G1"):
            words = dict(re.findall(r"([XE])(-?\d*\.?\d+)", line.split(";")[0]))
            nx = float(words.get("X", x))
            if feature == "External perimeter" and float(words.get("E", 0)) > 0:
                walls += [x, nx]
            x = nx
    assert max(walls) - min(walls) == pytest.approx(20.0, abs=1.0)

