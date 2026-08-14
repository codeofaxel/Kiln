"""The SIGTERM path ends the process — guaranteed, bounded, always.

Diagnosed live 2026-08-09 on a machine carrying 15 accumulated ``kiln
serve`` processes, 10 of which ignored SIGTERM outright.  Stack dumps
of an instrumented server showed the full chain: the handler RAN, then
(1) stalled ~50s in the heater watchdog's un-wakeable poll sleep, then
(2) ``sys.exit(0)`` unwound the asyncio loop abnormally, so anyio never
sent its worker threads their shutdown command — and those workers are
non-daemon, blocked forever on ``queue.get()``, so the interpreter sat
in ``threading._shutdown`` joining them for eternity.  The process
survived its own exit.

The fix is ``_graceful_shutdown``: a daemon dead-man timer armed FIRST,
every service stop isolated in its own try/except, and ``os._exit`` in
a ``finally`` — the same lesson ``parent_watchdog`` already carries for
its own exit path ("``sys.exit`` from a non-main thread only raises;
use ``os._exit`` to actually terminate").  These tests pin the three
properties that made the husk possible when absent:

* the exit call is reached even when a stop wedges (the dead-man);
* one raising stop cannot skip the stops after it;
* there is no path through the function that leaves the process alive.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import kiln.server as server


class _Recorder:
    """A stand-in service whose stop() records, raises, or wedges."""

    def __init__(self, behavior: str = "ok", wedge_s: float = 0.0) -> None:
        self.behavior = behavior
        self.wedge_s = wedge_s
        self.stopped = False

    def stop(self) -> None:
        if self.behavior == "raise":
            raise RuntimeError("stop blew up")
        if self.behavior == "wedge":
            time.sleep(self.wedge_s)
        self.stopped = True


def _run_shutdown(
    services: dict, *, deadline_s: float = 5.0, exits: list | None = None
) -> tuple[list, float]:
    """Run _graceful_shutdown against fake services; return (exits, elapsed).

    ``hard_exit`` is captured instead of executed — the production
    value is ``os._exit``, which a test can observe only from outside
    the process.  What is testable in-process is the contract around
    it: that it is CALLED, exactly how fast, and despite what failures.
    ``exits`` may be supplied by the caller so the dead-man timer's
    append is observable WHILE a wedged stop still blocks the call.
    """
    exits = [] if exits is None else exits
    patches = {
        "_get_scheduler": services.get("scheduler", _Recorder()),
        "_get_webhook_mgr": services.get("webhook", _Recorder()),
        "_get_heater_watchdog": services.get("watchdog", _Recorder()),
        "_get_stream_proxy": services.get("proxy", _Recorder()),
    }
    started = time.monotonic()
    with patch.object(server, "_get_scheduler", lambda: patches["_get_scheduler"]), \
         patch.object(server, "_get_webhook_mgr", lambda: patches["_get_webhook_mgr"]), \
         patch.object(server, "_get_heater_watchdog", lambda: patches["_get_heater_watchdog"]), \
         patch.object(server, "_get_stream_proxy", lambda: patches["_get_stream_proxy"]), \
         patch.object(server, "_get_cloud_sync", lambda: services.get("cloud_sync")), \
         patch.object(server, "_watchers", {}):
        server._graceful_shutdown(hard_exit=exits.append, deadline_s=deadline_s)
    return exits, time.monotonic() - started


class TestGracefulShutdown:
    def test_happy_path_stops_everything_and_exits_fast(self) -> None:
        services = {name: _Recorder() for name in ("scheduler", "webhook", "watchdog", "proxy")}
        exits, elapsed = _run_shutdown(services)
        assert exits == [0]
        assert elapsed < 1.0, "a clean shutdown must not linger"
        assert all(s.stopped for s in services.values())

    def test_a_raising_stop_skips_nothing(self) -> None:
        services = {
            "scheduler": _Recorder("raise"),
            "webhook": _Recorder(),
            "watchdog": _Recorder("raise"),
            "proxy": _Recorder(),
        }
        exits, _ = _run_shutdown(services)
        assert exits == [0]
        assert services["webhook"].stopped
        assert services["proxy"].stopped

    def test_dead_man_timer_fires_when_a_stop_wedges(self) -> None:
        """The husk-maker: a stop that never returns.  The timer must
        end the process anyway, at the deadline rather than never."""
        services = {"scheduler": _Recorder("wedge", wedge_s=3.0)}
        exits: list[int] = []
        done = threading.Event()

        def _run() -> None:
            _run_shutdown(services, deadline_s=0.3, exits=exits)
            done.set()

        worker = threading.Thread(target=_run, daemon=True)
        started = time.monotonic()
        worker.start()

        # The dead-man exit must arrive around the 0.3s deadline WHILE
        # the wedged stop is still sleeping — not after it finishes.
        while time.monotonic() - started < 2.0 and not exits:
            time.sleep(0.02)
        elapsed = time.monotonic() - started
        assert exits and exits[0] == 0, "dead-man timer never fired on a wedged stop"
        assert elapsed < 2.0, (
            f"dead-man exit arrived only after the wedge ({elapsed:.2f}s)"
        )

        done.wait(timeout=5.0)

    def test_no_cloud_sync_configured_is_fine(self) -> None:
        exits, _ = _run_shutdown({}, deadline_s=5.0)
        assert exits == [0]

    def test_production_exit_is_os_exit_never_sys_exit(self) -> None:
        """The default must be ``os._exit`` — ``sys.exit`` is exactly
        the bug (SystemExit into the interrupted event-loop frame, an
        abnormal unwind, and non-daemon anyio workers left unjoinable).
        Pinned on the signature so a refactor cannot quietly regress it."""
        import inspect
        import os

        sig = inspect.signature(server._graceful_shutdown)
        assert sig.parameters["hard_exit"].default is os._exit


class _FakePrinter:
    """An adapter that records whether its connection was handed back."""

    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.disconnected = 0

    def disconnect(self) -> None:
        self.disconnected += 1
        if self.raises:
            raise RuntimeError("close blew up")


class _FakeRegistry:
    def __init__(self, adapters: dict) -> None:
        self._adapters = adapters

    def list_all(self) -> dict:
        return self._adapters


class TestReleasePrinterConnections:
    """A server that exits still holding a printer's connection leaves it
    short until the printer's own side times the socket out.  With one
    server per MCP session that shortfall is what a user meets as "the
    printer is on but Kiln can't reach it" (2026-08-14)."""

    def _release(self, singleton=None, registry: dict | None = None) -> None:
        with patch.object(server, "_adapter", singleton), patch.object(
            server, "_get_registry", lambda: _FakeRegistry(registry or {})
        ):
            server._release_printer_connections()

    def test_releases_the_default_adapter(self) -> None:
        printer = _FakePrinter()
        self._release(singleton=printer)
        assert printer.disconnected == 1

    def test_releases_every_registered_printer(self) -> None:
        a, b = _FakePrinter(), _FakePrinter()
        self._release(registry={"a": a, "b": b})
        assert a.disconnected == 1
        assert b.disconnected == 1

    def test_an_aliased_printer_is_released_once(self) -> None:
        """config.yaml registers an alias per printer ("default"), so the
        same object arrives twice; closing it twice is noise at best."""
        printer = _FakePrinter()
        self._release(singleton=printer, registry={"default": printer, "a1": printer})
        assert printer.disconnected == 1

    def test_one_failure_does_not_strand_the_others(self) -> None:
        bad, good = _FakePrinter(raises=True), _FakePrinter()
        self._release(registry={"bad": bad, "good": good})
        assert good.disconnected == 1, "a raising close skipped the next printer"

    def test_unreadable_registry_still_releases_the_default(self) -> None:
        printer = _FakePrinter()

        def _boom() -> None:
            raise RuntimeError("registry unavailable")

        with patch.object(server, "_adapter", printer), patch.object(
            server, "_get_registry", _boom
        ):
            server._release_printer_connections()
        assert printer.disconnected == 1

    def test_no_printers_configured_is_fine(self) -> None:
        self._release()  # must not raise

    def test_wired_into_the_signal_path(self) -> None:
        """The SIGTERM path must release too, not just atexit — a host that
        kills the server is the common way a session ends."""
        released: list[str] = []
        with patch.object(
            server, "_release_printer_connections", lambda: released.append("yes")
        ):
            _run_shutdown({})
        assert released == ["yes"]
