"""Tests for local print history trend analysis.

Covers health scoring, failure rate trending, duration analysis,
failure mode detection, material reliability, and alert generation.
All analysis uses local DB data only — no external calls.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from kiln.persistence import KilnDB
from kiln.print_trend_analysis import (
    TrendAlert,
    TrendReport,
    _avg_duration,
    _classify_duration_trend,
    _classify_rate_trend,
    _compute_health_score,
    _failure_mode_counts,
    _material_stats,
    _split_halves,
    _success_rate,
    analyze_printer_trends,
)


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    """Fresh database for each test."""
    db_path = str(tmp_path / "test.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _print_record(
    job_id: str = "job-1",
    printer_name: str = "ender3",
    status: str = "completed",
    duration_seconds: float = 3600.0,
    **kwargs: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": job_id,
        "printer_name": printer_name,
        "file_name": "benchy.gcode",
        "status": status,
        "duration_seconds": duration_seconds,
        "material_type": "PLA",
        "created_at": time.time(),
        "completed_at": time.time(),
    }
    base.update(kwargs)
    return base


def _outcome(
    job_id: str = "job-1",
    printer_name: str = "ender3",
    outcome: str = "success",
    **kwargs: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": job_id,
        "printer_name": printer_name,
        "file_name": "benchy.gcode",
        "file_hash": "abc123",
        "material_type": "PLA",
        "outcome": outcome,
        "quality_grade": "good",
        "failure_mode": None,
        "settings": {"temp_tool": 210},
        "environment": {"ambient_temp": 22},
        "notes": None,
        "agent_id": "claude",
        "created_at": time.time(),
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestSplitHalves:
    def test_empty(self) -> None:
        older, newer = _split_halves([])
        assert older == []
        assert newer == []

    def test_single(self) -> None:
        recs = [{"id": 1}]
        older, newer = _split_halves(recs)
        assert older == recs
        assert newer == recs

    def test_even_split(self) -> None:
        recs = [{"id": i} for i in range(10)]
        older, newer = _split_halves(recs)
        assert len(older) == 5
        assert len(newer) == 5

    def test_odd_split(self) -> None:
        recs = [{"id": i} for i in range(7)]
        older, newer = _split_halves(recs)
        assert len(older) == 3
        assert len(newer) == 4


class TestSuccessRate:
    def test_empty(self) -> None:
        assert _success_rate([]) == 0.0

    def test_all_success_outcomes(self) -> None:
        recs = [{"outcome": "success"} for _ in range(5)]
        assert _success_rate(recs) == 1.0

    def test_all_failed_outcomes(self) -> None:
        recs = [{"outcome": "failed"} for _ in range(5)]
        assert _success_rate(recs) == 0.0

    def test_mixed_outcomes(self) -> None:
        recs = [{"outcome": "success"}, {"outcome": "failed"}, {"outcome": "success"}]
        assert abs(_success_rate(recs) - 2 / 3) < 0.01

    def test_status_field_fallback(self) -> None:
        recs = [{"status": "completed"}, {"status": "failed"}]
        assert _success_rate(recs) == 0.5


class TestClassifyRateTrend:
    def test_stable(self) -> None:
        assert _classify_rate_trend(0.8, 0.8) == "stable"

    def test_worsening(self) -> None:
        assert _classify_rate_trend(0.9, 0.7) == "worsening"

    def test_improving(self) -> None:
        assert _classify_rate_trend(0.6, 0.8) == "improving"

    def test_small_change_is_stable(self) -> None:
        assert _classify_rate_trend(0.8, 0.75) == "stable"


class TestAvgDuration:
    def test_empty(self) -> None:
        assert _avg_duration([]) is None

    def test_no_durations(self) -> None:
        assert _avg_duration([{"duration_seconds": None}]) is None

    def test_zero_durations_excluded(self) -> None:
        recs = [{"duration_seconds": 0}, {"duration_seconds": 100}]
        assert _avg_duration(recs) == 100.0

    def test_normal_avg(self) -> None:
        recs = [{"duration_seconds": 100}, {"duration_seconds": 200}]
        assert _avg_duration(recs) == 150.0


class TestClassifyDurationTrend:
    def test_stable(self) -> None:
        assert _classify_duration_trend(100, 105) == "stable"

    def test_increasing(self) -> None:
        assert _classify_duration_trend(100, 150) == "increasing"

    def test_decreasing(self) -> None:
        assert _classify_duration_trend(100, 60) == "decreasing"

    def test_none_older(self) -> None:
        assert _classify_duration_trend(None, 100) == "stable"

    def test_none_newer(self) -> None:
        assert _classify_duration_trend(100, None) == "stable"

    def test_zero_older(self) -> None:
        assert _classify_duration_trend(0, 100) == "stable"


class TestFailureModeCounts:
    def test_empty(self) -> None:
        assert _failure_mode_counts([]) == []

    def test_counts_only_failed(self) -> None:
        recs = [
            {"outcome": "failed", "failure_mode": "warping"},
            {"outcome": "success", "failure_mode": "warping"},
        ]
        result = _failure_mode_counts(recs)
        assert len(result) == 1
        assert result[0]["count"] == 1

    def test_sorted_by_frequency(self) -> None:
        recs = [
            {"outcome": "failed", "failure_mode": "clog"},
            {"outcome": "failed", "failure_mode": "warping"},
            {"outcome": "failed", "failure_mode": "warping"},
        ]
        result = _failure_mode_counts(recs)
        assert result[0]["mode"] == "warping"
        assert result[0]["count"] == 2
        assert result[1]["mode"] == "clog"

    def test_none_failure_mode_excluded(self) -> None:
        recs = [{"outcome": "failed", "failure_mode": None}]
        assert _failure_mode_counts(recs) == []


class TestMaterialStats:
    def test_empty(self) -> None:
        assert _material_stats([]) == {}

    def test_single_material(self) -> None:
        recs = [
            {"material_type": "PLA", "outcome": "success"},
            {"material_type": "PLA", "outcome": "failed"},
        ]
        result = _material_stats(recs)
        assert "PLA" in result
        assert result["PLA"]["total"] == 2
        assert result["PLA"]["success_rate"] == 0.5

    def test_multiple_materials(self) -> None:
        recs = [
            {"material_type": "PLA", "outcome": "success"},
            {"material_type": "ABS", "outcome": "failed"},
        ]
        result = _material_stats(recs)
        assert len(result) == 2
        assert result["PLA"]["success_rate"] == 1.0
        assert result["ABS"]["success_rate"] == 0.0

    def test_none_material_excluded(self) -> None:
        recs = [{"material_type": None, "outcome": "success"}]
        assert _material_stats(recs) == {}


class TestHealthScore:
    def test_perfect_health(self) -> None:
        score = _compute_health_score(1.0, "stable", "stable", 20)
        assert score == 1.0

    def test_zero_success_rate(self) -> None:
        score = _compute_health_score(0.0, "stable", "stable", 20)
        assert score == 0.0

    def test_worsening_trend_penalty(self) -> None:
        stable = _compute_health_score(0.8, "stable", "stable", 20)
        worsening = _compute_health_score(0.8, "worsening", "stable", 20)
        assert worsening < stable

    def test_improving_trend_bonus(self) -> None:
        stable = _compute_health_score(0.8, "stable", "stable", 20)
        improving = _compute_health_score(0.8, "improving", "stable", 20)
        assert improving > stable

    def test_low_sample_discount(self) -> None:
        good_sample = _compute_health_score(0.8, "stable", "stable", 20)
        low_sample = _compute_health_score(0.8, "stable", "stable", 2)
        assert low_sample < good_sample

    def test_clamps_to_zero(self) -> None:
        score = _compute_health_score(0.0, "worsening", "increasing", 20)
        assert score == 0.0

    def test_clamps_to_one(self) -> None:
        score = _compute_health_score(1.0, "improving", "stable", 50)
        assert score == 1.0


# ---------------------------------------------------------------------------
# Integration tests with real DB
# ---------------------------------------------------------------------------


class TestAnalyzePrinterTrends:
    def test_empty_printer(self, db: KilnDB) -> None:
        report = analyze_printer_trends("nonexistent", db=db)
        assert report.total_prints == 0
        assert report.health_score == 0.0
        assert report.failure_rate_trend == "stable"

    def test_all_successful_prints(self, db: KilnDB) -> None:
        for i in range(10):
            db.save_print_record(_print_record(
                job_id=f"job-{i}",
                status="completed",
                duration_seconds=3600.0,
            ))
        report = analyze_printer_trends("ender3", db=db)
        assert report.success_rate == 1.0
        assert report.health_score > 0.8
        assert report.failure_rate_trend == "stable"

    def test_worsening_failure_rate(self, db: KilnDB) -> None:
        # First half: all success
        for i in range(5):
            db.save_print_record(_print_record(
                job_id=f"old-{i}",
                status="completed",
                completed_at=time.time() - 86400 * 20,
                created_at=time.time() - 86400 * 20,
            ))
        # Second half: mostly failures
        for i in range(5):
            db.save_print_record(_print_record(
                job_id=f"new-{i}",
                status="failed" if i < 4 else "completed",
                completed_at=time.time() - 86400 * 2,
                created_at=time.time() - 86400 * 2,
            ))
        report = analyze_printer_trends("ender3", db=db)
        assert report.failure_rate_trend == "worsening"
        assert any(a.category == "failure_rate" for a in report.alerts)

    def test_recurring_failure_mode_alert(self, db: KilnDB) -> None:
        for i in range(5):
            db.save_print_outcome(_outcome(
                job_id=f"job-{i}",
                outcome="failed",
                failure_mode="warping",
            ))
        report = analyze_printer_trends("ender3", db=db)
        assert any(a.category == "recurring_failure" for a in report.alerts)

    def test_low_material_reliability_alert(self, db: KilnDB) -> None:
        for i in range(4):
            db.save_print_outcome(_outcome(
                job_id=f"job-{i}",
                outcome="failed" if i < 3 else "success",
                material_type="ABS",
                failure_mode="warping" if i < 3 else None,
            ))
        report = analyze_printer_trends("ender3", db=db)
        assert any(a.category == "material_reliability" for a in report.alerts)
        assert "ABS" in report.material_reliability

    def test_sample_size_alert(self, db: KilnDB) -> None:
        db.save_print_record(_print_record(job_id="solo"))
        report = analyze_printer_trends("ender3", db=db)
        assert any(a.category == "sample_size" for a in report.alerts)

    def test_custom_lookback(self, db: KilnDB) -> None:
        # Record outside the lookback window
        db.save_print_record(_print_record(
            job_id="old",
            completed_at=time.time() - 86400 * 10,
            created_at=time.time() - 86400 * 10,
        ))
        # Lookback only 5 days
        report = analyze_printer_trends("ender3", db=db, lookback_days=5)
        assert report.total_prints == 0

    def test_critical_health_alert(self, db: KilnDB) -> None:
        for i in range(10):
            db.save_print_record(_print_record(
                job_id=f"fail-{i}",
                status="failed",
            ))
        report = analyze_printer_trends("ender3", db=db)
        assert any(a.category == "health" for a in report.alerts)
        assert report.health_score < 0.5


class TestTrendReportSerialization:
    def test_to_dict_roundtrip(self) -> None:
        report = TrendReport(
            printer_name="ender3",
            health_score=0.85,
            total_prints=20,
            success_rate=0.9,
            failure_rate_trend="stable",
            avg_duration_seconds=3600.0,
            duration_trend="stable",
            top_failure_modes=[{"mode": "warping", "count": 2}],
            material_reliability={"PLA": {"total": 10, "success_rate": 0.9}},
            alerts=[TrendAlert("warning", "test", "Test alert")],
        )
        d = report.to_dict()
        assert d["printer_name"] == "ender3"
        assert d["health_score"] == 0.85
        assert len(d["alerts"]) == 1
        assert d["alerts"][0]["severity"] == "warning"

    def test_none_duration(self) -> None:
        report = TrendReport(
            printer_name="p",
            health_score=0.5,
            total_prints=0,
            success_rate=0.0,
            failure_rate_trend="stable",
            avg_duration_seconds=None,
            duration_trend="stable",
            top_failure_modes=[],
            material_reliability={},
        )
        d = report.to_dict()
        assert d["avg_duration_seconds"] is None
