"""Tests for mesh preview rendering."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from kiln.preview import render_multi_view_preview


def test_render_multi_view_preview_svg(tmp_path: Path) -> None:
    stl = tmp_path / "triangle.stl"
    stl.write_text(
        "\n".join(
            [
                "solid tri",
                "facet normal 0 0 1",
                "  outer loop",
                "    vertex 0 0 0",
                "    vertex 10 0 0",
                "    vertex 0 10 0",
                "  endloop",
                "endfacet",
                "endsolid tri",
            ]
        ),
        encoding="utf-8",
    )

    out = tmp_path / "preview.svg"
    result = render_multi_view_preview(str(stl), output_path=str(out))

    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<svg" in content
    assert "Isometric" in content
    assert result.path == str(out)
    assert result.format == "svg"
    assert result.views == ["isometric", "dimetric", "trimetric"]


def test_render_3mf_with_colors_svg(tmp_path: Path) -> None:
    """Colored 3MF files produce SVGs with per-face colors (not orange)."""
    model_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="10" y="0" z="0" />
          <vertex x="0" y="10" z="0" />
          <vertex x="10" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="1" p1="1" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""

    fpath = tmp_path / "colored.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
    fpath.write_bytes(buf.getvalue())

    out = tmp_path / "colored_preview.svg"
    result = render_multi_view_preview(str(fpath), output_path=str(out))

    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<svg" in content
    # Should NOT have the hardcoded orange stroke — colored faces use their own
    assert "#b66342" not in content
    assert result.triangle_count == 2


def test_render_3mf_without_colors_uses_orange(tmp_path: Path) -> None:
    """Colorless 3MF falls back to standard orange rendering."""
    model_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="10" y="0" z="0" />
          <vertex x="0" y="10" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

    fpath = tmp_path / "plain.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
    fpath.write_bytes(buf.getvalue())

    out = tmp_path / "plain_preview.svg"
    result = render_multi_view_preview(str(fpath), output_path=str(out))

    content = out.read_text(encoding="utf-8")
    # Should have the orange stroke since no colors
    assert "#b66342" in content
    assert result.triangle_count == 1
