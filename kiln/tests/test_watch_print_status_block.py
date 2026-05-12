"""Tests for the block_until_event subscribe mode on watch_print_status.

Covers the long-poll pattern added to monitoring_tools.py: agent calls
``watch_print_status(watch_id, block_until_event=True, timeout=N)``, the
tool subscribes to the event bus, blocks server-side until a matching
event fires (or timeout), then returns the event payload alongside the
watcher's current status.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# Patch PrinterNotFoundError into kiln.printers so the monitoring_tools
# plugin imports succeed (mirrors test_plugin_tools.py / test_monitoring_tools_parity.py).
import kiln.printers as _printers_pkg

if not hasattr(_printers_pkg, "PrinterNotFoundError"):
    from kiln.registry import PrinterNotFoundError as _PNFE

    _printers_pkg.PrinterNotFoundError = _PNFE  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_mcp():
    """Mock MCP server that captures registered tools by name."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    return MockMCP(), tools


@pytest.fixture()
def monitoring_tools(mock_mcp):
    """Register the monitoring plugin and return the tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.monitoring_tools import plugin

    plugin.register(mcp)
    return tools


@pytest.fixture()
def watcher_in_registry():
    """Yield a freshly-constructed _PrintWatcher registered in _watchers.

    Cleans up after the test even if it raised.
    """
    import kiln.server as _srv
    from kiln.plugins.monitoring_tools import _PrintWatcher

    created: list[str] = []

    def _make(watch_id: str, printer_name: str = "test-printer"):
        adapter = MagicMock()
        watcher = _PrintWatcher(
            watch_id=watch_id,
            adapter=adapter,
            printer_name=printer_name,
        )
        watcher._start_time = time.time()
        _srv._watchers[watch_id] = watcher
        created.append(watch_id)
        return watcher

    yield _make

    for wid in created:
        watcher = _srv._watchers.pop(wid, None)
        if watcher is not None and watcher._thread is not None:
            try:
                watcher.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNonBlockingDefaultPreserved:
    """The default (block_until_event=False) must behave exactly as today."""

    def test_returns_immediately_without_event_keys(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        watcher_in_registry("w-default", printer_name="printer-A")

        with patch("kiln.server._check_auth", return_value=None):
            result = monitoring_tools["watch_print_status"](watch_id="w-default")

        assert result["success"] is True
        assert result["watch_id"] == "w-default"
        assert result["printer_name"] == "printer-A"
        # No blocking-mode keys should appear on the default path.
        assert "events_received" not in result
        assert "timed_out" not in result
        assert "watcher_already_finished" not in result


class TestBlockUntilEventReceives:
    """block_until_event=True wakes up on matching published event."""

    def test_wakes_on_vision_alert_for_same_printer(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        import kiln.server as _srv
        from kiln.events import EventType

        watcher_in_registry("w-wake-1", printer_name="printer-B")
        bus = _srv._get_event_bus()

        def publish_later() -> None:
            time.sleep(0.1)
            bus.publish(
                EventType.VISION_ALERT,
                {"printer_name": "printer-B", "alert_type": "stall"},
                source="test",
            )

        t = threading.Thread(target=publish_later, daemon=True)
        t.start()
        try:
            with patch("kiln.server._check_auth", return_value=None):
                result = monitoring_tools["watch_print_status"](
                    watch_id="w-wake-1",
                    block_until_event=True,
                    timeout=3,
                )
        finally:
            t.join(timeout=2)

        assert result["success"] is True
        assert "events_received" in result
        assert len(result["events_received"]) >= 1
        first = result["events_received"][0]
        assert first["type"] == "vision.alert"
        assert first["data"]["printer_name"] == "printer-B"
        assert first["data"]["alert_type"] == "stall"
        # Status block should still be merged in.
        assert result["watch_id"] == "w-wake-1"

    def test_wakes_on_print_terminal_with_matching_watch_id(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        import kiln.server as _srv
        from kiln.events import EventType

        watcher_in_registry("w-wake-2", printer_name="printer-C")
        bus = _srv._get_event_bus()

        def publish_later() -> None:
            time.sleep(0.1)
            bus.publish(
                EventType.PRINT_TERMINAL,
                {
                    "watch_id": "w-wake-2",
                    "printer_name": "printer-C",
                    "outcome": "completed",
                },
                source="test",
            )

        t = threading.Thread(target=publish_later, daemon=True)
        t.start()
        try:
            with patch("kiln.server._check_auth", return_value=None):
                result = monitoring_tools["watch_print_status"](
                    watch_id="w-wake-2",
                    block_until_event=True,
                    timeout=3,
                )
        finally:
            t.join(timeout=2)

        assert result["success"] is True
        assert len(result["events_received"]) >= 1
        assert result["events_received"][0]["type"] == "print.terminal"
        assert result["events_received"][0]["data"]["outcome"] == "completed"


class TestBlockUntilEventTimesOut:
    """block_until_event=True returns clean timed_out when nothing fires."""

    def test_returns_timed_out_with_status_block(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        watcher_in_registry("w-timeout", printer_name="printer-D")

        start = time.time()
        with patch("kiln.server._check_auth", return_value=None):
            result = monitoring_tools["watch_print_status"](
                watch_id="w-timeout",
                block_until_event=True,
                timeout=1,
            )
        elapsed = time.time() - start

        assert result["success"] is True
        assert result.get("timed_out") is True
        assert result.get("timeout_seconds") == 1
        assert result["events_received"] == []
        # Sanity: should have actually blocked roughly the timeout interval.
        assert 0.8 <= elapsed <= 2.5
        # Status block should be present.
        assert result["watch_id"] == "w-timeout"


class TestPrinterNameFiltering:
    """Events for a different printer must not wake the subscriber."""

    def test_event_for_other_printer_is_ignored(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        import kiln.server as _srv
        from kiln.events import EventType

        watcher_in_registry("w-filter", printer_name="printer-E")
        bus = _srv._get_event_bus()

        def publish_unrelated() -> None:
            time.sleep(0.1)
            # Wrong printer — should be filtered out.
            bus.publish(
                EventType.VISION_ALERT,
                {"printer_name": "printer-NOT-E", "alert_type": "stall"},
                source="test",
            )

        t = threading.Thread(target=publish_unrelated, daemon=True)
        t.start()
        try:
            with patch("kiln.server._check_auth", return_value=None):
                result = monitoring_tools["watch_print_status"](
                    watch_id="w-filter",
                    block_until_event=True,
                    timeout=1,
                )
        finally:
            t.join(timeout=2)

        # Should have timed out — the published event was for a different printer.
        assert result["success"] is True
        assert result.get("timed_out") is True
        assert result["events_received"] == []


class TestAlreadyFinishedShortCircuit:
    """A watcher that has already finished returns immediately, no wait."""

    def test_finished_watcher_returns_without_blocking(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        watcher = watcher_in_registry("w-done", printer_name="printer-F")
        watcher._result = {
            "success": True,
            "watch_id": "w-done",
            "outcome": "completed",
            "elapsed_seconds": 30.0,
            "progress_log": [],
            "snapshots": [],
            "snapshot_failures": 0,
        }
        watcher._outcome = "completed"

        start = time.time()
        with patch("kiln.server._check_auth", return_value=None):
            result = monitoring_tools["watch_print_status"](
                watch_id="w-done",
                block_until_event=True,
                timeout=10,
            )
        elapsed = time.time() - start

        assert result["success"] is True
        assert result.get("watcher_already_finished") is True
        # Must not have blocked anywhere close to the 10s timeout.
        assert elapsed < 1.0


class TestEventTypeValidation:
    """Unknown event_types should return a structured validation error."""

    def test_unknown_event_type_rejected(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        watcher_in_registry("w-bad-evt", printer_name="printer-G")

        with patch("kiln.server._check_auth", return_value=None):
            result = monitoring_tools["watch_print_status"](
                watch_id="w-bad-evt",
                block_until_event=True,
                timeout=1,
                event_types=["totally.fake.event"],
            )

        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "totally.fake.event" in result["error"]["message"]


class TestCustomEventTypes:
    """Custom event_types filter wakes on the requested type and only that."""

    def test_custom_event_types_filter_to_vision_check(
        self, monitoring_tools, watcher_in_registry,
    ) -> None:
        import kiln.server as _srv
        from kiln.events import EventType

        watcher_in_registry("w-custom", printer_name="printer-H")
        bus = _srv._get_event_bus()

        def publish_both() -> None:
            time.sleep(0.1)
            # vision.alert should NOT wake us (not in event_types).
            bus.publish(
                EventType.VISION_ALERT,
                {"printer_name": "printer-H", "alert_type": "stall"},
                source="test",
            )
            time.sleep(0.05)
            # vision.check IS in event_types — should wake us.
            bus.publish(
                EventType.VISION_CHECK,
                {"printer_name": "printer-H", "phase": "mid_print"},
                source="test",
            )

        t = threading.Thread(target=publish_both, daemon=True)
        t.start()
        try:
            with patch("kiln.server._check_auth", return_value=None):
                result = monitoring_tools["watch_print_status"](
                    watch_id="w-custom",
                    block_until_event=True,
                    timeout=3,
                    event_types=["vision.check"],
                )
        finally:
            t.join(timeout=2)

        assert result["success"] is True
        assert len(result["events_received"]) >= 1
        # The wake-up event should be vision.check, not vision.alert.
        types_received = {e["type"] for e in result["events_received"]}
        assert "vision.check" in types_received
        assert "vision.alert" not in types_received


class TestNotFoundStillRaises:
    """Unknown watch_id returns NOT_FOUND even when block_until_event=True."""

    def test_missing_watcher_with_block_param(self, monitoring_tools) -> None:
        with patch("kiln.server._check_auth", return_value=None):
            result = monitoring_tools["watch_print_status"](
                watch_id="w-ghost",
                block_until_event=True,
                timeout=1,
            )

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
