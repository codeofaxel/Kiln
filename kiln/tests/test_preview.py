"""Tests for mesh preview rendering."""

from __future__ import annotations

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
