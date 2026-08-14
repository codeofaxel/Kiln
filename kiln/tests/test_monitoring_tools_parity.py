"""Tests for monitoring tools plugin parity with server.py.

Covers:
    - Emergency latch blocks start_monitored_print (safety-critical)
    - State isolation: watchers use server.py's shared _watchers dict
    - watch_print accepts cancel_at_percent parameter
    - _PrintWatcher auto-cancels at cancel_at_percent threshold
    - _PrintWatcher camera ground-truth hashing (camera_changed flag)
    - Accessor pattern: lazy getters used instead of bare module globals

These are TDD tests — some will fail against the current plugin code and
should pass after the parity fixes are applied.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch PrinterNotFoundError into kiln.printers so that monitoring_tools
# plugin imports succeed (matches test_plugin_tools.py pattern).
# ---------------------------------------------------------------------------
import kiln.printers as _printers_pkg


@pytest.fixture(autouse=True)
def _preview_gate_off(monkeypatch):
    """These suites predate the preview-consent gate and are not about it.

    Every command that starts a print now requires a preview token (see
    test_every_door_aims.py, which is where the gate itself is tested).
    These tests exercise override merging, material detection and latch
    behaviour, so they take the same bypass CI does rather than each
    growing a token they have no opinion about.
    """
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")


if not hasattr(_printers_pkg, "PrinterNotFoundError"):
    from kiln.registry import PrinterNotFoundError as _PNFE

    _printers_pkg.PrinterNotFoundError = _PNFE  # type: ignore[attr-defined]


# ===================================================================
# Shared fixtures
# ===================================================================


@pytest.fixture()
def mock_mcp():
    """Create a mock MCP server that captures registered tools."""
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
    """Register monitoring plugin and return captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.monitoring_tools import plugin

    plugin.register(mcp)
    return tools


def _make_mock_adapter(*, state="printing", completion=50.0, can_snapshot=False):
    """Build a mock adapter with configurable state and job."""
    from kiln.printers import PrinterStatus

    adapter = MagicMock()
    mock_state = MagicMock()
    mock_state.state = PrinterStatus(state)
    mock_state.to_dict.return_value = {"state": state}
    adapter.get_state.return_value = mock_state

    mock_job = MagicMock()
    mock_job.completion = completion
    mock_job.to_dict.return_value = {
        "completion": completion,
        "print_time_seconds": 3600,
        "print_time_left_seconds": 1800,
    }
    adapter.get_job.return_value = mock_job

    mock_caps = MagicMock()
    mock_caps.can_snapshot = can_snapshot
    adapter.capabilities = mock_caps

    return adapter


# ===================================================================
# Test 1: Emergency latch blocks start_monitored_print
# ===================================================================


class TestEmergencyLatchBlocksStartMonitoredPrint:
    """Safety-critical: start_monitored_print must refuse to print when
    the emergency latch is active. The plugin copy was missing this check;
    server.py has it at line 8797."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._check_rate_limit", return_value=None)
    @patch("kiln.server._check_confirmation", return_value=None)
    @patch("kiln.server._emergency_latch_error")
    def test_estop_latched_blocks_print(
        self, mock_latch, _mock_conf, _mock_rate, _mock_auth, monitoring_tools,
    ) -> None:
        mock_latch.return_value = {
            "success": False,
            "error": {
                "code": "E_STOP_LATCHED",
                "message": "Emergency latch is active for printer 'default'.",
                "retryable": False,
            },
            "emergency_status": {"latched": True},
        }

        adapter = _make_mock_adapter(state="idle")
        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["start_monitored_print"](
                file_name="test.gcode",
            )

        assert result["error"]["code"] == "E_STOP_LATCHED"
        adapter.start_print.assert_not_called()

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._check_rate_limit", return_value=None)
    @patch("kiln.server._check_confirmation", return_value=None)
    @patch("kiln.server._emergency_latch_error", return_value=None)
    @patch("kiln.server._event_bus")
    def test_no_latch_allows_print(
        self, _mock_bus, _mock_latch, _mock_conf, _mock_rate, _mock_auth,
        monitoring_tools,
    ) -> None:
        adapter = _make_mock_adapter(state="idle")
        mock_print_result = MagicMock()
        mock_print_result.to_dict.return_value = {"status": "ok"}
        adapter.start_print.return_value = mock_print_result

        mock_monitor = MagicMock()
        mock_policy = MagicMock()
        mock_policy.to_dict.return_value = {"delay_seconds": 120}

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"), \
             patch("kiln.server.preflight_check", return_value={"ready": True}), \
             patch("kiln.server._audit"), \
             patch("kiln.server._get_heater_watchdog") as mock_hw, \
             patch("kiln.server._heater_watchdog", mock_hw.return_value), \
             patch("kiln.print_monitor.FirstLayerMonitor", return_value=mock_monitor), \
             patch("kiln.print_monitor.MonitorPolicy", return_value=mock_policy):
            result = monitoring_tools["start_monitored_print"](
                file_name="test.gcode",
            )

        assert result["success"] is True
        adapter.start_print.assert_called_once_with("test.gcode")


# ===================================================================
# Test 2: State isolation — watchers use server.py's shared dict
# ===================================================================


class TestStateIsolation:
    """Plugin tools must read/write to kiln.server._watchers, not a
    local dict. This ensures watch_print_status and stop_watch_print
    from server.py can see watchers created by the plugin."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_watch_print_creates_watcher_in_server_dict(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        import kiln.server as _srv

        adapter = _make_mock_adapter(state="printing", completion=10.0)

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"]()

        assert result["success"] is True
        watch_id = result["watch_id"]

        # The watcher should be in server.py's _watchers, not just a local dict
        assert watch_id in _srv._watchers, (
            "Watcher was not stored in kiln.server._watchers — "
            "plugin is using a local dict instead of the shared one"
        )

        # Clean up
        watcher = _srv._watchers.pop(watch_id, None)
        if watcher is not None:
            watcher.stop()

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_stop_watch_print_removes_from_server_dict(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        import kiln.server as _srv

        adapter = _make_mock_adapter(state="printing", completion=10.0)

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"]()

        watch_id = result["watch_id"]
        assert watch_id in _srv._watchers

        monitoring_tools["stop_watch_print"](watch_id)
        assert watch_id not in _srv._watchers


# ===================================================================
# Test 3: watch_print accepts cancel_at_percent
# ===================================================================


class TestWatchPrintCancelAtPercent:
    """The plugin's watch_print must accept cancel_at_percent and
    forward it to _PrintWatcher. The plugin copy was missing this
    parameter."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_cancel_at_percent_in_response(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        import kiln.server as _srv

        adapter = _make_mock_adapter(state="printing", completion=10.0)

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"](cancel_at_percent=50.0)

        assert result["success"] is True
        assert result.get("cancel_at_percent") == 50.0

        # Clean up
        wid = result["watch_id"]
        watcher = _srv._watchers.pop(wid, None)
        if watcher is not None:
            watcher.stop()

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    def test_cancel_at_percent_stored_on_watcher(
        self, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        import kiln.server as _srv

        adapter = _make_mock_adapter(state="printing", completion=10.0)

        with patch("kiln.server._get_adapter", return_value=adapter), \
             patch("kiln.server._registry"):
            result = monitoring_tools["watch_print"](cancel_at_percent=75.0)

        wid = result["watch_id"]
        watcher = _srv._watchers.get(wid)
        assert watcher is not None
        assert watcher._cancel_at_percent == 75.0

        # Clean up
        _srv._watchers.pop(wid, None)
        watcher.stop()


# ===================================================================
# Test 4: _PrintWatcher auto-cancels at cancel_at_percent
# ===================================================================


class TestPrintWatcherAutoCancel:
    """_PrintWatcher must auto-cancel the print when completion reaches
    or exceeds cancel_at_percent. Tests the server.py _PrintWatcher
    which has the correct implementation."""

    def test_auto_cancel_triggers_above_threshold(self) -> None:
        from kiln.printers.base import (
            JobProgress,
            PrinterCapabilities,
            PrinterState,
            PrinterStatus,
        )
        from kiln.server import _PrintWatcher

        adapter = MagicMock()
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING,
        )
        # First poll: 20%, second poll: 30% (above 25% threshold)
        adapter.get_job.side_effect = [
            JobProgress(completion=20.0),
            JobProgress(completion=30.0),
        ]
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)
        adapter.cancel_print.return_value = MagicMock(
            to_dict=lambda: {"status": "ok"},
        )

        watcher = _PrintWatcher(
            watch_id="test-autocancel",
            adapter=adapter,
            printer_name="test",
            poll_interval=1,
            timeout=30,
            cancel_at_percent=25.0,
        )
        watcher.start()
        watcher._thread.join(timeout=10)

        assert watcher._outcome == "auto_cancelled"
        adapter.cancel_print.assert_called_once()

    def test_cancel_at_zero_does_not_trigger(self) -> None:
        """cancel_at_percent=0 means auto-cancel is disabled.

        Verifies that a watcher with cancel_at_percent=0 does NOT call
        adapter.cancel_print(), even when completion is high. Uses the
        PAUSED terminal state to exit quickly (avoids the 30s IDLE guard).
        """
        from kiln.printers.base import (
            JobProgress,
            PrinterCapabilities,
            PrinterState,
            PrinterStatus,
        )
        from kiln.server import _PrintWatcher

        adapter = MagicMock()
        call_count = 0

        def state_side_effect():
            nonlocal call_count
            call_count += 1
            # First poll: printing. Second poll: paused (terminal state).
            if call_count > 1:
                return PrinterState(connected=True, state=PrinterStatus.PAUSED)
            return PrinterState(connected=True, state=PrinterStatus.PRINTING)

        adapter.get_state.side_effect = state_side_effect
        adapter.get_job.return_value = JobProgress(completion=100.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        watcher = _PrintWatcher(
            watch_id="test-no-cancel",
            adapter=adapter,
            printer_name="test",
            poll_interval=1,
            timeout=60,
            cancel_at_percent=0.0,
        )
        watcher.start()
        watcher._thread.join(timeout=10)

        adapter.cancel_print.assert_not_called()
        # Exits via PAUSED, not auto_cancelled
        assert watcher._outcome == "paused"


# ===================================================================
# Test 5: _PrintWatcher camera ground-truth hashing
# ===================================================================


class TestPrintWatcherCameraGroundTruth:
    """_PrintWatcher must track snapshot hashes to detect camera changes.
    The plugin copy was missing _prev_snapshot_hash and the camera_changed
    flag in snapshot dicts."""

    def test_prev_snapshot_hash_initialized_none(self) -> None:
        from kiln.server import _PrintWatcher

        adapter = MagicMock()
        watcher = _PrintWatcher(
            watch_id="hash-init",
            adapter=adapter,
            printer_name="test",
        )
        assert watcher._prev_snapshot_hash is None

    def test_camera_changed_flag_on_different_snapshots(self) -> None:
        from kiln.printers.base import (
            JobProgress,
            PrinterCapabilities,
            PrinterState,
            PrinterStatus,
        )
        from kiln.server import _PrintWatcher, _watchers

        # Two different images
        img1 = b"\x89PNG" + b"\x00" * 200
        img2 = b"\x89PNG" + b"\xff" * 200

        adapter = MagicMock()
        call_count = 0

        def state_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return PrinterState(connected=True, state=PrinterStatus.IDLE)
            return PrinterState(connected=True, state=PrinterStatus.PRINTING)

        adapter.get_state.side_effect = state_side_effect
        adapter.get_job.return_value = JobProgress(completion=50.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=True)
        adapter.get_snapshot.side_effect = [img1, img2]

        watcher = _PrintWatcher(
            watch_id="cam-test",
            adapter=adapter,
            printer_name="test",
            poll_interval=1,
            snapshot_interval=1,
            max_snapshots=2,
            timeout=30,
        )
        _watchers["cam-test"] = watcher
        watcher.start()
        watcher._thread.join(timeout=10)

        try:
            snaps = watcher._snapshots
            if len(snaps) >= 2:
                # First snapshot: no previous hash, camera_changed = False
                assert snaps[0].get("camera_changed") is False
                # Second snapshot: different image, camera_changed = True
                assert snaps[1].get("camera_changed") is True
            else:
                pytest.fail(
                    f"Expected at least 2 snapshots, got {len(snaps)}"
                )
        finally:
            _watchers.pop("cam-test", None)

    def test_same_image_camera_changed_false(self) -> None:
        from kiln.printers.base import (
            JobProgress,
            PrinterCapabilities,
            PrinterState,
            PrinterStatus,
        )
        from kiln.server import _PrintWatcher, _watchers

        # Same image twice
        img = b"\x89PNG" + b"\xAB" * 200

        adapter = MagicMock()
        call_count = 0

        def state_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return PrinterState(connected=True, state=PrinterStatus.IDLE)
            return PrinterState(connected=True, state=PrinterStatus.PRINTING)

        adapter.get_state.side_effect = state_side_effect
        adapter.get_job.return_value = JobProgress(completion=50.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=True)
        adapter.get_snapshot.side_effect = [img, img]

        watcher = _PrintWatcher(
            watch_id="cam-same",
            adapter=adapter,
            printer_name="test",
            poll_interval=1,
            snapshot_interval=1,
            max_snapshots=2,
            timeout=30,
        )
        _watchers["cam-same"] = watcher
        watcher.start()
        watcher._thread.join(timeout=10)

        try:
            snaps = watcher._snapshots
            if len(snaps) >= 2:
                # First snapshot: no previous hash
                assert snaps[0].get("camera_changed") is False
                # Second snapshot: same image, camera_changed = False
                assert snaps[1].get("camera_changed") is False
        finally:
            _watchers.pop("cam-same", None)


# ===================================================================
# Test 6: Accessor pattern — lazy getters used
# ===================================================================


class TestAccessorPattern:
    """Plugin tools must use _get_registry() (the lazy getter) instead of
    accessing _registry directly. This ensures the registry is properly
    initialized on first use."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_monitor_print_vision_uses_registry_getter(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = _make_mock_adapter()

        with patch("kiln.server._get_registry"), \
             patch("kiln.server._get_adapter", return_value=adapter):
            # Call without printer_name so it uses _get_adapter
            result = monitoring_tools["monitor_print_vision"]()

        assert result["success"] is True

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._event_bus")
    @patch("kiln.server._estimate_print_cost", return_value=None)
    def test_monitor_print_vision_with_name_uses_registry(
        self, _mock_cost, _mock_bus, _mock_auth, monitoring_tools,
    ) -> None:
        adapter = _make_mock_adapter()

        # When printer_name is given, the plugin should call
        # _registry.get(printer_name) — which currently goes through
        # _srv._registry directly. After the fix, it should use
        # _get_registry().get(printer_name).
        with patch("kiln.server._get_registry") as mock_get_reg:
            mock_get_reg.return_value.get.return_value = adapter
            monitoring_tools["monitor_print_vision"](
                printer_name="test-printer",
            )

        mock_get_reg.assert_called()
        mock_get_reg.return_value.get.assert_called_with("test-printer")
