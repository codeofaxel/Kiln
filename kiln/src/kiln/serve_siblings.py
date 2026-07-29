"""Detect accumulated ``kiln serve`` processes on this machine.

Every MCP client session (Claude Code, Claude Desktop, Codex, …)
spawns its own ``kiln serve``.  That is correct while the session is
alive — but client hosts do not reliably kill the server when a
session ends, and the orphan watchdog (:mod:`kiln.parent_watchdog`)
only fires when the parent process *dies*.  The common leak is a
parent that stays alive as an idle husk: the session is gone, the
helper process isn't, and the server it spawned idles forever.
Field report: 18 accumulated servers (~0.9 GB RSS) against a single
active session.

This module is the ONE shared detector for that condition.  Every
surface that reports health wires through here — ``health_check``,
``kiln_health``, ``get_started``, ``kiln doctor``/``verify``, and
``kiln serve`` startup — so the count and the advice never drift
between doors.

Detection is a process-table scan (``ps`` on POSIX).  From the
outside we cannot tell a live session's server from a husk, so the
report counts everything and leaves the trimming decision to the
user: if the count exceeds the number of sessions they actually have
open, the rest are clutter.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

# Warn when the total number of ``kiln serve`` processes (including
# the one doing the asking) reaches this many.  A user juggling
# Claude Code, Claude Desktop, and another MCP host legitimately runs
# a few; double digits means husks are piling up.  Override with
# ``KILN_SERVE_SIBLING_WARN_THRESHOLD``.
_DEFAULT_WARN_THRESHOLD = 5

def _warn_threshold() -> int:
    raw = os.environ.get("KILN_SERVE_SIBLING_WARN_THRESHOLD", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_WARN_THRESHOLD
    return max(2, value)


def _list_serve_processes() -> list[dict] | None:
    """Return one entry per ``kiln serve`` process, or ``None`` when
    the process table cannot be read (non-POSIX platform, ``ps``
    missing or failing).  ``None`` means "unknown", never "zero".
    """
    if not sys.platform.startswith(("linux", "darwin")) and os.name != "posix":
        return None
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,etime=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except Exception as exc:
        logger.debug("serve-siblings: ps scan failed: %s", exc)
        return None

    procs: list[dict] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, etime, args = parts
        # The server's argv is ``<path>/kiln serve …`` (or the kiln3d
        # alias), ``<python> <path>/kiln serve …``, or ``<python> -m
        # kiln serve …`` — every launch shape pyproject's
        # [project.scripts] and kiln/__main__.py support.  Require the
        # ``kiln`` token to be the executable itself, the script an
        # interpreter is running, or the -m module — a mention deeper
        # in the args is not a server.  This also skips wrappers that
        # repeat the server's command line in their own args (macOS
        # spawns ``…/Helpers/disclaimer <real command>`` around each
        # server; counting those would double-count every one).
        tokens = args.split()
        if not any(
            os.path.basename(tok) in ("kiln", "kiln3d")
            and idx + 1 < len(tokens)
            and tokens[idx + 1] == "serve"
            and (
                idx == 0
                or tokens[idx - 1] == "-m"
                or os.path.basename(tokens[idx - 1]).lower().startswith("python")
            )
            for idx, tok in enumerate(tokens)
        ):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        procs.append({"pid": pid, "age": etime})
    return procs


def check_serve_siblings() -> dict:
    """Report every ``kiln serve`` process on this machine.

    Returns::

        {
            "count": int | None,      # total servers, None = scan unavailable
            "pids": [int, ...],       # oldest first
            "oldest_age": str | None, # ps etime of the longest-lived one
            "warning": str | None,    # set when count >= threshold
        }

    ``warning`` is user-facing text.  Agents seeing it set should
    relay it verbatim — the user, not the agent, decides what to trim.
    """
    procs = _list_serve_processes()
    if procs is None:
        return {"count": None, "pids": [], "oldest_age": None, "warning": None}

    # ps etime sorts correctly only within equal widths ([[dd-]hh:]mm:ss),
    # so order by padded length first, then lexically — longest-lived first.
    procs.sort(key=lambda p: (len(p["age"]), p["age"]), reverse=True)
    result: dict = {
        "count": len(procs),
        "pids": [p["pid"] for p in procs],
        "oldest_age": procs[0]["age"] if procs else None,
        "warning": None,
    }
    threshold = _warn_threshold()
    if len(procs) >= threshold:
        result["warning"] = (
            f"{len(procs)} 'kiln serve' processes are running on this machine "
            f"(oldest: {result['oldest_age']}). Each open MCP client session "
            f"keeps one alive; if you have fewer sessions than that open, the "
            f"rest are leftovers from closed sessions and can be trimmed. "
            f"Close unused Claude/MCP-client windows, or kill the oldest PIDs: "
            f"{result['pids'][:10]}"
        )
    return result


def log_sibling_warning_at_startup() -> None:
    """Best-effort startup door: warn on stderr when servers have piled up.

    Called from ``kiln serve`` before ``mcp.run()``.  stderr reaches
    the MCP host's log, so a user (or an agent reading server logs)
    sees the pile-up at the moment one more server joins it.  Never
    raises — startup must not fail on a diagnostic.
    """
    try:
        report = check_serve_siblings()
        if report["warning"]:
            logger.warning("serve-siblings: %s", report["warning"])
            print(f"[kiln] {report['warning']}", file=sys.stderr)
    except Exception as exc:
        logger.debug("serve-siblings: startup check failed: %s", exc)
