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

This module is the ONE shared detector AND janitor for that
condition.  Every surface that reports health wires through here —
``health_check``, ``kiln_health``, ``get_started``, ``kiln doctor``/
``verify``, and ``kiln serve`` startup — so the count and the advice
never drift between doors.  ``plan_trim`` names the leftovers and
``perform_trim`` shuts them down, behind the ``trim_serve_processes``
MCP tool and the ``kiln trim`` CLI command.

Detection is a process-table scan (``ps`` on POSIX), restricted to
this user's own processes.  Nothing here runs in the background: no
threads, no per-request bookkeeping, no state files.  A scan happens
only when a health surface asks or the user runs a trim.

WHY TRIMMING IS SAFE, AND WHERE IT ISN'T
----------------------------------------
Killing a ``kiln serve`` process never stops a physical print — the
printer does not care that a laptop process exited.  What a kill can
end is *monitoring*: a watcher loop that would have reported progress
or raised an alert.  So the one genuinely harmful case is trimming a
server while a print is running, because the user goes on believing
Kiln is watching when it is not.  Everything else a killed server was
doing is recoverable by reconnecting or re-running the command.

Hence the guard: :func:`printing_now` asks every registered printer
whether it is mid-job, and a trim refuses while any of them is unless
explicitly forced.  That single machine-wide question replaces any
need for per-server liveness bookkeeping — if nothing is printing,
no monitoring can be lost, so trimming costs at most a reconnect.

Which servers are leftovers is then a ranking problem, answered
without any always-on machinery: process start time, plus (far
better) the user's own count of how many agent sessions they truly
have open.  The user is the one source of truth for that, so
``open_sessions`` keeps the N most recently started servers and
proposes the rest.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)

# Warn when the total number of ``kiln serve`` processes (including
# the one doing the asking) reaches this many.  A user juggling
# Claude Code, Claude Desktop, and another MCP host legitimately runs
# a few; double digits means husks are piling up.  Override with
# ``KILN_SERVE_SIBLING_WARN_THRESHOLD``.
_DEFAULT_WARN_THRESHOLD = 5

# Without the user's own session count, a trim only proposes servers
# older than this.  Six hours keeps a session parked over lunch safe
# while still catching overnight leftovers.
_DEFAULT_IDLE_HOURS = 6.0


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
            ["ps", "-axo", "pid=,uid=,etime=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except Exception as exc:
        logger.debug("serve-siblings: ps scan failed: %s", exc)
        return None

    my_uid = os.getuid()
    procs: list[dict] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_str, uid_str, etime, args = parts
        # Only this user's servers: on a shared machine, counting —
        # let alone signalling — someone else's processes is wrong,
        # and SIGTERM would fail with EPERM anyway.
        try:
            if int(uid_str) != my_uid:
                continue
        except ValueError:
            continue
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
        # Plain English, and never a PID the reader has to handle: the
        # offer is "Kiln cleans this up", the fallback is the one that
        # needs no tools at all (leftovers die with their app), and
        # neither implies urgency, because none exists — leftovers
        # cost memory, not correctness.
        result["warning"] = (
            f"{len(procs)} background copies of Kiln's server are running "
            f"(oldest has been up {_humanize_etime(result['oldest_age'])}). "
            f"Each open agent session normally keeps just one — the rest "
            f"are leftovers from closed sessions, quietly using memory. "
            f"Nothing is broken and no print is at risk. Kiln can close "
            f"the leftovers for you whenever you like — just say so, and "
            f"it will check that nothing is printing first (tool: "
            f"trim_serve_processes; terminal: `kiln trim`). They also "
            f"clear on their own next time you fully quit your Claude/MCP "
            f"apps."
        )
    return result


def _etime_seconds(etime: str) -> float | None:
    """Parse ps etime ([[dd-]hh:]mm:ss) to seconds; None if unparseable."""
    try:
        days = 0
        clock = etime
        if "-" in clock:
            day_part, clock = clock.split("-", 1)
            days = int(day_part)
        fields = [int(f) for f in clock.split(":")]
        hours = fields[0] if len(fields) == 3 else 0
        minutes, seconds = fields[-2], fields[-1]
        return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)
    except Exception:
        return None


# Printer states that mean "a job is in flight, monitoring matters".
_ACTIVE_PRINT_STATES = frozenset({"printing", "paused", "cancelling", "busy"})


def printing_now() -> dict:
    """Ask every registered printer whether a job is in flight.

    This is the whole safety story for trimming (see module docstring):
    a kill can only cost the user *monitoring*, and monitoring only
    matters while something is printing.  Returns::

        {
            "active": [ "name (printing)", ... ],   # jobs in flight
            "unknown": [ "name: <error>", ... ],    # could not ask
        }

    Never raises and never blocks for long — each adapter is asked
    once, failures are recorded as ``unknown`` rather than propagated,
    and an install with no printers configured trivially returns empty
    lists (nothing to lose, so nothing to guard).
    """
    active: list[str] = []
    unknown: list[str] = []
    try:
        import kiln.server as _srv

        adapters = _srv._get_registry().list_all()
    except Exception as exc:
        logger.debug("serve-siblings: registry unavailable: %s", exc)
        return {"active": [], "unknown": [f"registry unavailable: {exc}"]}

    seen: set[int] = set()
    for name, adapter in adapters.items():
        # config.yaml registers an alias per printer ("default"); ask
        # each physical adapter once.
        if id(adapter) in seen:
            continue
        seen.add(id(adapter))
        try:
            state = adapter.get_state().state
            value = getattr(state, "value", str(state))
            if value in _ACTIVE_PRINT_STATES:
                active.append(f"{name} ({value})")
        except Exception as exc:
            unknown.append(f"{name}: {exc}")
    return {"active": active, "unknown": unknown}


def plan_trim(open_sessions: int | None = None) -> dict:
    """Decide which sibling servers look like leftovers — without killing.

    This process is NEVER a candidate.  Beyond that, ranking is by
    process start time (most recently started first), which stands in
    for "most likely to back a session the user still has open":

    * ``open_sessions=K`` — the user told us how many agent sessions
      they truly have open, and they are the one source of truth for
      it.  Keep the K most recently started servers (this process
      counts toward K when it is itself one of them) and propose the
      rest.
    * ``open_sessions=None`` — no count given, so fall back to the
      conservative default: propose only servers older than
      ``_DEFAULT_IDLE_HOURS``.

    Returns::

        {
            "scanned": int | None,   # None = process table unreadable
            "candidates": [{"pid", "age", "age_human", "reason"}, ...],
            "kept": [{"pid", "age", "reason"}, ...],
        }
    """
    procs = _list_serve_processes()
    if procs is None:
        return {"scanned": None, "candidates": [], "kept": []}

    self_pid = os.getpid()
    candidates: list[dict] = []
    kept: list[dict] = []

    self_is_a_server = any(p["pid"] == self_pid for p in procs)
    others = [p for p in procs if p["pid"] != self_pid]
    if self_is_a_server:
        kept.append(
            {
                "pid": self_pid,
                "age": next(p["age"] for p in procs if p["pid"] == self_pid),
                "reason": "this session's own server",
            }
        )

    # Youngest first — a server that started recently most plausibly
    # backs a session the user still has open.
    others.sort(key=lambda p: _etime_seconds(p["age"]) or 0.0)

    if open_sessions is not None:
        sessions = max(0, int(open_sessions))
        # Inside a server (the MCP tool path) this process already
        # consumed one of the user's session slots; from a plain
        # terminal (``kiln trim``) the caller is not a server, so all
        # K slots still have to be filled from the scanned pool.
        # Getting this backwards would let ``--open-sessions 1`` kill
        # the server backing the user's only session.
        keep_slots = max(0, sessions - 1) if self_is_a_server else sessions
        plural = "session" if sessions == 1 else "sessions"
        for rank, proc in enumerate(others):
            entry = {"pid": proc["pid"], "age": proc["age"]}
            if rank < keep_slots:
                kept.append(
                    {
                        **entry,
                        "reason": (
                            f"one of the {sessions} {plural} you have open "
                            f"(started {_humanize_etime(proc['age'])} ago)"
                        ),
                    }
                )
            else:
                candidates.append(
                    {
                        **entry,
                        "age_human": _humanize_etime(proc["age"]),
                        "reason": (
                            f"beyond the {sessions} {plural} you have open "
                            f"(running {_humanize_etime(proc['age'])})"
                        ),
                    }
                )
    else:
        idle_limit_s = _DEFAULT_IDLE_HOURS * 3600
        for proc in others:
            entry = {"pid": proc["pid"], "age": proc["age"]}
            age_s = _etime_seconds(proc["age"])
            if age_s is not None and age_s > idle_limit_s:
                candidates.append(
                    {
                        **entry,
                        "age_human": _humanize_etime(proc["age"]),
                        "reason": f"running {_humanize_etime(proc['age'])}",
                    }
                )
            else:
                kept.append({**entry, "reason": "started recently"})

    # Oldest first, mirroring check_serve_siblings ordering.
    candidates.sort(key=lambda c: (len(c["age"]), c["age"]), reverse=True)
    return {"scanned": len(procs), "candidates": candidates, "kept": kept}


def perform_trim(open_sessions: int | None = None, force: bool = False) -> dict:
    """Shut down the leftover servers ``plan_trim`` identified.

    Refuses while any printer has a job in flight unless *force* is
    set — that is the one case where a kill costs the user something
    they cannot trivially recover (see module docstring).  Re-plans
    against a fresh process scan immediately before signalling, so a
    recycled PID can never be hit: every target is matcher-verified as
    a ``kiln serve`` at kill time.  SIGTERM only; never this process.
    """
    printing = printing_now()
    if printing["active"] and not force:
        return {
            "blocked": True,
            "printing": printing,
            "scanned": None,
            "trimmed": [],
            "failed": [],
            "kept": [],
        }

    plan = plan_trim(open_sessions=open_sessions)
    trimmed: list[dict] = []
    failed: list[dict] = []
    for cand in plan["candidates"]:
        try:
            os.kill(cand["pid"], signal.SIGTERM)
            trimmed.append(cand)
        except ProcessLookupError:
            trimmed.append({**cand, "reason": cand["reason"] + " (already gone)"})
        except Exception as exc:
            failed.append({**cand, "error": str(exc)})

    return {
        "blocked": False,
        "printing": printing,
        "scanned": plan["scanned"],
        "trimmed": trimmed,
        "failed": failed,
        "kept": plan["kept"],
    }


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
