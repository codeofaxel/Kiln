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
from typing import Any

#: The child's entry: drop the bootstrap's own argv slot, then run the script
#: as ``__main__`` without its folder on the path.
_BOOTSTRAP = "import runpy, sys; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"


def run_in_child(
    script: str,
    request: dict[str, Any],
    *,
    timeout_s: float,
    what: str,
    error: type[Exception],
) -> dict[str, Any]:
    """Run *script* on *request* and return its answer.

    Raises *error* when the child is stopped at *timeout_s*, exits without an
    answer, or refuses; the message names *what* ran ("the CAD kernel") or
    carries the child's own sentence.
    """
    workdir = tempfile.mkdtemp(prefix="kiln_child_")
    request_path = os.path.join(workdir, "request.json")
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(request, fh)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    try:
        run = subprocess.run(
            [sys.executable, "-c", _BOOTSTRAP, os.path.abspath(script), request_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=workdir,
            env=env,
        )
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
