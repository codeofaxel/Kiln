"""End-to-end integration test for the failure recovery pipeline.

Walks the full chain that an agent (or eventually an autonomous
coordinator) runs to recover from a real print failure:

    telemetry -> detect_failure -> plan_recovery -> start_recovery
    -> record_monitoring_check x N -> complete_recovery

This test exists specifically because every individual stage has its
own unit tests, but no test verifies the chain works end-to-end with
realistic state transitions.  The audit at /tmp/patent_bundle_audit_2026_04_26.md
identified the absence of this test as the reason the
``session.failure_report`` typo could ship — the typo lived in the
hand-off between stages, where unit tests don't reach.

Coverage:

  * Critical-severity failure (thermal_runaway) requires 5 monitoring
    checks per patent claim 78.  ``complete_recovery`` raises
    :class:`MonitoringThresholdNotMet` when called early.
  * High-severity failure (layer_shift) requires 3 monitoring checks.
  * The pro-outcome side path is exercised end-to-end with kiln-pro
    mocked — the bug fix at recovery_tools.py:920 means the kiln-pro
    record_outcome IS actually called.
  * Vocabulary normalization: the failure_type string from the
    detector resolves to a canonical mode via failure_vocabulary —
    catches the bug class where two engines spell the same failure
    differently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.failure_vocabulary import normalize_failure_type
from kiln.print_recovery import (
    FailureType,
    MonitoringThresholdNotMet,
    PrintRecovery,
    RecoveryStatus,
    RecoveryStrategy,
    _CRITICAL_MONITORING_CHECKS,
    _MIN_MONITORING_CHECKS,
)


# ===================================================================
# Helpers
# ===================================================================


def _critical_thermal_telemetry() -> dict[str, Any]:
    """Telemetry that the engine classifies as critical thermal runaway."""
    return {
        "hotend_temp": 320.0,    # exceeds _THERMAL_RUNAWAY_ABS_MAX (300)
        "hotend_target": 220.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "connected": True,
        "filament_detected": True,
    }


def _layer_shift_telemetry() -> dict[str, Any]:
    """Telemetry that the engine classifies as a layer shift (high severity)."""
    return {
        "hotend_temp": 220.0,
        "hotend_target": 220.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "x_position": 100.0,
        "x_expected": 100.5,
        "y_position": 80.0,
        "y_expected": 82.0,
        "connected": True,
        "filament_detected": True,
    }


def _job_info() -> dict[str, Any]:
    return {
        "file_name": "benchy.gcode",
        "layer": 50,
        "total_layers": 200,
        "z_mm": 10.0,
        "material": "pla",
    }


# ===================================================================
# Critical severity — full pipeline + threshold enforcement
# ===================================================================


class TestPipelineE2ECritical:
    """Critical failure (thermal_runaway) requires 5 monitoring checks."""

    def test_full_pipeline_with_threshold_enforcement(self):
        engine = PrintRecovery()

        # Stage 1: detect from telemetry
        failure = engine.detect_failure(
            printer_name="bambu-a1",
            telemetry=_critical_thermal_telemetry(),
            job_info=_job_info(),
        )
        assert failure is not None
        assert failure.failure_type == FailureType.THERMAL_RUNAWAY
        assert failure.severity == "critical"
        # Vocabulary boundary: detector's value normalizes cleanly.
        assert normalize_failure_type(failure.failure_type.value) == "thermal_runaway"

        # Stage 2: plan recovery -> SAFE_ABORT (engine's only thermal strategy)
        plan = engine.plan_recovery(failure)
        assert plan.strategy == RecoveryStrategy.SAFE_ABORT
        assert plan.requires_confirmation is False  # HIGH-confidence safe abort

        # Stage 3: start session -> EXECUTING (no confirmation needed)
        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.EXECUTING
        # Patent claim 78: critical failures need 5 checks, not 3.
        assert session.monitoring_required == _CRITICAL_MONITORING_CHECKS

        # Stage 4a: try to complete BEFORE meeting threshold -> claim 79
        with pytest.raises(MonitoringThresholdNotMet) as excinfo:
            engine.complete_recovery(session.session_id, success=True)
        assert excinfo.value.deficit == _CRITICAL_MONITORING_CHECKS
        # Structured error carries everything the UI needs.
        struct = excinfo.value.to_dict()
        assert struct["code"] == "MONITORING_THRESHOLD_NOT_MET"
        assert struct["monitoring_required"] == _CRITICAL_MONITORING_CHECKS
        assert struct["deficit"] == _CRITICAL_MONITORING_CHECKS
        assert "record" in struct["remediation"].lower()

        # Stage 4b: record 5 passing checks
        for _ in range(_CRITICAL_MONITORING_CHECKS):
            session = engine.record_monitoring_check(
                session.session_id, passed=True
            )
        assert session.monitoring_passed == _CRITICAL_MONITORING_CHECKS

        # Stage 5: complete now succeeds
        session = engine.complete_recovery(
            session.session_id, success=True, notes="thermal recovered safely"
        )
        assert session.status == RecoveryStatus.COMPLETED
        assert session.completed_at is not None

    def test_failure_completion_bypasses_threshold(self):
        """Operator can give up at any time — failure path is always allowed."""
        engine = PrintRecovery()
        failure = engine.detect_failure(
            printer_name="bambu-a1",
            telemetry=_critical_thermal_telemetry(),
            job_info=_job_info(),
        )
        plan = engine.plan_recovery(failure)
        session = engine.start_recovery(plan, failure)

        # No monitoring checks recorded.  Failure completion still works.
        session = engine.complete_recovery(
            session.session_id, success=False, notes="gave up"
        )
        assert session.status == RecoveryStatus.FAILED


# ===================================================================
# High severity — 3 monitoring checks, layer shift requires confirmation
# ===================================================================


class TestPipelineE2EHigh:
    """High-severity failures (layer_shift) require 3 monitoring checks
    and human confirmation before executing."""

    def test_full_pipeline_with_confirmation(self):
        engine = PrintRecovery()
        failure = engine.detect_failure(
            printer_name="voron",
            telemetry=_layer_shift_telemetry(),
            job_info=_job_info(),
        )
        assert failure.failure_type == FailureType.LAYER_SHIFT
        assert failure.severity == "high"

        plan = engine.plan_recovery(failure)
        # Layer shift -> RESUME_FROM_LAYER, MEDIUM confidence -> needs confirm
        assert plan.strategy == RecoveryStrategy.RESUME_FROM_LAYER
        assert plan.requires_confirmation is True

        session = engine.start_recovery(plan, failure)
        assert session.status == RecoveryStatus.AWAITING_CONFIRMATION
        # High severity -> 3 checks (not 5).
        assert session.monitoring_required == _MIN_MONITORING_CHECKS

        # Operator confirms
        session = engine.confirm_recovery(session.session_id)
        assert session.status == RecoveryStatus.EXECUTING

        # Record 3 passing checks
        for _ in range(_MIN_MONITORING_CHECKS):
            session = engine.record_monitoring_check(
                session.session_id, passed=True
            )

        session = engine.complete_recovery(
            session.session_id, success=True, notes="resumed cleanly"
        )
        assert session.status == RecoveryStatus.COMPLETED


# ===================================================================
# Pro-outcome side path — closes the loop opened by the typo bug
# ===================================================================


class TestPipelineE2EProOutcome:
    """The recovery pipeline records outcomes to kiln-pro when installed.

    This is the regression test the audit recommended — the kiln-pro
    outcome side path was silently broken by the
    ``session.failure_report`` typo, and no test exercised the chain
    end-to-end.  Now we walk the full pipeline AND verify the outcome
    actually lands in kiln-pro's store.
    """

    def test_outcome_recorded_via_complete_print_recovery_tool(self):
        # Use the ACTUAL MCP tool, not a direct engine call, so the
        # kiln-pro side path is exercised.
        from kiln.plugins.recovery_tools import plugin
        from kiln import server as _srv

        captured_tools: dict = {}

        class _MockMCP:
            def tool(self):
                def _decorator(fn):
                    captured_tools[fn.__name__] = fn
                    return fn
                return _decorator

        plugin.register(_MockMCP())

        engine = PrintRecovery()
        # Inject our test engine into the singleton lookup.
        recorded: dict = {}

        def _capture_record_outcome(**kwargs):
            recorded.update(kwargs)
            return {"recorded_at": 0.0, "strategy": kwargs["strategy"]}

        fake_pro_features = MagicMock()
        fake_pro_features.recovery = MagicMock()

        # Walk the full pipeline through MCP tools so the kiln-pro
        # bridge logic actually runs.
        with patch("kiln.print_recovery.get_recovery_engine", return_value=engine):
            with patch.object(_srv, "_check_auth", return_value=None):
                # Stage 1: detect
                detect_result = captured_tools["detect_print_failure"](
                    printer_name="bambu-a1",
                    telemetry=_critical_thermal_telemetry(),
                    job_info=_job_info(),
                )
                assert detect_result["failure_detected"] is True
                failure_id = detect_result["failure"]["failure_id"]

                # Stage 2: plan
                plan_result = captured_tools["plan_failure_recovery"](
                    failure_id=failure_id,
                )
                assert plan_result["success"] is True
                plan_id = plan_result["plan"]["plan_id"]

                # Stage 3: start
                start_result = captured_tools["start_print_recovery"](
                    plan_id=plan_id, failure_id=failure_id,
                )
                assert start_result["success"] is True
                session_id = start_result["session"]["session_id"]

                # Stage 4: 5 monitoring checks (critical severity)
                for _ in range(_CRITICAL_MONITORING_CHECKS):
                    check_result = captured_tools["record_recovery_check"](
                        session_id=session_id, passed=True,
                    )
                    assert check_result["success"] is True

                # Stage 5: complete WITH kiln-pro mocked
                with patch.dict(
                    "sys.modules",
                    {
                        "kiln_pro": MagicMock(),
                        "kiln_pro.bridge": MagicMock(pro_features=fake_pro_features),
                        "kiln_pro.recovery": MagicMock(),
                        "kiln_pro.recovery.outcome_learning": MagicMock(
                            record_outcome=_capture_record_outcome,
                        ),
                    },
                ):
                    complete_result = captured_tools["complete_print_recovery"](
                        session_id=session_id,
                        success=True,
                        notes="pipeline e2e test",
                    )

        # The pipeline reached the end successfully.
        assert complete_result["success"] is True
        # AND the kiln-pro outcome was actually recorded.  Without the
        # session.failure_report -> session.failure fix, this would
        # be empty (record_outcome silently never called).
        assert recorded, (
            "kiln-pro record_outcome was not invoked end-to-end — the "
            "pipeline broke between stages.  This regression caught "
            "the session.failure_report typo when it shipped."
        )
        assert recorded["failure_type"] == "thermal_runaway"
        assert recorded["strategy"] == "safe_abort"
        assert recorded["printer_name"] == "bambu-a1"
        assert recorded["material_type"] == "pla"
        assert recorded["success"] is True
        assert complete_result["pro_outcome_recorded"] is True
