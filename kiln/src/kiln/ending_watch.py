"""The ending of a print outlives the session that started it.

Kiln sees every print it starts, because it causes it.  Inside a live
process it also sees the ending: the print watchdog polls, a Bambu's push
thread announces the terminal frame, and the pending outcome row opened at
the start is resolved on that edge.  What it did not survive was the
process going away first.  An agent session lasts minutes and a print
lasts hours, the CLI exits the moment the start is acknowledged, and the
next process to connect finds an idle machine that cannot say how the
last job ended -- so the row settled to ``unknown`` and the person was
asked a question Kiln could have answered.

This module is the fix at the one place every start passes
(:meth:`~kiln.printers.base.PrinterAdapter.start_print`): a small detached
process per MACHINE that waits for the starting process to go away, then
holds the printer's status itself -- the same adapter, the same
``get_state`` wrap, the same lifecycle hook -- until no pending print
remains on that machine.  While the parent lives it holds nothing, so a
Bambu's rationed MQTT slots are not spent twice; when the parent is gone
it takes over the one slot the parent released.

The start time rides on the pending row (``created_at`` is wall clock), so
the ending the watcher sees banks the print's duration too, which is what
the telemetry's ``prints_hours_known`` counts.

Never on the hosted process (printers live on the person's machine), never
under a test run or a sandbox unless a test asks for it by name, and
switched off with ``KILN_ENDING_WATCH=0``.  Nothing here can raise into a
print.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ``KILN_ENDING_WATCH=0`` switches the watcher off on this machine.
SWITCH_ENV = "KILN_ENDING_WATCH"
#: A test that wants the spawn to run (with Popen patched) sets this.
TEST_OPT_IN_ENV = "KILN_ENDING_WATCH_IN_TESTS"
#: How often the watcher asks whether the starting process is still alive.
PARENT_POLL_S = 5.0
#: How often the watcher reads the printer once it holds it: within the
#: watched-ending gap by a wide margin, so the ending banks as SEEN.
STATE_POLL_S = 2.5
#: The longest a watcher holds a machine; a print past this is a record.
MAX_WATCH_S = 48 * 3600.0
#: How long a machine may sit NOT printing with a row still pending before
#: the watcher leaves the row for the next session's reconcile or the
#: person's answer.  The adapter's own reconnect reconcile normally settles
#: it within seconds; this is the floor under an adapter that has none.
NOT_PRINTING_GRACE_S = 10 * 60.0
#: Backoff when the printer cannot be reached: start, ceiling.
UNREACHABLE_BACKOFF_S = (10.0, 300.0)

_ACTIVE_STATES = frozenset({"printing", "paused", "busy", "cancelling"})


# ---------------------------------------------------------------------------
# The spawn side -- runs inside start_print, must never raise
# ---------------------------------------------------------------------------


def _switched_off() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() in {"0", "off", "false", "no"}


def should_spawn() -> bool:
    """Whether this process should spawn a watcher for a print it started."""
    if _switched_off():
        return False
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return False
    except Exception:  # noqa: BLE001
        return False
    if os.environ.get(TEST_OPT_IN_ENV, "").strip() == "1":
        return True
    try:
        from kiln.dev_home_guard import SANDBOX_ENV, _pytest_running

        if _pytest_running() or os.environ.get(SANDBOX_ENV, "").strip():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())[:80] or "printer"


def watch_dir() -> Path:
    from kiln.startup_failure import kiln_home

    return kiln_home() / "ending_watch"


def _pid_file(printer_name: str) -> Path:
    return watch_dir() / f"{_slug(printer_name)}.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _watcher_running(printer_name: str) -> bool:
    try:
        pid = int(_pid_file(printer_name).read_text().strip() or 0)
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def spawn_ending_watch(adapter: Any, file_name: str) -> int | None:
    """Spawn the detached watcher for the machine *adapter* just started.

    One watcher per machine: a live one already watching keeps watching
    (it leaves only when no pending row remains, so the new print is
    covered).  Returns the child's pid, or ``None`` when nothing was
    spawned.  Never raises.
    """
    try:
        if not should_spawn():
            return None
        from kiln.printers.base import outcome_printer_name

        name = outcome_printer_name(adapter)
        if not name:
            return None
        if _watcher_running(name):
            logger.debug("ending watch: %s already watched", name)
            return None
        wdir = watch_dir()
        wdir.mkdir(parents=True, exist_ok=True)
        log_path = wdir / f"{_slug(name)}.log"
        cmd = [
            sys.executable, "-m", "kiln.ending_watch",
            "--printer", name,
            "--parent", str(os.getpid()),
        ]
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(  # noqa: S603 -- our own interpreter, our own module
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=str(Path.home()),
                env=dict(os.environ),
            )
        _pid_file(name).write_text(str(proc.pid))
        logger.info("ending watch for %s: pid %s (file %s)", name, proc.pid, file_name)
        return proc.pid
    except Exception:  # noqa: BLE001 -- a watcher never affects a print
        logger.debug("ending watch spawn failed (non-fatal)", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The watcher side -- its own process
# ---------------------------------------------------------------------------


def _pending_rows(printer_name: str) -> list[dict[str, Any]]:
    from kiln.persistence import get_db

    rows = get_db().list_unresolved_outcomes(printer_name=printer_name, limit=50)
    return [r for r in rows if r.get("outcome") == "pending"]


def _resolve(printer_name: str) -> Any:
    """The adapter for *printer_name* in THIS process, or ``None``.

    The registry, then config.yaml, then the default printer when the name
    the row carries is a backend family (a CLI-built adapter files under
    its family) and the default is of that family.
    """
    from kiln import server as _srv

    try:
        return _srv._resolve_adapter(printer_name)
    except Exception:  # noqa: BLE001
        pass
    try:
        default = _srv._get_adapter()
    except Exception:  # noqa: BLE001
        return None
    family = str(getattr(default, "name", "") or "").lower()
    return default if family and family == printer_name.lower() else None


def _stamp_start_from_row(adapter: Any, row: dict[str, Any]) -> None:
    """Give the stopwatch the row's wall-clock start, so the ending banks hours."""
    try:
        from kiln.printers.progress_motion import note_job_start

        created = float(row.get("created_at") or 0.0)
        if created <= 0:
            return
        age = max(0.0, time.time() - created)
        note_job_start(adapter, None, at=time.monotonic() - age)
    except Exception:  # noqa: BLE001
        logger.debug("ending watch: start stamp failed", exc_info=True)


def watch(
    printer_name: str,
    parent_pid: int,
    *,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    resolve: Any = _resolve,
    pending: Any = _pending_rows,
    max_watch_s: float = MAX_WATCH_S,
) -> str:
    """Hold *printer_name* until nothing is pending on it.  Returns why it left.

    Pure enough to drive from a test: the sleeper, the clock, the adapter
    resolver and the pending-row reader are all injectable.
    """
    began = clock()
    # Phase 1: the starting process is alive and watching; hold nothing.
    while parent_pid > 0 and _pid_alive(parent_pid):
        if not pending(printer_name):
            return "settled_by_parent"
        if clock() - began > max_watch_s:
            return "timed_out"
        sleep(PARENT_POLL_S)
    if not pending(printer_name):
        return "nothing_pending"
    # Phase 2: the parent is gone; take the machine.
    adapter = resolve(printer_name)
    if adapter is None:
        return "no_adapter"
    stamped = False
    backoff = UNREACHABLE_BACKOFF_S[0]
    not_printing_since: float | None = None
    try:
        while clock() - began <= max_watch_s:
            rows = pending(printer_name)
            if not rows:
                return "settled"
            if not stamped:
                _stamp_start_from_row(adapter, rows[0])
                stamped = True
            try:
                state = adapter.get_state()
            except Exception:  # noqa: BLE001
                logger.debug("ending watch: %s unreachable, retry in %.0fs", printer_name, backoff)
                sleep(backoff)
                backoff = min(backoff * 2, UNREACHABLE_BACKOFF_S[1])
                continue
            backoff = UNREACHABLE_BACKOFF_S[0]
            word = str(getattr(getattr(state, "state", None), "value", "") or "").lower()
            if word in _ACTIVE_STATES:
                not_printing_since = None
            else:
                now = clock()
                if not_printing_since is None:
                    not_printing_since = now
                elif now - not_printing_since > NOT_PRINTING_GRACE_S:
                    # The adapter's own reconcile had its chance; the row
                    # stays pending for the next session and the person.
                    return "not_printing"
            sleep(STATE_POLL_S)
        return "timed_out"
    finally:
        with contextlib.suppress(Exception):
            adapter.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kiln.ending_watch", add_help=True)
    parser.add_argument("--printer", required=True)
    parser.add_argument("--parent", type=int, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pid_file = _pid_file(args.printer)
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        why = watch(args.printer, args.parent)
        logger.info("ending watch for %s left: %s", args.printer, why)
        return 0
    finally:
        try:
            if pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
