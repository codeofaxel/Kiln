"""Tests for kiln.print_health_monitor — real-time printer health monitoring.

Covers:
- detect_print_phase: heating, thresholds, edge cases
- MonitorPolicy: env var loading, from_dict, defaults
- PrintHealthMonitor: session lifecycle, snapshots, issue reporting, stall detection
- Health history: append, prune, retrieval
- Dataclass serialization: to_dict for all key types
- Singleton: get_print_health_monitor
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest

import kiln.print_health_monitor as _phm_mod
from kiln.print_health_monitor import (
    HealthMetric,
    HealthSeverity,
    MonitorPolicy,
    MonitorSession,
    MonitorSnapshot,
    MonitorStatus,
    PrinterHealthReport,
    PrintHealthMonitor,
    PrintPhase,
    detect_print_phase,
    get_print_health_monitor,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _phm_mod._print_health_monitor = None
    yield
    _phm_mod._print_health_monitor = None


@pytest.fixture
def _clean_env(monkeypatch):
    """Remove all KILN_MONITOR_* env vars."""
    for key in list(os.environ):
        if key.startswith("KILN_MONITOR_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# detect_print_phase
# ---------------------------------------------------------------------------


class TestDetectPrintPhase:

    def test_heating(self):
        assert detect_print_phase(10.0, is_heating=True) == PrintPhase.HEATING

    def test_first_layer(self):
        assert detect_print_phase(0.0) == PrintPhase.FIRST_LAYER
        assert detect_print_phase(3.0) == PrintPhase.FIRST_LAYER

    def test_infill(self):
        assert detect_print_phase(5.0) == PrintPhase.INFILL
        assert detect_print_phase(50.0) == PrintPhase.INFILL

    def test_perimeters(self):
        assert detect_print_phase(70.0) == PrintPhase.PERIMETERS
        assert detect_print_phase(85.0) == PrintPhase.PERIMETERS

    def test_top_layers(self):
        assert detect_print_phase(90.0) == PrintPhase.TOP_LAYERS
        assert detect_print_phase(99.0) == PrintPhase.TOP_LAYERS

    def test_completion_at_100(self):
        assert detect_print_phase(100.0) == PrintPhase.TOP_LAYERS

    def test_none_completion(self):
        assert detect_print_phase(None) == PrintPhase.UNKNOWN

    def test_negative_completion(self):
        assert detect_print_phase(-5.0) == PrintPhase.UNKNOWN


# ---------------------------------------------------------------------------
# MonitorPolicy
# ---------------------------------------------------------------------------


class TestMonitorPolicy:

    def test_defaults(self):
        p = MonitorPolicy()
        assert p.check_delay_seconds == 60
        assert p.check_count == 5
        assert p.check_interval_seconds == 30
        assert p.auto_pause_on_failure is True
        assert p.stall_timeout == 600
        assert p.temp_drift_threshold == 5.0
        assert p.history_max_hours == 72

    def test_from_dict_known_fields(self):
        p = MonitorPolicy.from_dict({"check_count": 10, "stall_timeout": 300})
        assert p.check_count == 10
        assert p.stall_timeout == 300

    def test_from_dict_ignores_unknown_fields(self):
        p = MonitorPolicy.from_dict({"check_count": 10, "unknown_field": True})
        assert p.check_count == 10

    def test_from_env_int_vars(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_CHECK_DELAY", "120")
        monkeypatch.setenv("KILN_MONITOR_CHECK_COUNT", "10")
        monkeypatch.setenv("KILN_MONITOR_CHECK_INTERVAL", "60")
        monkeypatch.setenv("KILN_MONITOR_STALL_TIMEOUT", "300")
        monkeypatch.setenv("KILN_MONITOR_HISTORY_MAX_HOURS", "48")
        p = MonitorPolicy.from_env()
        assert p.check_delay_seconds == 120
        assert p.check_count == 10
        assert p.check_interval_seconds == 60
        assert p.stall_timeout == 300
        assert p.history_max_hours == 48

    def test_from_env_bool_vars(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_AUTO_PAUSE", "false")
        monkeypatch.setenv("KILN_MONITOR_REQUIRE_CAMERA", "true")
        p = MonitorPolicy.from_env()
        assert p.auto_pause_on_failure is False
        assert p.require_camera is True

    def test_from_env_float_var(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_TEMP_DRIFT_THRESHOLD", "3.5")
        p = MonitorPolicy.from_env()
        assert p.temp_drift_threshold == 3.5

    def test_from_env_invalid_int_keeps_default(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_CHECK_DELAY", "not_a_number")
        p = MonitorPolicy.from_env()
        assert p.check_delay_seconds == 60  # default

    def test_from_env_invalid_float_keeps_default(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_TEMP_DRIFT_THRESHOLD", "bad")
        p = MonitorPolicy.from_env()
        assert p.temp_drift_threshold == 5.0  # default

    def test_to_dict(self):
        p = MonitorPolicy()
        d = p.to_dict()
        assert d["check_count"] == 5
        assert d["stall_timeout"] == 600


# ---------------------------------------------------------------------------
# PrintHealthMonitor — session lifecycle
# ---------------------------------------------------------------------------


class TestMonitorSessionLifecycle:

    def test_start_and_stop_monitoring(self):
        monitor = PrintHealthMonitor()
        # Mock check_health to avoid needing a real registry
        monitor.check_health = MagicMock(  # type: ignore[method-assign]
            return_value=PrinterHealthReport(
                printer_name="voron",
                metrics=[],
                overall_status=HealthSeverity.OK,
                checked_at=time.time(),
            )
        )

        # Use high check_count and long delay so thread is still alive when we stop
        sid = monitor.start_monitoring(
            "voron",
            interval_seconds=60,
            policy=MonitorPolicy(check_delay_seconds=999, check_count=100),
        )
        assert isinstance(sid, str)

        session = monitor.stop_monitoring("voron")
        assert isinstance(session, MonitorSession)
        assert session.status in (MonitorStatus.COMPLETED, MonitorStatus.MONITORING)

    def test_start_duplicate_raises(self):
        monitor = PrintHealthMonitor()
        monitor.check_health = MagicMock(  # type: ignore[method-assign]
            return_value=PrinterHealthReport(
                printer_name="voron",
                metrics=[],
                overall_status=HealthSeverity.OK,
                checked_at=time.time(),
            )
        )

        monitor.start_monitoring("voron", interval_seconds=60, policy=MonitorPolicy(check_delay_seconds=999, check_count=1))
        with pytest.raises(ValueError, match="already has an active"):
            monitor.start_monitoring("voron")

        # Cleanup
        monitor.stop_monitoring("voron")

    def test_stop_nonexistent_raises(self):
        monitor = PrintHealthMonitor()
        with pytest.raises(KeyError, match="No active monitoring"):
            monitor.stop_monitoring("ghost")


# ---------------------------------------------------------------------------
# PrintHealthMonitor — snapshots
# ---------------------------------------------------------------------------


class TestMonitorSnapshots:

    def test_capture_snapshot(self):
        monitor = PrintHealthMonitor()
        # Create a session manually
        policy = MonitorPolicy()
        session = MonitorSession(
            session_id="test-session",
            printer_name="voron",
            job_id="job-1",
            policy=policy,
        )
        monitor._sessions["test-session"] = session
        monitor._stall_state["test-session"] = _phm_mod._StallTracker()

        snap = monitor.capture_snapshot(
            "test-session",
            completion_pct=50.0,
            hotend_temp=205.0,
            hotend_target=210.0,
            bed_temp=60.0,
            bed_target=60.0,
        )

        assert isinstance(snap, MonitorSnapshot)
        assert snap.completion_pct == 50.0
        assert snap.hotend_temp == 205.0
        assert snap.phase == "infill"  # 50% → infill
        assert len(session.snapshots) == 1

    def test_capture_snapshot_nonexistent_session_raises(self):
        monitor = PrintHealthMonitor()
        with pytest.raises(KeyError, match="not found"):
            monitor.capture_snapshot("nonexistent", completion_pct=50.0)

    def test_capture_snapshot_completed_session_raises(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="done",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(),
            status=MonitorStatus.COMPLETED,
        )
        monitor._sessions["done"] = session
        with pytest.raises(ValueError, match="not actively monitoring"):
            monitor.capture_snapshot("done", completion_pct=50.0)

    def test_capture_snapshot_heating_phase(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="test",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(),
        )
        monitor._sessions["test"] = session
        monitor._stall_state["test"] = _phm_mod._StallTracker()

        snap = monitor.capture_snapshot(
            "test",
            completion_pct=0.0,
            hotend_temp=50.0,
            hotend_target=210.0,  # big gap → heating
        )
        assert snap.phase == "heating"


# ---------------------------------------------------------------------------
# PrintHealthMonitor — issue reporting
# ---------------------------------------------------------------------------


class TestIssueReporting:

    def _session(self, monitor, sid="test"):
        session = MonitorSession(
            session_id=sid,
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(),
        )
        monitor._sessions[sid] = session
        return session

    def test_report_issue(self):
        monitor = PrintHealthMonitor()
        self._session(monitor)
        issue = monitor.report_issue("test", "layer_shift", 0.9, detail="shifted 2mm")
        assert issue["issue_type"] == "layer_shift"
        assert issue["confidence"] == 0.9
        assert issue["auto_pause_triggered"] is True  # 0.9 >= 0.8 threshold

    def test_report_issue_below_threshold(self):
        monitor = PrintHealthMonitor()
        self._session(monitor)
        issue = monitor.report_issue("test", "minor_stringing", 0.3)
        assert issue["auto_pause_triggered"] is False

    def test_report_issue_invalid_confidence(self):
        monitor = PrintHealthMonitor()
        self._session(monitor)
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            monitor.report_issue("test", "bad", 1.5)

    def test_report_issue_nonexistent_session(self):
        monitor = PrintHealthMonitor()
        with pytest.raises(KeyError, match="not found"):
            monitor.report_issue("ghost", "bad", 0.5)


# ---------------------------------------------------------------------------
# Auto-pause honoring (KILN_MONITOR_AUTO_PAUSE flag actually pauses)
# ---------------------------------------------------------------------------


class TestAutoPauseHonoring:
    """The auto_pause_triggered flag must drive a real pause_print() call.

    Before this contract was honored, report_issue() set the flag on
    the issue dict but no code ever called the adapter's pause_print().
    These tests pin that the flag now drives a real call, scoped by
    the KILN_MONITOR_PAUSE_DISABLED kill-switch and idempotency
    against an already-paused printer.
    """

    def _session(self, monitor, sid="autopause-test", printer="voron"):
        session = MonitorSession(
            session_id=sid,
            printer_name=printer,
            job_id="job-1",
            policy=MonitorPolicy(),
        )
        monitor._sessions[sid] = session
        return session

    def _patched_registry(self, monkeypatch, adapter):
        """Make ``from kiln.registry import get_printer_registry`` return our fake."""
        fake_registry = MagicMock()
        fake_registry.get.return_value = adapter

        import sys
        import types

        fake_module = types.ModuleType("kiln.registry")
        fake_module.get_printer_registry = lambda: fake_registry  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiln.registry", fake_module)
        return fake_registry

    def _make_adapter(self, *, is_paused: bool = False):
        adapter = MagicMock()
        state = MagicMock()
        state.is_paused = is_paused
        adapter.get_state.return_value = state
        return adapter

    def test_pause_called_when_auto_pause_triggered(self, monkeypatch):
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        issue = monitor.report_issue("autopause-test", "thermal_runaway", 1.0)

        assert issue["auto_pause_triggered"] is True
        assert "auto_pause_skipped" not in issue
        assert "auto_pause_error" not in issue
        adapter.pause_print.assert_called_once()

    def test_pause_skipped_when_KILN_MONITOR_PAUSE_DISABLED_is_true(self, monkeypatch):
        monkeypatch.setenv("KILN_MONITOR_PAUSE_DISABLED", "true")
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        issue = monitor.report_issue("autopause-test", "thermal_runaway", 1.0)

        assert issue["auto_pause_triggered"] is True
        assert issue["auto_pause_skipped"] == "kill_switch"
        adapter.pause_print.assert_not_called()

    def test_kill_switch_accepts_alternate_truthy_values(self, monkeypatch):
        # Sanity: spot-check the parser also accepts "1" and "yes".
        monitor = PrintHealthMonitor()
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        for raw in ("1", "YES", "Yes"):
            adapter.reset_mock()
            self._session(monitor, sid=f"kill-{raw}")
            monkeypatch.setenv("KILN_MONITOR_PAUSE_DISABLED", raw)
            issue = monitor.report_issue(f"kill-{raw}", "thermal_runaway", 1.0)
            assert issue["auto_pause_skipped"] == "kill_switch", raw
            adapter.pause_print.assert_not_called()

    def test_pause_skipped_when_already_paused(self, monkeypatch):
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = self._make_adapter(is_paused=True)
        self._patched_registry(monkeypatch, adapter)

        issue = monitor.report_issue("autopause-test", "thermal_runaway", 1.0)

        assert issue["auto_pause_triggered"] is True
        assert issue["auto_pause_skipped"] == "already_paused"
        adapter.pause_print.assert_not_called()

    def test_pause_failure_does_not_break_monitor(self, monkeypatch):
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = self._make_adapter(is_paused=False)
        adapter.pause_print.side_effect = RuntimeError("printer offline")
        self._patched_registry(monkeypatch, adapter)

        # Must not raise.
        issue = monitor.report_issue("autopause-test", "thermal_runaway", 1.0)

        assert issue["auto_pause_triggered"] is True
        assert "auto_pause_error" in issue
        assert "printer offline" in issue["auto_pause_error"]
        adapter.pause_print.assert_called_once()

    def test_state_probe_failure_still_attempts_pause(self, monkeypatch):
        # Defensive: if the adapter's get_state() blows up we should
        # still try to pause (better to over-pause than under-pause).
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = MagicMock()
        adapter.get_state.side_effect = RuntimeError("state unavailable")
        self._patched_registry(monkeypatch, adapter)

        issue = monitor.report_issue("autopause-test", "thermal_runaway", 1.0)

        assert issue["auto_pause_triggered"] is True
        assert "auto_pause_skipped" not in issue
        assert "auto_pause_error" not in issue
        adapter.pause_print.assert_called_once()

    def test_low_confidence_does_not_pause(self, monkeypatch):
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        self._session(monitor)
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        issue = monitor.report_issue("autopause-test", "minor_stringing", 0.5)

        assert issue["auto_pause_triggered"] is False
        adapter.pause_print.assert_not_called()

    def test_connection_only_critical_does_not_pause(self, monkeypatch):
        # The _monitor_loop carveout should down-rank connection-only
        # criticals to confidence 0.5 (below threshold), so report_issue
        # records the issue but auto_pause_triggered is False.
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        session = self._session(monitor, sid="conn-only", printer="voron")
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        # Build a CRITICAL report whose only critical metric is connection_status.
        now = time.time()
        report = PrinterHealthReport(
            printer_name="voron",
            metrics=[
                HealthMetric(
                    metric_name="connection_status",
                    current_value=0.0,
                    expected_value=1.0,
                    deviation=1.0,
                    is_warning=True,
                    timestamp=now,
                    severity=HealthSeverity.CRITICAL,
                    unit="bool",
                    detail="Printer is offline",
                ),
                HealthMetric(
                    metric_name="hotend_temperature",
                    current_value=210.0,
                    expected_value=210.0,
                    deviation=0.0,
                    is_warning=False,
                    timestamp=now,
                    severity=HealthSeverity.OK,
                    unit="°C",
                ),
            ],
            overall_status=HealthSeverity.CRITICAL,
            checked_at=now,
            session_id=session.session_id,
        )

        # Replicate the _monitor_loop carveout logic.
        from kiln.print_health_monitor import _CONNECTION_HEALTH_METRICS
        critical_names = {m.metric_name for m in report.metrics if m.severity == HealthSeverity.CRITICAL}
        connection_only = bool(critical_names) and critical_names.issubset(_CONNECTION_HEALTH_METRICS)
        assert connection_only is True

        confidence = 0.5 if connection_only else 1.0
        issue = monitor.report_issue(session.session_id, "health_critical", confidence)

        assert confidence == 0.5
        assert issue["auto_pause_triggered"] is False
        adapter.pause_print.assert_not_called()

    def test_mixed_critical_with_connection_still_pauses(self, monkeypatch):
        # Connection + hotend both CRITICAL -> NOT connection-only ->
        # full confidence 1.0 -> pause fires.
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        session = self._session(monitor, sid="mixed-crit", printer="voron")
        adapter = self._make_adapter(is_paused=False)
        self._patched_registry(monkeypatch, adapter)

        now = time.time()
        report = PrinterHealthReport(
            printer_name="voron",
            metrics=[
                HealthMetric(
                    metric_name="connection_status",
                    current_value=0.0,
                    expected_value=1.0,
                    deviation=1.0,
                    is_warning=True,
                    timestamp=now,
                    severity=HealthSeverity.CRITICAL,
                    unit="bool",
                ),
                HealthMetric(
                    metric_name="hotend_temperature",
                    current_value=260.0,
                    expected_value=210.0,
                    deviation=50.0,
                    is_warning=True,
                    timestamp=now,
                    severity=HealthSeverity.CRITICAL,
                    unit="°C",
                ),
            ],
            overall_status=HealthSeverity.CRITICAL,
            checked_at=now,
            session_id=session.session_id,
        )

        from kiln.print_health_monitor import _CONNECTION_HEALTH_METRICS
        critical_names = {m.metric_name for m in report.metrics if m.severity == HealthSeverity.CRITICAL}
        connection_only = bool(critical_names) and critical_names.issubset(_CONNECTION_HEALTH_METRICS)
        assert connection_only is False

        confidence = 0.5 if connection_only else 1.0
        issue = monitor.report_issue(session.session_id, "health_critical", confidence)

        assert confidence == 1.0
        assert issue["auto_pause_triggered"] is True
        adapter.pause_print.assert_called_once()


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------


class TestStallDetection:

    def test_no_stall_when_progress_advances(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="test",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(stall_timeout=600),
        )
        monitor._sessions["test"] = session
        tracker = _phm_mod._StallTracker()
        monitor._stall_state["test"] = tracker

        result = monitor._check_stall("test", 10.0)
        assert result is None
        assert tracker.last_progress == 10.0

        result = monitor._check_stall("test", 20.0)
        assert result is None
        assert tracker.last_progress == 20.0

    def test_stall_detected_after_timeout(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="test",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(stall_timeout=10),
        )
        monitor._sessions["test"] = session
        tracker = _phm_mod._StallTracker()
        tracker.last_progress = 50.0
        tracker.last_progress_time = time.time() - 20  # 20s ago, timeout is 10s
        monitor._stall_state["test"] = tracker

        result = monitor._check_stall("test", 50.0)  # same progress
        assert result is not None
        assert result["alert_type"] == "stall"
        assert result["completion_pct"] == 50.0
        assert session.status == MonitorStatus.STALLED

    def test_stall_detection_disabled_with_zero_timeout(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="test",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(stall_timeout=0),
        )
        monitor._sessions["test"] = session
        tracker = _phm_mod._StallTracker()
        tracker.last_progress = 50.0
        tracker.last_progress_time = time.time() - 99999
        monitor._stall_state["test"] = tracker

        result = monitor._check_stall("test", 50.0)
        assert result is None

    def test_no_double_stall_alert(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="test",
            printer_name="voron",
            job_id="job-1",
            policy=MonitorPolicy(stall_timeout=1),
        )
        monitor._sessions["test"] = session
        tracker = _phm_mod._StallTracker()
        tracker.last_progress = 50.0
        tracker.last_progress_time = time.time() - 10
        monitor._stall_state["test"] = tracker

        result1 = monitor._check_stall("test", 50.0)
        assert result1 is not None
        assert tracker.stalled is True

        # Second call should return None (already stalled)
        result2 = monitor._check_stall("test", 50.0)
        assert result2 is None

    def test_publish_stall_event_reaches_event_bus(self, monkeypatch):
        # Regression: the publish path previously imported a nonexistent
        # `get_event_bus` from kiln.events, so the surrounding try/except
        # swallowed an ImportError on every call and the bus never saw
        # the event.
        from kiln.events import Event, EventType

        mock_bus = MagicMock()
        monkeypatch.setattr("kiln.server._get_event_bus", lambda: mock_bus)

        monitor = PrintHealthMonitor()
        alert_data = {
            "alert_type": "stall",
            "printer_name": "voron-350",
            "completion_pct": 50.0,
        }
        monitor._publish_stall_event(alert_data)

        mock_bus.publish.assert_called_once()
        (published_event,) = mock_bus.publish.call_args[0]
        assert isinstance(published_event, Event)
        assert published_event.type == EventType.PRINTER_ERROR
        assert published_event.data == alert_data
        assert published_event.source == "print_health_monitor"


# ---------------------------------------------------------------------------
# Health history
# ---------------------------------------------------------------------------


class TestHealthHistory:

    def test_get_empty_history(self):
        monitor = PrintHealthMonitor()
        assert monitor.get_health_history("voron") == []

    def test_history_respects_time_window(self):
        monitor = PrintHealthMonitor()
        old_report = PrinterHealthReport(
            printer_name="voron",
            metrics=[],
            overall_status=HealthSeverity.OK,
            checked_at=time.time() - 100_000,  # way in the past
        )
        new_report = PrinterHealthReport(
            printer_name="voron",
            metrics=[],
            overall_status=HealthSeverity.OK,
            checked_at=time.time(),
        )
        monitor._health_history["voron"] = [old_report, new_report]

        results = monitor.get_health_history("voron", hours=1)
        assert len(results) == 1
        assert results[0] is new_report


# ---------------------------------------------------------------------------
# Session queries
# ---------------------------------------------------------------------------


class TestSessionQueries:

    def test_get_session(self):
        monitor = PrintHealthMonitor()
        session = MonitorSession(
            session_id="abc",
            printer_name="voron",
            job_id="j1",
            policy=MonitorPolicy(),
        )
        monitor._sessions["abc"] = session
        assert monitor.get_session("abc") is session

    def test_get_session_not_found(self):
        monitor = PrintHealthMonitor()
        with pytest.raises(KeyError, match="not found"):
            monitor.get_session("nonexistent")

    def test_list_sessions_all(self):
        monitor = PrintHealthMonitor()
        for i in range(3):
            monitor._sessions[f"s{i}"] = MonitorSession(
                session_id=f"s{i}",
                printer_name=f"printer-{i}",
                job_id=f"j{i}",
                policy=MonitorPolicy(),
            )
        assert len(monitor.list_sessions()) == 3

    def test_list_sessions_filtered_by_printer(self):
        monitor = PrintHealthMonitor()
        monitor._sessions["s0"] = MonitorSession(
            session_id="s0", printer_name="voron", job_id="j0", policy=MonitorPolicy()
        )
        monitor._sessions["s1"] = MonitorSession(
            session_id="s1", printer_name="ender", job_id="j1", policy=MonitorPolicy()
        )
        results = monitor.list_sessions(printer_name="voron")
        assert len(results) == 1
        assert results[0].printer_name == "voron"

    def test_list_sessions_filtered_by_status(self):
        monitor = PrintHealthMonitor()
        monitor._sessions["active"] = MonitorSession(
            session_id="active", printer_name="voron", job_id="j0", policy=MonitorPolicy()
        )
        monitor._sessions["done"] = MonitorSession(
            session_id="done",
            printer_name="ender",
            job_id="j1",
            policy=MonitorPolicy(),
            status=MonitorStatus.COMPLETED,
        )
        results = monitor.list_sessions(status=MonitorStatus.COMPLETED)
        assert len(results) == 1
        assert results[0].session_id == "done"


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestDataclassSerialization:

    def test_health_metric_to_dict(self):
        m = HealthMetric(
            metric_name="hotend",
            current_value=210.0,
            expected_value=210.0,
            deviation=0.0,
            is_warning=False,
            timestamp=1000.0,
            severity=HealthSeverity.OK,
            unit="°C",
        )
        d = m.to_dict()
        assert d["severity"] == "ok"
        assert d["metric_name"] == "hotend"

    def test_printer_health_report_to_dict(self):
        r = PrinterHealthReport(
            printer_name="voron",
            metrics=[],
            overall_status=HealthSeverity.WARNING,
            checked_at=1000.0,
            phase=PrintPhase.INFILL,
        )
        d = r.to_dict()
        assert d["overall_status"] == "warning"
        assert d["phase"] == "infill"

    def test_monitor_snapshot_to_dict(self):
        s = MonitorSnapshot(
            timestamp=1000.0,
            printer_name="voron",
            phase="infill",
            completion_pct=50.0,
            hotend_temp=210.0,
        )
        d = s.to_dict()
        assert d["completion_pct"] == 50.0
        assert d["hotend_temp"] == 210.0

    def test_monitor_session_to_dict(self):
        s = MonitorSession(
            session_id="abc",
            printer_name="voron",
            job_id="j1",
            policy=MonitorPolicy(),
        )
        d = s.to_dict()
        assert d["session_id"] == "abc"
        assert d["status"] == "monitoring"
        assert isinstance(d["policy"], dict)


# ---------------------------------------------------------------------------
# Predictive risk integration
# ---------------------------------------------------------------------------


class TestPredictiveRiskIntegration:
    """Tests for the kiln-pro predict_risk integration in the monitor loop.

    The wiring is signal-only: red signals get recorded as issues via
    report_issue (so they sit alongside vision-based and health-based
    detections in the agent's view).  Amber signals are NOT recorded
    as issues — they're informational.  ImportError (free tier) is a
    silent no-op.
    """

    def _make_health_report_with_temps(
        self,
        printer_name: str,
        hotend_actual: float,
        hotend_target: float,
        timestamp: float,
    ) -> PrinterHealthReport:
        """Build a health report carrying a hotend temp metric."""
        metric = HealthMetric(
            metric_name="hotend_temperature",
            current_value=hotend_actual,
            expected_value=hotend_target,
            deviation=abs(hotend_actual - hotend_target),
            is_warning=False,
            timestamp=timestamp,
            severity=HealthSeverity.OK,
            unit="°C",
        )
        return PrinterHealthReport(
            printer_name=printer_name,
            metrics=[metric],
            overall_status=HealthSeverity.OK,
            checked_at=timestamp,
        )

    def _build_active_session(self) -> tuple[PrintHealthMonitor, MonitorSession]:
        """Build a real MonitorSession in MONITORING state without spawning a thread."""
        from kiln.print_health_monitor import _StallTracker
        import uuid as _uuid

        monitor = PrintHealthMonitor()
        session_id = str(_uuid.uuid4())
        session = MonitorSession(
            session_id=session_id,
            printer_name="voron",
            job_id="j-test",
            policy=MonitorPolicy(),
        )
        monitor._sessions[session_id] = session
        monitor._stall_state[session_id] = _StallTracker()
        return monitor, session

    def test_telemetry_translation_extracts_hotend_pair(self):
        """Helper translates HealthMetric -> predictor's telemetry shape."""
        report = self._make_health_report_with_temps(
            "voron", hotend_actual=205.0, hotend_target=200.0, timestamp=1000.0,
        )
        out = PrintHealthMonitor._telemetry_from_health_report(report)
        assert out["hotend_temp"] == 205.0
        assert out["hotend_target"] == 200.0
        assert out["timestamp"] == 1000.0

    def test_red_signal_becomes_issue(self):
        """A red predictive signal produces an issue in the session."""
        monitor, session = self._build_active_session()
        session.health_reports.append(
            self._make_health_report_with_temps("voron", 220.0, 200.0, 1000.0)
        )

        fake_assessment = {
            "risk_score": 0.7,
            "severity": "red",
            "signals": [
                {
                    "kind": "thermal_drift",
                    "severity": "red",
                    "weight": 0.55,
                    "message": "Pause now and inspect heater wiring.",
                    "evidence": {"slope_c_per_min": 2.0},
                },
            ],
        }

        from unittest.mock import patch
        with patch.dict(
            "sys.modules",
            {
                "kiln_pro": MagicMock(),
                "kiln_pro.recovery": MagicMock(),
                "kiln_pro.recovery.predictive": MagicMock(
                    predict_risk=lambda **kw: fake_assessment,
                ),
            },
        ):
            monitor._maybe_record_predictive_signals(session)

        red_issues = [
            i for i in session.issues
            if i["issue_type"] == "predictive_red_thermal_drift"
        ]
        assert len(red_issues) == 1
        assert red_issues[0]["confidence"] == 1.0
        assert "Pause now" in red_issues[0]["detail"]

    def test_amber_signal_does_not_create_issue(self):
        """Amber severity signals are informational only — never an issue."""
        monitor, session = self._build_active_session()
        session.health_reports.append(
            self._make_health_report_with_temps("voron", 205.0, 200.0, 1000.0)
        )

        fake_assessment = {
            "risk_score": 0.3,
            "severity": "amber",
            "signals": [
                {
                    "kind": "thermal_drift",
                    "severity": "amber",
                    "weight": 0.3,
                    "message": "Hotend drifting slightly.",
                    "evidence": {"slope_c_per_min": 0.7},
                },
            ],
        }

        from unittest.mock import patch
        with patch.dict(
            "sys.modules",
            {
                "kiln_pro": MagicMock(),
                "kiln_pro.recovery": MagicMock(),
                "kiln_pro.recovery.predictive": MagicMock(
                    predict_risk=lambda **kw: fake_assessment,
                ),
            },
        ):
            monitor._maybe_record_predictive_signals(session)

        assert not session.issues

    def test_kiln_pro_not_installed_silent_no_op(self):
        """ImportError on kiln_pro.recovery.predictive is a clean skip."""
        monitor, session = self._build_active_session()
        session.health_reports.append(
            self._make_health_report_with_temps("voron", 220.0, 200.0, 1000.0)
        )

        import builtins
        real_import = builtins.__import__

        def _blocking_import(name, *args, **kwargs):
            if name.startswith("kiln_pro"):
                raise ImportError(f"blocked for test: {name}")
            return real_import(name, *args, **kwargs)

        from unittest.mock import patch
        with patch("builtins.__import__", side_effect=_blocking_import):
            # Must not raise.
            monitor._maybe_record_predictive_signals(session)

        assert not session.issues

    def test_empty_history_does_not_call_predict(self):
        """No health reports yet -> nothing to translate, predictor not called."""
        monitor, session = self._build_active_session()
        # Intentionally no health_reports appended.

        called = {"yes": False}

        def _spy_predict_risk(**kw):
            called["yes"] = True
            return {"risk_score": 0.0, "severity": "clear", "signals": []}

        from unittest.mock import patch
        with patch.dict(
            "sys.modules",
            {
                "kiln_pro": MagicMock(),
                "kiln_pro.recovery": MagicMock(),
                "kiln_pro.recovery.predictive": MagicMock(
                    predict_risk=_spy_predict_risk,
                ),
            },
        ):
            monitor._maybe_record_predictive_signals(session)

        assert called["yes"] is False
        assert not session.issues


# ---------------------------------------------------------------------------
# Detective failure detection integration
# ---------------------------------------------------------------------------


class TestDetectiveIntegration:
    """Tests for the kiln.print_recovery.detect_failure integration.

    The detective runs alongside the predictive engine on every monitor
    tick.  Critical/high failures clear the auto-pause threshold via
    the severity-to-confidence mapping; medium/low stay visible in the
    issue stream but don't trip pause.  ImportError on the recovery
    engine is a silent no-op.
    """

    def _make_health_report(
        self,
        printer_name: str = "voron",
        hotend_actual: float = 220.0,
        hotend_target: float = 200.0,
        bed_actual: float = 60.0,
        bed_target: float = 60.0,
        connected: bool = True,
        timestamp: float = 1000.0,
    ) -> PrinterHealthReport:
        """Build a health report with thermal + connection metrics."""
        metrics = [
            HealthMetric(
                metric_name="hotend_temperature",
                current_value=hotend_actual,
                expected_value=hotend_target,
                deviation=abs(hotend_actual - hotend_target),
                is_warning=False,
                timestamp=timestamp,
                severity=HealthSeverity.OK,
                unit="°C",
            ),
            HealthMetric(
                metric_name="bed_temperature",
                current_value=bed_actual,
                expected_value=bed_target,
                deviation=abs(bed_actual - bed_target),
                is_warning=False,
                timestamp=timestamp,
                severity=HealthSeverity.OK,
                unit="°C",
            ),
            HealthMetric(
                metric_name="connection_status",
                current_value=1.0 if connected else 0.0,
                expected_value=1.0,
                deviation=0.0 if connected else 1.0,
                is_warning=not connected,
                timestamp=timestamp,
                severity=HealthSeverity.OK if connected else HealthSeverity.CRITICAL,
                unit="bool",
            ),
        ]
        return PrinterHealthReport(
            printer_name=printer_name,
            metrics=metrics,
            overall_status=HealthSeverity.OK,
            checked_at=timestamp,
        )

    def _build_active_session(self) -> tuple[PrintHealthMonitor, MonitorSession]:
        """Build a real MonitorSession in MONITORING state."""
        from kiln.print_health_monitor import _StallTracker
        import uuid as _uuid

        monitor = PrintHealthMonitor()
        session_id = str(_uuid.uuid4())
        session = MonitorSession(
            session_id=session_id,
            printer_name="voron",
            job_id="my_print.gcode",
            policy=MonitorPolicy(),
        )
        monitor._sessions[session_id] = session
        monitor._stall_state[session_id] = _StallTracker()
        return monitor, session

    @staticmethod
    def _make_failure_report(
        *,
        failure_type=None,
        severity: str = "critical",
        printer_name: str = "voron",
        probable_cause: str = "Heater control failure or thermistor malfunction",
        evidence=None,
        failure_id: str = "fid-test-1",
    ):
        """Build a FailureReport without exercising the live detector."""
        from kiln.print_recovery import FailureReport, FailureType

        return FailureReport(
            failure_id=failure_id,
            failure_type=failure_type or FailureType.THERMAL_RUNAWAY,
            detected_at="2026-04-26T00:00:00+00:00",
            printer_name=printer_name,
            evidence=list(evidence or ["Hotend temperature exceeds threshold"]),
            severity=severity,
            probable_cause=probable_cause,
        )

    def test_detect_failure_creates_issue_when_telemetry_critical(self):
        """A critical thermal failure surfaces as an issue with confidence 1.0."""
        monitor, session = self._build_active_session()
        report = self._make_health_report(
            hotend_actual=300.0, hotend_target=200.0,
        )

        failure = self._make_failure_report(severity="critical")

        from unittest.mock import patch

        fake_engine = MagicMock()
        fake_engine.detect_failure.return_value = failure
        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=fake_engine,
        ):
            monitor._maybe_detect_failure(session, report)

        det_issues = [
            i for i in session.issues
            if i["issue_type"] == "detect_failure_thermal_runaway"
        ]
        assert len(det_issues) == 1
        assert det_issues[0]["confidence"] == 1.0
        # Telemetry should have flowed: detector got hotend_temp=300 + target=200.
        called_kwargs = fake_engine.detect_failure.call_args.kwargs
        assert called_kwargs["printer_name"] == "voron"
        assert called_kwargs["telemetry"]["hotend_temp"] == 300.0
        assert called_kwargs["telemetry"]["hotend_target"] == 200.0
        assert called_kwargs["job_info"]["file_name"] == "my_print.gcode"

    def test_no_failure_no_issue(self):
        """Detector returns None -> helper produces no issue."""
        monitor, session = self._build_active_session()
        report = self._make_health_report()  # all green

        from unittest.mock import patch

        fake_engine = MagicMock()
        fake_engine.detect_failure.return_value = None
        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=fake_engine,
        ):
            monitor._maybe_detect_failure(session, report)

        assert not session.issues

    @pytest.mark.parametrize(
        ("severity", "expected_confidence"),
        [
            ("critical", 1.0),
            ("high", 0.85),
            ("medium", 0.6),
            ("low", 0.4),
        ],
    )
    def test_severity_to_confidence_mapping(self, severity, expected_confidence):
        """Each severity bucket maps to its prescribed confidence."""
        monitor, session = self._build_active_session()
        report = self._make_health_report()
        failure = self._make_failure_report(severity=severity)

        from unittest.mock import patch

        fake_engine = MagicMock()
        fake_engine.detect_failure.return_value = failure
        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=fake_engine,
        ):
            monitor._maybe_detect_failure(session, report)

        assert len(session.issues) == 1
        assert session.issues[0]["confidence"] == expected_confidence

    def test_failure_id_attached_to_issue_metadata(self):
        """The recorded issue dict carries the FailureReport's failure_id."""
        monitor, session = self._build_active_session()
        report = self._make_health_report()
        failure = self._make_failure_report(
            severity="critical",
            failure_id="fid-correlation-xyz",
        )

        from unittest.mock import patch

        fake_engine = MagicMock()
        fake_engine.detect_failure.return_value = failure
        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=fake_engine,
        ):
            monitor._maybe_detect_failure(session, report)

        assert session.issues[-1]["failure_id"] == "fid-correlation-xyz"

    def test_kiln_print_recovery_import_failure_silent(self):
        """ImportError on the recovery engine is a clean skip; no issue raised."""
        monitor, session = self._build_active_session()
        report = self._make_health_report(hotend_actual=300.0, hotend_target=200.0)

        import builtins

        real_import = builtins.__import__

        def _blocking_import(name, *args, **kwargs):
            if name == "kiln.print_recovery":
                raise ImportError(f"blocked for test: {name}")
            return real_import(name, *args, **kwargs)

        from unittest.mock import patch

        with patch("builtins.__import__", side_effect=_blocking_import):
            # Must not raise.
            monitor._maybe_detect_failure(session, report)

        assert not session.issues


# ---------------------------------------------------------------------------
# get_latest_signals — agent-facing surface
# ---------------------------------------------------------------------------


class TestGetLatestSignals:
    """Contract tests for PrintHealthMonitor.get_latest_signals.

    The MCP ``monitor_print`` one-shot tool reads this surface to show
    agents the latest predictive + detective signals alongside printer
    state.  These tests pin the shape and the latest-wins selection
    behaviour.
    """

    def _build_active_session(
        self,
        monitor: PrintHealthMonitor | None = None,
        printer_name: str = "voron",
    ) -> tuple[PrintHealthMonitor, MonitorSession]:
        from kiln.print_health_monitor import _StallTracker
        import uuid as _uuid

        monitor = monitor or PrintHealthMonitor()
        session_id = str(_uuid.uuid4())
        session = MonitorSession(
            session_id=session_id,
            printer_name=printer_name,
            job_id="job-x",
            policy=MonitorPolicy(),
        )
        monitor._sessions[session_id] = session
        monitor._stall_state[session_id] = _StallTracker()
        return monitor, session

    def test_returns_inactive_when_no_session(self):
        monitor = PrintHealthMonitor()
        out = monitor.get_latest_signals("ghost-printer")
        assert out["monitoring_active"] is False
        assert out["predictive"] is None
        assert out["detective"] is None
        assert out["session_id"] is None
        assert isinstance(out["as_of"], float)

    def test_surfaces_predictive_red(self):
        monitor, session = self._build_active_session()
        session.issues.append({
            "issue_type": "predictive_red_thermal_drift",
            "confidence": 1.0,
            "detail": "Pause now and inspect heater wiring.",
            "auto_pause_triggered": True,
            "reported_at": 1234.0,
            "snapshot_count": 0,
        })

        out = monitor.get_latest_signals("voron")
        assert out["monitoring_active"] is True
        assert out["session_id"] == session.session_id
        assert out["predictive"] is not None
        assert out["predictive"]["kind"] == "thermal_drift"
        assert out["predictive"]["severity"] == "red"
        assert out["predictive"]["detail"] == "Pause now and inspect heater wiring."
        assert out["detective"] is None

    def test_surfaces_detective_failure(self):
        monitor, session = self._build_active_session()
        session.issues.append({
            "issue_type": "detect_failure_thermal_runaway",
            "confidence": 1.0,
            "detail": "Heater control failure or thermistor malfunction",
            "auto_pause_triggered": True,
            "reported_at": 1234.0,
            "snapshot_count": 0,
            "failure_id": "fid-runaway-1",
        })

        out = monitor.get_latest_signals("voron")
        assert out["detective"] is not None
        assert out["detective"]["failure_type"] == "thermal_runaway"
        assert out["detective"]["failure_id"] == "fid-runaway-1"
        assert out["detective"]["severity"] == "critical"
        assert out["predictive"] is None

    def test_returns_latest_when_multiple_signals(self):
        """Multiple signals in the same session -> latest wins."""
        monitor, session = self._build_active_session()
        session.issues.extend([
            {
                "issue_type": "predictive_red_thermal_drift",
                "confidence": 1.0,
                "detail": "first amber-style alert",
                "auto_pause_triggered": True,
                "reported_at": 1000.0,
                "snapshot_count": 0,
            },
            {
                "issue_type": "predictive_red_thermal_drift",
                "confidence": 1.0,
                "detail": "second amber-style alert",
                "auto_pause_triggered": True,
                "reported_at": 1100.0,
                "snapshot_count": 0,
            },
            {
                "issue_type": "predictive_red_layer_time",
                "confidence": 1.0,
                "detail": "latest red signal — layer time spiking",
                "auto_pause_triggered": True,
                "reported_at": 1200.0,
                "snapshot_count": 0,
            },
        ])

        out = monitor.get_latest_signals("voron")
        assert out["predictive"] is not None
        assert out["predictive"]["kind"] == "layer_time"
        assert out["predictive"]["detail"] == "latest red signal — layer time spiking"

    def test_predictive_and_detective_independent(self):
        """One predictive + one detective in the same session -> both populated."""
        monitor, session = self._build_active_session()
        session.issues.extend([
            {
                "issue_type": "predictive_red_thermal_drift",
                "confidence": 1.0,
                "detail": "predictive trend",
                "auto_pause_triggered": True,
                "reported_at": 1000.0,
                "snapshot_count": 0,
            },
            {
                "issue_type": "detect_failure_thermal_runaway",
                "confidence": 1.0,
                "detail": "detective threshold cross",
                "auto_pause_triggered": True,
                "reported_at": 1100.0,
                "snapshot_count": 0,
                "failure_id": "fid-runaway-2",
            },
        ])

        out = monitor.get_latest_signals("voron")
        assert out["predictive"] is not None
        assert out["predictive"]["kind"] == "thermal_drift"
        assert out["detective"] is not None
        assert out["detective"]["failure_type"] == "thermal_runaway"
        assert out["detective"]["failure_id"] == "fid-runaway-2"
        assert out["detective"]["severity"] == "critical"

    def test_inactive_session_envelope_carries_all_keys(self):
        """The inactive shape must include EVERY new field at None/0/False.

        Agents prompt-engineer against a stable shape — an inactive
        printer mustn't omit keys, just zero them.  Otherwise the
        agent has to handle KeyError separately from None.
        """
        monitor = PrintHealthMonitor()
        out = monitor.get_latest_signals("ghost")
        # All fields are present, set to safe inactive values.
        assert out["monitoring_active"] is False
        assert out["session_id"] is None
        assert out["session_started_at"] is None
        assert out["issue_count"] == 0
        assert out["report_count"] == 0
        assert out["risk"] is None
        assert out["predictive"] is None
        assert out["detective"] is None
        assert out["auto_pause"] is None

    def test_risk_block_populated_from_assessment_cache(self):
        """latest_risk_assessment on the session -> risk block surfaces."""
        monitor, session = self._build_active_session()
        session.latest_risk_assessment = {
            "risk_score": 0.55,
            "severity": "amber",
            "signals": [
                {"kind": "thermal_drift", "severity": "amber", "weight": 0.3},
                {"kind": "flow_drift", "severity": "amber", "weight": 0.25},
                # info-severity signal — must NOT contribute to kinds list
                {"kind": "insufficient_history", "severity": "info", "weight": 0.0},
            ],
        }
        out = monitor.get_latest_signals("voron")
        assert out["risk"] is not None
        assert out["risk"]["score"] == 0.55
        assert out["risk"]["severity"] == "amber"
        # Sorted, deduped, info-severity excluded.
        assert out["risk"]["kinds"] == ["flow_drift", "thermal_drift"]

    def test_risk_block_none_when_no_assessment_yet(self):
        """Session active but predict_risk hasn't run yet -> risk None."""
        monitor, session = self._build_active_session()
        # Default: no latest_risk_assessment cached
        out = monitor.get_latest_signals("voron")
        assert out["risk"] is None
        # But monitoring_active should still be True.
        assert out["monitoring_active"] is True

    def test_auto_pause_block_surfaces_most_recent_triggered(self):
        """auto_pause block reports the freshest issue with the flag set."""
        monitor, session = self._build_active_session()
        session.issues.extend([
            {
                "issue_type": "health_critical",
                "confidence": 1.0,
                "detail": "first auto-pause",
                "auto_pause_triggered": True,
                "reported_at": 1000.0,
                "snapshot_count": 0,
            },
            {
                "issue_type": "predictive_red_thermal_drift",
                "confidence": 1.0,
                "detail": "later predictive",
                "auto_pause_triggered": True,
                "reported_at": 1500.0,
                "snapshot_count": 0,
                "auto_pause_skipped": "kill_switch",
            },
            # Below threshold — must NOT count
            {
                "issue_type": "predictive_red_other",
                "confidence": 0.3,
                "detail": "low-confidence",
                "auto_pause_triggered": False,
                "reported_at": 1700.0,
                "snapshot_count": 0,
            },
        ])
        out = monitor.get_latest_signals("voron")
        assert out["auto_pause"] is not None
        # Latest issue with auto_pause_triggered=True (not the False one)
        assert out["auto_pause"]["issue_type"] == "predictive_red_thermal_drift"
        assert out["auto_pause"]["triggered_at"] == 1500.0
        # Skipped reason carried through
        assert out["auto_pause"]["skipped"] == "kill_switch"
        assert out["auto_pause"]["error"] is None
        # Age is non-negative
        assert out["auto_pause"]["age_seconds"] >= 0.0

    def test_auto_pause_block_none_when_no_triggered_issue(self):
        """No issue with auto_pause_triggered=True -> auto_pause None."""
        monitor, session = self._build_active_session()
        session.issues.append({
            "issue_type": "low_severity_warning",
            "confidence": 0.3,
            "detail": "below threshold",
            "auto_pause_triggered": False,
            "reported_at": 1000.0,
            "snapshot_count": 0,
        })
        out = monitor.get_latest_signals("voron")
        assert out["auto_pause"] is None

    def test_issue_and_report_counts_track_session(self):
        """issue_count + report_count reflect session state."""
        monitor, session = self._build_active_session()
        # Add two issues + three reports.
        session.issues.extend([{"issue_type": f"i{i}"} for i in range(2)])
        session.health_reports.extend([
            PrinterHealthReport(
                printer_name="voron",
                metrics=[],
                overall_status=HealthSeverity.OK,
                checked_at=time.time(),
            )
            for _ in range(3)
        ])
        out = monitor.get_latest_signals("voron")
        assert out["issue_count"] == 2
        assert out["report_count"] == 3
        assert out["session_started_at"] == session.started_at


# ---------------------------------------------------------------------------
# Auto-cancel emergency
# ---------------------------------------------------------------------------


class TestAutoCancel:
    """Auto-cancel only fires after sustained thermal critical AND a pause attempt.

    Pause is the first response; cancel is the last-resort second
    response when the pause didn't restore safe conditions.  These
    tests pin the persistence rule so a single noisy tick can't
    cancel a print, and the cancel path stays gated behind
    ``policy.auto_cancel_on_emergency`` (which is False by default).
    """

    def _session(self, monitor, *, sid="autocancel-test", printer="voron",
                 auto_cancel: bool = True):
        from kiln.print_health_monitor import _EmergencyTracker

        session = MonitorSession(
            session_id=sid,
            printer_name=printer,
            job_id="job-cancel",
            policy=MonitorPolicy(auto_cancel_on_emergency=auto_cancel),
        )
        monitor._sessions[sid] = session
        monitor._emergency_state[sid] = _EmergencyTracker()
        return session

    def _patched_registry(self, monkeypatch, adapter):
        """Make ``from kiln.registry import get_printer_registry`` return our fake."""
        fake_registry = MagicMock()
        fake_registry.get.return_value = adapter

        import sys
        import types

        fake_module = types.ModuleType("kiln.registry")
        fake_module.get_printer_registry = lambda: fake_registry  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiln.registry", fake_module)
        return fake_registry

    def _thermal_critical_report(self, *, session_id, printer="voron"):
        now = time.time()
        return PrinterHealthReport(
            printer_name=printer,
            metrics=[
                HealthMetric(
                    metric_name="hotend_temperature",
                    current_value=290.0,
                    expected_value=210.0,
                    deviation=80.0,
                    is_warning=True,
                    timestamp=now,
                    severity=HealthSeverity.CRITICAL,
                    unit="°C",
                    detail="Hotend runaway",
                ),
            ],
            overall_status=HealthSeverity.CRITICAL,
            checked_at=now,
            session_id=session_id,
        )

    def test_auto_cancel_triggers_on_sustained_thermal_runaway_after_pause(
        self, monkeypatch
    ):
        monkeypatch.delenv("KILN_MONITOR_PAUSE_DISABLED", raising=False)
        monitor = PrintHealthMonitor()
        session = self._session(monitor, auto_cancel=True)
        adapter = MagicMock()
        self._patched_registry(monkeypatch, adapter)

        report = self._thermal_critical_report(session_id=session.session_id)

        # Tick 1 — first thermal critical, pause fires.  No cancel
        # yet because pause is the first response and persistence
        # has only reached 1.
        monitor._maybe_auto_cancel(session, report, pause_fired_this_tick=True)
        assert adapter.cancel_print.call_count == 0

        # Tick 2 — still thermal critical AFTER the pause attempt.
        # Persistence reaches 2 ⇒ auto_cancel fires.
        monitor._maybe_auto_cancel(session, report, pause_fired_this_tick=False)
        adapter.cancel_print.assert_called_once()
        assert any(
            issue.get("auto_cancel_triggered") for issue in session.issues
        )

    def test_auto_cancel_does_not_fire_when_disabled(self, monkeypatch):
        monitor = PrintHealthMonitor()
        session = self._session(monitor, auto_cancel=False)
        adapter = MagicMock()
        self._patched_registry(monkeypatch, adapter)

        report = self._thermal_critical_report(session_id=session.session_id)

        # Both ticks see thermal critical AND a pause attempt; auto_cancel
        # disabled means cancel must not fire even though persistence
        # would otherwise satisfy the rule.
        monitor._maybe_auto_cancel(session, report, pause_fired_this_tick=True)
        monitor._maybe_auto_cancel(session, report, pause_fired_this_tick=False)
        adapter.cancel_print.assert_not_called()
        assert not any(
            issue.get("auto_cancel_triggered") for issue in session.issues
        )

    def test_auto_cancel_does_not_fire_on_first_critical(self, monkeypatch):
        # Persistence rule: cancel must not fire on the very first
        # thermal-critical tick — the pause path gets the first turn.
        monitor = PrintHealthMonitor()
        session = self._session(monitor, auto_cancel=True)
        adapter = MagicMock()
        self._patched_registry(monkeypatch, adapter)

        report = self._thermal_critical_report(session_id=session.session_id)
        monitor._maybe_auto_cancel(session, report, pause_fired_this_tick=True)

        adapter.cancel_print.assert_not_called()
        assert not any(
            issue.get("auto_cancel_triggered") for issue in session.issues
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:

    def test_get_returns_same_instance(self):
        a = get_print_health_monitor()
        b = get_print_health_monitor()
        assert a is b

    def test_get_creates_instance(self):
        m = get_print_health_monitor()
        assert isinstance(m, PrintHealthMonitor)
