"""Tests for kiln.recovery -- print job recovery system.

Coverage areas:
- CheckpointData creation, to_dict, edge cases (zero values, empty extra)
- RecoveryCheckpoint creation, serialization, auto-generated fields
- RecoveryRecommendation and RecoveryResult serialization
- RecoveryManager initialization (defaults, env var config, singleton)
- Checkpoint lifecycle: save, get_latest, get_all, clear
- plan_recovery for every FailureType enum value
- Power loss specific planning (with/without checkpoint)
- execute_recovery success/failure paths, retry tracking
- Retry counting: get_retry_count, reset_retries, max retries exceeded
- list_recoverable_jobs filtering
- is_safety_critical / is_print_compromised classification
- Thread safety of concurrent checkpoint saves
- Module-level convenience functions (get_recovery_manager, save_checkpoint, plan_recovery)
"""

from __future__ import annotations

import os
import threading
from unittest import mock

import pytest

from kiln.recovery import (
    CheckpointData,
    FailureType,
    RecoveryCheckpoint,
    RecoveryError,
    RecoveryManager,
    RecoveryRecommendation,
    RecoveryResult,
    RecoveryStrategy,
    _PRINT_COMPROMISED_FAILURES,
    _SAFETY_CRITICAL_FAILURES,
    get_recovery_manager,
    plan_recovery,
    save_checkpoint,
)


# ---------------------------------------------------------------------------
# 1. CheckpointData
# ---------------------------------------------------------------------------


class TestCheckpointData:
    """Covers creation, defaults, to_dict, zero values, and extra dict."""

    def test_default_values(self):
        data = CheckpointData()
        assert data.z_height_mm == 0.0
        assert data.layer_number == 0
        assert data.hotend_temp_c == 0.0
        assert data.bed_temp_c == 0.0
        assert data.filament_used_mm == 0.0
        assert data.fan_speed_pct == 0.0
        assert data.flow_rate_pct == 100.0
        assert data.extra == {}

    def test_explicit_values(self):
        data = CheckpointData(
            z_height_mm=22.4,
            layer_number=112,
            hotend_temp_c=210.0,
            bed_temp_c=60.0,
            filament_used_mm=3400.0,
            fan_speed_pct=80.0,
            flow_rate_pct=95.0,
            extra={"mmu_slot": 2},
        )
        assert data.z_height_mm == 22.4
        assert data.layer_number == 112
        assert data.extra == {"mmu_slot": 2}

    def test_to_dict_returns_all_fields(self):
        data = CheckpointData(z_height_mm=1.5, layer_number=10, extra={"k": "v"})
        d = data.to_dict()
        assert d["z_height_mm"] == 1.5
        assert d["layer_number"] == 10
        assert d["hotend_temp_c"] == 0.0
        assert d["bed_temp_c"] == 0.0
        assert d["filament_used_mm"] == 0.0
        assert d["fan_speed_pct"] == 0.0
        assert d["flow_rate_pct"] == 100.0
        assert d["extra"] == {"k": "v"}

    def test_to_dict_extra_is_copy(self):
        original = {"sensor": 42}
        data = CheckpointData(extra=original)
        d = data.to_dict()
        d["extra"]["sensor"] = 999
        assert data.extra["sensor"] == 42

    def test_zero_values(self):
        data = CheckpointData(
            z_height_mm=0.0,
            layer_number=0,
            hotend_temp_c=0.0,
            bed_temp_c=0.0,
            filament_used_mm=0.0,
        )
        d = data.to_dict()
        assert all(d[k] == 0.0 for k in ["z_height_mm", "hotend_temp_c", "bed_temp_c", "filament_used_mm"])
        assert d["layer_number"] == 0


# ---------------------------------------------------------------------------
# 2. RecoveryCheckpoint
# ---------------------------------------------------------------------------


class TestRecoveryCheckpoint:
    """Covers creation, auto-generated fields, and serialization."""

    def test_creation_with_required_fields(self):
        cp = RecoveryCheckpoint(job_id="j1", printer_id="p1")
        assert cp.job_id == "j1"
        assert cp.printer_id == "p1"
        assert len(cp.checkpoint_id) == 16
        assert cp.created_at > 0
        assert cp.phase == ""
        assert cp.progress_pct == 0.0
        assert isinstance(cp.data, CheckpointData)

    def test_unique_checkpoint_ids(self):
        ids = {RecoveryCheckpoint(job_id="j", printer_id="p").checkpoint_id for _ in range(50)}
        assert len(ids) == 50

    def test_to_dict(self):
        data = CheckpointData(z_height_mm=5.0, layer_number=25)
        cp = RecoveryCheckpoint(
            job_id="job-1",
            printer_id="prusa-mk4",
            checkpoint_id="abc123",
            created_at=1000.0,
            phase="printing",
            progress_pct=45.0,
            data=data,
        )
        d = cp.to_dict()
        assert d["job_id"] == "job-1"
        assert d["printer_id"] == "prusa-mk4"
        assert d["checkpoint_id"] == "abc123"
        assert d["created_at"] == 1000.0
        assert d["phase"] == "printing"
        assert d["progress_pct"] == 45.0
        assert d["data"]["z_height_mm"] == 5.0
        assert d["data"]["layer_number"] == 25


# ---------------------------------------------------------------------------
# 3. RecoveryRecommendation serialization
# ---------------------------------------------------------------------------


class TestRecoveryRecommendation:
    """Covers to_dict with and without checkpoint."""

    def test_to_dict_without_checkpoint(self):
        rec = RecoveryRecommendation(
            job_id="j1",
            failure_type=FailureType.POWER_LOSS,
            recommended_strategy=RecoveryStrategy.RESTART_FROM_BEGINNING,
            alternative_strategies=[RecoveryStrategy.MANUAL_INTERVENTION],
            estimated_waste_pct=100.0,
            risk_assessment="Power loss.",
            auto_recoverable=False,
            safety_critical=False,
        )
        d = rec.to_dict()
        assert d["job_id"] == "j1"
        assert d["failure_type"] == "power_loss"
        assert d["recommended_strategy"] == "restart_from_beginning"
        assert d["alternative_strategies"] == ["manual_intervention"]
        assert d["checkpoint"] is None
        assert d["estimated_waste_pct"] == 100.0
        assert d["auto_recoverable"] is False
        assert d["safety_critical"] is False

    def test_to_dict_with_checkpoint(self):
        cp = RecoveryCheckpoint(job_id="j1", printer_id="p1", checkpoint_id="cp1", created_at=1.0)
        rec = RecoveryRecommendation(
            job_id="j1",
            failure_type=FailureType.FILAMENT_RUNOUT,
            recommended_strategy=RecoveryStrategy.PAUSE_AND_INTERVENE,
            checkpoint=cp,
        )
        d = rec.to_dict()
        assert d["checkpoint"] is not None
        assert d["checkpoint"]["checkpoint_id"] == "cp1"


# ---------------------------------------------------------------------------
# 4. RecoveryResult serialization
# ---------------------------------------------------------------------------


class TestRecoveryResult:
    """Covers to_dict with success and error scenarios."""

    def test_to_dict_success(self):
        res = RecoveryResult(
            job_id="j1",
            strategy_used=RecoveryStrategy.RESUME_FROM_CHECKPOINT,
            success=True,
            resumed_from_checkpoint=True,
            time_saved_s=45.0,
        )
        d = res.to_dict()
        assert d["job_id"] == "j1"
        assert d["strategy_used"] == "resume_from_checkpoint"
        assert d["success"] is True
        assert d["resumed_from_checkpoint"] is True
        assert d["time_saved_s"] == 45.0
        assert d["error"] is None

    def test_to_dict_with_error(self):
        res = RecoveryResult(
            job_id="j1",
            strategy_used=RecoveryStrategy.ABORT,
            success=False,
            error="Something went wrong",
        )
        d = res.to_dict()
        assert d["success"] is False
        assert d["error"] == "Something went wrong"


# ---------------------------------------------------------------------------
# 5. RecoveryError
# ---------------------------------------------------------------------------


class TestRecoveryError:
    """Covers RecoveryError with and without cause."""

    def test_message(self):
        err = RecoveryError("test error")
        assert str(err) == "test error"
        assert err.cause is None

    def test_with_cause(self):
        cause = ValueError("root cause")
        err = RecoveryError("wrapper", cause=cause)
        assert err.cause is cause


# ---------------------------------------------------------------------------
# 6. RecoveryManager initialization
# ---------------------------------------------------------------------------


class TestRecoveryManagerInit:
    """Covers default values, constructor overrides, and env var config."""

    def test_default_max_retries(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = RecoveryManager()
            assert mgr._max_retries == 3

    def test_max_retries_from_env(self):
        with mock.patch.dict(os.environ, {"KILN_RECOVERY_MAX_RETRIES": "7"}, clear=True):
            mgr = RecoveryManager()
            assert mgr._max_retries == 7

    def test_max_retries_from_constructor(self):
        mgr = RecoveryManager(max_retries=5)
        assert mgr._max_retries == 5

    def test_default_checkpoint_interval(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = RecoveryManager()
            assert mgr._checkpoint_interval_s == 30.0

    def test_checkpoint_interval_from_env(self):
        with mock.patch.dict(os.environ, {"KILN_RECOVERY_CHECKPOINT_INTERVAL": "10.5"}, clear=True):
            mgr = RecoveryManager()
            assert mgr._checkpoint_interval_s == 10.5

    def test_checkpoint_interval_from_constructor(self):
        mgr = RecoveryManager(checkpoint_interval_s=15.0)
        assert mgr._checkpoint_interval_s == 15.0

    def test_empty_initial_state(self):
        mgr = RecoveryManager()
        assert mgr._checkpoints == {}
        assert mgr._retry_counts == {}


# ---------------------------------------------------------------------------
# 7. save_checkpoint / get_latest_checkpoint / get_all / clear
# ---------------------------------------------------------------------------


class TestCheckpointLifecycle:
    """Covers save, get_latest, get_all, clear, and _build_checkpoint_data."""

    def test_save_and_get_latest(self):
        mgr = RecoveryManager()
        cp = mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        assert cp.job_id == "j1"
        assert cp.printer_id == "p1"
        assert cp.phase == "printing"
        assert cp.progress_pct == 50.0

        latest = mgr.get_latest_checkpoint("j1")
        assert latest is not None
        assert latest.checkpoint_id == cp.checkpoint_id

    def test_get_latest_returns_most_recent(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "heating", 0.0)
        cp2 = mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        latest = mgr.get_latest_checkpoint("j1")
        assert latest is not None
        assert latest.checkpoint_id == cp2.checkpoint_id

    def test_get_latest_no_checkpoints(self):
        mgr = RecoveryManager()
        assert mgr.get_latest_checkpoint("nonexistent") is None

    def test_get_all_checkpoints_ordered(self):
        mgr = RecoveryManager()
        cp1 = mgr.save_checkpoint("j1", "p1", "heating", 0.0)
        cp2 = mgr.save_checkpoint("j1", "p1", "printing", 25.0)
        cp3 = mgr.save_checkpoint("j1", "p1", "printing", 75.0)
        all_cps = mgr.get_all_checkpoints("j1")
        assert len(all_cps) == 3
        assert all_cps[0].checkpoint_id == cp1.checkpoint_id
        assert all_cps[2].checkpoint_id == cp3.checkpoint_id

    def test_get_all_checkpoints_empty(self):
        mgr = RecoveryManager()
        assert mgr.get_all_checkpoints("nonexistent") == []

    def test_get_all_returns_copy(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        all_cps = mgr.get_all_checkpoints("j1")
        all_cps.clear()
        assert len(mgr.get_all_checkpoints("j1")) == 1

    def test_clear_checkpoints(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        removed = mgr.clear_checkpoints("j1")
        assert removed == 2
        assert mgr.get_latest_checkpoint("j1") is None

    def test_clear_checkpoints_nonexistent(self):
        mgr = RecoveryManager()
        removed = mgr.clear_checkpoints("nonexistent")
        assert removed == 0

    def test_clear_does_not_affect_other_jobs(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.save_checkpoint("j2", "p1", "printing", 20.0)
        mgr.clear_checkpoints("j1")
        assert mgr.get_latest_checkpoint("j1") is None
        assert mgr.get_latest_checkpoint("j2") is not None

    def test_save_with_state_data_known_keys(self):
        mgr = RecoveryManager()
        cp = mgr.save_checkpoint(
            "j1", "p1", "printing", 50.0,
            state_data={
                "z_height_mm": 22.4,
                "layer_number": 112,
                "hotend_temp_c": 210.0,
                "bed_temp_c": 60.0,
                "filament_used_mm": 3400.0,
                "fan_speed_pct": 80.0,
                "flow_rate_pct": 95.0,
            },
        )
        assert cp.data.z_height_mm == 22.4
        assert cp.data.layer_number == 112
        assert cp.data.hotend_temp_c == 210.0
        assert cp.data.bed_temp_c == 60.0
        assert cp.data.filament_used_mm == 3400.0
        assert cp.data.fan_speed_pct == 80.0
        assert cp.data.flow_rate_pct == 95.0
        assert cp.data.extra == {}

    def test_save_with_state_data_extra_keys(self):
        mgr = RecoveryManager()
        cp = mgr.save_checkpoint(
            "j1", "p1", "printing", 50.0,
            state_data={
                "z_height_mm": 10.0,
                "mmu_slot": 2,
                "enclosure_temp_c": 35.0,
            },
        )
        assert cp.data.z_height_mm == 10.0
        assert cp.data.extra == {"mmu_slot": 2, "enclosure_temp_c": 35.0}

    def test_save_with_no_state_data(self):
        mgr = RecoveryManager()
        cp = mgr.save_checkpoint("j1", "p1", "heating", 0.0)
        assert cp.data.z_height_mm == 0.0
        assert cp.data.extra == {}

    def test_save_with_empty_state_data(self):
        mgr = RecoveryManager()
        cp = mgr.save_checkpoint("j1", "p1", "heating", 0.0, state_data={})
        assert cp.data.z_height_mm == 0.0
        assert cp.data.extra == {}


# ---------------------------------------------------------------------------
# 8. plan_recovery — every FailureType
# ---------------------------------------------------------------------------


class TestPlanRecoveryUserCancelled:
    def test_user_cancelled_recommends_abort(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.USER_CANCELLED)
        assert rec.recommended_strategy == RecoveryStrategy.ABORT
        assert rec.alternative_strategies == []
        assert rec.auto_recoverable is False
        assert rec.safety_critical is False

    def test_user_cancelled_waste_equals_progress(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 45.0)
        rec = mgr.plan_recovery("j1", FailureType.USER_CANCELLED)
        assert rec.estimated_waste_pct == 45.0


class TestPlanRecoveryThermalRunaway:
    def test_thermal_runaway_recommends_emergency_stop(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.THERMAL_RUNAWAY)
        assert rec.recommended_strategy == RecoveryStrategy.EMERGENCY_STOP
        assert RecoveryStrategy.ABORT in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is False
        assert rec.safety_critical is True

    def test_thermal_runaway_risk_mentions_thermistors(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.THERMAL_RUNAWAY)
        assert "thermistor" in rec.risk_assessment.lower()


class TestPlanRecoveryBedAdhesion:
    def test_bed_adhesion_recommends_abort(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.BED_ADHESION_FAILURE)
        assert rec.recommended_strategy == RecoveryStrategy.ABORT
        assert RecoveryStrategy.CANCEL_AND_RETRY in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is False
        assert rec.safety_critical is True


class TestPlanRecoveryLayerShift:
    def test_layer_shift_recommends_abort(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.LAYER_SHIFT)
        assert rec.recommended_strategy == RecoveryStrategy.ABORT
        assert RecoveryStrategy.CANCEL_AND_RETRY in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is False
        assert rec.safety_critical is False


class TestPlanRecoveryFirstLayer:
    def test_first_layer_recommends_cancel_and_retry(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.FIRST_LAYER_FAILURE)
        assert rec.recommended_strategy == RecoveryStrategy.CANCEL_AND_RETRY
        assert RecoveryStrategy.ABORT in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is True
        assert rec.safety_critical is False


class TestPlanRecoveryFilamentRunout:
    def test_filament_runout_recommends_pause(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.FILAMENT_RUNOUT)
        assert rec.recommended_strategy == RecoveryStrategy.PAUSE_AND_INTERVENE
        assert rec.estimated_waste_pct == 0.0
        assert rec.auto_recoverable is False

    def test_filament_runout_with_checkpoint_includes_resume_alt(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        rec = mgr.plan_recovery("j1", FailureType.FILAMENT_RUNOUT)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT in rec.alternative_strategies
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies

    def test_filament_runout_without_checkpoint_no_resume_alt(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.FILAMENT_RUNOUT)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT not in rec.alternative_strategies
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies


class TestPlanRecoveryNozzleClog:
    def test_nozzle_clog_recommends_pause(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.NOZZLE_CLOG)
        assert rec.recommended_strategy == RecoveryStrategy.PAUSE_AND_INTERVENE
        assert rec.estimated_waste_pct == 0.0
        assert rec.auto_recoverable is False

    def test_nozzle_clog_with_checkpoint_includes_resume_alt(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 30.0)
        rec = mgr.plan_recovery("j1", FailureType.NOZZLE_CLOG)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT in rec.alternative_strategies

    def test_nozzle_clog_without_checkpoint_no_resume_alt(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.NOZZLE_CLOG)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT not in rec.alternative_strategies


class TestPlanRecoveryPowerLoss:
    """Power loss planning with and without checkpoint."""

    def test_power_loss_with_checkpoint_recommends_resume(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint(
            "j1", "p1", "printing", 60.0,
            state_data={"z_height_mm": 30.0, "layer_number": 150},
        )
        rec = mgr.plan_recovery("j1", FailureType.POWER_LOSS)
        assert rec.recommended_strategy == RecoveryStrategy.RESUME_FROM_CHECKPOINT
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies
        assert RecoveryStrategy.MANUAL_INTERVENTION in rec.alternative_strategies
        assert rec.estimated_waste_pct == pytest.approx(40.0)
        assert rec.auto_recoverable is False
        assert rec.safety_critical is False

    def test_power_loss_with_checkpoint_risk_mentions_z(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint(
            "j1", "p1", "printing", 60.0,
            state_data={"z_height_mm": 30.0, "layer_number": 150},
        )
        rec = mgr.plan_recovery("j1", FailureType.POWER_LOSS)
        assert "Z=30.00mm" in rec.risk_assessment
        assert "layer 150" in rec.risk_assessment

    def test_power_loss_without_checkpoint_recommends_restart(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.POWER_LOSS)
        assert rec.recommended_strategy == RecoveryStrategy.RESTART_FROM_BEGINNING
        assert RecoveryStrategy.MANUAL_INTERVENTION in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is False


class TestPlanRecoveryNetworkDisconnect:
    def test_network_disconnect_recommends_retry(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.NETWORK_DISCONNECT)
        assert rec.recommended_strategy == RecoveryStrategy.RETRY_CURRENT_STEP
        assert rec.estimated_waste_pct == 0.0
        assert rec.auto_recoverable is True

    def test_network_disconnect_with_checkpoint_includes_resume_alt(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 40.0)
        rec = mgr.plan_recovery("j1", FailureType.NETWORK_DISCONNECT)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT in rec.alternative_strategies

    def test_network_disconnect_without_checkpoint_no_resume_alt(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.NETWORK_DISCONNECT)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT not in rec.alternative_strategies
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies


class TestPlanRecoveryTimeout:
    def test_timeout_recommends_retry(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.TIMEOUT)
        assert rec.recommended_strategy == RecoveryStrategy.RETRY_CURRENT_STEP
        assert rec.estimated_waste_pct == 0.0
        assert rec.auto_recoverable is True

    def test_timeout_with_checkpoint_includes_resume_alt(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 20.0)
        rec = mgr.plan_recovery("j1", FailureType.TIMEOUT)
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT in rec.alternative_strategies


class TestPlanRecoveryPrinterError:
    def test_printer_error_high_progress_recommends_resume(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        rec = mgr.plan_recovery("j1", FailureType.PRINTER_ERROR)
        assert rec.recommended_strategy == RecoveryStrategy.RESUME_FROM_CHECKPOINT
        assert RecoveryStrategy.RETRY_CURRENT_STEP in rec.alternative_strategies
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies
        assert rec.estimated_waste_pct == pytest.approx(50.0)
        assert rec.auto_recoverable is False

    def test_printer_error_low_progress_recommends_retry(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 5.0)
        rec = mgr.plan_recovery("j1", FailureType.PRINTER_ERROR)
        assert rec.recommended_strategy == RecoveryStrategy.RETRY_CURRENT_STEP
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies
        assert rec.auto_recoverable is True

    def test_printer_error_no_checkpoint_recommends_retry(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.PRINTER_ERROR)
        assert rec.recommended_strategy == RecoveryStrategy.RETRY_CURRENT_STEP
        assert rec.auto_recoverable is True

    def test_printer_error_boundary_at_10_percent(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        rec = mgr.plan_recovery("j1", FailureType.PRINTER_ERROR)
        # progress <= 10.0, so low-progress path
        assert rec.recommended_strategy == RecoveryStrategy.RETRY_CURRENT_STEP

    def test_printer_error_just_above_10_percent(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.1)
        rec = mgr.plan_recovery("j1", FailureType.PRINTER_ERROR)
        assert rec.recommended_strategy == RecoveryStrategy.RESUME_FROM_CHECKPOINT


class TestPlanRecoverySoftwareCrash:
    def test_software_crash_with_checkpoint_recommends_resume(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 70.0)
        rec = mgr.plan_recovery("j1", FailureType.SOFTWARE_CRASH)
        assert rec.recommended_strategy == RecoveryStrategy.RESUME_FROM_CHECKPOINT
        assert RecoveryStrategy.RESTART_FROM_BEGINNING in rec.alternative_strategies
        assert rec.estimated_waste_pct == pytest.approx(30.0)
        assert rec.auto_recoverable is True

    def test_software_crash_without_checkpoint_recommends_restart(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.SOFTWARE_CRASH)
        assert rec.recommended_strategy == RecoveryStrategy.RESTART_FROM_BEGINNING
        assert RecoveryStrategy.RETRY_CURRENT_STEP in rec.alternative_strategies
        assert rec.estimated_waste_pct == 100.0
        assert rec.auto_recoverable is True


class TestPlanRecoveryCommon:
    """Cross-cutting assertions for plan_recovery."""

    def test_plan_attaches_checkpoint(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 50.0)
        rec = mgr.plan_recovery("j1", FailureType.POWER_LOSS)
        assert rec.checkpoint is not None
        assert rec.checkpoint.job_id == "j1"

    def test_plan_no_checkpoint_attached_when_none(self):
        mgr = RecoveryManager()
        rec = mgr.plan_recovery("j1", FailureType.POWER_LOSS)
        assert rec.checkpoint is None

    def test_every_failure_type_produces_recommendation(self):
        mgr = RecoveryManager()
        for ft in FailureType:
            rec = mgr.plan_recovery("j1", ft)
            assert isinstance(rec, RecoveryRecommendation)
            assert rec.failure_type == ft
            assert isinstance(rec.recommended_strategy, RecoveryStrategy)


# ---------------------------------------------------------------------------
# 9. execute_recovery
# ---------------------------------------------------------------------------


class TestExecuteRecovery:
    """Covers success/failure paths, retry counter, and checkpoint usage."""

    def test_execute_success(self):
        mgr = RecoveryManager()
        result = mgr.execute_recovery("j1", RecoveryStrategy.RESTART_FROM_BEGINNING)
        assert result.success is True
        assert result.job_id == "j1"
        assert result.strategy_used == RecoveryStrategy.RESTART_FROM_BEGINNING
        assert result.resumed_from_checkpoint is False
        assert result.time_saved_s == 0.0
        assert result.error is None

    def test_execute_resume_with_checkpoint(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 60.0)
        result = mgr.execute_recovery("j1", RecoveryStrategy.RESUME_FROM_CHECKPOINT)
        assert result.resumed_from_checkpoint is True
        assert result.time_saved_s == 60.0

    def test_execute_resume_without_checkpoint(self):
        mgr = RecoveryManager()
        result = mgr.execute_recovery("j1", RecoveryStrategy.RESUME_FROM_CHECKPOINT)
        assert result.resumed_from_checkpoint is False
        assert result.time_saved_s == 0.0

    def test_execute_increments_retry_count(self):
        mgr = RecoveryManager()
        assert mgr.get_retry_count("j1") == 0
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        assert mgr.get_retry_count("j1") == 1
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        assert mgr.get_retry_count("j1") == 2

    def test_execute_max_retries_exceeded(self):
        mgr = RecoveryManager(max_retries=2)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        with pytest.raises(RecoveryError, match="exceeded max retries"):
            mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)

    def test_execute_max_retries_exact_boundary(self):
        mgr = RecoveryManager(max_retries=1)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        with pytest.raises(RecoveryError, match="exceeded max retries"):
            mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)

    def test_execute_different_jobs_independent_retries(self):
        mgr = RecoveryManager(max_retries=1)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        # j1 is at max, j2 should be fine
        result = mgr.execute_recovery("j2", RecoveryStrategy.RETRY_CURRENT_STEP)
        assert result.success is True


# ---------------------------------------------------------------------------
# 10. Retry tracking
# ---------------------------------------------------------------------------


class TestRetryTracking:
    """Covers get_retry_count, reset_retries."""

    def test_get_retry_count_unknown_job(self):
        mgr = RecoveryManager()
        assert mgr.get_retry_count("nonexistent") == 0

    def test_reset_retries(self):
        mgr = RecoveryManager()
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        assert mgr.get_retry_count("j1") == 2
        mgr.reset_retries("j1")
        assert mgr.get_retry_count("j1") == 0

    def test_reset_retries_allows_further_execution(self):
        mgr = RecoveryManager(max_retries=1)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        with pytest.raises(RecoveryError):
            mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        mgr.reset_retries("j1")
        result = mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        assert result.success is True

    def test_reset_retries_nonexistent_job_is_noop(self):
        mgr = RecoveryManager()
        mgr.reset_retries("nonexistent")  # should not raise
        assert mgr.get_retry_count("nonexistent") == 0


# ---------------------------------------------------------------------------
# 11. list_recoverable_jobs
# ---------------------------------------------------------------------------


class TestListRecoverableJobs:
    """Covers filtering by checkpoint existence and retry limits."""

    def test_empty_when_no_checkpoints(self):
        mgr = RecoveryManager()
        assert mgr.list_recoverable_jobs() == []

    def test_returns_jobs_with_checkpoints(self):
        mgr = RecoveryManager()
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.save_checkpoint("j2", "p1", "printing", 20.0)
        jobs = mgr.list_recoverable_jobs()
        assert set(jobs) == {"j1", "j2"}

    def test_excludes_jobs_at_max_retries(self):
        mgr = RecoveryManager(max_retries=1)
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.save_checkpoint("j2", "p1", "printing", 20.0)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        # j1 is now at max retries (1 of 1)
        jobs = mgr.list_recoverable_jobs()
        assert jobs == ["j2"]

    def test_excludes_jobs_beyond_max_retries(self):
        mgr = RecoveryManager(max_retries=2)
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        # j1 is now at max retries (2 of 2)
        assert mgr.list_recoverable_jobs() == []

    def test_includes_jobs_below_max_retries(self):
        mgr = RecoveryManager(max_retries=3)
        mgr.save_checkpoint("j1", "p1", "printing", 10.0)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        mgr.execute_recovery("j1", RecoveryStrategy.RETRY_CURRENT_STEP)
        # j1 has 2 retries, max is 3
        assert mgr.list_recoverable_jobs() == ["j1"]


# ---------------------------------------------------------------------------
# 12. is_safety_critical / is_print_compromised
# ---------------------------------------------------------------------------


class TestSafetyClassification:
    """Covers is_safety_critical and is_print_compromised for all FailureTypes."""

    def test_thermal_runaway_is_safety_critical(self):
        mgr = RecoveryManager()
        assert mgr.is_safety_critical(FailureType.THERMAL_RUNAWAY) is True

    def test_bed_adhesion_is_safety_critical(self):
        mgr = RecoveryManager()
        assert mgr.is_safety_critical(FailureType.BED_ADHESION_FAILURE) is True

    def test_non_safety_critical_failures(self):
        mgr = RecoveryManager()
        non_critical = [
            FailureType.POWER_LOSS,
            FailureType.FILAMENT_RUNOUT,
            FailureType.NOZZLE_CLOG,
            FailureType.LAYER_SHIFT,
            FailureType.FIRST_LAYER_FAILURE,
            FailureType.NETWORK_DISCONNECT,
            FailureType.PRINTER_ERROR,
            FailureType.SOFTWARE_CRASH,
            FailureType.TIMEOUT,
            FailureType.USER_CANCELLED,
        ]
        for ft in non_critical:
            assert mgr.is_safety_critical(ft) is False, f"{ft} should not be safety critical"

    def test_layer_shift_is_print_compromised(self):
        mgr = RecoveryManager()
        assert mgr.is_print_compromised(FailureType.LAYER_SHIFT) is True

    def test_bed_adhesion_is_print_compromised(self):
        mgr = RecoveryManager()
        assert mgr.is_print_compromised(FailureType.BED_ADHESION_FAILURE) is True

    def test_first_layer_is_print_compromised(self):
        mgr = RecoveryManager()
        assert mgr.is_print_compromised(FailureType.FIRST_LAYER_FAILURE) is True

    def test_non_compromised_failures(self):
        mgr = RecoveryManager()
        non_compromised = [
            FailureType.POWER_LOSS,
            FailureType.FILAMENT_RUNOUT,
            FailureType.NOZZLE_CLOG,
            FailureType.THERMAL_RUNAWAY,
            FailureType.NETWORK_DISCONNECT,
            FailureType.PRINTER_ERROR,
            FailureType.SOFTWARE_CRASH,
            FailureType.TIMEOUT,
            FailureType.USER_CANCELLED,
        ]
        for ft in non_compromised:
            assert mgr.is_print_compromised(ft) is False, f"{ft} should not be print compromised"

    def test_safety_critical_frozenset_contents(self):
        assert _SAFETY_CRITICAL_FAILURES == frozenset({
            FailureType.THERMAL_RUNAWAY,
            FailureType.BED_ADHESION_FAILURE,
        })

    def test_print_compromised_frozenset_contents(self):
        assert _PRINT_COMPROMISED_FAILURES == frozenset({
            FailureType.LAYER_SHIFT,
            FailureType.BED_ADHESION_FAILURE,
            FailureType.FIRST_LAYER_FAILURE,
        })


# ---------------------------------------------------------------------------
# 13. Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Verifies concurrent checkpoint saves don't lose data."""

    def test_concurrent_checkpoint_saves(self):
        mgr = RecoveryManager()
        num_threads = 20
        saves_per_thread = 50
        barrier = threading.Barrier(num_threads)

        def save_many(thread_idx: int) -> None:
            barrier.wait()
            for i in range(saves_per_thread):
                mgr.save_checkpoint(
                    f"job-{thread_idx}",
                    "p1",
                    "printing",
                    float(i),
                )

        threads = [
            threading.Thread(target=save_many, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread saved `saves_per_thread` checkpoints for its own job
        for t in range(num_threads):
            cps = mgr.get_all_checkpoints(f"job-{t}")
            assert len(cps) == saves_per_thread

    def test_concurrent_saves_to_same_job(self):
        mgr = RecoveryManager()
        num_threads = 10
        saves_per_thread = 100
        barrier = threading.Barrier(num_threads)

        def save_many() -> None:
            barrier.wait()
            for i in range(saves_per_thread):
                mgr.save_checkpoint("shared-job", "p1", "printing", float(i))

        threads = [threading.Thread(target=save_many) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = len(mgr.get_all_checkpoints("shared-job"))
        assert total == num_threads * saves_per_thread


# ---------------------------------------------------------------------------
# 14. Module-level convenience functions
# ---------------------------------------------------------------------------


class TestModuleLevelFunctions:
    """Covers get_recovery_manager, save_checkpoint, plan_recovery module functions."""

    def test_get_recovery_manager_returns_singleton(self):
        # Reset the module-level singleton to ensure a clean test
        import kiln.recovery as mod

        with mock.patch.object(mod, "_manager", None):
            mgr1 = get_recovery_manager()
            mgr2 = get_recovery_manager()
            assert mgr1 is mgr2

    def test_get_recovery_manager_creates_instance(self):
        import kiln.recovery as mod

        with mock.patch.object(mod, "_manager", None):
            mgr = get_recovery_manager()
            assert isinstance(mgr, RecoveryManager)

    def test_module_save_checkpoint(self):
        import kiln.recovery as mod

        mgr = RecoveryManager()
        with mock.patch.object(mod, "_manager", mgr):
            cp = save_checkpoint("j1", "p1", "printing", 50.0)
            assert cp.job_id == "j1"
            assert mgr.get_latest_checkpoint("j1") is not None

    def test_module_plan_recovery(self):
        import kiln.recovery as mod

        mgr = RecoveryManager()
        with mock.patch.object(mod, "_manager", mgr):
            rec = plan_recovery("j1", FailureType.USER_CANCELLED)
            assert rec.recommended_strategy == RecoveryStrategy.ABORT

    def test_singleton_thread_safety(self):
        import kiln.recovery as mod

        instances = []
        barrier = threading.Barrier(10)

        def get_mgr() -> None:
            barrier.wait()
            instances.append(get_recovery_manager())

        with mock.patch.object(mod, "_manager", None):
            threads = [threading.Thread(target=get_mgr) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # All threads should get the same instance
        assert all(inst is instances[0] for inst in instances)


# ---------------------------------------------------------------------------
# 15. Enum string values
# ---------------------------------------------------------------------------


class TestEnumValues:
    """Verify enums use string values for JSON serialization."""

    def test_failure_type_values(self):
        assert FailureType.POWER_LOSS.value == "power_loss"
        assert FailureType.FILAMENT_RUNOUT.value == "filament_runout"
        assert FailureType.NOZZLE_CLOG.value == "nozzle_clog"
        assert FailureType.BED_ADHESION_FAILURE.value == "bed_adhesion_failure"
        assert FailureType.THERMAL_RUNAWAY.value == "thermal_runaway"
        assert FailureType.LAYER_SHIFT.value == "layer_shift"
        assert FailureType.FIRST_LAYER_FAILURE.value == "first_layer_failure"
        assert FailureType.NETWORK_DISCONNECT.value == "network_disconnect"
        assert FailureType.PRINTER_ERROR.value == "printer_error"
        assert FailureType.SOFTWARE_CRASH.value == "software_crash"
        assert FailureType.TIMEOUT.value == "timeout"
        assert FailureType.USER_CANCELLED.value == "user_cancelled"

    def test_recovery_strategy_values(self):
        assert RecoveryStrategy.RESTART_FROM_BEGINNING.value == "restart_from_beginning"
        assert RecoveryStrategy.RESUME_FROM_CHECKPOINT.value == "resume_from_checkpoint"
        assert RecoveryStrategy.RETRY_CURRENT_STEP.value == "retry_current_step"
        assert RecoveryStrategy.PAUSE_AND_INTERVENE.value == "pause_and_intervene"
        assert RecoveryStrategy.EMERGENCY_STOP.value == "emergency_stop"
        assert RecoveryStrategy.CANCEL_AND_RETRY.value == "cancel_and_retry"
        assert RecoveryStrategy.MANUAL_INTERVENTION.value == "manual_intervention"
        assert RecoveryStrategy.ABORT.value == "abort"

    def test_failure_types_are_strings(self):
        for ft in FailureType:
            assert isinstance(ft.value, str)
            assert isinstance(ft, str)

    def test_recovery_strategies_are_strings(self):
        for rs in RecoveryStrategy:
            assert isinstance(rs.value, str)
            assert isinstance(rs, str)
