"""Tests for kiln.failure_recovery.

Coverage areas:
- FailureType enum values
- FailureClassification, RecoveryPlan, FailureAnalysis dataclasses
- classify_failure heuristics for each failure type
- plan_recovery for each failure type
- analyze_failure full pipeline
- get_failure_history / record_failure persistence
- Edge cases: no data, unknown failure, empty inputs
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from kiln.failure_recovery import (
    FailureAnalysis,
    FailureClassification,
    FailureType,
    RecoveryAction,
    RecoveryPlan,
    analyze_failure,
    classify_failure,
    get_failure_history,
    plan_recovery,
    record_failure,
)


class TestFailureTypeEnum:
    """FailureType enum uses string values and covers all expected types."""

    def test_all_types_have_string_values(self):
        for ft in FailureType:
            assert isinstance(ft.value, str)

    def test_expected_types_exist(self):
        expected = {
            "spaghetti", "layer_shift", "adhesion_loss", "nozzle_clog",
            "stringing", "thermal_runaway", "power_loss", "filament_runout",
            "warping", "unknown",
        }
        actual = {ft.value for ft in FailureType}
        assert expected == actual


class TestRecoveryActionEnum:
    """RecoveryAction enum uses string values."""

    def test_all_actions_have_string_values(self):
        for ra in RecoveryAction:
            assert isinstance(ra.value, str)

    def test_expected_actions_exist(self):
        expected = {
            "restart", "resume", "adjust_and_restart",
            "maintenance_required", "manual_intervention", "safety_shutdown",
        }
        actual = {ra.value for ra in RecoveryAction}
        assert expected == actual


class TestFailureClassificationDataclass:
    """FailureClassification to_dict() and field types."""

    def test_to_dict_serialises_enum(self):
        fc = FailureClassification(
            failure_type=FailureType.SPAGHETTI,
            confidence=0.8,
            evidence=["blob detected"],
            progress_at_failure=0.3,
            time_printing_seconds=600,
            material_wasted_grams=2.5,
        )
        d = fc.to_dict()
        assert d["failure_type"] == "spaghetti"
        assert d["confidence"] == 0.8
        assert d["evidence"] == ["blob detected"]

    def test_to_dict_returns_dict(self):
        fc = FailureClassification(
            failure_type=FailureType.UNKNOWN,
            confidence=0.1,
            evidence=[],
            progress_at_failure=0.0,
            time_printing_seconds=0,
            material_wasted_grams=0.0,
        )
        assert isinstance(fc.to_dict(), dict)


class TestRecoveryPlanDataclass:
    """RecoveryPlan to_dict() serialises the action enum."""

    def test_to_dict_serialises_action(self):
        rp = RecoveryPlan(
            action=RecoveryAction.RESTART,
            steps=["step 1"],
            automated=False,
            estimated_time_minutes=10,
            risk_level="low",
            settings_adjustments={},
            prevent_recurrence=["tip"],
        )
        d = rp.to_dict()
        assert d["action"] == "restart"
        assert d["steps"] == ["step 1"]


class TestFailureAnalysisDataclass:
    """FailureAnalysis to_dict() nests classification and plan."""

    def test_to_dict_nests_correctly(self):
        fc = FailureClassification(
            failure_type=FailureType.WARPING,
            confidence=0.7,
            evidence=["corner lift"],
            progress_at_failure=0.5,
            time_printing_seconds=1800,
            material_wasted_grams=5.0,
        )
        rp = RecoveryPlan(
            action=RecoveryAction.ADJUST_AND_RESTART,
            steps=["clean bed"],
            automated=False,
            estimated_time_minutes=15,
            risk_level="low",
            settings_adjustments={"bed_temp": "+5C"},
            prevent_recurrence=["use brim"],
        )
        fa = FailureAnalysis(
            classification=fc,
            recovery_plan=rp,
            similar_failures=[],
            printer_health={"status": "good"},
        )
        d = fa.to_dict()
        assert d["classification"]["failure_type"] == "warping"
        assert d["recovery_plan"]["action"] == "adjust_and_restart"
        assert d["printer_health"]["status"] == "good"


class TestClassifyFailure:
    """classify_failure heuristic classification for each failure type."""

    def test_thermal_runaway_from_error_message(self):
        result = classify_failure(error_message="thermal runaway detected", progress=0.9)
        assert result.failure_type == FailureType.THERMAL_RUNAWAY
        assert result.confidence > 0.3

    def test_nozzle_clog_from_error_message(self):
        result = classify_failure(error_message="nozzle clog detected, under-extrusion")
        assert result.failure_type == FailureType.NOZZLE_CLOG

    def test_power_loss_from_error_message(self):
        result = classify_failure(error_message="power loss recovery available")
        assert result.failure_type == FailureType.POWER_LOSS

    def test_filament_runout_from_error_message(self):
        result = classify_failure(error_message="filament runout sensor triggered")
        assert result.failure_type == FailureType.FILAMENT_RUNOUT

    def test_layer_shift_from_error_message(self):
        result = classify_failure(error_message="layer shift detected on Y axis")
        assert result.failure_type == FailureType.LAYER_SHIFT

    def test_adhesion_loss_from_low_progress(self):
        result = classify_failure(progress=0.05)
        assert result.failure_type == FailureType.ADHESION_LOSS

    def test_adhesion_loss_from_error_message(self):
        result = classify_failure(error_message="bed adhesion failure")
        assert result.failure_type == FailureType.ADHESION_LOSS

    def test_spaghetti_from_error_message(self):
        result = classify_failure(error_message="spaghetti monster detected")
        assert result.failure_type == FailureType.SPAGHETTI

    def test_stringing_from_error_message(self):
        result = classify_failure(error_message="excessive stringing between parts")
        assert result.failure_type == FailureType.STRINGING

    def test_warping_from_error_message(self):
        result = classify_failure(error_message="warping detected on corners")
        assert result.failure_type == FailureType.WARPING

    def test_unknown_when_no_data(self):
        result = classify_failure()
        assert result.failure_type == FailureType.UNKNOWN

    def test_temperature_spike_detects_thermal_runaway(self):
        temp_history = [
            {"tool_actual": 200},
            {"tool_actual": 210},
            {"tool_actual": 350},  # Spike!
        ]
        result = classify_failure(temperature_history=temp_history)
        assert result.failure_type == FailureType.THERMAL_RUNAWAY

    def test_large_temperature_variation(self):
        temp_history = [
            {"tool_actual": 180},
            {"tool_actual": 220},
        ]
        result = classify_failure(temperature_history=temp_history)
        # 40C variation should trigger thermal evidence
        assert any("variation" in e.lower() for e in result.evidence)

    def test_power_loss_from_events(self):
        events = [
            {"type": "power_disconnect", "data": {}, "timestamp": 100},
            {"type": "disconnect", "data": {}, "timestamp": 200},
        ]
        result = classify_failure(events=events)
        assert result.failure_type == FailureType.POWER_LOSS

    def test_filament_runout_from_events(self):
        events = [
            {"type": "filament_runout", "data": {}, "timestamp": 100},
        ]
        result = classify_failure(events=events)
        assert result.failure_type == FailureType.FILAMENT_RUNOUT

    def test_no_error_with_progress_suggests_power_loss(self):
        result = classify_failure(progress=0.5)
        # No error message + partial progress → power loss evidence
        assert any("power" in e.lower() for e in result.evidence)

    def test_confidence_clamped_to_one(self):
        result = classify_failure(
            error_message="thermal runaway maxtemp heating failed",
            progress=0.95,
            temperature_history=[{"tool_actual": 350}, {"tool_actual": 200}],
        )
        assert result.confidence <= 1.0

    def test_material_wasted_calculated_from_events(self):
        events = [
            {"type": "print_start", "timestamp": 1000.0},
            {"type": "error", "timestamp": 4600.0},  # 3600s = 1 hour
        ]
        result = classify_failure(events=events)
        assert result.material_wasted_grams > 0


class TestPlanRecovery:
    """plan_recovery returns correct plan for each failure type."""

    @pytest.mark.parametrize("failure_type", list(FailureType))
    def test_plan_for_every_failure_type(self, failure_type):
        fc = FailureClassification(
            failure_type=failure_type,
            confidence=0.8,
            evidence=["test"],
            progress_at_failure=0.5,
            time_printing_seconds=1000,
            material_wasted_grams=3.0,
        )
        plan = plan_recovery(fc)
        assert isinstance(plan, RecoveryPlan)
        assert len(plan.steps) > 0
        assert plan.risk_level in ("low", "medium", "high")

    def test_power_loss_resume_with_capability(self):
        fc = FailureClassification(
            failure_type=FailureType.POWER_LOSS,
            confidence=0.9,
            evidence=["power event"],
            progress_at_failure=0.6,
            time_printing_seconds=3600,
            material_wasted_grams=10.0,
        )
        plan = plan_recovery(fc, printer_capabilities={"power_loss_recovery": True})
        assert plan.action == RecoveryAction.RESUME
        assert plan.automated is True

    def test_power_loss_restart_without_capability(self):
        fc = FailureClassification(
            failure_type=FailureType.POWER_LOSS,
            confidence=0.9,
            evidence=["power event"],
            progress_at_failure=0.6,
            time_printing_seconds=3600,
            material_wasted_grams=10.0,
        )
        plan = plan_recovery(fc, printer_capabilities={})
        assert plan.action == RecoveryAction.RESTART

    def test_filament_runout_resume_with_sensor(self):
        fc = FailureClassification(
            failure_type=FailureType.FILAMENT_RUNOUT,
            confidence=0.9,
            evidence=["runout"],
            progress_at_failure=0.7,
            time_printing_seconds=5000,
            material_wasted_grams=15.0,
        )
        plan = plan_recovery(fc, printer_capabilities={"filament_sensor": True})
        assert plan.action == RecoveryAction.RESUME

    def test_thermal_runaway_safety_shutdown(self):
        fc = FailureClassification(
            failure_type=FailureType.THERMAL_RUNAWAY,
            confidence=0.9,
            evidence=["temp spike"],
            progress_at_failure=0.5,
            time_printing_seconds=1800,
            material_wasted_grams=5.0,
        )
        plan = plan_recovery(fc)
        assert plan.action == RecoveryAction.SAFETY_SHUTDOWN
        assert plan.risk_level == "high"


class TestAnalyzeFailure:
    """analyze_failure full pipeline test."""

    @patch("kiln.failure_recovery.get_failure_history", return_value=[])
    def test_full_analysis_returns_analysis(self, mock_history):
        result = analyze_failure(
            error_message="thermal runaway",
            progress=0.9,
            printer_name="test_printer",
        )
        assert isinstance(result, FailureAnalysis)
        assert result.classification.failure_type == FailureType.THERMAL_RUNAWAY
        assert result.recovery_plan.action == RecoveryAction.SAFETY_SHUTDOWN

    @patch("kiln.failure_recovery.get_failure_history", return_value=[])
    def test_analysis_with_no_data(self, mock_history):
        result = analyze_failure()
        assert isinstance(result, FailureAnalysis)
        assert result.classification.failure_type == FailureType.UNKNOWN


class TestFailureHistoryPersistence:
    """Tests for get_failure_history and record_failure with mock DB."""

    def _make_mock_db(self):
        """Create an in-memory SQLite DB with the failure_records table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE failure_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                printer_name TEXT,
                failure_type TEXT NOT NULL,
                confidence REAL,
                progress_at_failure REAL,
                recovery_action TEXT,
                settings_adjustments TEXT,
                evidence TEXT,
                resolved BOOLEAN DEFAULT 0,
                timestamp REAL NOT NULL
            )
        """)
        conn.commit()
        db = MagicMock()
        db._conn = conn
        return db

    @patch("kiln.persistence.get_db")
    def test_record_and_retrieve_failure(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        fc = FailureClassification(
            failure_type=FailureType.SPAGHETTI,
            confidence=0.8,
            evidence=["blob detected"],
            progress_at_failure=0.3,
            time_printing_seconds=600,
            material_wasted_grams=2.5,
        )
        rp = RecoveryPlan(
            action=RecoveryAction.RESTART,
            steps=["clean bed"],
            automated=False,
            estimated_time_minutes=10,
            risk_level="low",
            settings_adjustments={"bed_temp": "+5C"},
            prevent_recurrence=["brim"],
        )

        record_failure(fc, rp, printer_name="ender3", job_id="job-1")

        records = get_failure_history(printer_name="ender3")
        assert len(records) == 1
        assert records[0]["failure_type"] == "spaghetti"
        assert records[0]["printer_name"] == "ender3"

    @patch("kiln.persistence.get_db")
    def test_get_history_filter_by_type(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        # Insert two records of different types
        import time

        now = time.time()
        db._conn.execute(
            "INSERT INTO failure_records (failure_type, confidence, timestamp) VALUES (?, ?, ?)",
            ("spaghetti", 0.8, now),
        )
        db._conn.execute(
            "INSERT INTO failure_records (failure_type, confidence, timestamp) VALUES (?, ?, ?)",
            ("warping", 0.6, now),
        )
        db._conn.commit()

        records = get_failure_history(failure_type="spaghetti")
        assert len(records) == 1
        assert records[0]["failure_type"] == "spaghetti"

    @patch("kiln.persistence.get_db")
    def test_get_history_respects_limit(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        import time

        now = time.time()
        for i in range(10):
            db._conn.execute(
                "INSERT INTO failure_records (failure_type, confidence, timestamp) VALUES (?, ?, ?)",
                ("spaghetti", 0.5, now + i),
            )
        db._conn.commit()

        records = get_failure_history(limit=3)
        assert len(records) == 3

    @patch("kiln.persistence.get_db")
    def test_get_history_empty_table(self, mock_get_db):
        db = self._make_mock_db()
        mock_get_db.return_value = db

        records = get_failure_history()
        assert records == []
