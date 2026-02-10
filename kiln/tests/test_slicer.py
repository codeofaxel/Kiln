"""Tests for kiln.slicer — slicer discovery and slicing."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kiln.slicer import (
    SlicerError,
    SlicerNotFoundError,
    SliceResult,
    SlicerInfo,
    find_slicer,
    slice_file,
    _get_version,
)


# ---------------------------------------------------------------------------
# find_slicer
# ---------------------------------------------------------------------------


class TestFindSlicer:
    """Tests for slicer binary discovery."""

    def test_explicit_path_found(self, tmp_path):
        """When an explicit path is given that exists, return it."""
        slicer = tmp_path / "prusa-slicer"
        slicer.write_text("#!/bin/sh\necho test")
        slicer.chmod(0o755)

        with patch("kiln.slicer._get_version", return_value="2.7.1"):
            info = find_slicer(str(slicer))

        assert info.path == str(slicer)
        assert info.version == "2.7.1"

    def test_explicit_path_not_found(self):
        """When an explicit path is given that doesn't exist, raise."""
        with pytest.raises(SlicerNotFoundError, match="not found or not executable"):
            find_slicer("/nonexistent/prusa-slicer")

    def test_auto_detect_on_path(self):
        """When a slicer is on PATH, find it."""
        with patch("shutil.which", return_value="/usr/bin/prusa-slicer"):
            with patch("kiln.slicer._get_version", return_value="2.7.1"):
                info = find_slicer()

        assert info.path == "/usr/bin/prusa-slicer"
        assert info.name == "prusa-slicer"

    def test_auto_detect_nothing_found(self):
        """When nothing is on PATH and no macOS apps, raise."""
        with patch("shutil.which", return_value=None):
            with patch("os.path.isfile", return_value=False):
                with patch.dict(os.environ, {}, clear=True):
                    with pytest.raises(SlicerNotFoundError):
                        find_slicer()

    def test_env_var_fallback(self, tmp_path):
        """KILN_SLICER_PATH env var is used as fallback."""
        slicer = tmp_path / "orca-slicer"
        slicer.write_text("#!/bin/sh\necho test")
        slicer.chmod(0o755)

        with patch("shutil.which", return_value=None):
            with patch("kiln.slicer._MACOS_PATHS", []):
                with patch.dict(os.environ, {"KILN_SLICER_PATH": str(slicer)}):
                    with patch("kiln.slicer._get_version", return_value=None):
                        info = find_slicer()

        assert info.path == str(slicer)


class TestGetVersion:
    """Tests for _get_version helper."""

    def test_version_captured(self):
        """Captures the first line of --version output."""
        mock_result = MagicMock()
        mock_result.stdout = "PrusaSlicer-2.7.1+linux-x64\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            version = _get_version("/usr/bin/prusa-slicer")

        assert version == "PrusaSlicer-2.7.1+linux-x64"

    def test_version_error_returns_none(self):
        """If --version fails, return None."""
        with patch("subprocess.run", side_effect=OSError("not found")):
            version = _get_version("/nonexistent")

        assert version is None


# ---------------------------------------------------------------------------
# slice_file
# ---------------------------------------------------------------------------


class TestSliceFile:
    """Tests for the slicing function."""

    def test_input_not_found(self):
        """Raise FileNotFoundError when input doesn't exist."""
        with pytest.raises(FileNotFoundError, match="not found"):
            slice_file("/nonexistent/model.stl")

    def test_unsupported_extension(self, tmp_path):
        """Raise SlicerError for unsupported file types."""
        bad_file = tmp_path / "model.txt"
        bad_file.write_text("not a model")

        with pytest.raises(SlicerError, match="Unsupported input format"):
            slice_file(str(bad_file))

    def test_successful_slice(self, tmp_path):
        """Successful slicing returns a SliceResult with output path."""
        stl = tmp_path / "benchy.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        out_dir = tmp_path / "output"
        expected_out = out_dir / "benchy.gcode"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "Done"
        mock_run.stderr = ""

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer", version="2.7.1"
            )
            with patch("subprocess.run", return_value=mock_run):
                # Create the expected output file so the post-check passes
                out_dir.mkdir()
                expected_out.write_text("; gcode")

                result = slice_file(
                    str(stl),
                    output_dir=str(out_dir),
                )

        assert result.success is True
        assert result.output_path == str(expected_out)
        assert result.slicer == "prusa-slicer"

    def test_slicer_failure(self, tmp_path):
        """SlicerError raised when slicer exits non-zero."""
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        mock_run = MagicMock()
        mock_run.returncode = 1
        mock_run.stdout = ""
        mock_run.stderr = "Error: bad geometry"

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", return_value=mock_run):
                with pytest.raises(SlicerError, match="exited with code 1"):
                    slice_file(str(stl))

    def test_timeout(self, tmp_path):
        """SlicerError raised on subprocess timeout."""
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
                with pytest.raises(SlicerError, match="timed out"):
                    slice_file(str(stl), timeout=300)

    def test_profile_not_found(self, tmp_path):
        """SlicerError when profile file doesn't exist."""
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with pytest.raises(SlicerError, match="Profile file not found"):
                slice_file(str(stl), profile="/nonexistent/profile.ini")

    def test_output_missing_after_slice(self, tmp_path):
        """SlicerError when slicer succeeds but output file not found."""
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "Done"
        mock_run.stderr = ""

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", return_value=mock_run):
                with pytest.raises(SlicerError, match="output file not found"):
                    slice_file(str(stl), output_dir=str(tmp_path / "nonexistent"))

    def test_custom_output_name(self, tmp_path):
        """Output name can be overridden."""
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        out_dir = tmp_path / "output"
        expected_out = out_dir / "custom.gcode"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""
        mock_run.stderr = ""

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", return_value=mock_run):
                out_dir.mkdir()
                expected_out.write_text("; gcode")

                result = slice_file(
                    str(stl),
                    output_dir=str(out_dir),
                    output_name="custom.gcode",
                )

        assert result.output_path == str(expected_out)


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Tests for dataclass to_dict methods."""

    def test_slicer_info_to_dict(self):
        info = SlicerInfo(path="/usr/bin/ps", name="prusa-slicer", version="2.7")
        d = info.to_dict()
        assert d["path"] == "/usr/bin/ps"
        assert d["name"] == "prusa-slicer"
        assert d["version"] == "2.7"

    def test_slice_result_to_dict(self):
        r = SliceResult(
            success=True,
            output_path="/tmp/out.gcode",
            slicer="prusa-slicer",
            message="Done",
            stderr="warning: thin wall",
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["output_path"] == "/tmp/out.gcode"
        assert "thin wall" in d["stderr"]
