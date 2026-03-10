"""Tests for the print safety monitor (kiln monitor)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiln.print_safety_monitor import (
    Alert,
    MonitorConfig,
    PrintSafetyMonitor,
    _TempSample,
)
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    state: PrinterStatus = PrinterStatus.PRINTING,
    connected: bool = True,
    tool_temp_actual: float | None = 220.0,
    tool_temp_target: float | None = 220.0,
    bed_temp_actual: float | None = 65.0,
    bed_temp_target: float | None = 65.0,
    completion: float | None = 42.0,
    current_layer: int | None = 83,
    total_layers: int | None = 174,
    file_name: str | None = "test.gcode",
    print_error: int | None = None,
    wifi_signal: str | None = None,
    can_snapshot: bool = False,
) -> MagicMock:
    """Create a mock PrinterAdapter with sensible defaults."""
    adapter = MagicMock()
    adapter.capabilities = PrinterCapabilities(can_snapshot=can_snapshot)
    adapter.get_state.return_value = PrinterState(
        connected=connected,
        state=state,
        tool_temp_actual=tool_temp_actual,
        tool_temp_target=tool_temp_target,
        bed_temp_actual=bed_temp_actual,
        bed_temp_target=bed_temp_target,
        print_error=print_error,
        wifi_signal=wifi_signal,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=file_name,
        completion=completion,
        current_layer=current_layer,
        total_layers=total_layers,
    )
    return adapter


def _make_monitor(adapter, *, json_mode: bool = True, **config_kwargs) -> PrintSafetyMonitor:
    """Create a PrintSafetyMonitor with default config."""
    config = MonitorConfig(poll_interval=1.0, snapshot_interval=0, **config_kwargs)
    return PrintSafetyMonitor(
        adapter=adapter,
        printer_name="test-printer",
        config=config,
        json_mode=json_mode,
    )


# ---------------------------------------------------------------------------
# MonitorConfig tests
# ---------------------------------------------------------------------------


class TestMonitorConfig:
    def test_defaults(self):
        config = MonitorConfig()
        assert config.poll_interval == 10.0
        assert config.snapshot_interval == 300
        assert config.auto_pause is True
        assert config.auto_cancel is False
        assert config.timeout == 0.0

    def test_to_dict(self):
        config = MonitorConfig(poll_interval=5.0, auto_cancel=True)
        d = config.to_dict()
        assert d["poll_interval"] == 5.0
        assert d["auto_cancel"] is True
        assert "snapshot_dir" in d


# ---------------------------------------------------------------------------
# Tier 1 — Emergency detection tests
# ---------------------------------------------------------------------------


class TestTier1Emergency:
    def test_error_state_fires_emergency(self):
        adapter = _make_adapter(state=PrinterStatus.ERROR, print_error=0x03008014)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        alert = mon._check_tier1_emergency(state, connected=True)
        assert alert is not None
        assert alert.severity == "emergency"
        assert alert.rule == "error_state"
        assert "03008014" in alert.detail

    def test_connection_lost_fires_after_60s(self):
        adapter = _make_adapter()
        mon = _make_monitor(adapter)
        mon._print_was_active = True

        # First check — starts the timer.
        alert = mon._check_tier1_emergency(None, connected=False)
        assert alert is None
        assert mon._connection_lost_since is not None

        # Simulate 61 seconds later.
        mon._connection_lost_since = time.time() - 61
        alert = mon._check_tier1_emergency(None, connected=False)
        assert alert is not None
        assert alert.rule == "connection_lost"

    def test_connection_lost_resets_on_reconnect(self):
        adapter = _make_adapter()
        mon = _make_monitor(adapter)
        mon._print_was_active = True

        # Start connection loss.
        mon._check_tier1_emergency(None, connected=False)
        assert mon._connection_lost_since is not None

        # Reconnect — timer should reset.
        state = adapter.get_state()
        mon._check_tier1_emergency(state, connected=True)
        assert mon._connection_lost_since is None

    def test_thermal_runaway_requires_rising_trend(self):
        adapter = _make_adapter(tool_temp_actual=245.0, tool_temp_target=220.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()

        # Need 3+ rising samples to confirm trend.
        now = time.time()
        mon._temp_history.append(_TempSample(now - 20, 240.0, 220.0, 65.0, 65.0))
        mon._temp_history.append(_TempSample(now - 10, 242.0, 220.0, 65.0, 65.0))
        mon._temp_history.append(_TempSample(now, 245.0, 220.0, 65.0, 65.0))

        alert = mon._check_tier1_emergency(state, connected=True)
        assert alert is not None
        assert alert.rule == "thermal_runaway"

    def test_thermal_runaway_not_triggered_on_stable_overshoot(self):
        adapter = _make_adapter(tool_temp_actual=245.0, tool_temp_target=220.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()

        # Stable (not rising) — all same temp.
        now = time.time()
        mon._temp_history.append(_TempSample(now - 20, 245.0, 220.0, 65.0, 65.0))
        mon._temp_history.append(_TempSample(now - 10, 245.0, 220.0, 65.0, 65.0))
        mon._temp_history.append(_TempSample(now, 245.0, 220.0, 65.0, 65.0))

        alert = mon._check_tier1_emergency(state, connected=True)
        assert alert is None

    def test_no_emergency_on_idle_disconnect(self):
        """Connection lost when no print was active should not fire."""
        adapter = _make_adapter()
        mon = _make_monitor(adapter)
        mon._print_was_active = False  # Never saw a print.

        mon._connection_lost_since = time.time() - 120
        alert = mon._check_tier1_emergency(None, connected=False)
        assert alert is None


# ---------------------------------------------------------------------------
# Tier 2 — Critical detection tests
# ---------------------------------------------------------------------------


class TestTier2Critical:
    def test_temp_drift_sustained_60s(self):
        adapter = _make_adapter(tool_temp_actual=236.0, tool_temp_target=220.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        # First call — starts drift timer.
        alert = mon._check_tier2_critical(state, job)
        assert alert is None
        assert mon._hotend_drift_since is not None

        # Simulate 61 seconds later.
        mon._hotend_drift_since = time.time() - 61
        alert = mon._check_tier2_critical(state, job)
        assert alert is not None
        assert alert.rule == "temp_drift"
        assert alert.severity == "critical"

    def test_temp_drift_resets_on_recovery(self):
        adapter = _make_adapter(tool_temp_actual=236.0, tool_temp_target=220.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        mon._check_tier2_critical(state, job)
        assert mon._hotend_drift_since is not None

        # Temp recovers.
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.PRINTING,
            tool_temp_actual=221.0, tool_temp_target=220.0,
            bed_temp_actual=65.0, bed_temp_target=65.0,
        )
        state2 = adapter.get_state()
        mon._check_tier2_critical(state2, job)
        assert mon._hotend_drift_since is None

    def test_stall_detected_after_10min(self):
        adapter = _make_adapter(completion=42.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        # First call records completion.
        mon._check_tier2_critical(state, job)
        assert mon._last_completion == 42.0

        # Same completion, 11 minutes later.
        mon._last_completion_time = time.time() - 660
        alert = mon._check_tier2_critical(state, job)
        assert alert is not None
        assert alert.rule == "stall"

    def test_print_error_nonzero(self):
        adapter = _make_adapter(print_error=0x03008014)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        alert = mon._check_tier2_critical(state, job)
        assert alert is not None
        assert alert.rule == "print_error"
        assert "03008014" in alert.detail

    def test_stall_with_none_completion(self):
        """Stall detection should be skipped when completion is None."""
        adapter = _make_adapter(completion=None)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        alert = mon._check_tier2_critical(state, job)
        assert alert is None  # No stall alert because completion is None


# ---------------------------------------------------------------------------
# Tier 3 — Warning detection tests
# ---------------------------------------------------------------------------


class TestTier3Warning:
    def test_temp_fluctuation_5c(self):
        adapter = _make_adapter(tool_temp_actual=226.0, tool_temp_target=220.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()

        alert = mon._check_tier3_warning(state)
        assert alert is not None
        assert alert.rule == "temp_fluctuation"
        assert alert.severity == "warning"

    def test_wifi_degraded(self):
        adapter = _make_adapter(wifi_signal="-85dBm")
        mon = _make_monitor(adapter)
        state = adapter.get_state()

        alert = mon._check_tier3_warning(state)
        assert alert is not None
        assert alert.rule == "wifi_degraded"

    def test_missing_data_skips_checks(self):
        """No alerts when temp data is None."""
        adapter = _make_adapter(
            tool_temp_actual=None, tool_temp_target=None,
            bed_temp_actual=None, bed_temp_target=None,
            wifi_signal=None,
        )
        mon = _make_monitor(adapter)
        state = adapter.get_state()

        alert = mon._check_tier3_warning(state)
        assert alert is None


# ---------------------------------------------------------------------------
# Action execution tests
# ---------------------------------------------------------------------------


class TestActionExecution:
    def test_auto_pause_calls_pause_print(self):
        adapter = _make_adapter()
        mon = _make_monitor(adapter, auto_pause=True)

        alert = Alert(time.time(), "critical", "temp_drift", "test", "pause")
        mon._execute_action(alert)
        adapter.pause_print.assert_called_once()

    def test_auto_cancel_calls_emergency_stop(self):
        adapter = _make_adapter()
        mon = _make_monitor(adapter, auto_cancel=True)

        alert = Alert(time.time(), "emergency", "error_state", "test", "emergency_stop")
        mon._execute_action(alert)
        adapter.emergency_stop.assert_called_once()

    def test_no_auto_pause_does_not_pause(self):
        adapter = _make_adapter()
        mon = _make_monitor(adapter, auto_pause=False)

        alert = Alert(time.time(), "critical", "temp_drift", "test", "pause")
        mon._execute_action(alert)
        adapter.pause_print.assert_not_called()


# ---------------------------------------------------------------------------
# Main loop tests
# ---------------------------------------------------------------------------


class TestMainLoop:
    @patch("kiln.print_safety_monitor.time.sleep")
    def test_normal_print_completion(self, mock_sleep):
        """Print transitions from PRINTING to IDLE — should exit 0."""
        adapter = _make_adapter(state=PrinterStatus.PRINTING, completion=99.5)
        mon = _make_monitor(adapter)

        # After first poll cycle, transition to IDLE.
        call_count = [0]

        def side_effect():
            call_count[0] += 1
            if call_count[0] >= 1:
                adapter.get_state.return_value = PrinterState(
                    connected=True, state=PrinterStatus.IDLE,
                    tool_temp_actual=150.0, tool_temp_target=0.0,
                    bed_temp_actual=40.0, bed_temp_target=0.0,
                )
                adapter.get_job.return_value = JobProgress(
                    completion=100.0, file_name="test.gcode",
                    current_layer=174, total_layers=174,
                )

        mock_sleep.side_effect = lambda _: side_effect()
        exit_code = mon.run()
        assert exit_code == 0

    @patch("kiln.print_safety_monitor.time.sleep")
    def test_printer_idle_at_start_no_print(self, mock_sleep):
        """Printer idle at start, no print starts within timeout — exit 1."""
        adapter = _make_adapter(state=PrinterStatus.IDLE)
        mon = _make_monitor(adapter)

        # _wait_for_print_start will poll and never see PRINTING.
        # We need it to timeout quickly.
        with patch("kiln.print_safety_monitor._WAIT_FOR_PRINT_TIMEOUT", 0.1):
            exit_code = mon.run()
        assert exit_code == 1

    @patch("kiln.print_safety_monitor.time.sleep")
    def test_timeout_honored(self, mock_sleep):
        """Monitor should exit 1 when timeout is reached."""
        adapter = _make_adapter(state=PrinterStatus.PRINTING, completion=50.0)
        mon = _make_monitor(adapter, timeout=0.01)

        # Sleep just returns (time progresses naturally).
        mock_sleep.side_effect = lambda _: None
        exit_code = mon.run()
        assert exit_code == 1

    @patch("kiln.print_safety_monitor.time.sleep")
    def test_json_output_format(self, mock_sleep, capsys):
        """JSON mode should output valid JSON lines."""
        adapter = _make_adapter(state=PrinterStatus.PRINTING, completion=50.0)
        mon = _make_monitor(adapter, json_mode=True, timeout=0.01)

        mock_sleep.side_effect = lambda _: None
        mon.run()

        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l.strip()]

        # Should have at least one line.
        assert len(lines) >= 1

        # Each line should be valid JSON.
        for line in lines:
            data = json.loads(line)
            assert "ts" in data
            assert "type" in data

    @patch("kiln.print_safety_monitor.time.sleep")
    def test_connection_drop_during_print(self, mock_sleep):
        """Connection drops should be tracked, not immediately fatal."""
        adapter = _make_adapter(state=PrinterStatus.PRINTING)
        mon = _make_monitor(adapter, timeout=0.01)

        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate connection drop.
                adapter.get_state.side_effect = PrinterError("Connection lost")

        mock_sleep.side_effect = sleep_side_effect
        # Should eventually timeout (not crash).
        exit_code = mon.run()
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestSnapshotManagement:
    def test_snapshot_saved_to_disk(self, tmp_path):
        adapter = _make_adapter(can_snapshot=True)
        adapter.get_snapshot.return_value = b"\xff\xd8\xff" + b"\x00" * 200  # Fake JPEG

        config = MonitorConfig(
            poll_interval=1.0,
            snapshot_interval=0,  # Will be overridden.
            snapshot_dir=tmp_path,
        )
        mon = PrintSafetyMonitor(adapter, "test", config, json_mode=True)
        mon._can_snapshot = True
        mon._config.snapshot_interval = 1  # Capture every second.
        mon._last_snapshot_time = 0  # Force capture.

        with patch("kiln.print_monitor.analyze_snapshot_basic",
                   return_value={"valid": True}):
            result = mon._maybe_capture_snapshot(50.0)

        assert result is not None
        assert "path" in result
        assert Path(result["path"]).exists()
        assert mon._snapshot_count == 1

    def test_snapshot_interval_respected(self):
        adapter = _make_adapter(can_snapshot=True)
        mon = _make_monitor(adapter)
        mon._can_snapshot = True
        mon._config.snapshot_interval = 300
        mon._last_snapshot_time = time.time()  # Just took one.

        result = mon._maybe_capture_snapshot(50.0)
        assert result is None  # Too soon.

    def test_no_camera_skips_snapshots(self):
        adapter = _make_adapter(can_snapshot=False)
        mon = _make_monitor(adapter)
        mon._config.snapshot_interval = 1

        result = mon._maybe_capture_snapshot(50.0)
        assert result is None
        adapter.get_snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful degradation tests
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_no_temp_data(self):
        """All temp checks should be skipped when data is None."""
        adapter = _make_adapter(
            tool_temp_actual=None, tool_temp_target=None,
            bed_temp_actual=None, bed_temp_target=None,
        )
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        assert mon._check_tier1_emergency(state, True) is None
        assert mon._check_tier2_critical(state, job) is None
        assert mon._check_tier3_warning(state) is None

    def test_no_completion_data(self):
        """Stall detection should be skipped when completion is None."""
        adapter = _make_adapter(completion=None, current_layer=None, total_layers=None)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        # Should not crash or return stall.
        alert = mon._check_tier2_critical(state, job)
        assert alert is None

    def test_snapshot_failure_non_fatal(self):
        """Snapshot capture failure should not crash the monitor."""
        adapter = _make_adapter(can_snapshot=True)
        adapter.get_snapshot.side_effect = PrinterError("Camera offline")
        mon = _make_monitor(adapter)
        mon._can_snapshot = True
        mon._config.snapshot_interval = 1
        mon._last_snapshot_time = 0

        result = mon._maybe_capture_snapshot(50.0)
        assert result is None  # Graceful failure.

    def test_temp_target_zero_skips_checks(self):
        """When target is 0 (heater off), temp checks should be skipped."""
        adapter = _make_adapter(tool_temp_actual=25.0, tool_temp_target=0.0,
                                bed_temp_actual=22.0, bed_temp_target=0.0)
        mon = _make_monitor(adapter)
        state = adapter.get_state()
        job = adapter.get_job()

        assert mon._check_tier1_emergency(state, True) is None
        assert mon._check_tier2_critical(state, job) is None
        assert mon._check_tier3_warning(state) is None


# ---------------------------------------------------------------------------
# Alert debouncing tests
# ---------------------------------------------------------------------------


class TestDebouncing:
    def test_same_rule_debounced(self):
        mon = _make_monitor(_make_adapter())
        alert = Alert(time.time(), "warning", "temp_fluctuation", "test", "log")

        # First time — not debounced.
        assert mon._should_debounce(alert) is False
        mon._last_alert_by_rule["temp_fluctuation"] = alert.timestamp

        # Immediately again — debounced.
        alert2 = Alert(time.time(), "warning", "temp_fluctuation", "test2", "log")
        assert mon._should_debounce(alert2) is True

    def test_different_rule_not_debounced(self):
        mon = _make_monitor(_make_adapter())
        mon._last_alert_by_rule["temp_fluctuation"] = time.time()

        alert = Alert(time.time(), "warning", "wifi_degraded", "test", "log")
        assert mon._should_debounce(alert) is False

    def test_debounce_expires_after_60s(self):
        mon = _make_monitor(_make_adapter())
        mon._last_alert_by_rule["temp_fluctuation"] = time.time() - 61

        alert = Alert(time.time(), "warning", "temp_fluctuation", "test", "log")
        assert mon._should_debounce(alert) is False


# ---------------------------------------------------------------------------
# Preheat suppression tests
# ---------------------------------------------------------------------------


class TestPreheatSuppression:
    def test_temp_warning_suppressed_during_preheat(self):
        """Temp fluctuation warnings should NOT fire when heater is still ramping up."""
        adapter = _make_adapter(
            tool_temp_actual=120.0, tool_temp_target=220.0,  # 100°C below target
            bed_temp_actual=30.0, bed_temp_target=65.0,
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time()  # Just started

        state = adapter.get_state()
        alert = mon._check_tier3_warning(state)
        assert alert is None  # Suppressed — still heating.

    def test_temp_warning_fires_after_preheat(self):
        """Temp warnings should fire normally once heater is near target."""
        adapter = _make_adapter(
            tool_temp_actual=210.0, tool_temp_target=220.0,  # Within 10°C
            bed_temp_actual=64.0, bed_temp_target=65.0,
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time() - 30  # 30s ago

        state = adapter.get_state()
        alert = mon._check_tier3_warning(state)
        # 10°C drift — should fire since hotend is within 10°C of target (not preheat).
        assert alert is not None
        assert alert.rule == "temp_fluctuation"

    def test_preheat_grace_period_expires(self):
        """After 5 minutes, preheat suppression should end even if below target."""
        adapter = _make_adapter(
            tool_temp_actual=150.0, tool_temp_target=220.0,
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time() - 301  # 5+ minutes ago

        state = adapter.get_state()
        alert = mon._check_tier3_warning(state)
        assert alert is not None  # Grace expired — warning fires.

    def test_preheat_does_not_suppress_wifi_warning(self):
        """WiFi warnings should still fire during preheat."""
        adapter = _make_adapter(
            tool_temp_actual=100.0, tool_temp_target=220.0,
            wifi_signal="-85dBm",
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time()

        state = adapter.get_state()
        alert = mon._check_tier3_warning(state)
        assert alert is not None
        assert alert.rule == "wifi_degraded"

    def test_preheat_does_not_suppress_emergency(self):
        """Emergency alerts must ALWAYS fire, even during preheat."""
        adapter = _make_adapter(
            state=PrinterStatus.ERROR,
            tool_temp_actual=100.0, tool_temp_target=220.0,
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time()

        state = adapter.get_state()
        alert = mon._check_tier1_emergency(state, True)
        assert alert is not None
        assert alert.severity == "emergency"

    def test_bed_preheat_also_suppresses(self):
        """Preheat detection should also apply when bed is still ramping."""
        adapter = _make_adapter(
            tool_temp_actual=220.0, tool_temp_target=220.0,  # Hotend at target
            bed_temp_actual=30.0, bed_temp_target=65.0,  # Bed still cold
        )
        mon = _make_monitor(adapter)
        mon._print_was_active = True
        mon._active_state_start = time.time()

        # Bed below target means preheat phase, but temp_fluctuation only checks hotend
        # drift. Since hotend is at target (0°C drift), no warning fires anyway.
        state = adapter.get_state()
        alert = mon._check_tier3_warning(state)
        assert alert is None  # No hotend drift, so nothing to suppress.
