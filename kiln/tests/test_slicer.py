"""Tests for kiln.slicer — slicer discovery and slicing."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kiln.slicer import (
    SlicerError,
    SliceResult,
    SlicerInfo,
    SlicerNotFoundError,
    _get_version,
    _merge_multicolor_gcode,
    find_slicer,
    slice_file,
    slice_multicolor_copies,
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
        with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(SlicerNotFoundError):
                    find_slicer()

    def test_env_var_fallback(self, tmp_path):
        """KILN_SLICER_PATH env var is used as fallback."""
        slicer = tmp_path / "orca-slicer"
        slicer.write_text("#!/bin/sh\necho test")
        slicer.chmod(0o755)

        with patch("shutil.which", return_value=None), patch("kiln.slicer._MACOS_PATHS", []):
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

        def fake_slicer_run(*args, **kwargs):
            # Written DURING the run, as the real slicer does — a file
            # written up front is a stale leftover, which slice_file now
            # removes before running.
            expected_out.write_text("; gcode")
            return mock_run

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer", version="2.7.1"
            )
            with patch("subprocess.run", side_effect=fake_slicer_run):
                out_dir.mkdir()
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
                with pytest.raises(SlicerError, match="output file was not created"):
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

        def fake_slicer_run(*args, **kwargs):
            expected_out.write_text("; gcode")
            return mock_run

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", side_effect=fake_slicer_run):
                out_dir.mkdir()
                result = slice_file(
                    str(stl),
                    output_dir=str(out_dir),
                    output_name="custom.gcode",
                )

        assert result.output_path == str(expected_out)

    def test_no_printer_flag_passed_to_slicer(self, tmp_path):
        """PrusaSlicer should NOT receive a --printer flag (it doesn't exist)."""
        stl = tmp_path / "mini.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")
        out_dir = tmp_path / "output"
        expected_out = out_dir / "mini.gcode"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""
        mock_run.stderr = ""

        def fake_slicer_run(*args, **kwargs):
            expected_out.write_text("; gcode")
            return mock_run

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", side_effect=fake_slicer_run) as mock_subprocess:
                out_dir.mkdir()
                slice_file(
                    str(stl),
                    output_dir=str(out_dir),
                )

        cmd = mock_subprocess.call_args.args[0]
        assert "--printer" not in cmd


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


# ---------------------------------------------------------------------------
# _merge_multicolor_gcode
# ---------------------------------------------------------------------------

SAMPLE_GCODE_0 = """\
; generated by PrusaSlicer
M83
G28
M104 S200

;BEFORE_LAYER_CHANGE
;Z:0.2
;LAYER_CHANGE
G1 Z0.2 F600
G1 X10 Y10 E0.5

;BEFORE_LAYER_CHANGE
;Z:0.4
;LAYER_CHANGE
G1 Z0.4 F600
G1 X20 Y20 E0.5
"""

SAMPLE_GCODE_1 = """\
; generated by PrusaSlicer
M83
G28
M104 S200

;BEFORE_LAYER_CHANGE
;Z:0.2
;LAYER_CHANGE
G1 Z0.2 F600
G1 X50 Y50 E0.5

;BEFORE_LAYER_CHANGE
;Z:0.4
;LAYER_CHANGE
G1 Z0.4 F600
G1 X60 Y60 E0.5
"""


class TestMergeMulticolorGcode:
    """Tests for _merge_multicolor_gcode."""

    def test_empty_bodies_raises(self):
        with pytest.raises(ValueError, match="No gcode bodies"):
            _merge_multicolor_gcode([])

    def test_single_body_passthrough(self):
        result = _merge_multicolor_gcode([SAMPLE_GCODE_0])
        assert result == SAMPLE_GCODE_0

    def test_two_bodies_merged_with_tool_changes(self):
        result = _merge_multicolor_gcode([SAMPLE_GCODE_0, SAMPLE_GCODE_1])

        # Should contain T0 and T1
        assert "\nT0\n" in result
        assert "\nT1\n" in result

        # Should have layer changes from both copies
        assert result.count(";LAYER_CHANGE") == 4

        # Should have header from first copy only (not duplicated)
        assert result.count("M83") <= 1  # header stripped from copy 1
        assert result.count("G28") <= 1

        # Should have gcode from both copies
        assert "G1 X10 Y10" in result
        assert "G1 X50 Y50" in result

    def test_three_bodies_has_t0_t1_t2(self):
        gcode_2 = SAMPLE_GCODE_1.replace("X50", "X90").replace("X60", "X100")
        result = _merge_multicolor_gcode([SAMPLE_GCODE_0, SAMPLE_GCODE_1, gcode_2])
        assert "\nT0\n" in result
        assert "\nT1\n" in result
        assert "\nT2\n" in result
        assert result.count(";LAYER_CHANGE") == 6


class TestSliceMulticolorCopies:
    """Tests for slice_multicolor_copies (mocked slicer)."""

    def test_count_less_than_2_raises(self):
        with pytest.raises(ValueError, match="count must be >= 2"):
            slice_multicolor_copies("/tmp/model.stl", 1)

    def test_successful_multicolor_slice(self, tmp_path):
        """Mocked end-to-end multicolor slicing."""
        import struct

        # Create a minimal binary STL (a single triangle)
        stl = tmp_path / "cube.stl"
        with open(stl, "wb") as f:
            f.write(b"\x00" * 80)  # header
            f.write(struct.pack("<I", 1))  # 1 triangle
            # Normal
            f.write(struct.pack("<3f", 0, 0, 1))
            # Vertices (a small triangle at origin)
            f.write(struct.pack("<3f", 0, 0, 0))
            f.write(struct.pack("<3f", 10, 0, 0))
            f.write(struct.pack("<3f", 5, 10, 0))
            # Attribute byte count
            f.write(struct.pack("<H", 0))

        # Mock slice_file to return fake gcode for each copy
        call_count = [0]

        def mock_slice_file(input_path, **kwargs):
            out_dir = kwargs.get("output_dir", str(tmp_path))
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"copy_{call_count[0]}.gcode")
            with open(out_path, "w") as f:
                f.write(
                    f"; copy {call_count[0]}\n"
                    f";BEFORE_LAYER_CHANGE\n"
                    f";Z:0.2\n"
                    f";LAYER_CHANGE\n"
                    f"G1 Z0.2 F600\n"
                    f"G1 X{call_count[0]*30} Y10 E0.5\n"
                )
            call_count[0] += 1
            return SliceResult(
                success=True,
                output_path=out_path,
                slicer="prusa-slicer",
                message="OK",
            )

        with patch("kiln.slicer.slice_file", side_effect=mock_slice_file):
            result = slice_multicolor_copies(
                str(stl),
                3,
                output_dir=str(tmp_path / "out"),
            )

        assert result.success is True
        assert result.output_path is not None
        assert os.path.isfile(result.output_path)

        with open(result.output_path) as f:
            content = f.read()

        assert "T0" in content
        assert "T1" in content
        assert "T2" in content
        assert content.count(";LAYER_CHANGE") == 3


class TestCrashAfterFinishing:
    """A slicer killed by a signal AFTER writing complete G-code must not
    read as failure.  PrusaSlicer 2.9.4's CLI segfaults in its
    object-conflict bookkeeping (Print::export_gcode → ConflictResult
    assignment) on stacked parts meeting at an exactly-coincident plane —
    the shape Kiln's banded multicolor compose produces — after the file
    is fully exported.  Salvage is narrow: signal deaths only, and only
    with the summary footer present; a reported error exit never salvages."""

    def _run(self, tmp_path, returncode, gcode_body, *, stale_body=None):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        expected_out = out_dir / "model.gcode"
        if stale_body is not None:
            # Leftover from a previous slice of the same model: the output
            # path is deterministic (input stem into the output dir), so
            # this is exactly what a crashed run finds already on disk.
            expected_out.write_text(stale_body)

        mock_run = MagicMock()
        mock_run.returncode = returncode
        mock_run.stdout = "Slicing result exported"
        mock_run.stderr = ""

        def fake_slicer_run(*args, **kwargs):
            # The mocked slicer writes DURING the run, as the real one does.
            # An earlier version of this helper wrote the file up front,
            # which made a stale leftover indistinguishable from fresh
            # output — the blindness that let the crash-salvage certify a
            # previous model's toolpath as this run's success.
            if gcode_body is not None:
                expected_out.write_text(gcode_body)
            return mock_run

        with patch("kiln.slicer.find_slicer") as mock_find:
            mock_find.return_value = SlicerInfo(
                path="/usr/bin/prusa-slicer", name="prusa-slicer"
            )
            with patch("subprocess.run", side_effect=fake_slicer_run):
                return slice_file(str(stl), output_dir=str(out_dir))

    def test_signal_death_with_complete_output_is_salvaged(self, tmp_path):
        result = self._run(
            tmp_path, -11,
            "G1 X1\nT1\nG1 X2\n; filament used [mm] = 427.40, 418.04\n; end\n",
        )
        assert result.success is True
        assert "signal 11" in result.message
        assert "verified complete" in result.message

    def test_signal_death_with_truncated_output_still_fails(self, tmp_path):
        with pytest.raises(SlicerError, match="exited with code -11"):
            self._run(tmp_path, -11, "G1 X1\nG1 X2\n")  # no footer — died mid-write

    def test_error_exit_never_salvages_even_with_complete_output(self, tmp_path):
        """A REPORTED error is the slicer telling us something went wrong —
        a complete-looking file does not overrule it."""
        with pytest.raises(SlicerError, match="exited with code 1"):
            self._run(
                tmp_path, 1,
                "G1 X1\n; filament used [mm] = 100.00\n",
            )

    def test_signal_death_with_no_output_still_fails(self, tmp_path):
        with pytest.raises(SlicerError, match="exited with code -11"):
            self._run(tmp_path, -11, None)

    def test_stale_gcode_from_a_previous_run_is_not_salvaged(self, tmp_path):
        """A slicer killed BEFORE writing anything must fail even though a
        previous run's complete file sits at the deterministic output path.
        Salvaging that file returns the PREVIOUS model's toolpath with
        success=True — and that path feeds start_print, so the failure
        mode is a real print of the wrong geometry."""
        with pytest.raises(SlicerError, match="exited with code -11"):
            self._run(
                tmp_path, -11, None,
                stale_body="G1 X9\n; filament used [mm] = 99.00\n; end\n",
            )

    def test_stale_gcode_survives_alongside_a_fresh_salvage(self, tmp_path):
        """A leftover file must not poison the LEGITIMATE salvage either:
        when this run does write complete G-code before the signal death,
        the fresh output wins."""
        result = self._run(
            tmp_path, -11,
            "G1 X1\nT1\n; filament used [mm] = 427.40\n; end\n",
            stale_body="G1 X9\n; filament used [mm] = 99.00\n; end\n",
        )
        assert result.success is True
        assert "verified complete" in result.message

    def test_stale_gcode_does_not_mask_an_exit_zero_run_that_wrote_nothing(
        self, tmp_path
    ):
        """Same leftover, non-signal shape: a slicer that exits 0 without
        writing must not have last run's file vouch for it."""
        with pytest.raises(SlicerError, match="output file was not created"):
            self._run(
                tmp_path, 0, None,
                stale_body="G1 X9\n; filament used [mm] = 99.00\n; end\n",
            )
