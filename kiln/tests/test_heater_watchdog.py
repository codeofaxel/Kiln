"""Tests for the HeaterWatchdog background service.

Covers:
    - Lifecycle (start/stop, disabled when timeout=0)
    - Notification tracking (heater set, print start/end)
    - Auto-cooldown when idle timeout expires
    - No cooldown during active prints
    - No cooldown when heaters already off
    - Event bus emission on cooldown
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from kiln.heater_watchdog import HeaterWatchdog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeState:
    """Minimal printer state for testing."""
    tool_temp_target: Optional[float] = 0
    bed_temp_target: Optional[float] = 0


class FakeAdapter:
    """Minimal adapter that records cooldown calls."""

    def __init__(
        self,
        tool_target: float = 0,
        bed_target: float = 0,
    ) -> None:
        self.state = FakeState(
            tool_temp_target=tool_target,
            bed_temp_target=bed_target,
        )
        self.set_tool_calls: List[float] = []
        self.set_bed_calls: List[float] = []

    def get_state(self):
        return self.state

    def set_tool_temp(self, temp: float):
        self.set_tool_calls.append(temp)
        self.state.tool_temp_target = temp
        return True

    def set_bed_temp(self, temp: float):
        self.set_bed_calls.append(temp)
        self.state.bed_temp_target = temp
        return True


def _make_watchdog(
    adapter: Optional[FakeAdapter] = None,
    timeout_minutes: float = 0.01,  # very short for tests (~0.6s)
    poll_interval: float = 0.1,
    event_bus=None,
) -> HeaterWatchdog:
    if adapter is None:
        adapter = FakeAdapter()
    return HeaterWatchdog(
        get_adapter=lambda: adapter,
        timeout_minutes=timeout_minutes,
        poll_interval=poll_interval,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_start_stop(self) -> None:
        wd = _make_watchdog()
        wd.start()
        assert wd.is_running
        wd.stop()
        assert not wd.is_running

    def test_disabled_when_timeout_zero(self) -> None:
        wd = _make_watchdog(timeout_minutes=0)
        wd.start()
        # Should not actually start the thread.
        assert not wd.is_running

    def test_double_start_is_noop(self) -> None:
        wd = _make_watchdog()
        wd.start()
        thread1 = wd._thread
        wd.start()  # second call
        assert wd._thread is thread1
        wd.stop()

    def test_stop_without_start_is_safe(self) -> None:
        wd = _make_watchdog()
        wd.stop()  # should not raise


# ---------------------------------------------------------------------------
# Notification tracking
# ---------------------------------------------------------------------------

class TestNotifications:

    def test_notify_heater_set_updates_timestamp(self) -> None:
        wd = _make_watchdog()
        assert wd._last_heater_activity is None
        wd.notify_heater_set()
        assert wd._last_heater_activity is not None

    def test_notify_print_started_sets_flag(self) -> None:
        wd = _make_watchdog()
        assert not wd._print_active
        wd.notify_print_started()
        assert wd._print_active
        assert wd._last_heater_activity is not None

    def test_notify_print_ended_clears_flag(self) -> None:
        wd = _make_watchdog()
        wd.notify_print_started()
        wd.notify_print_ended()
        assert not wd._print_active
        # Timestamp should be refreshed (grace period for user inspection)
        assert wd._last_heater_activity is not None


# ---------------------------------------------------------------------------
# Auto-cooldown logic (_tick)
# ---------------------------------------------------------------------------

class TestCooldown:

    def test_cooldown_when_idle_timeout_expired(self) -> None:
        """Heaters on + idle + timeout expired = cooldown."""
        adapter = FakeAdapter(tool_target=210, bed_target=60)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)

        # Simulate heater set in the past.
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert adapter.set_tool_calls == [0]
        assert adapter.set_bed_calls == [0]
        # Tracker should be cleared.
        assert wd._last_heater_activity is None

    def test_no_cooldown_during_active_print(self) -> None:
        """Even if timeout expired, don't touch heaters during a print."""
        adapter = FakeAdapter(tool_target=210, bed_target=60)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)

        wd._last_heater_activity = time.monotonic() - 100
        wd._print_active = True

        wd._tick()

        assert adapter.set_tool_calls == []
        assert adapter.set_bed_calls == []

    def test_no_cooldown_when_heaters_already_off(self) -> None:
        """Timeout expired but heaters at 0 — just clear the tracker."""
        adapter = FakeAdapter(tool_target=0, bed_target=0)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)

        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert adapter.set_tool_calls == []
        assert adapter.set_bed_calls == []
        assert wd._last_heater_activity is None

    def test_no_cooldown_before_timeout(self) -> None:
        """Heaters on but timeout not expired yet — do nothing."""
        adapter = FakeAdapter(tool_target=210, bed_target=60)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=999)

        wd.notify_heater_set()

        wd._tick()

        assert adapter.set_tool_calls == []
        assert adapter.set_bed_calls == []

    def test_no_cooldown_when_no_activity_tracked(self) -> None:
        """No heater activity tracked — do nothing."""
        adapter = FakeAdapter(tool_target=210, bed_target=60)
        wd = _make_watchdog(adapter=adapter)

        wd._tick()

        assert adapter.set_tool_calls == []
        assert adapter.set_bed_calls == []

    def test_cooldown_only_tool(self) -> None:
        """Only tool heater on — only cool tool."""
        adapter = FakeAdapter(tool_target=210, bed_target=0)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert adapter.set_tool_calls == [0]
        assert adapter.set_bed_calls == []

    def test_cooldown_only_bed(self) -> None:
        """Only bed heater on — only cool bed."""
        adapter = FakeAdapter(tool_target=0, bed_target=60)
        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert adapter.set_tool_calls == []
        assert adapter.set_bed_calls == [0]

    def test_adapter_error_does_not_crash(self) -> None:
        """If get_state raises, tick should not crash."""
        def bad_adapter():
            raise RuntimeError("offline")

        wd = _make_watchdog(timeout_minutes=0.0001)
        wd._get_adapter = bad_adapter
        wd._last_heater_activity = time.monotonic() - 100

        # Should not raise.
        wd._tick()

    def test_cooldown_adapter_error_does_not_crash(self) -> None:
        """If set_tool_temp raises, tick should not crash."""
        adapter = FakeAdapter(tool_target=210, bed_target=0)
        adapter.set_tool_temp = MagicMock(side_effect=RuntimeError("fail"))

        wd = _make_watchdog(adapter=adapter, timeout_minutes=0.0001)
        wd._last_heater_activity = time.monotonic() - 100

        # Should not raise.
        wd._tick()


# ---------------------------------------------------------------------------
# Event bus emission
# ---------------------------------------------------------------------------

class TestEventBus:

    def test_emits_event_on_cooldown(self) -> None:
        adapter = FakeAdapter(tool_target=210, bed_target=60)
        event_bus = MagicMock()
        wd = _make_watchdog(
            adapter=adapter,
            timeout_minutes=0.0001,
            event_bus=event_bus,
        )
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert event_bus.publish.called
        call_args = event_bus.publish.call_args
        # First positional arg is the event type.
        from kiln.events import EventType
        assert call_args[0][0] == EventType.TEMPERATURE_WARNING
        payload = call_args[0][1]
        assert payload["source"] == "heater_watchdog"
        assert payload["action"] == "auto_cooldown"
        assert len(payload["cooled"]) == 2

    def test_no_event_when_no_cooldown(self) -> None:
        adapter = FakeAdapter(tool_target=0, bed_target=0)
        event_bus = MagicMock()
        wd = _make_watchdog(
            adapter=adapter,
            timeout_minutes=0.0001,
            event_bus=event_bus,
        )
        wd._last_heater_activity = time.monotonic() - 100

        wd._tick()

        assert not event_bus.publish.called


# ---------------------------------------------------------------------------
# Integration: background thread actually fires
# ---------------------------------------------------------------------------

class TestIntegration:

    def test_background_thread_fires_cooldown(self) -> None:
        """Start the watchdog and verify it actually cools down via the thread."""
        adapter = FakeAdapter(tool_target=200, bed_target=60)
        wd = _make_watchdog(
            adapter=adapter,
            timeout_minutes=0.0001,  # ~6ms
            poll_interval=0.05,
        )

        wd.notify_heater_set()
        wd.start()

        # Wait for the background thread to fire.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if adapter.set_tool_calls:
                break
            time.sleep(0.05)

        wd.stop()

        assert adapter.set_tool_calls == [0]
        assert adapter.set_bed_calls == [0]

    def test_timeout_minutes_property(self) -> None:
        wd = _make_watchdog(timeout_minutes=45)
        assert wd.timeout_minutes == 45.0
