"""A Bambu estimate runs at the acceleration the machine will really run.

Kiln slices a Bambu print with no acceleration commands of its own and wraps
it after the maker's start sequence, so the printer prints at whatever that
sequence set.  PrusaSlicer, told nothing, times the print at 1500 mm/s².
Measured 2026-09-30 on an 80 x 55 x 28 mm enclosure through ``bambu_a1``:
1h19m at 1500, 1h05m at the sequence's own 6000 -- and the printer's screen
had been showing HALF the slicer's number on top of that, through a factor
that blamed the slicer for slowness the profiles had caused.

The limits ride in the estimate only.  The slicer-backed half proves the
G-code is untouched by them: every feedrate identical, and no motion
command written.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiln.slicer_profiles as sp_mod
from kiln.printers.bambu_3mf import start_sequence_motion
from kiln.slicer_orca import ini_to_settings
from kiln.slicer_profiles import (
    list_slicer_profiles,
    resolve_multiextruder_profile,
    resolve_slicer_profile,
)

_LIMIT_KEYS = (
    "machine_max_acceleration_extruding",
    "machine_max_acceleration_travel",
    "machine_max_acceleration_x",
    "machine_max_acceleration_y",
    "machine_max_feedrate_x",
    "machine_max_feedrate_y",
    "machine_max_jerk_x",
    "machine_max_jerk_y",
)


@pytest.fixture(autouse=True)
def _fresh_profile_cache() -> Iterator[None]:
    sp_mod._cache.clear()
    sp_mod._loaded = False
    sp_mod._temp_cache.clear()
    yield
    sp_mod._cache.clear()
    sp_mod._loaded = False
    sp_mod._temp_cache.clear()


def _ini(path: str) -> dict[str, str]:
    return ini_to_settings(path)


class TestTheStartSequenceIsRead:
    def test_the_a1(self) -> None:
        motion = start_sequence_motion("bambu_a1")
        assert motion["accel"] == 6000
        assert motion["max_accel_x"] == motion["max_accel_y"] == 12000
        assert motion["max_feedrate_x"] == 500
        assert motion["jerk_x"] == 9

    def test_a_calibration_block_that_slows_the_machine_wins(self) -> None:
        """The X1C sets 10000, then its optional flow-calibration block sets
        5000 and nothing restores it.  The slower answer is the honest one."""
        assert start_sequence_motion("bambu_x1c")["accel"] == 5000

    def test_an_axis_limit_below_the_working_acceleration_is_kept(self) -> None:
        """The A2L asks 10000 but caps each axis at 6000."""
        motion = start_sequence_motion("bambu_a2l")
        assert motion["accel"] == 10000
        assert motion["max_accel_x"] == 6000

    @pytest.mark.parametrize("model", ["ender3", "k1c", "bambu_not_a_model", "", None])
    def test_a_machine_with_no_sequence_of_its_own_gets_nothing(self, model) -> None:
        assert start_sequence_motion(model) is None


class TestTheEstimateCarriesIt:
    def test_every_bambu_profile_carries_its_own_sequences_limits(self) -> None:
        """Read through the same reader the wrap's sequence is, never copied."""
        checked = 0
        for pid in list_slicer_profiles():
            motion = start_sequence_motion(pid)
            settings = _ini(resolve_slicer_profile(pid))
            if motion is None:
                assert not set(_LIMIT_KEYS) & set(settings), pid
                continue
            checked += 1
            accel = f"{motion['accel']:g}"
            assert settings["machine_max_acceleration_extruding"] == f"{accel},{accel}", pid
            x = f"{motion['max_accel_x']:g}"
            assert settings["machine_max_acceleration_x"] == f"{x},{x}", pid
        assert checked >= 13, "every Bambu model ships a start sequence"

    def test_a_stated_role_acceleration_raises_the_working_one(self) -> None:
        """Stated accelerations are written into the print and the machine
        runs them, so the estimate must not clamp them down."""
        settings = _ini(resolve_slicer_profile("bambu_a1", overrides={"infill_acceleration": "8000"}))
        assert settings["machine_max_acceleration_extruding"] == "8000,8000"

    def test_the_multi_extruder_door(self) -> None:
        settings = _ini(resolve_multiextruder_profile("bambu_a1", 4))
        assert settings["machine_max_acceleration_extruding"] == "6000,6000"

    def test_a_callers_own_limit_is_theirs(self) -> None:
        settings = _ini(
            resolve_slicer_profile("bambu_a1", overrides={"machine_max_acceleration_x": "3000,3000"})
        )
        assert settings["machine_max_acceleration_x"] == "3000,3000"
        assert "machine_max_acceleration_extruding" not in settings

    def test_limits_written_into_the_gcode_are_never_added(self) -> None:
        """A profile that emits its limits would send a number from here to
        the machine."""
        settings = _ini(
            resolve_slicer_profile("bambu_a1", overrides={"machine_limits_usage": "emit_to_gcode"})
        )
        assert not set(_LIMIT_KEYS) & set(settings)


def _find_prusaslicer() -> str | None:
    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


_PRUSA = _find_prusaslicer()


def _plate(path: Path) -> str:
    """A 60 x 40 x 3 mm plate, as ASCII: PrusaSlicer sniffs a binary STL of
    round coordinates at the origin as ASCII and refuses it."""
    v = [(0, 0, 0), (60, 0, 0), (60, 40, 0), (0, 40, 0), (0, 0, 3), (60, 0, 3), (60, 40, 3), (0, 40, 3)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid plate"]
    for a, b, c in faces:
        lines.append(" facet normal 0 0 0\n  outer loop")
        lines.extend(f"   vertex {x} {y} {z}" for x, y, z in (v[a], v[b], v[c]))
        lines.append("  endloop\n endfacet")
    lines.append("endsolid plate")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return str(path)


def _slice(ini: str, model: str) -> str:
    out = os.path.join(tempfile.mkdtemp(), "o.gcode")
    run = subprocess.run(
        [_PRUSA, "--load", ini, "--export-gcode", "--output", out, model],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert os.path.isfile(out), (run.stderr or run.stdout)[-400:]
    return Path(out).read_text(encoding="utf-8", errors="replace")


def _seconds(gcode: str) -> int:
    m = re.search(r"^; estimated printing time \(normal mode\) = (.+)$", gcode, re.MULTILINE)
    assert m
    return sum(
        int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        for n, u in re.findall(r"(\d+)([dhms])", m.group(1))
    )


def _feedrates(gcode: str) -> set[str]:
    return set(re.findall(r"^G1 [^;\n]*F(\d+(?:\.\d+)?)", gcode, re.MULTILINE))


@pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed")
class TestOnlyTheEstimateMoves:
    def test_faster_estimate_same_gcode(self, tmp_path: Path) -> None:
        """A/B against the slicer's own defaults -- what every Bambu profile
        was timed with before 2026-09-30."""
        model = _plate(tmp_path / "plate.stl")
        own = resolve_slicer_profile("bambu_a1")
        defaults = resolve_slicer_profile(
            "bambu_a1",
            overrides={
                "machine_max_acceleration_extruding": "1500,1250",
                "machine_max_acceleration_travel": "1500,1250",
                "machine_max_acceleration_x": "9000,1000",
                "machine_max_acceleration_y": "9000,1000",
            },
        )
        fast, slow = _slice(own, model), _slice(defaults, model)
        assert _seconds(fast) < 0.95 * _seconds(slow), (_seconds(fast), _seconds(slow))
        assert _feedrates(fast) == _feedrates(slow), "a machine limit changed the G-code"
        assert not re.search(r"^(M201|M203|M204|M205)\b", fast, re.MULTILINE)
