"""Tests for kiln.multicolor_3mf — multi-color 3MF composer."""

from __future__ import annotations

import os
import struct
import zipfile
from pathlib import Path

import pytest

from kiln.multicolor_3mf import (
    ColorPart,
    _parse_stl,
    compose_multicolor_3mf,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _binary_stl(tmp_path: Path, name: str, triangles: list[tuple] | None = None) -> Path:
    """Write a minimal valid binary STL.  Default: two triangles sharing 3 vertices."""
    if triangles is None:
        # Two back-to-back triangles that share all 3 vertices (tests dedup)
        triangles = [
            ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ]

    stl = tmp_path / name
    buf = bytearray(b"\x00" * 80)
    buf += struct.pack("<I", len(triangles))
    for tri in triangles:
        normal, v1, v2, v3 = tri
        buf += struct.pack("<fff", *normal)
        buf += struct.pack("<fff", *v1)
        buf += struct.pack("<fff", *v2)
        buf += struct.pack("<fff", *v3)
        buf += struct.pack("<H", 0)
    stl.write_bytes(bytes(buf))
    return stl


def _ascii_stl(tmp_path: Path, name: str) -> Path:
    """Write a minimal valid ASCII STL."""
    content = (
        "solid test\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid test\n"
    )
    stl = tmp_path / name
    stl.write_text(content)
    return stl


@pytest.fixture
def stl_a(tmp_path: Path) -> Path:
    """Binary STL with 2 triangles sharing 3 unique vertices."""
    return _binary_stl(tmp_path, "part_a.stl")


@pytest.fixture
def stl_b(tmp_path: Path) -> Path:
    """Binary STL at a different position — 1 triangle, 3 unique vertices."""
    triangles = [
        ((0.0, 1.0, 0.0), (5.0, 5.0, 5.0), (6.0, 5.0, 5.0), (5.0, 6.0, 5.0)),
    ]
    return _binary_stl(tmp_path, "part_b.stl", triangles)


@pytest.fixture
def stl_c(tmp_path: Path) -> Path:
    """A third part for 3-color tests."""
    triangles = [
        ((1.0, 0.0, 0.0), (10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (10.0, 1.0, 0.0)),
    ]
    return _binary_stl(tmp_path, "part_c.stl", triangles)


@pytest.fixture
def ascii_stl(tmp_path: Path) -> Path:
    return _ascii_stl(tmp_path, "ascii.stl")


# ---------------------------------------------------------------------------
# _parse_stl — binary
# ---------------------------------------------------------------------------


def test_parse_binary_triangle_count(stl_a: Path):
    _, triangles = _parse_stl(str(stl_a))
    assert len(triangles) == 2


def test_parse_binary_deduplicates_shared_vertices(stl_a: Path):
    """Two identical triangles share 3 vertices → only 3 unique after dedup."""
    vertices, triangles = _parse_stl(str(stl_a))
    assert len(vertices) == 3
    assert len(set(vertices)) == len(vertices)


def test_parse_binary_distinct_vertices_no_dedup(stl_b: Path):
    """Single triangle with 3 unique vertices → 3 vertices in output."""
    vertices, triangles = _parse_stl(str(stl_b))
    assert len(vertices) == 3
    assert len(triangles) == 1


def test_parse_binary_triangle_indices_valid(stl_a: Path):
    vertices, triangles = _parse_stl(str(stl_a))
    for v1, v2, v3 in triangles:
        assert 0 <= v1 < len(vertices)
        assert 0 <= v2 < len(vertices)
        assert 0 <= v3 < len(vertices)


# ---------------------------------------------------------------------------
# _parse_stl — ASCII
# ---------------------------------------------------------------------------


def test_parse_ascii_triangle_count(ascii_stl: Path):
    _, triangles = _parse_stl(str(ascii_stl))
    assert len(triangles) == 1


def test_parse_ascii_vertices(ascii_stl: Path):
    vertices, _ = _parse_stl(str(ascii_stl))
    assert len(vertices) == 3


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — input validation
# ---------------------------------------------------------------------------


def test_compose_empty_parts_returns_error():
    result = compose_multicolor_3mf([])
    assert result["success"] is False
    assert "No parts" in result["error"]


def test_compose_missing_stl_returns_error(tmp_path: Path):
    result = compose_multicolor_3mf([
        ColorPart(stl_path=str(tmp_path / "ghost.stl"), extruder=1),
    ])
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_compose_extruder_zero_returns_error(stl_a: Path):
    result = compose_multicolor_3mf([
        ColorPart(stl_path=str(stl_a), extruder=0),
    ])
    assert result["success"] is False
    assert "extruder" in result["error"].lower()


def test_compose_extruder_negative_returns_error(stl_a: Path):
    result = compose_multicolor_3mf([
        ColorPart(stl_path=str(stl_a), extruder=-1),
    ])
    assert result["success"] is False


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — output: valid ZIP / 3MF structure
# ---------------------------------------------------------------------------


def test_compose_creates_file(stl_a: Path, stl_b: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=2)],
        output_path=out,
    )
    assert result["success"] is True
    assert os.path.isfile(out)


def test_compose_output_is_valid_zip(stl_a: Path, stl_b: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=2)],
        output_path=out,
    )
    assert zipfile.is_zipfile(out)


def test_compose_required_files_in_zip(stl_a: Path, stl_b: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=2)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "3D/3dmodel.model" in names
    assert "Metadata/model_settings.config" in names


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — 3dmodel.model content
# ---------------------------------------------------------------------------


def test_compose_model_xml_has_all_objects(stl_a: Path, stl_b: Path, stl_c: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name="body"),
         ColorPart(stl_path=str(stl_b), extruder=2, name="accent"),
         ColorPart(stl_path=str(stl_c), extruder=3, name="detail")],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert 'id="1"' in xml
    assert 'id="2"' in xml
    assert 'id="3"' in xml
    assert "body" in xml
    assert "accent" in xml
    assert "detail" in xml


def test_compose_model_xml_has_vertices_and_triangles(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert "<vertices>" in xml
    assert "<triangles>" in xml
    assert "<vertex x=" in xml
    assert "<triangle v1=" in xml


def test_compose_slic3rpe_extruder_in_build_section(stl_a: Path, stl_b: Path, tmp_path: Path):
    """PrusaSlicer reads extruder from slic3rpe:extruder attribute on <item>."""
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=3)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert 'slic3rpe:extruder="1"' in xml
    assert 'slic3rpe:extruder="3"' in xml


def test_compose_identity_transform_in_build(stl_a: Path, tmp_path: Path):
    """Parts must carry identity transform (no translation applied)."""
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    # Identity rotation + zero translation. Translations are always formatted as floats.
    assert "1 0 0 0 1 0 0 0 1 0.000000 0.000000 0.000000" in xml


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — model_settings.config (BambuStudio)
# ---------------------------------------------------------------------------


def test_compose_bambu_settings_has_extruder_keys(stl_a: Path, stl_b: Path, tmp_path: Path):
    """BambuStudio reads extruder from model_settings.config."""
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=2)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert 'key="extruder"' in cfg
    assert 'value="1"' in cfg
    assert 'value="2"' in cfg


def test_compose_bambu_settings_has_name(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name="coaster_body")],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert "coaster_body" in cfg


def test_compose_color_hex_in_settings(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, color="#AABBCC")],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert "AABBCC" in cfg


def test_compose_material_in_settings(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, material="PLA Grey")],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert "PLA Grey" in cfg


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — return values
# ---------------------------------------------------------------------------


def test_compose_returns_correct_counts(stl_a: Path, stl_b: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1),
         ColorPart(stl_path=str(stl_b), extruder=2)],
        output_path=out,
    )
    assert result["parts"] == 2
    assert result["total_triangles"] == 3   # 2 from stl_a + 1 from stl_b
    assert result["total_vertices"] == 6    # 3 unique per STL (no sharing across parts)


def test_compose_extruder_map_in_result(stl_a: Path, stl_b: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name="body"),
         ColorPart(stl_path=str(stl_b), extruder=2, name="accent")],
        output_path=out,
    )
    assert "extruder_map" in result
    assert "1" in result["extruder_map"]
    assert "2" in result["extruder_map"]


def test_compose_message_mentions_slicer_compat(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "out.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    assert "BambuStudio" in result["message"] or "bambu" in result["message"].lower()


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — edge cases
# ---------------------------------------------------------------------------


def test_compose_single_part_works(stl_a: Path, tmp_path: Path):
    """Single-part 3MF is valid (degenerate multi-color case)."""
    out = str(tmp_path / "single.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name="only")],
        output_path=out,
    )
    assert result["success"] is True
    assert result["parts"] == 1


def test_compose_four_parts_ams(stl_a: Path, stl_b: Path, stl_c: Path, tmp_path: Path):
    """Simulate full 4-slot AMS setup."""
    stl_d = _binary_stl(tmp_path, "part_d.stl", [
        ((0.0, 0.0, -1.0), (20.0, 0.0, 0.0), (21.0, 0.0, 0.0), (20.0, 1.0, 0.0)),
    ])
    out = str(tmp_path / "four_color.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name="slot1"),
         ColorPart(stl_path=str(stl_b), extruder=2, name="slot2"),
         ColorPart(stl_path=str(stl_c), extruder=3, name="slot3"),
         ColorPart(stl_path=str(stl_d), extruder=4, name="slot4")],
        output_path=out,
    )
    assert result["success"] is True
    assert result["parts"] == 4
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    for slot in range(1, 5):
        assert f'slic3rpe:extruder="{slot}"' in xml


def test_compose_default_output_path(stl_a: Path):
    """No output_path → temp file created."""
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
    )
    assert result["success"] is True
    assert result["output_path"].endswith(".3mf")
    assert os.path.isfile(result["output_path"])
    os.unlink(result["output_path"])  # cleanup


def test_compose_ascii_stl_works(ascii_stl: Path, tmp_path: Path):
    """ASCII STL files should parse and compose without error."""
    out = str(tmp_path / "ascii_out.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(ascii_stl), extruder=1, name="ascii_part")],
        output_path=out,
    )
    assert result["success"] is True
    assert result["total_triangles"] == 1


def test_compose_xml_escapes_special_chars(stl_a: Path, tmp_path: Path):
    """Names with XML-special characters must be escaped in output."""
    out = str(tmp_path / "escaped.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name='part <"body"> & more')],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
        cfg = zf.read("Metadata/model_settings.config").decode()
    # Raw special chars must NOT appear unescaped
    assert '<"body">' not in xml
    assert '<"body">' not in cfg
    # Escaped forms should be present
    assert "&lt;" in xml or "&lt;" in cfg


def test_compose_output_path_returned(stl_a: Path, tmp_path: Path):
    out = str(tmp_path / "check_path.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    assert result["output_path"] == out


# ---------------------------------------------------------------------------
# Material safety integration
# ---------------------------------------------------------------------------


def test_compose_no_material_no_safety_keys(stl_a: Path, tmp_path: Path):
    """Parts without material field → no safety_level in result."""
    out = str(tmp_path / "no_mat.3mf")
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    assert result["success"] is True
    assert "safety_level" not in result


def test_compose_compatible_materials_ok(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Compatible materials (PLA + PLA) → safety_level='ok', succeeds."""
    out = str(tmp_path / "compat.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="PLA"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    assert result["safety_level"] == "ok"
    assert "✅" in result["safety_message"]
    assert result.get("hardware_warnings", []) == []


def test_compose_incompatible_materials_blocked(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Incompatible materials (PLA + ABS) → success=False, always blocked."""
    out = str(tmp_path / "incompat.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="ABS"),
        ],
        output_path=out,
    )
    assert result["success"] is False
    assert "⛔" in result["error"]
    assert "safety" in result
    assert result["safety"]["safe"] is False


def test_compose_incompatible_block_surfaces_hardware_warnings(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    """Even on a hard block, hardware_warnings should be in the error response."""
    out = str(tmp_path / "block_hw.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="ABS"),
        ],
        output_path=out,
    )
    assert result["success"] is False
    assert "hardware_warnings" in result
    assert any("enclosure" in w.lower() for w in result["hardware_warnings"])


def test_compose_caution_materials_succeed_with_warning(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Caution-level materials (PLA + TPU) → succeeds with safety_level='caution'."""
    out = str(tmp_path / "caution.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="TPU"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    assert result["safety_level"] == "caution"
    assert "⚠️" in result["safety_message"]


def test_compose_flush_matrix_embedded_in_3mf(stl_a: Path, stl_b: Path, tmp_path: Path):
    """When materials are specified, project_settings.config with flush matrix is in the ZIP."""
    out = str(tmp_path / "flush.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="PLA"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    assert result.get("flush_matrix_embedded") is True
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "Metadata/project_settings.config" in names
        cfg = zf.read("Metadata/project_settings.config").decode()
        assert "flush_volumes_matrix" in cfg


def test_compose_flush_matrix_has_16_values(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Flush matrix for 4-slot AMS Lite = 4×4 = 16 values."""
    import json

    out = str(tmp_path / "flush16.3mf")
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="PLA"),
        ],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        cfg = json.loads(zf.read("Metadata/project_settings.config").decode())
    assert len(cfg["flush_volumes_matrix"]) == 16


def test_compose_no_flush_matrix_without_materials(stl_a: Path, tmp_path: Path):
    """No materials → no project_settings.config in ZIP."""
    out = str(tmp_path / "no_flush.3mf")
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=out,
    )
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "Metadata/project_settings.config" not in names


def test_compose_hardware_warnings_in_result(stl_a: Path, stl_b: Path, tmp_path: Path):
    """PLA-CF triggers a hardened nozzle warning in the result."""
    out = str(tmp_path / "hw_warn.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, material="PLA"),
            ColorPart(stl_path=str(stl_b), extruder=2, material="PLA-CF"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    assert "hardware_warnings" in result
    assert any("hardened" in w.lower() for w in result["hardware_warnings"])


# ---------------------------------------------------------------------------
# Spec-visible colors — colorgroup + per-triangle references
# ---------------------------------------------------------------------------


def _model_xml(path: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("3D/3dmodel.model").decode("utf-8")


def test_compose_writes_spec_visible_colorgroup(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Colors land where spec-compliant readers look, not only in the
    slicer sidecar — one colorgroup entry per distinct part color, and a
    reference on every triangle of a colored object (the exact shape
    three.js' 3MFLoader bakes to vertex colors)."""
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#f72323"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="2366F7"),  # bare hex too
        ],
        output_path=str(out),
    )
    xml = _model_xml(str(out))
    assert '<m:colorgroup id="3">' in xml  # objects 1..2, group takes the next id
    assert '<m:color color="#F72323"/>' in xml
    assert '<m:color color="#2366F7"/>' in xml
    assert 'pid="3" p1="0"' in xml and 'pid="3" p1="1"' in xml
    # Deliberately NO object-level pid — the proven three.js shape is
    # per-triangle references only.
    assert "pindex=" not in xml


def test_compose_shares_one_palette_entry_per_distinct_color(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#F72323"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="#f72323"),
        ],
        output_path=str(out),
    )
    assert _model_xml(str(out)).count("<m:color ") == 1


def test_compose_without_colors_writes_no_colorgroup(stl_a: Path, tmp_path: Path):
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)], output_path=str(out),
    )
    xml = _model_xml(str(out))
    assert "colorgroup" not in xml
    assert "p1=" not in xml


def test_compose_invalid_color_hint_is_not_a_color_claim(stl_a: Path, tmp_path: Path):
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, color="not-a-color")],
        output_path=str(out),
    )
    assert "colorgroup" not in _model_xml(str(out))


def test_spec_colors_survive_without_the_sidecar(stl_a: Path, stl_b: Path, tmp_path: Path):
    """The 2026-08-01 gap, closed at the source: strip the slicer sidecar
    and a spec reader still sees every part color."""
    from kiln.threemf_parser import parse_colored_3mf

    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#F72323"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="#2366F7"),
        ],
        output_path=str(out),
    )
    stripped = tmp_path / "stripped.3mf"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(stripped, "w") as dst:
        for name in src.namelist():
            if name != "Metadata/model_settings.config":
                dst.writestr(name, src.read(name))
    mesh = parse_colored_3mf(str(stripped))
    assert mesh.colors_found is True
    assert {t.color for t in mesh.triangles} == {(247, 35, 35), (35, 102, 247)}


# ---------------------------------------------------------------------------
# Thumbnail — the colored render, with the compose never held hostage
# ---------------------------------------------------------------------------


def test_thumbnail_is_the_colored_render(stl_a: Path, stl_b: Path, tmp_path: Path):
    """The embedded plate_1.png shows the parts in their real colors —
    a grey thumbnail undersells a multicolor print on every slicer LCD."""
    Image = pytest.importorskip("PIL.Image", reason="colored renderer needs PIL")

    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#F72323"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="#2366F7"),
        ],
        output_path=str(out),
    )
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/plate_1.png" in zf.namelist()
        import io

        img = Image.open(io.BytesIO(zf.read("Metadata/plate_1.png"))).convert("RGB")
    pixels = [img.getpixel((x, y)) for y in range(0, img.height, 4) for x in range(0, img.width, 4)]
    reddish = sum(1 for r, g, b in pixels if r > 140 and g < 90 and b < 90)
    bluish = sum(1 for r, g, b in pixels if b > 140 and r < 90 and g < 110)
    assert reddish > 0 and bluish > 0, (
        "the embedded thumbnail carries neither part color — it regressed to grey"
    )


def test_thumbnail_failure_never_fails_the_compose(
    stl_a: Path, tmp_path: Path, monkeypatch
):
    import kiln.multicolor_3mf as m3

    monkeypatch.setattr(
        m3, "_render_colored_thumbnail",
        lambda parsed: (_ for _ in ()).throw(RuntimeError("no renderer")),
    )
    monkeypatch.setattr(m3, "_generate_thumbnail_openscad", lambda paths: None)
    out = tmp_path / "out.3mf"
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, color="#F72323")],
        output_path=str(out),
    )
    assert result["success"] is True
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/plate_1.png" not in zf.namelist()


# ---------------------------------------------------------------------------
# The PrusaSlicer channel and the Bambu-family version stamp
# ---------------------------------------------------------------------------


def test_compose_writes_the_prusa_model_config(stl_a: Path, stl_b: Path, tmp_path: Path):
    """PrusaSlicer reads per-object extruders ONLY from
    Metadata/Slic3r_PE_model.config — without it a multicolor 3MF prints
    entirely with extruder 1 (measured: zero tool changes, second filament
    0.00 mm).  Every object gets a full-range volume plus the extruder at
    both volume and object level."""
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, name="zone_0"),
            ColorPart(stl_path=str(stl_b), extruder=2, name="zone_1"),
        ],
        output_path=str(out),
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/Slic3r_PE_model.config").decode()
    # stl_a has 2 triangles (deduped), stl_b has 1
    assert '<object id="1" instances_count="1">' in cfg
    assert '<volume firstid="0" lastid="1">' in cfg
    assert '<object id="2" instances_count="1">' in cfg
    assert '<volume firstid="0" lastid="0">' in cfg
    for level in ("volume", "object"):
        assert f'<metadata type="{level}" key="extruder" value="1"/>' in cfg
        assert f'<metadata type="{level}" key="extruder" value="2"/>' in cfg
    assert 'value="zone_0"' in cfg and 'value="zone_1"' in cfg


def test_prusa_config_escapes_part_names(stl_a: Path, tmp_path: Path):
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1, name='a<b>&"c"')],
        output_path=str(out),
    )
    with zipfile.ZipFile(out) as zf:
        cfg = zf.read("Metadata/Slic3r_PE_model.config").decode()
    assert "a&lt;b&gt;&amp;&quot;c&quot;" in cfg
    assert "<b>" not in cfg


def test_compose_stamps_the_bambu_family_version(stl_a: Path, tmp_path: Path):
    """Without the stamp OrcaSlicer misreads the file as 'generated by an
    old OrcaSlicer version' and warns it loads geometry only.  The key is
    inert in both forks' readers (sets an integer, never the is-project
    flag), so BambuStudio's third-party color import is untouched."""
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)], output_path=str(out),
    )
    assert '<metadata name="BambuStudio:3mfVersion">1</metadata>' in _model_xml(str(out))
