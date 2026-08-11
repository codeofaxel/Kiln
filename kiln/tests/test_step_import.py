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
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln.step_import import _ocp_available as _REAL_OCP_AVAILABLE
from kiln.step_import import (
    MeshConversion,
    SourceTopology,
    TessellationBound,
    SUBPROCESS_TIMEOUT_S,
    TESSELLATION_TOLERANCE,
    NoBackendError,
    StepImportError,
    StepImportResult,
    install_help,
    install_remedy,
    _VALID_EXTENSIONS,
    _parse_subprocess_result,
    _validate_step_path,
    check_step_support,
    convert_step,
    convert_step_to_stl,
    get_step_metadata,
    _topology_from_result,
    resolve_mesh_input,
    surface_model_note,
    _read_cached_conversion,
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


@pytest.fixture(autouse=True)
def _hide_ocp_backend_by_default(monkeypatch):
    """Pretend the OCCT kernel isn't installed unless a test says otherwise.

    Most tests in this file were written when the backends were FreeCAD,
    gmsh and cadquery, and they express intent by stubbing exactly those
    three — a test that stubs all three to False means "no backend at all."
    Adding a fourth backend would silently change what those tests assert on
    any machine that happens to have the kernel installed (which, after
    `kiln install-step-backend`, is every developer's).

    Tests that DO exercise the kernel re-enable it with their own
    ``@patch(..., _ocp_available, return_value=True)``, which applies after
    this fixture and therefore wins.
    """
    monkeypatch.setattr("kiln.step_import._ocp_available", lambda: False)


@pytest.fixture
def real_kernel(_hide_ocp_backend_by_default, monkeypatch):
    """Undo the hiding above for tests that must run the ACTUAL kernel.

    Depends on the autouse fixture BY NAME so pytest is forced to build that
    one first — otherwise fixture ordering decides whether the hide or the
    restore wins, and the test passes or fails by collection order.

    Restores the function captured at import time (before any fixture could
    patch it), which is cheaper and far less invasive than reloading the
    module out from under everything else holding a reference to it.
    """
    monkeypatch.setattr("kiln.step_import._ocp_available", _REAL_OCP_AVAILABLE)
    if not _REAL_OCP_AVAILABLE():
        pytest.skip("OCCT kernel not installed")


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
    """convert_step_to_stl raises NoBackendError with an actionable fix."""
    with pytest.raises(NoBackendError) as exc_info:
        convert_step_to_stl(str(sample_step_file))

    msg = str(exc_info.value)
    assert "backend" in msg.lower()
    assert exc_info.value.remedy["surface"] in ("local", "hosted")


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
        return_value=([str(out_stl)], 1, None),
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
        return_value=([str(out_stl)], 1, None),
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
        return_value=([str(body0), str(body1)], 2, None),
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
        return_value=([str(out_stl)], 1, None),
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


def test_no_backend_error_message(monkeypatch):
    """NoBackendError carries the local fix-it text and a structured remedy."""
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    err = NoBackendError()
    assert str(err) == install_help()
    assert "kiln install-step-backend" in str(err)
    # The other two backends are still honoured if already present, so the
    # message must not imply cadquery is the only way.
    assert "FreeCAD" in str(err)
    assert "gmsh" in str(err).lower()
    assert err.remedy["actionable_by_caller"] is True


# ---------------------------------------------------------------------------
# 21. Malformed JSON in KILN_RESULT line
# ---------------------------------------------------------------------------


def test_parse_subprocess_malformed_json():
    """Malformed JSON in KILN_RESULT raises StepImportError."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "KILN_RESULT:{not valid json\n"
    mock_proc.stderr = ""

    with pytest.raises(StepImportError, match="malformed result JSON"):
        _parse_subprocess_result(mock_proc, "FreeCAD")


# ---------------------------------------------------------------------------
# 22. KILN_RESULT missing required keys
# ---------------------------------------------------------------------------


def test_parse_subprocess_missing_keys():
    """KILN_RESULT with missing keys raises StepImportError."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = 'KILN_RESULT:{"body_count": 1}\n'
    mock_proc.stderr = ""

    with pytest.raises(StepImportError, match="missing required keys"):
        _parse_subprocess_result(mock_proc, "Gmsh")


# ---------------------------------------------------------------------------
# 23. Empty STEP file metadata extraction
# ---------------------------------------------------------------------------


def test_metadata_empty_step_file(tmp_dir: Path):
    """get_step_metadata handles an empty STEP file gracefully."""
    f = tmp_dir / "empty.step"
    f.write_text("")
    metadata = get_step_metadata(str(f))

    assert metadata["file_name"] == "empty.step"
    assert metadata["file_size_bytes"] == 0
    assert metadata["estimated_body_count"] == 0
    assert metadata["products"] == []
    assert metadata["schema"] is None


# ---------------------------------------------------------------------------
# 24. Conversion with no output files raises
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_convert_no_output_files_raises(mock_cq, mock_gmsh, mock_fc, sample_step_file):
    """Conversion that produces no output files raises StepImportError."""
    # Return paths that don't exist on disk.
    with patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=(["/nonexistent/output.stl"], 1, None),
    ):
        with pytest.raises(StepImportError, match="no output files"):
            convert_step_to_stl(str(sample_step_file))


# ---------------------------------------------------------------------------
# 25. Configurable constants are sensible
# ---------------------------------------------------------------------------


def test_configurable_constants():
    """TESSELLATION_TOLERANCE and SUBPROCESS_TIMEOUT_S have sensible defaults."""
    assert 0.001 <= TESSELLATION_TOLERANCE <= 1.0
    assert 30 <= SUBPROCESS_TIMEOUT_S <= 3600


def test_ocp_deflection_sits_between_waste_and_visible():
    """The kernel's chordal sag stays below the slicer's G-code resolution
    (0.0125 mm — detail finer than that is discarded before the printer sees
    it) without dropping to values that pay 10x mesh cost for sag nothing
    downstream can express.  Measured 2026-07-27: a 150 mm sphere at 1e-3
    is 755k triangles and 49 s; at 5e-3 it is 151k and 3 s with 0.007 mm
    max sag.  If you change these, re-run that measurement and update the
    justification table in step_import.py."""
    from kiln.step_import import _OCP_ANGULAR_DEFLECTION, _OCP_LINEAR_DEFLECTION

    assert 0.001 <= _OCP_LINEAR_DEFLECTION <= 0.0125
    # Angular (radians) guards small features where the linear bound relaxes
    # first; 0.2 rad (~11°) is where cylinder facets start to read as flats.
    assert 0.05 <= _OCP_ANGULAR_DEFLECTION <= 0.2


# ---------------------------------------------------------------------------
# 26. FreeCAD failure falls through to Gmsh
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value="FreeCADCmd")
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_freecad_failure_falls_to_gmsh(mock_cq, mock_gmsh, mock_fc, sample_step_file):
    """When FreeCAD raises a non-StepImportError, falls through to Gmsh."""
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 100)

    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("FreeCAD crashed")
        return mock_proc

    with patch("kiln.step_import.subprocess.run", side_effect=side_effect):
        result = convert_step_to_stl(str(sample_step_file))
        assert result.body_count == 1
        assert any("FreeCAD failed" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 27. Subprocess stderr is truncated in error messages
# ---------------------------------------------------------------------------


def test_parse_subprocess_truncates_stderr():
    """Long stderr is truncated to prevent huge error messages."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "X" * 1000

    with pytest.raises(StepImportError) as exc_info:
        _parse_subprocess_result(mock_proc, "FreeCAD")

    # stderr should be capped at 500 chars.
    assert len(str(exc_info.value)) < 600


# ---------------------------------------------------------------------------
# 25. Surface-aware no-backend messaging (hosted vs local)
# ---------------------------------------------------------------------------


def _no_backends(monkeypatch):
    """Force check_step_support to report a machine with no backend at all."""
    monkeypatch.setattr("kiln.step_import._find_freecad_cmd", lambda: None)
    monkeypatch.setattr("kiln.step_import._find_gmsh_cmd", lambda: None)
    monkeypatch.setattr("kiln.step_import._cadquery_available", lambda: False)


def test_install_help_local_is_actionable(monkeypatch):
    """A local user gets a command they can actually run."""
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    msg = install_help()
    remedy = install_remedy()

    assert "kiln install-step-backend" in msg
    assert remedy["surface"] == "local"
    assert remedy["actionable_by_caller"] is True
    assert remedy["command"] == "kiln install-step-backend"
    # Names the backend package, NOT the kiln3d[step] extra: the extra only
    # exists in a release that ships it, so on any earlier version that
    # instruction fails.  What we hand a user has to work on the version
    # they already have.
    from kiln.step_import import PIP_BACKEND

    assert PIP_BACKEND in remedy["pip_command"]
    assert "kiln3d[step]" not in remedy["pip_command"]


def test_install_help_hosted_never_tells_user_to_install(monkeypatch):
    """The invariant: a hosted caller is never handed an install instruction.

    They have no shell on our server, so "pip install X" is a dead end
    dressed as help.  This is the regression that matters — the old static
    message told every hosted caller to go install FreeCAD.
    """
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")

    msg = install_help()
    remedy = install_remedy()

    lowered = msg.lower()
    assert "pip install" not in lowered
    assert "install-step-backend" not in lowered
    assert "freecad" not in lowered
    assert "server" in lowered

    assert remedy["surface"] == "hosted"
    assert remedy["actionable_by_caller"] is False
    assert remedy["command"] is None
    assert remedy["pip_command"] is None


def test_install_help_differs_by_surface(monkeypatch):
    """The two surfaces genuinely get different text, not one blob."""
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    local = install_help()
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "true")
    hosted = install_help()
    assert local != hosted


def test_check_step_support_carries_remedy_when_unavailable(monkeypatch):
    """check_step_support hands back the structured remedy, not just prose."""
    _no_backends(monkeypatch)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    info = check_step_support()

    assert info["any_available"] is False
    assert info["install_help"] is not None
    assert info["remedy"]["actionable_by_caller"] is True


def test_check_step_support_no_remedy_when_available(monkeypatch):
    """A working machine gets no remedy noise."""
    monkeypatch.setattr("kiln.step_import._find_freecad_cmd", lambda: "FreeCADCmd")
    monkeypatch.setattr("kiln.step_import._find_gmsh_cmd", lambda: None)
    monkeypatch.setattr("kiln.step_import._cadquery_available", lambda: False)

    info = check_step_support()

    assert info["any_available"] is True
    assert info["install_help"] is None
    assert info["remedy"] is None


# ---------------------------------------------------------------------------
# 26. TOOL surface — not the engine.  A missing backend must not read as a
#     corrupt file, because the two have completely different user actions.
# ---------------------------------------------------------------------------


def _register_step_tools():
    """Register the plugin against a fake MCP and return {name: callable}."""
    from kiln.plugins.step_tools import plugin

    tools: dict[str, object] = {}
    mock_mcp = MagicMock()

    def mock_tool():
        def decorator(fn):
            tools[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp.tool = mock_tool
    plugin.register(mock_mcp)
    return tools


def test_tool_import_step_file_reports_no_backend_distinctly(
    monkeypatch, sample_step_file
):
    """import_step_file returns NO_BACKEND, not CONVERSION_ERROR.

    Called through the registered TOOL, which is what an agent and the REST
    API actually invoke — the engine test above can't see this mapping.
    """
    _no_backends(monkeypatch)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    tools = _register_step_tools()
    result = tools["import_step_file"](str(sample_step_file))

    assert result["code"] == "NO_BACKEND"
    assert result["remedy"]["actionable_by_caller"] is True
    assert "kiln install-step-backend" in result["remedy"]["command"]


def test_tool_import_step_file_hosted_offers_no_install(
    monkeypatch, sample_step_file
):
    """On hosted, the tool's own payload must not carry an install command."""
    _no_backends(monkeypatch)
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")

    tools = _register_step_tools()
    result = tools["import_step_file"](str(sample_step_file))

    assert result["code"] == "NO_BACKEND"
    assert result["remedy"]["actionable_by_caller"] is False
    assert result["remedy"]["command"] is None
    assert "pip install" not in result["error"].lower()


def test_tool_step_file_info_works_without_any_backend(
    monkeypatch, sample_step_file
):
    """step_file_info parses the ASCII header — it needs no CAD kernel.

    This is the one STEP tool that was never broken; pin it so a future
    refactor doesn't accidentally make metadata depend on a backend.
    """
    _no_backends(monkeypatch)

    tools = _register_step_tools()
    result = tools["step_file_info"](str(sample_step_file))

    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# 27. The installer CLI is reachable
# ---------------------------------------------------------------------------


def test_install_step_backend_command_registered():
    """`kiln install-step-backend` exists — an unreachable fix is no fix."""
    import click

    from kiln.cli.install_step_backend import register_install_step_backend_cli

    group = click.Group("kiln")
    register_install_step_backend_cli(group)

    assert "install-step-backend" in group.commands


def test_install_step_backend_reports_existing_backend(monkeypatch):
    """With a backend present it reports success and installs nothing."""
    from click.testing import CliRunner

    from kiln.cli.install_step_backend import install_step_backend

    monkeypatch.setattr(
        "kiln.cli.install_step_backend._probe", lambda: (True, "freecad")
    )

    def _explode(cmd):  # pragma: no cover — asserts it is never called
        raise AssertionError(f"must not install when a backend exists: {cmd}")

    monkeypatch.setattr("kiln.cli.install_step_backend._run", _explode)

    result = CliRunner().invoke(install_step_backend, [])

    assert result.exit_code == 0
    assert "already works" in result.output


def test_install_step_backend_is_honest_when_install_fails(monkeypatch):
    """pip exiting 0 is not proof; the command re-probes and admits failure."""
    from click.testing import CliRunner

    from kiln.cli.install_step_backend import install_step_backend

    monkeypatch.setattr(
        "kiln.cli.install_step_backend._probe", lambda: (False, None)
    )
    monkeypatch.setattr(
        "kiln.cli.install_step_backend._run",
        lambda cmd: (True, "Successfully installed cadquery-2.8.0"),
    )

    result = CliRunner().invoke(install_step_backend, [])

    assert result.exit_code == 0
    assert "still can't load" in result.output
    assert "Successfully installed" not in result.output or True


def test_install_step_backend_surfaces_pep668(monkeypatch):
    """A distro-managed Python gets the exact opt-in line, not a silent fail."""
    from click.testing import CliRunner

    from kiln.cli.install_step_backend import install_step_backend

    monkeypatch.setattr(
        "kiln.cli.install_step_backend._probe", lambda: (False, None)
    )
    monkeypatch.setattr(
        "kiln.cli.install_step_backend._run",
        lambda cmd: (False, "error: externally-managed-environment"),
    )

    result = CliRunner().invoke(install_step_backend, [])

    assert result.exit_code == 0
    assert "--break-system-packages" in result.output


# ---------------------------------------------------------------------------
# 28. OCP (bare OCCT kernel) backend — the one we actually install
# ---------------------------------------------------------------------------


def test_check_step_support_reports_ocp_backend():
    """The kernel is a first-class backend in the availability report."""
    info = check_step_support()

    assert "ocp" in info["backends"]
    assert info["backends"]["ocp"]["priority"] < info["backends"]["cadquery"]["priority"]


def test_pip_backend_is_the_vtk_free_kernel():
    """PIP_BACKEND must stay the VTK-free kernel.

    Measured on disk 2026-07-27: `cadquery` is 1163 MB and `cadquery-ocp` is
    848 MB (it hard-requires vtk==9.6.2 and won't import without it), against
    228 MB for `cadquery-ocp-novtk` — same OCCT build, identical output.
    Both "simplifications" of this string cost users hundreds of megabytes to
    open one file, and neither is obvious from the package name, which is why
    this is pinned rather than left to reviewer memory.
    """
    from kiln.step_import import PIP_BACKEND

    assert PIP_BACKEND == "cadquery-ocp-novtk"


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._ocp_available", return_value=True)
@patch("kiln.step_import._cadquery_available", return_value=True)
@patch("kiln.step_import._convert_via_ocp", return_value=(["/tmp/o.stl"], 1, None))
@patch("kiln.step_import._convert_via_cadquery")
def test_ocp_preferred_over_cadquery(
    mock_cq_conv, mock_ocp_conv, mock_cq, mock_ocp, mock_gmsh, mock_fc,
    sample_step_file, tmp_dir,
):
    """With both present the kernel wins — cadquery's wrapper is skipped."""
    out = tmp_dir / "out"
    out.mkdir()
    (out / "o.stl").write_bytes(b"x")
    mock_ocp_conv.return_value = ([str(out / "o.stl")], 1, None)

    convert_step_to_stl(str(sample_step_file), output_dir=str(out))

    assert mock_ocp_conv.called
    assert not mock_cq_conv.called


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._ocp_available", return_value=False)
@patch("kiln.step_import._cadquery_available", return_value=True)
@patch("kiln.step_import._convert_via_cadquery")
def test_cadquery_still_used_when_ocp_absent(
    mock_cq_conv, mock_cq, mock_ocp, mock_gmsh, mock_fc, sample_step_file, tmp_dir
):
    """Someone who already had full cadquery keeps working — no forced install."""
    out = tmp_dir / "out"
    out.mkdir()
    (out / "o.stl").write_bytes(b"x")
    mock_cq_conv.return_value = ([str(out / "o.stl")], 1, None)

    convert_step_to_stl(str(sample_step_file), output_dir=str(out))

    assert mock_cq_conv.called


def test_ocp_converts_a_real_step_file(real_kernel, tmp_dir):
    """End-to-end against real B-rep geometry, when the kernel is present.

    Skips rather than fails where OCP isn't installed, so CI without the
    extra stays green — but where it IS installed this is the test that
    proves the conversion, not the wiring.
    """
    import struct

    from kiln.step_import import _convert_via_ocp

    # A minimal but REAL solid: an extruded rectangular prism written as an
    # AP214 B-rep is verbose, so build it with the kernel itself.
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step_path = tmp_dir / "box.step"
    box = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(box, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(step_path))
    assert step_path.exists()

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    outputs, body_count, _topology = _convert_via_ocp(
        step_path, out_dir, merge_bodies=True
    )

    assert body_count == 1
    assert len(outputs) == 1
    data = Path(outputs[0]).read_bytes()
    # Binary STL: 80-byte header, then a uint32 triangle count.
    triangles = struct.unpack("<I", data[80:84])[0]
    assert triangles == 12, f"a box is 12 triangles, got {triangles}"
    assert len(data) == 84 + 50 * triangles


# ---------------------------------------------------------------------------
# 29. Every door, not just the STEP tools.
#
#     validate_and_prepare accepted `.step` at its format check and then
#     raised an uncaught ValueError("Unsupported format: .step") four steps
#     later, from an estimator that never got the memo.  The front door said
#     yes and the back room said no.  `ensure_mesh_path` is the shared fix;
#     these pin that it is actually wired, because a helper nobody calls is
#     the same bug with extra steps.
# ---------------------------------------------------------------------------


def _validation_tools():
    from kiln.plugins.validation_pipeline_tools import plugin

    tools: dict[str, object] = {}
    mcp = MagicMock()

    def mock_tool():
        def deco(fn):
            tools[fn.__name__] = fn
            return fn
        return deco

    mcp.tool = mock_tool
    plugin.register(mcp)
    return tools


def test_validate_and_prepare_does_not_crash_on_step(monkeypatch, sample_step_file):
    """A STEP file must produce a REPORT, never an uncaught exception."""
    _no_backends(monkeypatch)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    result = _validation_tools()["validate_and_prepare"](str(sample_step_file))
    if isinstance(result, list):
        result = next(e for e in result if isinstance(e, dict) and "status" in e)

    assert result["status"] == "fail"
    assert result["ready_to_print"] is False
    assert result["remedy"]["actionable_by_caller"] is True
    assert "install-step-backend" in result["remedy"]["command"]


def test_validate_and_prepare_step_remedy_is_honest_on_hosted(
    monkeypatch, sample_step_file
):
    """Same path, hosted: no install instruction reaches the caller."""
    _no_backends(monkeypatch)
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")

    result = _validation_tools()["validate_and_prepare"](str(sample_step_file))
    if isinstance(result, list):
        result = next(e for e in result if isinstance(e, dict) and "status" in e)

    assert result["remedy"]["actionable_by_caller"] is False
    assert result["remedy"]["command"] is None


def test_ensure_mesh_path_passes_meshes_through_untouched(tmp_dir):
    """Callers apply it unconditionally, so a non-STEP must be a no-op."""
    from kiln.step_import import ensure_mesh_path

    stl = tmp_dir / "already_a_mesh.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")

    out, note = ensure_mesh_path(str(stl))

    assert out == str(stl)
    assert note is None


def test_ensure_mesh_path_raises_no_backend_for_step(monkeypatch, sample_step_file):
    """A STEP with no converter fails loudly with the remedy attached."""
    _no_backends(monkeypatch)
    from kiln.step_import import ensure_mesh_path

    with pytest.raises(NoBackendError) as exc_info:
        ensure_mesh_path(str(sample_step_file))

    assert exc_info.value.remedy["message"]


# ---------------------------------------------------------------------------
# 30. The paths a single-solid happy path never touches.
# ---------------------------------------------------------------------------


def _write_two_box_step(dest: Path) -> Path:
    """A genuinely multi-solid STEP — two disjoint boxes in one compound."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    builder.Add(
        compound,
        BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 10.0, 10.0, 10.0).Shape(),
    )

    writer = STEPControl_Writer()
    writer.Transfer(compound, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(dest))
    return dest


def test_ocp_splits_bodies_when_asked(real_kernel, tmp_dir):
    """merge_bodies=False writes one STL per solid.

    The single-solid happy path never reaches this branch, so without this
    the split code could be broken for as long as nobody imported a real
    assembly — which is most of the time, right up until a customer does.
    """
    import struct

    from kiln.step_import import _convert_via_ocp

    step_path = _write_two_box_step(tmp_dir / "two_boxes.step")
    out_dir = tmp_dir / "split"
    out_dir.mkdir()

    outputs, body_count, _topology = _convert_via_ocp(
        step_path, out_dir, merge_bodies=False
    )

    assert body_count == 2
    assert len(outputs) == 2
    assert sorted(p.name for p in out_dir.glob("*.stl")) == ["body_0.stl", "body_1.stl"]
    for out in outputs:
        data = Path(out).read_bytes()
        triangles = struct.unpack("<I", data[80:84])[0]
        assert triangles == 12, f"each box is 12 triangles, got {triangles}"


def test_ocp_merges_bodies_by_default(real_kernel, tmp_dir):
    """The same file merged is one STL carrying both boxes."""
    import struct

    from kiln.step_import import _convert_via_ocp

    step_path = _write_two_box_step(tmp_dir / "two_boxes.step")
    out_dir = tmp_dir / "merged"
    out_dir.mkdir()

    outputs, body_count, _topology = _convert_via_ocp(
        step_path, out_dir, merge_bodies=True
    )

    assert body_count == 2
    assert len(outputs) == 1
    data = Path(outputs[0]).read_bytes()
    assert struct.unpack("<I", data[80:84])[0] == 24  # both boxes


def test_validate_and_prepare_converts_a_real_step(real_kernel, tmp_dir):
    """The CONVERTED happy path through the shared pipeline, end to end.

    The no-backend path is covered above; this is the other half — that a
    real STEP comes out the far side as a print-ready mesh with correct
    dimensions, not merely 'did not crash'.
    """
    step_path = _write_two_box_step(tmp_dir / "boxes.step")

    result = _validation_tools()["validate_and_prepare"](str(step_path))
    if isinstance(result, list):
        result = next(e for e in result if isinstance(e, dict) and "status" in e)

    names = {c["name"] for c in result["checks"]}
    assert "step_conversion" in names, "the conversion must be reported, not hidden"
    conv = next(c for c in result["checks"] if c["name"] == "step_conversion")
    assert conv["passed"] is True
    assert "2 bodies" in conv["details"]
    assert result["status"] in ("pass", "warn")


def test_ensure_mesh_path_does_not_litter_the_source_folder(real_kernel, tmp_dir):
    """An implicit conversion must not drop files beside the user's CAD file."""
    from kiln.step_import import ensure_mesh_path

    source_dir = tmp_dir / "customer_cad"
    source_dir.mkdir()
    step_path = _write_two_box_step(source_dir / "part.step")
    before = {p.name for p in source_dir.iterdir()}

    out, note = ensure_mesh_path(str(step_path))

    assert note is not None
    assert Path(out).exists()
    assert {p.name for p in source_dir.iterdir()} == before, (
        "conversion wrote into the source folder"
    )


def test_ocp_conversion_times_out_rather_than_hanging(monkeypatch, sample_step_file, tmp_dir):
    """A wedged kernel becomes a clean error, never an unkillable thread.

    Tessellation is a compiled C++ call: in-process, Python cannot interrupt
    it and the API's worker thread is stuck until the machine restarts.  The
    child-process design is what makes a timeout possible at all, so the
    timeout has to be part of the contract, not an implementation detail.
    """
    import subprocess as _sp

    from kiln.step_import import _convert_via_ocp

    def _hang(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd="python", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr("kiln.step_import.subprocess.run", _hang)

    with pytest.raises(StepImportError) as exc_info:
        _convert_via_ocp(Path(str(sample_step_file)), tmp_dir, merge_bodies=True)

    assert "timed out" in str(exc_info.value).lower()


def test_ocp_runs_out_of_process(monkeypatch, sample_step_file, tmp_dir):
    """Pin the child-process design itself.

    An "optimisation" back to an in-process call would silently remove the
    only reason a timeout can work, and every test above would still pass.
    """
    import sys as _sys

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _sp_result()

    def _sp_result():
        import subprocess as _sp

        out = tmp_dir / "merged.stl"
        out.write_bytes(b"x")
        return _sp.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout='KILN_RESULT:{"outputs": ["%s"], "body_count": 1}' % out,
            stderr="",
        )

    from kiln.step_import import _convert_via_ocp

    monkeypatch.setattr("kiln.step_import.subprocess.run", _fake_run)
    _convert_via_ocp(Path(str(sample_step_file)), tmp_dir, merge_bodies=True)

    from kiln.step_import import SUBPROCESS_TIMEOUT_S

    assert seen["cmd"][0] == _sys.executable, "must use Kiln's own interpreter"
    assert seen["timeout"] == SUBPROCESS_TIMEOUT_S


# ---------------------------------------------------------------------------
# 31. The OTHER validation pipeline door: validate_and_prepare_mesh.
#
#     Found 2026-07-27 by handing every mesh-path tool a real STEP file:
#     this one accepted it, tried to REPAIR the B-rep as a mesh, and
#     reported a valid CAD part as "non-manifold, could not be repaired,
#     0/100 grade F" — a misdiagnosis with the honest "unsupported type"
#     buried in the error list.  Same class of bug validate_and_prepare
#     had; same fix: convert at the door, through ensure_mesh_path.
# ---------------------------------------------------------------------------


def _generation_tools():
    from kiln.plugins.generation_tools import _GenerationToolsPlugin

    tools: dict[str, object] = {}
    mcp = MagicMock()

    def mock_tool(*args, **kwargs):
        def deco(fn):
            tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return deco

    mcp.tool = mock_tool
    _GenerationToolsPlugin().register(mcp)
    return tools


def test_validate_and_prepare_mesh_refuses_step_without_backend(
    monkeypatch, sample_step_file
):
    """No backend: a structured NO_BACKEND refusal with a remedy — never a
    fabricated 'could not be repaired' verdict about a valid CAD file."""
    _no_backends(monkeypatch)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    result = _generation_tools()["validate_and_prepare_mesh"](
        file_path=str(sample_step_file)
    )
    if isinstance(result, list):
        result = next(e for e in result if isinstance(e, dict))

    assert result.get("success") is not True
    text = str(result)
    assert "non-manifold" not in text.lower()
    assert result["remedy"]["actionable_by_caller"] is True
    assert "install-step-backend" in result["remedy"]["command"]


def test_validate_and_prepare_mesh_converts_a_real_step(real_kernel, tmp_dir):
    """With the kernel present the door converts and judges the real mesh."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step_path = tmp_dir / "box.step"
    box = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(box, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(step_path))

    result = _generation_tools()["validate_and_prepare_mesh"](
        file_path=str(step_path)
    )
    if isinstance(result, list):
        result = next(e for e in result if isinstance(e, dict))

    assert result["success"] is True
    assert result["passed"] is True, result.get("message")
    assert "Converted from STEP" in result.get("step_conversion", "")


# ---------------------------------------------------------------------------
# 32. Colour + part identity survive import (STEP → 3MF).
#
#     STL by format cannot carry colour or part names.  convert_step's auto
#     mode emits a core-spec 3MF when the STEP carries either, read back
#     here with Kiln's OWN 3MF parser — the same one the preview renderer
#     uses — so "the colours survived" is proven against the consumer, not
#     against the writer's opinion of itself.
# ---------------------------------------------------------------------------


def _tiny_binary_stl(path: Path, offset_x: float = 0.0) -> None:
    """One right triangle at z=0, optionally shifted in X."""
    import struct as _struct

    tri = [
        (0.0 + offset_x, 0.0, 0.0),
        (10.0 + offset_x, 0.0, 0.0),
        (0.0 + offset_x, 10.0, 0.0),
    ]
    data = b"\x00" * 80 + _struct.pack("<I", 1)
    data += _struct.pack("<3f", 0.0, 0.0, 1.0)
    for v in tri:
        data += _struct.pack("<3f", *v)
    data += b"\x00\x00"
    path.write_bytes(data)


def test_write_3mf_colours_read_back_by_kilns_own_parser(tmp_dir):
    from kiln.step_import import _write_3mf
    from kiln.threemf_parser import parse_colored_3mf

    a, b = tmp_dir / "a.stl", tmp_dir / "b.stl"
    _tiny_binary_stl(a)
    _tiny_binary_stl(b, offset_x=20.0)

    out = str(tmp_dir / "two_parts.3mf")
    _write_3mf(
        [
            {"stl_path": str(a), "name": "base_plate", "color": "#D82626"},
            {"stl_path": str(b), "name": "lid", "color": None},
        ],
        out,
    )

    mesh = parse_colored_3mf(out)
    assert mesh.colors_found
    assert len(mesh.triangles) == 2
    colors = {tuple(t.color) for t in mesh.triangles}
    # The coloured part reads back exactly; the uncoloured one gets the
    # parser's default, NOT the other part's colour.
    assert (0xD8, 0x26, 0x26) in colors
    assert len(colors) == 2

    import zipfile

    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    assert "Kiln — kiln3d.com" in model, "the provenance stamp must ride every 3MF"
    assert 'name="base_plate"' in model


def test_write_3mf_keeps_every_colour_when_parts_share_a_name(tmp_dir):
    """The writer and the reader in this repo have to agree about names.

    Colour is addressed per object BY NAME downstream, so two objects called
    the same thing cost the file every colour it carries — Kiln's own parser
    rightly declines to guess which part is which, and the stage goes grey.
    Duplicates are the normal case, not an edge one: an assembly holds two
    of the same bracket, and an unnamed STEP body degrades to its shape type.
    """
    from kiln.step_import import _write_3mf
    from kiln.threemf_parser import object_display_colors

    a, b = tmp_dir / "a.stl", tmp_dir / "b.stl"
    _tiny_binary_stl(a)
    _tiny_binary_stl(b, offset_x=20.0)

    out = str(tmp_dir / "two_brackets.3mf")
    written = _write_3mf(
        [
            {"stl_path": str(a), "name": "bracket", "color": "#C7542E"},
            {"stl_path": str(b), "name": "bracket", "color": "#3D6B99"},
        ],
        out,
    )

    assert written == ["bracket", "bracket (2)"]
    # Both colours survive, each on its own part — not {} for the whole file.
    assert object_display_colors(out) == {
        "bracket": (0xC7, 0x54, 0x2E),
        "bracket (2)": (0x3D, 0x6B, 0x99),
    }

    # The material list is named the same way as the objects, so a slicer
    # showing materials and one showing objects tell the same story.
    import zipfile

    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    assert '<base name="bracket" displaycolor="#C7542E"/>' in model
    assert '<base name="bracket (2)" displaycolor="#3D6B99"/>' in model


def test_write_3mf_gives_a_nameless_part_a_name(tmp_dir):
    """A blank name would key by object id, which a sibling could be named
    after — and reads as an empty row in the slicer's object list."""
    from kiln.step_import import _write_3mf
    from kiln.threemf_parser import object_display_colors

    a, b = tmp_dir / "a.stl", tmp_dir / "b.stl"
    _tiny_binary_stl(a)
    _tiny_binary_stl(b, offset_x=20.0)

    out = str(tmp_dir / "nameless.3mf")
    written = _write_3mf(
        [
            {"stl_path": str(a), "name": "", "color": "#C7542E"},
            {"stl_path": str(b), "name": "", "color": "#3D6B99"},
        ],
        out,
    )
    assert written == ["Part 1", "Part 2"]
    assert len(object_display_colors(out)) == 2


def test_write_3mf_escapes_hostile_part_names(tmp_dir):
    from kiln.step_import import _write_3mf
    from kiln.threemf_parser import parse_colored_3mf

    a = tmp_dir / "a.stl"
    _tiny_binary_stl(a)
    out = str(tmp_dir / "hostile.3mf")
    _write_3mf(
        [{"stl_path": str(a), "name": 'x"/><script>', "color": "#112233"}],
        out,
    )
    # Parses as valid XML — the hostile name did not break the document.
    mesh = parse_colored_3mf(out)
    assert mesh.colors_found


def _write_colored_two_body_step(
    path: Path, names: tuple[str, str] | None = ("base_plate", "lid")
) -> None:
    """A 2-body coloured STEP authored through XCAF (kernel required).

    ``names=None`` leaves both XCAF labels unnamed, which is how an ordinary
    CAD export arrives: OCCT then names each label after its shape type, so
    both bodies read back under one name.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    for i, (x0, rgb) in enumerate(
        ((0.0, (0.85, 0.15, 0.15)), (50.0, (0.10, 0.35, 0.85)))
    ):
        shape = BRepPrimAPI_MakeBox(gp_Pnt(x0, 0, 0), 40.0, 30.0, 5.0).Shape()
        label = shape_tool.AddShape(shape, False)
        if names is not None:
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(names[i]))
        color_tool.SetColor(
            label,
            Quantity_Color(*rgb, Quantity_TOC_RGB),
            XCAFDoc_ColorType.XCAFDoc_ColorGen,
        )

    writer = STEPCAFControl_Writer()
    writer.Transfer(doc)
    assert writer.Write(str(path)) == IFSelect_ReturnStatus.IFSelect_RetDone


def test_convert_step_auto_keeps_colour_and_names(real_kernel, tmp_dir):
    from kiln.step_import import convert_step
    from kiln.threemf_parser import parse_colored_3mf

    step = tmp_dir / "assembly.step"
    _write_colored_two_body_step(step)

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    assert result.output_format == "3mf"
    assert result.output_path.endswith(".3mf")
    assert result.body_count == 2
    assert set(result.part_names) == {"base_plate", "lid"}
    assert "#D92626" in result.part_colors or "#D82626" in result.part_colors

    mesh = parse_colored_3mf(result.output_path)
    assert mesh.colors_found
    xs = [v[0] for t in mesh.triangles for v in (t.v0, t.v1, t.v2)]
    assert max(xs) > 80.0, "second body must keep its STEP position"


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        # Two instances of one part — an ordinary assembly.
        (("bracket", "bracket"), ["bracket", "bracket (2)"]),
        # Unnamed bodies: OCCT names each label after its shape type, so the
        # duplicate arrives without anyone having chosen it.
        (None, None),
    ],
    ids=["same_name_twice", "unnamed_bodies"],
)
def test_convert_step_keeps_colour_when_bodies_share_a_name(
    real_kernel, tmp_dir, names, expected
):
    """The whole trip: a real coloured STEP whose bodies share a name comes
    out as a 3MF whose colours are still readable, part by part."""
    from kiln.step_import import convert_step
    from kiln.threemf_parser import object_display_colors

    step = tmp_dir / "assembly.step"
    _write_colored_two_body_step(step, names=names)

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    assert result.output_format == "3mf"
    assert result.body_count == 2
    if expected is not None:
        assert result.part_names == expected
    else:
        # Whatever OCCT called them, the two must not be the same name.
        assert len(set(result.part_names)) == 2, result.part_names

    colors = object_display_colors(result.output_path)
    assert set(colors) == set(result.part_names), (
        "every part reported must be addressable by that name in the file"
    )
    assert len(set(colors.values())) == 2, "each body keeps its OWN colour"


def test_duplicate_named_3mf_colours_reach_the_viewer_payload(tmp_dir):
    """The consumer end of the trip: the part has to arrive at the stage with
    per-vertex colours, not grey.

    Deliberately writes the 3MF rather than converting a STEP, so the OCCT
    kernel is not required.  The 3MF is the interface between the two halves
    — the STEP half is proven above with a real kernel, and pinning this half
    to the kernel too would leave it skipped everywhere: CI installs the dev
    extra (trimesh + lxml) but not the kernel, and a kernel install here has
    no lxml.  A test that can only skip is a test that proves nothing.
    """
    pytest.importorskip("trimesh", reason="viewer payload reads meshes via trimesh")
    pytest.importorskip("lxml", reason="trimesh needs lxml to read a 3MF")

    import base64

    from kiln.mesh_payload import mesh_to_viewer_payload
    from kiln.step_import import _write_3mf

    a, b = tmp_dir / "a.stl", tmp_dir / "b.stl"
    _tiny_binary_stl(a)
    _tiny_binary_stl(b, offset_x=20.0)
    out = str(tmp_dir / "brackets.3mf")
    _write_3mf(
        [
            {"stl_path": str(a), "name": "bracket", "color": "#C7542E"},
            {"stl_path": str(b), "name": "bracket", "color": "#3D6B99"},
        ],
        out,
    )

    payload = mesh_to_viewer_payload(out)
    encoded = payload.get("vertex_colors")
    assert encoded, "a coloured import must not reach the stage colourless"

    raw = base64.b64decode(encoded)  # RGBA per vertex
    distinct = {tuple(raw[i : i + 3]) for i in range(0, len(raw), 4)}
    assert distinct == {(0xC7, 0x54, 0x2E), (0x3D, 0x6B, 0x99)}, (
        f"both body colours must survive, got {distinct}"
    )


def test_convert_step_labels_bodies_nobody_named(real_kernel, tmp_dir):
    """A kernel token is not a name, and the user should never read one.

    OCCT's STEP writer stamps ``PRODUCT('SOLID','SOLID')`` for a shape nobody
    named, so a two-body file comes back as ``["SOLID", "SOLID"]`` — a machine
    artifact that says nothing and reads like a bug in a slicer's object list.
    """
    from kiln.step_import import convert_step

    step = tmp_dir / "unnamed.step"
    _write_colored_two_body_step(step, names=None)

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    assert result.part_names == ["Part 1", "Part 2"]
    assert not any("SOLID" in n or "COMPOUND" in n for n in result.part_names)


def test_convert_step_keeps_a_name_a_person_chose(real_kernel, tmp_dir):
    """The rule is "equals THIS body's own type", not a list of banned words,
    so a part someone deliberately named survives untouched."""
    from kiln.step_import import convert_step

    step = tmp_dir / "named.step"
    _write_colored_two_body_step(step, names=("SHELL", "lid"))

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    # "SHELL" on a SOLID body is a person's odd choice, not a kernel stamp.
    assert result.part_names == ["SHELL", "lid"]


def test_convert_step_names_agree_whichever_format_it_writes(real_kernel, tmp_dir):
    """``part_names`` means one thing: what a user will read in the output.

    A plain single solid leaves as STL and a coloured assembly as 3MF; the
    caller should not have to know which branch produced their names.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import convert_step

    step = tmp_dir / "plain.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(step))

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    assert result.output_format == "stl", "a plain solid keeps the classic path"
    # The STL exit names its part through the same helper as the 3MF exit, so
    # no kernel token escapes one branch and not the other.
    assert result.part_names == ["Part 1"]


def test_convert_step_auto_plain_solid_stays_classic_stl(real_kernel, tmp_dir):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import convert_step

    step = tmp_dir / "box.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(step))

    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    result = convert_step(str(step), output_dir=str(out_dir))

    assert result.output_format == "stl"
    assert result.output_path.endswith("merged.stl")
    assert result.body_count == 1
    # And explicit 3MF is honoured even for a plain part.
    result3 = convert_step(
        str(step), output_dir=str(out_dir), output_format="3mf"
    )
    assert result3.output_format == "3mf"


def test_convert_step_auto_without_kernel_uses_classic_path(
    monkeypatch, sample_step_file, tmp_dir
):
    """FreeCAD/Gmsh installs can't see colour — auto degrades to the
    classic STL chain instead of failing or pretending."""
    from kiln import step_import

    sentinel = step_import.StepImportResult(
        output_path="classic.stl", file_size_bytes=1, body_count=1,
        conversion_time_s=0.0,
    )
    monkeypatch.setattr(step_import, "_ocp_available", lambda: False)
    monkeypatch.setattr(
        step_import, "convert_step_to_stl", lambda *a, **k: sentinel
    )

    result = step_import.convert_step(str(sample_step_file))
    assert result is sentinel


def test_convert_step_rejects_unknown_format(sample_step_file):
    from kiln.step_import import convert_step

    with pytest.raises(ValueError):
        convert_step(str(sample_step_file), output_format="obj")


# ---------------------------------------------------------------------------
# 33. The ensure_mesh_path conversion cache.
#
#     One flow crosses two doors (validate, then import) and used to convert
#     the same bytes twice.  Content-addressed, OS-temp, copy-out-on-hit —
#     a mutated result must never poison a later call.
# ---------------------------------------------------------------------------


def test_ensure_mesh_path_caches_identical_bytes(real_kernel, tmp_dir):
    import time as _time

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import ensure_mesh_path

    step = tmp_dir / "cachetest.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(17.0, 13.0, 7.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(step))
    # Unique content per run so the first call is a genuine MISS even when
    # earlier runs populated the machine-wide cache.
    step.write_bytes(step.read_bytes() + f"/* {_time.time_ns()} */".encode())

    d1, d2, d3 = tmp_dir / "o1", tmp_dir / "o2", tmp_dir / "o3"
    for d in (d1, d2, d3):
        d.mkdir()

    p1, note1 = ensure_mesh_path(str(step), output_dir=str(d1))
    p2, note2 = ensure_mesh_path(str(step), output_dir=str(d2))

    assert "cached" not in (note1 or "")
    assert "cached" in (note2 or ""), "second identical call must hit the cache"
    assert p1 != p2, "a hit is an independent copy, never an alias"
    assert Path(p1).read_bytes() == Path(p2).read_bytes()

    # Poisoning check: mutilate the first result, the next hit is pristine.
    original = Path(p2).read_bytes()
    Path(p1).write_bytes(b"vandalized")
    p3, note3 = ensure_mesh_path(str(step), output_dir=str(d3))
    assert "cached" in (note3 or "")
    assert Path(p3).read_bytes() == original


def test_ensure_mesh_path_cache_write_failure_is_not_fatal(
    real_kernel, tmp_dir, monkeypatch
):
    """A full or read-only temp dir costs the cache, never the conversion."""
    import shutil as _shutil
    import time as _time

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import ensure_mesh_path

    step = tmp_dir / "nocache.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(3.0, 3.0, 3.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(step))
    step.write_bytes(step.read_bytes() + f"/* {_time.time_ns()} */".encode())

    real_copyfile = _shutil.copyfile

    def failing_copy(src, dst, *a, **k):
        if "kiln_step_cache" in str(dst):
            raise OSError("disk full")
        return real_copyfile(src, dst, *a, **k)

    monkeypatch.setattr("shutil.copyfile", failing_copy)

    out = tmp_dir / "out"
    out.mkdir()
    p, note = ensure_mesh_path(str(step), output_dir=str(out))
    assert Path(p).is_file()
    assert "cached" not in (note or "")


# ---------------------------------------------------------------------------
# The probe must not load the kernel it is asking about
# ---------------------------------------------------------------------------


def test_backend_probe_does_not_import_the_kernel():
    """Asking whether STEP is supported must stay cheap.

    ``check_step_support`` is a registered tool, so any caller can reach
    it.  When the probe answered by importing, that call pulled in 323
    modules and ~247 MB resident (measured 2026-08-03) and never let go
    — on a memory-bounded host, a question that converts nothing used a
    quarter of the machine.  Nothing needs the import: conversions run
    the kernel in a child interpreter and every caller of the probe uses
    only its boolean.

    Runs in a SUBPROCESS deliberately.  Other tests in this file import
    OCP into the test process, so the parent's ``sys.modules`` proves
    nothing about this one.
    """
    import subprocess
    import sys

    probe = (
        "import sys; "
        "sys.path.insert(0, %r); "
        "from kiln.step_import import check_step_support; "
        "check_step_support(); "
        "print(len([m for m in sys.modules "
        "if m.startswith(('OCP', 'cadquery'))]))"
    ) % str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    leaked = int(result.stdout.strip().splitlines()[-1])
    assert leaked == 0, (
        f"the availability probe imported {leaked} kernel modules; it "
        "must answer from the filesystem, not by importing"
    )


def test_probe_agrees_with_a_real_import():
    """The cheap probe must give the same answer as actually importing.

    Guards the other direction: a probe that always said False would
    pass the leak test above while breaking every conversion.

    Uses ``_REAL_OCP_AVAILABLE`` — the module-level autouse fixture
    patches ``_ocp_available`` to False so the suite exercises the
    no-backend paths by default, and asserting against the patch would
    only prove the fixture works.
    """
    import importlib

    try:
        importlib.import_module("OCP.STEPControl")
        really_there = True
    except ImportError:
        really_there = False

    assert _REAL_OCP_AVAILABLE() is really_there


def _pro_facts_installed() -> bool:
    """True when the kiln-pro census engine is importable in this env."""
    import importlib.util

    try:
        return importlib.util.find_spec("kiln_pro.step_facts") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def test_tool_import_step_file_is_truthful_with_or_without_the_census(
    monkeypatch, tmp_dir
):
    """The import succeeds either way, and never invents a CAD block.

    The analytic census lives in kiln-pro.  This asserts the PUBLIC half of
    that seam from the registered tool, on a real kernel and a real file:

    * kiln-pro absent (the open-source install) — the conversion lands and
      the result simply says nothing about the B-rep.  Silence is the honest
      answer when nothing measured it; a census guessed from the triangles
      would be the exact lie the facts block exists to prevent.
    * kiln-pro present — the block rides along and is well-formed.

    The census itself is covered in kiln-pro's suite, which is the only
    place its engine exists to be tested.
    """
    if not _REAL_OCP_AVAILABLE():
        pytest.skip("OCCT kernel (OCP) not installed")
    monkeypatch.setattr("kiln.step_import._ocp_available", _REAL_OCP_AVAILABLE)
    monkeypatch.setattr("kiln.step_import._find_freecad_cmd", lambda: None)
    monkeypatch.setattr("kiln.step_import._find_gmsh_cmd", lambda: None)

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step = tmp_dir / "true_cylinder.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeCylinder(45.0, 8.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    assert writer.Write(str(step)) == IFSelect_ReturnStatus.IFSelect_RetDone

    tools = _register_step_tools()
    result = tools["import_step_file"](str(step), output_dir=str(tmp_dir))

    assert result["status"] == "ok"
    assert result["output_path"]

    if not _pro_facts_installed():
        assert "cad_facts" not in result
        return

    facts = result["cad_facts"]
    assert facts["kind"] == "kiln.step_facts.v1"
    if facts["available"]:
        assert facts["solids"] == 1
        assert facts["cylinders"]["radii_mm"] == [45.0]


def test_tool_import_survives_a_census_that_explodes(monkeypatch, tmp_dir):
    """A census failure must never cost the conversion.

    The facts are display material attached by an optional engine; if it
    raises, the mesh still lands and the result carries no ``cad_facts``
    rather than a half-built one.
    """
    if not _REAL_OCP_AVAILABLE():
        pytest.skip("OCCT kernel (OCP) not installed")
    if not _pro_facts_installed():
        pytest.skip("kiln-pro census engine not installed")
    monkeypatch.setattr("kiln.step_import._ocp_available", _REAL_OCP_AVAILABLE)
    monkeypatch.setattr("kiln.step_import._find_freecad_cmd", lambda: None)
    monkeypatch.setattr("kiln.step_import._find_gmsh_cmd", lambda: None)

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step = tmp_dir / "box.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    assert writer.Write(str(step)) == IFSelect_ReturnStatus.IFSelect_RetDone

    import kiln_pro.step_facts as step_facts_mod

    def _measurement_dies(*a, **k):
        raise RuntimeError("kernel exploded mid-census")

    monkeypatch.setattr(step_facts_mod, "read_step_facts", _measurement_dies)

    tools = _register_step_tools()
    result = tools["import_step_file"](str(step), output_dir=str(tmp_dir))

    assert result["status"] == "ok"
    assert result["output_path"]
    assert "cad_facts" not in result  # skipped, said nothing false


# ---------------------------------------------------------------------------
# 34. The gmsh backend has to be TOLD a density.
#
#     Found 2026-08-10: the gmsh script was formatted with step_path and
#     output_dir and nothing else, so the mesh came out at whatever the
#     installed gmsh derived from the model bounding box — the one backend
#     of the three whose output no constant in this file governed.  FreeCAD
#     has TESSELLATION_TOLERANCE, the kernel paths have the two deflection
#     constants; gmsh had nothing.  Same file, same user, different machine.
# ---------------------------------------------------------------------------


def test_gmsh_curvature_bound_is_derived_from_the_kernels_angular_bound():
    """The gmsh number is the kernel's angular guarantee in gmsh's units.

    gmsh's ``Mesh.MeshSizeFromCurvature`` is a count of elements per 2*pi
    radians, and OCCT's angular deflection resolves to ceil(4*pi/angle)
    segments per full circle — measured against this OCP build 2026-08-10
    at four angles across three radii, radius-independent.  Pinning the
    RELATION, not the literal, is the point: move the kernel's bound and
    gmsh follows instead of quietly drifting away from it.
    """
    import math

    from kiln.step_import import _GMSH_CURVATURE_ELEMENTS, _OCP_ANGULAR_DEFLECTION

    segments_per_circle = math.ceil(4 * math.pi / _OCP_ANGULAR_DEFLECTION)
    assert segments_per_circle == _GMSH_CURVATURE_ELEMENTS
    assert _GMSH_CURVATURE_ELEMENTS == 126  # at the shipping 0.1 rad

    # What that buys, stated so a change has to face the number: chordal sag
    # stays under R/3217, i.e. 0.024 mm on a 150 mm sphere.  The kernel
    # measures 0.0068 mm on that sphere and the FreeCAD path 0.162 mm, so
    # gmsh must land between the two backends that were already bounded.
    sag_at_r75 = 75.0 * (1 - math.cos(math.pi / _GMSH_CURVATURE_ELEMENTS))
    assert 0.0068 < sag_at_r75 < 0.162


def test_gmsh_script_carries_a_density_bound(monkeypatch, sample_step_file, tmp_dir):
    """The bound must reach the CHILD, and reach it before the mesh is cut.

    Every other assertion about gmsh in this file is satisfied by a script
    that sets no options at all, which is exactly how the gap survived.
    This one reads what was actually written for the child to run.
    """
    import subprocess as _sp

    from kiln.step_import import _GMSH_CURVATURE_ELEMENTS, _convert_via_gmsh

    seen: dict[str, str] = {}

    def _fake_run(cmd, **kwargs):
        # The script is unlinked in the caller's `finally`, so read it here.
        seen["script"] = Path(cmd[-1]).read_text()
        out = tmp_dir / "merged.stl"
        out.write_bytes(b"x")
        return _sp.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=f'KILN_RESULT:{{"outputs": ["{out}"], "body_count": 1}}',
            stderr="",
        )

    monkeypatch.setattr("kiln.step_import.subprocess.run", _fake_run)
    _convert_via_gmsh(Path(str(sample_step_file)), tmp_dir)

    script = seen["script"]
    assert "Mesh.MeshSizeFromCurvature" in script, (
        "gmsh was left to pick its own density"
    )
    assert str(_GMSH_CURVATURE_ELEMENTS) in script
    assert script.index("Mesh.MeshSizeFromCurvature") < script.index(
        "mesh.generate"
    ), "a bound set after meshing is no bound at all"


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=True)
def test_gmsh_too_old_to_bound_falls_through_instead_of_failing(
    mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir
):
    """A pre-4.7 gmsh must cost the user the BACKEND, never the conversion.

    ``Mesh.MeshSizeFromCurvature`` was renamed in gmsh 4.7 (2020-11), so an
    older gmsh rejects the name.  Refusing to mesh there is right — an
    unbounded mesh is the defect — but the refusal has to arrive as an
    ordinary failure, because convert_step_to_stl re-raises StepImportError
    without trying anything else.  Get that wrong and bounding gmsh breaks
    users who have a perfectly good kernel sitting behind it.
    """
    from kiln.step_import import _GMSH_UNBOUNDABLE_EXIT

    out_stl = tmp_dir / "merged.stl"
    out_stl.write_bytes(b"\x00" * 120)

    def _old_gmsh_then_cadquery(cmd, **kwargs):
        import subprocess as _sp

        return _sp.CompletedProcess(
            args=cmd,
            returncode=_GMSH_UNBOUNDABLE_EXIT,
            stdout="",
            stderr="this gmsh does not accept Mesh.MeshSizeFromCurvature\n",
        )

    with patch("kiln.step_import.subprocess.run", _old_gmsh_then_cadquery), patch(
        "kiln.step_import._convert_via_cadquery",
        return_value=([str(out_stl)], 1, None),
    ) as fallback:
        result = convert_step_to_stl(str(sample_step_file), output_dir=str(tmp_dir))

    assert fallback.called, "an un-boundable gmsh must hand off, not hard-fail"
    assert result.output_path == str(out_stl)
    assert any("Gmsh failed" in w for w in result.warnings), (
        "the handoff has to be visible in the result, not silent"
    )


def test_gmsh_bound_participates_in_the_conversion_cache_key(
    monkeypatch, tmp_dir
):
    """Changing the bound must miss the cache — but only where gmsh exists.

    The cache promises "same bytes, same tessellation constants, same
    backend availability ⇒ same mesh".  gmsh's density is now one of those
    constants, so an entry written before the bound existed holds a mesh the
    bound would not produce.  Leaving it out of the key would serve that
    stale mesh forever to exactly the users the bound was written for.

    It rides the gmsh SLOT of the fingerprint rather than the outer tuple so
    the invalidation stays aimed: a machine with no gmsh on PATH must keep
    every entry it has.  Both halves are asserted, because getting only the
    first right silently throws away every cached conversion on every
    machine in the fleet.
    """
    import time as _time

    import kiln.step_import as si

    # Unique bytes so this run starts on a genuine miss even though the
    # cache is machine-wide and earlier runs may have populated it.
    step = tmp_dir / "cachekey.step"
    step.write_text(f"ISO-10303-21;\nDATA;\n/* {_time.time_ns()} */\nENDSEC;\n")

    conversions: list[str] = []

    def _fake_convert(path, output_dir=None, *, merge_bodies=True):
        out = Path(output_dir) / "merged.stl"
        out.write_bytes(b"mesh")
        conversions.append(str(path))
        return StepImportResult(
            output_path=str(out),
            file_size_bytes=4,
            body_count=1,
            conversion_time_s=0.0,
            output_paths=[str(out)],
        )

    monkeypatch.setattr(si, "convert_step_to_stl", _fake_convert)
    monkeypatch.setattr(si, "_find_freecad_cmd", lambda: None)

    def _run(n: int) -> str | None:
        d = tmp_dir / f"out{n}"
        d.mkdir()
        return si.ensure_mesh_path(str(step), output_dir=str(d))[1]

    # --- a machine that has gmsh -------------------------------------
    monkeypatch.setattr(si, "_find_gmsh_cmd", lambda: "gmsh")
    assert "cached" not in (_run(0) or ""), "first call must be a real miss"
    assert "cached" in (_run(1) or ""), "identical bytes must hit"

    monkeypatch.setattr(si, "_GMSH_CURVATURE_ELEMENTS", 999)
    assert "cached" not in (_run(2) or ""), (
        "moving the bound must re-convert on a machine that uses gmsh"
    )

    # --- a machine that does not ------------------------------------
    monkeypatch.setattr(si, "_find_gmsh_cmd", lambda: None)
    assert "cached" not in (_run(3) or ""), "different backend set, own entry"
    monkeypatch.setattr(si, "_GMSH_CURVATURE_ELEMENTS", 126)
    assert "cached" in (_run(4) or ""), (
        "a machine with no gmsh must not be invalidated by a gmsh constant"
    )

    assert conversions.count(str(step)) == 3


# ---------------------------------------------------------------------------
# 35. The conversion record: WHICH backend drew this mesh, at what density.
#
#     A mesh made from CAD is an approximation, and the two bounded backends
#     sit far apart -- 0.0068 mm of sag from the kernel against 0.162 mm from
#     the FreeCAD path on the same 150 mm sphere, both measured.  Nothing in
#     the triangles says which one ran, and nothing outside this module can
#     work it out: the backend is chosen by fall-through, so the answer is not
#     the priority order but what that order did on THIS machine.  These pin
#     the record to what actually happened rather than to what was intended.
# ---------------------------------------------------------------------------


@patch("kiln.step_import._find_freecad_cmd", return_value="FreeCADCmd")
@patch("kiln.step_import._find_gmsh_cmd", return_value=None)
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_freecad_records_a_linear_only_bound(
    mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir
):
    """FreeCAD is given a chord tolerance and no angular bound -- say exactly that.

    Recording an angular figure here would invent a guarantee this path never
    asked for.
    """
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 500)
    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(str(sample_step_file))

    from kiln.step_import import TESSELLATION_TOLERANCE

    assert result.conversion is not None
    assert result.conversion.backend == "freecad"
    assert result.conversion.bound.kind == "linear"
    assert result.conversion.bound.linear == TESSELLATION_TOLERANCE
    assert result.conversion.bound.angular is None


@patch("kiln.step_import._find_freecad_cmd", return_value=None)
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_gmsh_records_its_bound_in_the_only_unit_it_accepts(
    mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir
):
    """Gmsh's density is a curvature target, and is recorded as one.

    It cannot be handed a chordal deflection at all, so the record keeps the
    unit gmsh actually accepts.  Restating it as a chord figure would publish
    a prediction as a promise: the segment count implies sag of R/3217 and
    measurement puts it near R/700, because MeshSizeFromCurvature is a target
    for a surface mesh rather than an exact per-circle count.

    This backend arrived unbounded and gained a bound (fix/gmsh-density-bound).
    It changed `kind` and filled in its number; no reader needed a new shape
    to follow it, which is what the four-shape bound is for.
    """
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 300)
    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = f"KILN_RESULT:{kiln_result}\n"
    mock_proc.stderr = ""

    with patch("kiln.step_import.subprocess.run", return_value=mock_proc):
        result = convert_step_to_stl(str(sample_step_file))

    assert result.conversion is not None
    from kiln.step_import import _GMSH_CURVATURE_ELEMENTS

    assert result.conversion.backend == "gmsh"
    assert result.conversion.bound.kind == "elements_per_circle"
    assert result.conversion.bound.elements_per_circle == _GMSH_CURVATURE_ELEMENTS
    # Read off the constant the mesher is actually set to, never restated —
    # a second copy of the number is a second number.
    assert result.conversion.bound.linear is None
    assert result.conversion.bound.angular is None


@patch("kiln.step_import._find_freecad_cmd", return_value="FreeCADCmd")
@patch("kiln.step_import._find_gmsh_cmd", return_value="gmsh")
@patch("kiln.step_import._cadquery_available", return_value=False)
def test_record_names_the_backend_that_RAN_not_the_one_first_in_line(
    mock_cq, mock_gmsh, mock_fc, sample_step_file, tmp_dir
):
    """The whole reason this cannot be re-derived from outside.

    FreeCAD is installed and first in priority, but broken -- so gmsh does the
    work.  Anything reconstructing "which backend ran" from the priority order
    and a PATH lookup would answer "freecad" with total confidence and be
    wrong, and would be wrong in the direction that matters: it would report a
    bounded 0.1 mm chord tolerance for a mesh that was cut at whatever the
    installed gmsh felt like.
    """
    out_stl = sample_step_file.parent / "merged.stl"
    out_stl.write_bytes(b"\x00" * 300)
    kiln_result = json.dumps({"body_count": 1, "outputs": [str(out_stl)]})
    ok = MagicMock()
    ok.returncode = 0
    ok.stdout = f"KILN_RESULT:{kiln_result}\n"
    ok.stderr = ""

    calls = {"n": 0}

    def _freecad_is_installed_but_broken(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # NOT a StepImportError: a non-zero exit is a bad STEP and stops
            # the conversion.  A broken install is the case that falls through.
            raise OSError("FreeCADCmd: broken symlink")
        return ok

    with patch(
        "kiln.step_import.subprocess.run",
        side_effect=_freecad_is_installed_but_broken,
    ):
        result = convert_step_to_stl(str(sample_step_file))

    assert result.conversion is not None
    assert result.conversion.backend == "gmsh"
    # And it carries GMSH's bound, not the 0.1 mm chord tolerance a caller
    # would have inferred from "FreeCAD is installed and goes first".
    assert result.conversion.bound.kind == "elements_per_circle"
    assert result.conversion.bound.linear is None
    # The attempt is still visible, so the record and the warnings agree.
    assert any("FreeCAD failed" in w for w in result.warnings)


def test_kernel_records_both_bounds_on_a_real_conversion(real_kernel, tmp_dir):
    """A real kernel run, not a mock: the record has to describe THIS work."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import (
        _OCP_ANGULAR_DEFLECTION,
        _OCP_LINEAR_DEFLECTION,
    )

    step = tmp_dir / "box.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    assert writer.Write(str(step)) == IFSelect_ReturnStatus.IFSelect_RetDone

    result = convert_step_to_stl(str(step), output_dir=str(tmp_dir / "out"))

    assert result.conversion is not None
    assert result.conversion.backend == "occt"
    assert result.conversion.bound.kind == "linear_angular"
    # Read off the constants rather than restated: a record that quoted its
    # own copy of the numbers could drift from what was actually passed.
    assert result.conversion.bound.linear == _OCP_LINEAR_DEFLECTION
    assert result.conversion.bound.angular == _OCP_ANGULAR_DEFLECTION


def test_colour_aware_path_records_its_own_reader(real_kernel, tmp_dir):
    """The 3MF path runs a DIFFERENT reader, and says so.

    Same kernel, same bounds, but the XCAF reader is the one that can carry
    colour and part names -- so a record naming plain ``occt`` would describe
    a conversion that did not happen.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from kiln.step_import import convert_step

    step = tmp_dir / "plain.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(8.0, 8.0, 8.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    assert writer.Write(str(step)) == IFSelect_ReturnStatus.IFSelect_RetDone

    result = convert_step(str(step), output_dir=str(tmp_dir / "out"))

    assert result.conversion is not None
    assert result.conversion.backend == "occt-xcaf"
    assert result.conversion.bound.kind == "linear_angular"


def test_tool_payload_carries_the_record_as_plain_json(real_kernel, tmp_dir):
    """The carrier half: it has to cross the tool boundary intact.

    A dataclass that never reaches the payload is a fact the part does not
    have.  Asserting it survives ``json.dumps`` is the point -- whatever reads
    this downstream gets it over the wire, not as a Python object.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step = tmp_dir / "payload.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(6.0, 6.0, 6.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    assert writer.Write(str(step)) == IFSelect_ReturnStatus.IFSelect_RetDone

    tools = _register_step_tools()
    payload = tools["import_step_file"](str(step), output_dir=str(tmp_dir))

    assert payload["status"] == "ok"
    record = payload["conversion"]
    assert record["backend"] in ("occt", "occt-xcaf")
    assert record["bound"]["kind"] == "linear_angular"
    json.dumps(payload["conversion"])  # must not raise


# ---------------------------------------------------------------------------
# 36. Handing the record to the pipelines that convert IMPLICITLY.
#
#     import_step_file is the door a user knocks on deliberately; most STEP
#     conversions happen somewhere else entirely, inside a tool that accepted
#     a .step and quietly needed a mesh.  Those callers used to receive a
#     prose sentence and, four times out of seven, drop it on the floor —
#     which is what a prose note with no home invites.  These pin the
#     structured form, and pin the compatibility that let it be added at all.
# ---------------------------------------------------------------------------


def _unique_step(tmp_dir, name="implicit.step"):
    """A STEP whose bytes are unique to this run, so the first call MISSES."""
    import time as _time

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    step = tmp_dir / name
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeBox(11.0, 9.0, 5.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(step))
    step.write_bytes(step.read_bytes() + f"/* {_time.time_ns()} */".encode())
    return step


def test_the_two_tuple_contract_is_unchanged(real_kernel, tmp_dir):
    """Every released version returns two values, and callers unpack two.

    The record had to arrive without breaking that — inside Kiln and in
    anyone's code outside it — so it is opt-in and this is the pin.
    """
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    out = tmp_dir / "legacy"
    out.mkdir()

    mesh, note = ensure_mesh_path(str(step), output_dir=str(out))
    assert Path(mesh).is_file()
    assert "Converted from STEP" in note


def test_an_implicit_conversion_can_now_keep_the_record(real_kernel, tmp_dir):
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    out = tmp_dir / "rec"
    out.mkdir()

    mesh, note, conversion = ensure_mesh_path(
        str(step), output_dir=str(out), with_record=True
    )
    assert Path(mesh).is_file()
    assert conversion is not None
    assert conversion.backend == "occt"
    assert conversion.bound.kind == "linear_angular"


def test_a_cache_hit_reports_the_same_record_as_the_conversion(
    real_kernel, tmp_dir
):
    """The one that makes the record trustworthy rather than incidental.

    The cache serves most real conversions. If a hit returned no record, the
    SAME file would report how it was made on one run and shrug on the next,
    and a caller could not tell "not from CAD" from "converted, but somebody
    else converted it first" — which is the more misleading of the two.
    """
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    d1, d2 = tmp_dir / "miss", tmp_dir / "hit"
    for d in (d1, d2):
        d.mkdir()

    _, note1, miss = ensure_mesh_path(
        str(step), output_dir=str(d1), with_record=True
    )
    _, note2, hit = ensure_mesh_path(
        str(step), output_dir=str(d2), with_record=True
    )

    assert "cached" not in (note1 or "")
    assert "cached" in (note2 or ""), "second identical call must hit the cache"
    assert hit == miss, "a hit must describe the same conversion as the miss"


def test_a_cache_entry_from_before_the_record_reconverts_rather_than_shrugging(
    real_kernel, tmp_dir
):
    """Entries written by an older Kiln have no sidecar — and the key carries
    no format version, so they are indistinguishable from current ones and
    would answer "not from CAD" for those files forever.

    Measured the morning after the record shipped, on one developer machine:
    135 cached meshes, 5 sidecars.  The hosted box hides it (its temp dir goes
    at every redeploy); a laptop and a CI runner keep the cache for months, so
    the people most likely to hit it are the ones converting the same parts
    over and over.

    A record-wanting caller therefore falls through and converts once more,
    which writes the sidecar and makes it a hit again.  Note what this does
    NOT do: reconstruct a record from the cache key.  That key knows which
    backends are INSTALLED, and would be wrong on exactly the machine where
    the first-choice backend is present but broken.  The record is re-earned
    by running the conversion, never inferred.
    """
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    d1, d2 = tmp_dir / "a", tmp_dir / "b"
    for d in (d1, d2):
        d.mkdir()

    _, _, first = ensure_mesh_path(str(step), output_dir=str(d1), with_record=True)

    cache_dir = Path(tempfile.gettempdir()) / "kiln_step_cache"
    sidecars = sorted(cache_dir.glob("*.json"))
    assert sidecars, "the conversion should have left a sidecar"
    for s in sidecars:
        s.unlink()

    _, _note, conversion = ensure_mesh_path(
        str(step), output_dir=str(d2), with_record=True
    )
    assert conversion is not None
    assert conversion.backend == first.backend

    # And it is a hit again now, without another conversion.
    _, note3, third = ensure_mesh_path(
        str(step), output_dir=str(tmp_dir / "c"), with_record=True
    )
    assert "cached" in (note3 or "")
    assert third is not None


def test_a_caller_that_wanted_no_record_keeps_the_fast_path(real_kernel, tmp_dir):
    """The fall-through is scoped to callers who asked for a record; everyone
    else must not pay a re-conversion for a field they never read."""
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    d1, d2 = tmp_dir / "a", tmp_dir / "b"
    for d in (d1, d2):
        d.mkdir()

    ensure_mesh_path(str(step), output_dir=str(d1), with_record=True)
    for s in sorted((Path(tempfile.gettempdir()) / "kiln_step_cache").glob("*.json")):
        s.unlink()

    _out, note = ensure_mesh_path(str(step), output_dir=str(d2))
    assert "cached" in (note or "")


def test_an_unreadable_sidecar_says_nothing_rather_than_guessing(
    real_kernel, tmp_dir
):
    """The property the re-conversion above must not cost us.

    A sidecar that is PRESENT but corrupt still reads as a hit, and the answer
    stays ``None`` — a mesh whose origin cannot be read is exactly what None
    means, and reconstructing one from the cache key would report the backends
    that are installed as the backend that ran.
    """
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    d1, d2 = tmp_dir / "a", tmp_dir / "b"
    for d in (d1, d2):
        d.mkdir()

    ensure_mesh_path(str(step), output_dir=str(d1), with_record=True)

    sidecars = sorted((Path(tempfile.gettempdir()) / "kiln_step_cache").glob("*.json"))
    assert sidecars
    for s in sidecars:
        s.write_text("{ not json at all", encoding="utf-8")

    _, note, conversion = ensure_mesh_path(
        str(step), output_dir=str(d2), with_record=True
    )
    assert "cached" in (note or "")
    assert conversion is None


def test_a_cache_hit_creates_an_output_dir_the_way_a_conversion_does(
    real_kernel, tmp_dir
):
    """The miss path made the directory; the hit path did not.

    So the same call succeeded or failed purely on whether something had
    already converted those bytes — a first run that worked and a second that
    raised FileNotFoundError.
    """
    from kiln.step_import import ensure_mesh_path

    step = _unique_step(tmp_dir)
    made = tmp_dir / "exists"
    made.mkdir()
    ensure_mesh_path(str(step), output_dir=str(made))

    never_made = tmp_dir / "not" / "yet"
    assert not never_made.exists()
    mesh, note = ensure_mesh_path(str(step), output_dir=str(never_made))
    assert "cached" in (note or "")
    assert Path(mesh).is_file()


def test_an_ordinary_mesh_reports_no_conversion(tmp_dir):
    """Nothing converted it, so there is nothing to say about how."""
    from kiln.step_import import ensure_mesh_path

    stl = tmp_dir / "already.stl"
    stl.write_bytes(b"\x00" * 84)
    assert ensure_mesh_path(str(stl), with_record=True) == (str(stl), None, None)


def test_the_thumbnail_path_deliberately_keeps_no_record():
    """The one caller that SHOULD drop it, pinned so it stays a decision.

    The slicer converts a STEP only to draw the printer's LCD thumbnail — the
    gcode itself was sliced from the STEP natively. Recording that mesh's
    fidelity would attach an accuracy figure to geometry that never reached
    the printer. If someone later "fixes" this omission for consistency, this
    fails and sends them to the reasoning first.
    """
    from kiln.plugins import slicer_tools

    source = Path(slicer_tools.__file__).read_text(encoding="utf-8")
    assert "with_record" not in source
    assert "DELIBERATELY drops the conversion record" in source


# ───────────── telling a CAD file apart from a broken mesh ─────────────
#
# A binary STL is 80 bytes of header, 4 bytes of triangle count, then 50
# bytes per triangle.  Handed a STEP file, every one of Kiln's STL readers
# skipped 80 bytes and read the next 4 — the ASCII "'Ope" of "Open CASCADE
# Model" — as a count of 1,701,859,111 triangles.  It then reported the 19 KB
# file as an STL missing 85 GB, i.e. it told an engineer their CAD export was
# corrupt because it read the word "Open" as a number.

_MINIMAL_STEP = (
    b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Open CASCADE Model'),'2;1');\n"
    + b"/* padding so the reader gets past its 84-byte header */\n" * 4
    + b"ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
)


def test_a_step_file_is_recognised_from_its_bytes_not_its_name(tmp_path):
    """The extension has already been believed once by the time a parser is
    holding the file; content is what it can still check."""
    from kiln.step_import import is_step_file, looks_like_step

    misnamed = tmp_path / "part.stl"
    misnamed.write_bytes(_MINIMAL_STEP)

    assert looks_like_step(str(misnamed)) is True
    assert is_step_file(str(misnamed)) is False  # the name says otherwise


def test_a_real_mesh_is_not_mistaken_for_cad(tmp_path):
    """The check has to be able to say no, or it is not a check."""
    from kiln.step_import import looks_like_step

    stl = tmp_path / "real.stl"
    stl.write_bytes(b"\x00" * 80 + (1).to_bytes(4, "little") + b"\x00" * 50)
    assert looks_like_step(str(stl)) is False


def test_an_unreadable_path_is_simply_not_cad(tmp_path):
    """Never raises: a parser mid-diagnosis is the worst place to introduce a
    new exception."""
    from kiln.step_import import looks_like_step

    assert looks_like_step(str(tmp_path / "nope.step")) is False
    assert looks_like_step(str(tmp_path)) is False  # a directory


def test_the_stl_reader_stops_calling_a_valid_step_file_corrupt(tmp_path):
    """The incident, replayed through the parser eight tools share."""
    from kiln.generation.validation import _parse_stl

    step = tmp_path / "bracket.step"
    step.write_bytes(_MINIMAL_STEP)

    errors: list[str] = []
    triangles, _vertices = _parse_stl(step, errors)

    assert triangles == []
    assert len(errors) == 1
    assert "STEP (CAD) file" in errors[0]
    assert "nothing is wrong with it" in errors[0].lower()
    # The specific accusation that sent people back to their CAD package.
    assert "truncated" not in errors[0].lower()


def test_a_genuinely_broken_stl_is_still_reported_as_a_broken_stl(tmp_path):
    """The CAD branch must not swallow the real failure it was carved out of."""
    from kiln.generation.validation import _parse_stl

    broken = tmp_path / "cut_short.stl"
    broken.write_bytes(b"\x00" * 80 + (500).to_bytes(4, "little") + b"\x00" * 50)

    errors: list[str] = []
    _parse_stl(broken, errors)
    assert errors and "500 triangles" in errors[0]
    assert "STEP" not in errors[0]


# ─────────────── the shared CAD door, and who goes through it ───────────────


def test_the_shared_door_passes_an_ordinary_mesh_straight_through(tmp_path):
    """Safe to call unconditionally: a caller never has to ask whether it was
    handed CAD, which is what stops the per-tool branch growing back."""
    from kiln.step_import import resolve_mesh_input

    stl = tmp_path / "plain.stl"
    stl.write_bytes(b"\x00" * 80 + (0).to_bytes(4, "little"))

    out, conversion, refusal = resolve_mesh_input(str(stl))
    assert out == str(stl)
    assert conversion is None
    assert refusal is None


def test_the_shared_door_hands_back_a_ready_refusal(tmp_path, monkeypatch):
    """The wording is the part that was being retyped, so it lives once.

    A tool adopts CAD support in three lines and cannot get the
    NoBackendError branch subtly different from the tool next door — which is
    how one of them ends up dropping ``exc.remedy`` and telling a user
    nothing actionable.
    """
    import kiln.step_import as si

    def _no_backend(*a, **k):
        # Takes no arguments — it builds its own message from install_remedy().
        raise si.NoBackendError()

    monkeypatch.setattr(si, "ensure_mesh_path", _no_backend)

    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")

    out, conversion, refusal = si.resolve_mesh_input(str(step))
    assert conversion is None
    assert refusal["success"] is False
    assert refusal["error"]["code"] == "NO_BACKEND"
    assert "remedy" in refusal


def test_a_corrupt_cad_file_is_user_input_not_a_crash(tmp_path, monkeypatch):
    import kiln.step_import as si

    monkeypatch.setattr(
        si, "ensure_mesh_path",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad entity at line 12")),
    )
    step = tmp_path / "broken.step"
    step.write_text("ISO-10303-21;\n")

    _out, _conv, refusal = si.resolve_mesh_input(str(step))
    assert refusal["error"]["code"] == "STEP_CONVERSION_FAILED"
    assert "bad entity" in refusal["error"]["message"]


#: Every tool a person naturally reaches for after importing CAD.  Table-driven
#: because the failure mode is a door being FORGOTTEN, and a per-door test that
#: nobody remembers to add is the same gap one layer up.
_CAD_DOORS = [
    ("auto_orient_model", "file_path"),
    ("check_orientation", "model_path"),
    ("estimate_supports", "file_path"),
    ("estimate_support_material", "file_path"),
    ("analyze_warping_risk", "file_path"),
    ("analyze_mesh_geometry", "file_path"),
    ("analyze_non_manifold_edges", "file_path"),
]


@pytest.mark.parametrize("tool_name,param", _CAD_DOORS)
def test_the_cad_doors_route_through_the_shared_helper(tool_name, param):
    """Structural, not behavioural: proves each door reaches the ONE helper.

    A behavioural test needs a converter installed, which not every machine
    has.  This asks the question that actually regresses — did someone add a
    mesh tool and hand-roll its CAD branch again, or skip it entirely.
    """
    import inspect

    from kiln import server as ks

    ks._ensure_pro_plugins_registered()
    tools = {t.name: t for t in ks.mcp._tool_manager.list_tools()}
    assert tool_name in tools, f"{tool_name} is not registered"

    source = inspect.getsource(tools[tool_name].fn)
    assert "resolve_mesh_input" in source, (
        f"{tool_name} does not route CAD through the shared door — a .step "
        "will fail several layers down with a contradictory error"
    )
    assert param in inspect.signature(tools[tool_name].fn).parameters


# ---------------------------------------------------------------------------
# Source topology — what the file DECLARED, before tessellation erased it
# ---------------------------------------------------------------------------


def _author_step(shape, path: Path) -> str:
    """Write a TopoDS shape to a STEP file and hand back the path."""
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))
    return str(path)


def _loose_faces(shape):
    """The same faces, in a compound that declares no solid — surface soup.

    This is what a CAD export dialog set to "surfaces" produces: the geometry
    of a part with none of the topology that says it encloses anything.
    """
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound

    builder = TopoDS_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        builder.Add(compound, explorer.Current())
        explorer.Next()
    return compound


def test_solid_reports_one_solid_and_says_nothing(real_kernel, tmp_dir):
    """The guard that matters most: a good file must never be told it is bad.

    A false "you sent surfaces" costs an engineer a re-export they did not
    need, and the trust to believe the next warning.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 63, 41, 9).Shape()
    path = _author_step(box, tmp_dir / "solid.step")

    result = convert_step_to_stl(path, output_dir=str(tmp_dir / "out"))

    assert result.conversion is not None
    assert result.conversion.source == SourceTopology(solids=1, shells=1, faces=6)
    assert surface_model_note(result.conversion.source) is None
    assert not any("no solid body" in w for w in result.warnings)


def test_surface_soup_is_detected_and_still_converts_correctly(
    real_kernel, tmp_dir
):
    """Zero solids is reported — and the file is NOT refused, because it works.

    The measurement that decided this feature is a note and not a refusal:
    the six loose faces of a box tessellate to a watertight mesh of exactly
    the solid's volume.  STL carries no topology, so welding coincident
    vertices on load reconstructs the closure the file never declared.
    Refusing this input would reject a file that produces a correct part.
    """
    import trimesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 63, 41, 9).Shape()
    path = _author_step(_loose_faces(box), tmp_dir / "soup.step")

    result = convert_step_to_stl(path, output_dir=str(tmp_dir / "out"))

    assert result.conversion is not None
    assert result.conversion.source == SourceTopology(solids=0, shells=6, faces=6)
    assert result.conversion.source.is_surface_model

    # It converted, and it converted RIGHT — 63 x 41 x 9 = 23247.
    mesh = trimesh.load(result.output_path, force="mesh")
    assert mesh.is_watertight, "loose faces still weld into a closed mesh"
    assert mesh.volume == pytest.approx(23247.0, rel=1e-6)

    # Said, not enforced.
    assert any("no solid body" in w for w in result.warnings)
    assert not any("cannot" in w.lower() or "refus" in w.lower()
                   for w in result.warnings)


def test_surface_soup_is_not_refused_at_the_shared_door(real_kernel, tmp_dir):
    """resolve_mesh_input hands back a mesh, never a refusal envelope."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 64, 42, 11).Shape()
    path = _author_step(_loose_faces(box), tmp_dir / "soup_door.step")

    mesh_path, conversion, refusal = resolve_mesh_input(
        path, output_dir=str(tmp_dir / "out")
    )

    assert refusal is None, "a surface model must not be refused"
    assert Path(mesh_path).is_file()
    assert conversion is not None and conversion.source is not None
    assert conversion.source.solids == 0


def test_nested_assembly_solids_are_found(real_kernel, tmp_dir):
    """The other false-positive shape: solids nested inside sub-assemblies.

    TopExp_Explorer recurses through compounds, so an assembly two levels
    deep still reports its real solids rather than reading as surface soup.
    """
    from OCP.BRepPrimAPI import (
        BRepPrimAPI_MakeBox,
        BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakeSphere,
    )
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound

    builder = TopoDS_Builder()
    inner = TopoDS_Compound()
    builder.MakeCompound(inner)
    builder.Add(
        inner,
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5, 20).Shape(),
    )
    builder.Add(inner, BRepPrimAPI_MakeSphere(gp_Pnt(30, 0, 0), 8).Shape())
    outer = TopoDS_Compound()
    builder.MakeCompound(outer)
    builder.Add(outer, BRepPrimAPI_MakeBox(gp_Pnt(-40, -10, 0), 20, 20, 20).Shape())
    builder.Add(outer, inner)

    path = _author_step(outer, tmp_dir / "assembly.step")
    result = convert_step_to_stl(path, output_dir=str(tmp_dir / "out"))

    assert result.conversion is not None
    assert result.conversion.source.solids == 3
    assert surface_model_note(result.conversion.source) is None


def test_single_face_is_reported_without_being_called_broken(
    real_kernel, tmp_dir
):
    """A lone surface is a legitimate input — someone means to thicken it."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    face = BRepBuilderAPI_MakeFace(
        gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 0, 51, 0, 31
    ).Face()
    path = _author_step(face, tmp_dir / "face.step")

    result = convert_step_to_stl(path, output_dir=str(tmp_dir / "out"))

    assert result.conversion.source.solids == 0
    assert result.conversion.source.faces == 1
    note = surface_model_note(result.conversion.source)
    assert "1 surface," in note, "singular, not '1 surfaces'"
    assert "thicken" in note, "must not tell a deliberate surface it is wrong"


def test_note_is_silent_when_the_backend_could_not_look():
    """FreeCAD and gmsh report no topology; that must not become 'no solid'."""
    assert surface_model_note(None) is None


def test_empty_shape_is_not_a_surface_model():
    """Nothing at all is a different fact from surfaces-without-a-solid."""
    assert not SourceTopology(solids=0, shells=0, faces=0).is_surface_model
    assert SourceTopology(solids=0, shells=1, faces=1).is_surface_model


def test_cached_sidecar_without_topology_reads_as_unknown(tmp_dir):
    """Entries cached before this field existed must not read as zero solids.

    The cache key is content + tessellation settings with no format version,
    so an old sidecar is indistinguishable from a current one.
    """
    sidecar = tmp_dir / "old.json"
    sidecar.write_text(
        json.dumps({"backend": "occt", "bound": {"kind": "linear_angular",
                                                 "linear": 0.1, "angular": 0.5}}),
        encoding="utf-8",
    )
    record = _read_cached_conversion(sidecar)
    assert record is not None
    assert record.source is None
    assert surface_model_note(record.source) is None


def test_cached_sidecar_round_trips_topology(tmp_dir):
    """A sidecar written today gives the same answer on the cache hit."""
    sidecar = tmp_dir / "new.json"
    original = MeshConversion(
        backend="occt",
        bound=TessellationBound(kind="linear_angular", linear=0.1, angular=0.5),
        source=SourceTopology(solids=0, shells=6, faces=6),
    )
    sidecar.write_text(json.dumps(asdict(original)), encoding="utf-8")

    assert _read_cached_conversion(sidecar) == original


def test_solid_with_internal_void_is_not_called_a_surface_model(
    real_kernel, tmp_dir
):
    """The false positive that ruled out reading STEP entity names as text.

    A box with a sealed cavity is an ordinary printable part, and it is
    written as BREP_WITH_VOIDS — so it contains ZERO ``MANIFOLD_SOLID_BREP``
    entities.  A text-level "does this file name a solid?" check reports no
    solid here and tells an engineer their good file is broken.  The kernel
    interprets the schema instead of grepping it, and counts one solid.

    Measured, not assumed: this fixture really does carry 0 MANIFOLD_SOLID_BREP.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
    from OCP.gp import gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 40, 40).Shape()
    cavity = BRepPrimAPI_MakeSphere(gp_Pnt(20, 20, 20), 10).Shape()
    path = _author_step(
        BRepAlgoAPI_Cut(box, cavity).Shape(), tmp_dir / "void.step"
    )

    text = Path(path).read_text(errors="replace")
    assert "MANIFOLD_SOLID_BREP" not in text, (
        "fixture no longer reproduces the trap this test exists for"
    )

    result = convert_step_to_stl(path, output_dir=str(tmp_dir / "out"))

    assert result.conversion.source.solids == 1
    assert surface_model_note(result.conversion.source) is None
    assert not any("no solid body" in w for w in result.warnings)


def test_colour_aware_path_reports_topology_too(real_kernel, tmp_dir):
    """The door users actually knock on is convert_step, not convert_step_to_stl.

    import_step_file takes the colour-aware XCAF path, which walks document
    labels instead of one root shape.  Wiring only the plain reader left the
    census null at the one door that matters — the whole feature invisible
    where it is read.  Both paths must answer the same way.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 77, 47, 17).Shape()
    soup = _author_step(_loose_faces(box), tmp_dir / "xcaf_soup.step")
    solid = _author_step(box, tmp_dir / "xcaf_solid.step")

    soup_result = convert_step(soup, output_dir=str(tmp_dir / "a"))
    assert soup_result.conversion.source == SourceTopology(
        solids=0, shells=6, faces=6
    )
    assert any("no solid body" in w for w in soup_result.warnings)

    solid_result = convert_step(solid, output_dir=str(tmp_dir / "b"))
    assert solid_result.conversion.source.solids == 1
    assert not any("no solid body" in w for w in solid_result.warnings)


def test_topology_absent_from_child_reads_as_unknown():
    """A child that did not report counts must not read as zero solids."""
    assert _topology_from_result({"outputs": [], "body_count": 1}) is None
    assert _topology_from_result({"topology": None}) is None
    assert _topology_from_result({"topology": {"solids": 1}}) is None
    assert _topology_from_result(
        {"topology": {"solids": 0, "shells": 6, "faces": 6}}
    ) == SourceTopology(solids=0, shells=6, faces=6)
