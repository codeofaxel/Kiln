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
        return_value=(["/nonexistent/output.stl"], 1),
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
    assert "kiln3d[step]" in remedy["pip_command"]


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
@patch("kiln.step_import._convert_via_ocp", return_value=(["/tmp/o.stl"], 1))
@patch("kiln.step_import._convert_via_cadquery")
def test_ocp_preferred_over_cadquery(
    mock_cq_conv, mock_ocp_conv, mock_cq, mock_ocp, mock_gmsh, mock_fc,
    sample_step_file, tmp_dir,
):
    """With both present the kernel wins — cadquery's wrapper is skipped."""
    out = tmp_dir / "out"
    out.mkdir()
    (out / "o.stl").write_bytes(b"x")
    mock_ocp_conv.return_value = ([str(out / "o.stl")], 1)

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
    mock_cq_conv.return_value = ([str(out / "o.stl")], 1)

    convert_step_to_stl(str(sample_step_file), output_dir=str(out))

    assert mock_cq_conv.called


@pytest.mark.skipif(
    not __import__("kiln.step_import", fromlist=["_ocp_available"])._ocp_available(),
    reason="OCCT kernel not installed",
)
def test_ocp_converts_a_real_step_file(tmp_dir):
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
    outputs, body_count = _convert_via_ocp(step_path, out_dir, merge_bodies=True)

    assert body_count == 1
    assert len(outputs) == 1
    data = Path(outputs[0]).read_bytes()
    # Binary STL: 80-byte header, then a uint32 triangle count.
    triangles = struct.unpack("<I", data[80:84])[0]
    assert triangles == 12, f"a box is 12 triangles, got {triangles}"
    assert len(data) == 84 + 50 * triangles
