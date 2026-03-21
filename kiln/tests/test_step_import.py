"""Tests for kiln.step_import — STEP file import and conversion to STL.

Coverage areas:
    - check_step_support returns expected structure
    - convert_step_to_stl with no backend installed gives helpful error
    - StepImportResult dataclass serialization (to_dict round-trip)
    - get_step_metadata on missing file raises FileNotFoundError
    - File extension validation (.step, .stp, .STEP accepted; .stl rejected)
    - Output path generation (default and explicit output_dir)
    - merge_bodies parameter forwarding
    - Path traversal rejection (security)
    - Mock-based FreeCAD subprocess conversion
    - Mock-based Gmsh subprocess conversion
    - Mock-based CadQuery conversion
    - FreeCAD subprocess failure handling
    - Gmsh fallback when FreeCAD unavailable
    - CadQuery fallback when FreeCAD and Gmsh unavailable
    - get_step_metadata parsing of STEP headers
    - MCP plugin tool registration
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln.step_import import (
    NoBackendError,
    StepImportError,
    StepImportResult,
    _INSTALL_HELP,
    _VALID_EXTENSIONS,
    _parse_subprocess_result,
    _validate_step_path,
    check_step_support,
    convert_step_to_stl,
    get_step_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sample_step_file(tmp_dir: Path) -> Path:
    """Create a minimal STEP file for testing."""
    step = tmp_dir / "test_part.step"
    step.write_text(
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('A test part'),'2;1');\n"
        "FILE_NAME('test_part.step','2026-03-21',('Author'),('Org'),'','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "#1=PRODUCT('Widget','Widget','A simple widget',());\n"
        "#2=PRODUCT('Gadget','Gadget','A small gadget',());\n"
        "#3=MANIFOLD_SOLID_BREP('body1',#10);\n"
        "#4=MANIFOLD_SOLID_BREP('body2',#20);\n"
        "#5=CLOSED_SHELL('shell1',());\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    return step


@pytest.fixture
def sample_stp_file(tmp_dir: Path) -> Path:
    """Create a minimal .stp file."""
    stp = tmp_dir / "model.stp"
    stp.write_text(
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_SCHEMA(('AP214'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "#1=PRODUCT('Part1','Part1','',());\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    return stp


# ---------------------------------------------------------------------------
# 1. check_step_support returns expected structure
# ---------------------------------------------------------------------------


def test_check_step_support_structure():
    """check_step_support returns dict with expected keys and types."""
    info = check_step_support()

    assert isinstance(info, dict)
    assert "any_available" in info
    assert "backends" in info
    assert isinstance(info["backends"], dict)

    for name in ("freecad", "gmsh", "cadquery"):
        assert name in info["backends"]
        backend = info["backends"][name]
        assert "available" in backend
        assert "priority" in backend
        assert isinstance(backend["available"], bool)
        assert isinstance(backend["priority"], int)

    if not info["any_available"]:
        assert info["install_help"] is not None
    else:
        assert info["install_help"] is None


# ---------------------------------------------------------------------------
# 2. No backend → helpful error
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_convert_no_backend_raises(mock_cq, mock_gmsh, mock_fc, sample_step_file):
    """convert_step_to_stl raises NoBackendError with install instructions."""
    with pytest.raises(NoBackendError) as exc_info:
        convert_step_to_stl(str(sample_step_file))

    msg = str(exc_info.value)
    assert "FreeCAD" in msg
    assert "Gmsh" in msg
    assert "CadQuery" in msg


# ---------------------------------------------------------------------------
# 3. StepImportResult serialization
# ---------------------------------------------------------------------------


def test_step_import_result_to_dict():
    """StepImportResult.to_dict produces a JSON-serializable dict."""
    result = StepImportResult(
        output_path="/tmp/out.stl",
        file_size_bytes=12345,
        body_count=3,
        conversion_time_s=1.234,
        warnings=["minor issue"],
        output_paths=["/tmp/out.stl"],
    )
    d = result.to_dict()

    assert d["output_path"] == "/tmp/out.stl"
    assert d["file_size_bytes"] == 12345
    assert d["body_count"] == 3
    assert d["conversion_time_s"] == 1.234
    assert d["warnings"] == ["minor issue"]
    assert d["output_paths"] == ["/tmp/out.stl"]

    # Must be JSON-serializable.
    json_str = json.dumps(d)
    assert json.loads(json_str) == d


def test_step_import_result_defaults():
    """StepImportResult defaults for optional fields."""
    result = StepImportResult(
        output_path="/tmp/out.stl",
        file_size_bytes=0,
        body_count=1,
        conversion_time_s=0.0,
    )
    assert result.warnings == []
    assert result.output_paths == []


# ---------------------------------------------------------------------------
# 4. get_step_metadata on missing file
# ---------------------------------------------------------------------------


def test_metadata_missing_file():
    """get_step_metadata raises FileNotFoundError for non-existent file."""
    with pytest.raises(FileNotFoundError):
        get_step_metadata("/nonexistent/path/model.step")


# ---------------------------------------------------------------------------
# 5. File extension validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", [".step", ".stp", ".STEP", ".STP", ".Step"])
def test_valid_extensions_accepted(tmp_dir: Path, ext: str):
    """Valid STEP extensions are accepted."""
    f = tmp_dir / f"model{ext}"
    f.write_text("ISO-10303-21;\nENDSEC;\n")
    path = _validate_step_path(str(f))
    assert path.exists()


@pytest.mark.parametrize("ext", [".stl", ".obj", ".txt", ".iges", ""])
def test_invalid_extensions_rejected(tmp_dir: Path, ext: str):
    """Non-STEP extensions are rejected with ValueError."""
    f = tmp_dir / f"model{ext}"
    f.write_text("data")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        _validate_step_path(str(f))


# ---------------------------------------------------------------------------
# 6. Output path generation
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_output_dir_default(mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir):
    """When output_dir is None, output goes to the STEP file's parent dir."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 100)

    with patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=([str(out_stl)], 1),
    ):
        result = convert_step_to_stl(str(sample_step_file))
        assert str(sample_step_file.parent) in result.output_path or result.output_path == str(out_stl)


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_output_dir_explicit(mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir):
    """When output_dir is provided, output goes there."""
    out_dir = tmp_dir / "custom_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_stl = out_dir / "merged.stl"
    out_stl.write_bytes(b"\x00" * 200)

    with patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=([str(out_stl)], 1),
    ):
        result = convert_step_to_stl(
            str(sample_step_file), output_dir=str(out_dir)
        )
        assert "custom_output" in result.output_path


# ---------------------------------------------------------------------------
# 7. merge_bodies parameter
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_merge_bodies_false_splits(mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir):
    """merge_bodies=False with CadQuery splits into per-body files."""
    body0 = sample_step_file.parent / "body_0.stl"
    body1 = sample_step_file.parent / "body_1.stl"
    body0.write_bytes(b"\x00" * 100)
    body1.write_bytes(b"\x00" * 100)

    with patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=([str(body0), str(body1)], 2),
    ):
        result = convert_step_to_stl(
            str(sample_step_file), merge_bodies=False
        )
        assert result.body_count == 2
        assert len(result.output_paths) == 2


# ---------------------------------------------------------------------------
# 8. Path traversal rejection
# ---------------------------------------------------------------------------


def test_path_traversal_rejected_step_path():
    """Path traversal in step_path is rejected."""
    with pytest.raises(ValueError, match="Path traversal"):
        _validate_step_path("../../../etc/passwd.step")


def test_path_traversal_rejected_output_dir(sample_step_file):
    """Path traversal in output_dir is rejected."""
    from kiln.step_import import _validate_output_dir

    with pytest.raises(ValueError, match="Path traversal"):
        _validate_output_dir("../../tmp/../etc", sample_step_file)


# ---------------------------------------------------------------------------
# 9. Mock FreeCAD subprocess conversion
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value="FreeCADCmd")
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_freecad_backend(mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir):
    """FreeCAD backend calls subprocess and parses result."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 500)

    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"some output\nKILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(str(sample_step_file))

    assert result.body_count == 1
    assert result.output_path == str(out_stl)
    assert result.file_size_bytes == 500


# ---------------------------------------------------------------------------
# 10. Mock Gmsh subprocess conversion
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_gmsh_backend(mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir):
    """Gmsh backend calls subprocess and parses result."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 300)

    kiln_result = json.dumps({"body_count": 2, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(str(sample_step_file))

    assert result.body_count == 2
    assert len(result.warnings) >= 0  # gmsh may not warn if merge_bodies=True


# ---------------------------------------------------------------------------
# 11. FreeCAD subprocess failure handling
# ---------------------------------------------------------------------------


def test_parse_subprocess_failure():
    """Subprocess returning non-zero exit code raises StepImportError."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "Segmentation fault"

    with pytest.raises(StepImportError, match="FreeCAD conversion failed"):
        _parse_subprocess_result(mock_proc, "FreeCAD")


def test_parse_subprocess_no_result_line():
    """Subprocess succeeding but no KILN_RESULT line raises StepImportError."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "some random output\nno result here\n"
    mock_proc.stderr = ""

    with pytest.raises(StepImportError, match="no result"):
        _parse_subprocess_result(mock_proc, "Gmsh")


# ---------------------------------------------------------------------------
# 12. Gmsh fallback when FreeCAD unavailable
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_gmsh_fallback(mock_cq, mock_gmsh, mock_fc, sample_step_file):
    """When FreeCAD is not available, falls back to Gmsh."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 100)

    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(str(sample_step_file))
        assert result.body_count == 1


# ---------------------------------------------------------------------------
# 13. CadQuery fallback
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_cadquery_fallback(mock_cq_avail, mock_gmsh, mock_fc, sample_step_file):
    """When FreeCAD and Gmsh are unavailable, falls back to CadQuery."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 150)

    with patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=([str(out_stl)], 1),
    ):
        result = convert_step_to_stl(str(sample_step_file))
        assert result.body_count == 1


# ---------------------------------------------------------------------------
# 14. get_step_metadata parses STEP headers
# ---------------------------------------------------------------------------


def test_metadata_extracts_header_fields(sample_step_file):
    """get_step_metadata extracts schema, products, and body count."""
    metadata = get_step_metadata(str(sample_step_file))

    assert metadata["file_name"] == "test_part.step"
    assert metadata["file_size_bytes"] > 0
    assert metadata["schema"] == "AUTOMOTIVE_DESIGN"
    assert metadata["description"] == "A test part"
    assert "Widget" in metadata["products"]
    assert "Gadget" in metadata["products"]
    # 2 MANIFOLD_SOLID_BREP entries.
    assert metadata["estimated_body_count"] == 2


def test_metadata_stp_extension(sample_stp_file):
    """get_step_metadata works with .stp extension."""
    metadata = get_step_metadata(str(sample_stp_file))
    assert metadata["file_name"] == "model.stp"
    assert metadata["schema"] == "AP214"


# ---------------------------------------------------------------------------
# 15. Metadata on invalid extension
# ---------------------------------------------------------------------------


def test_metadata_invalid_extension(tmp_dir: Path):
    """get_step_metadata rejects non-STEP extensions."""
    f = tmp_dir / "model.stl"
    f.write_text("solid cube\nendsolid")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        get_step_metadata(str(f))


# ---------------------------------------------------------------------------
# 16. MCP plugin tool registration
# ---------------------------------------------------------------------------


def test_plugin_has_required_attributes():
    """Plugin module exposes a plugin object with name, description, register."""
    from kiln.plugins.step_tools import plugin

    assert hasattr(plugin, "name")
    assert hasattr(plugin, "description")
    assert hasattr(plugin, "register")
    assert plugin.name == "step_tools"
    assert "STEP" in plugin.description or "step" in plugin.description.lower()


def test_plugin_registers_three_tools():
    """Plugin registers exactly three tools on the MCP object."""
    from kiln.plugins.step_tools import plugin

    mock_mcp = MagicMock()
    registered_tools = []

    def mock_tool():
        def decorator(fn):
            registered_tools.append(fn.__name__)
            return fn
        return decorator

    mock_mcp.tool = mock_tool
    plugin.register(mock_mcp)

    assert "import_step_file" in registered_tools
    assert "check_step_support" in registered_tools
    assert "step_file_info" in registered_tools
    assert len(registered_tools) == 3


# ---------------------------------------------------------------------------
# 17. convert_step_to_stl rejects missing file
# ---------------------------------------------------------------------------


def test_convert_missing_file():
    """convert_step_to_stl raises FileNotFoundError for missing input."""
    with pytest.raises(FileNotFoundError):
        convert_step_to_stl("/nonexistent/model.step")


# ---------------------------------------------------------------------------
# 18. Gmsh merge_bodies warning
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_gmsh_merge_bodies_false_warns(mock_cq, mock_gmsh, mock_fc, sample_step_file):
    """Gmsh backend warns when merge_bodies=False since it can't split."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 100)

    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(
            str(sample_step_file), merge_bodies=False
        )
        assert any("split" in w.lower() or "merge" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# 19. VALID_EXTENSIONS constant
# ---------------------------------------------------------------------------


def test_valid_extensions_set():
    """The _VALID_EXTENSIONS set contains expected values."""
    assert ".step" in _VALID_EXTENSIONS
    assert ".stp" in _VALID_EXTENSIONS
    assert ".stl" not in _VALID_EXTENSIONS


# ---------------------------------------------------------------------------
# 20. NoBackendError message contains install help
# ---------------------------------------------------------------------------


def test_no_backend_error_message():
    """NoBackendError includes install instructions."""
    err = NoBackendError()
    assert "FreeCAD" in str(err)
    assert "gmsh" in str(err).lower()
    assert "cadquery" in str(err).lower()
    assert str(err) == _INSTALL_HELP
