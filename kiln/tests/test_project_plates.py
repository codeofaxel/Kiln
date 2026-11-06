"""A project of several plates is sliced one plate at a time, the plate named.

A project saved by Bambu Studio or OrcaSlicer can hold several plates: one
build, each plate's objects placed where that plate stands in the project's
layout.  Kiln read it as one plate, so a real two-plate project (measured
2026-11-05: 15 objects, 9 on plate 1 and 6 on plate 2, a 256 mm bed) was a
part 563 mm wide and every slicing door refused it as "larger than the bed:
rescale the model or split it into parts that fit".

Now the plates are read (:mod:`kiln.project_plates`), a project with several
is never sliced on a guess, and the plate named is written out as a project
of its own that every check and every slicer meets as an ordinary one-plate
file.  Measured on that real project: Bambu Studio slices Kiln's file for a
plate to the same layers and footprint as its own slice of that plate.

These tests write projects by hand in the layout the slicers write.  The last
ones slice, and skip where no slicer is installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from kiln.project_plates import (
    NO_SUCH_PLATE,
    PLATE_NOT_CHOSEN,
    PlateChoiceError,
    one_plate,
    read_plates,
)

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships'
          '+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
          "</Types>")
_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
         'relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/'
         '3dmanufacturing/2013/01/3dmodel"/></Relationships>')
#: A plate and a fifth: how far apart a project lays out the plates of a 256 mm bed.
_STRIDE = 256.0 * 1.2


def _box_mesh(size: float) -> str:
    """A cube standing on the plate, its corner at the object's origin."""
    s = f"{size:g}"
    corners = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in corners)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def _project(path: Path, placed: list[tuple[int, int, float, float]], *, plates: int | None = None,
             bed: float | None = 256.0, size: float = 20.0) -> str:
    """A project in the layout Bambu Studio writes.  *placed* is one entry per
    build item: ``(object id, plate number, x, y)``, the place in the project's
    own layout.  Each object's mesh is kept in a model part of its own."""
    object_ids = sorted({oid for oid, _plate, _x, _y in placed})
    # Each object is one part, kept in a model file of its own under an id
    # no other object uses, and listed in the model's relationships.
    resources = "".join(
        f'\n  <object id="{oid}" p:UUID="0000000{oid}-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n   <components>\n'
        f'    <component p:path="/3D/Objects/object_{oid}.model" objectid="{oid + 100}" '
        f'p:UUID="000{oid}0000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0" />\n'
        f"   </components>\n  </object>" for oid in object_ids
    )
    items = "".join(
        f'\n  <item objectid="{oid}" p:UUID="0000001{index}-b1ec-4553-aec9-835e5b724bb4" '
        f'transform="1 0 0 0 1 0 0 0 1 {x:g} {y:g} 0" printable="1" />'
        for index, (oid, _plate, x, y) in enumerate(placed)
    )
    root = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" xmlns="{_CORE}" '
            f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="{_PRODUCTION}" '
            f'requiredextensions="p">\n <metadata name="Application">BambuStudio-01.10.02.67</metadata>\n'
            f' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>{resources}\n </resources>\n'
            f' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">{items}\n </build>\n</model>\n')
    count = plates or max(plate for _oid, plate, _x, _y in placed)
    copies: dict[int, int] = {}
    on_plate: dict[int, list[tuple[int, int]]] = {n: [] for n in range(1, count + 1)}
    for oid, plate, _x, _y in placed:
        on_plate[plate].append((oid, copies.get(oid, 0)))
        copies[oid] = copies.get(oid, 0) + 1
    settings = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for oid in object_ids:
        settings.append(f'  <object id="{oid}">\n    <metadata key="name" value="part {oid}"/>\n'
                        f'    <metadata key="extruder" value="1"/>\n'
                        f'    <part id="{oid + 100}" subtype="normal_part">\n'
                        f'      <metadata key="name" value="part {oid}"/>\n'
                        f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                        f"    </part>\n  </object>\n")
    sliced = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for number, instances in on_plate.items():
        settings.append(f'  <plate>\n    <metadata key="plater_id" value="{number}"/>\n'
                        f'    <metadata key="plater_name" value=""/>\n')
        for oid, copy in instances:
            settings.append(f'    <model_instance>\n      <metadata key="object_id" value="{oid}"/>\n'
                            f'      <metadata key="instance_id" value="{copy}"/>\n'
                            f'      <metadata key="identify_id" value="{100 * oid + copy}"/>\n    </model_instance>\n')
        settings.append("  </plate>\n")
        sliced.append(f'  <plate>\n    <metadata key="index" value="{number}"/>\n'
                      f'    <metadata key="weight" value="{number}.5"/>\n  </plate>\n')
    settings.append("  <assemble>\n")
    for instances in on_plate.values():
        for oid, copy in instances:
            settings.append(f'   <assemble_item object_id="{oid}" instance_id="{copy}" '
                            f'transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0" />\n')
    settings.append("  </assemble>\n</config>\n")
    sliced.append("</config>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", root)
        zf.writestr("3D/_rels/3dmodel.model.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships">\n' + "".join(
                f' <Relationship Target="/3D/Objects/object_{oid}.model" Id="rel-{oid}" '
                f'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' for oid in object_ids
            ) + "</Relationships>\n"))
        for oid in object_ids:
            zf.writestr(f"3D/Objects/object_{oid}.model", (
                f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" xmlns="{_CORE}" '
                f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="{_PRODUCTION}" '
                f'requiredextensions="p">\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
                f'  <object id="{oid + 100}" p:UUID="000{oid}0000-81cb-4c03-9d28-80fed5dfa1dc" type="model">'
                f"{_box_mesh(size)}</object>\n </resources>\n <build/>\n</model>\n"))
        zf.writestr("Metadata/model_settings.config", "".join(settings))
        zf.writestr("Metadata/slice_info.config", "".join(sliced))
        if bed is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(
                {"printable_area": ["0x0", f"{bed:g}x0", f"{bed:g}x{bed:g}", f"0x{bed:g}"], "printer_model": "A printer"}))
        zf.writestr("Metadata/plate_1.png", bytes(range(200)))
    return str(path)


def _two_plates(path: Path) -> str:
    """Object 2 on plate 1 at (100, 100); object 4 on plate 2, 50 mm in from that plate's corner."""
    return _project(path, [(2, 1, 100.0, 100.0), (4, 2, _STRIDE + 50.0, 60.0)])


def _bounds(path: str) -> dict[str, float]:
    from kiln.printers.bed_fit import compute_3mf_geometry_bbox

    box = compute_3mf_geometry_bbox(path)
    assert box is not None
    return {key: round(value, 6) for key, value in box.items()}


def _member(path: str, name: str) -> str:
    return zipfile.ZipFile(path).read(name).decode()


# ---------------------------------------------------------------------------
# Reading the plates
# ---------------------------------------------------------------------------


def test_the_plates_of_a_project_are_read(tmp_path: Path) -> None:
    plates = read_plates(_two_plates(tmp_path / "project.3mf"))
    assert [plate.to_dict() for plate in plates] == [
        {"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1},
    ]


def test_a_file_that_is_no_project_has_none(tmp_path: Path) -> None:
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("[Content_Types].xml", _TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", f'<model unit="millimeter" xmlns="{_CORE}"><resources>'
                    f'<object id="1" type="model">{_box_mesh(10)}</object></resources>'
                    f'<build><item objectid="1"/></build></model>')
    (tmp_path / "part.stl").write_bytes(b"solid x\nendsolid x\n")
    (tmp_path / "broken.3mf").write_bytes(b"not a zip")

    assert read_plates(str(plain)) == []
    assert read_plates(str(tmp_path / "part.stl")) == []
    assert read_plates(str(tmp_path / "broken.3mf")) == []
    assert one_plate(str(plain), None) is None
    assert one_plate(str(tmp_path / "part.stl"), None) is None


def test_an_empty_second_plate_does_not_make_a_project_of_several(tmp_path: Path) -> None:
    project = _project(tmp_path / "project.3mf", [(2, 1, 100.0, 100.0)], plates=2)
    assert len(read_plates(project)) == 1
    assert one_plate(project, None) is None and one_plate(project, 1) is None


# ---------------------------------------------------------------------------
# Never on a guess
# ---------------------------------------------------------------------------


def test_a_project_of_several_plates_is_not_sliced_until_one_is_named(tmp_path: Path) -> None:
    project = _two_plates(tmp_path / "project.3mf")

    with pytest.raises(PlateChoiceError) as refused:
        one_plate(project, None, out_dir=str(tmp_path))

    assert refused.value.code == PLATE_NOT_CHOSEN
    assert refused.value.plates == [{"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1}]
    assert str(refused.value) == (
        "project.3mf is a project with 2 plates (plate 1: 1 object; plate 2: 1 object), and Kiln slices one "
        "plate at a time. Name the plate with plate_number on a slice or an estimate; to print one, slice it "
        "first and print the file that slice names."
    )
    assert not list(tmp_path.glob("*_plate*.3mf"))


def test_a_plate_the_project_does_not_have_is_refused_with_the_ones_it_has(tmp_path: Path) -> None:
    project = _two_plates(tmp_path / "project.3mf")
    with pytest.raises(PlateChoiceError) as refused:
        one_plate(project, 7, out_dir=str(tmp_path))
    assert refused.value.code == NO_SUCH_PLATE
    assert str(refused.value) == "project.3mf has no plate 7. Its plates: plate 1: 1 object; plate 2: 1 object."

    stl = tmp_path / "part.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")
    with pytest.raises(PlateChoiceError) as refused:
        one_plate(str(stl), 2)
    assert refused.value.code == NO_SUCH_PLATE


# ---------------------------------------------------------------------------
# One plate, as a project of its own
# ---------------------------------------------------------------------------


def test_the_plate_named_comes_out_where_the_project_had_it(tmp_path: Path) -> None:
    project = _two_plates(tmp_path / "project.3mf")

    taken = one_plate(project, 2, out_dir=str(tmp_path / "out"), bed=(0.0, 0.0, 256.0, 256.0))

    assert taken is not None and taken.as_arranged and taken.of == 2
    assert os.path.basename(taken.path) == "project_plate2.3mf"
    assert taken.note == "Plate 2 of 2 (1 object), placed as the project arranged it."
    # 50 mm in from the plate's corner in the project; 50 mm in from the bed's now.
    assert _bounds(taken.path) == {"x_min": 50.0, "x_max": 70.0, "y_min": 60.0, "y_max": 80.0,
                                   "z_min": 0.0, "z_max": 20.0}
    # One plate, numbered 1, and nothing about the other plate's object left
    # for a slicer to look for.
    assert [plate.to_dict() for plate in read_plates(taken.path)] == [{"plate_number": 1, "objects": 1}]
    root = _member(taken.path, "3D/3dmodel.model")
    assert re.findall(r'<item objectid="(\d+)"', root) == ["4"]
    settings = _member(taken.path, "Metadata/model_settings.config")
    assert settings.count("<plate>") == 1 and 'key="plater_id" value="1"' in settings
    assert re.findall(r'<assemble_item object_id="(\d+)"', settings) == ["4"]
    sliced = _member(taken.path, "Metadata/slice_info.config")
    assert sliced.count("<plate>") == 1 and 'key="index" value="1"' in sliced and 'value="2.5"' in sliced
    # The other plate's own model file leaves with it, and so does the line that
    # listed it; every other part of the project is as it was.
    before, after = zipfile.ZipFile(project), zipfile.ZipFile(taken.path)
    other = "3D/Objects/object_2.model"
    assert after.namelist() == [name for name in before.namelist() if name != other]
    listed = _member(taken.path, "3D/_rels/3dmodel.model.rels")
    assert "object_4.model" in listed and "object_2.model" not in listed
    rewritten = {"3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
                 "Metadata/model_settings.config", "Metadata/slice_info.config"}
    for name in after.namelist():
        if name not in rewritten:
            assert after.read(name) == before.read(name), name


def test_the_first_plate_stays_put(tmp_path: Path) -> None:
    taken = one_plate(_two_plates(tmp_path / "project.3mf"), 1, out_dir=str(tmp_path / "out"),
                      bed=(0.0, 0.0, 256.0, 256.0))
    assert taken is not None and taken.as_arranged
    assert _bounds(taken.path)["x_min"] == 100.0 and _bounds(taken.path)["y_min"] == 100.0


def test_a_plate_keeps_its_place_from_the_middle_of_another_bed(tmp_path: Path) -> None:
    """Arranged for a 256 mm bed, sliced for a 180 mm one: the same distance
    from the middle of the bed, 38 mm nearer its corner."""
    taken = one_plate(_two_plates(tmp_path / "project.3mf"), 2, out_dir=str(tmp_path / "out"),
                      bed=(0.0, 0.0, 180.0, 180.0))
    assert taken is not None and taken.as_arranged
    assert _bounds(taken.path)["x_min"] == 12.0 and _bounds(taken.path)["y_min"] == 22.0


def test_plates_past_the_first_row_stand_toward_negative_y(tmp_path: Path) -> None:
    """Three plates are laid out two across: the third starts the second row."""
    project = _project(tmp_path / "project.3mf",
                       [(2, 1, 100.0, 100.0), (4, 2, _STRIDE + 50.0, 60.0), (6, 3, 30.0, -_STRIDE + 40.0)])

    taken = one_plate(project, 3, out_dir=str(tmp_path / "out"), bed=(0.0, 0.0, 256.0, 256.0))

    assert taken is not None and taken.as_arranged
    assert _bounds(taken.path)["x_min"] == 30.0 and _bounds(taken.path)["y_min"] == 40.0


def test_a_plate_that_is_not_where_the_layout_puts_it_goes_to_the_middle_and_says_so(tmp_path: Path) -> None:
    """The place is used only when the plate's own objects confirm it."""
    project = _project(tmp_path / "project.3mf", [(2, 1, 100.0, 100.0), (4, 2, 900.0, 60.0)])

    taken = one_plate(project, 2, out_dir=str(tmp_path / "out"), bed=(0.0, 0.0, 256.0, 256.0))

    assert taken is not None and not taken.as_arranged
    assert _bounds(taken.path)["x_min"] == 118.0 and _bounds(taken.path)["y_min"] == 118.0
    assert taken.note == ("Plate 2 of 2 (1 object), moved as a group to the middle of the bed: where the plate "
                          "stands in the project could not be read.")


def test_two_copies_of_one_object_on_two_plates_are_told_apart(tmp_path: Path) -> None:
    project = _project(tmp_path / "project.3mf", [(2, 1, 100.0, 100.0), (2, 2, _STRIDE + 50.0, 60.0)])

    taken = one_plate(project, 2, out_dir=str(tmp_path / "out"), bed=(0.0, 0.0, 256.0, 256.0))

    assert taken is not None
    assert _bounds(taken.path)["x_min"] == 50.0
    settings = _member(taken.path, "Metadata/model_settings.config")
    # The second copy is this project's first.
    assert re.findall(r'key="instance_id" value="(\d+)"', settings) == ["0"]
    assert re.findall(r'<assemble_item object_id="2" instance_id="(\d+)"', settings) == ["0"]


# ---------------------------------------------------------------------------
# At the slicing step and at the doors
# ---------------------------------------------------------------------------


def test_the_slicing_step_refuses_before_it_looks_for_a_slicer(tmp_path: Path, monkeypatch) -> None:
    import kiln.slicer as slicer

    monkeypatch.setattr(slicer, "find_slicer", lambda *a, **k: pytest.fail("looked for a slicer"))
    project = _two_plates(tmp_path / "project.3mf")

    with pytest.raises(slicer.ProjectPlateError) as refused:
        slicer.slice_file(project, output_dir=str(tmp_path / "out"))
    assert refused.value.code == PLATE_NOT_CHOSEN and len(refused.value.plates) == 2

    with pytest.raises(slicer.ProjectPlateError) as refused:
        slicer.slice_file(project, output_dir=str(tmp_path / "out"), plate_number=5)
    assert refused.value.code == NO_SUCH_PLATE


def test_the_plate_is_what_the_slicer_is_handed_and_the_result_says_which(tmp_path: Path, monkeypatch) -> None:
    import kiln.slicer as slicer

    handed: dict[str, str] = {}

    def fake(_slicer, input_abs, sliced_input, out_file, **_kwargs):
        handed["input"] = input_abs
        return slicer.SliceResult(success=True, output_path=out_file, slicer="fake", message="Sliced it")

    monkeypatch.setattr(slicer, "find_slicer", lambda *a, **k: slicer.SlicerInfo(path="/bin/true", name="PrusaSlicer"))
    monkeypatch.setattr(slicer, "_slice_decided", fake)
    monkeypatch.setattr(slicer, "detect_3mf_multicolor", lambda *_a, **_k: None, raising=False)

    result = slicer.slice_file(_two_plates(tmp_path / "project.3mf"), output_dir=str(tmp_path / "out"), plate_number=2)

    assert os.path.basename(handed["input"]) == "project_plate2.3mf"
    assert result.project_plate["plate_number"] == 2 and result.project_plate["of"] == 2
    assert result.project_plate["file"] == handed["input"]
    assert result.message == "Sliced it. Plate 2 of 2 (1 object), placed as the project arranged it."
    assert result.to_dict()["project_plate"]["as_arranged"] is True


def test_a_file_sliced_as_it_is_costs_no_look_at_the_printer(tmp_path: Path) -> None:
    """The bed is asked for only once a plate is being taken."""
    def bed():
        pytest.fail("looked up the bed for a file that is sliced as it is")

    loose = tmp_path / "part.stl"
    loose.write_bytes(b"solid part\nendsolid part\n")
    assert one_plate(str(loose), None, bed=bed) is None
    one = _project(tmp_path / "one.3mf", [(2, 1, 100.0, 100.0)])
    assert one_plate(one, None, bed=bed) is None


def test_a_file_that_cannot_be_opened_is_left_to_the_checks_that_follow(tmp_path: Path) -> None:
    """With no plate named this step has nothing to say about a broken file;
    with one named, it says it cannot be had."""
    broken = tmp_path / "part.3mf"
    broken.write_bytes(b"not an archive")

    assert one_plate(str(broken), None) is None
    with pytest.raises(PlateChoiceError) as refused:
        one_plate(str(broken), 2)
    assert refused.value.code == "PLATE_NOT_READ"


def test_a_profile_that_cannot_be_read_does_not_stop_a_plain_slice(tmp_path: Path) -> None:
    from kiln.slicer import take_project_plate

    profile = tmp_path / "own.ini"
    profile.write_bytes(b"; caf\xe9\nbed_shape = 0x0,256x0\n")
    loose = tmp_path / "part.stl"
    loose.write_bytes(b"solid part\nendsolid part\n")
    assert take_project_plate(str(loose), None, str(profile)) is None
    # A plate is still taken: the project's own bed stands in for the printer's.
    taken = take_project_plate(_two_plates(tmp_path / "project.3mf"), 2, str(profile))
    assert taken is not None and taken.as_arranged


@pytest.fixture()
def doors(monkeypatch) -> dict:
    """The slice tools as an agent reaches them, signed in."""
    import kiln.server as server
    from kiln.plugins.estimate_tools import plugin as estimates
    from kiln.plugins.slicer_tools import plugin

    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    registered: dict = {}

    class _Mcp:
        def tool(self, **_kwargs):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    estimates.register(_Mcp())
    registered["run_quick_print"] = server.run_quick_print
    registered["run_reslice_and_print"] = server.run_reslice_and_print
    return registered


def _error(reply) -> dict:
    reply = reply if isinstance(reply, dict) else next(block for block in reply if isinstance(block, dict))
    assert reply.get("success") is False, reply
    error = reply["error"]
    return error if isinstance(error, dict) else reply


@pytest.mark.parametrize("door, argument", [
    ("slice_model", "input_path"), ("reslice_with_overrides", "input_path"),
    ("slice_and_estimate", "input_path"), ("estimate_print_time", "file_path"),
])
def test_a_door_that_slices_refuses_until_a_plate_is_named(tmp_path: Path, doors, door, argument) -> None:
    """Each door that takes a plate says the same thing with none named: the
    sentence as written, its code, and the plates to choose from."""
    more = {"overrides": {"layer_height": 0.2}} if door == "reslice_with_overrides" else {}
    reply = doors[door](**{argument: _two_plates(tmp_path / "project.3mf")}, printer_id="bambu_a1", **more)

    error = _error(reply)
    assert error["code"] == PLATE_NOT_CHOSEN
    assert error["message"].startswith("project.3mf is a project with 2 plates (plate 1: 1 object; plate 2: 1 object)")
    assert reply["plates"] == [{"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1}]


@pytest.mark.parametrize("door, argument", [("slice_model", "input_path"), ("estimate_print_time", "file_path")])
def test_a_plate_the_project_does_not_hold_is_named_back(tmp_path: Path, doors, door, argument) -> None:
    reply = doors[door](**{argument: _two_plates(tmp_path / "project.3mf")}, printer_id="bambu_a1", plate_number=5)

    error = _error(reply)
    assert error["code"] == "NO_SUCH_PLATE"
    assert reply["plates"] == [{"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1}]


@pytest.mark.parametrize("door, argument", [
    ("slice_and_print", "input_path"), ("run_quick_print", "model_path"), ("run_reslice_and_print", "model_path"),
])
def test_a_door_that_prints_refuses_a_project_of_several_plates_first(tmp_path: Path, doors, door, argument) -> None:
    """A print door prints the file the person previewed, so it names no
    plate: it says how to get one plate as a file of its own, before it asks
    for a preview or touches a printer."""
    reply = doors[door](**{argument: _two_plates(tmp_path / "project.3mf")})

    error = _error(reply)
    assert error["code"] == PLATE_NOT_CHOSEN
    assert "to print one, slice it first and print the file that slice names" in error["message"]
    assert reply["plates"] == [{"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1}]


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


def _wall_bounds(gcode: str) -> tuple[float, float, float, float]:
    """``(x0, x1, y0, y1)`` of every wall the file prints."""
    from kiln.gcode import feature_label

    xs: list[float] = []
    ys: list[float] = []
    x = y = 0.0
    wall = False
    number = re.compile(r"([XYE])(-?\d*\.?\d+)")
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                label = feature_label(line)
                if label is not None:
                    wall = "wall" in label.lower() or "perimeter" in label.lower()
            elif line.startswith("G1"):
                words = dict(number.findall(line.split(";")[0]))
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                if wall and float(words.get("E", 0)) > 0 and (nx, ny) != (x, y):
                    xs += [x, nx]
                    ys += [y, ny]
                x, y = nx, ny
    assert xs, "no walls in the sliced file"
    return min(xs), max(xs), min(ys), max(ys)


@pytest.mark.skipif("Bambu Studio" not in _SLICERS, reason="Bambu Studio is not installed")
@pytest.mark.parametrize("slicer", [name for name in ("Bambu Studio", "PrusaSlicer") if name in _SLICERS] or ["none"])
def test_a_plate_of_a_project_bambu_studio_saved_prints_where_it_was_arranged(tmp_path, monkeypatch, slicer):
    """Two slabs too wide to share a plate, saved by Bambu Studio itself: it
    arranges them onto two plates.  The second plate, sliced through Kiln, is
    the second slab alone, in the middle of the bed where its plate had it."""
    import subprocess

    trimesh = pytest.importorskip("trimesh")
    from kiln import monitor_twin
    from kiln.slicer import ProjectPlateError, slice_file
    from kiln.slicer_orca import ini_to_settings, write_orca_presets
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    profile = resolve_slicer_profile("bambu_a1")
    # 150 mm and 140 mm square: told apart by size, and two do not fit a 256 mm bed.
    for name, side in (("first", 150.0), ("second", 140.0)):
        trimesh.creation.box(extents=(side, side, 1.0)).export(str(tmp_path / f"{name}.stl"))
    presets = write_orca_presets(ini_to_settings(profile), str(tmp_path), name="a1")
    project = tmp_path / "project.3mf"
    saved = subprocess.run(
        [_SLICERS["Bambu Studio"], "--load-settings", f"{presets.machine_path};{presets.process_path}",
         "--load-filaments", ";".join(presets.filament_paths), "--arrange", "1", "--export-3mf", str(project),
         str(tmp_path / "first.stl"), str(tmp_path / "second.stl")],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
    )
    assert project.is_file(), f"Bambu Studio saved no project (exit {saved.returncode})"
    assert [plate.to_dict() for plate in read_plates(str(project))] == [
        {"plate_number": 1, "objects": 1}, {"plate_number": 2, "objects": 1},
    ], "Bambu Studio did not lay the two slabs out on two plates"

    with pytest.raises(ProjectPlateError) as refused:
        slice_file(str(project), output_dir=str(tmp_path / "out"), profile=profile,
                   slicer_path=_SLICERS[slicer], timeout=600)
    assert refused.value.code == PLATE_NOT_CHOSEN

    result = slice_file(str(project), output_dir=str(tmp_path / "out"), profile=profile,
                        slicer_path=_SLICERS[slicer], timeout=600, plate_number=2)

    x0, x1, y0, y1 = _wall_bounds(result.output_path)
    # The second slab alone (140 mm, not 150), about the middle of the bed.
    assert x1 - x0 == pytest.approx(140.0, abs=1.0) and y1 - y0 == pytest.approx(140.0, abs=1.0)
    assert (x0 + x1) / 2 == pytest.approx(128.0, abs=1.0) and (y0 + y1) / 2 == pytest.approx(128.0, abs=1.0)
    assert result.project_plate["plate_number"] == 2 and result.project_plate["as_arranged"] is True
    assert result.message.endswith("Plate 2 of 2 (1 object), placed as the project arranged it.")
