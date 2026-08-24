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

Memory is the cheaper half of the cost.  A Bambu or Elegoo printer
rations LAN connection slots, so a leftover server that ever touched
the printer also holds one — and enough of them lock the user out of
their own machine.  Field report 2026-08-14: five servers, five held
MQTT slots, printer pingable and powered on, every call timing out
with an error that blamed Bambu Studio.  The adapters now hand the
slot back when idle (``KILN_BAMBU_IDLE_DISCONNECT_S``), which fixes
the accrual at its source; this module is what makes the pile-up
visible and names it as a cause when a connection does fail.

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
import time

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

# How long a SIGTERM'd server gets to actually exit before the kill is
# escalated, and how long a SIGKILL gets to land before the PID is
# declared unkillable.  Short on purpose: candidates are idle husks, a
# healthy one exits in milliseconds, and the wedged ones (see
# ``perform_trim``) never exit on SIGTERM no matter how long the wait.
_TERM_GRACE_S = 2.0
_KILL_GRACE_S = 2.0
_EXIT_POLL_INTERVAL_S = 0.1


# Printer families that ration LAN connection slots: a Bambu accepts only a
# few simultaneous MQTT clients, an Elegoo only a few websockets.  For these,
# a pile-up of servers is not merely a memory cost — each server holds a slot
# from first use, so enough of them starve the printer and the next call times
# out.  The symptom is indistinguishable from a powered-off printer, which is
# why it has to be named rather than left to the user to deduce.
_SLOT_RATIONED_TYPES = frozenset({"bambu", "elegoo"})


def _bare_host(value: str) -> str:
    """Reduce a configured host to the bare name lsof matches on.

    Config stores a bare IP for Bambu/Elegoo but a full URL for the HTTP
    backends, and the same normalisation has to hold for both or the scan
    silently matches nothing.
    """
    return value.split("//")[-1].split("/")[0].split(":")[0].strip()


def slot_rationed_hosts() -> list[str]:
    """Hosts of configured printers that ration connection slots.

    Deliberately cheap and side-effect-free — one env read plus one small
    YAML parse, no adapters built and no network touched — because the
    startup door calls this before the server is serving anything.  An
    unreadable config answers "none": a warning that overstates the stakes on
    every install would be its own kind of wrong.
    """
    hosts: list[str] = []
    if os.environ.get("KILN_PRINTER_TYPE", "").strip().lower() in _SLOT_RATIONED_TYPES:
        env_host = _bare_host(os.environ.get("KILN_PRINTER_HOST", ""))
        if env_host:
            hosts.append(env_host)
        else:
            # Type says it rations, but we cannot name the machine.  Recorded
            # as an unnamed host so callers still know the stakes are higher,
            # even though there is nothing to scan.
            hosts.append("")
    try:
        from pathlib import Path

        import yaml

        raw = (Path.home() / ".kiln" / "config.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        logger.debug("serve-siblings: config read for printer types failed: %s", exc)
        return hosts
    printers = data.get("printers")
    entries = printers.values() if isinstance(printers, dict) else printers
    if not isinstance(entries, (list, tuple)) and not isinstance(printers, dict):
        return hosts
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).strip().lower() not in _SLOT_RATIONED_TYPES:
            continue
        host = _bare_host(str(entry.get("host") or ""))
        # config.yaml registers an alias per printer ("default"), so the same
        # machine can appear twice; scan each host once.
        if host not in hosts:
            hosts.append(host)
    return hosts


def slot_rationed_printers() -> bool:
    """True when a printer that rations connection slots is configured."""
    return bool(slot_rationed_hosts())


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
        # offer is "Kiln cleans this up" and the fallback is the one that
        # needs no tools at all (leftovers die with their app).
        #
        # How urgent this is depends on what is plugged in, so the stakes
        # sentence is not a constant.  On an HTTP-polled printer the old
        # line held: leftovers cost memory, not correctness.  On a Bambu or
        # an Elegoo it was FALSE — those ration LAN connection slots, each
        # server holds one from first use, and enough of them lock the user
        # out of their own printer.  Saying "nothing is broken" to someone
        # whose printer had just stopped answering is what sent them to
        # power-cycle the printer instead of running `kiln trim`
        # (2026-08-14 field report: five servers, five held MQTT slots).
        if slot_rationed_printers():
            stakes = (
                "On your printer this one does bite: Bambu and Elegoo "
                "machines accept only a few LAN connections at a time, and "
                "each of these copies can hold one. That is a common reason "
                "a printer that is powered on and on the network suddenly "
                "times out — the printer is fine, its connection slots are "
                "just taken. No print already running is at risk."
            )
        else:
            stakes = (
                "Nothing is broken and no print is at risk — they are just "
                "quietly using memory."
            )
        result["warning"] = (
            f"{len(procs)} background copies of Kiln's server are running "
            f"(oldest has been up {_humanize_etime(result['oldest_age'])}). "
            f"Each open agent session normally keeps just one — the rest "
            f"are leftovers from closed sessions. {stakes} Kiln can close "
            f"the leftovers for you whenever you like — just say so, and "
            f"it will check that nothing is printing first (tool: "
            f"trim_serve_processes; terminal: `kiln trim`). They also "
            f"clear on their own next time you fully quit your Claude/MCP "
            f"apps."
        )
    return result


def printer_connection_holders(host: str) -> dict:
    """Name this machine's processes currently connected to *host*.

    The process count alone is a proxy: a server that never touched the
    printer holds no slot.  This asks the kernel the exact question instead —
    which local processes have a socket open to the printer right now —
    because that is the number that decides whether the next call gets in.

    Returns::

        {
            "supported": bool,          # False = lsof unavailable, unknown
            "holders": [{"pid", "command", "is_kiln"}, ...],
            "kiln_count": int,          # holders that are Kiln's own servers
        }

    ``supported: False`` means "could not tell", never "none" — a diagnostic
    that reports a clean bill of health because its tool was missing is worse
    than one that admits it does not know.
    """
    unknown = {"supported": False, "holders": [], "kiln_count": 0}
    if not host or os.name != "posix":
        return unknown
    try:
        out = subprocess.run(
            # -n/-P skip DNS and port-name lookups (slow, and irrelevant
            # here); the @host filter keeps this to the printer's sockets.
            ["lsof", "-nP", "-a", "-u", str(os.getuid()), "-i", f"@{host}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        logger.debug("serve-siblings: lsof scan failed: %s", exc)
        return unknown
    # lsof exits 1 with no output when nothing matches, which is a real
    # "zero holders" answer, not a failure.  Anything else with no header
    # means the tool did not run as expected.
    lines = out.stdout.splitlines()
    if not lines:
        return {"supported": out.returncode in (0, 1), "holders": [], "kiln_count": 0}

    holders: dict[int, dict] = {}
    for line in lines[1:]:  # skip the COMMAND/PID/... header
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if "(ESTABLISHED)" not in line:
            continue
        holders.setdefault(pid, {"pid": pid, "command": parts[0]})

    if holders:
        serve_pids = {p["pid"] for p in (_list_serve_processes() or [])}
        for pid, entry in holders.items():
            entry["is_kiln"] = pid in serve_pids
    ordered = sorted(holders.values(), key=lambda h: h["pid"])
    return {
        "supported": True,
        "holders": ordered,
        "kiln_count": sum(1 for h in ordered if h.get("is_kiln")),
    }


def printer_slot_report() -> dict:
    """Are this machine's own servers using up a printer's connection slots?

    The ONE answer to that question, so the terminal (``kiln doctor``) and the
    agent-facing health tools cannot give a user different stories about the
    same printer.  Returns::

        {
            "checked": bool,           # False = nothing to check / cannot tell
            "hosts": [ {host, kiln_count, total, pids}, ... ],
            "warning": str | None,     # set when Kiln holds more than one slot
        }

    Only runs the socket scan for printers that actually ration connections,
    so installs with an HTTP-polled printer pay nothing for it.

    ``warning`` is user-facing text.  Agents seeing it set should relay it —
    it names the one cause of a printer timeout that the user cannot guess and
    would otherwise "fix" by power-cycling hardware that was never at fault.
    """
    hosts = [h for h in slot_rationed_hosts() if h]
    if not hosts:
        return {"checked": False, "hosts": [], "warning": None}

    reports: list[dict] = []
    checked = False
    for host in hosts:
        held = printer_connection_holders(host)
        if not held["supported"]:
            continue
        checked = True
        reports.append(
            {
                "host": host,
                "kiln_count": held["kiln_count"],
                "total": len(held["holders"]),
                "pids": [h["pid"] for h in held["holders"] if h.get("is_kiln")],
            }
        )

    crowded = [r for r in reports if r["kiln_count"] > 1]
    warning = None
    if crowded:
        worst = max(crowded, key=lambda r: r["kiln_count"])
        warning = (
            f"{worst['kiln_count']} copies of Kiln's server are each holding a "
            f"connection to the printer at {worst['host']}. These printers "
            f"allow only a few connections at once, so this is a common reason "
            f"one that is powered on and on the network still times out — the "
            f"printer is fine, its connection slots are taken. Closing the "
            f"leftover servers frees them (tool: trim_serve_processes; "
            f"terminal: `kiln trim`). Power-cycling the printer will not help."
        )
    return {"checked": checked, "hosts": reports, "warning": warning}


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
            from kiln.printers.engagement import internal_read

            # This answers "is anything actually printing?" before trimming
            # sibling servers, so a refusal must never be able to disguise a
            # RUNNING machine as unknown.  Kiln is asking about its own
            # hardware here; nobody is commanding a printer.
            with internal_read():
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
      conservative default: propose servers older than
      ``_DEFAULT_IDLE_HOURS``, plus anything beyond the
      ``_warn_threshold()`` most recently started — the same "more
      copies than any plausible number of live sessions" line the
      pile-up warning draws, so the default trim can always get back
      under it (age alone no-opped on same-day pile-ups).

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
        # Two conditions propose a server, either alone sufficient.  Age
        # alone used to be the whole default — and it no-ops in exactly
        # the pile-up case this module was written for (2026-08-07:
        # eleven servers holding ~1 GB, every one under six hours old,
        # default plan "nothing to trim"; the module docstring's own
        # field report, 18 servers overnight, is the LUCKY shape).  So
        # the warn threshold caps the default keep too: it is the same
        # line ``check_serve_siblings`` already draws for "more copies
        # than any plausible number of live sessions", and a janitor
        # whose default cannot get back under the line its own warning
        # fires at is theater.  Worst case is unchanged from the module
        # doctrine: nothing is printing (``perform_trim`` guards that),
        # so an over-trim costs a still-open session one reconnect.
        idle_limit_s = _DEFAULT_IDLE_HOURS * 3600
        threshold = _warn_threshold()
        keep_slots = max(0, threshold - 1) if self_is_a_server else threshold
        for rank, proc in enumerate(others):
            entry = {"pid": proc["pid"], "age": proc["age"]}
            age_s = _etime_seconds(proc["age"])
            aged_out = age_s is not None and age_s > idle_limit_s
            beyond_cap = rank >= keep_slots
            if aged_out or beyond_cap:
                reason = (
                    f"running {_humanize_etime(proc['age'])}"
                    if aged_out
                    else (
                        f"beyond the {threshold} most recently started — "
                        f"more copies than open sessions plausibly need "
                        f"(running {_humanize_etime(proc['age'])})"
                    )
                )
                candidates.append(
                    {
                        **entry,
                        "age_human": _humanize_etime(proc["age"]),
                        "reason": reason,
                    }
                )
            else:
                kept.append({**entry, "reason": "started recently"})

    # Oldest first, mirroring check_serve_siblings ordering.
    candidates.sort(key=lambda c: (len(c["age"]), c["age"]), reverse=True)
    return {"scanned": len(procs), "candidates": candidates, "kept": kept}


def _is_zombie(pid: int) -> bool:
    """True when *pid* has exited and only awaits its parent's reap.

    A zombie holds a process-table slot but no memory, so for trimming
    purposes it is dead — reporting it as a survivor would tell the
    user a cleanup failed when everything reclaimable was reclaimed.
    ``kill(pid, 0)`` cannot make this distinction (it succeeds on
    zombies), so ask ps for the state.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return out.returncode == 0 and out.stdout.strip().startswith("Z")


def _still_running(pid: int) -> bool:
    """True while *pid* is a live, non-zombie process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but is no longer ours to probe — treat as running so
        # the caller reports a failure instead of a phantom success.
        return True
    except Exception:
        return True
    return not _is_zombie(pid)


def _await_exit(pids: list[int], deadline_s: float) -> set[int]:
    """Poll until every pid is gone or *deadline_s* passes; return survivors.

    Returns as soon as the set is empty, so the healthy case (servers
    that honor SIGTERM) costs milliseconds, not the full grace window.
    """
    remaining = set(pids)
    deadline = time.monotonic() + deadline_s
    while remaining:
        remaining = {pid for pid in remaining if _still_running(pid)}
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(_EXIT_POLL_INTERVAL_S)
    return remaining


def perform_trim(open_sessions: int | None = None, force: bool = False) -> dict:
    """Shut down the leftover servers ``plan_trim`` identified.

    Refuses while any printer has a job in flight unless *force* is
    set — that is the one case where a kill costs the user something
    they cannot trivially recover (see module docstring).  Re-plans
    against a fresh process scan immediately before signalling, so a
    recycled PID can never be hit: every target is matcher-verified as
    a ``kiln serve`` at kill time.  Never this process.

    ``trimmed`` means VERIFIED gone — a delivered signal is not an
    exited process, and nothing enters ``trimmed`` unmeasured.
    (2026-08-07: six servers were reported "trimmed" and were all still
    alive a minute later with their original start times.  ``kiln
    serve`` installs a SIGTERM handler that runs several ``.stop()``
    calls before exiting; Python signal handlers need the main thread
    at a bytecode boundary, so a wedged main thread ignores SIGTERM
    forever, and the old code appended to ``trimmed`` the moment
    ``os.kill`` didn't raise.)  So: SIGTERM first (a healthy server
    exits cleanly in milliseconds), poll for actual exit, escalate the
    survivors to SIGKILL — which no handler can ignore — and re-verify.
    A PID still alive after both lands in ``failed`` with the reason,
    never in ``trimmed``.  Zombies count as gone: their memory is
    already reclaimed and only a parent's reap is pending.
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
    signalled: list[dict] = []
    for cand in plan["candidates"]:
        try:
            os.kill(cand["pid"], signal.SIGTERM)
            signalled.append(cand)
        except ProcessLookupError:
            trimmed.append({**cand, "reason": cand["reason"] + " (already gone)"})
        except Exception as exc:
            failed.append({**cand, "error": str(exc)})

    term_survivors = _await_exit(
        [c["pid"] for c in signalled], _TERM_GRACE_S
    )
    escalate: list[dict] = []
    for cand in signalled:
        if cand["pid"] in term_survivors:
            escalate.append(cand)
        else:
            trimmed.append(cand)

    kill_pending: list[dict] = []
    for cand in escalate:
        try:
            os.kill(cand["pid"], signal.SIGKILL)
            kill_pending.append(cand)
        except ProcessLookupError:
            # Died between the poll and the escalation — still verified.
            trimmed.append(cand)
        except Exception as exc:
            failed.append(
                {**cand, "error": f"ignored SIGTERM, and SIGKILL failed: {exc}"}
            )

    unkillable = _await_exit([c["pid"] for c in kill_pending], _KILL_GRACE_S)
    for cand in kill_pending:
        if cand["pid"] in unkillable:
            failed.append(
                {
                    **cand,
                    "error": (
                        "still running after SIGTERM and SIGKILL "
                        f"({_TERM_GRACE_S + _KILL_GRACE_S:g}s) — likely stuck "
                        "in an uninterruptible kernel wait; it should clear "
                        "when the machine or the blocking I/O does"
                    ),
                }
            )
        else:
            trimmed.append(
                {**cand, "reason": cand["reason"] + " (needed a force kill)"}
            )

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
