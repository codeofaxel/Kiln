"""3MF geometry through the mesh validation engine.

Every tool that writes a 3MF (region painting, multicolor composition,
height coloring, texture recovery) is measured afterwards by
``analyze_mesh``; a 3MF that measures as zero triangles makes all of
them report a broken result for a good file.  These tests pin the
stdlib 3MF reader at each door of ``validation.py`` that dispatches on
file extension, and the build-item / component transforms it honours.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from kiln.generation.validation import (
    analyze_mesh,
    convert_to_stl,
    estimate_support_volume,
    validate_mesh,
)

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

# A closed square pyramid: 10 x 10 base on z=0, apex at z=10.
# Six triangles, outward winding, volume = base * height / 3 = 333.33.
_PYRAMID_MESH = """\
<mesh>
  <vertices>
    <vertex x="0" y="0" z="0" />
    <vertex x="10" y="0" z="0" />
    <vertex x="10" y="10" z="0" />
    <vertex x="0" y="10" z="0" />
    <vertex x="5" y="5" z="10" />
  </vertices>
  <triangles>
    <triangle v1="0" v2="2" v3="1" />
    <triangle v1="0" v2="3" v3="2" />
    <triangle v1="0" v2="1" v3="4" />
    <triangle v1="1" v2="2" v3="4" />
    <triangle v1="2" v2="3" v3="4" />
    <triangle v1="3" v2="0" v3="4" />
  </triangles>
</mesh>"""

_PYRAMID_TRIANGLES = 6
_PYRAMID_VOLUME = 1000.0 / 3.0


_PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _model_xml(resources: str, build: str, *, unit: str = "millimeter") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="{unit}" xmlns="{_CORE_NS}" xmlns:p="{_PRODUCTION_NS}">\n'
        f"  <resources>\n{resources}\n  </resources>\n"
        f"  <build>\n{build}\n  </build>\n"
        f"</model>\n"
    )


def _pyramid_model(
    build: str = '<item objectid="1" />', *, unit: str = "millimeter"
) -> str:
    return _model_xml(
        f'<object id="1" type="model">{_PYRAMID_MESH}</object>',
        build,
        unit=unit,
    )


def _write_3mf(
    tmp_path: Path,
    model_xml: str,
    *,
    name: str = "part.3mf",
    parts: dict[str, str] | None = None,
) -> str:
    """Write a 3MF archive; *parts* adds extra model files by archive name."""
    fpath = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
        for part_name, part_xml in (parts or {}).items():
            zf.writestr(part_name, part_xml)
    fpath.write_bytes(buf.getvalue())
    return str(fpath)


class TestAnalyzeMesh3mf:
    """analyze_mesh reads a 3MF as real geometry, not an unsupported format."""

    def test_reports_real_triangle_count(self, tmp_path: Path) -> None:
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model()))

        assert result.triangle_count == _PYRAMID_TRIANGLES
        assert result.vertex_count == 5
        assert not any("Unsupported format" in i for i in result.printability_issues)

    def test_measures_geometry(self, tmp_path: Path) -> None:
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model()))

        assert result.dimensions_mm == {
            "width_mm": 10.0,
            "depth_mm": 10.0,
            "height_mm": 10.0,
        }
        assert result.volume_mm3 == pytest.approx(_PYRAMID_VOLUME, rel=1e-3)

    def test_build_item_transform_moves_geometry(self, tmp_path: Path) -> None:
        # 3MF transform: row-major 3x3 then translation (m30 m31 m32).
        build = '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 -20 5" />'
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(build)))

        assert result.triangle_count == _PYRAMID_TRIANGLES
        assert result.bounding_box is not None
        assert result.bounding_box["x_min"] == pytest.approx(100.0)
        assert result.bounding_box["y_min"] == pytest.approx(-20.0)
        assert result.bounding_box["z_min"] == pytest.approx(5.0)
        assert result.bounding_box["z_max"] == pytest.approx(15.0)

    def test_build_item_rotation_is_applied(self, tmp_path: Path) -> None:
        # Rotate 90 degrees about X: y -> z, z -> -y.  Apex (5, 5, 10)
        # lands at (5, -10, 5); the pyramid is now 10 wide, 10 deep, 10 tall
        # but spans y in [-10, 0].
        build = '<item objectid="1" transform="1 0 0 0 0 1 0 -1 0 0 0 0" />'
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(build)))

        assert result.bounding_box is not None
        assert result.bounding_box["y_min"] == pytest.approx(-10.0)
        assert result.bounding_box["y_max"] == pytest.approx(0.0)
        assert result.bounding_box["z_max"] == pytest.approx(10.0)

    def test_repeated_build_item_counts_every_instance(self, tmp_path: Path) -> None:
        build = (
            '<item objectid="1" />\n'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 50 0 0" />'
        )
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(build)))

        assert result.triangle_count == 2 * _PYRAMID_TRIANGLES
        assert result.dimensions_mm is not None
        assert result.dimensions_mm["width_mm"] == 60.0

    def test_component_transform_composes_with_build_transform(
        self, tmp_path: Path
    ) -> None:
        resources = (
            f'<object id="1" type="model">{_PYRAMID_MESH}</object>\n'
            '<object id="2" type="model">\n'
            "  <components>\n"
            '    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 10" />\n'
            "  </components>\n"
            "</object>"
        )
        build = '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 10" />'
        result = analyze_mesh(_write_3mf(tmp_path, _model_xml(resources, build)))

        assert result.triangle_count == _PYRAMID_TRIANGLES
        assert result.bounding_box is not None
        assert result.bounding_box["z_min"] == pytest.approx(20.0)
        assert result.bounding_box["z_max"] == pytest.approx(30.0)

    def test_empty_archive_reports_no_geometry(self, tmp_path: Path) -> None:
        result = analyze_mesh(
            _write_3mf(tmp_path, _model_xml("", ""))
        )

        assert result.triangle_count == 0
        assert result.printability_issues
        assert not any("Unsupported format" in i for i in result.printability_issues)

    def test_not_a_zip_reports_parse_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.3mf"
        bad.write_bytes(b"this is not a zip archive")

        result = analyze_mesh(str(bad))

        assert result.triangle_count == 0
        assert result.printability_issues
        assert not any("Unsupported format" in i for i in result.printability_issues)


class TestOtherDoors3mf:
    """Every extension dispatcher in validation.py reads the same 3MF."""

    def test_validate_mesh_accepts_3mf(self, tmp_path: Path) -> None:
        result = validate_mesh(_write_3mf(tmp_path, _pyramid_model()))

        assert result.valid, result.errors
        assert result.triangle_count == _PYRAMID_TRIANGLES

    def test_estimate_support_volume_accepts_3mf(self, tmp_path: Path) -> None:
        result = estimate_support_volume(_write_3mf(tmp_path, _pyramid_model()))

        assert result["total_triangles"] == _PYRAMID_TRIANGLES

    def test_convert_to_stl_accepts_3mf(self, tmp_path: Path) -> None:
        stl_path = convert_to_stl(_write_3mf(tmp_path, _pyramid_model()))

        assert Path(stl_path).suffix == ".stl"
        assert analyze_mesh(stl_path).triangle_count == _PYRAMID_TRIANGLES

    def test_printability_loader_accepts_3mf(self, tmp_path: Path) -> None:
        from kiln.printability import _parse_mesh

        triangles, vertices = _parse_mesh(_write_3mf(tmp_path, _pyramid_model()))

        assert len(triangles) == _PYRAMID_TRIANGLES
        assert len(vertices) == 5

    def test_auto_orient_loader_accepts_3mf(self, tmp_path: Path) -> None:
        from kiln.auto_orient import _parse_mesh

        triangles, vertices = _parse_mesh(_write_3mf(tmp_path, _pyramid_model()))

        assert len(triangles) == _PYRAMID_TRIANGLES
        assert len(vertices) == 5

    def test_check_stability_accepts_3mf(self, tmp_path: Path) -> None:
        from kiln.auto_orient import check_stability

        result = check_stability(_write_3mf(tmp_path, _pyramid_model()))

        assert result.height_mm == pytest.approx(10.0)


class TestScaledAndCyclic3mf:
    """Transform scaling and component cycles."""

    def test_scaled_build_transform_scales_geometry(self, tmp_path: Path) -> None:
        build = '<item objectid="1" transform="2 0 0 0 2 0 0 0 2 0 0 0" />'
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(build)))

        assert result.dimensions_mm == {
            "width_mm": 20.0,
            "depth_mm": 20.0,
            "height_mm": 20.0,
        }
        assert result.volume_mm3 == pytest.approx(8 * _PYRAMID_VOLUME, rel=1e-3)

    def test_component_cycle_terminates_and_counts_each_object_once(
        self, tmp_path: Path
    ) -> None:
        resources = (
            f'<object id="1" type="model">{_PYRAMID_MESH}\n'
            '  <components><component objectid="2" /></components>\n'
            "</object>\n"
            f'<object id="2" type="model">{_PYRAMID_MESH}\n'
            '  <components><component objectid="1" /></components>\n'
            "</object>"
        )
        result = analyze_mesh(
            _write_3mf(tmp_path, _model_xml(resources, '<item objectid="1" />'))
        )

        assert result.triangle_count == 2 * _PYRAMID_TRIANGLES


class TestProductionExtension3mf:
    """Meshes stored in other model parts of the archive (``p:path``).

    This is how BambuStudio / OrcaSlicer project files, and therefore
    every MakerWorld download, are laid out: the root model holds only
    placements and component references, each mesh lives in its own
    ``3D/Objects/object_N.model``.
    """

    _PART = "3D/Objects/object_1.model"

    def _part_xml(self) -> str:
        # The part's object shares id 1 with the root object that
        # references it — ids are scoped per model part.
        return _model_xml(
            f'<object id="1" type="model">{_PYRAMID_MESH}</object>', ""
        )

    def test_component_in_other_part_is_followed(self, tmp_path: Path) -> None:
        resources = (
            '<object id="1" type="model">\n'
            "  <components>\n"
            f'    <component p:path="/{self._PART}" objectid="1"'
            ' transform="1 0 0 0 1 0 0 0 1 0 0 10" />\n'
            "  </components>\n"
            "</object>"
        )
        build = '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0" />'
        path = _write_3mf(
            tmp_path, _model_xml(resources, build), parts={self._PART: self._part_xml()}
        )

        result = analyze_mesh(path)

        assert result.triangle_count == _PYRAMID_TRIANGLES
        assert result.bounding_box is not None
        assert result.bounding_box["x_min"] == pytest.approx(5.0)
        assert result.bounding_box["z_min"] == pytest.approx(10.0)
        assert result.bounding_box["z_max"] == pytest.approx(20.0)

    def test_build_item_in_other_part_is_followed(self, tmp_path: Path) -> None:
        build = f'<item objectid="1" p:path="/{self._PART}" />'
        path = _write_3mf(
            tmp_path, _model_xml("", build), parts={self._PART: self._part_xml()}
        )

        result = analyze_mesh(path)

        assert result.triangle_count == _PYRAMID_TRIANGLES

    def test_extract_model_from_3mf_follows_parts_and_places_them(
        self, tmp_path: Path
    ) -> None:
        """The STL a user extracts is the plate as the slicer places it: the
        mesh behind ``p:path``, under the build item's transform."""
        from kiln.generation.validation import extract_model_from_3mf

        resources = (
            '<object id="2" type="model"><components>'
            f'    <component p:path="/{self._PART}" objectid="1" />'
            "</components></object>"
        )
        build = '<item objectid="2" transform="2 0 0 0 2 0 0 0 2 0 0 0" />'
        path = _write_3mf(
            tmp_path, _model_xml(resources, build), parts={self._PART: self._part_xml()}
        )

        result = extract_model_from_3mf(path, output_path=str(tmp_path / "out.stl"))

        assert result["triangle_count"] == _PYRAMID_TRIANGLES
        assert result["vertex_count"] == 5
        assert result["dimensions"] == {"x_mm": 20.0, "y_mm": 20.0, "z_mm": 20.0}

    def test_missing_part_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        build = '<item objectid="1" p:path="/3D/Objects/nope.model" />'
        result = analyze_mesh(_write_3mf(tmp_path, _model_xml("", build)))

        assert result.triangle_count == 0
        assert any("missing from the archive" in i for i in result.printability_issues)


class TestUnits3mf:
    """The model ``unit`` attribute scales geometry into millimetres."""

    def test_inch_model_measures_in_mm(self, tmp_path: Path) -> None:
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(unit="inch")))

        assert result.dimensions_mm == {
            "width_mm": 254.0,
            "depth_mm": 254.0,
            "height_mm": 254.0,
        }

    def test_unit_scales_build_translation_too(self, tmp_path: Path) -> None:
        build = '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 1 0 0" />'
        result = analyze_mesh(
            _write_3mf(tmp_path, _pyramid_model(build, unit="centimeter"))
        )

        assert result.bounding_box is not None
        assert result.bounding_box["x_min"] == pytest.approx(10.0)
        assert result.bounding_box["x_max"] == pytest.approx(110.0)

    def test_unknown_unit_is_reported(self, tmp_path: Path) -> None:
        result = analyze_mesh(_write_3mf(tmp_path, _pyramid_model(unit="furlong")))

        assert result.triangle_count == 0
        assert any("unit" in i.lower() for i in result.printability_issues)
