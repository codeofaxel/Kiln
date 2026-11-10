"""Do the upgrade for the user — safely — so the agent can offer, not instruct.

The promise behind ``upgrade_kiln``: when a newer Kiln is needed,
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

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "kiln3d"
UPGRADE_COMMAND = f"pip install --upgrade {PACKAGE_NAME}"

# A successful upgrade rewrites files but does NOT reload the running process.
RESTART_NOTE = (
    "Restart Kiln once (or your MCP client) at a safe moment — not mid-print — "
    "to finish; about ten seconds, and I'll pick up right where we left off."
)

_DEFAULT_TIMEOUT_S = 300

# Set by a program that installs Kiln and keeps it up to date itself (a plugin
# manager pinning one release, an OS package).  Its value is the command that
# updates this copy; while it is set, Kiln never replaces its own files.
MANAGED_UPDATE_ENV = "KILN_UPDATE_COMMAND"


def managed_update_command() -> str | None:
    """The command that updates this install, when another program manages it.

    ``None`` means Kiln updates itself as usual.  Every door that would update
    Kiln in place asks this first and, when it answers, names the command
    instead of running pip.
    """
    value = os.environ.get(MANAGED_UPDATE_ENV, "").strip()
    return value or None


def managed_message(command: str) -> str:
    return (
        "This copy of Kiln is kept up to date by the program that installed it, "
        f"so it doesn't update itself. Update it there: {command}"
    )


# Set by uv for the programs it runs; names the uv that started this Kiln.
UV_ENV = "UV"

# Installer output that means the computer could not reach the package index.
_OFFLINE_RE = re.compile(
    r"name resolution|name or service not known|nodename nor servname|getaddrinfo failed"
    r"|failed to establish a new connection|network is unreachable|no route to host"
    r"|connection (?:refused|reset|timed out)|dns error|failed to fetch|could not connect"
    r"|max retries exceeded|proxyerror|temporary failure",
    re.IGNORECASE,
)

_TAIL_LINES = 3


@dataclass(frozen=True)
class InstallRun:
    """What running an installer came to.

    ``argv`` is the command that decided the outcome (``None`` when there was
    none to run), ``tail`` its last lines, ``offline`` whether a failure reads
    as no connection.  ``stdout`` is the successful run's full output and
    ``error`` the words of a command that could not be started.
    """

    ok: bool
    argv: list[str] | None
    tail: str
    offline: bool
    stdout: str = ""
    error: str = ""


def installer_argvs(
    requirement: str,
    *,
    target: Path,
    find_spec: Callable[[str], object] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[list[str]]:
    """The commands that can put *requirement* into the folder *target*, best first.

    Only installers this computer has are listed: this interpreter's own pip,
    then uv (the one that started Kiln, else one on PATH).  Each writes into
    *target* alone and never into the environment Kiln runs from.  An empty
    list means there is nothing to run.
    """
    find_spec = find_spec or importlib.util.find_spec
    which = which or shutil.which
    argvs: list[list[str]] = []
    try:
        has_pip = find_spec("pip") is not None
    except Exception:  # noqa: BLE001 -- a broken finder means "no pip here"
        has_pip = False
    if has_pip:
        argvs.append(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps",
             "--target", str(target), requirement]
        )
    uv = os.environ.get(UV_ENV, "").strip() or which("uv")
    if uv:
        argvs.append(
            [uv, "pip", "install", "--no-deps", "--target", str(target),
             "--python", sys.executable, requirement]
        )
    return argvs


def run_installer(
    argvs: list[list[str]],
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> InstallRun:
    """Run *argvs* in order until one succeeds, and say what happened.

    Never raises: a command that cannot start, times out or exits non-zero is
    a failed run, and the next one is tried.  A failure that reads as no
    connection ends the attempt, since every installer needs the same network.
    """
    if runner is None:
        runner = subprocess.run
    last = InstallRun(ok=False, argv=None, tail="", offline=False)
    for argv in argvs:
        try:
            proc = runner(argv, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised
            last = InstallRun(ok=False, argv=argv, tail=str(exc), offline=False, error=str(exc))
            continue
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        if proc.returncode == 0:
            return InstallRun(ok=True, argv=argv, tail=_tail(stdout), offline=False, stdout=stdout)
        offline = bool(_OFFLINE_RE.search(f"{stdout}\n{stderr}"))
        last = InstallRun(ok=False, argv=argv, tail=_tail(stderr or stdout), offline=offline)
        if offline:
            break
    return last


def _tail(text: str) -> str:
    return "\n".join((text or "").strip().splitlines()[-_TAIL_LINES:])


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
      ``managed`` | ``deferred_active_print`` | ``updated`` |
      ``already_latest`` | ``failed`` — each carrying a ``message`` written
      for the user.  ``managed`` means another program keeps this install up
      to date (:func:`managed_update_command`); nothing is run.
    """
    before = current_version()

    managed = managed_update_command()
    if managed:
        return {
            "ok": False,
            "status": "managed",
            "current": before,
            "restart_required": False,
            "command": managed,
            "message": managed_message(managed),
        }

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

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME]
    run = run_installer([cmd], runner=runner, timeout=timeout)
    if run.error:
        return {
            "ok": False,
            "status": "failed",
            "current": before,
            "restart_required": False,
            "command": UPGRADE_COMMAND,
            "message": (
                f"I couldn't run the update here ({run.error}). You can do it in one "
                f"line: {UPGRADE_COMMAND}"
            ),
        }

    if not run.ok:
        return {
            "ok": False,
            "status": "failed",
            "current": before,
            "restart_required": False,
            "command": UPGRADE_COMMAND,
            "detail": run.tail,
            "message": (
                "I couldn't update automatically — this install may be managed "
                f"by another tool (pipx, uv, your OS). Run this and you're set: "
                f"{UPGRADE_COMMAND}"
            ),
        }

    installed = _parse_installed_version(run.stdout)
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
