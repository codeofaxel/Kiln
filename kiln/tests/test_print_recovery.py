"""Tests for kiln.print_recovery -- AI-driven failure recovery with automatic resume.

Coverage areas:
- Failure detection from telemetry (each failure type)
- Layer shift detection (position delta, history-based)
- Thermal runaway detection (delta, absolute max, bed, boundary conditions)
- Filament detection (sensor signal, flow anomaly)
- Adhesion detection (sensor, temperature drop, history)
- Nozzle clog detection (sensor, pressure-based)
- Spaghetti detection (sensor signal)
- Warping detection (sensor signal)
- Communication loss detection (connected flag)
- Recovery planning (strategy selection per failure type)
- Confidence computation (high/medium/low for various scenarios)
- Preparation steps (correct steps per strategy)
- Recovery G-code generation (valid G-code for resume, restart, abort)
- Recovery session lifecycle (detect -> plan -> start -> monitor -> complete)
- Recovery confirmation (awaiting -> confirmed -> executing)
- Recovery cancellation (cancel at various stages)
- Concurrency (thread-safe session management)
- Statistics (success rate tracking, failure type distribution)
- Edge cases (no failure, empty telemetry, missing fields, invalid IDs)
- Dataclass serialization (to_dict with enum conversion)
- Singleton pattern (get_recovery_engine)
"""

from __future__ import annotations

import threading
from typing import Any
from unittest import mock

import pytest

from kiln.print_recovery import (
    FailureReport,
    FailureType,
    PrintRecovery,
    RecoveryConfidence,
    RecoveryPlan,
    RecoverySession,
    RecoveryStatus,
    RecoveryStrategy,
    get_recovery_engine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> PrintRecovery:
    """Fresh PrintRecovery instance for each test."""
    return PrintRecovery()


def _make_telemetry(**overrides: Any) -> dict[str, Any]:
    """Build a telemetry dict with sensible defaults."""
    base: dict[str, Any] = {
        "hotend_temp": 200.0,
        "hotend_target": 200.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "connected": True,
        "filament_detected": True,
    }
    base.update(overrides)
    return base


def _make_job_info(**overrides: Any) -> dict[str, Any]:
    """Build a job_info dict with sensible defaults."""
    base: dict[str, Any] = {
        "file_name": "test_print.gcode",
        "layer": 50,
        "total_layers": 200,
        "z_mm": 10.0,
    }
    base.update(overrides)
    return base


def _detect_and_plan(
    engine: PrintRecovery,
    telemetry: dict[str, Any],
    *,
    job_info: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> tuple[FailureReport, RecoveryPlan]:
    """Helper: detect a failure and generate a plan."""
    failure = engine.detect_failure(
        printer_name="test-printer",
        telemetry=telemetry,
        telemetry_history=history,
        job_info=job_info or _make_job_info(),
    )
    assert failure is not None
    plan = engine.plan_recovery(failure)
    return failure, plan


# ---------------------------------------------------------------------------
# 1. Failure detection -- each failure type
# ---------------------------------------------------------------------------


class TestFailureDetection:
    """General failure detection from telemetry."""

    def test_no_failure_in_normal_telemetry(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        result = engine.detect_failure(printer_name="printer-1", telemetry=telemetry)
        assert result is None

    def test_empty_printer_name_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="printer_name is required"):
            engine.detect_failure(printer_name="", telemetry={})

    def test_whitespace_printer_name_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="printer_name is required"):
            engine.detect_failure(printer_name="   ", telemetry={})

    def test_empty_telemetry_no_failure(self, engine: PrintRecovery):
        result = engine.detect_failure(printer_name="p1", telemetry={})
        assert result is None

    def test_failure_stored_in_history(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=320)
        engine.detect_failure(printer_name="p1", telemetry=telemetry)
        history = engine.get_failure_history()
        assert len(history) == 1
        assert history[0].failure_type == FailureType.THERMAL_RUNAWAY

    def test_history_limit_enforced(self, engine: PrintRecovery):
        engine._max_history = 3
        for _ in range(5):
            engine.detect_failure(
                printer_name="p1", telemetry=_make_telemetry(connected=False)
            )
        assert len(engine._failure_history) == 3

    def test_multiple_failures_detected_sequentially(self, engine: PrintRecovery):
        engine.detect_failure(
            printer_name="p1", telemetry=_make_telemetry(connected=False)
        )
        engine.detect_failure(
            printer_name="p2", telemetry=_make_telemetry(hotend_temp=320)
        )
        history = engine.get_failure_history()
        assert len(history) == 2

    def test_detector_priority_thermal_over_layer_shift(self, engine: PrintRecovery):
        """Thermal runaway should be detected before layer shift."""
        telemetry = _make_telemetry(
            hotend_temp=320,
            x_position=100,
            x_expected=0,
            y_position=100,
            y_expected=0,
        )
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.THERMAL_RUNAWAY


# ---------------------------------------------------------------------------
# 2. Layer shift detection
# ---------------------------------------------------------------------------


class TestLayerShiftDetection:
    """Layer shift detection from position data."""

    def test_no_shift_within_threshold(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=10.0, x_expected=10.3, y_position=20.0, y_expected=20.2
        )
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_shift_above_threshold(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=10.0, x_expected=11.0, y_position=20.0, y_expected=20.0
        )
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.LAYER_SHIFT
        assert failure.severity == "high"
        assert len(failure.evidence) > 0

    def test_shift_diagonal(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=0.4, y_position=0.0, y_expected=0.4
        )
        # sqrt(0.16 + 0.16) = ~0.566 > 0.5 threshold
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.LAYER_SHIFT

    def test_shift_at_threshold_triggers(self, engine: PrintRecovery):
        # Exactly at threshold (0.5mm on X only) -- >= boundary triggers detection
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=0.5, y_position=0.0, y_expected=0.0
        )
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.LAYER_SHIFT

    def test_shift_just_below_threshold_no_failure(self, engine: PrintRecovery):
        # Just below threshold (0.49mm on X only) -- should not trigger
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=0.49, y_position=0.0, y_expected=0.0
        )
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_shift_includes_job_info(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        job = _make_job_info(file_name="calibration.gcode", layer=42, z_mm=8.4)
        failure = engine.detect_failure(
            printer_name="p1", telemetry=telemetry, job_info=job
        )
        assert failure is not None
        assert failure.job_name == "calibration.gcode"
        assert failure.failed_layer == 42
        assert failure.failure_z_mm == 8.4

    def test_history_based_shift_with_flag(self, engine: PrintRecovery):
        history = [
            {"x_position": 10.0, "y_position": 20.0},
            {"x_position": 25.0, "y_position": 20.0},
        ]
        telemetry = _make_telemetry(
            x_position=25.0, y_position=20.0, layer_shift_detected=True
        )
        failure = engine.detect_failure(
            printer_name="p1", telemetry=telemetry, telemetry_history=history
        )
        assert failure is not None
        assert failure.failure_type == FailureType.LAYER_SHIFT


# ---------------------------------------------------------------------------
# 3. Thermal runaway detection
# ---------------------------------------------------------------------------


class TestThermalDetection:
    """Thermal runaway detection -- temperature anomalies."""

    def test_normal_temp_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=200, hotend_target=200)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_temp_above_absolute_max(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=310)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.THERMAL_RUNAWAY
        assert failure.severity == "critical"

    def test_temp_exactly_at_abs_max_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=300, hotend_target=300)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_temp_above_target_by_delta(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=220, hotend_target=200)
        # delta = 20 > threshold of 15
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.THERMAL_RUNAWAY

    def test_temp_slightly_above_target_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=210, hotend_target=200)
        # delta = 10 < threshold of 15
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_bed_temp_runaway(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            hotend_temp=200,
            hotend_target=200,
            bed_temp=100,
            bed_target=60,
        )
        # bed delta = 40 > threshold
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.THERMAL_RUNAWAY

    def test_hotend_temp_none_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        telemetry["hotend_temp"] = None
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        # Should not crash, no thermal failure detected
        assert result is None

    def test_target_zero_no_delta_check(self, engine: PrintRecovery):
        """When target is 0 (heater off), don't flag delta as runaway."""
        telemetry = _make_telemetry(hotend_temp=50, hotend_target=0)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_thermal_report_contributing_factors(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=320)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert len(failure.contributing_factors) > 0


# ---------------------------------------------------------------------------
# 4. Filament detection
# ---------------------------------------------------------------------------


class TestFilamentDetection:
    """Filament runout detection."""

    def test_filament_sensor_triggered(self, engine: PrintRecovery):
        telemetry = _make_telemetry(filament_detected=False)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.FILAMENT_RUNOUT
        assert failure.severity == "medium"

    def test_filament_present_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(filament_detected=True)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_flow_anomaly_detection(self, engine: PrintRecovery):
        telemetry = _make_telemetry(flow_rate=0.5, expected_flow=5.0)
        # ratio = 0.1 < 0.3 threshold
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.FILAMENT_RUNOUT

    def test_flow_normal_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(flow_rate=4.5, expected_flow=5.0)
        # ratio = 0.9 > 0.3 threshold
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_no_flow_data_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None


# ---------------------------------------------------------------------------
# 5. Adhesion detection
# ---------------------------------------------------------------------------


class TestAdhesionDetection:
    """Adhesion failure detection."""

    def test_adhesion_sensor_triggered(self, engine: PrintRecovery):
        telemetry = _make_telemetry(adhesion_lost=True)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.ADHESION_FAILURE
        assert failure.severity == "high"

    def test_bed_temp_drop_with_history(self, engine: PrintRecovery):
        # Need >= 2 history entries for sustained drop detection
        history = [
            _make_telemetry(bed_temp=58),
            _make_telemetry(bed_temp=55),
        ]
        telemetry = _make_telemetry(bed_temp=45, bed_target=60)
        # drop from target = 15 > 10 threshold, and prev (55) > current (45)
        failure = engine.detect_failure(
            printer_name="p1", telemetry=telemetry, telemetry_history=history
        )
        assert failure is not None
        assert failure.failure_type == FailureType.ADHESION_FAILURE

    def test_bed_temp_normal_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(bed_temp=58, bed_target=60)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None


# ---------------------------------------------------------------------------
# 6. Nozzle clog detection
# ---------------------------------------------------------------------------


class TestNozzleClogDetection:
    """Nozzle clog detection."""

    def test_clog_sensor_triggered(self, engine: PrintRecovery):
        telemetry = _make_telemetry(nozzle_clogged=True)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.NOZZLE_CLOG

    def test_pressure_above_threshold(self, engine: PrintRecovery):
        telemetry = _make_telemetry(extruder_pressure=150, pressure_threshold=100)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.NOZZLE_CLOG

    def test_pressure_below_threshold_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(extruder_pressure=80, pressure_threshold=100)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_no_pressure_data_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None


# ---------------------------------------------------------------------------
# 7. Spaghetti / warping / communication loss detection
# ---------------------------------------------------------------------------


class TestSpaghettiDetection:
    """Spaghetti detection from sensor."""

    def test_spaghetti_detected(self, engine: PrintRecovery):
        telemetry = _make_telemetry(spaghetti_detected=True)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.SPAGHETTI
        assert failure.severity == "critical"

    def test_no_spaghetti_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None


class TestWarpingDetection:
    """Warping detection from sensor."""

    def test_warping_detected(self, engine: PrintRecovery):
        telemetry = _make_telemetry(warping_detected=True)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.WARPING
        assert failure.severity == "medium"


class TestCommunicationLossDetection:
    """Communication loss detection."""

    def test_disconnected(self, engine: PrintRecovery):
        telemetry = _make_telemetry(connected=False)
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None
        assert failure.failure_type == FailureType.COMMUNICATION_LOSS

    def test_connected_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry(connected=True)
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None

    def test_connected_missing_no_failure(self, engine: PrintRecovery):
        telemetry = _make_telemetry()
        del telemetry["connected"]
        result = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert result is None


# ---------------------------------------------------------------------------
# 8. Recovery planning -- strategy selection per failure type
# ---------------------------------------------------------------------------


class TestRecoveryPlanning:
    """Strategy selection per failure type."""

    def test_thermal_runaway_selects_safe_abort(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT

    def test_filament_runout_selects_wait_retry(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        assert plan.strategy == RecoveryStrategy.WAIT_AND_RETRY

    def test_communication_loss_selects_wait_retry(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(connected=False))
        assert plan.strategy == RecoveryStrategy.WAIT_AND_RETRY

    def test_layer_shift_selects_resume(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        assert plan.strategy == RecoveryStrategy.RESUME_FROM_LAYER

    def test_adhesion_failure_selects_restart(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(adhesion_lost=True))
        assert plan.strategy == RecoveryStrategy.RESTART_WITH_COMPENSATION

    def test_spaghetti_selects_safe_abort(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(
            engine, _make_telemetry(spaghetti_detected=True)
        )
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT

    def test_warping_selects_restart(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(
            engine, _make_telemetry(warping_detected=True)
        )
        assert plan.strategy == RecoveryStrategy.RESTART_WITH_COMPENSATION

    def test_nozzle_clog_selects_wait_retry(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(
            engine, _make_telemetry(nozzle_clogged=True)
        )
        assert plan.strategy == RecoveryStrategy.WAIT_AND_RETRY

    def test_plan_has_preparation_steps(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert len(plan.preparation_steps) > 0

    def test_plan_has_risks(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert len(plan.risks) > 0

    def test_plan_has_reason(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert plan.reason != ""

    def test_resume_plan_has_overlap_layers(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        assert plan.layer_overlap == 3  # layer shift uses 3-layer overlap

    def test_resume_plan_has_resume_layer(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(
            engine, telemetry, job_info=_make_job_info(layer=50)
        )
        assert plan.resume_layer is not None
        assert plan.resume_layer < 50  # Should back up from failed layer

    def test_restart_plan_has_adjustments(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(adhesion_lost=True))
        assert len(plan.parameter_adjustments) > 0
        assert "bed_temp_offset" in plan.parameter_adjustments


# ---------------------------------------------------------------------------
# 9. Confidence computation
# ---------------------------------------------------------------------------


class TestConfidenceComputation:
    """Confidence levels for various failure/strategy combinations."""

    def test_thermal_abort_high_confidence(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert plan.confidence == RecoveryConfidence.HIGH

    def test_filament_wait_high_confidence(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        assert plan.confidence == RecoveryConfidence.HIGH

    def test_layer_shift_resume_medium_confidence(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        assert plan.confidence == RecoveryConfidence.MEDIUM

    def test_spaghetti_abort_high_confidence(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(
            engine, _make_telemetry(spaghetti_detected=True)
        )
        assert plan.confidence == RecoveryConfidence.HIGH

    def test_adhesion_restart_medium_confidence(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(adhesion_lost=True))
        assert plan.confidence == RecoveryConfidence.MEDIUM

    def test_high_confidence_auto_executes(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert plan.requires_confirmation is False

    def test_medium_confidence_requires_confirmation(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        assert plan.requires_confirmation is True

    def test_estimated_success_rate_populated(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert plan.estimated_success_pct > 0.0


# ---------------------------------------------------------------------------
# 10. Preparation steps
# ---------------------------------------------------------------------------


class TestPreparationSteps:
    """Correct preparation steps generated per strategy."""

    def test_safe_abort_includes_heater_off(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        steps_text = " ".join(plan.preparation_steps)
        assert "heater" in steps_text.lower() or "M104" in steps_text

    def test_resume_includes_home(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        steps_text = " ".join(plan.preparation_steps)
        assert "home" in steps_text.lower() or "G28" in steps_text

    def test_layer_shift_includes_belt_check(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        _, plan = _detect_and_plan(engine, telemetry)
        steps_text = " ".join(plan.preparation_steps)
        assert "belt" in steps_text.lower()

    def test_filament_wait_includes_load(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        steps_text = " ".join(plan.preparation_steps)
        assert "filament" in steps_text.lower() or "load" in steps_text.lower()

    def test_nozzle_clog_includes_cold_pull(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(
            engine, _make_telemetry(nozzle_clogged=True)
        )
        steps_text = " ".join(plan.preparation_steps)
        assert "cold pull" in steps_text.lower()

    def test_restart_includes_clean_bed(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(adhesion_lost=True))
        steps_text = " ".join(plan.preparation_steps)
        assert "clean" in steps_text.lower()

    def test_thermal_abort_includes_emergency_warning(self, engine: PrintRecovery):
        _, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        steps_text = " ".join(plan.preparation_steps)
        assert "emergency" in steps_text.lower() or "IMMEDIATE" in steps_text


# ---------------------------------------------------------------------------
# 11. Recovery G-code generation
# ---------------------------------------------------------------------------


class TestRecoveryGcodeGeneration:
    """Valid G-code generated for each strategy."""

    def test_safe_abort_gcode(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        assert "M104 S0" in steps
        assert "M140 S0" in steps
        assert "M84" in steps

    def test_resume_gcode_includes_home(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure, plan = _detect_and_plan(engine, telemetry)
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        assert "G28" in steps

    def test_resume_gcode_includes_temp_commands(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure, plan = _detect_and_plan(engine, telemetry)
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        has_temp = any("M104" in s or "M109" in s for s in steps)
        assert has_temp

    def test_resume_gcode_includes_prime(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure, plan = _detect_and_plan(engine, telemetry)
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        has_prime = any("E5" in s or "E10" in s for s in steps)
        assert has_prime

    def test_wait_retry_filament_gcode(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        assert len(steps) > 0

    def test_no_recovery_gcode(self, engine: PrintRecovery):
        # Stringing maps to NO_RECOVERY -- need to plan directly.
        failure = FailureReport(
            failure_id="test",
            failure_type=FailureType.STRINGING,
            detected_at="2024-01-01T00:00:00+00:00",
            printer_name="p1",
        )
        plan = engine.plan_recovery(failure)
        assert plan.strategy == RecoveryStrategy.NO_RECOVERY
        session = engine.start_recovery(plan, failure)
        steps = engine.get_recovery_steps(session.session_id)
        assert any("no recovery" in s.lower() or "No recovery" in s for s in steps)


# ---------------------------------------------------------------------------
# 12. Recovery session lifecycle
# ---------------------------------------------------------------------------


class TestRecoverySession:
    """Full lifecycle: detect -> plan -> start -> monitor -> complete."""

    def test_full_lifecycle_success(self, engine: PrintRecovery):
        # 1. Detect
        failure = engine.detect_failure(
            printer_name="p1", telemetry=_make_telemetry(hotend_temp=320)
        )
        assert failure is not None

        # 2. Plan
        plan = engine.plan_recovery(failure)
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT

        # 3. Start (high confidence = auto-execute)
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.EXECUTING

        # 4. Monitor
        session = engine.record_monitoring_check(session.session_id, passed=True, notes="Temps falling")
        assert session.status == RecoveryStatus.MONITORING
        assert session.monitoring_checks == 1
        assert session.monitoring_passed == 1

        session = engine.record_monitoring_check(session.session_id, passed=True, notes="All clear")
        assert session.monitoring_checks == 2

        # 5. Complete
        session = engine.complete_recovery(session.session_id, success=True, notes="Safe shutdown")
        assert session.status == RecoveryStatus.COMPLETED
        assert session.completed_at is not None
        assert session.result_notes == "Safe shutdown"

    def test_full_lifecycle_with_confirmation(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        plan = engine.plan_recovery(failure)
        assert plan.requires_confirmation is True

        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.AWAITING_CONFIRMATION

        session = engine.confirm_recovery(session.session_id)
        assert session.status == RecoveryStatus.EXECUTING

        session = engine.complete_recovery(session.session_id, success=True)
        assert session.status == RecoveryStatus.COMPLETED

    def test_session_retrievable_by_id(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        retrieved = engine.get_session(session.session_id)
        assert retrieved is not None
        assert retrieved.session_id == session.session_id

    def test_nonexistent_session_returns_none(self, engine: PrintRecovery):
        assert engine.get_session("nonexistent-id") is None

    def test_active_sessions_tracking(self, engine: PrintRecovery):
        f1, p1 = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        s1 = engine.start_recovery(p1, f1)

        f2, p2 = _detect_and_plan(engine, _make_telemetry(connected=False))
        s2 = engine.start_recovery(p2, f2)

        active = engine.get_active_sessions()
        assert len(active) == 2

        engine.complete_recovery(s1.session_id, success=True)
        active = engine.get_active_sessions()
        assert len(active) == 1
        assert active[0].session_id == s2.session_id

    def test_failed_recovery(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        session = engine.complete_recovery(
            session.session_id, success=False, notes="Temperature did not decrease"
        )
        assert session.status == RecoveryStatus.FAILED


# ---------------------------------------------------------------------------
# 13. Recovery confirmation
# ---------------------------------------------------------------------------


class TestRecoveryConfirmation:
    """Awaiting confirmation -> confirmed -> executing flow."""

    def test_confirm_transitions_to_executing(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure, plan = _detect_and_plan(engine, telemetry)
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.AWAITING_CONFIRMATION

        session = engine.confirm_recovery(session.session_id)
        assert session.status == RecoveryStatus.EXECUTING

    def test_confirm_nonexistent_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.confirm_recovery("nonexistent")

    def test_confirm_already_executing_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.EXECUTING

        with pytest.raises(ValueError, match="not awaiting_confirmation"):
            engine.confirm_recovery(session.session_id)

    def test_confirm_completed_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.complete_recovery(session.session_id, success=True)

        with pytest.raises(ValueError, match="not awaiting_confirmation"):
            engine.confirm_recovery(session.session_id)


# ---------------------------------------------------------------------------
# 14. Recovery cancellation
# ---------------------------------------------------------------------------


class TestRecoveryCancellation:
    """Cancel at various stages."""

    def test_cancel_awaiting_confirmation(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure, plan = _detect_and_plan(engine, telemetry)
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.AWAITING_CONFIRMATION

        session = engine.cancel_recovery(session.session_id, reason="User decided not to")
        assert session.status == RecoveryStatus.CANCELLED
        assert session.completed_at is not None
        assert "User decided" in session.result_notes

    def test_cancel_executing(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.EXECUTING

        session = engine.cancel_recovery(session.session_id)
        assert session.status == RecoveryStatus.CANCELLED

    def test_cancel_monitoring(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.record_monitoring_check(session.session_id, passed=True)

        session = engine.cancel_recovery(session.session_id, reason="Looks bad")
        assert session.status == RecoveryStatus.CANCELLED

    def test_cancel_nonexistent_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.cancel_recovery("nonexistent")

    def test_cancel_completed_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.complete_recovery(session.session_id, success=True)

        with pytest.raises(ValueError, match="terminal state"):
            engine.cancel_recovery(session.session_id)

    def test_cancel_already_cancelled_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.cancel_recovery(session.session_id)

        with pytest.raises(ValueError, match="terminal state"):
            engine.cancel_recovery(session.session_id)

    def test_cancel_without_reason_has_default(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        session = engine.cancel_recovery(session.session_id)
        assert session.result_notes == "Cancelled by user"


# ---------------------------------------------------------------------------
# 15. Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Thread-safe session management."""

    def test_concurrent_session_creation(self, engine: PrintRecovery):
        results: list[RecoverySession] = []
        errors: list[Exception] = []

        def _create_session(idx: int) -> None:
            try:
                failure = FailureReport(
                    failure_id=f"fail-{idx}",
                    failure_type=FailureType.THERMAL_RUNAWAY,
                    detected_at="2024-01-01T00:00:00+00:00",
                    printer_name=f"printer-{idx}",
                    severity="critical",
                )
                plan = engine.plan_recovery(failure)
                session = engine.start_recovery(plan, failure)
                results.append(session)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_create_session, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        # All session IDs should be unique.
        ids = {s.session_id for s in results}
        assert len(ids) == 10

    def test_concurrent_detection(self, engine: PrintRecovery):
        results: list[FailureReport | None] = []

        def _detect(idx: int) -> None:
            result = engine.detect_failure(
                printer_name=f"p-{idx}",
                telemetry=_make_telemetry(hotend_temp=320),
            )
            results.append(result)

        threads = [threading.Thread(target=_detect, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is not None for r in results)
        assert len(engine.get_failure_history(limit=100)) == 10

    def test_concurrent_complete_and_cancel(self, engine: PrintRecovery):
        """Completing and cancelling different sessions concurrently."""
        sessions = []
        for i in range(6):
            failure = FailureReport(
                failure_id=f"fail-{i}",
                failure_type=FailureType.THERMAL_RUNAWAY,
                detected_at="2024-01-01T00:00:00+00:00",
                printer_name=f"printer-{i}",
            )
            plan = engine.plan_recovery(failure)
            session = engine.start_recovery(plan, failure)
            sessions.append(session)

        errors: list[Exception] = []

        def _complete(s: RecoverySession) -> None:
            try:
                engine.complete_recovery(s.session_id, success=True)
            except Exception as exc:
                errors.append(exc)

        def _cancel(s: RecoverySession) -> None:
            try:
                engine.cancel_recovery(s.session_id)
            except Exception as exc:
                errors.append(exc)

        threads = []
        for i, s in enumerate(sessions):
            if i % 2 == 0:
                threads.append(threading.Thread(target=_complete, args=(s,)))
            else:
                threads.append(threading.Thread(target=_cancel, args=(s,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        active = engine.get_active_sessions()
        assert len(active) == 0


# ---------------------------------------------------------------------------
# 16. Statistics
# ---------------------------------------------------------------------------


class TestStatistics:
    """Recovery statistics tracking."""

    def test_empty_statistics(self, engine: PrintRecovery):
        stats = engine.get_recovery_statistics()
        assert stats["total_failures_detected"] == 0
        assert stats["total_recovery_sessions"] == 0
        assert stats["success_rate"] == 0.0

    def test_statistics_after_successful_recovery(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.complete_recovery(session.session_id, success=True)

        stats = engine.get_recovery_statistics()
        assert stats["total_failures_detected"] == 1
        assert stats["total_recovery_sessions"] == 1
        assert stats["completed"] == 1
        assert stats["success_rate"] == 1.0

    def test_statistics_with_mixed_outcomes(self, engine: PrintRecovery):
        # Successful recovery
        f1, p1 = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        s1 = engine.start_recovery(p1, f1)
        engine.complete_recovery(s1.session_id, success=True)

        # Failed recovery
        f2, p2 = _detect_and_plan(engine, _make_telemetry(connected=False))
        s2 = engine.start_recovery(p2, f2)
        engine.complete_recovery(s2.session_id, success=False)

        # Cancelled
        f3, p3 = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        s3 = engine.start_recovery(p3, f3)
        engine.cancel_recovery(s3.session_id)

        stats = engine.get_recovery_statistics()
        assert stats["total_recovery_sessions"] == 3
        assert stats["completed"] == 1
        assert stats["failed"] == 1
        assert stats["cancelled"] == 1
        assert stats["success_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_failure_type_distribution(self, engine: PrintRecovery):
        engine.detect_failure(printer_name="p1", telemetry=_make_telemetry(hotend_temp=320))
        engine.detect_failure(printer_name="p2", telemetry=_make_telemetry(hotend_temp=320))
        engine.detect_failure(printer_name="p3", telemetry=_make_telemetry(connected=False))

        stats = engine.get_recovery_statistics()
        dist = stats["failure_type_distribution"]
        assert dist["thermal_runaway"] == 2
        assert dist["communication_loss"] == 1

    def test_strategy_statistics(self, engine: PrintRecovery):
        f1, p1 = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        s1 = engine.start_recovery(p1, f1)
        engine.complete_recovery(s1.session_id, success=True)

        stats = engine.get_recovery_statistics()
        strategy_stats = stats["strategy_statistics"]
        assert "safe_abort" in strategy_stats
        assert strategy_stats["safe_abort"]["total"] == 1
        assert strategy_stats["safe_abort"]["successful"] == 1


# ---------------------------------------------------------------------------
# 17. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_complete_nonexistent_session_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.complete_recovery("nonexistent", success=True)

    def test_complete_already_completed_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.complete_recovery(session.session_id, success=True)

        with pytest.raises(ValueError, match="terminal state"):
            engine.complete_recovery(session.session_id, success=True)

    def test_monitor_nonexistent_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.record_monitoring_check("nonexistent", passed=True)

    def test_monitor_completed_session_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.complete_recovery(session.session_id, success=True)

        with pytest.raises(ValueError, match="cannot record"):
            engine.record_monitoring_check(session.session_id, passed=True)

    def test_get_steps_nonexistent_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.get_recovery_steps("nonexistent")

    def test_plan_unknown_failure_type(self, engine: PrintRecovery):
        """Plan for a failure type with no explicit strategies falls to safe abort."""
        failure = FailureReport(
            failure_id="test",
            failure_type=FailureType.BLOB_DETECTED,
            detected_at="2024-01-01T00:00:00+00:00",
            printer_name="p1",
        )
        plan = engine.plan_recovery(failure)
        # Blob has SAFE_ABORT as primary
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT

    def test_resume_layer_minimum_is_1(self, engine: PrintRecovery):
        """Resume layer should never go below 1 even with overlap."""
        failure = FailureReport(
            failure_id="test",
            failure_type=FailureType.LAYER_SHIFT,
            detected_at="2024-01-01T00:00:00+00:00",
            printer_name="p1",
            failed_layer=2,
            total_layers=100,
        )
        plan = engine.plan_recovery(failure)
        assert plan.resume_layer is not None
        assert plan.resume_layer >= 1

    def test_failure_history_limit_parameter(self, engine: PrintRecovery):
        for _ in range(10):
            engine.detect_failure(
                printer_name="p1", telemetry=_make_telemetry(connected=False)
            )
        limited = engine.get_failure_history(limit=3)
        assert len(limited) == 3

    def test_failure_history_newest_first(self, engine: PrintRecovery):
        engine.detect_failure(printer_name="first", telemetry=_make_telemetry(connected=False))
        engine.detect_failure(printer_name="second", telemetry=_make_telemetry(hotend_temp=320))
        history = engine.get_failure_history(limit=10)
        assert history[0].printer_name == "second"
        assert history[1].printer_name == "first"

    def test_no_telemetry_history_does_not_crash(self, engine: PrintRecovery):
        telemetry = _make_telemetry(
            x_position=0.0, x_expected=5.0, y_position=0.0, y_expected=0.0
        )
        failure = engine.detect_failure(
            printer_name="p1",
            telemetry=telemetry,
            telemetry_history=None,
        )
        assert failure is not None

    def test_missing_job_info_fields(self, engine: PrintRecovery):
        telemetry = _make_telemetry(hotend_temp=320)
        failure = engine.detect_failure(
            printer_name="p1", telemetry=telemetry, job_info={}
        )
        assert failure is not None
        assert failure.job_name is None
        assert failure.failed_layer is None

    def test_monitoring_check_notes_tracked(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.record_monitoring_check(
            session.session_id, passed=True, notes="Temp at 180C"
        )
        updated = engine.get_session(session.session_id)
        assert any("Temp at 180C" in step for step in updated.steps_completed)

    def test_cancel_cancelled_session_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.cancel_recovery(session.session_id)
        with pytest.raises(ValueError, match="terminal state"):
            engine.cancel_recovery(session.session_id)


# ---------------------------------------------------------------------------
# 18. Dataclass serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict() methods with correct enum conversion."""

    def test_failure_report_to_dict(self):
        report = FailureReport(
            failure_id="abc",
            failure_type=FailureType.THERMAL_RUNAWAY,
            detected_at="2024-01-01T00:00:00+00:00",
            printer_name="voron",
            severity="critical",
            evidence=["temp high"],
        )
        d = report.to_dict()
        assert d["failure_type"] == "thermal_runaway"
        assert d["failure_id"] == "abc"
        assert d["printer_name"] == "voron"
        assert d["severity"] == "critical"
        assert d["evidence"] == ["temp high"]

    def test_recovery_plan_to_dict(self):
        plan = RecoveryPlan(
            plan_id="plan-1",
            failure_id="fail-1",
            strategy=RecoveryStrategy.SAFE_ABORT,
            confidence=RecoveryConfidence.HIGH,
            risks=["part lost"],
        )
        d = plan.to_dict()
        assert d["strategy"] == "safe_abort"
        assert d["confidence"] == "high"
        assert d["risks"] == ["part lost"]

    def test_recovery_session_to_dict(self):
        plan = RecoveryPlan(
            plan_id="plan-1",
            failure_id="fail-1",
            strategy=RecoveryStrategy.SAFE_ABORT,
            confidence=RecoveryConfidence.HIGH,
        )
        failure = FailureReport(
            failure_id="fail-1",
            failure_type=FailureType.THERMAL_RUNAWAY,
            detected_at="2024-01-01T00:00:00+00:00",
            printer_name="p1",
        )
        session = RecoverySession(
            session_id="sess-1",
            plan=plan,
            failure=failure,
            status=RecoveryStatus.EXECUTING,
            started_at="2024-01-01T00:00:00+00:00",
        )
        d = session.to_dict()
        assert d["status"] == "executing"
        assert d["plan"]["strategy"] == "safe_abort"
        assert d["failure"]["failure_type"] == "thermal_runaway"
        assert d["session_id"] == "sess-1"

    def test_all_enum_values_serializable(self):
        """All enum values should be strings."""
        for ft in FailureType:
            assert isinstance(ft.value, str)
        for rs in RecoveryStrategy:
            assert isinstance(rs.value, str)
        for rc in RecoveryConfidence:
            assert isinstance(rc.value, str)
        for rs in RecoveryStatus:
            assert isinstance(rs.value, str)


# ---------------------------------------------------------------------------
# 19. Singleton pattern
# ---------------------------------------------------------------------------


class TestSingleton:
    """Module-level singleton via get_recovery_engine."""

    def test_singleton_returns_same_instance(self):
        import kiln.print_recovery as mod

        # Reset singleton for test isolation.
        mod._engine = None
        e1 = get_recovery_engine()
        e2 = get_recovery_engine()
        assert e1 is e2
        # Clean up.
        mod._engine = None

    def test_singleton_is_print_recovery(self):
        import kiln.print_recovery as mod

        mod._engine = None
        engine = get_recovery_engine()
        assert isinstance(engine, PrintRecovery)
        mod._engine = None


# ---------------------------------------------------------------------------
# 20. Event emission
# ---------------------------------------------------------------------------


class TestEventEmission:
    """Event emission on failure detection."""

    def test_event_emitted_on_failure(self, engine: PrintRecovery):
        """Event emission is best-effort -- should not raise even if bus unavailable."""
        telemetry = _make_telemetry(hotend_temp=320)
        # Should not raise even without a server event bus.
        failure = engine.detect_failure(printer_name="p1", telemetry=telemetry)
        assert failure is not None

    def test_event_emission_with_mock_bus(self, engine: PrintRecovery):
        with mock.patch("kiln.print_recovery.PrintRecovery._emit_event") as mock_emit:
            engine.detect_failure(
                printer_name="p1", telemetry=_make_telemetry(hotend_temp=320)
            )
            mock_emit.assert_called_once()


# ---------------------------------------------------------------------------
# 21. Strategy map completeness
# ---------------------------------------------------------------------------


class TestStrategyMap:
    """Every failure type has at least one strategy."""

    def test_all_failure_types_have_strategies(self, engine: PrintRecovery):
        for ft in FailureType:
            strategies = engine._recovery_strategies.get(ft, [])
            assert len(strategies) > 0, f"No strategies for {ft.value}"

    def test_stringing_maps_to_no_recovery(self, engine: PrintRecovery):
        strategies = engine._recovery_strategies[FailureType.STRINGING]
        assert RecoveryStrategy.NO_RECOVERY in strategies

    def test_thermal_runaway_only_abort(self, engine: PrintRecovery):
        strategies = engine._recovery_strategies[FailureType.THERMAL_RUNAWAY]
        assert strategies == [RecoveryStrategy.SAFE_ABORT]
