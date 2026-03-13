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
