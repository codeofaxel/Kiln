"""A watched child that is stopped takes the processes it started with it.

2026-10-16: on the shared server a hole check that had outlived its caller
kept running, reparented to init, for ten minutes -- the kill reached the
child and not what the child had started.  The child now runs in a session
of its own and the stop is sent to the whole session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from kiln import child_interpreter


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and "Z" not in out.stdout


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups")
def test_a_timed_out_child_s_own_child_is_stopped_too(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "slow.py"
    script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    request = tmp_path / "request.json"
    request.write_text("{}")
    command = [sys.executable, "-c", child_interpreter._BOOTSTRAP, str(script), str(request)]
    with pytest.raises(subprocess.TimeoutExpired):
        child_interpreter._run_watched(
            command, str(tmp_path), dict(os.environ), timeout_s=1.5, max_memory_mb=10_000, what="slow"
        )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the grandchild outlived the stop"


def test_the_hole_check_gives_up_inside_a_minute():
    """A check that cannot finish in a minute is stopped and said, never
    waited on past what a chat host waits for the whole make."""
    from kiln import hole_check

    assert hole_check._CHILD_TIMEOUT_S <= 60


def test_a_small_binary_stl_is_checked_in_process_even_with_an_allowance(tmp_path, monkeypatch):
    """The child is for the model that could take a shared server down; a
    small part pays the interpreter start for nothing."""
    import struct

    from kiln import hole_check

    small = tmp_path / "small.stl"
    small.write_bytes(b"\0" * 80 + struct.pack("<I", 1000) + b"\0" * 50 * 1000)
    big = tmp_path / "big.stl"
    big.write_bytes(b"\0" * 80 + struct.pack("<I", hole_check.RUN_HERE_BELOW_TRIANGLES + 1))
    ascii_stl = tmp_path / "ascii.stl"
    ascii_stl.write_text("solid x\nendsolid x\n" + " " * 80)
    forged = tmp_path / "forged.stl"  # claims 1000 triangles, carries far more
    forged.write_bytes(b"\0" * 80 + struct.pack("<I", 1000) + b"\0" * 50 * 5000)
    assert hole_check._small_enough_to_run_here(str(small), 256) is True
    assert hole_check._small_enough_to_run_here(str(big), 256) is False
    assert hole_check._small_enough_to_run_here(str(ascii_stl), 256) is False
    assert hole_check._small_enough_to_run_here(str(forged), 256) is False
    # The allowance still rules: 1000 triangles is ~2 MB, over half of 3.
    assert hole_check._small_enough_to_run_here(str(small), 3) is False

    monkeypatch.setenv(hole_check.ALLOWANCE_ENV, "256")
    seen: list[str] = []
    monkeypatch.setattr(
        "kiln.generation.validation.detect_holes",
        lambda path, diagnostics=None: seen.append("inline") or [],
    )
    monkeypatch.setattr(
        "kiln.child_interpreter.run_in_child",
        lambda *a, **k: seen.append("child") or {"holes": [], "diagnostics": {}},
    )
    hole_check.find_holes(str(small))
    hole_check.find_holes(str(big))
    assert seen == ["inline", "child"]
