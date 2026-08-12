"""Keep the bridge process alive for as long as the run that started it.

``kiln bridge start`` promises a bridge that runs "until you log out".  Until
now it delivered a bare detached process: kill it — or let it crash, or get it
OOM-killed — and nothing brought it back and nobody was told.  Measured
2026-08-11 against a real printer: ``kill -9`` on the bridge mid-print left the
printer happily printing (the machine owns the job) and the relay answering a
correct ``409``, but the bridge stayed dead until a human typed
``kiln bridge start`` again.  Earlier the same day the bridge had been found
simply "off" for the same reason, with the web app silently unreachable for
however long that had been true.

**Why this is a separate process and not a retry loop inside the bridge.**
:class:`~kiln.bridge_client.BridgeClient` already reconnects forever on a
dropped *socket*, and that is a different failure.  A socket drop leaves the
process alive to notice it; ``kill -9``, an OOM kill, or a hard interpreter
crash do not, and no in-process code can outlive them by definition.  Something
outside the process has to be watching, which means a second process.

**Why this does not replace the login service.**  Two supervisors stacked on
one child is a bug, not belt-and-braces.  ``kiln bridge enable`` already hands
the job to the operating system on the two platforms whose supervisors do it
properly — launchd (``KeepAlive``) and systemd (``Restart=always``) — and those
also survive a logout and a reboot, which this cannot.  So this module
supervises exactly the two cases the OS does not cover: a manual
``kiln bridge start`` on any platform, and ``enable`` on Windows, whose
``Run`` key only launches at login and never watches afterwards.

**Why it gives up.**  A bridge that dies within seconds of every start is not
crashing, it is refusing: a revoked licence, a missing dependency, a broken
install.  Restarting that forever burns a core and buries the reason.  After
:data:`_MAX_RAPID_RESTARTS` consecutive short-lived runs the supervisor stops
and — this is the part that matters — leaves its state file behind saying so,
which is how ``kiln bridge status`` can explain a dead bridge instead of
reporting a shrug.  A run that lasted long enough to be healthy resets the
budget, so a genuine crash after a week of service still gets the full five.

The loop itself (:func:`supervise`) takes its spawn, sleep and clock as
arguments, so the whole restart policy is exercised in tests without spawning a
process or waiting a real second.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Where the supervisor advertises itself.  Deliberately NOT the bridge's own
#: ``bridge.state``: the child rewrites that file on every connect and drop,
#: and a crash record that a reconnect erases is a crash record nobody reads.
_SUPERVISOR_STATE = "~/.kiln/bridge.supervisor"

_FIRST_RETRY_S = 1.0
_MAX_RETRY_S = 60.0

#: A run this long has cleared startup: the bridge either connected or entered
#: its own reconnect loop, which never exits on its own.  Anything shorter is
#: counted against the crash-loop budget.
_HEALTHY_RUN_S = 60.0

#: Consecutive short-lived runs we will restart through before stopping and
#: recording why.  Five restarts at 1/2/4/8/16s is about half a minute of
#: patience — long enough to ride out a transient, short enough that a broken
#: install surfaces while the person who caused it is still at the keyboard.
_MAX_RAPID_RESTARTS = 5

# Verdicts from :func:`supervise` — why the loop returned.
STOPPED = "stopped"          # asked to stop (SIGTERM / `kiln bridge stop`)
CLEAN_EXIT = "clean-exit"    # the bridge exited 0; respect it, don't restart
GAVE_UP = "gave-up"          # crash loop; stopped and recorded the reason


# ---------------------------------------------------------------------------
# Supervisor state — read by `kiln bridge status`, written only here
# ---------------------------------------------------------------------------


def _state_path() -> str:
    return os.path.expanduser(_SUPERVISOR_STATE)


def read_supervisor_state() -> dict[str, Any]:
    """The supervisor's last-written state, or ``{}``.

    Never raises: a missing file simply means no supervisor has run, and a
    truncated one (killed mid-write) must not break ``kiln bridge status``.
    """
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_supervisor_state(**fields: Any) -> None:
    """Merge *fields* into the supervisor state file (best-effort, never raises).

    A merge rather than a write because the fields have different lifetimes:
    ``pid`` is set once at startup and ``restarts`` accumulates over the run,
    and neither should erase the other.
    """
    try:
        state = read_supervisor_state()
        state.update(fields)
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def clear_supervisor_state() -> None:
    """Remove the supervisor state file (best-effort).

    Called on a deliberate stop — NOT after a give-up, whose whole purpose is
    to leave an explanation on disk for the next ``kiln bridge status``.
    """
    with contextlib.suppress(OSError):
        os.unlink(_state_path())


def supervisor_pid() -> int | None:
    """The pid recorded by a supervisor, or ``None``.  Liveness is the
    caller's job — ``kiln.cli.bridge_commands`` already owns the
    cross-platform "is this pid alive" probe and must not grow a second one.
    """
    pid = read_supervisor_state().get("pid")
    return pid if isinstance(pid, int) else None


# ---------------------------------------------------------------------------
# The restart policy
# ---------------------------------------------------------------------------


def supervise(
    *,
    spawn: Callable[[], Any],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    should_continue: Callable[[], bool] = lambda: True,
    on_child: Callable[[Any], None] = lambda _child: None,
    record: Callable[..., None] = update_supervisor_state,
    max_rapid_restarts: int = _MAX_RAPID_RESTARTS,
    healthy_run_s: float = _HEALTHY_RUN_S,
) -> str:
    """Run the bridge, restarting it when it dies.  Returns a verdict constant.

    *spawn* returns a process handle exposing ``.pid`` and a blocking
    ``.wait()`` that yields the exit code — :class:`subprocess.Popen` in
    production, a fake in tests.  *on_child* is handed each new handle so the
    caller's signal handler can reach the live child and terminate it.

    Every dependency that touches the clock or the OS is an argument, because
    the interesting behaviour here is entirely in the policy — when to retry,
    how long to wait, when to stop — and none of it should need a real process
    or a real second to test.
    """
    rapid_failures = 0
    delay = _FIRST_RETRY_S
    restarts = 0

    while should_continue():
        started = clock()
        child = spawn()
        on_child(child)
        logger.info("bridge supervisor: bridge running (pid %s)", getattr(child, "pid", "?"))
        code = child.wait()
        ran_for = clock() - started

        if not should_continue():
            # Asked to stop while the child was running; its exit is ours.
            return STOPPED
        if code == 0:
            # The bridge decided it was done.  It never does this today, but
            # if it ever learns to, second-guessing it is how you build a
            # process a user cannot turn off.
            logger.info("bridge supervisor: bridge exited cleanly; not restarting")
            return CLEAN_EXIT

        if ran_for >= healthy_run_s:
            rapid_failures = 0
            delay = _FIRST_RETRY_S
        else:
            rapid_failures += 1

        if rapid_failures > max_rapid_restarts:
            logger.error(
                "bridge supervisor: bridge died %d times in a row within %.0fs "
                "of starting (last exit %s); not restarting again",
                rapid_failures, healthy_run_s, code,
            )
            record(gave_up=True, last_exit={"code": code, "at": time.time()},
                   restarts=restarts)
            return GAVE_UP

        restarts += 1
        logger.warning(
            "bridge supervisor: bridge exited %s after %.0fs; restarting in %.0fs",
            code, ran_for, delay,
        )
        record(restarts=restarts, last_exit={"code": code, "at": time.time()},
               gave_up=False)
        sleep(delay)
        delay = min(delay * 2, _MAX_RETRY_S)

    return STOPPED


# ---------------------------------------------------------------------------
# Production wiring
# ---------------------------------------------------------------------------


class _Runner:
    """Holds the live child so a signal handler can stop it.

    ``child.wait()`` blocks, and a SIGTERM handler that only sets a flag would
    not be noticed until the child happened to die on its own — which, for a
    bridge whose reconnect loop never exits, is never.  So the handler has to
    reach in and terminate the child itself.
    """

    def __init__(self) -> None:
        self._stop = False
        self._child: Any = None

    def keep_going(self) -> bool:
        return not self._stop

    def track(self, child: Any) -> None:
        self._child = child
        if self._stop:  # stop arrived between the check and the spawn
            self.terminate_child()

    def terminate_child(self) -> None:
        child = self._child
        if child is not None:
            with contextlib.suppress(Exception):
                child.terminate()

    def request_stop(self, *_signal_args: Any) -> None:
        self._stop = True
        self.terminate_child()


def _spawn_child() -> subprocess.Popen:
    """Start one bridge process, sharing our stdout/stderr.

    The caller (``kiln bridge start`` / the Windows login entry) already points
    those at ``~/.kiln/bridge.log``, so the bridge's log lines and the
    supervisor's restart lines land in one file in the order they happened.
    """
    return subprocess.Popen([sys.executable, "-m", "kiln.bridge_client"])


def run_supervisor() -> None:
    """Blocking entry point: ``python -m kiln.bridge_supervisor``."""
    logging.basicConfig(level=logging.INFO)
    runner = _Runner()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(sig, runner.request_stop)

    # A fresh run supersedes whatever the last one concluded — including a
    # give-up record, which has served its purpose once someone starts again.
    clear_supervisor_state()
    update_supervisor_state(pid=os.getpid(), started=time.time(), restarts=0,
                            gave_up=False)
    verdict = STOPPED
    try:
        verdict = supervise(
            spawn=_spawn_child,
            should_continue=runner.keep_going,
            on_child=runner.track,
        )
    finally:
        # A give-up is the one outcome whose record has to outlive the process:
        # it is the only thing that can tell the next `kiln bridge status` why
        # there is no bridge.  Everything else — a deliberate stop, a clean
        # exit, or a crash in the supervisor itself (whose traceback is in the
        # log) — leaves nothing worth reporting, so it leaves nothing behind.
        if verdict != GAVE_UP:
            clear_supervisor_state()


if __name__ == "__main__":
    run_supervisor()
