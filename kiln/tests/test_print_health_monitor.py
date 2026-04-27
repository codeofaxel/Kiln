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
