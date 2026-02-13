"""Tests for the fleet analytics engine.

Covers fleet snapshots, printer analytics, job recording, utilization
calculations, top performers, problem printers, report generation,
material/failure breakdowns, time-series generation, and period filtering.
"""

from __future__ import annotations

import time

import pytest

from kiln.fleet_analytics import (
    FleetAnalytics,
    TimeSeriesPoint,
    get_fleet_analytics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_printer(
    analytics: FleetAnalytics,
    printer_id: str = "voron-350",
    state: str = "idle",
    *,
    model: str = "Voron 2.4",
    current_job: str | None = None,
    timestamp: float | None = None,
) -> None:
    """Register a printer with a state observation."""
    analytics.record_printer_state(
        printer_id,
        state,
        model=model,
        current_job=current_job,
        timestamp=timestamp,
    )


def _record_job(
    analytics: FleetAnalytics,
    job_id: str = "job-1",
    printer_id: str = "voron-350",
    *,
    success: bool = True,
    duration_s: float = 3600.0,
    material: str = "PLA",
    filament_used_mm: float = 5000.0,
    failure_mode: str | None = None,
    revenue: float = 0.0,
    recorded_at: float | None = None,
) -> None:
    """Record a job with sensible defaults."""
    analytics.record_job(
        job_id,
        printer_id,
        success=success,
        duration_s=duration_s,
        material=material,
        filament_used_mm=filament_used_mm,
        failure_mode=failure_mode,
        revenue=revenue,
        recorded_at=recorded_at,
    )


@pytest.fixture()
def analytics() -> FleetAnalytics:
    """Fresh analytics engine for each test."""
    return FleetAnalytics()


# ---------------------------------------------------------------------------
# FleetSnapshot — empty fleet
# ---------------------------------------------------------------------------


class TestEmptyFleetSnapshot:
    """Snapshot behavior when no printers or jobs are registered."""

    def test_empty_snapshot_all_zeros(self, analytics: FleetAnalytics) -> None:
        snap = analytics.get_fleet_snapshot()
        assert snap.total_printers == 0
        assert snap.online_printers == 0
        assert snap.printing_printers == 0
        assert snap.idle_printers == 0
        assert snap.error_printers == 0
        assert snap.total_jobs_today == 0
        assert snap.successful_jobs_today == 0
        assert snap.failed_jobs_today == 0
        assert snap.fleet_utilization_pct == 0.0
        assert snap.avg_print_time_today_s == 0.0
        assert snap.total_filament_used_today_mm == 0.0
        assert snap.revenue_today == 0.0
        assert snap.top_material == "none"
        assert snap.top_failure_mode is None

    def test_empty_snapshot_has_timestamp(self, analytics: FleetAnalytics) -> None:
        before = time.time()
        snap = analytics.get_fleet_snapshot()
        after = time.time()
        assert before <= snap.timestamp <= after

    def test_snapshot_to_dict_keys(self, analytics: FleetAnalytics) -> None:
        d = analytics.get_fleet_snapshot().to_dict()
        expected_keys = {
            "timestamp",
            "total_printers",
            "online_printers",
            "printing_printers",
            "idle_printers",
            "error_printers",
            "total_jobs_today",
            "successful_jobs_today",
            "failed_jobs_today",
            "fleet_utilization_pct",
            "avg_print_time_today_s",
            "total_filament_used_today_mm",
            "revenue_today",
            "top_material",
            "top_failure_mode",
        }
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# FleetSnapshot — with printers and jobs
# ---------------------------------------------------------------------------


class TestFleetSnapshotWithData:
    """Snapshot behavior with printers registered and jobs recorded."""

    def test_printer_state_counts(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "printing")
        _register_printer(analytics, "p3", "error")
        _register_printer(analytics, "p4", "offline")

        snap = analytics.get_fleet_snapshot()
        assert snap.total_printers == 4
        assert snap.online_printers == 3  # idle + printing + error
        assert snap.printing_printers == 1
        assert snap.idle_printers == 1
        assert snap.error_printers == 1

    def test_utilization_calculation(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "printing")
        _register_printer(analytics, "p2", "idle")
        _register_printer(analytics, "p3", "idle")
        _register_printer(analytics, "p4", "printing")

        snap = analytics.get_fleet_snapshot()
        assert snap.fleet_utilization_pct == pytest.approx(50.0)

    def test_job_statistics(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", success=True, duration_s=1800.0)
        _record_job(analytics, "j2", "p1", success=True, duration_s=3600.0)
        _record_job(analytics, "j3", "p1", success=False, failure_mode="adhesion")

        snap = analytics.get_fleet_snapshot()
        assert snap.total_jobs_today == 3
        assert snap.successful_jobs_today == 2
        assert snap.failed_jobs_today == 1

    def test_avg_print_time(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", duration_s=1000.0)
        _record_job(analytics, "j2", "p1", duration_s=3000.0)

        snap = analytics.get_fleet_snapshot()
        assert snap.avg_print_time_today_s == pytest.approx(2000.0)

    def test_filament_usage(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", filament_used_mm=1000.0)
        _record_job(analytics, "j2", "p1", filament_used_mm=2500.0)

        snap = analytics.get_fleet_snapshot()
        assert snap.total_filament_used_today_mm == pytest.approx(3500.0)

    def test_revenue_tracking(self, analytics: FleetAnalytics) -> None:
        analytics.record_revenue(10.50)
        analytics.record_revenue(25.00)

        snap = analytics.get_fleet_snapshot()
        assert snap.revenue_today == pytest.approx(35.50)

    def test_top_material(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", material="PLA")
        _record_job(analytics, "j2", "p1", material="PLA")
        _record_job(analytics, "j3", "p1", material="ABS")

        snap = analytics.get_fleet_snapshot()
        assert snap.top_material == "PLA"

    def test_top_failure_mode(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", success=False, failure_mode="adhesion")
        _record_job(analytics, "j2", "p1", success=False, failure_mode="adhesion")
        _record_job(analytics, "j3", "p1", success=False, failure_mode="stringing")

        snap = analytics.get_fleet_snapshot()
        assert snap.top_failure_mode == "adhesion"


# ---------------------------------------------------------------------------
# PrinterAnalytics
# ---------------------------------------------------------------------------


class TestPrinterAnalytics:
    """Analytics for individual printers."""

    def test_unknown_printer_raises(self, analytics: FleetAnalytics) -> None:
        with pytest.raises(ValueError, match="Printer not tracked"):
            analytics.get_printer_analytics("nonexistent")

    def test_basic_printer_analytics(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle", model="Ender 3")
        _record_job(analytics, "j1", "p1", success=True, duration_s=2000.0)
        _record_job(analytics, "j2", "p1", success=True, duration_s=4000.0)
        _record_job(analytics, "j3", "p1", success=False, duration_s=1000.0)

        pa = analytics.get_printer_analytics("p1")
        assert pa.printer_id == "p1"
        assert pa.printer_model == "Ender 3"
        assert pa.job_count_24h == 3
        assert pa.success_rate_24h == pytest.approx(66.67, rel=0.01)
        assert pa.avg_job_duration_s == pytest.approx(2333.33, rel=0.01)
        assert pa.current_state == "idle"
        assert pa.current_job is None

    def test_printer_with_current_job(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "printing", current_job="benchy.gcode")
        pa = analytics.get_printer_analytics("p1")
        assert pa.current_state == "printing"
        assert pa.current_job == "benchy.gcode"

    def test_filament_per_printer(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "idle")
        _record_job(analytics, "j1", "p1", filament_used_mm=1000.0)
        _record_job(analytics, "j2", "p2", filament_used_mm=3000.0)

        pa1 = analytics.get_printer_analytics("p1")
        pa2 = analytics.get_printer_analytics("p2")
        assert pa1.filament_used_24h_mm == pytest.approx(1000.0)
        assert pa2.filament_used_24h_mm == pytest.approx(3000.0)

    def test_error_counting(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        analytics.record_error("p1", "Thermal runaway")
        analytics.record_error("p1", "Nozzle clog")

        pa = analytics.get_printer_analytics("p1")
        assert pa.error_count_24h == 2

    def test_printer_analytics_to_dict(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        d = analytics.get_printer_analytics("p1").to_dict()
        expected_keys = {
            "printer_id",
            "printer_model",
            "uptime_pct",
            "job_count_24h",
            "success_rate_24h",
            "avg_job_duration_s",
            "current_state",
            "current_job",
            "filament_used_24h_mm",
            "error_count_24h",
        }
        assert set(d.keys()) == expected_keys

    def test_get_all_printer_analytics(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "printing")
        _register_printer(analytics, "p3", "error")

        all_pa = analytics.get_all_printer_analytics()
        assert len(all_pa) == 3
        ids = [pa.printer_id for pa in all_pa]
        assert ids == ["p1", "p2", "p3"]  # sorted


# ---------------------------------------------------------------------------
# Uptime calculation
# ---------------------------------------------------------------------------


class TestUptimeCalculation:
    """Uptime percentage is based on state history observations."""

    def test_all_online_states_full_uptime(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        # Record multiple online observations
        now = time.time()
        for i in range(10):
            analytics.record_printer_state("p1", "idle", timestamp=now - i * 60)

        pa = analytics.get_printer_analytics("p1")
        assert pa.uptime_pct == pytest.approx(100.0)

    def test_half_offline_half_uptime(self, analytics: FleetAnalytics) -> None:
        now = time.time()
        # Register directly via record_printer_state to control observations
        for i in range(5):
            analytics.record_printer_state("p1", "idle", timestamp=now - i * 60)
        for i in range(5, 10):
            analytics.record_printer_state("p1", "offline", timestamp=now - i * 60)

        pa = analytics.get_printer_analytics("p1")
        assert pa.uptime_pct == pytest.approx(50.0)

    def test_no_history_uses_current_state(self, analytics: FleetAnalytics) -> None:
        # Register with a far-future timestamp so no history falls in 24h window
        far_past = time.time() - 200000
        _register_printer(analytics, "p1", "idle", timestamp=far_past)

        pa = analytics.get_printer_analytics("p1")
        # Current state is "idle" (not offline/error) so uptime is 100%
        assert pa.uptime_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Utilization trend (time series)
# ---------------------------------------------------------------------------


class TestUtilizationTrend:
    """Time-series utilization data."""

    def test_empty_fleet_returns_zero_points(self, analytics: FleetAnalytics) -> None:
        points = analytics.get_utilization_trend(period="24h", interval_minutes=60)
        # Should have ~24 points for 24h at 60min intervals
        assert len(points) >= 23
        assert all(p.value == 0.0 for p in points)
        assert all(p.label == "utilization_pct" for p in points)

    def test_utilization_reflects_state_history(self, analytics: FleetAnalytics) -> None:
        now = time.time()
        # Put printing states in the most recent bucket
        for i in range(5):
            analytics.record_printer_state("p1", "printing", timestamp=now - i * 30)

        points = analytics.get_utilization_trend(period="24h", interval_minutes=60)
        # Last bucket should have utilization > 0
        last_point = points[-1]
        assert last_point.value == pytest.approx(100.0)

    def test_invalid_period_raises(self, analytics: FleetAnalytics) -> None:
        with pytest.raises(ValueError, match="Invalid period"):
            analytics.get_utilization_trend(period="1y")

    def test_time_series_point_to_dict(self) -> None:
        p = TimeSeriesPoint(timestamp=1000.0, value=42.5, label="test")
        d = p.to_dict()
        assert d == {"timestamp": 1000.0, "value": 42.5, "label": "test"}


# ---------------------------------------------------------------------------
# Top performers and problem printers
# ---------------------------------------------------------------------------


class TestRankingQueries:
    """Top performers and problem printers."""

    def test_top_performers_sorted_by_success_rate(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "idle")
        _register_printer(analytics, "p3", "idle")

        # p1: 100% success
        _record_job(analytics, "j1", "p1", success=True)
        # p2: 50% success
        _record_job(analytics, "j2", "p2", success=True)
        _record_job(analytics, "j3", "p2", success=False)
        # p3: 0% success
        _record_job(analytics, "j4", "p3", success=False)

        top = analytics.get_top_performers(limit=3)
        assert len(top) == 3
        assert top[0].printer_id == "p1"
        assert top[0].success_rate_24h == pytest.approx(100.0)
        assert top[2].printer_id == "p3"
        assert top[2].success_rate_24h == pytest.approx(0.0)

    def test_top_performers_limit(self, analytics: FleetAnalytics) -> None:
        for i in range(10):
            _register_printer(analytics, f"p{i}", "idle")
            _record_job(analytics, f"j{i}", f"p{i}", success=True)

        top = analytics.get_top_performers(limit=3)
        assert len(top) == 3

    def test_problem_printers_above_threshold(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "idle")

        # p1: 4 errors (above default threshold of 3)
        for i in range(4):
            analytics.record_error("p1", f"Error {i}")
        # p2: 2 errors (below threshold)
        analytics.record_error("p2", "Error 0")
        analytics.record_error("p2", "Error 1")

        problems = analytics.get_problem_printers(error_threshold=3)
        assert len(problems) == 1
        assert problems[0].printer_id == "p1"
        assert problems[0].error_count_24h == 4

    def test_problem_printers_custom_threshold(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        analytics.record_error("p1", "Error")
        analytics.record_error("p1", "Error")

        # threshold=1 should include p1 (2 errors > 1)
        problems = analytics.get_problem_printers(error_threshold=1)
        assert len(problems) == 1

        # threshold=5 should exclude p1
        problems = analytics.get_problem_printers(error_threshold=5)
        assert len(problems) == 0

    def test_no_problem_printers_in_healthy_fleet(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _register_printer(analytics, "p2", "idle")

        problems = analytics.get_problem_printers()
        assert problems == []


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


class TestReportGeneration:
    """Comprehensive report generation."""

    def test_empty_report(self, analytics: FleetAnalytics) -> None:
        report = analytics.generate_report()
        assert report.period == "24h"
        assert report.fleet_snapshot.total_printers == 0
        assert report.printer_analytics == []
        assert isinstance(report.utilization_history, list)
        assert isinstance(report.success_rate_history, list)
        assert isinstance(report.revenue_history, list)
        assert report.material_breakdown == {}
        assert report.failure_breakdown == {}

    def test_report_with_data(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "printing", current_job="benchy.gcode")
        _register_printer(analytics, "p2", "idle")
        _record_job(analytics, "j1", "p1", success=True, material="PLA")
        _record_job(
            analytics,
            "j2",
            "p1",
            success=False,
            material="ABS",
            failure_mode="warping",
        )
        analytics.record_revenue(15.00)

        report = analytics.generate_report()
        assert report.fleet_snapshot.total_printers == 2
        assert report.fleet_snapshot.printing_printers == 1
        assert len(report.printer_analytics) == 2
        assert report.material_breakdown == {"PLA": 1, "ABS": 1}
        assert report.failure_breakdown == {"warping": 1}
        assert report.fleet_snapshot.revenue_today == pytest.approx(15.00)

    def test_report_invalid_period_raises(self, analytics: FleetAnalytics) -> None:
        with pytest.raises(ValueError, match="Invalid period"):
            analytics.generate_report(period="1y")

    def test_report_7d_period(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", success=True)

        report = analytics.generate_report(period="7d")
        assert report.period == "7d"
        assert report.fleet_snapshot.total_jobs_today == 1

    def test_report_30d_period(self, analytics: FleetAnalytics) -> None:
        report = analytics.generate_report(period="30d")
        assert report.period == "30d"

    def test_report_to_dict(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        d = analytics.generate_report().to_dict()
        expected_keys = {
            "period",
            "fleet_snapshot",
            "printer_analytics",
            "utilization_history",
            "success_rate_history",
            "revenue_history",
            "material_breakdown",
            "failure_breakdown",
        }
        assert set(d.keys()) == expected_keys
        assert isinstance(d["fleet_snapshot"], dict)
        assert isinstance(d["printer_analytics"], list)

    def test_report_time_series_lengths_match(self, analytics: FleetAnalytics) -> None:
        report = analytics.generate_report(period="24h")
        # All time series should have the same number of points
        assert len(report.utilization_history) == len(report.success_rate_history)
        assert len(report.utilization_history) == len(report.revenue_history)


# ---------------------------------------------------------------------------
# Material and failure breakdowns
# ---------------------------------------------------------------------------


class TestBreakdowns:
    """Material and failure mode breakdowns."""

    def test_material_breakdown_counts_jobs(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", material="PLA")
        _record_job(analytics, "j2", "p1", material="PLA")
        _record_job(analytics, "j3", "p1", material="ABS")
        _record_job(analytics, "j4", "p1", material="PETG")
        _record_job(analytics, "j5", "p1", material="PETG")
        _record_job(analytics, "j6", "p1", material="PETG")

        report = analytics.generate_report()
        assert report.material_breakdown == {"PLA": 2, "ABS": 1, "PETG": 3}

    def test_failure_breakdown_ignores_successes(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", success=True)  # no failure_mode
        _record_job(analytics, "j2", "p1", success=False, failure_mode="adhesion")
        _record_job(analytics, "j3", "p1", success=False, failure_mode="adhesion")
        _record_job(analytics, "j4", "p1", success=False, failure_mode="stringing")

        report = analytics.generate_report()
        assert report.failure_breakdown == {"adhesion": 2, "stringing": 1}

    def test_empty_failure_breakdown(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", success=True)

        report = analytics.generate_report()
        assert report.failure_breakdown == {}


# ---------------------------------------------------------------------------
# Period filtering
# ---------------------------------------------------------------------------


class TestPeriodFiltering:
    """Jobs outside the time window are excluded from analytics."""

    def test_old_jobs_excluded_from_snapshot(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        old_time = time.time() - 100000  # ~28 hours ago
        _record_job(analytics, "j-old", "p1", success=True, recorded_at=old_time)
        _record_job(analytics, "j-new", "p1", success=True)

        snap = analytics.get_fleet_snapshot()
        assert snap.total_jobs_today == 1  # only the recent one

    def test_old_errors_excluded_from_printer_analytics(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        old_time = time.time() - 100000
        analytics.record_error("p1", "Old error", timestamp=old_time)
        analytics.record_error("p1", "Recent error")

        pa = analytics.get_printer_analytics("p1")
        assert pa.error_count_24h == 1

    def test_7d_includes_week_old_jobs(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        three_days_ago = time.time() - 259200
        _record_job(analytics, "j1", "p1", success=True, recorded_at=three_days_ago)
        _record_job(analytics, "j2", "p1", success=True)

        report = analytics.generate_report(period="7d")
        assert report.fleet_snapshot.total_jobs_today == 2

    def test_old_revenue_excluded(self, analytics: FleetAnalytics) -> None:
        old_time = time.time() - 100000
        analytics.record_revenue(100.0, timestamp=old_time)
        analytics.record_revenue(25.0)

        snap = analytics.get_fleet_snapshot()
        assert snap.revenue_today == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Module-level singleton accessor."""

    def test_get_fleet_analytics_returns_instance(self) -> None:
        instance = get_fleet_analytics()
        assert isinstance(instance, FleetAnalytics)

    def test_singleton_returns_same_instance(self) -> None:
        a = get_fleet_analytics()
        b = get_fleet_analytics()
        assert a is b


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    """FleetAnalytics constructor validation."""

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid period"):
            FleetAnalytics(period="1y")

    def test_valid_periods(self) -> None:
        for period in ("24h", "7d", "30d"):
            fa = FleetAnalytics(period=period)
            assert fa._default_period == period


# ---------------------------------------------------------------------------
# Event recording edge cases
# ---------------------------------------------------------------------------


class TestEventRecording:
    """Edge cases in event recording."""

    def test_record_job_with_zero_duration(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        _record_job(analytics, "j1", "p1", duration_s=0.0)

        snap = analytics.get_fleet_snapshot()
        assert snap.avg_print_time_today_s == 0.0

    def test_record_many_jobs_evicts_oldest(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        analytics._max_job_records = 10
        for i in range(20):
            _record_job(analytics, f"j{i}", "p1")

        assert len(analytics._job_records) == 10

    def test_record_many_errors_evicts_oldest(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle")
        analytics._max_error_records = 5
        for i in range(10):
            analytics.record_error("p1", f"Error {i}")

        assert len(analytics._error_records) == 5

    def test_state_history_eviction(self, analytics: FleetAnalytics) -> None:
        analytics._max_state_history = 5
        for i in range(10):
            analytics.record_printer_state(f"p{i}", "idle")

        assert len(analytics._state_history) == 5

    def test_printer_model_cached(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle", model="Voron 2.4")
        _register_printer(analytics, "p1", "printing")  # no model update

        pa = analytics.get_printer_analytics("p1")
        assert pa.printer_model == "Voron 2.4"

    def test_state_update_preserves_model(self, analytics: FleetAnalytics) -> None:
        _register_printer(analytics, "p1", "idle", model="Prusa MK4")
        analytics.record_printer_state("p1", "printing")

        pa = analytics.get_printer_analytics("p1")
        assert pa.printer_model == "Prusa MK4"
        assert pa.current_state == "printing"
