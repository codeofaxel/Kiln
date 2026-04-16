"""Tests for kiln.model_visualizer."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln.model_visualizer import (
    _ANGLE_ROTATIONS,
    _CAMERA_ANGLES,
    _BoundingBoxInfo,
    _compile_scad_for_bbox,
    _find_openscad,
    _get_bounding_box,
    _make_scad_wrapper,
    visualize_model,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_stl(tmp_path: Path) -> Path:
    """Create a minimal valid binary STL with 1 triangle."""
    stl = tmp_path / "test.stl"
    header = b"\x00" * 80
    num_triangles = struct.pack("<I", 1)
    # Normal + 3 vertices + attribute byte count
    triangle = struct.pack("<fff", 0, 0, 1)  # normal
    triangle += struct.pack("<fff", 0, 0, 0)  # v1
    triangle += struct.pack("<fff", 1, 0, 0)  # v2
    triangle += struct.pack("<fff", 0, 1, 0)  # v3
    triangle += struct.pack("<H", 0)  # attribute
    stl.write_bytes(header + num_triangles + triangle)
    return stl


@pytest.fixture
def tmp_scad(tmp_path: Path) -> Path:
    """Create a minimal SCAD file."""
    scad = tmp_path / "test.scad"
    scad.write_text("cube([10, 10, 10]);")
    return scad


# ---------------------------------------------------------------------------
# _find_openscad
# ---------------------------------------------------------------------------


def test_find_openscad_on_path():
    """Should find openscad if it's on PATH."""
    with patch("shutil.which", side_effect=lambda p: "/usr/bin/openscad" if p == "openscad" else None):
        assert _find_openscad() == "openscad"


def test_find_openscad_not_found():
    """Should raise FileNotFoundError when no openscad binary exists."""
    with patch("shutil.which", return_value=None), pytest.raises(FileNotFoundError, match="OpenSCAD not found"):
        _find_openscad()


# ---------------------------------------------------------------------------
# _make_scad_wrapper
# ---------------------------------------------------------------------------


def test_make_scad_wrapper_stl(tmp_stl: Path):
    """Should create a wrapper .scad that imports the STL."""
    wrapper = _make_scad_wrapper(str(tmp_stl))
    try:
        assert wrapper != str(tmp_stl)
        content = Path(wrapper).read_text()
        assert "import(" in content
        assert str(tmp_stl) in content
    finally:
        os.unlink(wrapper)


def test_make_scad_wrapper_scad(tmp_scad: Path):
    """SCAD files should be returned directly, no wrapper."""
    result = _make_scad_wrapper(str(tmp_scad))
    assert result == str(tmp_scad)


# ---------------------------------------------------------------------------
# visualize_model — error cases
# ---------------------------------------------------------------------------


def test_visualize_file_not_found():
    result = visualize_model("/nonexistent/model.stl")
    assert result["success"] is False
    assert result["code"] == "FILE_NOT_FOUND"


def test_visualize_unsupported_format(tmp_path: Path):
    gcode = tmp_path / "test.gcode"
    gcode.write_text("G28\n")
    result = visualize_model(str(gcode))
    assert result["success"] is False
    assert result["code"] == "UNSUPPORTED_FORMAT"


def test_visualize_invalid_angles(tmp_stl: Path):
    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"):
        result = visualize_model(str(tmp_stl), angles=["nonexistent_angle"])
    assert result["success"] is False
    assert result["code"] == "INVALID_ANGLES"


def test_visualize_openscad_not_found(tmp_stl: Path):
    with patch("kiln.model_visualizer._find_openscad", side_effect=FileNotFoundError("not found")):
        result = visualize_model(str(tmp_stl))
    assert result["success"] is False
    assert result["code"] == "OPENSCAD_NOT_FOUND"


# ---------------------------------------------------------------------------
# visualize_model — successful rendering (mocked OpenSCAD)
# ---------------------------------------------------------------------------


def test_visualize_all_angles(tmp_stl: Path, tmp_path: Path):
    """Mock OpenSCAD to produce dummy PNGs and verify all 6 angles are rendered."""
    output_dir = str(tmp_path / "output")

    def mock_run(cmd, **kwargs):
        # Find the -o argument and create a dummy PNG there
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"fake-png-data")
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(str(tmp_stl), output_dir=output_dir)

    assert result["success"] is True
    assert result["rendered"] == 6
    assert result["failed"] == 0
    assert len(result["views"]) == 6

    angle_names = {v["angle"] for v in result["views"]}
    assert angle_names == {"isometric", "front", "right", "top", "bottom", "back"}

    for view in result["views"]:
        assert view["path"] is not None
        assert view["description"]


def test_visualize_subset_angles(tmp_stl: Path, tmp_path: Path):
    """Should only render requested angles."""
    output_dir = str(tmp_path / "output")

    def mock_run(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"fake-png-data")
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(
            str(tmp_stl), angles=["top", "bottom"], output_dir=output_dir,
        )

    assert result["success"] is True
    assert result["rendered"] == 2
    assert len(result["views"]) == 2


def test_visualize_partial_failure(tmp_stl: Path, tmp_path: Path):
    """If some angles fail, should still return partial success."""
    output_dir = str(tmp_path / "output")
    call_count = 0

    def mock_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        if call_count <= 3:
            # First 3 succeed
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_bytes(b"fake-png-data")
            mock.returncode = 0
        else:
            # Rest fail
            mock.returncode = 1
            mock.stderr = "render error"
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(str(tmp_stl), output_dir=output_dir)

    assert result["success"] is True  # partial success
    assert result["rendered"] == 3
    assert result["failed"] == 3


# ---------------------------------------------------------------------------
# Camera angles config
# ---------------------------------------------------------------------------


def test_camera_angles_have_six_entries():
    assert len(_CAMERA_ANGLES) == 6


def test_camera_angles_have_required_fields():
    for label, description in _CAMERA_ANGLES:
        assert isinstance(label, str)
        assert isinstance(description, str)
        assert label  # non-empty
        assert label in _ANGLE_ROTATIONS


# ---------------------------------------------------------------------------
# _get_bounding_box — pure SCAD path
# ---------------------------------------------------------------------------


class TestGetBoundingBoxPureScad:
    """_get_bounding_box compiles pure SCAD to STL for bbox measurement."""

    def test_pure_scad_uses_compile_path(self, tmp_scad: Path, tmp_stl: Path):
        """A pure parametric SCAD file (no import()) triggers compilation."""
        with patch(
            "kiln.model_visualizer._compile_scad_for_bbox",
            return_value=str(tmp_stl),
        ) as mock_compile:
            result = _get_bounding_box(str(tmp_scad))
        mock_compile.assert_called_once_with(str(tmp_scad))
        assert isinstance(result, _BoundingBoxInfo)
        assert result.distance > 0

    def test_scad_with_import_skips_compile(self, tmp_path: Path, tmp_stl: Path):
        """A SCAD file that uses import() reads the STL directly, no compile."""
        scad = tmp_path / "wrapper.scad"
        scad.write_text(f'import("{tmp_stl}");')
        with patch("kiln.model_visualizer._compile_scad_for_bbox") as mock_compile:
            result = _get_bounding_box(str(scad))
        mock_compile.assert_not_called()
        assert isinstance(result, _BoundingBoxInfo)

    def test_compile_failure_returns_default(self, tmp_scad: Path):
        """If compilation fails, fall back to default bbox (no crash)."""
        with patch(
            "kiln.model_visualizer._compile_scad_for_bbox",
            return_value=None,
        ):
            result = _get_bounding_box(str(tmp_scad))
        assert result.distance == 250  # _DEFAULT_DISTANCE


# ---------------------------------------------------------------------------
# _compile_scad_for_bbox
# ---------------------------------------------------------------------------


class TestCompileScadForBbox:
    """_compile_scad_for_bbox wraps OpenSCAD subprocess correctly."""

    def test_returns_stl_path_on_success(self, tmp_scad: Path, tmp_path: Path):
        """Returns a non-None path when OpenSCAD exits 0 and produces output."""
        fake_stl = tmp_path / "fake.stl"
        # Write > 84 bytes so size check passes.
        fake_stl.write_bytes(b"\x00" * 200)

        def _fake_mkstemp(suffix="", prefix="", **_kw):
            import os
            fd = os.open(str(fake_stl), os.O_WRONLY | os.O_CREAT)
            return fd, str(fake_stl)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("tempfile.mkstemp", side_effect=_fake_mkstemp), \
             patch("subprocess.run", return_value=mock_result):
            path = _compile_scad_for_bbox(str(tmp_scad))

        assert path == str(fake_stl)

    def test_returns_none_when_openscad_missing(self, tmp_scad: Path):
        """Returns None if OpenSCAD binary is not found."""
        with patch(
            "kiln.model_visualizer._find_openscad",
            side_effect=FileNotFoundError("not found"),
        ):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None

    def test_returns_none_on_nonzero_exit(self, tmp_scad: Path):
        """Returns None if OpenSCAD exits non-zero (compile error in SCAD)."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("subprocess.run", return_value=mock_result):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None

    def test_returns_none_on_timeout(self, tmp_scad: Path):
        """Returns None if OpenSCAD times out."""
        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("openscad", 30)):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None
