"""The slicer startup-crash retry — the PrusaSlicer twin of ``openscad_runner``.

The incident this replays: seven PrusaSlicer crash reports on one machine
between 2026-08-11 and 2026-08-17, every one SIGTRAP with libc's
``loc->decimal_point is NULL`` guard, every one 0.06–0.13s after launch —
the same macOS locale race :mod:`kiln.openscad_runner` already retries for
OpenSCAD, dead before the model mattered.  Before this retry, each one was
a user's slice failing for no reason that would not have recurred on the
very next try.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import slicer as slicer_mod
from kiln.slicer import SlicerError, SlicerInfo, slice_file

_TRAP = -int(signal.SIGTRAP)
_SEGV = -int(signal.SIGSEGV)

#: A G-code body whose tail carries the completeness footer.
_COMPLETE_GCODE = "G1 X0 Y0\n" * 8 + "; filament used [g] = 4.2\n"


class FakeSlicer:
    """Stands in for ``subprocess.run`` and records every launch.

    :param returncodes: One entry per launch; the final entry repeats, so
        ``[-5]`` crashes every time and ``[-5, 0]`` crashes once then works.
    :param write_on: Launch numbers (1-based) that write *content* to the
        ``--output`` path before the return code is reported — the shape of
        a process that produced output and then died on the way out.
    :param content: What those launches write.
    """

    def __init__(self, returncodes, *, write_on=(), content=_COMPLETE_GCODE):
        self._returncodes = list(returncodes)
        self._write_on = set(write_on)
        self._content = content
        self.launches = 0

    def __call__(self, argv, **kwargs):
        self.launches += 1
        rc = self._returncodes[min(self.launches - 1, len(self._returncodes) - 1)]
        if self.launches in self._write_on or (rc == 0 and 0 in self._write_on):
            out = argv[argv.index("--output") + 1]
            Path(out).write_text(self._content)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")


@pytest.fixture()
def stl(tmp_path: Path) -> Path:
    f = tmp_path / "model.stl"
    f.write_text("solid x\nendsolid x\n")
    return f


@pytest.fixture()
def prusa(tmp_path: Path):
    """Route find_slicer at a fake PrusaSlicer binary."""
    binary = tmp_path / "PrusaSlicer"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    info = SlicerInfo(name="PrusaSlicer", path=str(binary), version="2.9.4")
    with patch.object(slicer_mod, "find_slicer", return_value=info):
        yield info


def _slice(stl: Path, tmp_path: Path):
    return slice_file(str(stl), output_dir=str(tmp_path / "out"))


def test_startup_crash_is_retried_and_the_user_never_sees_it(
    stl: Path, tmp_path: Path, prusa
) -> None:
    """The incident, replayed: SIGTRAP milliseconds in, then a clean run."""
    fake = FakeSlicer([_TRAP, 0], write_on=(2,))
    with patch("subprocess.run", side_effect=fake):
        result = _slice(stl, tmp_path)
    assert result.success
    assert fake.launches == 2


def test_a_persistent_crash_still_fails_after_the_attempts_run_out(
    stl: Path, tmp_path: Path, prusa
) -> None:
    fake = FakeSlicer([_TRAP])
    with patch("subprocess.run", side_effect=fake):
        with pytest.raises(SlicerError):
            _slice(stl, tmp_path)
    assert fake.launches == slicer_mod._SLICE_ATTEMPTS


def test_a_crash_after_a_complete_write_is_salvaged_not_retried(
    stl: Path, tmp_path: Path, prusa
) -> None:
    """The pre-existing salvage rule outranks the retry: complete output
    from a crashed process is kept, and relaunching would throw away a
    good slice to re-earn it."""
    fake = FakeSlicer([_SEGV], write_on=(1,))
    with patch("subprocess.run", side_effect=fake):
        result = _slice(stl, tmp_path)
    assert result.success
    assert "AFTER writing complete G-code" in result.message
    assert fake.launches == 1


def test_a_real_error_exit_is_not_retried(stl: Path, tmp_path: Path, prusa) -> None:
    """Exit 1 is the slicer ANSWERING (bad model, bad profile) — retrying
    burns time to print the same message twice."""
    fake = FakeSlicer([1])
    with patch("subprocess.run", side_effect=fake):
        with pytest.raises(SlicerError):
            _slice(stl, tmp_path)
    assert fake.launches == 1


def test_a_late_crash_is_a_verdict_about_the_model_not_the_startup(
    stl: Path, tmp_path: Path, prusa, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside the startup window the crash is blamed on the geometry and
    surfaced, not re-rolled.  The window is shrunk to zero rather than the
    fake slowed down, so the test stays instant."""
    monkeypatch.setattr(slicer_mod, "_STARTUP_CRASH_WINDOW_S", 0.0)
    fake = FakeSlicer([_TRAP, 0], write_on=(2,))
    with patch("subprocess.run", side_effect=fake):
        with pytest.raises(SlicerError):
            _slice(stl, tmp_path)
    assert fake.launches == 1


def test_a_partial_write_from_a_crashed_run_never_poses_as_the_result(
    stl: Path, tmp_path: Path, prusa
) -> None:
    """The crashed launch leaves a TRUNCATED file (no footer); the retry
    must clear it, run again, and succeed on the clean write."""
    truncated_then_complete = FakeSlicer(
        [_TRAP, 0], write_on=(1, 2), content=_COMPLETE_GCODE
    )
    # First launch writes truncated content, second writes complete.
    real_call = truncated_then_complete.__call__

    def call(argv, **kwargs):
        truncated_then_complete._content = (
            "G1 X0 Y0\n" if truncated_then_complete.launches == 0 else _COMPLETE_GCODE
        )
        return real_call(argv, **kwargs)

    with patch("subprocess.run", side_effect=call):
        result = _slice(stl, tmp_path)
    assert result.success
    assert truncated_then_complete.launches == 2
    # And the message does NOT claim a salvage — this was a fresh clean run.
    assert "AFTER writing complete G-code" not in result.message


def test_orca_keeps_its_deterministic_bambu_verdict_unretried(tmp_path: Path) -> None:
    """-11 on the Orca dialect is a real answer with a real message; the
    retry must not crash the same way twice more to say the same thing."""
    binary = tmp_path / "BambuStudio"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    info = SlicerInfo(name="BambuStudio", path=str(binary), version="2.3.2")
    stl = tmp_path / "model.stl"
    stl.write_text("solid x\nendsolid x\n")

    launches = {"n": 0}

    def fake(argv, **kwargs):
        launches["n"] += 1
        return subprocess.CompletedProcess(argv, -11, stdout="", stderr="")

    with patch.object(slicer_mod, "find_slicer", return_value=info), \
         patch.object(slicer_mod, "slicer_cli_family", return_value=slicer_mod._CLI_BAMBU), \
         patch.object(slicer_mod, "resolve_slicer_profile", create=True), \
         patch.object(slicer_mod, "ini_to_settings", return_value={}), \
         patch.object(slicer_mod, "write_orca_presets") as presets, \
         patch("subprocess.run", side_effect=fake):
        presets.return_value.machine_path = tmp_path / "m.json"
        presets.return_value.process_path = tmp_path / "p.json"
        presets.return_value.filament_path = tmp_path / "f.json"
        profile = tmp_path / "profile.ini"
        profile.write_text("[print]\n")
        with pytest.raises(SlicerError, match="known crash"):
            slice_file(str(stl), output_dir=str(tmp_path / "out"), profile=str(profile))
    assert launches["n"] == 1
