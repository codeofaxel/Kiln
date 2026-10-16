"""The hole check, run where a model too big for it cannot take the server along.

:func:`kiln.generation.validation.detect_holes` holds every triangle of a
mesh as Python objects: about 2 MB per thousand triangles.  On a machine
that serves many people from 2 GB, one large model can use all of it.

So a machine may give the check an allowance -- ``KILN_HOLE_CHECK_MAX_MB`` --
and the check then runs in a process of its own, stopped if it goes past.
With no allowance set, which is every ordinary install, it runs as it always
has, with no limit: a person's own computer is theirs to use.
"""

from __future__ import annotations

import os
import sys
from typing import Any

#: The allowance, in MB.  Unset or not a positive number: no limit.
ALLOWANCE_ENV = "KILN_HOLE_CHECK_MAX_MB"

#: The check reads a 1.1M-triangle mesh in about 30 s on a laptop; past
#: this it is stuck -- or starved: on the shared server a check that took
#: 1 s elsewhere ran past six minutes under load (2026-10-16), and the
#: person waiting had been told "no answer" by their chat at four.  A check
#: that cannot finish inside a minute is stopped and SAID, and the part's
#: printability is judged without it.
_CHILD_TIMEOUT_S = 60


class HoleCheckStopped(Exception):
    """The check did not finish, so nothing is known about the model's holes."""

    def __init__(self, reason: str, *, held_mb: float | None = None, allowed_mb: float | None = None) -> None:
        super().__init__(reason)
        self.held_mb = held_mb
        self.allowed_mb = allowed_mb


def allowance_mb() -> float | None:
    """What this machine allows the hole check, or ``None`` for no limit."""
    try:
        value = float(os.environ.get(ALLOWANCE_ENV, ""))
    except ValueError:
        return None
    return value if value > 0 else None


#: Below this many triangles the check runs in this process even where an
#: allowance is set.  The child is for the model that could take a shared
#: server down (about 2 MB a thousand triangles, so 150k is ~300 MB at
#: most); under it the child costs more than it saves -- a fresh
#: interpreter importing Kiln is tens of seconds of CPU on a shared core,
#: at low priority behind the server itself (2026-10-16: a 1 s check of an
#: 88k-triangle part spent minutes in its child).
RUN_HERE_BELOW_TRIANGLES = 150_000


#: What the check holds per triangle, in MB, with room: the module docstring's
#: measured 2 MB a thousand, doubled.
_MB_PER_TRIANGLE = 0.004


def _small_enough_to_run_here(file_path: str, allowed_mb: float) -> bool:
    """Whether *file_path* is a binary STL small enough to check in-process
    under *allowed_mb*.

    Only a binary STL says its triangle count up front (bytes 80-84), and the
    count is believed only when the file is exactly the size that count
    implies -- a header is cheap to forge, a file is not.  The estimated peak
    must fit in half the allowance, so a small allowance is never bypassed by
    the fixed ceiling.  Anything else is answered ``False`` and gets the
    child, as before.
    """
    try:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as fh:
            head = fh.read(84)
        if len(head) < 84 or head[:5].lower() == b"solid":
            return False
        count = int.from_bytes(head[80:84], "little")
        if not 0 < count <= RUN_HERE_BELOW_TRIANGLES:
            return False
        if size != 84 + 50 * count:
            return False
        return count * _MB_PER_TRIANGLE <= allowed_mb / 2
    except OSError:
        return False


def find_holes(file_path: str, *, diagnostics: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """The holes in *file_path*, as :func:`detect_holes` reports them.

    Raises :class:`HoleCheckStopped` when the machine's allowance stopped the
    check; ``ValueError`` / ``OSError`` as ``detect_holes`` does.
    """
    allowed = allowance_mb()
    if allowed is None or _small_enough_to_run_here(file_path, allowed):
        from kiln.generation.validation import detect_holes

        return detect_holes(file_path, diagnostics=diagnostics)

    from kiln.child_interpreter import ChildOverMemory, run_in_child

    try:
        answer = run_in_child(
            __file__,
            {"path": os.path.abspath(file_path)},
            timeout_s=_CHILD_TIMEOUT_S,
            what="the hole check",
            error=HoleCheckStopped,
            max_memory_mb=allowed,
        )
    except ChildOverMemory as exc:
        raise HoleCheckStopped(str(exc), held_mb=exc.held_mb, allowed_mb=exc.allowed_mb) from exc
    if "error" in answer:
        kinds = {"ValueError": ValueError, "FileNotFoundError": FileNotFoundError}
        raise kinds.get(answer["error"], OSError)(answer["message"])
    if diagnostics is not None:
        for key, count in answer["diagnostics"].items():
            diagnostics[key] = diagnostics.get(key, 0) + count
    return answer["holes"]


def _child(request: dict[str, Any]) -> dict[str, Any]:
    from kiln.generation.validation import detect_holes

    diagnostics: dict[str, int] = {}
    try:
        holes = detect_holes(request["path"], diagnostics=diagnostics)
    except (ValueError, OSError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"holes": holes, "diagnostics": diagnostics}


if __name__ == "__main__":
    import json

    with open(sys.argv[1], encoding="utf-8") as _fh:
        print(json.dumps(_child(json.load(_fh))))
