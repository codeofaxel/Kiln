"""Tests for kiln.slicer — slicer discovery and slicing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Every bundled profile must survive a real slicer
# ---------------------------------------------------------------------------


def _real_prusaslicer() -> str | None:
    """A PrusaSlicer binary on this machine, or None."""
    import shutil

    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


def _write_cube(path: str, size: float = 20.0) -> str:
    """Smallest printable solid: a binary-STL cube."""
    import struct

    v = [
        (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
        (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return path


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestBundledProfilesActuallySlice:
    """Drive the real binary, because mocks cannot see this failure class.

    Every slicer test in this file mocks ``subprocess.run``, which is why a
    profile that PrusaSlicer refuses outright shipped: the refusal is a clean
    ``exit 0`` with no output file, so nothing short of the real binary
    distinguishes it from success.  Seven bundled Bambu profiles were in that
    state — see ``TestRelativeExtrusionNeedsLayerReset`` in
    ``test_slicer_profiles.py`` for the mechanism.
    """

    def _slice(self, printer_id: str, tmp_path) -> str:
        from kiln.slicer_profiles import resolve_slicer_profile

        tmp_path = Path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            profile=resolve_slicer_profile(printer_id),
            slicer_path=_real_prusaslicer(),
            output_dir=str(tmp_path / "out"),
            output_name=f"{printer_id}.gcode",
            timeout=300,
        )
        return result.output_path

    @pytest.mark.parametrize(
        "printer_id",
        [
            "bambu_a1", "bambu_a1_mini", "bambu_a2l", "bambu_h2s", "bambu_p1p",
            "bambu_p1s", "bambu_p2s", "bambu_x1c", "bambu_x1e",
        ],
    )
    def test_relative_e_profile_produces_gcode(self, printer_id, tmp_path) -> None:
        """All nine, not just the two that happened to declare layer_gcode."""
        assert os.path.getsize(self._slice(printer_id, tmp_path)) > 0

    def test_p2s_gcode_is_relative_e_and_resets_each_layer(self, tmp_path) -> None:
        """The output is correct, not merely non-empty."""
        import re

        body = Path(self._slice("bambu_p2s", tmp_path)).read_text(encoding="utf-8")
        assert re.search(r"^M83", body, re.M), "expected relative extrusion"
        assert not re.search(r"^M82", body, re.M), "absolute extrusion leaked in"
        layers = body.count(";LAYER_CHANGE")
        resets = len(re.findall(r"^G92 E0", body, re.M))
        assert layers > 1
        assert resets >= layers - 1, f"{resets} E resets across {layers} layers"

    @pytest.mark.slow
    def test_every_prusaslicer_profile_in_the_bundle(self, tmp_path) -> None:
        """The wider net: any profile we tell users to slice with must slice."""
        from kiln.slicer_profiles import get_slicer_profile, list_slicer_profiles

        failed = []
        for pid in list_slicer_profiles():
            if get_slicer_profile(pid).slicer != "prusaslicer":
                continue
            try:
                assert os.path.getsize(self._slice(pid, tmp_path / pid)) > 0
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{pid}: {str(exc)[:80]}")
        assert not failed, "bundled profiles that cannot slice:\n" + "\n".join(failed)


# ---------------------------------------------------------------------------
# CLI dialect detection
# ---------------------------------------------------------------------------


class TestSlicerCliFamily:
    """Kiln builds one argv shape; it must know when that shape is wrong.

    ``slice_file`` emits ``--export-gcode/--output/--load``, which OrcaSlicer
    and BambuStudio do not have.  Those users used to reach the subprocess and
    get ``Invalid option --export-gcode`` and exit 254 from a tool that had
    just told them it found their slicer.
    """

    def _info(self, path, version=None, name=None):
        from kiln.slicer import SlicerInfo

        return SlicerInfo(path=path, name=name or os.path.basename(path).lower(), version=version)

    def test_orca_banner_is_bambu_family(self):
        from kiln.slicer import slicer_cli_family

        assert slicer_cli_family(self._info("/x/OrcaSlicer", "OrcaSlicer-2.3.2:")) == "bambu"

    def test_bambustudio_is_recognised_by_filename(self):
        """Its --version is itself an invalid option, so the banner is a log line."""
        from kiln.slicer import slicer_cli_family

        banner = "[2026-08-11 03:39:29] [trace] Initializing StaticPrintConfigs"
        assert slicer_cli_family(self._info("/x/BambuStudio", banner)) == "bambu"

    def test_banner_beats_filename(self):
        """A binary named orca-slicer that reports PrusaSlicer is a PrusaSlicer."""
        from kiln.slicer import slicer_cli_family

        info = self._info("/x/orca-slicer", "PrusaSlicer-2.9.4 based on Slic3r (with GUI support)")
        assert slicer_cli_family(info) == "prusa"

    def test_prusaslicer_banner_mentioning_slic3r_is_prusa(self):
        from kiln.slicer import slicer_cli_family

        info = self._info("/x/PrusaSlicer", "PrusaSlicer-2.9.4 based on Slic3r (with GUI support)")
        assert slicer_cli_family(info) == "prusa"

    def test_unknown_binary_falls_open_to_prusa(self):
        """No banner and no recognisable name keeps the pre-existing path."""
        from kiln.slicer import slicer_cli_family

        assert slicer_cli_family(self._info("/x/mystery", None)) == "prusa"

    def test_slice_file_runs_the_orca_dialect_not_the_slic3r_one(self, tmp_path):
        """An Orca binary is DRIVEN now, and never handed a Slic3r flag.

        This replaces the refusal that stood here while the Orca command line
        was unimplemented.  What it asserts is the part that must not regress:
        the dialects are never mixed.  Whether the argv it builds actually
        slices is a question no mock can answer — ``test_slicer_orca.py``
        drives the real binary for that.
        """
        from kiln.slicer import SlicerInfo

        stl = _write_cube(str(tmp_path / "cube.stl"))
        fake = SlicerInfo(path="/x/OrcaSlicer", name="orcaslicer", version="OrcaSlicer-2.3.2:")
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            work = cmd[cmd.index("--outputdir") + 1]
            with open(os.path.join(work, "plate_1.gcode"), "w") as fh:
                fh.write("G28\n; filament used [mm] = 1\n")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("kiln.slicer.find_slicer", return_value=fake), \
             patch("kiln.slicer.subprocess.run", side_effect=fake_run):
            result = slice_file(stl, output_dir=str(tmp_path / "out"))

        assert result.success is True
        assert "--slice" in seen["cmd"]
        assert not {"--export-gcode", "--output", "--load"} & set(seen["cmd"])

    def test_not_found_message_names_every_slicer_kiln_can_drive(self):
        """The advice has to match the capability, and has been wrong both ways.

        It first offered "PrusaSlicer or OrcaSlicer" while every command Kiln
        built was PrusaSlicer-only, sending the group most likely to own
        OrcaSlicer — Bambu owners — into a wall.  The fix for that named
        PrusaSlicer alone, which went stale the moment the Orca command line
        landed: a user with nothing installed was told to install one of the
        two slicers that would now work.

        So this asserts the invariant rather than either specific list: the
        message names each dialect ``slice_file`` can actually run.
        """
        with patch("shutil.which", return_value=None), \
             patch("kiln.slicer._MACOS_PATHS", []), \
             patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SlicerNotFoundError) as exc:
                find_slicer()
        message = str(exc.value).lower()
        assert "prusaslicer" in message
        assert "orcaslicer" in message


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestRealBinariesClassifyCorrectly:
    """The false-positive risk is the whole concern: never refuse a PrusaSlicer."""

    def test_real_prusaslicer_is_prusa_family(self):
        from kiln.slicer import find_slicer, slicer_cli_family

        assert slicer_cli_family(find_slicer(_real_prusaslicer())) == "prusa"

    @pytest.mark.parametrize(
        "app", ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
                "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"],
    )
    def test_real_bambu_family_binaries(self, app):
        from kiln.slicer import find_slicer, slicer_cli_family

        if not os.path.isfile(app):
            pytest.skip(f"{os.path.basename(app)} not installed")
        assert slicer_cli_family(find_slicer(app)) == "bambu"
