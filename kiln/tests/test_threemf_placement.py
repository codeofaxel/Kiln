"""Moving a 3MF across the plate edits its build items and nothing else.

A placed copy has to be the file the person approved, moved: a painted
3MF keeps every paint byte, a slicer export keeps its sidecars and its
other model parts, and the geometry bbox -- as the slicer will place it --
moves by exactly the millimetres asked for.
"""

from __future__ import annotations

import re
import zipfile

import pytest

from kiln.printers.bed_fit import compute_3mf_geometry_bbox
from kiln.threemf_placement import translate_3mf, translate_model_xml

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def _tri_object(oid: int, x0: float, y0: float) -> str:
    return (
        f'<object id="{oid}" type="model"><mesh><vertices>'
        f'<vertex x="{x0}" y="{y0}" z="0"/><vertex x="{x0 + 10}" y="{y0}" z="0"/>'
        f'<vertex x="{x0 + 5}" y="{y0 + 10}" z="8"/>'
        '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
    )


def _model_xml(build: str, *, unit: str = "millimeter", objects: str | None = None) -> str:
    objects = objects if objects is not None else _tri_object(1, 0, 0) + _tri_object(2, 100, 100)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="{unit}" xml:lang="en-US" xmlns="{_CORE}">'
        f"<resources>{objects}</resources><build>{build}</build></model>"
    )


def _write_3mf(path, model_xml: str, extra: dict[str, bytes] | None = None) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", model_xml)
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return str(path)


def _item_translations(path: str) -> list[tuple[float, float, float]]:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    out = []
    for m in re.finditer(r'<item [^>]*transform="([^"]+)"', xml):
        values = [float(v) for v in m.group(1).split()]
        assert len(values) == 12
        out.append((values[9], values[10], values[11]))
    return out


class TestTheMove:
    def test_the_placed_geometry_moves_by_exactly_the_millimetres_asked(self, tmp_path):
        src = _write_3mf(tmp_path / "two.3mf", _model_xml(
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0"/><item objectid="2"/>'
        ))
        before = compute_3mf_geometry_bbox(src)
        dst = str(tmp_path / "moved.3mf")
        translate_3mf(src, 37.5, -12.25, dst)
        after = compute_3mf_geometry_bbox(dst)
        for axis, delta in (("x", 37.5), ("y", -12.25), ("z", 0.0)):
            assert after[f"{axis}_min"] == pytest.approx(before[f"{axis}_min"] + delta)
            assert after[f"{axis}_max"] == pytest.approx(before[f"{axis}_max"] + delta)

    def test_an_item_without_a_transform_gets_one_and_a_rotation_is_kept(self, tmp_path):
        src = _write_3mf(tmp_path / "two.3mf", _model_xml(
            '<item objectid="1" transform="0 1 0 -1 0 0 0 0 1 20 30 5"/><item objectid="2"/>'
        ))
        dst = str(tmp_path / "moved.3mf")
        translate_3mf(src, 10, 4, dst)
        assert _item_translations(dst) == [(30.0, 34.0, 5.0), (10.0, 4.0, 0.0)]
        with zipfile.ZipFile(dst) as zf:
            xml = zf.read("3D/3dmodel.model").decode()
        assert 'transform="0 1 0 -1 0 0 0 0 1 30 34 5"' in xml, "the rotation part is untouched"
        assert '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 4 0"/>' in xml

    def test_a_model_in_inches_is_moved_in_inches_and_lands_on_the_millimetre(self, tmp_path):
        src = _write_3mf(tmp_path / "inch.3mf", _model_xml('<item objectid="1"/>', unit="inch", objects=_tri_object(1, 0, 0)))
        before = compute_3mf_geometry_bbox(src)
        dst = str(tmp_path / "moved.3mf")
        translate_3mf(src, 25.4, 50.8, dst)
        assert _item_translations(dst) == [(1.0, 2.0, 0.0)]
        after = compute_3mf_geometry_bbox(dst)
        assert after["x_min"] == pytest.approx(before["x_min"] + 25.4)
        assert after["y_max"] == pytest.approx(before["y_max"] + 50.8)


class TestNothingElseChanges:
    def test_a_painted_3mf_keeps_its_paint_byte_for_byte(self, tmp_path):
        from kiln.multicolor_3mf import compose_painted_3mf

        tris = [
            ((0, 0, 0), (10, 0, 0), (5, 10, 8)),
            ((0, 0, 0), (5, 10, 8), (0, 10, 0)),
            ((10, 0, 0), (10, 10, 0), (5, 10, 8)),
        ]
        painted = compose_painted_3mf(tris, ["#FF0000", "#00FF00", None], output_path=str(tmp_path / "painted.3mf"))["output_path"]
        with zipfile.ZipFile(painted) as zf:
            names_before = zf.namelist()
            root_before = zf.read("3D/3dmodel.model")
            others_before = {n: zf.read(n) for n in names_before if n != "3D/3dmodel.model"}
        resources_before = re.search(rb"<resources>.*</resources>", root_before, re.DOTALL).group(0)
        assert b"mmu_segmentation" in resources_before, "the fixture is really painted"

        dst = str(tmp_path / "placed.3mf")
        translate_3mf(painted, 40, 40, dst)

        with zipfile.ZipFile(dst) as zf:
            assert zf.namelist() == names_before
            root_after = zf.read("3D/3dmodel.model")
            assert {n: zf.read(n) for n in zf.namelist() if n != "3D/3dmodel.model"} == others_before
        resources_after = re.search(rb"<resources>.*</resources>", root_after, re.DOTALL).group(0)
        assert resources_after == resources_before
        assert root_after.count(b"mmu_segmentation") == root_before.count(b"mmu_segmentation")
        # The whole root minus its build block is identical too.
        strip = lambda b: re.sub(rb"<build>.*</build>", b"", b, flags=re.DOTALL)  # noqa: E731
        assert strip(root_after) == strip(root_before)
        assert compute_3mf_geometry_bbox(dst)["x_min"] == pytest.approx(compute_3mf_geometry_bbox(painted)["x_min"] + 40)

    def test_other_members_keep_their_content_order_and_compression(self, tmp_path):
        path = tmp_path / "proj.3mf"
        model = _model_xml('<item objectid="1"/>', objects=_tri_object(1, 0, 0))
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>", compress_type=zipfile.ZIP_STORED)
            zf.writestr("Metadata/project_settings.config", b'{"printer": "x"}', compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("3D/3dmodel.model", model, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n" + bytes(range(64)), compress_type=zipfile.ZIP_STORED)
        dst = str(tmp_path / "moved.3mf")
        translate_3mf(str(path), 3, 4, dst)
        with zipfile.ZipFile(path) as a, zipfile.ZipFile(dst) as b:
            assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()]
            for ia, ib in zip(a.infolist(), b.infolist(), strict=True):
                assert ia.compress_type == ib.compress_type
                if ia.filename != "3D/3dmodel.model":
                    assert a.read(ia) == b.read(ib)

    def test_a_production_extension_export_keeps_its_other_model_parts(self, tmp_path):
        part = "3D/Objects/object_1.model"
        root = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<model unit="millimeter" xmlns="{_CORE}" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources><object id="2" p:UUID="u2" type="model"><components>'
            f'<component p:path="/{part}" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            "</components></object></resources>"
            '<build p:UUID="ub"><item objectid="2" p:UUID="ui" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build></model>'
        )
        mesh = f'<?xml version="1.0"?><model unit="millimeter" xmlns="{_CORE}"><resources>{_tri_object(1, -5, -5)}</resources><build/></model>'
        path = tmp_path / "bambu.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", root)
            zf.writestr(part, mesh)
        dst = str(tmp_path / "moved.3mf")
        translate_3mf(str(path), -20, 15, dst)
        with zipfile.ZipFile(dst) as zf:
            assert zf.read(part).decode() == mesh
            assert 'p:UUID="ui" transform="1 0 0 0 1 0 0 0 1 80 115 0"' in zf.read("3D/3dmodel.model").decode()
        assert compute_3mf_geometry_bbox(dst)["x_min"] == pytest.approx(75.0)
        assert compute_3mf_geometry_bbox(dst)["y_min"] == pytest.approx(110.0)


class TestRefusals:
    def test_no_build_items_is_an_error_not_a_guess(self):
        for xml in (_model_xml("", objects=_tri_object(1, 0, 0)), _model_xml("", objects=_tri_object(1, 0, 0)).replace("<build></build>", "<build/>")):
            with pytest.raises(ValueError, match="no build items"):
                translate_model_xml(xml.encode(), 1, 1)

    def test_a_malformed_transform_is_an_error(self):
        xml = _model_xml('<item objectid="1" transform="1 0 0 0 1"/>', objects=_tri_object(1, 0, 0))
        with pytest.raises(ValueError, match="12 numbers"):
            translate_model_xml(xml.encode(), 1, 1)

    def test_an_unknown_unit_is_an_error(self):
        xml = _model_xml('<item objectid="1"/>', unit="furlong", objects=_tri_object(1, 0, 0))
        with pytest.raises(ValueError, match="unit"):
            translate_model_xml(xml.encode(), 1, 1)

    def test_dst_must_differ_from_src(self, tmp_path):
        src = _write_3mf(tmp_path / "a.3mf", _model_xml('<item objectid="1"/>', objects=_tri_object(1, 0, 0)))
        with pytest.raises(ValueError, match="dst must differ"):
            translate_3mf(src, 1, 1, src)

    def test_a_negative_zero_is_written_as_zero(self):
        xml = _model_xml('<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0.5 0 0"/>', objects=_tri_object(1, 0, 0))
        out = translate_model_xml(xml.encode(), -0.5, -0.0).decode()
        assert 'transform="1 0 0 0 1 0 0 0 1 0 0 0"' in out
