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


def test_compose_accepts_css_shorthand_hex(stl_a: Path, stl_b: Path, tmp_path: Path):
    """``#F00`` is a color claim.  It used to fail the six/eight-digit
    parse, and a rejected hint is SILENT — the colorgroup was omitted
    while the tool still returned success, still summarised the requested
    colors and still mapped AMS slots, so the file came out grey with
    nothing telling the user why."""
    out = tmp_path / "out.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#F00"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="06fc"),  # RGBA shorthand, bare
        ],
        output_path=str(out),
    )
    xml = _model_xml(str(out))
    assert '<m:color color="#FF0000"/>' in xml
    assert '<m:color color="#0066FF"/>' in xml


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
    monkeypatch.setattr(
        m3, "_generate_thumbnail_openscad", lambda paths, offsets=None: None,
    )
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


# ---------------------------------------------------------------------------
# compose_painted_3mf — one watertight object, colors per triangle
# ---------------------------------------------------------------------------


def _cube_soup(edge: float = 10.0):
    """A closed cube as a triangle soup (12 triangles, outward winding)."""
    lo, hi = 0.0, edge
    c = {
        (x, y, z): (lo if x == 0 else hi, lo if y == 0 else hi, lo if z == 0 else hi)
        for x in (0, 1) for y in (0, 1) for z in (0, 1)
    }
    quads = [
        # (corner indices as (x,y,z) bits), outward winding per face
        (((0,0,0), (0,1,0), (1,1,0), (1,0,0))),   # bottom (z=lo, -z)
        (((0,0,1), (1,0,1), (1,1,1), (0,1,1))),   # top (+z)
        (((0,0,0), (1,0,0), (1,0,1), (0,0,1))),   # front (-y)
        (((1,0,0), (1,1,0), (1,1,1), (1,0,1))),   # right (+x)
        (((1,1,0), (0,1,0), (0,1,1), (1,1,1))),   # back (+y)
        (((0,1,0), (0,0,0), (0,0,1), (0,1,1))),   # left (-x)
    ]
    tris = []
    for a, b, c2, d in quads:
        tris.append((c[a], c[b], c[c2]))
        tris.append((c[a], c[c2], c[d]))
    return tris


def _painted_model_xml(path: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("3D/3dmodel.model").decode("utf-8")


class TestComposePainted:
    def test_one_watertight_object_with_per_triangle_colors(self, tmp_path: Path):
        from kiln.multicolor_3mf import compose_painted_3mf

        tris = _cube_soup()
        colors = ["#F72323" if i < 2 else "#2366F7" for i in range(len(tris))]
        out = tmp_path / "painted.3mf"
        result = compose_painted_3mf(tris, colors, output_path=str(out))
        assert result["success"] is True
        assert result["form"] == "painted_single_object"
        # exact-coordinate dedup: a closed cube has 8 vertices, not 36
        assert result["total_vertices"] == 8
        assert result["total_triangles"] == 12
        assert result["colors"] == ["#F72323", "#2366F7"]

        xml = _painted_model_xml(str(out))
        assert xml.count("<object ") == 1
        assert xml.count('pid="2" p1="0"') == 2   # the two red faces
        assert xml.count('pid="2" p1="1"') == 10
        assert '<metadata name="BambuStudio:3mfVersion">1</metadata>' in xml

        # The emitted object is closed: every directed edge appears exactly
        # once with its reverse — the property slicers call manifold.
        import re

        tri_rows = re.findall(
            r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', xml,
        )
        edges: dict[tuple[str, str], int] = {}
        for a, b, c in tri_rows:
            for e in ((a, b), (b, c), (c, a)):
                edges[e] = edges.get(e, 0) + 1
        assert all(count == 1 for count in edges.values())
        assert all((b, a) in edges for (a, b) in edges)

    def test_uncolored_triangles_carry_no_reference(self, tmp_path: Path):
        from kiln.multicolor_3mf import compose_painted_3mf

        tris = _cube_soup()
        colors: list = ["#F72323"] * 2 + [None] * 10
        out = tmp_path / "partial.3mf"
        result = compose_painted_3mf(tris, colors, output_path=str(out))
        assert result["success"] is True
        xml = _painted_model_xml(str(out))
        assert xml.count("p1=") == 2

    def test_slicer_channels_are_present(self, tmp_path: Path):
        from kiln.multicolor_3mf import compose_painted_3mf

        out = tmp_path / "p.3mf"
        compose_painted_3mf(
            _cube_soup(), ["#F72323"] * 12, output_path=str(out), name="cube",
        )
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "Metadata/model_settings.config" in names
            assert "Metadata/Slic3r_PE_model.config" in names
            cfg = zf.read("Metadata/Slic3r_PE_model.config").decode()
        assert '<volume firstid="0" lastid="11">' in cfg

    def test_refuses_dishonest_input(self, tmp_path: Path):
        from kiln.multicolor_3mf import compose_painted_3mf

        assert compose_painted_3mf([], [], output_path=str(tmp_path / "x.3mf"))[
            "success"
        ] is False
        result = compose_painted_3mf(
            _cube_soup(), ["#F72323"], output_path=str(tmp_path / "y.3mf"),
        )
        assert result["success"] is False
        assert "length" in result["error"]

    def test_thumbnail_is_embedded(self, tmp_path: Path):
        pytest.importorskip("PIL.Image", reason="colored renderer needs PIL")
        from kiln.multicolor_3mf import compose_painted_3mf

        out = tmp_path / "t.3mf"
        compose_painted_3mf(_cube_soup(), ["#F72323"] * 12, output_path=str(out))
        with zipfile.ZipFile(out) as zf:
            assert "Metadata/plate_1.png" in zf.namelist()


# ---------------------------------------------------------------------------
# Native painting channel — the slicers' own per-triangle state strings
# ---------------------------------------------------------------------------


def test_painted_state_string_canonical_values():
    """Pin the exact wire strings, as data.

    Derived from TriangleSelector::serialize + FacetsAnnotation::
    get_triangle_as_string (prusa3d/PrusaSlicer, src/libslic3r/
    TriangleSelector.cpp + Model.cpp): an unsplit triangle painted state n
    is one nibble ``n << 2`` for n in 1..2, else two nibbles ``0xC`` then
    ``n - 3``, hex-encoded nibble-REVERSED.  The decoder in
    kiln.threemf_parser pins this same canon — a divergence must fail here,
    not disagree silently."""
    from kiln.multicolor_3mf import painted_state_string

    assert painted_state_string(1) == "4"     # 1 << 2, single nibble
    assert painted_state_string(2) == "8"     # 2 << 2, single nibble
    assert painted_state_string(3) == "0C"    # first two-nibble state
    assert painted_state_string(4) == "1C"
    assert painted_state_string(16) == "DC"   # the shared ceiling


def test_painted_state_string_matches_orca_const_filaments_table():
    """OrcaSlicer's own Model.cpp carries the same canon as a literal
    (CONST_FILAMENTS, filaments 1..16) — the independent cross-check."""
    from kiln.multicolor_3mf import painted_state_string

    orca_const_filaments = [
        "4", "8", "0C", "1C", "2C", "3C", "4C", "5C",
        "6C", "7C", "8C", "9C", "AC", "BC", "CC", "DC",
    ]
    assert [painted_state_string(k) for k in range(1, 17)] == orca_const_filaments


def test_painted_state_string_refuses_out_of_range():
    """State 0 is 'unpainted' (no attribute, never a string), and the
    Bambu/Orca fork's decoder reads nothing past state 16 — encoding 17+
    would repaint triangles or crash a reader."""
    from kiln.multicolor_3mf import PAINTED_STATE_MAX, painted_state_string

    assert PAINTED_STATE_MAX == 16
    for bad in (0, -1, 17, 255):
        with pytest.raises(ValueError):
            painted_state_string(bad)


def test_painted_triangles_carry_both_native_spellings(tmp_path: Path):
    """Every colored triangle carries the SAME serialized state under both
    attribute names — slic3rpe:mmu_segmentation (PrusaSlicer's reader has
    zero colorgroup handling; this is the only channel it sees) and
    paint_color (BambuStudio/OrcaSlicer).  Measured proof: this exact shape
    took a two-color probe from 0 tool changes / one filament to 2 tool
    changes / both filaments in PrusaSlicer CLI, and BambuStudio's own
    export re-emits the identical strings."""
    import re

    from kiln.multicolor_3mf import compose_painted_3mf

    tris = _cube_soup()
    colors = ["#F72323" if i < 2 else "#2366F7" for i in range(len(tris))]
    out = tmp_path / "native.3mf"
    result = compose_painted_3mf(tris, colors, output_path=str(out))
    assert result["success"] is True
    assert "native_paint_truncated" not in result

    xml = _painted_model_xml(str(out))
    # The attribute's namespace prefix must actually be declared.
    assert 'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"' in xml
    rows = re.findall(r"<triangle [^>]*/>", xml)
    assert len(rows) == 12
    # Palette index 0 -> state 1 -> "4"; index 1 -> state 2 -> "8".
    red = [r for r in rows if 'p1="0"' in r]
    blue = [r for r in rows if 'p1="1"' in r]
    assert len(red) == 2 and len(blue) == 10
    for row in red:
        assert 'slic3rpe:mmu_segmentation="4"' in row
        assert 'paint_color="4"' in row
    for row in blue:
        assert 'slic3rpe:mmu_segmentation="8"' in row
        assert 'paint_color="8"' in row


def test_painted_uncolored_triangles_carry_no_native_state(tmp_path: Path):
    """Unpainted means NO attribute — a state-0 string does not exist in
    the wire format (serialize stores nothing for NONE)."""
    import re

    from kiln.multicolor_3mf import compose_painted_3mf

    tris = _cube_soup()
    colors: list = ["#F72323"] * 2 + [None] * 10
    out = tmp_path / "partial_native.3mf"
    compose_painted_3mf(tris, colors, output_path=str(out))
    xml = _painted_model_xml(str(out))
    rows = re.findall(r"<triangle [^>]*/>", xml)
    bare = [r for r in rows if "p1=" not in r]
    assert len(bare) == 10
    for row in bare:
        assert "mmu_segmentation" not in row
        assert "paint_color" not in row


def test_painted_palette_beyond_native_cap_truncates_honestly(tmp_path: Path):
    """A 17th color cannot ride the native channel (state 17 exceeds the
    fork-shared ceiling).  Its triangles keep the spec colorgroup
    reference, get NO native state (a wrong state would repaint them with
    someone else's filament), and the truncation is surfaced."""
    import re

    from kiln.multicolor_3mf import compose_painted_3mf

    lower = _cube_soup()
    upper = [
        tuple((x, y, z + 10.0) for x, y, z in tri) for tri in _cube_soup()
    ]
    tris = lower + upper  # 24 triangles
    colors = [f"#{i:02X}00{i:02X}" for i in range(17)] + ["#000000"] * 7
    out = tmp_path / "overflow.3mf"
    result = compose_painted_3mf(tris, colors, output_path=str(out))
    assert result["success"] is True
    assert result["native_paint_truncated"] == 1
    assert "native painting limit" in result["message"]

    xml = _painted_model_xml(str(out))
    over = [
        r for r in re.findall(r"<triangle [^>]*/>", xml) if 'p1="16"' in r
    ]
    assert len(over) == 1
    assert "mmu_segmentation" not in over[0]
    assert "paint_color" not in over[0]
    # states 1..16 still present for the palette entries that fit
    assert 'slic3rpe:mmu_segmentation="4"' in xml
    assert 'slic3rpe:mmu_segmentation="DC"' in xml


def test_banded_writer_carries_no_native_paint_attributes(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    """The per-object BANDED writer must NOT gain the painting attributes:
    its per-object extruder channels (sidecar + build items) already work
    in every slicer, and painting the triangles too could double-assign
    the same geometry to two different filament channels."""
    out = tmp_path / "banded.3mf"
    compose_multicolor_3mf(
        [
            ColorPart(stl_path=str(stl_a), extruder=1, color="#F72323"),
            ColorPart(stl_path=str(stl_b), extruder=2, color="#2366F7"),
        ],
        output_path=str(out),
    )
    xml = _model_xml(str(out))
    assert "mmu_segmentation" not in xml
    assert "paint_color" not in xml


# ---------------------------------------------------------------------------
# Degenerate-triangle guard — the banded path
# ---------------------------------------------------------------------------


def test_banded_degenerate_triangle_skipped_and_counted(tmp_path: Path):
    """A degenerate input triangle (repeated vertex after exact dedup —
    common in real-world scans) would emit spec-forbidden repeated indices.
    It is dropped at parse time and counted; every emitted row is valid and
    the sidecar volume range matches what was actually emitted."""
    import re

    stl = _binary_stl(tmp_path, "degen.stl", [
        # one real triangle
        ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
        # one degenerate: two vertices identical
        ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
    ])
    out = tmp_path / "degen.3mf"
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl), extruder=1, name="scanned")],
        output_path=str(out),
    )
    assert result["success"] is True
    assert result["degenerate_skipped"] == 1
    assert result["total_triangles"] == 1

    with zipfile.ZipFile(out) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
        cfg = zf.read("Metadata/Slic3r_PE_model.config").decode()
    rows = re.findall(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', xml)
    assert len(rows) == 1
    assert all(len({a, b, c}) == 3 for a, b, c in rows)
    # volume range derives from the EMITTED triangle count, not the input
    assert '<volume firstid="0" lastid="0">' in cfg


def test_banded_all_degenerate_part_refuses(tmp_path: Path):
    """A part whose every triangle is degenerate is refused the same way
    an empty part is — an honest error naming the part, never a hollow
    zero-triangle object."""
    stl = _binary_stl(tmp_path, "all_degen.stl", [
        ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
        ((0.0, 0.0, 1.0), (5.0, 5.0, 5.0), (5.0, 5.0, 5.0), (5.0, 5.0, 5.0)),
    ])
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl), extruder=1, name="scan_junk")],
        output_path=str(tmp_path / "x.3mf"),
    )
    assert result["success"] is False
    assert "degenerate" in result["error"]
    assert "scan_junk" in result["error"]


def test_banded_no_degenerate_key_on_clean_input(stl_a: Path, tmp_path: Path):
    """degenerate_skipped surfaces ONLY when something was skipped."""
    result = compose_multicolor_3mf(
        [ColorPart(stl_path=str(stl_a), extruder=1)],
        output_path=str(tmp_path / "clean.3mf"),
    )
    assert result["success"] is True
    assert "degenerate_skipped" not in result


def test_painted_drops_degenerate_triangles_with_their_colors(tmp_path: Path):
    """A triangle whose vertices collapse under exact dedup would emit
    spec-forbidden repeated indices; it is dropped WITH its color and
    counted, never silently kept or silently lost."""
    from kiln.multicolor_3mf import compose_painted_3mf

    out = tmp_path / "degen.3mf"
    result = compose_painted_3mf(
        [
            ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
        ],
        ["#F72323", "#2366F7"],
        output_path=str(out),
    )
    assert result["success"] is True
    assert result["total_triangles"] == 1
    assert result["degenerate_skipped"] == 1
    # the degenerate face's color went with it — no phantom palette entry
    assert result["colors"] == ["#F72323"]
    import re

    rows = re.findall(
        r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', _painted_model_xml(str(out)),
    )
    assert all(len({a, b, c}) == 3 for a, b, c in rows)


def test_painted_all_degenerate_refuses(tmp_path: Path):
    from kiln.multicolor_3mf import compose_painted_3mf

    result = compose_painted_3mf(
        [((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0))],
        ["#F72323"],
        output_path=str(tmp_path / "x.3mf"),
    )
    assert result["success"] is False


# ---------------------------------------------------------------------------
# auto_arrange_parts — placement geometry
#
# Regression territory for the coincident-copies defect: the user-facing
# multicolor pipeline once emitted every copy at the origin, so N copies
# sliced into a single footprint.  These tests assert numeric geometry
# (translations, world-space bounding boxes), never string presence.
# ---------------------------------------------------------------------------


def _square_stl(
    tmp_path: Path, name: str, size: float = 10.0, offset: tuple[float, float] = (0.0, 0.0)
) -> Path:
    """Flat square plate spanning [ox, ox+size] x [oy, oy+size] at z=0."""
    ox, oy = offset
    triangles = [
        ((0.0, 0.0, 1.0), (ox, oy, 0.0), (ox + size, oy, 0.0), (ox + size, oy + size, 0.0)),
        ((0.0, 0.0, 1.0), (ox, oy, 0.0), (ox + size, oy + size, 0.0), (ox, oy + size, 0.0)),
    ]
    return _binary_stl(tmp_path, name, triangles)


def _world_xy_bbox(part: ColorPart) -> tuple[float, float, float, float]:
    """Translated (min_x, min_y, max_x, max_y) of a positioned part."""
    from kiln.multicolor_3mf import _stl_bounding_box

    mn_x, mn_y, _, mx_x, mx_y, _ = _stl_bounding_box(part.stl_path)
    return (mn_x + part.x, mn_y + part.y, mx_x + part.x, mx_y + part.y)


def _xy_disjoint(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_arrange_copies_of_same_file_do_not_overlap(tmp_path: Path):
    from kiln.multicolor_3mf import auto_arrange_parts

    square = _square_stl(tmp_path, "sq.stl", size=10.0)
    parts = auto_arrange_parts(
        [{"stl_path": str(square), "extruder": i + 1, "group": i} for i in range(4)],
        plate_width=256.0,
        plate_depth=256.0,
        gap_mm=10.0,
    )
    boxes = [_world_xy_bbox(p) for p in parts]
    for i in range(4):
        for j in range(i + 1, 4):
            assert _xy_disjoint(boxes[i], boxes[j]), (boxes[i], boxes[j])
    # Four 10mm squares + three 10mm gaps → the footprint actually moved.
    union_w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    assert abs(union_w - 70.0) < 0.01


def test_arrange_normalizes_origin_centered_mesh(tmp_path: Path):
    """A mesh centered on the origin must not hang off the plate corner."""
    from kiln.multicolor_3mf import auto_arrange_parts

    centered = _square_stl(tmp_path, "centered.stl", size=10.0, offset=(-5.0, -5.0))
    parts = auto_arrange_parts(
        [{"stl_path": str(centered), "extruder": 1}],
        plate_width=256.0,
        plate_depth=256.0,
    )
    mn_x, mn_y, mx_x, mx_y = _world_xy_bbox(parts[0])
    assert mn_x >= 0.0 and mn_y >= 0.0
    assert mx_x <= 256.0 and mx_y <= 256.0


def test_arrange_centers_layout_on_plate(tmp_path: Path):
    from kiln.multicolor_3mf import auto_arrange_parts

    square = _square_stl(tmp_path, "sq.stl", size=10.0)
    parts = auto_arrange_parts(
        [{"stl_path": str(square), "extruder": 1}],
        plate_width=100.0,
        plate_depth=80.0,
    )
    mn_x, mn_y, mx_x, mx_y = _world_xy_bbox(parts[0])
    assert abs(mn_x - 45.0) < 0.01 and abs(mx_x - 55.0) < 0.01
    assert abs(mn_y - 35.0) < 0.01 and abs(mx_y - 45.0) < 0.01


def test_arrange_printer_id_sets_plate_depth(tmp_path: Path, monkeypatch):
    """The printer's build volume must drive BOTH plate dimensions."""
    import kiln.printers.bed_fit as bed_fit
    from kiln.multicolor_3mf import auto_arrange_parts

    monkeypatch.setattr(
        bed_fit, "resolve_build_volume", lambda _pid: ("m", (100.0, 80.0, 50.0))
    )
    square = _square_stl(tmp_path, "sq.stl", size=10.0)
    parts = auto_arrange_parts(
        [{"stl_path": str(square), "extruder": 1}],
        printer_id="mock_printer",
    )
    mn_x, mn_y, _, _ = _world_xy_bbox(parts[0])
    # Centered on a 100x80 plate — the y-center proves depth was resolved,
    # not left at the 256 default (which would center at y=123).
    assert abs(mn_x - 45.0) < 0.01
    assert abs(mn_y - 35.0) < 0.01


def test_arrange_same_group_shares_translation(tmp_path: Path):
    """Parts in one group keep their relative positions (body + inlay)."""
    from kiln.multicolor_3mf import auto_arrange_parts

    body = _square_stl(tmp_path, "body.stl", size=20.0)
    inlay = _square_stl(tmp_path, "inlay.stl", size=6.0, offset=(7.0, 7.0))
    parts = auto_arrange_parts(
        [
            {"stl_path": str(body), "extruder": 1, "group": 0},
            {"stl_path": str(inlay), "extruder": 2, "group": 0},
        ],
        plate_width=256.0,
        plate_depth=256.0,
    )
    assert (parts[0].x, parts[0].y) == (parts[1].x, parts[1].y)


# ---------------------------------------------------------------------------
# compose_multicolor_3mf — coincident-copy refusal
# ---------------------------------------------------------------------------


def test_compose_refuses_same_mesh_coincident(stl_a: Path, tmp_path: Path):
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="copy_1"),
            ColorPart(str(stl_a), extruder=2, name="copy_2"),
        ],
        output_path=str(tmp_path / "stacked.3mf"),
    )
    assert result["success"] is False
    assert "same position" in result["error"]
    assert "auto_arrange_parts" in result["error"]


def test_compose_same_mesh_distinct_positions_ok(stl_a: Path, tmp_path: Path):
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="copy_1"),
            ColorPart(str(stl_a), extruder=2, name="copy_2", x=30.0),
        ],
        output_path=str(tmp_path / "spaced.3mf"),
    )
    assert result["success"] is True


def test_compose_distinct_meshes_coincident_allowed(stl_a: Path, stl_b: Path, tmp_path: Path):
    """Different meshes at one position are a multi-color unit — legitimate."""
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="body"),
            ColorPart(str(stl_b), extruder=2, name="inlay"),
        ],
        output_path=str(tmp_path / "unit.3mf"),
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# _parse_mesh_file — OBJ/GLB support
# ---------------------------------------------------------------------------


def test_compose_obj_part_works(tmp_path: Path):
    obj_path = tmp_path / "part.obj"
    obj_path.write_text(
        "v 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 10 0\nf 1 2 3\nf 1 3 4\n"
    )
    result = compose_multicolor_3mf(
        [ColorPart(str(obj_path), extruder=1, name="obj_part")],
        output_path=str(tmp_path / "obj.3mf"),
    )
    assert result["success"] is True
    assert result["total_triangles"] == 2


def test_parse_mesh_file_rejects_unknown_ext(tmp_path: Path):
    from kiln.multicolor_3mf import _parse_mesh_file

    bad = tmp_path / "part.ply"
    bad.write_text("ply")
    with pytest.raises(ValueError, match="Unsupported mesh format"):
        _parse_mesh_file(str(bad))


# ---------------------------------------------------------------------------
# Metadata/Slic3r_PE_model.config — the dialect PrusaSlicer actually honors
#
# Measured with a real 4-extruder PrusaSlicer profile: without this file
# every object printed with extruder 1 (filament used 888/0/0/0 mm); with
# it, usage split across all four and T0..T3 tool changes appeared.  The
# slic3rpe:extruder item attribute alone is NOT read.
# ---------------------------------------------------------------------------


def test_compose_writes_slic3r_pe_config(stl_a: Path, stl_b: Path, tmp_path: Path):
    import xml.etree.ElementTree as ET

    out = str(tmp_path / "pe.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="body"),
            ColorPart(str(stl_b), extruder=3, name="accent"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/Slic3r_PE_model.config" in zf.namelist()
        root = ET.fromstring(zf.read("Metadata/Slic3r_PE_model.config"))

    extruders = {}
    for obj in root.findall("object"):
        for md in obj.findall("metadata"):
            if md.get("type") == "object" and md.get("key") == "extruder":
                extruders[obj.get("id")] = int(md.get("value"))
        # every object also carries a volume-level assignment with a real
        # triangle range — PrusaSlicer applies extruders per volume
        vol = obj.find("volume")
        assert vol is not None
        assert int(vol.get("lastid")) >= int(vol.get("firstid"))
        vol_extruder = [
            int(md.get("value"))
            for md in vol.findall("metadata")
            if md.get("key") == "extruder"
        ]
        assert vol_extruder == [extruders[obj.get("id")]]

    assert extruders == {"1": 1, "2": 3}


# ---------------------------------------------------------------------------
# slicer_note — the Bambu Studio detour is announced, not discovered
# ---------------------------------------------------------------------------


def test_compose_multi_extruder_carries_slicer_note(stl_a: Path, stl_b: Path, tmp_path: Path):
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="body"),
            ColorPart(str(stl_b), extruder=2, name="accent"),
        ],
        output_path=str(tmp_path / "note.3mf"),
    )
    assert result["success"] is True
    assert "Bambu Studio" in result["slicer_note"]
    assert "prime" in result["slicer_note"].lower()


def test_compose_single_extruder_has_no_slicer_note(stl_a: Path, tmp_path: Path):
    result = compose_multicolor_3mf(
        [ColorPart(str(stl_a), extruder=1, name="solo")],
        output_path=str(tmp_path / "solo.3mf"),
    )
    assert result["success"] is True
    assert "slicer_note" not in result


# ---------------------------------------------------------------------------
# Duplicate part names — colour is addressed per object BY NAME downstream
# ---------------------------------------------------------------------------


def test_compose_keeps_colours_when_two_parts_share_a_name(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    """Two parts called "body" is an ordinary thing for a caller to ask for.

    Left duplicated, the name makes every colour in the file unattributable —
    Kiln's own parser declines to guess which part is which and the preview
    goes grey, so the composer makes the names unique on the way out.
    """
    from kiln.threemf_parser import object_display_colors

    out = str(tmp_path / "dupes.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="body", color="#C7542E"),
            ColorPart(str(stl_b), extruder=2, name="body", color="#3D6B99"),
        ],
        output_path=out,
    )
    assert result["success"] is True
    assert object_display_colors(out) == {
        "body": (0xC7, 0x54, 0x2E),
        "body (2)": (0x3D, 0x6B, 0x99),
    }


def test_compose_names_agree_across_every_document(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    """The core model, BambuStudio's sidecar and the PrusaSlicer family's each
    name the same objects; a slicer's object list and the model it names have
    to be the same list."""
    import re

    out = str(tmp_path / "agree.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1, name="body"),
            ColorPart(str(stl_b), extruder=2, name="body"),
        ],
        output_path=out,
    )
    assert result["success"] is True

    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode()
        bambu = zf.read("Metadata/model_settings.config").decode()
        prusa = zf.read("Metadata/Slic3r_PE_model.config").decode()

    assert re.findall(r'<object id="\d+" type="model" name="([^"]*)"', model) == [
        "body",
        "body (2)",
    ]
    assert re.findall(r'<metadata key="name"\s+value="([^"]*)"', bambu) == [
        "body",
        "body (2)",
    ]
    assert re.findall(
        r'<metadata type="object" key="name" value="([^"]*)"', prusa
    ) == ["body", "body (2)"]


def test_compose_nameless_parts_keep_the_positional_names(
    stl_a: Path, stl_b: Path, tmp_path: Path
):
    """A part with no name is labelled the way a person would label it."""
    import re

    out = str(tmp_path / "nameless.3mf")
    result = compose_multicolor_3mf(
        [
            ColorPart(str(stl_a), extruder=1),
            ColorPart(str(stl_b), extruder=2),
        ],
        output_path=out,
    )
    assert result["success"] is True
    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode()
    assert re.findall(r'<object id="\d+" type="model" name="([^"]*)"', model) == [
        "Part 1",
        "Part 2",
    ]
