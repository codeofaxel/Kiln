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


def _humanize_etime(etime: str) -> str:
    """Turn ps etime ([[dd-]hh:]mm:ss) into plain English ("about 2 days").

    The warning is read by people who don't know what an etime — or a
    PID — is.  Falls back to the raw string on anything unparseable.
    """
    try:
        days = 0
        clock = etime
        if "-" in clock:
            day_part, clock = clock.split("-", 1)
            days = int(day_part)
        fields = [int(f) for f in clock.split(":")]
        hours = fields[0] if len(fields) == 3 else 0
        minutes = fields[-2]
        total_hours = days * 24 + hours
        if total_hours >= 36:
            return f"about {round(total_hours / 24)} days"
        if total_hours >= 1:
            return f"about {total_hours} hour{'s' if total_hours != 1 else ''}"
        if minutes >= 1:
            return f"about {minutes} minute{'s' if minutes != 1 else ''}"
        return "under a minute"
    except Exception:
        return etime


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
        # Plain-English, no chore assigned: the reader may not know
        # what a PID is, and "go restart everything now" is friction.
        # The honest framing is self-healing — once the client apps
        # close (which happens in normal life anyway), the leftovers
        # lose their parent and the orphan watchdog
        # (kiln.parent_watchdog) shuts them down within a minute.  No
        # urgency is implied because none exists: leftovers cost
        # memory, not correctness.  PIDs trail as a power-user
        # shortcut only.
        result["warning"] = (
            f"{len(procs)} background copies of Kiln's server are running "
            f"(oldest has been up {_humanize_etime(result['oldest_age'])}). "
            f"Each open agent session normally keeps just one — the rest "
            f"are leftovers from closed sessions, quietly using memory. "
            f"No action needed: they clean themselves up within a minute "
            f"of their app fully closing, so they'll clear next time you "
            f"quit your Claude/MCP apps. (Power users who want the memory "
            f"back sooner can kill the oldest process IDs: "
            f"{result['pids'][:10]}.)"
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
