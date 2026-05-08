"""Tests for the orphan watchdog that exits ``kiln serve`` when its
MCP host dies.

Covers:
    - No-op when ``KILN_DISABLE_ORPHAN_WATCHDOG`` is set
    - No-op when started directly under a supervisor (PPID=1)
    - Exits when ``getppid()`` transitions to 1
    - Tolerates a non-1 reparent (session leader move, not init)
    - Custom interval honored
    - ``KILN_ORPHAN_WATCHDOG_INTERVAL_S`` env override honored
"""
from __future__ import annotations

import os
import threading
import time
from unittest import mock

import pytest

from kiln.parent_watchdog import start_parent_watchdog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for(condition, timeout: float = 2.0, poll: float = 0.02) -> bool:
    """Poll ``condition()`` until truthy or timeout.  Returns the
    final result so tests can assert on it directly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return condition()


# ---------------------------------------------------------------------------
# Disabled / no-op cases
# ---------------------------------------------------------------------------


class TestDisabledCases:
    def test_returns_none_when_env_disable_set(self, monkeypatch):
        """The escape hatch users set when running under systemd /
        launchd manually — the watchdog must not even start a
        thread."""
        monkeypatch.setenv("KILN_DISABLE_ORPHAN_WATCHDOG", "1")
        thread = start_parent_watchdog()
        assert thread is None

    def test_returns_none_when_already_orphan_at_startup(self, monkeypatch):
        """Process started directly under init (PPID=1) is
        supervisor-managed; nothing to watch."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 1)
        thread = start_parent_watchdog()
        assert thread is None


# ---------------------------------------------------------------------------
# Orphan-detection behavior
# ---------------------------------------------------------------------------


class TestOrphanDetection:
    def test_calls_on_orphaned_when_parent_becomes_1(self, monkeypatch):
        """The core contract: ``getppid()`` going from non-1 → 1
        triggers the orphan callback (which the real ``kiln serve``
        wires to ``os._exit(0)``)."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)

        # Stay alive a few cycles, then orphan and stay orphaned.
        # Exhausting a fixed list would crash the daemon thread
        # silently and the test would time out, so we keep returning
        # 1 forever after the orphan moment.
        ppid_calls: list[int] = []
        def fake_getppid() -> int:
            ppid_calls.append(0)
            return 1 if len(ppid_calls) >= 3 else 99999
        orphaned = threading.Event()

        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", fake_getppid)
        start_parent_watchdog(
            interval_s=0.05,
            on_orphaned=orphaned.set,
        )
        assert _wait_for(orphaned.is_set, timeout=2.0)

    def test_keeps_running_when_parent_alive(self, monkeypatch):
        """Steady state — same parent every poll → never exits."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        orphaned = threading.Event()

        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 99999)
        start_parent_watchdog(
            interval_s=0.05,
            on_orphaned=orphaned.set,
        )
        # Give the thread plenty of poll cycles.  If it ever fires,
        # the test fails.
        time.sleep(0.4)
        assert not orphaned.is_set()

    def test_tolerates_non_init_reparent(self, monkeypatch):
        """Some shells reparent backgrounded jobs to a session
        leader (e.g. PID changes 9999 → 1234, but not to 1).  The
        process still has a real parent — the watchdog must not
        exit."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)

        # Initial ppid = 99999, then re-parented to 12345 (not init).
        ppid_calls: list[int] = []
        def fake_getppid() -> int:
            ppid_calls.append(0)
            return 99999 if len(ppid_calls) <= 2 else 12345
        orphaned = threading.Event()

        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", fake_getppid)
        start_parent_watchdog(
            interval_s=0.02,
            on_orphaned=orphaned.set,
        )
        time.sleep(0.3)
        assert not orphaned.is_set()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_thread_is_daemon(self, monkeypatch):
        """The watchdog must not prevent process shutdown — daemon
        threads die when the main thread exits."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 99999)
        thread = start_parent_watchdog(interval_s=60.0)
        assert thread is not None
        assert thread.daemon is True

    def test_thread_has_descriptive_name(self, monkeypatch):
        """``ps``/``top`` output is more useful when the thread name
        identifies the watchdog."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 99999)
        thread = start_parent_watchdog(interval_s=60.0)
        assert thread is not None
        assert "watchdog" in thread.name.lower()

    def test_env_var_interval_override(self, monkeypatch):
        """``KILN_ORPHAN_WATCHDOG_INTERVAL_S`` lets tests / advanced
        users tune the poll cadence without code changes."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setenv("KILN_ORPHAN_WATCHDOG_INTERVAL_S", "0.05")

        ppid_calls: list[int] = []
        def fake_getppid() -> int:
            ppid_calls.append(0)
            return 1 if len(ppid_calls) >= 2 else 99999
        orphaned = threading.Event()

        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", fake_getppid)
        start_parent_watchdog(on_orphaned=orphaned.set)
        # If the env override took effect (~50ms), this fires within
        # a few hundred ms; if it didn't (default 30s), this test
        # would hang on the wait_for default.
        assert _wait_for(orphaned.is_set, timeout=2.0)

    def test_garbage_env_interval_falls_back_to_default(self, monkeypatch):
        """A garbage env value must not crash the process at
        startup — fall back to the safe default."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setenv("KILN_ORPHAN_WATCHDOG_INTERVAL_S", "not-a-number")
        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 99999)
        thread = start_parent_watchdog()
        assert thread is not None  # didn't blow up

    def test_interval_floor_of_one_second(self, monkeypatch):
        """A 0 or negative interval would spin the CPU.  Silently
        clamp to 1s — the user doesn't need to know they tried to
        misconfigure."""
        monkeypatch.delenv("KILN_DISABLE_ORPHAN_WATCHDOG", raising=False)
        monkeypatch.setattr("kiln.parent_watchdog.os.getppid", lambda: 99999)
        with mock.patch("kiln.parent_watchdog.time.sleep") as fake_sleep:
            start_parent_watchdog(interval_s=0)
            # Give the daemon a beat to enter the loop and call
            # sleep() once.
            time.sleep(0.1)
            assert fake_sleep.call_count >= 1
            assert fake_sleep.call_args_list[0].args[0] >= 1.0
