"""Parent-process watchdog for ``kiln serve``.

MCP servers are spawned by their host (Claude Code, Claude Desktop,
or any other MCP client) and ought to die when the host disconnects.
``mcp.run()`` correctly exits when stdin closes — but on macOS, when
the parent process is force-killed or crashes without closing the
pipe (e.g. ``kill -9``, OS-level OOM, parent panic), the child is
adopted by ``launchd`` (PPID becomes 1), stdin stays open from
launchd's perspective, and ``mcp.run()`` blocks forever waiting for
JSON-RPC traffic that will never arrive.

Result: zombie ``kiln serve`` processes that hold open file
descriptors, network connections, and event-loop state for hours or
days until the user notices.  Field reports of 24+ accumulated
zombies in 2.5 days, eating 15,000+ ephemeral TCP ports.

This watchdog runs as a daemon thread and exits the process when
``getppid()`` transitions from "real parent" to ``1`` (orphaned by
launchd / init).  Cheap (one syscall every 30s), reliable
(``getppid`` never lies), and conservative (only exits on a clear
parent-died signal, never on a transient).

Use:

    from kiln.parent_watchdog import start_parent_watchdog
    start_parent_watchdog()
    mcp.run()

Disable when ``kiln serve`` is managed directly by an init system
(systemd, launchd) by setting ``KILN_DISABLE_ORPHAN_WATCHDOG=1`` in
the environment — those supervisors set PPID=1 from the start, and
the watchdog correctly no-ops in that case anyway, but the env var
makes the intent explicit and silences the startup log line.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

# Default poll interval — 30 seconds is a balance between "exit
# promptly when orphaned" (so accumulated zombies don't pile up) and
# "minimal syscall overhead" (one ``getppid()`` is sub-microsecond,
# but waking the thread costs more).  Override with
# ``KILN_ORPHAN_WATCHDOG_INTERVAL_S`` for tests.
_DEFAULT_INTERVAL_S = 30.0


def start_parent_watchdog(
    *,
    interval_s: float | None = None,
    on_orphaned: callable | None = None,
) -> threading.Thread | None:
    """Start a daemon thread that exits the process when orphaned.

    Returns the thread (started, daemonized) on success, or ``None``
    when the watchdog is disabled or the initial state is already
    "no parent worth watching."

    Args:
        interval_s: Override the poll interval (seconds).  Falls
            back to the ``KILN_ORPHAN_WATCHDOG_INTERVAL_S`` env var,
            then to 30s.
        on_orphaned: Optional callback for tests.  When set, the
            watchdog calls it instead of ``sys.exit(0)``.  The real
            ``kiln serve`` never sets this.
    """
    if os.environ.get("KILN_DISABLE_ORPHAN_WATCHDOG"):
        logger.debug("orphan-watchdog: disabled by KILN_DISABLE_ORPHAN_WATCHDOG")
        return None

    initial_ppid = os.getppid()
    if initial_ppid == 1:
        # The process was started directly under init/launchd (e.g.
        # systemd unit, brew services, manual nohup).  Nothing to
        # watch — the supervisor manages the lifetime.
        logger.debug(
            "orphan-watchdog: PPID is already 1 at startup — skipping watchdog "
            "(process is supervisor-managed, not parented to an MCP client)",
        )
        return None

    if interval_s is None:
        env_override = os.environ.get("KILN_ORPHAN_WATCHDOG_INTERVAL_S")
        if env_override:
            try:
                interval_s = float(env_override)
            except ValueError:
                interval_s = _DEFAULT_INTERVAL_S
        else:
            interval_s = _DEFAULT_INTERVAL_S
    interval_s = max(1.0, float(interval_s))

    def _loop() -> None:
        while True:
            time.sleep(interval_s)
            current = os.getppid()
            if current == initial_ppid:
                continue
            if current == 1:
                # Parent died and the process was reparented to
                # init/launchd.  The MCP host is gone; nobody is
                # going to talk to us again.  Exit cleanly so file
                # descriptors and connections release.
                logger.info(
                    "orphan-watchdog: parent (PID %d) is gone, exiting",
                    initial_ppid,
                )
                if on_orphaned is not None:
                    on_orphaned()
                    return
                # ``sys.exit`` from a non-main thread only raises
                # ``SystemExit`` in that thread — not effective.
                # Use ``os._exit`` to actually terminate the process.
                # We've already done the best-effort logging above;
                # there's nothing else worth running atexit handlers
                # for in an orphaned MCP server.
                os._exit(0)
            # Parent changed but still nonzero — unusual (re-parented
            # to a session leader rather than init).  Track the new
            # parent so the next "vanished to init" check still works.
            logger.debug(
                "orphan-watchdog: parent changed %d → %d (still alive)",
                initial_ppid,
                current,
            )

    thread = threading.Thread(
        target=_loop,
        name="kiln-orphan-watchdog",
        daemon=True,
    )
    thread.start()
    logger.debug(
        "orphan-watchdog: started (parent=%d, interval=%.1fs)",
        initial_ppid,
        interval_s,
    )
    return thread
