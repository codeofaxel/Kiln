"""Run one of Kiln's own files in a fresh interpreter, importing what the parent imports.

Some work runs in a child interpreter: a C++ kernel call cannot be timed out
from Python, and a pathological part must not take the server's memory with
it.  The child must see the same libraries the parent does, and nothing that
merely sits beside the script:

* **The parent's import path, handed over.**  A child that works its own out
  from ``HOME`` loses the user's site-packages whenever ``HOME`` has moved --
  Kiln's test sandbox moves it -- and then finds a different numpy, or none
  (2026-10-01: "No module named 'numpy'" from the mesh offset under pytest).
* **Not the script's own folder.**  Python puts a script's folder first on its
  path, and these scripts live inside the ``kiln`` package, where
  ``kiln/queue.py`` stood in for the standard library's ``queue`` for
  everything the child imported -- trimesh pulls it in, and a thread pool there
  would have failed on a print queue with no ``SimpleQueue``.  The script is
  run with :func:`runpy.run_path` from an empty folder instead.

The script reads the JSON request at ``sys.argv[1]`` and prints its answer as
one JSON line; an answer carrying ``refused`` is the child declining, in a
sentence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

#: The child's entry: drop the bootstrap's own argv slot, then run the script
#: as ``__main__`` without its folder on the path.
_BOOTSTRAP = "import runpy, sys; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"


class ChildOverMemory(Exception):
    """The child was stopped for holding more memory than it was allowed."""

    def __init__(self, what: str, held_mb: float, allowed_mb: float) -> None:
        super().__init__(f"{what} held {held_mb:.0f} MB, past the {allowed_mb:.0f} MB it is allowed, and was stopped")
        self.held_mb = held_mb
        self.allowed_mb = allowed_mb


#: How often a child with a memory allowance is looked at.  The hole check
#: gains about 100 MB a second on a large mesh, so it overshoots by tens of MB.
_MEMORY_LOOK_S = 0.2


def resident_mb(pid: int) -> float | None:
    """The memory process *pid* holds right now, in MB; ``None`` when it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
        shown = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(shown.stdout.strip()) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_in_child(
    script: str,
    request: dict[str, Any],
    *,
    timeout_s: float,
    what: str,
    error: type[Exception],
    max_memory_mb: float | None = None,
) -> dict[str, Any]:
    """Run *script* on *request* and return its answer.

    Raises *error* when the child is stopped at *timeout_s*, exits without an
    answer, or refuses; the message names *what* ran ("the CAD kernel") or
    carries the child's own sentence.  With *max_memory_mb*, a child seen
    holding more than that is stopped and :class:`ChildOverMemory` raised.
    """
    workdir = tempfile.mkdtemp(prefix="kiln_child_")
    request_path = os.path.join(workdir, "request.json")
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(request, fh)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    command = [sys.executable, "-c", _BOOTSTRAP, os.path.abspath(script), request_path]
    try:
        if max_memory_mb is None:
            run = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, cwd=workdir, env=env)
        else:
            run = _run_watched(command, workdir, env, timeout_s=timeout_s, max_memory_mb=max_memory_mb, what=what)
    except subprocess.TimeoutExpired as exc:
        raise error(f"{what} took longer than {timeout_s:g} s and was stopped") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    lines = [ln for ln in run.stdout.splitlines() if ln.startswith("{")]
    if run.returncode != 0 or not lines:
        reason = (run.stderr or run.stdout).strip().splitlines()
        raise error(reason[-1] if reason else f"{what} exited with {run.returncode}")
    answer = json.loads(lines[-1])
    if "refused" in answer:
        raise error(answer["refused"])
    return answer


def _run_watched(
    command: list[str],
    workdir: str,
    env: dict[str, str],
    *,
    timeout_s: float,
    max_memory_mb: float,
    what: str,
) -> subprocess.CompletedProcess[str]:
    """Run *command*, looking at its memory as it goes."""
    started = time.monotonic()
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=workdir, env=env,
    ) as child:
        while True:
            try:
                out, err = child.communicate(timeout=_MEMORY_LOOK_S)
                return subprocess.CompletedProcess(command, child.returncode, out, err)
            except subprocess.TimeoutExpired:
                held = resident_mb(child.pid)
                over = held is not None and held > max_memory_mb
                if not over and time.monotonic() - started <= timeout_s:
                    continue
                child.kill()
                child.communicate()
                if over:
                    raise ChildOverMemory(what, held, max_memory_mb) from None
                raise
