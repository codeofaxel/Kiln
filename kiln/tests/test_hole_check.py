"""The hole check under a machine's memory allowance (``kiln.hole_check``).

With no allowance the check runs as it always has.  With one it runs in a
process of its own; past the allowance it is stopped, and the analysis says
the holes were not checked -- which is not the same as saying there are none.
"""

from __future__ import annotations

import math
import os
import struct
import sys
import types
from pathlib import Path

import pytest

from kiln import child_interpreter, hole_check
from kiln.printability import analyze_printability


def _bore(cx: float, cy: float, radius: float, depth: float, segments: int = 24) -> list:
    tris = []
    for i in range(segments):
        a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
        bl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), 0.0)
        br = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), 0.0)
        tl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), depth)
        tr = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), depth)
        tris += [(bl, tr, br), (bl, tl, tr)]
    return tris


def _write(path: Path, tris: list) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
    return str(path)


@pytest.fixture
def two_bores(tmp_path: Path) -> str:
    """A 3 mm bore, and a 0.5 mm one the detector turns away as too small to print."""
    return _write(tmp_path / "bores.stl", _bore(5, 5, 1.5, 6) + _bore(20, 20, 0.25, 6))


def test_with_no_allowance_the_check_runs_in_this_process(two_bores, monkeypatch):
    monkeypatch.delenv(hole_check.ALLOWANCE_ENV, raising=False)
    monkeypatch.setattr(child_interpreter, "run_in_child", lambda *a, **k: pytest.fail("no child without an allowance"))
    said: dict[str, int] = {}
    holes = hole_check.find_holes(two_bores, diagnostics=said)
    assert [round(h["diameter_mm"], 1) for h in holes] == [3.0]
    assert said.get("sub_floor_clusters") == 1


def test_under_an_allowance_a_process_of_its_own_gives_the_same_answer(two_bores, monkeypatch):
    monkeypatch.delenv(hole_check.ALLOWANCE_ENV, raising=False)
    here_said: dict[str, int] = {}
    here = hole_check.find_holes(two_bores, diagnostics=here_said)

    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, "4000")
    from kiln.generation import validation

    monkeypatch.setattr(validation, "detect_holes", lambda *a, **k: pytest.fail("ran in this process"))
    apart_said: dict[str, int] = {}
    apart = hole_check.find_holes(two_bores, diagnostics=apart_said)
    assert apart == here
    assert apart_said == here_said


def test_a_check_past_its_allowance_is_stopped(two_bores, monkeypatch):
    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, "5")  # less than an idle interpreter holds
    monkeypatch.setattr(child_interpreter, "_MEMORY_LOOK_S", 0.01)
    with pytest.raises(hole_check.HoleCheckStopped) as stopped:
        hole_check.find_holes(two_bores)
    assert stopped.value.allowed_mb == 5 and stopped.value.held_mb > 5


def test_a_file_the_check_cannot_read_fails_the_same_way_in_either_place(tmp_path, monkeypatch):
    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, "4000")
    with pytest.raises(FileNotFoundError):
        hole_check.find_holes(str(tmp_path / "missing.stl"))
    folder = tmp_path / "folder.stl"
    folder.mkdir()
    with pytest.raises(OSError):
        hole_check.find_holes(str(folder))


@pytest.mark.parametrize("value", ["", "0", "-5", "lots"])
def test_an_allowance_that_is_not_a_size_is_no_allowance(value, monkeypatch):
    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, value)
    assert hole_check.allowance_mb() is None


def test_this_process_can_be_weighed():
    held = child_interpreter.resident_mb(os.getpid())
    assert held is not None and 5 < held < 20_000


def test_an_analysis_whose_hole_check_was_stopped_says_so_and_tells_the_operator(two_bores, monkeypatch):
    """Holes not checked is said in words, the rest of the report stands, and the machine's operator hears."""
    told: list[tuple[str, dict]] = []
    bridge = types.ModuleType("kiln_pro.bridge")


    class _OnlyAnAlert:
        """A paid package that offers the operator's alert and nothing else."""

        def is_available(self, *_a, **_k) -> bool:
            return False

        def ops_alert(self, kind: str, facts: dict) -> None:
            told.append((kind, facts))

        def __getattr__(self, _name: str) -> None:
            return None

    bridge.pro_features = _OnlyAnAlert()
    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)

    monkeypatch.delenv(hole_check.ALLOWANCE_ENV, raising=False)
    sound = analyze_printability(two_bores)
    assert len(sound.holes) == 1
    assert not [r for r in sound.recommendations if "did not check this model's holes" in r]
    assert told == []

    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, "5")
    monkeypatch.setattr(child_interpreter, "_MEMORY_LOOK_S", 0.01)
    stopped = analyze_printability(two_bores)
    assert stopped.holes == []
    said = [r for r in stopped.recommendations if "did not check this model's holes" in r]
    assert len(said) == 1 and "96 triangles" in said[0] and "no limit" in said[0]
    assert [kind for kind, _ in told] == ["hole_check_stopped"]
    assert told[0][1]["triangles"] == 96 and told[0][1]["allowed_mb"] == 5
