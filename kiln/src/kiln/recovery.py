"""Print job recovery system for interrupted FDM 3D printing jobs.

Provides checkpoint-based recovery for interrupted print jobs across all
supported FDM printers (OctoPrint, Moonraker/Klipper, Bambu Lab).  The
system saves periodic checkpoints during job execution and, on failure,
recommends a recovery strategy based on the failure type, printer
capabilities, and available checkpoint data.

FDM-specific recovery strategies:

- **Power loss**: Resume from Z-height checkpoint if the printer firmware
  supports power-loss recovery (e.g. Klipper ``SAVE_VARIABLE``, Marlin
  ``M413``).  Otherwise restart from the beginning.
- **Filament runout**: Pause, prompt for filament load, then resume from
  the current layer.
- **Nozzle clog**: Pause, perform cold-pull or manual cleaning, then
  resume.
- **Bed adhesion failure**: Stop immediately -- continuing risks nozzle
  collision with loose plastic.
- **Thermal runaway**: Emergency stop.  Printer must be physically
  inspected before any retry.
- **Layer shift**: Stop -- print geometry is irrecoverably compromised.
- **First layer failure**: Cancel and retry from the beginning.

Checkpoint data includes: Z height, layer number, hotend temperature,
bed temperature, and filament consumed (mm).

Example::

    mgr = get_recovery_manager()
    cp = mgr.save_checkpoint(
        "job-1", "prusa-mk4", "printing",
        progress_pct=45.0,
        state_data={
            "z_height_mm": 22.4,
            "layer_number": 112,
            "hotend_temp_c": 210.0,
            "bed_temp_c": 60.0,
            "filament_used_mm": 3400.0,
        },
    )
    plan = mgr.plan_recovery("job-1", FailureType.FILAMENT_RUNOUT)
    result = mgr.execute_recovery("job-1", plan.recommended_strategy)
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FailureType(str, enum.Enum):
    """Categories of print job failure that trigger recovery analysis."""

    POWER_LOSS = "power_loss"
    FILAMENT_RUNOUT = "filament_runout"
    NOZZLE_CLOG = "nozzle_clog"
    BED_ADHESION_FAILURE = "bed_adhesion_failure"
    THERMAL_RUNAWAY = "thermal_runaway"
    LAYER_SHIFT = "layer_shift"
    FIRST_LAYER_FAILURE = "first_layer_failure"
    NETWORK_DISCONNECT = "network_disconnect"
    PRINTER_ERROR = "printer_error"
    SOFTWARE_CRASH = "software_crash"
    TIMEOUT = "timeout"
    USER_CANCELLED = "user_cancelled"


class RecoveryStrategy(str, enum.Enum):
    """Available recovery actions the system can recommend or execute."""

    RESTART_FROM_BEGINNING = "restart_from_beginning"
    RESUME_FROM_CHECKPOINT = "resume_from_checkpoint"
    RETRY_CURRENT_STEP = "retry_current_step"
    PAUSE_AND_INTERVENE = "pause_and_intervene"
    EMERGENCY_STOP = "emergency_stop"
    CANCEL_AND_RETRY = "cancel_and_retry"
    MANUAL_INTERVENTION = "manual_intervention"
    ABORT = "abort"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CheckpointData:
    """FDM-specific state captured at a checkpoint.

    :param z_height_mm: Current Z position in millimetres.
    :param layer_number: Current layer index (0-based).
    :param hotend_temp_c: Hotend temperature in Celsius at checkpoint time.
    :param bed_temp_c: Heated bed temperature in Celsius.
    :param filament_used_mm: Total filament extruded so far in millimetres.
    :param fan_speed_pct: Part cooling fan speed as a percentage (0-100).
    :param flow_rate_pct: Flow rate multiplier percentage (default 100).
    :param extra: Additional printer-specific state (e.g. MMU slot,
        enclosure temp).
    """

    z_height_mm: float = 0.0
    layer_number: int = 0
    hotend_temp_c: float = 0.0
    bed_temp_c: float = 0.0
    filament_used_mm: float = 0.0
    fan_speed_pct: float = 0.0
    flow_rate_pct: float = 100.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "z_height_mm": self.z_height_mm,
            "layer_number": self.layer_number,
            "hotend_temp_c": self.hotend_temp_c,
            "bed_temp_c": self.bed_temp_c,
            "filament_used_mm": self.filament_used_mm,
            "fan_speed_pct": self.fan_speed_pct,
            "flow_rate_pct": self.flow_rate_pct,
            "extra": dict(self.extra),
        }


@dataclass
class RecoveryCheckpoint:
    """A snapshot of print job progress at a specific moment.

    :param job_id: The print job this checkpoint belongs to.
    :param printer_id: Printer executing the job.
    :param checkpoint_id: Unique identifier (auto-generated).
    :param created_at: Unix timestamp of checkpoint creation.
    :param phase: Current operational phase (e.g. ``"printing"``,
        ``"heating"``, ``"leveling"``).
    :param progress_pct: Completion percentage at checkpoint time.
    :param data: FDM-specific checkpoint state.
    """

    job_id: str
    printer_id: str
    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: float = field(default_factory=time.time)
    phase: str = ""
    progress_pct: float = 0.0
    data: CheckpointData = field(default_factory=CheckpointData)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "job_id": self.job_id,
            "printer_id": self.printer_id,
            "checkpoint_id": self.checkpoint_id,
            "created_at": self.created_at,
            "phase": self.phase,
            "progress_pct": self.progress_pct,
            "data": self.data.to_dict(),
        }


@dataclass
class RecoveryRecommendation:
    """A recommended recovery approach for a failed print job.

    :param job_id: The failed job.
    :param failure_type: What caused the failure.
    :param recommended_strategy: Best recovery action.
    :param alternative_strategies: Other viable strategies, ordered by
        preference.
    :param checkpoint: Latest checkpoint available, or ``None``.
    :param estimated_waste_pct: Estimated material/time waste percentage.
    :param risk_assessment: Human-readable risk description.
    :param auto_recoverable: Whether recovery can proceed without human
        intervention.
    :param safety_critical: Whether the failure poses a safety hazard that
        must be addressed before any retry.
    """

    job_id: str
    failure_type: FailureType
    recommended_strategy: RecoveryStrategy
    alternative_strategies: List[RecoveryStrategy] = field(default_factory=list)
    checkpoint: Optional[RecoveryCheckpoint] = None
    estimated_waste_pct: float = 0.0
    risk_assessment: str = ""
    auto_recoverable: bool = False
    safety_critical: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "job_id": self.job_id,
            "failure_type": self.failure_type.value,
            "recommended_strategy": self.recommended_strategy.value,
            "alternative_strategies": [s.value for s in self.alternative_strategies],
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "estimated_waste_pct": self.estimated_waste_pct,
            "risk_assessment": self.risk_assessment,
            "auto_recoverable": self.auto_recoverable,
            "safety_critical": self.safety_critical,
        }


@dataclass
class RecoveryResult:
    """Outcome of an executed recovery attempt.

    :param job_id: The job that was recovered.
    :param strategy_used: Which strategy was applied.
    :param success: Whether the recovery succeeded.
    :param resumed_from_checkpoint: Whether a checkpoint was used.
    :param time_saved_s: Estimated seconds saved vs. starting from scratch.
    :param error: Error message if recovery failed.
    """

    job_id: str
    strategy_used: RecoveryStrategy
    success: bool
    resumed_from_checkpoint: bool = False
    time_saved_s: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "job_id": self.job_id,
            "strategy_used": self.strategy_used.value,
            "success": self.success,
            "resumed_from_checkpoint": self.resumed_from_checkpoint,
            "time_saved_s": self.time_saved_s,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Recovery manager
# ---------------------------------------------------------------------------


class RecoveryError(Exception):
    """Raised when a recovery operation cannot proceed."""

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


# Failure types that indicate a physical safety hazard.  These require
# the printer to be inspected before any retry attempt.
_SAFETY_CRITICAL_FAILURES: frozenset[FailureType] = frozenset({
    FailureType.THERMAL_RUNAWAY,
    FailureType.BED_ADHESION_FAILURE,
})

# Failure types where the print is irrecoverably damaged and continuing
# would waste filament.
_PRINT_COMPROMISED_FAILURES: frozenset[FailureType] = frozenset({
    FailureType.LAYER_SHIFT,
    FailureType.BED_ADHESION_FAILURE,
    FailureType.FIRST_LAYER_FAILURE,
})


class RecoveryManager:
    """Thread-safe manager for print job checkpoints, recovery planning,
    and retry tracking.

    :param max_retries: Maximum retry attempts before a job is declared
        unrecoverable.  Override with the ``KILN_RECOVERY_MAX_RETRIES``
        environment variable.
    :param checkpoint_interval_s: Minimum seconds between checkpoints for
        the same job (advisory; callers decide when to checkpoint).
        Override with ``KILN_RECOVERY_CHECKPOINT_INTERVAL``.
    """

    def __init__(
        self,
        *,
        max_retries: Optional[int] = None,
        checkpoint_interval_s: Optional[float] = None,
    ) -> None:
        self._max_retries = max_retries or int(
            os.environ.get("KILN_RECOVERY_MAX_RETRIES", "3")
        )
        self._checkpoint_interval_s = checkpoint_interval_s or float(
            os.environ.get("KILN_RECOVERY_CHECKPOINT_INTERVAL", "30.0")
        )
        self._lock = threading.Lock()
        # job_id -> list of checkpoints (newest last)
        self._checkpoints: Dict[str, List[RecoveryCheckpoint]] = {}
        # job_id -> retry count
        self._retry_counts: Dict[str, int] = {}

    # -- checkpoints -------------------------------------------------------

    def save_checkpoint(
        self,
        job_id: str,
        printer_id: str,
        phase: str,
        progress_pct: float,
        *,
        state_data: Optional[Dict[str, Any]] = None,
    ) -> RecoveryCheckpoint:
        """Save a recovery checkpoint for a running print job.

        :param job_id: The print job to checkpoint.
        :param printer_id: Printer executing the job.
        :param phase: Current operational phase (e.g. ``"printing"``).
        :param progress_pct: Completion percentage (0-100).
        :param state_data: FDM-specific state dict.  Expected keys:
            ``z_height_mm``, ``layer_number``, ``hotend_temp_c``,
            ``bed_temp_c``, ``filament_used_mm``.  Unknown keys are
            stored in :attr:`CheckpointData.extra`.
        :returns: The created :class:`RecoveryCheckpoint`.
        """
        data = self._build_checkpoint_data(state_data or {})
        cp = RecoveryCheckpoint(
            job_id=job_id,
            printer_id=printer_id,
            phase=phase,
            progress_pct=progress_pct,
            data=data,
        )
        with self._lock:
            self._checkpoints.setdefault(job_id, []).append(cp)
        logger.debug(
            "Saved checkpoint %s for job %s (%.1f%%, layer %d, Z=%.2fmm)",
            cp.checkpoint_id,
            job_id,
            progress_pct,
            data.layer_number,
            data.z_height_mm,
        )
        return cp

    @staticmethod
    def _build_checkpoint_data(raw: Dict[str, Any]) -> CheckpointData:
        """Parse a raw state dict into a :class:`CheckpointData`.

        Known keys are mapped to typed fields; everything else goes into
        the ``extra`` bucket.
        """
        _KNOWN_KEYS = {
            "z_height_mm", "layer_number", "hotend_temp_c",
            "bed_temp_c", "filament_used_mm", "fan_speed_pct",
            "flow_rate_pct",
        }
        known = {k: v for k, v in raw.items() if k in _KNOWN_KEYS}
        extra = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}
        return CheckpointData(**known, extra=extra)

    def get_latest_checkpoint(self, job_id: str) -> Optional[RecoveryCheckpoint]:
        """Return the most recent checkpoint for a job, or ``None``.

        :param job_id: The job to look up.
        """
        with self._lock:
            cps = self._checkpoints.get(job_id)
            if not cps:
                return None
            return cps[-1]

    def get_all_checkpoints(self, job_id: str) -> List[RecoveryCheckpoint]:
        """Return all checkpoints for a job, oldest first.

        :param job_id: The job to look up.
        """
        with self._lock:
            return list(self._checkpoints.get(job_id, []))

    def clear_checkpoints(self, job_id: str) -> int:
        """Remove all checkpoints for a job.

        :param job_id: The job whose checkpoints should be cleared.
        :returns: Number of checkpoints removed.
        """
        with self._lock:
            removed = self._checkpoints.pop(job_id, [])
        count = len(removed)
        if count:
            logger.debug("Cleared %d checkpoints for job %s", count, job_id)
        return count

    # -- recovery planning -------------------------------------------------

    def plan_recovery(
        self,
        job_id: str,
        failure_type: FailureType,
    ) -> RecoveryRecommendation:
        """Analyse a failure and recommend a recovery strategy.

        The recommendation depends on the failure type, available
        checkpoint data, and FDM-specific physical constraints.

        :param job_id: The failed print job.
        :param failure_type: What caused the failure.
        :returns: A :class:`RecoveryRecommendation` with the recommended
            strategy.
        """
        checkpoint = self.get_latest_checkpoint(job_id)

        strategy, alternatives, waste_pct, risk, auto = self._analyse_failure(
            failure_type, checkpoint,
        )
        safety_critical = failure_type in _SAFETY_CRITICAL_FAILURES

        recommendation = RecoveryRecommendation(
            job_id=job_id,
            failure_type=failure_type,
            recommended_strategy=strategy,
            alternative_strategies=alternatives,
            checkpoint=checkpoint,
            estimated_waste_pct=waste_pct,
            risk_assessment=risk,
            auto_recoverable=auto,
            safety_critical=safety_critical,
        )
        logger.info(
            "Recovery plan for job %s: %s (auto=%s, safety_critical=%s)",
            job_id,
            strategy.value,
            auto,
            safety_critical,
        )
        return recommendation

    def _analyse_failure(
        self,
        failure_type: FailureType,
        checkpoint: Optional[RecoveryCheckpoint],
    ) -> tuple[RecoveryStrategy, list[RecoveryStrategy], float, str, bool]:
        """Return (strategy, alternatives, waste_pct, risk, auto_recoverable).

        Core decision logic mapping failure type to recovery
        recommendation.  FDM-specific physical constraints drive every
        decision.
        """
        has_checkpoint = checkpoint is not None
        progress = checkpoint.progress_pct if checkpoint else 0.0

        # -- user_cancelled: always abort ----------------------------------
        if failure_type == FailureType.USER_CANCELLED:
            return (
                RecoveryStrategy.ABORT,
                [],
                progress,
                "Print cancelled by user. No recovery needed.",
                False,
            )

        # -- thermal_runaway: emergency stop, no retry ---------------------
        if failure_type == FailureType.THERMAL_RUNAWAY:
            return (
                RecoveryStrategy.EMERGENCY_STOP,
                [RecoveryStrategy.ABORT],
                100.0,
                "Thermal runaway detected. Printer firmware should have "
                "already cut heater power. Do NOT retry until the printer "
                "has been physically inspected for damaged thermistors, "
                "loose wiring, or faulty MOSFETs.",
                False,
            )

        # -- bed_adhesion_failure: stop immediately ------------------------
        if failure_type == FailureType.BED_ADHESION_FAILURE:
            return (
                RecoveryStrategy.ABORT,
                [RecoveryStrategy.CANCEL_AND_RETRY],
                100.0,
                "Bed adhesion failure. The print has detached from the "
                "build plate. Continuing risks nozzle collision with loose "
                "plastic, potential hotend damage, or fire from dragged "
                "filament on the heater block. Clean the bed, re-level, "
                "and retry from the beginning.",
                False,
            )

        # -- layer_shift: stop (print geometry compromised) ----------------
        if failure_type == FailureType.LAYER_SHIFT:
            return (
                RecoveryStrategy.ABORT,
                [RecoveryStrategy.CANCEL_AND_RETRY],
                100.0,
                "Layer shift detected. Print geometry is irrecoverably "
                "misaligned. Continuing would waste filament on a "
                "defective part. Check belt tension, pulley grub screws, "
                "and stepper driver current before retrying.",
                False,
            )

        # -- first_layer_failure: cancel and retry from beginning ----------
        if failure_type == FailureType.FIRST_LAYER_FAILURE:
            return (
                RecoveryStrategy.CANCEL_AND_RETRY,
                [RecoveryStrategy.ABORT],
                100.0,
                "First layer failure. Cancel and retry with adjusted "
                "Z-offset, bed temperature, or first-layer speed. Check "
                "bed cleanliness and leveling.",
                True,
            )

        # -- filament_runout: pause, load new filament, resume -------------
        if failure_type == FailureType.FILAMENT_RUNOUT:
            alts: list[RecoveryStrategy] = []
            if has_checkpoint:
                alts.append(RecoveryStrategy.RESUME_FROM_CHECKPOINT)
            alts.append(RecoveryStrategy.RESTART_FROM_BEGINNING)
            return (
                RecoveryStrategy.PAUSE_AND_INTERVENE,
                alts,
                0.0,
                "Filament runout detected. Printer should be paused. "
                "Load new filament, purge until colour is consistent, "
                "then resume. If the hotend has cooled and filament has "
                "solidified inside, a cold pull may be needed first.",
                False,
            )

        # -- nozzle_clog: pause, clean, resume -----------------------------
        if failure_type == FailureType.NOZZLE_CLOG:
            alts = []
            if has_checkpoint:
                alts.append(RecoveryStrategy.RESUME_FROM_CHECKPOINT)
            alts.append(RecoveryStrategy.RESTART_FROM_BEGINNING)
            return (
                RecoveryStrategy.PAUSE_AND_INTERVENE,
                alts,
                0.0,
                "Nozzle clog detected (under-extrusion or grinding). "
                "Pause the print, perform a cold pull or use an acupuncture "
                "needle to clear the nozzle. Verify extrusion before "
                "resuming. If the clog occurred many layers ago, the print "
                "may already be compromised.",
                False,
            )

        # -- power_loss: resume from Z-height if possible ------------------
        if failure_type == FailureType.POWER_LOSS:
            return self._plan_power_loss(checkpoint, has_checkpoint, progress)

        # -- network_disconnect: retry (printer may still be running) ------
        if failure_type == FailureType.NETWORK_DISCONNECT:
            alts = []
            if has_checkpoint:
                alts.append(RecoveryStrategy.RESUME_FROM_CHECKPOINT)
            alts.append(RecoveryStrategy.RESTART_FROM_BEGINNING)
            return (
                RecoveryStrategy.RETRY_CURRENT_STEP,
                alts,
                0.0,
                "Network connection to printer lost. The printer may still "
                "be printing autonomously (most FDM printers buffer G-code "
                "locally). Reconnect and check printer status before taking "
                "action.",
                True,
            )

        # -- timeout: retry (printer unresponsive) -------------------------
        if failure_type == FailureType.TIMEOUT:
            alts = []
            if has_checkpoint:
                alts.append(RecoveryStrategy.RESUME_FROM_CHECKPOINT)
            alts.append(RecoveryStrategy.RESTART_FROM_BEGINNING)
            return (
                RecoveryStrategy.RETRY_CURRENT_STEP,
                alts,
                0.0,
                "Printer communication timed out. Check network "
                "connectivity and printer responsiveness. The print may "
                "still be running if G-code is buffered on the printer.",
                True,
            )

        # -- printer_error: depends on progress ----------------------------
        if failure_type == FailureType.PRINTER_ERROR:
            if has_checkpoint and progress > 10.0:
                return (
                    RecoveryStrategy.RESUME_FROM_CHECKPOINT,
                    [
                        RecoveryStrategy.RETRY_CURRENT_STEP,
                        RecoveryStrategy.RESTART_FROM_BEGINNING,
                    ],
                    max(0.0, 100.0 - progress),
                    "Printer reported an error. If the printer can be "
                    "returned to a safe state (axes homed, temps stable), "
                    "resume from the last checkpoint. Otherwise restart.",
                    False,
                )
            return (
                RecoveryStrategy.RETRY_CURRENT_STEP,
                [RecoveryStrategy.RESTART_FROM_BEGINNING],
                progress,
                "Printer error with minimal progress. Retry the current "
                "step. If the error persists, check the printer hardware "
                "and firmware logs.",
                True,
            )

        # -- software_crash: resume from checkpoint if available -----------
        if failure_type == FailureType.SOFTWARE_CRASH:
            if has_checkpoint:
                return (
                    RecoveryStrategy.RESUME_FROM_CHECKPOINT,
                    [RecoveryStrategy.RESTART_FROM_BEGINNING],
                    max(0.0, 100.0 - progress),
                    "Kiln software crash. The printer may still be running "
                    "autonomously. Reconnect and check printer state before "
                    "deciding whether to resume from the last checkpoint.",
                    True,
                )
            return (
                RecoveryStrategy.RESTART_FROM_BEGINNING,
                [RecoveryStrategy.RETRY_CURRENT_STEP],
                100.0,
                "Kiln software crash with no checkpoint. Restart the print "
                "from the beginning after verifying printer state.",
                True,
            )

        # Fallback -- should not be reached with exhaustive enum handling.
        return (
            RecoveryStrategy.MANUAL_INTERVENTION,
            [RecoveryStrategy.ABORT],
            progress,
            f"Unknown failure type: {failure_type.value}. Manual review "
            "required before taking any action.",
            False,
        )

    def _plan_power_loss(
        self,
        checkpoint: Optional[RecoveryCheckpoint],
        has_checkpoint: bool,
        progress: float,
    ) -> tuple[RecoveryStrategy, list[RecoveryStrategy], float, str, bool]:
        """FDM-specific recovery planning for power loss events.

        Power-loss recovery on FDM printers depends on:

        1. Whether a Z-height checkpoint exists.
        2. Whether the printer firmware supports power-loss recovery
           (Marlin ``M413``, Klipper ``SAVE_VARIABLE``).
        3. Whether the bed and hotend can re-heat without shifting the
           part (bed springs, thermal expansion).

        Even with a checkpoint, resuming is risky because:
        - The nozzle may have oozed onto the part surface during cooldown.
        - Thermal contraction may have partially detached the part.
        - The printer must re-home X/Y (Z must NOT re-home or it will
          crash into the part).
        """
        if has_checkpoint and checkpoint is not None:
            z = checkpoint.data.z_height_mm
            layer = checkpoint.data.layer_number
            return (
                RecoveryStrategy.RESUME_FROM_CHECKPOINT,
                [
                    RecoveryStrategy.RESTART_FROM_BEGINNING,
                    RecoveryStrategy.MANUAL_INTERVENTION,
                ],
                max(0.0, 100.0 - progress),
                f"Power loss with checkpoint at Z={z:.2f}mm (layer {layer}). "
                "Resume is possible if the printer supports power-loss "
                "recovery and the part has not shifted on the bed. Before "
                "resuming: (1) do NOT home Z -- raise the nozzle manually "
                "or use G92 to set the Z position, (2) re-heat bed first "
                "to restore adhesion, (3) re-heat hotend, (4) prime the "
                "nozzle to clear ooze, (5) resume from the saved layer.",
                False,
            )
        return (
            RecoveryStrategy.RESTART_FROM_BEGINNING,
            [RecoveryStrategy.MANUAL_INTERVENTION],
            100.0,
            "Power loss with no checkpoint. Z position is unknown and "
            "the part may have shifted during cooldown. Full restart "
            "required after verifying the bed is clear.",
            False,
        )

    # -- recovery execution ------------------------------------------------

    def execute_recovery(
        self,
        job_id: str,
        strategy: RecoveryStrategy,
    ) -> RecoveryResult:
        """Record and execute a recovery attempt.

        Increments the retry counter for the job.  If max retries are
        exceeded, raises :class:`RecoveryError`.

        :param job_id: The job to recover.
        :param strategy: The strategy to apply.
        :returns: A :class:`RecoveryResult` describing the outcome.
        :raises RecoveryError: If max retries are exceeded.
        """
        with self._lock:
            count = self._retry_counts.get(job_id, 0)
            if count >= self._max_retries:
                raise RecoveryError(
                    f"Print job {job_id!r} has exceeded max retries "
                    f"({self._max_retries}). Manual intervention required. "
                    "Reset retries with reset_retries() to allow further "
                    "attempts."
                )
            self._retry_counts[job_id] = count + 1

        checkpoint = self.get_latest_checkpoint(job_id)
        resumed = (
            strategy == RecoveryStrategy.RESUME_FROM_CHECKPOINT
            and checkpoint is not None
        )
        time_saved = 0.0
        if resumed and checkpoint is not None:
            # Rough estimate: progress_pct maps to time saved.  A real
            # implementation would use the slicer's time estimate.
            time_saved = checkpoint.progress_pct

        logger.info(
            "Executing recovery for job %s: strategy=%s (attempt %d/%d)",
            job_id,
            strategy.value,
            count + 1,
            self._max_retries,
        )

        return RecoveryResult(
            job_id=job_id,
            strategy_used=strategy,
            success=True,
            resumed_from_checkpoint=resumed,
            time_saved_s=time_saved,
        )

    # -- retry tracking ----------------------------------------------------

    def get_retry_count(self, job_id: str) -> int:
        """Return the number of recovery attempts for a job.

        :param job_id: The job to check.
        """
        with self._lock:
            return self._retry_counts.get(job_id, 0)

    def reset_retries(self, job_id: str) -> None:
        """Reset the retry counter for a job.

        Call this after a successful recovery or when a job is
        resubmitted from scratch.

        :param job_id: The job to reset.
        """
        with self._lock:
            self._retry_counts.pop(job_id, None)
        logger.debug("Reset retry count for job %s", job_id)

    def list_recoverable_jobs(self) -> List[str]:
        """Return job IDs that have checkpoints and have not exceeded
        max retries.

        :returns: List of job IDs eligible for recovery.
        """
        with self._lock:
            result: List[str] = []
            for job_id in self._checkpoints:
                retries = self._retry_counts.get(job_id, 0)
                if retries < self._max_retries:
                    result.append(job_id)
            return result

    def is_safety_critical(self, failure_type: FailureType) -> bool:
        """Check whether a failure type indicates a physical safety hazard.

        :param failure_type: The failure to check.
        :returns: ``True`` if the printer must be physically inspected.
        """
        return failure_type in _SAFETY_CRITICAL_FAILURES

    def is_print_compromised(self, failure_type: FailureType) -> bool:
        """Check whether a failure type means the print is irrecoverably
        damaged.

        :param failure_type: The failure to check.
        :returns: ``True`` if the print geometry is compromised.
        """
        return failure_type in _PRINT_COMPROMISED_FAILURES


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: Optional[RecoveryManager] = None
_manager_lock = threading.Lock()


def get_recovery_manager() -> RecoveryManager:
    """Return the module-level :class:`RecoveryManager` singleton.

    The instance is lazily created on first call.  Thread-safe.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = RecoveryManager()
    return _manager


def save_checkpoint(
    job_id: str,
    printer_id: str,
    phase: str,
    progress_pct: float,
    *,
    state_data: Optional[Dict[str, Any]] = None,
) -> RecoveryCheckpoint:
    """Save a checkpoint via the module-level singleton.

    Convenience wrapper around :meth:`RecoveryManager.save_checkpoint`.
    """
    return get_recovery_manager().save_checkpoint(
        job_id,
        printer_id,
        phase,
        progress_pct,
        state_data=state_data,
    )


def plan_recovery(
    job_id: str,
    failure_type: FailureType,
) -> RecoveryRecommendation:
    """Plan recovery via the module-level singleton.

    Convenience wrapper around :meth:`RecoveryManager.plan_recovery`.
    """
    return get_recovery_manager().plan_recovery(job_id, failure_type)
