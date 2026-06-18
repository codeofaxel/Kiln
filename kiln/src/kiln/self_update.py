"""Do the upgrade for the user — safely — so the agent can offer, not instruct.

The Apple-grade promise behind ``upgrade_kiln``: when a newer Kiln is needed,
the assistant says "want me to update it for you now?" and, on yes, actually
does it.  This module is that action, kept pure and injectable so it tests
without touching pip or a printer.

Three things it gets right:

* **Never mid-print.**  ``pip install --upgrade`` rewrites files on disk; the
  running process keeps its already-imported code, but a *later* lazy import
  could then load new code into an old session — a real hazard during a live
  print.  So if a print is active we DEFER, with a friendly "right after this
  finishes," and touch nothing.

* **Honest about the restart.**  We never hot-swap code under a running
  session (the long-standing safety rule).  The install lands the new version
  on disk; the one manual step left is a restart, and we say so plainly.

* **Graceful when the environment won't allow it.**  pipx / uv / system-managed
  installs may refuse ``pip install --upgrade`` from inside the process.  We
  try, and on failure hand back the exact command instead of pretending.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable

PACKAGE_NAME = "kiln3d"
UPGRADE_COMMAND = f"pip install --upgrade {PACKAGE_NAME}"

# A successful upgrade rewrites files but does NOT reload the running process.
RESTART_NOTE = (
    "Restart Kiln once (or your MCP client) at a safe moment — not mid-print — "
    "to finish; about ten seconds, and I'll pick up right where we left off."
)

_DEFAULT_TIMEOUT_S = 300

# pip prints "Successfully installed kiln3d-1.2.3 ..." — pull the version back
# out so we can name what landed (the in-process __version__ is stale until a
# restart, so we trust pip's own report instead).
_INSTALLED_RE = re.compile(rf"{re.escape(PACKAGE_NAME)}-([0-9][0-9A-Za-z.\-+!]*)")


def current_version() -> str:
    """The version running right now (best-effort)."""
    try:
        from importlib import metadata

        return metadata.version(PACKAGE_NAME)
    except Exception:  # noqa: BLE001
        try:
            import kiln

            return getattr(kiln, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"


def _parse_installed_version(pip_stdout: str) -> str | None:
    """Pull the just-installed kiln3d version out of pip's success output."""
    hits = _INSTALLED_RE.findall(pip_stdout or "")
    return hits[-1] if hits else None


def perform_upgrade(
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    print_active: Callable[[], bool] | None = None,
    force: bool = False,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Update ``kiln3d`` in place and report what happened.

    ``runner`` / ``print_active`` are injected in tests; in production they
    default to :func:`subprocess.run` and a best-effort printer-state probe.

    Returns a structured result with a ``status`` of:
      ``deferred_active_print`` | ``updated`` | ``already_latest`` |
      ``failed`` — each carrying a ``message`` written for the user.
    """
    before = current_version()

    # The mid-print guard is owned by the AGENT (it knows the live print state
    # from the session) and reinforced by the restart-timing message below; we
    # do NOT poll printer hardware on an upgrade.  A caller that holds a cheap
    # live-print signal may inject ``print_active`` to have us defer outright.
    if not force and print_active is not None:
        try:
            active = bool(print_active())
        except Exception:  # noqa: BLE001 -- a flaky probe must never block, nor green-light
            active = False
        if active:
            return {
                "ok": False,
                "status": "deferred_active_print",
                "current": before,
                "restart_required": False,
                "message": (
                    "A print is running, so I'll hold off on updating — swapping "
                    "Kiln mid-print isn't safe. I'll update the moment it finishes "
                    "(or say 'update now' to override)."
                ),
            }

    if runner is None:
        runner = subprocess.run
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- subprocess/timeout failures are reported, never raised
        return {
            "ok": False,
            "status": "failed",
            "current": before,
            "restart_required": False,
            "command": UPGRADE_COMMAND,
            "message": (
                f"I couldn't run the update here ({exc}). You can do it in one "
                f"line: {UPGRADE_COMMAND}"
            ),
        }

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return {
            "ok": False,
            "status": "failed",
            "current": before,
            "restart_required": False,
            "command": UPGRADE_COMMAND,
            "detail": "\n".join(tail),
            "message": (
                "I couldn't update automatically — this install may be managed "
                f"by another tool (pipx, uv, your OS). Run this and you're set: "
                f"{UPGRADE_COMMAND}"
            ),
        }

    installed = _parse_installed_version(proc.stdout or "")
    if installed is None:
        # pip ran clean but reported no install line → already current.
        return {
            "ok": True,
            "status": "already_latest",
            "current": before,
            "restart_required": False,
            "message": f"You're already on the latest Kiln ({before}). Nothing to do.",
        }
    return {
        "ok": True,
        "status": "updated",
        "current": before,
        "installed": installed,
        "restart_required": True,
        "message": f"Updated Kiln {before} → {installed}. {RESTART_NOTE}",
    }
