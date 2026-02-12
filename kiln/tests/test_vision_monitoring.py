"""Tests for vision monitoring foundation.

Covers event types, capability flags, phase detection logic, and
the building blocks for monitor_print_vision and watch_print tools.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kiln.events import Event, EventType
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.server import _detect_phase, _PHASE_HINTS


class TestVisionEventTypes:
    """Verify new vision-related event types exist."""

    def test_vision_check_exists(self) -> None:
        assert EventType.VISION_CHECK.value == "vision.check"

    def test_vision_alert_exists(self) -> None:
        assert EventType.VISION_ALERT.value == "vision.alert"

    def test_vision_check_creates_event(self) -> None:
        event = Event(
            type=EventType.VISION_CHECK,
            data={"printer_name": "voron", "completion": 45.0},
            source="vision",
        )
        d = event.to_dict()
        assert d["type"] == "vision.check"
        assert d["data"]["printer_name"] == "voron"

    def test_vision_alert_creates_event(self) -> None:
        event = Event(
            type=EventType.VISION_ALERT,
            data={"printer_name": "voron", "action": "pause", "reason": "spaghetti detected"},
            source="vision",
        )
        d = event.to_dict()
        assert d["type"] == "vision.alert"


class TestSnapshotCapability:
    """Verify can_snapshot capability flag on adapters."""

    def test_default_is_false(self) -> None:
        caps = PrinterCapabilities()
        assert caps.can_snapshot is False

    def test_octoprint_has_snapshot(self) -> None:
        from kiln.printers.octoprint import OctoPrintAdapter
        adapter = OctoPrintAdapter(host="http://test", api_key="key")
        assert adapter.capabilities.can_snapshot is True

    def test_moonraker_has_snapshot(self) -> None:
        from kiln.printers.moonraker import MoonrakerAdapter
        adapter = MoonrakerAdapter(host="http://test")
        assert adapter.capabilities.can_snapshot is True

    def test_bambu_no_snapshot_without_ffmpeg(self) -> None:
        try:
            from kiln.printers.bambu import BambuAdapter
        except ImportError:
            pytest.skip("paho-mqtt not installed")
        with mock.patch("kiln.printers.bambu._find_ffmpeg", return_value=None):
            adapter = BambuAdapter(host="192.168.1.1", access_code="12345678", serial="SN123")
            assert adapter.capabilities.can_snapshot is False

    def test_bambu_has_snapshot_with_ffmpeg(self) -> None:
        try:
            from kiln.printers.bambu import BambuAdapter
        except ImportError:
            pytest.skip("paho-mqtt not installed")
        with mock.patch("kiln.printers.bambu._find_ffmpeg", return_value="/usr/bin/ffmpeg"):
            adapter = BambuAdapter(host="192.168.1.1", access_code="12345678", serial="SN123")
            assert adapter.capabilities.can_snapshot is True

    def test_capability_in_to_dict(self) -> None:
        caps = PrinterCapabilities(can_snapshot=True)
        d = caps.to_dict()
        assert d["can_snapshot"] is True


# -- Phase detection helpers (used by monitor_print_vision) ----------------

def _failure_hints(phase: str) -> list[str]:
    """Get failure hints from the actual server implementation."""
    return _PHASE_HINTS.get(phase, _PHASE_HINTS["unknown"])


class TestPhaseDetection:
    """Test print phase classification from completion percentage."""

    def test_none_completion(self) -> None:
        assert _detect_phase(None) == "unknown"

    def test_negative_completion(self) -> None:
        assert _detect_phase(-1.0) == "unknown"

    def test_zero_percent(self) -> None:
        assert _detect_phase(0.0) == "first_layers"

    def test_five_percent(self) -> None:
        assert _detect_phase(5.0) == "first_layers"

    def test_nine_percent(self) -> None:
        assert _detect_phase(9.9) == "first_layers"

    def test_ten_percent(self) -> None:
        assert _detect_phase(10.0) == "mid_print"

    def test_fifty_percent(self) -> None:
        assert _detect_phase(50.0) == "mid_print"

    def test_ninety_percent(self) -> None:
        assert _detect_phase(90.0) == "mid_print"

    def test_ninety_one_percent(self) -> None:
        assert _detect_phase(91.0) == "final_layers"

    def test_hundred_percent(self) -> None:
        assert _detect_phase(100.0) == "final_layers"


class TestFailureHints:
    def test_first_layers_mentions_adhesion(self) -> None:
        hints = _failure_hints("first_layers")
        assert any("adhesion" in h.lower() for h in hints)

    def test_mid_print_mentions_spaghetti(self) -> None:
        hints = _failure_hints("mid_print")
        assert any("spaghetti" in h.lower() for h in hints)

    def test_final_layers_mentions_cooling(self) -> None:
        hints = _failure_hints("final_layers")
        assert any("cooling" in h.lower() for h in hints)

    def test_unknown_has_hints(self) -> None:
        hints = _failure_hints("unknown")
        assert len(hints) >= 1

    def test_each_phase_has_multiple_hints(self) -> None:
        for phase in ("first_layers", "mid_print", "final_layers"):
            assert len(_failure_hints(phase)) >= 2


class TestVisionMonitoringData:
    """Test that vision monitoring data structures can be assembled correctly."""

    def test_snapshot_context_assembly(self) -> None:
        """Verify the monitoring context dict can be built from printer data."""
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            tool_temp_actual=210.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )
        job = JobProgress(
            file_name="benchy.gcode",
            completion=45.0,
            print_time_seconds=1800,
            print_time_left_seconds=2200,
        )
        phase = _detect_phase(job.completion)
        hints = _failure_hints(phase)

        context = {
            "printer_state": state.to_dict(),
            "job_progress": job.to_dict(),
            "monitoring_context": {
                "completion_percent": job.completion,
                "print_phase": phase,
                "failure_hints": hints,
            },
        }
        assert context["monitoring_context"]["print_phase"] == "mid_print"
        assert len(context["monitoring_context"]["failure_hints"]) >= 2
        assert context["printer_state"]["state"] == "printing"

    def test_idle_printer_context(self) -> None:
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)
        job = JobProgress()
        phase = _detect_phase(job.completion)
        assert phase == "unknown"

    def test_error_printer_context(self) -> None:
        state = PrinterState(connected=True, state=PrinterStatus.ERROR)
        assert state.to_dict()["state"] == "error"


class TestMonitorPrintVisionTool:
    """Integration tests for the monitor_print_vision MCP tool."""

    def test_idle_printer_includes_not_printing_flag(self) -> None:
        """monitor_print_vision should flag when printer is not printing."""
        from kiln.server import monitor_print_vision, _registry, _event_bus
        from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus, JobProgress, PrinterCapabilities

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
        adapter.get_job.return_value = JobProgress()
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        with mock.patch.object(_registry, 'get', return_value=adapter):
            result = monitor_print_vision(printer_name="test")
        assert result["success"] is True
        assert result["monitoring_context"]["is_printing"] is False

    def test_printing_printer_has_is_printing_true(self) -> None:
        from kiln.server import monitor_print_vision, _registry
        from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus, JobProgress, PrinterCapabilities

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        adapter.get_job.return_value = JobProgress(completion=45.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        with mock.patch.object(_registry, 'get', return_value=adapter):
            result = monitor_print_vision(printer_name="test")
        assert result["success"] is True
        assert result["monitoring_context"]["is_printing"] is True

    def test_snapshot_skipped_when_no_capability(self) -> None:
        from kiln.server import monitor_print_vision, _registry
        from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus, JobProgress, PrinterCapabilities

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        adapter.get_job.return_value = JobProgress(completion=50.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        with mock.patch.object(_registry, 'get', return_value=adapter):
            result = monitor_print_vision(printer_name="test", include_snapshot=True)
        assert result["snapshot"]["available"] is False
        assert result["snapshot"].get("reason") == "no_capability"
        adapter.get_snapshot.assert_not_called()


class TestWatchPrintTool:
    """Integration tests for watch_print edge cases."""

    def test_paused_printer_returns_paused_outcome(self) -> None:
        from kiln.server import watch_print, watch_print_status, _registry, _watchers
        from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus, JobProgress, PrinterCapabilities

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PAUSED)
        adapter.get_job.return_value = JobProgress(completion=50.0)
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        with mock.patch.object(_registry, 'get', return_value=adapter):
            result = watch_print(printer_name="test", poll_interval=1, timeout=60)
        assert result["success"] is True
        watch_id = result["watch_id"]
        # Wait for the background thread to finish (adapter returns PAUSED immediately)
        watcher = _watchers.get(watch_id)
        assert watcher is not None
        if watcher._thread is not None:
            watcher._thread.join(timeout=5)
        status = watch_print_status(watch_id)
        assert status["outcome"] == "paused"

    def test_idle_with_no_active_job_returns_no_active_print(self) -> None:
        from kiln.server import watch_print, _registry
        from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus, JobProgress, PrinterCapabilities

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
        adapter.get_job.return_value = JobProgress()  # No active job (completion is None)
        adapter.capabilities = PrinterCapabilities(can_snapshot=False)

        with mock.patch.object(_registry, 'get', return_value=adapter):
            result = watch_print(printer_name="test")
        assert result["outcome"] == "no_active_print"


# ---------------------------------------------------------------------------
# watch_print_status
# ---------------------------------------------------------------------------


class TestWatchPrintStatusTool:
    """Integration tests for the watch_print_status MCP tool."""

    def test_returns_status_for_active_watcher(self) -> None:
        from kiln.server import watch_print_status, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-active",
            adapter=adapter,
            printer_name="test-printer",
        )
        watcher._start_time = time.time()
        _watchers["w-active"] = watcher
        try:
            with mock.patch("kiln.server._check_auth", return_value=None):
                result = watch_print_status(watch_id="w-active")
            assert result["success"] is True
            assert result["watch_id"] == "w-active"
            assert result["printer_name"] == "test-printer"
            assert result["outcome"] == "running"
            assert isinstance(result["elapsed_seconds"], float)
        finally:
            _watchers.pop("w-active", None)

    def test_not_found_returns_error(self) -> None:
        from kiln.server import watch_print_status

        with mock.patch("kiln.server._check_auth", return_value=None):
            result = watch_print_status(watch_id="w-nonexistent")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "w-nonexistent" in result["error"]["message"]

    def test_auth_rejection(self) -> None:
        from kiln.server import watch_print_status

        auth_err = {"success": False, "error": {"code": "AUTH", "message": "denied"}}
        with mock.patch("kiln.server._check_auth", return_value=auth_err):
            result = watch_print_status(watch_id="w-any")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH"

    def test_finished_watcher_shows_result(self) -> None:
        from kiln.server import watch_print_status, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-done",
            adapter=adapter,
            printer_name="done-printer",
        )
        watcher._start_time = time.time() - 120.0
        # Simulate a finished watcher by setting its internal result
        watcher._result = {
            "success": True,
            "watch_id": "w-done",
            "outcome": "completed",
            "elapsed_seconds": 120.0,
            "progress_log": [],
            "snapshots": [],
            "snapshot_failures": 0,
        }
        watcher._outcome = "completed"
        _watchers["w-done"] = watcher
        try:
            with mock.patch("kiln.server._check_auth", return_value=None):
                result = watch_print_status(watch_id="w-done")
            assert result["success"] is True
            assert result["finished"] is True
            assert result["outcome"] == "completed"
            assert result["result"]["outcome"] == "completed"
        finally:
            _watchers.pop("w-done", None)

    def test_status_includes_snapshot_counts(self) -> None:
        from kiln.server import watch_print_status, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-snaps",
            adapter=adapter,
            printer_name="snap-printer",
        )
        watcher._start_time = time.time()
        watcher._snapshots = [{"ts": 1.0}, {"ts": 2.0}]
        watcher._snapshot_failures = 3
        _watchers["w-snaps"] = watcher
        try:
            with mock.patch("kiln.server._check_auth", return_value=None):
                result = watch_print_status(watch_id="w-snaps")
            assert result["success"] is True
            assert result["snapshots_collected"] == 2
            assert result["snapshot_failures"] == 3
        finally:
            _watchers.pop("w-snaps", None)


# ---------------------------------------------------------------------------
# stop_watch_print
# ---------------------------------------------------------------------------


class TestStopWatchPrintTool:
    """Integration tests for the stop_watch_print MCP tool."""

    def test_stops_active_watcher(self) -> None:
        from kiln.server import stop_watch_print, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-stop",
            adapter=adapter,
            printer_name="stop-printer",
        )
        watcher._start_time = time.time()
        _watchers["w-stop"] = watcher
        with mock.patch("kiln.server._check_auth", return_value=None):
            result = stop_watch_print(watch_id="w-stop")
        assert result["success"] is True
        assert result["outcome"] == "stopped"
        assert "w-stop" not in _watchers

    def test_not_found_returns_error(self) -> None:
        from kiln.server import stop_watch_print

        with mock.patch("kiln.server._check_auth", return_value=None):
            result = stop_watch_print(watch_id="w-gone")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "w-gone" in result["error"]["message"]

    def test_auth_rejection(self) -> None:
        from kiln.server import stop_watch_print

        auth_err = {"success": False, "error": {"code": "AUTH", "message": "denied"}}
        with mock.patch("kiln.server._check_auth", return_value=auth_err):
            result = stop_watch_print(watch_id="w-any")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH"

    def test_removes_watcher_from_registry(self) -> None:
        from kiln.server import stop_watch_print, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-remove",
            adapter=adapter,
            printer_name="remove-printer",
        )
        watcher._start_time = time.time()
        _watchers["w-remove"] = watcher

        with mock.patch("kiln.server._check_auth", return_value=None):
            stop_watch_print(watch_id="w-remove")

        assert "w-remove" not in _watchers
        # Calling again should return NOT_FOUND
        with mock.patch("kiln.server._check_auth", return_value=None):
            result = stop_watch_print(watch_id="w-remove")
        assert result["error"]["code"] == "NOT_FOUND"

    def test_stop_returns_final_result_if_already_finished(self) -> None:
        from kiln.server import stop_watch_print, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-finished",
            adapter=adapter,
            printer_name="finished-printer",
        )
        watcher._start_time = time.time() - 300.0
        watcher._result = {
            "success": True,
            "watch_id": "w-finished",
            "outcome": "completed",
            "elapsed_seconds": 300.0,
            "progress_log": [{"pct": 100.0}],
            "snapshots": [],
            "snapshot_failures": 0,
        }
        watcher._outcome = "completed"
        _watchers["w-finished"] = watcher

        with mock.patch("kiln.server._check_auth", return_value=None):
            result = stop_watch_print(watch_id="w-finished")

        assert result["success"] is True
        # When result was already set, stop() returns it
        assert result["outcome"] == "completed"
        assert "w-finished" not in _watchers

    def test_stop_includes_progress_and_snapshots(self) -> None:
        from kiln.server import stop_watch_print, _watchers, _PrintWatcher
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock(spec=PrinterAdapter)
        watcher = _PrintWatcher(
            watch_id="w-data",
            adapter=adapter,
            printer_name="data-printer",
        )
        watcher._start_time = time.time()
        watcher._progress_log = [{"pct": 10.0}, {"pct": 20.0}]
        watcher._snapshots = [{"ts": 1.0, "url": "http://snap/1"}]
        watcher._snapshot_failures = 1
        _watchers["w-data"] = watcher

        with mock.patch("kiln.server._check_auth", return_value=None):
            result = stop_watch_print(watch_id="w-data")

        assert result["success"] is True
        assert len(result["progress_log"]) == 2
        assert len(result["snapshots"]) == 1
        assert result["snapshot_failures"] == 1
