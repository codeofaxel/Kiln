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
