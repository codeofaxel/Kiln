"""Tests for 3MF color parser."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from kiln.threemf_parser import (
    _parse_hex_color,
    parse_colored_3mf,
)

# ---------------------------------------------------------------------------
# Helper: build minimal 3MF archives in memory
# ---------------------------------------------------------------------------


def _make_3mf(tmp_path: Path, model_xml: str, *, name: str = "test.3mf") -> str:
    """Create a 3MF ZIP with the given model XML and return its path."""
    fpath = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
    fpath.write_bytes(buf.getvalue())
    return str(fpath)


_BASIC_VERTICES = """\
<vertices>
  <vertex x="0" y="0" z="0" />
  <vertex x="10" y="0" z="0" />
  <vertex x="0" y="10" z="0" />
  <vertex x="10" y="10" z="0" />
  <vertex x="5" y="5" z="10" />
</vertices>"""


# ---------------------------------------------------------------------------
# _parse_hex_color
# ---------------------------------------------------------------------------


class TestParseHexColor:
    """Hex color parsing — both 6-digit and 8-digit formats."""

    def test_six_digit(self) -> None:
        assert _parse_hex_color("#FF0000") == (255, 0, 0)

    def test_eight_digit_strips_alpha(self) -> None:
        assert _parse_hex_color("#00FF00FF") == (0, 255, 0)

    def test_lowercase(self) -> None:
        assert _parse_hex_color("#aabbcc") == (170, 187, 204)

    def test_none_returns_fallback(self) -> None:
        assert _parse_hex_color(None) == (170, 170, 170)

    def test_malformed_returns_fallback(self) -> None:
        assert _parse_hex_color("not-a-color") == (170, 170, 170)

    def test_custom_fallback(self) -> None:
        assert _parse_hex_color(None, fallback=(1, 2, 3)) == (1, 2, 3)

    def test_short_hex_rejected(self) -> None:
        assert _parse_hex_color("#FFF") == (170, 170, 170)


# ---------------------------------------------------------------------------
# parse_colored_3mf — basematerials (core spec)
# ---------------------------------------------------------------------------


class TestParseBasematerials:
    """3MF files using <basematerials> (core spec)."""

    def test_two_color_basematerials(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="1" p1="1" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="2" />
  </build>
</model>"""
        path = _make_3mf(tmp_path, xml)
        mesh = parse_colored_3mf(path)

        assert len(mesh.triangles) == 2
        assert mesh.colors_found is True
        assert mesh.color_count == 2
        # First triangle inherits object pindex=0 → Red
        assert mesh.triangles[0].color == (255, 0, 0)
        # Second triangle overrides to p1=1 → Blue
        assert mesh.triangles[1].color == (0, 0, 255)

    def test_eight_digit_hex_from_bambu(self, tmp_path: Path) -> None:
        """Bambu Studio writes 8-digit hex (#RRGGBBAA)."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="White" displaycolor="#FFFFFFFF" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert mesh.triangles[0].color == (255, 255, 255)


# ---------------------------------------------------------------------------
# parse_colored_3mf — colorgroup (materials extension)
# ---------------------------------------------------------------------------


class TestParseColorgroup:
    """3MF files using <m:colorgroup> (materials extension)."""

    def test_four_color_colorgroup(self, tmp_path: Path) -> None:
        """Multi-color (4 colors, like camo textures)."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
  <resources>
    <m:colorgroup id="1">
      <m:color color="#3B5323" />
      <m:color color="#556B2F" />
      <m:color color="#8B7355" />
      <m:color color="#2F4F2F" />
    </m:colorgroup>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="1" p1="1" />
          <triangle v1="0" v2="1" v3="4" pid="1" p1="2" />
          <triangle v1="2" v2="3" v3="4" pid="1" p1="3" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 4
        assert mesh.colors_found is True
        assert mesh.color_count == 4
        assert mesh.triangles[0].color == (59, 83, 35)
        assert mesh.triangles[1].color == (85, 107, 47)
        assert mesh.triangles[2].color == (139, 115, 85)
        assert mesh.triangles[3].color == (47, 79, 47)


# ---------------------------------------------------------------------------
# parse_colored_3mf — multi-object assemblies
# ---------------------------------------------------------------------------


class TestParseAssembly:
    """Multi-object 3MF assemblies using <components>."""

    def test_two_part_assembly(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Green" displaycolor="#00FF00" />
    </basematerials>
    <object id="10" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="20" type="model" pid="1" pindex="1">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="30" type="model">
      <components>
        <component objectid="10" />
        <component objectid="20" />
      </components>
    </object>
  </resources>
  <build><item objectid="30" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 2
        assert mesh.colors_found is True
        assert mesh.triangles[0].color == (255, 0, 0)
        assert mesh.triangles[1].color == (0, 255, 0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_no_color_data_uses_default(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 1
        assert mesh.colors_found is False
        assert mesh.triangles[0].color == (170, 170, 170)

    def test_custom_default_color(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, xml), default_color=(50, 50, 50),
        )
        assert mesh.triangles[0].color == (50, 50, 50)

    def test_missing_model_xml_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "empty.3mf"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "not a model")
        fpath.write_bytes(buf.getvalue())

        with pytest.raises(ValueError, match="No 3D model XML found"):
            parse_colored_3mf(str(fpath))

    def test_corrupt_xml_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "corrupt.3mf"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<not valid xml >>>")
        fpath.write_bytes(buf.getvalue())

        with pytest.raises(ValueError, match="Failed to parse 3MF model XML"):
            parse_colored_3mf(str(fpath))

    def test_file_not_found_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_colored_3mf("/nonexistent/path.3mf")

    def test_not_a_zip_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "notazip.3mf"
        fpath.write_text("this is not a zip file")
        with pytest.raises(zipfile.BadZipFile):
            parse_colored_3mf(str(fpath))

    def test_empty_mesh_returns_empty(self, tmp_path: Path) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices></vertices>
        <triangles></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert len(mesh.triangles) == 0
        assert mesh.colors_found is False

    def test_to_dict(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        d = mesh.to_dict()
        assert d["triangle_count"] == 1
        assert isinstance(d["triangles"], list)
        assert d["colors_found"] is False

    def test_invalid_vertex_indices_skipped(self, tmp_path: Path) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="1" y="0" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="99" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert len(mesh.triangles) == 0
