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
    _CRITICAL_MONITORING_CHECKS,
    _MIN_MONITORING_CHECKS,
    FailureReport,
    FailureType,
    MonitoringThresholdNotMet,
    PrintRecovery,
    RecoveryConfidence,
    RecoveryPlan,
    RecoverySession,
    RecoveryStatus,
    RecoveryStrategy,
    _required_monitoring_checks,
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


def _satisfy_monitoring_threshold(
    engine: PrintRecovery,
    session_id: str,
    *,
    extra: int = 0,
) -> None:
    """Record enough passing checks for ``complete_recovery(success=True)``.

    Patent claim 76 requires ``monitoring_passed >= monitoring_required``
    before COMPLETED.  Tests that exercise downstream behaviour (stats,
    cancellation rules, etc.) call this helper to satisfy the gate
    without re-asserting the threshold logic in every test.
    """
    session = engine.get_session(session_id)
    assert session is not None, f"Session not found: {session_id!r}"
    needed = max(0, session.monitoring_required - session.monitoring_passed)
    for _ in range(needed + extra):
        engine.record_monitoring_check(session_id, passed=True)


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
# 8. The free floor -- what every install answers a failure with
# ---------------------------------------------------------------------------
#
# Kiln Pro's plan (resume from a layer, restart with changed settings, the
# G-code for it) moved to kiln-pro and is asked for through
# kiln._pro_recovery_bridge; its own tests live there.  Here: the floor, and
# that a plan arriving through the bridge is the one that runs.


def _failure(failure_type: FailureType, **overrides: Any) -> FailureReport:
    base: dict[str, Any] = {
        "failure_id": "f-floor",
        "failure_type": failure_type,
        "detected_at": "2026-10-24T00:00:00+00:00",
        "printer_name": "test-printer",
        "failed_layer": 50,
        "total_layers": 200,
        "failure_z_mm": 10.0,
        "material_type": "pla",
        "telemetry_snapshot": {"hotend_temp": 215, "bed_temp": 65},
    }
    base.update(overrides)
    return FailureReport(**base)


def _stops(steps: list[str]) -> bool:
    """Whether a G-code sequence turns a heater off (stops the print)."""
    return any(line.split(";", 1)[0].strip().upper().startswith(("M104 S0", "M140 S0")) for line in steps)


@pytest.fixture()
def free(monkeypatch):
    """A free plan: kiln-pro is not here and the plan does not include Kiln Pro."""
    from kiln import _pro_recovery_bridge as bridge

    monkeypatch.setattr(bridge, "_local", lambda kind, failure: None)
    monkeypatch.setattr(bridge, "_plan_includes_pro", lambda: False)
    monkeypatch.setattr(bridge, "_lapsed_signin", lambda: "")


class TestTheFreeFloor:
    """What a free plan is told, for every failure."""

    @pytest.mark.parametrize("failure_type", list(FailureType))
    def test_every_failure_has_an_answer(self, engine: PrintRecovery, free, failure_type):
        plan = engine.plan_recovery(_failure(failure_type))
        said = plan.to_dict()
        assert said["floor"] is True
        assert plan.reason and plan.preparation_steps
        # The floor carries none of Kiln Pro's plan.
        assert plan.resume_layer is None and plan.parameter_adjustments == {}
        assert said["estimated_success_pct"] is None

    @pytest.mark.parametrize(
        "failure_type",
        [FailureType.FILAMENT_RUNOUT, FailureType.COMMUNICATION_LOSS, FailureType.STRINGING],
    )
    def test_a_print_that_may_still_be_fine_is_never_stopped(self, engine: PrintRecovery, free, failure_type):
        """A printer paused for filament, a printer still printing while
        Kiln's connection is down, cosmetic stringing: stopping would destroy
        a print that can still finish, so the floor sends nothing that stops it."""
        failure = _failure(failure_type)
        plan = engine.plan_recovery(failure)
        assert plan.strategy != RecoveryStrategy.SAFE_ABORT
        session = engine.start_recovery(plan, failure)
        assert not _stops(engine.get_recovery_steps(session.session_id))

    @pytest.mark.parametrize(
        "failure_type",
        [FailureType.LAYER_SHIFT, FailureType.ADHESION_FAILURE, FailureType.BLOB_DETECTED,
         FailureType.POWER_LOSS, FailureType.NOZZLE_CLOG, FailureType.WARPING],
    )
    def test_a_lost_print_is_stopped_after_the_person_says_so(self, engine: PrintRecovery, free, failure_type):
        failure = _failure(failure_type)
        plan = engine.plan_recovery(failure)
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT and plan.requires_confirmation is True
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.AWAITING_CONFIRMATION
        assert _stops(engine.get_recovery_steps(session.session_id))

    @pytest.mark.parametrize("failure_type", [FailureType.THERMAL_RUNAWAY, FailureType.SPAGHETTI])
    def test_a_runaway_heater_or_spaghetti_stops_without_waiting(self, engine: PrintRecovery, free, failure_type):
        """Waiting on a confirmation while a heater runs away, or while a
        failed print wraps the hot end, can cost the machine."""
        failure = _failure(failure_type)
        plan = engine.plan_recovery(failure)
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT and plan.requires_confirmation is False
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.EXECUTING
        steps = engine.get_recovery_steps(session.session_id)
        assert "M104 S0" in steps and "M140 S0" in steps and "M84" in steps

    def test_a_runaway_heater_keeps_every_safety_line(self, engine: PrintRecovery, free):
        steps = " ".join(engine.plan_recovery(_failure(FailureType.THERMAL_RUNAWAY)).preparation_steps)
        assert "IMMEDIATE: Execute emergency stop if temperature is rising" in steps
        assert "cool completely before touching" in steps
        assert "heater and its temperature sensor" in steps
        assert "Do not print on it again" in steps

    def test_a_clog_warns_before_anyone_touches_the_nozzle(self, engine: PrintRecovery, free):
        from kiln.hotend_safety import WARNING_STEP

        assert engine.plan_recovery(_failure(FailureType.NOZZLE_CLOG)).preparation_steps[0] == WARNING_STEP

    def test_a_power_cut_says_what_the_printer_can_do_itself(self, engine: PrintRecovery, free):
        """The printer's own power-loss recovery is the printer's, and it covers
        a print run from its own memory card, never one streamed over USB."""
        steps = " ".join(engine.plan_recovery(_failure(FailureType.POWER_LOSS)).preparation_steps)
        assert "its own memory card" in steps and "power-loss recovery" in steps

    def test_what_kiln_pro_adds_is_said_once_and_only_where_it_adds_something(self, engine: PrintRecovery, free):
        shift = engine.plan_recovery(_failure(FailureType.LAYER_SHIFT)).to_dict()
        assert "Kiln Pro" in shift["pro_adds"] and shift["required_tier"] == "pro"
        assert shift["upgrade_url"].startswith("https://kiln3d.com/pricing")
        # A heater that ran away is answered with the safe stop at every tier:
        # nothing is pitched at that moment.
        runaway = engine.plan_recovery(_failure(FailureType.THERMAL_RUNAWAY)).to_dict()
        assert "pro_adds" not in runaway and "upgrade_url" not in runaway

    def test_a_sign_in_that_ended_is_told_to_sign_back_in_never_to_upgrade(self, engine: PrintRecovery, free, monkeypatch):
        from kiln import _pro_recovery_bridge as bridge

        monkeypatch.setattr(bridge, "_lapsed_signin", lambda: "Your Kiln session has expired.")
        said = engine.plan_recovery(_failure(FailureType.LAYER_SHIFT)).to_dict()
        assert said["sign_in_again"] == "Your Kiln session has expired."
        assert "pro_adds" not in said and "upgrade_url" not in said


class TestKilnProsPlan:
    """A plan that arrives through the bridge is the plan that runs."""

    _RESUME = {
        "success": True, "status": "ok", "known": True, "kind": "session",
        "plan": {
            "plan_id": "elsewhere", "failure_id": "elsewhere", "strategy": "resume_from_layer",
            "confidence": "medium", "resume_layer": 47, "layer_overlap": 3, "preparation_steps": ["Resume"],
            "parameter_adjustments": {"resume_z_mm": 9.4, "overlap_layers": 3}, "estimated_success_pct": 60.0,
            "estimated_time_minutes": 306.0, "risks": ["Weak seam"], "requires_confirmation": True,
            "reason": "Resume from layer 47",
        },
        "gcode": ["M104 S215", "G91", "G1 Z5 F600", "G90", "G28 X Y", "; Resume from layer 47"],
    }

    def _asked(self, monkeypatch, answer: dict):
        from kiln import _pro_recovery_bridge as bridge

        sent: list[tuple[str, dict]] = []

        def ask(kind, failure):
            sent.append((kind, failure))
            return bridge.Asked(answer=answer, included=True)

        monkeypatch.setattr(bridge, "ask", ask)
        return sent

    def test_its_plan_and_its_g_code_run(self, engine: PrintRecovery, monkeypatch):
        sent = self._asked(monkeypatch, self._RESUME)
        failure = _failure(FailureType.LAYER_SHIFT)
        plan = engine.plan_recovery(failure)
        said = plan.to_dict()
        assert said["strategy"] == "resume_from_layer" and said["resume_layer"] == 47
        assert said["failure_id"] == "f-floor" and "floor" not in said
        session = engine.start_recovery(plan, failure)
        assert engine.get_recovery_steps(session.session_id) == self._RESUME["gcode"]
        # Only the failure's own facts were sent: no printer, job or path.
        kind, facts = sent[0]
        assert kind == "session" and facts["failure_type"] == "layer_shift"
        assert "test-printer" not in str(facts) and "printer_name" not in facts

    def test_a_safe_abort_is_built_here_with_the_proven_park_route(self, engine: PrintRecovery, monkeypatch):
        from kiln.printers.safe_motion import TravelPlan

        abort = {**self._RESUME, "plan": {**self._RESUME["plan"], "strategy": "safe_abort", "confidence": "high"},
                 "gcode": None}
        self._asked(monkeypatch, abort)
        failure = _failure(FailureType.BLOB_DETECTED)
        plan = engine.plan_recovery(failure)
        route = TravelPlan(ok=True, reason="r", strategy="direct", waypoints=[(0.0, 0.0), (215.0, 215.0)])
        session = engine.start_recovery(plan, failure, park_route=route)
        steps = engine.get_recovery_steps(session.session_id)
        assert any("park clear" in line for line in steps) and steps[-1] == "M84"

    def test_a_plan_that_arrives_without_its_g_code_sends_nothing(self, engine: PrintRecovery, monkeypatch):
        self._asked(monkeypatch, {**self._RESUME, "gcode": None})
        failure = _failure(FailureType.LAYER_SHIFT)
        plan = engine.plan_recovery(failure)
        session = engine.start_recovery(plan, failure)
        assert engine.get_recovery_steps(session.session_id) == []

    def test_an_answer_that_is_not_a_plan_is_the_floor_and_says_why(self, engine: PrintRecovery, monkeypatch):
        self._asked(monkeypatch, {"success": True, "known": True, "plan": {"strategy": "teleport"}})
        said = engine.plan_recovery(_failure(FailureType.LAYER_SHIFT)).to_dict()
        assert said["floor"] is True and said["strategy"] == "safe_abort"
        assert said["pro_plan_missing"] and "pro_adds" not in said

    def test_a_paying_caller_whose_plan_did_not_arrive_is_told_why_not_pitched(self, engine: PrintRecovery, monkeypatch):
        from kiln import _pro_recovery_bridge as bridge
        from kiln.served_answer import Miss

        monkeypatch.setattr(bridge, "ask", lambda kind, failure: bridge.Asked(included=True, miss=Miss("offline")))
        said = engine.plan_recovery(_failure(FailureType.LAYER_SHIFT)).to_dict()
        assert said["floor"] is True and said["why"] == "offline"
        assert "this computer is offline" in said["pro_plan_missing"]
        assert "pro_adds" not in said and "upgrade_url" not in said


# ---------------------------------------------------------------------------
# 11b. Mid-print motion safety -- the safe abort runs with a partial print on
# the bed, so it may not home Z or move in X/Y before a lift.  (Kiln Pro's
# resume and restart sequences are held to the same in kiln-pro.)
# ---------------------------------------------------------------------------


def _sequence_for(strategy: RecoveryStrategy) -> list[str]:
    """Recovery gcode for *strategy* via the real engine path, for a plan
    that carries no G-code of its own."""
    engine = PrintRecovery()
    failure = FailureReport(
        failure_id="f-motion",
        failure_type=FailureType.POWER_LOSS,
        detected_at="2026-08-28T00:00:00+00:00",
        printer_name="test-printer",
        telemetry_snapshot={"hotend_temp": 210, "bed_temp": 60},
    )
    plan = RecoveryPlan(
        plan_id="p-motion",
        failure_id="f-motion",
        strategy=strategy,
        confidence=RecoveryConfidence.MEDIUM,
        resume_layer=42,
        parameter_adjustments={"resume_z_mm": 8.4, "overlap_layers": 2},
        requires_confirmation=False,
    )
    session = engine.start_recovery(plan, failure)
    return engine.get_recovery_steps(session.session_id)


_MID_PRINT_STRATEGIES = [
    RecoveryStrategy.RESUME_FROM_LAYER,
    RecoveryStrategy.PARTIAL_RECOVERY,
    RecoveryStrategy.SAFE_ABORT,
    RecoveryStrategy.RESTART_WITH_COMPENSATION,
]


class TestMidPrintMotionSafety:
    """No recovery sequence may home Z with a part on the bed."""

    @pytest.mark.parametrize("strategy", _MID_PRINT_STRATEGIES)
    def test_no_bare_g28(self, strategy: RecoveryStrategy):
        commands = _sequence_for(strategy)
        for cmd in commands:
            code = cmd.split(";", 1)[0].strip().upper()
            assert code != "G28", (
                f"{strategy.value}: bare G28 homes ALL axes — Z descends "
                f"toward a bed that still carries the partial print"
            )
            if code.startswith("G28"):
                assert "Z" not in code, (
                    f"{strategy.value}: {cmd!r} homes Z with a part on the bed"
                )

    @pytest.mark.parametrize("strategy", _MID_PRINT_STRATEGIES)
    def test_z_lift_precedes_homing(self, strategy: RecoveryStrategy):
        commands = _sequence_for(strategy)
        home_idx = next(
            (
                i
                for i, c in enumerate(commands)
                if c.split(";", 1)[0].strip().upper().startswith("G28")
            ),
            None,
        )
        if home_idx is None:
            return  # No homing in this sequence — nothing to order
        lifted_before = any(
            c.split(";", 1)[0].strip().upper().startswith(("G0 Z", "G1 Z"))
            for c in commands[:home_idx]
        )
        assert lifted_before, (
            f"{strategy.value}: homing at index {home_idx} with no Z lift "
            f"before it — the nozzle travels at part height: {commands}"
        )

    @pytest.mark.parametrize(
        "strategy",
        [RecoveryStrategy.RESUME_FROM_LAYER, RecoveryStrategy.PARTIAL_RECOVERY, RecoveryStrategy.RESTART_WITH_COMPENSATION],
    )
    def test_a_plan_with_no_g_code_of_its_own_moves_nothing(self, strategy: RecoveryStrategy):
        """Motion is never invented for a strategy whose planner did not write
        it: a hand-built or half-arrived plan sends only comments."""
        assert all(line.startswith(";") for line in _sequence_for(strategy))

    def test_abort_threads_a_proven_park_route(self):
        # A caller with live-machine facts hands the engine a PROVEN park
        # route; the abort parks along it, before M84 (after which
        # nothing moves).  Without one, the abort is byte-identical to
        # the legacy stay-at-home sequence.
        from kiln.printers.safe_motion import TravelPlan

        engine = PrintRecovery()
        failure = FailureReport(
            failure_id="f-park",
            failure_type=FailureType.THERMAL_RUNAWAY,
            detected_at="2026-08-29T00:00:00+00:00",
            printer_name="test-printer",
        )
        plan = RecoveryPlan(
            plan_id="p-park",
            failure_id="f-park",
            strategy=RecoveryStrategy.SAFE_ABORT,
            confidence=RecoveryConfidence.HIGH,
            requires_confirmation=False,
        )
        route = TravelPlan(
            ok=True, reason="r", strategy="direct",
            waypoints=[(0.0, 0.0), (215.0, 215.0)],
        )
        session = engine.start_recovery(plan, failure, park_route=route)
        steps = engine.get_recovery_steps(session.session_id)
        park_idx = next(i for i, c in enumerate(steps) if "park clear" in c)
        assert park_idx < steps.index("M84")

        # No route -> no park line, same as before the wiring.
        no_route = _sequence_for(RecoveryStrategy.SAFE_ABORT)
        assert not any("park" in c.lower() for c in no_route)

    def test_no_recovery_gcode(self, engine: PrintRecovery, free):
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

        # 4. Monitor — thermal_runaway is critical so the session was
        # provisioned with the 5-check threshold per patent claim 78.
        assert session.monitoring_required == _CRITICAL_MONITORING_CHECKS
        session = engine.record_monitoring_check(session.session_id, passed=True, notes="Temps falling")
        assert session.status == RecoveryStatus.MONITORING
        assert session.monitoring_checks == 1
        assert session.monitoring_passed == 1

        for note in ("Bed cooling", "All clear", "Steppers off", "No smoke"):
            session = engine.record_monitoring_check(session.session_id, passed=True, notes=note)
        assert session.monitoring_passed == _CRITICAL_MONITORING_CHECKS

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

        _satisfy_monitoring_threshold(engine, session.session_id)
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

        _satisfy_monitoring_threshold(engine, s1.session_id)
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
        _satisfy_monitoring_threshold(engine, session.session_id)
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
        _satisfy_monitoring_threshold(engine, session.session_id)
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
                severity="critical",
            )
            plan = engine.plan_recovery(failure)
            session = engine.start_recovery(plan, failure)
            # Pre-record monitoring checks for the half that will complete.
            if i % 2 == 0:
                _satisfy_monitoring_threshold(engine, session.session_id)
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
        _satisfy_monitoring_threshold(engine, session.session_id)
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
        _satisfy_monitoring_threshold(engine, s1.session_id)
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
        _satisfy_monitoring_threshold(engine, s1.session_id)
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
        _satisfy_monitoring_threshold(engine, session.session_id)
        engine.complete_recovery(session.session_id, success=True)

        with pytest.raises(ValueError, match="terminal state"):
            engine.complete_recovery(session.session_id, success=True)

    def test_monitor_nonexistent_raises(self, engine: PrintRecovery):
        with pytest.raises(ValueError, match="not found"):
            engine.record_monitoring_check("nonexistent", passed=True)

    def test_monitor_completed_session_raises(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        _satisfy_monitoring_threshold(engine, session.session_id)
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


# ---------------------------------------------------------------------------
# 20. Monitoring-checks threshold (patent KILN-003 claims 76, 78, 79)
# ---------------------------------------------------------------------------


class TestMonitoringThreshold:
    """Severity-aware monitoring-checks threshold enforcement."""

    def test_required_resolver_critical_returns_five(self):
        assert _required_monitoring_checks("critical") == _CRITICAL_MONITORING_CHECKS
        assert _CRITICAL_MONITORING_CHECKS == 5

    def test_required_resolver_default_returns_three(self):
        for sev in ("high", "medium", "low", "", None, "unknown"):
            assert _required_monitoring_checks(sev) == _MIN_MONITORING_CHECKS
        assert _MIN_MONITORING_CHECKS == 3

    def test_required_resolver_is_case_insensitive(self):
        assert _required_monitoring_checks("CRITICAL") == _CRITICAL_MONITORING_CHECKS
        assert _required_monitoring_checks("  Critical  ") == _CRITICAL_MONITORING_CHECKS

    def test_critical_session_provisions_five_checks(self, engine: PrintRecovery):
        # Thermal runaway is severity=critical per the detector.
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        assert failure.severity == "critical"
        session = engine.start_recovery(plan, failure)
        assert session.monitoring_required == _CRITICAL_MONITORING_CHECKS

    def test_default_session_provisions_three_checks(self, engine: PrintRecovery):
        # Filament runout is severity=medium per the detector.
        failure, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        assert failure.severity != "critical"
        session = engine.start_recovery(plan, failure)
        assert session.monitoring_required == _MIN_MONITORING_CHECKS

    def test_session_to_dict_includes_monitoring_required(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        assert session.to_dict()["monitoring_required"] == _CRITICAL_MONITORING_CHECKS

    def test_complete_below_threshold_raises_structured_error(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        # Two passing checks against a critical session that wants five.
        engine.record_monitoring_check(session.session_id, passed=True)
        engine.record_monitoring_check(session.session_id, passed=True)
        with pytest.raises(MonitoringThresholdNotMet) as excinfo:
            engine.complete_recovery(session.session_id, success=True)
        err = excinfo.value
        assert err.monitoring_passed == 2
        assert err.monitoring_required == _CRITICAL_MONITORING_CHECKS
        assert err.severity == "critical"
        assert err.session_id == session.session_id
        # Subclasses ValueError so existing handlers still catch it.
        assert isinstance(err, ValueError)

    def test_failed_completion_bypasses_threshold(self, engine: PrintRecovery):
        # Operator may give up on a recovery attempt at any time.
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        # Zero monitoring checks recorded.
        session = engine.complete_recovery(session.session_id, success=False, notes="abandoning")
        assert session.status == RecoveryStatus.FAILED

    def test_failed_checks_do_not_count_toward_threshold(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(filament_detected=False))
        session = engine.start_recovery(plan, failure)
        # Plan requires confirmation; satisfy that first.
        if session.status == RecoveryStatus.AWAITING_CONFIRMATION:
            engine.confirm_recovery(session.session_id)
        for _ in range(5):
            engine.record_monitoring_check(session.session_id, passed=False, notes="still bad")
        with pytest.raises(MonitoringThresholdNotMet) as excinfo:
            engine.complete_recovery(session.session_id, success=True)
        assert excinfo.value.monitoring_passed == 0
        assert excinfo.value.monitoring_checks == 5

    def test_threshold_error_to_dict_carries_all_fields(self, engine: PrintRecovery):
        failure, plan = _detect_and_plan(engine, _make_telemetry(hotend_temp=320))
        session = engine.start_recovery(plan, failure)
        engine.record_monitoring_check(session.session_id, passed=True)
        try:
            engine.complete_recovery(session.session_id, success=True)
        except MonitoringThresholdNotMet as exc:
            d = exc.to_dict()
            assert d["code"] == "MONITORING_THRESHOLD_NOT_MET"
            assert d["session_id"] == session.session_id
            assert d["monitoring_required"] == _CRITICAL_MONITORING_CHECKS
            assert d["monitoring_passed"] == 1
            assert d["deficit"] == _CRITICAL_MONITORING_CHECKS - 1
            assert d["severity"] == "critical"
            assert "passing checks recorded" in d["message"]
        else:
            pytest.fail("MonitoringThresholdNotMet was not raised")

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


class TestEveryFailureHasAFloor:
    """Every failure the detectors can raise has a floor answer of its own."""

    def test_every_failure_type_is_worded(self):
        from kiln import recovery_floor

        for ft in FailureType:
            assert ft.value in recovery_floor.HAPPENED, ft.value

    def test_a_heater_that_ran_away_is_only_ever_stopped(self, engine: PrintRecovery, free):
        plan = engine.plan_recovery(_failure(FailureType.THERMAL_RUNAWAY))
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT
