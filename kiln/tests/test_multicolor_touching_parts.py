"""Coloured parts that touch are ONE print.

The defect this pins (2026-10-23): ``compose_multicolor_3mf`` wrote every
part as a build object of its own, so two parts that overlap reached the
slicer as two objects occupying the same space.  OrcaSlicer, the slicer Kiln
switches to for a multicolour file, refused the plate ("gcode path conflicts
found between B and A", exit 155) while the compose had reported success;
PrusaSlicer printed the shared space twice (8.31 g against 5.91 g for the
same shape as one body).  The same compose also embedded a purge matrix
OrcaSlicer could not parse (an integer list, padded to four slots) and
listed one "extruder N -> name" line per part.

Covered here, against the real slicers where they are installed: touching
parts become one object of several parts and separate ones stay separate;
in an overlap the smaller part is printed; PrusaSlicer's slice gets the
parts as one object; the purge matrix parses and is the one purged; and the
message counts colours, not parts.
"""

from __future__ import annotations

import json
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
_PRUSA = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_SLIC3R_EXTRUDER = "{http://schemas.slic3r.org/3mf/2017/06}extruder"


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _box(path: Path, lo: tuple[float, float, float], hi: tuple[float, float, float]) -> str:
    """A closed, outward-wound box as a binary STL."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _model(path: str) -> ET.Element:
    with zipfile.ZipFile(path) as zf:
        return ET.fromstring(zf.read("3D/3dmodel.model"))


def _settings(path: str) -> ET.Element:
    with zipfile.ZipFile(path) as zf:
        return ET.fromstring(zf.read("Metadata/model_settings.config"))


def _build_objects(path: str) -> list[dict]:
    """Per build item: its object id, and per component (objectid, tx, ty, tz)."""
    root = _model(path)
    objects = {o.get("id"): o for o in root.iter(f"{_CORE}object")}
    out = []
    for item in root.iter(f"{_CORE}item"):
        obj = objects[item.get("objectid")]
        comps = [
            (c.get("objectid"), *[float(t) for t in c.get("transform").split()[9:12]])
            for c in obj.iter(f"{_CORE}component")
        ]
        out.append({
            "object": item.get("objectid"),
            "item_translation": [float(t) for t in item.get("transform").split()[9:12]],
            "components": comps,
        })
    return out


def _part_extruders(path: str, object_id: str) -> list[tuple[str, int]]:
    for obj in _settings(path).iter("object"):
        if obj.get("id") == object_id:
            return [
                (part.get("id"), int(next(
                    md.get("value") for md in part.findall("metadata")
                    if md.get("key") == "extruder"
                )))
                for part in obj.findall("part")
            ]
    return []


# ---------------------------------------------------------------------------
# Structure: one object of parts, or separate objects
# ---------------------------------------------------------------------------


class TestTouchingPartsAreOneObject:
    def test_overlapping_parts_become_one_object_smaller_part_last(self, tmp_path):
        # The small part is listed FIRST; it is sunk 5 mm into the big one.
        small = _box(tmp_path / "small.stl", (115, 105, 0), (125, 115, 10))
        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        out = str(tmp_path / "pair.3mf")
        result = compose_multicolor_3mf(
            [ColorPart(small, extruder=2, name="inlay"),
             ColorPart(big, extruder=1, name="body", x=1.5, y=-2.0)],
            output_path=out,
        )
        assert result["success"] is True, result
        assert (result["parts"], result["objects"], result["colours"]) == (2, 1, 2)

        build = _build_objects(out)
        assert len(build) == 1
        assert build[0]["item_translation"] == [0.0, 0.0, 0.0]
        # Largest first: body (mesh id 2) then inlay (mesh id 1), each at
        # its own placement.
        assert build[0]["components"] == [("2", 1.5, -2.0, 0.0), ("1", 0.0, 0.0, 0.0)]
        assert _part_extruders(out, build[0]["object"]) == [("2", 1), ("1", 2)]

    def test_parts_placed_apart_stay_separate_objects(self, tmp_path):
        a = _box(tmp_path / "a.stl", (100, 100, 0), (110, 110, 10))
        b = _box(tmp_path / "b.stl", (112, 100, 0), (122, 110, 10))  # 2 mm gap
        out = str(tmp_path / "apart.3mf")
        result = compose_multicolor_3mf(
            [ColorPart(a, extruder=1), ColorPart(b, extruder=2)], output_path=out,
        )
        assert result["objects"] == 2
        build = _build_objects(out)
        assert [entry["components"] for entry in build] == [[], []]
        root = _model(out)
        assert [i.get(_SLIC3R_EXTRUDER) for i in root.iter(f"{_CORE}item")] == ["1", "2"]

    def test_parts_sharing_a_face_join_and_joining_is_transitive(self, tmp_path):
        # a touches b at x=110, b touches c at x=120; a and c never meet.
        a = _box(tmp_path / "a.stl", (100, 100, 0), (110, 110, 10))
        b = _box(tmp_path / "b.stl", (110, 100, 0), (120, 110, 10))
        c = _box(tmp_path / "c.stl", (120, 100, 0), (130, 110, 10))
        d = _box(tmp_path / "d.stl", (150, 100, 0), (160, 110, 10))
        out = str(tmp_path / "chain.3mf")
        result = compose_multicolor_3mf(
            [ColorPart(p, extruder=i + 1) for i, p in enumerate((a, b, c, d))],
            output_path=out,
        )
        assert result["objects"] == 2
        build = _build_objects(out)
        assert [len(entry["components"]) for entry in build] == [3, 0]


# ---------------------------------------------------------------------------
# What the person is told
# ---------------------------------------------------------------------------


def _donut_like(tmp_path: Path, with_materials: bool = True) -> list[ColorPart]:
    dough = _box(tmp_path / "dough.stl", (100, 100, 0), (140, 140, 10))
    icing = _box(tmp_path / "icing.stl", (102, 102, 8), (138, 138, 12))
    parts = [
        ColorPart(dough, extruder=1, name="Donut", material="Dough" if with_materials else None),
        ColorPart(icing, extruder=2, name="Icing", material="Icing pink" if with_materials else None),
    ]
    for k in range(6):
        x = 105 + 5 * k
        sprinkle = _box(tmp_path / f"s{k}.stl", (x, 120, 11), (x + 3, 121, 13))
        blue = k % 2 == 0
        parts.append(ColorPart(
            sprinkle,
            extruder=3 if blue else 4,
            name=f"Sprinkle.{k:02d}",
            material=("Sprinkle blue" if blue else "Sprinkle yellow") if with_materials else None,
        ))
    return parts


class TestTheMessageCountsColours:
    def test_colours_and_slots_in_the_persons_terms(self, tmp_path):
        result = compose_multicolor_3mf(
            _donut_like(tmp_path), output_path=str(tmp_path / "donut.3mf"),
        )
        assert result["success"] is True, result
        assert (result["parts"], result["objects"], result["colours"]) == (8, 1, 4)
        summary = (
            "Dough in slot 1, Icing pink in slot 2, "
            "Sprinkle blue (3 pieces) in slot 3, Sprinkle yellow (3 pieces) in slot 4"
        )
        assert result["extruder_map"] == summary
        assert result["message"].startswith(f"Created a 4-colour 3MF (96 triangles): {summary}.")
        assert "8 parts touch, so they print as one object" in result["message"]
        assert "Sprinkle.03" not in result["message"]

    def test_unlabelled_copies_are_named_once_with_a_count(self, tmp_path):
        result = compose_multicolor_3mf(
            _donut_like(tmp_path, with_materials=False),
            output_path=str(tmp_path / "donut.3mf"),
        )
        assert result["extruder_map"] == (
            "Donut in slot 1, Icing in slot 2, "
            "Sprinkle (3 pieces) in slot 3, Sprinkle (3 pieces) in slot 4"
        )

    def test_the_slicer_note_claims_no_slice_it_did_not_run(self, tmp_path):
        result = compose_multicolor_3mf(
            _donut_like(tmp_path), output_path=str(tmp_path / "donut.3mf"),
        )
        note = result["slicer_note"]
        assert "as-is" not in note and "verified" not in note
        assert "PrusaSlicer" in note and "merge" in note


# ---------------------------------------------------------------------------
# PrusaSlicer's form of a joined object
# ---------------------------------------------------------------------------


class TestPrusaMultipartCopy:
    def test_a_joined_object_becomes_one_object_of_volumes(self, tmp_path):
        from kiln.multicolor_3mf import prusa_multipart_copy

        small = _box(tmp_path / "small.stl", (115, 105, 0), (125, 115, 10))
        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        src = str(tmp_path / "pair.3mf")
        compose_multicolor_3mf(
            [ColorPart(small, extruder=2, name="inlay"),
             ColorPart(big, extruder=1, name="body", x=1.5)],
            output_path=src,
        )
        dst = str(tmp_path / "prusa.3mf")
        assert prusa_multipart_copy(src, dst) is True

        root = _model(dst)
        objects = list(root.iter(f"{_CORE}object"))
        assert len(objects) == 1
        assert len(list(root.iter(f"{_CORE}item"))) == 1
        xs = [float(v.get("x")) for v in objects[0].iter(f"{_CORE}vertex")]
        assert (min(xs), max(xs)) == (101.5, 125.0)  # body moved by x=1.5
        with zipfile.ZipFile(dst) as zf:
            config = ET.fromstring(zf.read("Metadata/Slic3r_PE_model.config"))
        volumes = [
            (int(v.get("firstid")), int(v.get("lastid")), {
                md.get("key"): md.get("value") for md in v.findall("metadata")
            })
            for v in config.iter("volume")
        ]
        assert [(f, last, meta["name"], meta["extruder"]) for f, last, meta in volumes] == [
            (0, 11, "body", "1"), (12, 23, "inlay", "2"),
        ]

    def test_nothing_is_written_when_no_object_holds_parts(self, tmp_path):
        from kiln.multicolor_3mf import prusa_multipart_copy

        a = _box(tmp_path / "a.stl", (100, 100, 0), (110, 110, 10))
        b = _box(tmp_path / "b.stl", (130, 100, 0), (140, 110, 10))
        src = str(tmp_path / "apart.3mf")
        compose_multicolor_3mf(
            [ColorPart(a, extruder=1), ColorPart(b, extruder=2)], output_path=src,
        )
        dst = tmp_path / "prusa.3mf"
        assert prusa_multipart_copy(src, str(dst)) is False
        assert not dst.exists()

    def test_a_modifier_part_is_not_carried_over_as_solid(self, tmp_path):
        from kiln.multicolor_3mf import prusa_multipart_copy

        small = _box(tmp_path / "small.stl", (115, 105, 0), (125, 115, 10))
        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        src = str(tmp_path / "pair.3mf")
        compose_multicolor_3mf(
            [ColorPart(big, extruder=1), ColorPart(small, extruder=2)], output_path=src,
        )
        edited = str(tmp_path / "modifier.3mf")
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(edited, "w") as zout:
            for name in zin.namelist():
                data = zin.read(name)
                if name == "Metadata/model_settings.config":
                    data = data.replace(b'part id="2" subtype="normal_part"',
                                        b'part id="2" subtype="modifier_part"')
                zout.writestr(name, data)
        assert prusa_multipart_copy(edited, str(tmp_path / "prusa.3mf")) is False


def _nested_3mf(path: Path, outer: int, inner: int) -> str:
    """A small archive that places one cube ``outer * inner`` times through
    two levels of components: a few hundred bytes of structure, a large
    model once every placement is written out."""
    cube = (
        "<mesh><vertices>"
        + "".join(
            f'<vertex x="{x}" y="{y}" z="{z}"/>'
            for x, y, z in [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                            (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
        )
        + "</vertices><triangles>"
        + "".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
            for a, b, c in [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        )
        + "</triangles></mesh>"
    )

    def components(objectid: int, n: int, step: float) -> str:
        return "<components>" + "".join(
            f'<component objectid="{objectid}" transform="1 0 0 0 1 0 0 0 1 {100 + i * step} 100 0"/>'
            for i in range(n)
        ) + "</components>"

    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model">{cube}</object>'
        f'<object id="2" type="model">{components(1, inner, 0.0)}</object>'
        f'<object id="3" type="model">{components(2, outer, 0.0)}</object></resources>'
        '<build><item objectid="3"/></build></model>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", model)
    return str(path)


def _joined_pair(tmp_path: Path) -> str:
    small = _box(tmp_path / "small.stl", (115, 105, 0), (125, 115, 10))
    big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
    src = str(tmp_path / "pair.3mf")
    compose_multicolor_3mf(
        [ColorPart(big, extruder=1), ColorPart(small, extruder=2)], output_path=src,
    )
    return src


def _with_model_settings(src: str, dst: str, settings: bytes) -> str:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            zout.writestr(name, settings if name == "Metadata/model_settings.config" else data)
    return dst


class TestPrusaMultipartCopyHoldsToTheReadersBudgets:
    """The copy writes every placement out in full, so a mesh placed many
    times by components is counted every time, against the archive reader's
    own budgets, before anything is written; its sidecars are read by the
    reader's own hardened parser."""

    def test_vertices_and_triangles_summed_over_placements_are_capped(
        self, tmp_path, monkeypatch,
    ):
        from kiln import threemf_parser
        from kiln.multicolor_3mf import prusa_multipart_copy

        # 200 placements of a 20-entry cube: 4000 entries written.
        src = _nested_3mf(tmp_path / "nested.3mf", outer=10, inner=20)
        monkeypatch.setattr(threemf_parser, "_MAX_KEPT_MESH_ENTRIES", 10_000)
        assert prusa_multipart_copy(src, str(tmp_path / "fits.3mf")) is True
        monkeypatch.setattr(threemf_parser, "_MAX_KEPT_MESH_ENTRIES", 2_000)
        dst = tmp_path / "refused.3mf"
        assert prusa_multipart_copy(src, str(dst)) is False
        assert not dst.exists()

    def test_placements_are_capped(self, tmp_path, monkeypatch):
        from kiln import threemf_parser
        from kiln.multicolor_3mf import prusa_multipart_copy

        # 1 + 10 + 200 placements from a few dozen structure elements.
        src = _nested_3mf(tmp_path / "nested.3mf", outer=10, inner=20)
        monkeypatch.setattr(threemf_parser, "_MAX_RETAINED_ELEMENTS", 1_000)
        assert prusa_multipart_copy(src, str(tmp_path / "fits.3mf")) is True
        monkeypatch.setattr(threemf_parser, "_MAX_RETAINED_ELEMENTS", 150)
        dst = tmp_path / "refused.3mf"
        assert prusa_multipart_copy(src, str(dst)) is False
        assert not dst.exists()

    def test_an_amplifying_archive_is_refused_at_the_real_budget(self, tmp_path):
        from kiln.multicolor_3mf import prusa_multipart_copy

        # A million placements of one cube, from about 2000 elements.
        src = _nested_3mf(tmp_path / "bomb.3mf", outer=1000, inner=1000)
        dst = tmp_path / "refused.3mf"
        assert prusa_multipart_copy(src, str(dst)) is False
        assert not dst.exists()

    def test_a_sidecar_declaring_entities_is_refused_unparsed(self, tmp_path, monkeypatch):
        import xml.etree.ElementTree as stdlib_et

        from kiln.multicolor_3mf import prusa_multipart_copy

        src = _joined_pair(tmp_path)
        assert prusa_multipart_copy(src, str(tmp_path / "control.3mf")) is True
        hostile = _with_model_settings(
            src, str(tmp_path / "entity.3mf"),
            b'<?xml version="1.0"?><!DOCTYPE config [<!ENTITY a "aaaa">]>'
            b'<config><object id="3"><metadata key="name" value="&a;"/></object></config>',
        )
        parsed: list[bytes] = []
        real = stdlib_et.fromstring
        monkeypatch.setattr(
            stdlib_et, "fromstring", lambda data, *a, **k: parsed.append(data) or real(data, *a, **k),
        )
        dst = tmp_path / "refused.3mf"
        assert prusa_multipart_copy(hostile, str(dst)) is False
        assert not dst.exists()
        assert not [d for d in parsed if b"ENTITY" in d]

    def test_an_oversized_sidecar_is_refused_unparsed(self, tmp_path, monkeypatch):
        import xml.etree.ElementTree as stdlib_et

        from kiln import threemf_parser
        from kiln.multicolor_3mf import prusa_multipart_copy

        src = _joined_pair(tmp_path)
        # One element past the structure budget every archive part is read to.
        count = threemf_parser._MAX_RETAINED_ELEMENTS + 1
        bloated = _with_model_settings(
            src, str(tmp_path / "bloated.3mf"),
            b"<config>" + b'<metadata key="k" value="v"/>' * count + b"</config>",
        )
        parsed: list[bytes] = []
        real = stdlib_et.fromstring
        monkeypatch.setattr(
            stdlib_et, "fromstring", lambda data, *a, **k: parsed.append(data) or real(data, *a, **k),
        )
        dst = tmp_path / "refused.3mf"
        assert prusa_multipart_copy(bloated, str(dst)) is False
        assert not dst.exists()
        assert not [d for d in parsed if b'key="k"' in d]


# ---------------------------------------------------------------------------
# The real slicers
# ---------------------------------------------------------------------------


def _profile() -> str:
    from kiln.slicer_profiles import resolve_slicer_profile

    return resolve_slicer_profile("bambu_a1")


def _extrusion_x_by_tool(gcode: str, z_lo: float, z_hi: float, x_max: float) -> dict[str, tuple]:
    """X extent of each tool's extruding moves between two heights, left of
    *x_max* (the prime tower stands to the right of these parts)."""
    tool, z, x = "0", 0.0, None
    spans: dict[str, list[float]] = {}
    for raw in Path(gcode).read_text(errors="replace").splitlines():
        line = raw.split(";", 1)[0].strip()
        if re.fullmatch(r"T\d+", line):
            tool = line[1:]
            continue
        if not line.startswith(("G0", "G1")):
            continue
        words = dict(re.findall(r"([XYZE])(-?[\d.]+)", line))
        nx = float(words["X"]) if "X" in words else x
        if "Z" in words:
            z = float(words["Z"])
        if float(words.get("E", 0)) > 0 and nx is not None and x is not None \
                and z_lo <= z <= z_hi and max(x, nx) < x_max:
            span = spans.setdefault(tool, [1e9, -1e9])
            span[0] = min(span[0], x, nx)
            span[1] = max(span[1], x, nx)
        x = nx
    return {t: (round(a, 1), round(b, 1)) for t, (a, b) in spans.items()}


def _filament_g(gcode: str) -> float:
    text = Path(gcode).read_text(errors="replace")
    return float(re.search(r"; total filament used \[g\] = ([\d.]+)", text).group(1))


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
class TestOrcaSlicesTouchingParts:
    def test_overlapping_parts_slice_with_the_smaller_part_in_the_overlap(self, tmp_path):
        from kiln.slicer import slice_file

        # The small part runs the big one's full depth, 5 mm into it.
        small = _box(tmp_path / "small.stl", (115, 100, 0), (125, 120, 10))
        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        out = str(tmp_path / "pair.3mf")
        compose_multicolor_3mf(
            [ColorPart(small, extruder=2, color="#0000FF"),
             ColorPart(big, extruder=1, color="#FF0000")],
            output_path=out, printer_id="bambu_a1",
        )
        result = slice_file(
            out, output_dir=str(tmp_path / "g"), profile=_profile(),
            slicer_path=_ORCA, timeout=600,
        )
        assert result.success, result.message
        assert "crashed" not in result.message
        spans = _extrusion_x_by_tool(result.output_path, 4.0, 6.0, x_max=126.0)
        # Tool 1 (the small part) holds its whole width, 115..125 less the
        # wall's half-width; tool 0 (the body) stops where the small part
        # starts instead of running on to 120 underneath it.
        assert spans["1"][0] == pytest.approx(115.2, abs=0.3), spans
        assert spans["1"][1] == pytest.approx(124.8, abs=0.3), spans
        assert spans["0"][1] == pytest.approx(114.8, abs=0.3), spans

    @pytest.mark.parametrize("slots", [2, 3, 4])
    def test_the_embedded_purge_matrix_is_the_one_purged(self, tmp_path, slots):
        from kiln.slicer import slice_file

        # Parts placed apart, so the purge is the only thing under test.
        parts = [
            ColorPart(
                _box(tmp_path / f"p{k}.stl", (100 + 15 * k, 100, 0), (110 + 15 * k, 110, 10)),
                extruder=k + 1,
                material="PETG" if k == 1 else "PLA",
            )
            for k in range(slots)
        ]
        out = str(tmp_path / "purge.3mf")
        composed = compose_multicolor_3mf(parts, output_path=out, printer_id="bambu_a1")
        assert composed["flush_matrix_embedded"] is True
        with zipfile.ZipFile(out) as zf:
            embedded = json.loads(zf.read("Metadata/project_settings.config"))
        result = slice_file(
            out, output_dir=str(tmp_path / "g"), profile=_profile(),
            slicer_path=_ORCA, timeout=600,
        )
        assert result.success, result.message
        assert "crashed" not in result.message
        text = Path(result.output_path).read_text(errors="replace")
        used = re.search(r"^; flush_volumes_matrix = (.*)$", text, re.M).group(1)
        assert used.split(",") == embedded["flush_volumes_matrix"]
        assert re.search(r"^; flush_multiplier = 1$", text, re.M)
        tools = set(re.findall(r"^T(\d+)$", text, re.M))
        assert tools == {str(i) for i in range(slots)}


@pytest.mark.skipif(not _installed(_PRUSA), reason="PrusaSlicer not installed")
class TestPrusaSlicerSlicesTouchingParts:
    def test_the_shared_space_is_printed_once(self, tmp_path):
        """A part sunk 10 mm into another prints the same plastic as the same
        shape built from parts that only touch."""
        from kiln.slicer import slice_file

        big = _box(tmp_path / "big.stl", (100, 100, 0), (120, 120, 20))
        sunk = _box(tmp_path / "sunk.stl", (105, 105, 10), (115, 115, 25))
        resting = _box(tmp_path / "resting.stl", (105, 105, 20), (115, 115, 25))
        grams = []
        for name, top in (("sunk", sunk), ("resting", resting)):
            out = str(tmp_path / f"{name}.3mf")
            compose_multicolor_3mf(
                [ColorPart(big, extruder=1), ColorPart(top, extruder=2)],
                output_path=out, printer_id="bambu_a1",
            )
            result = slice_file(
                out, output_dir=str(tmp_path / name), profile=_profile(),
                slicer_path=_PRUSA, timeout=600,
            )
            assert result.success, result.message
            grams.append((_filament_g(result.output_path), result.message))
        (sunk_g, sunk_said), (resting_g, _said) = grams
        # Printed twice, the 1000 mm³ overlap would add about 1.2 g here.
        assert sunk_g == pytest.approx(resting_g, rel=0.03), grams
        assert "crashed" not in sunk_said
