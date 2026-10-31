"""Print failure detection and recovery sessions.

Detects print failures from telemetry data and classifies them, answers each
with a recovery plan, and runs the plan's recovery session (confirm, monitor,
complete) on every tier.

Supported failure types:
    - Layer shift, thermal runaway, filament runout, adhesion failure
    - Blob formation, power loss, nozzle clog, spaghetti, stringing, warping
    - Communication loss

The plan is the free floor on every install (:mod:`kiln.recovery_floor`:
what happened, the safety lines, and the safe thing to do now, which is to
stop the print only when it is already lost or unsafe).  Saving the print --
resuming from a layer, restarting with changed settings, the G-code for it --
is Kiln Pro's recovery plan (https://kiln3d.com/pricing), asked for one
failure at a time through :mod:`kiln._pro_recovery_bridge`.

Thread safety is guaranteed via :class:`threading.Lock` on all mutable
state.  The module-level :func:`get_recovery_engine` convenience function
returns a lazy singleton.

Usage::

    from kiln.print_recovery import get_recovery_engine

    engine = get_recovery_engine()
    failure = engine.detect_failure(
        printer_name="voron-350",
        telemetry={"hotend_temp": 280, "hotend_target": 200, ...},
        read_from_printer=True,  # only readings Kiln took itself announce
    )
    if failure:
        plan = engine.plan_recovery(failure)
        session = engine.start_recovery(plan, failure)
"""

from __future__ import annotations

import enum
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kiln import recovery_floor
from kiln.events import Event, EventBus, EventType
from kiln.printers.safe_motion import build_safe_abort_sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds for failure detection
# ---------------------------------------------------------------------------

_LAYER_SHIFT_THRESHOLD_MM = 0.5
_THERMAL_RUNAWAY_DELTA = 15.0
_THERMAL_RUNAWAY_ABS_MAX = 300.0
_FLOW_ANOMALY_THRESHOLD = 0.3  # ratio below expected
_TEMP_DROP_ADHESION_THRESHOLD = 10.0
_SPAGHETTI_EXTRUSION_RATIO_MIN = 0.2
_WARPING_TEMP_GRADIENT_MAX = 5.0  # degrees across bed

# The monitoring-checks-required threshold is
# resolved from the failure severity at session-creation time.
# Critical failures (thermal runaway, bed adhesion) demand more passing
# observations before recovery may be declared COMPLETED.
_MIN_MONITORING_CHECKS = 3
_CRITICAL_MONITORING_CHECKS = 5


def _required_monitoring_checks(severity: str | None) -> int:
    """Return the monitoring-checks threshold for a given failure severity."""
    if (severity or "").strip().lower() == "critical":
        return _CRITICAL_MONITORING_CHECKS
    return _MIN_MONITORING_CHECKS

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FailureType(str, enum.Enum):
    """Types of print failures that can be detected."""

    LAYER_SHIFT = "layer_shift"
    THERMAL_RUNAWAY = "thermal_runaway"
    FILAMENT_RUNOUT = "filament_runout"
    ADHESION_FAILURE = "adhesion_failure"
    BLOB_DETECTED = "blob_detected"
    POWER_LOSS = "power_loss"
    NOZZLE_CLOG = "nozzle_clog"
    SPAGHETTI = "spaghetti"
    STRINGING = "stringing"
    WARPING = "warping"
    COMMUNICATION_LOSS = "communication_loss"


class RecoveryStrategy(str, enum.Enum):
    """Strategy for recovering from a failure."""

    RESUME_FROM_LAYER = "resume_from_layer"
    RESTART_WITH_COMPENSATION = "restart_with_compensation"
    PARTIAL_RECOVERY = "partial_recovery"
    SAFE_ABORT = "safe_abort"
    WAIT_AND_RETRY = "wait_and_retry"
    NO_RECOVERY = "no_recovery"


class RecoveryConfidence(str, enum.Enum):
    """Confidence level in the recovery plan."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class RecoveryStatus(str, enum.Enum):
    """Status of a recovery session."""

    DETECTING = "detecting"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    MONITORING = "monitoring"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CheckpointData:
    """Per-job state snapshot used for resume-accuracy.

    Folded in from the deprecated :mod:`kiln.recovery` module.
    Populated either explicitly via :meth:`PrintRecovery.save_checkpoint`
    or implicitly from telemetry inside :meth:`PrintRecovery.detect_failure`.

    When attached to a :class:`FailureReport`, the resume planner reads
    ``z_height_mm`` directly instead of guessing the resume Z from
    ``failure_z_mm`` and an assumed layer height — which dramatically
    improves resume accuracy on prints whose layer heights vary
    (variable-layer-height slicing, adaptive layers, etc.).

    :param z_height_mm: Z position in millimetres at checkpoint time.
    :param layer_number: Layer index (0-based) at checkpoint time.
    :param hotend_temp_c: Hotend temperature in Celsius.
    :param bed_temp_c: Heated bed temperature in Celsius.
    :param filament_used_mm: Total filament extruded so far in millimetres.
    :param fan_speed_pct: Part cooling fan speed as a percentage (0-100).
    :param flow_rate_pct: Flow rate multiplier percentage (default 100).
    :param recorded_at: ISO-8601 timestamp the checkpoint was captured.
    :param extra: Additional printer-specific state (MMU slot, enclosure,
        accelerometer baselines, etc.).
    """

    z_height_mm: float = 0.0
    layer_number: int = 0
    hotend_temp_c: float = 0.0
    bed_temp_c: float = 0.0
    filament_used_mm: float = 0.0
    fan_speed_pct: float = 0.0
    flow_rate_pct: float = 100.0
    recorded_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "z_height_mm": self.z_height_mm,
            "layer_number": self.layer_number,
            "hotend_temp_c": self.hotend_temp_c,
            "bed_temp_c": self.bed_temp_c,
            "filament_used_mm": self.filament_used_mm,
            "fan_speed_pct": self.fan_speed_pct,
            "flow_rate_pct": self.flow_rate_pct,
            "recorded_at": self.recorded_at,
            "extra": dict(self.extra),
        }


@dataclass
class FailureReport:
    """Detailed analysis of a detected failure."""

    failure_id: str
    failure_type: FailureType
    detected_at: str
    printer_name: str
    job_name: str | None = None
    failed_layer: int | None = None
    total_layers: int | None = None
    failure_z_mm: float | None = None
    evidence: list[str] = field(default_factory=list)
    severity: str = "high"
    probable_cause: str = ""
    contributing_factors: list[str] = field(default_factory=list)
    telemetry_snapshot: dict[str, Any] = field(default_factory=dict)
    material_type: str | None = None
    # Path to the original G-code file.  Enables kiln-pro to generate a
    # resume-from-layer G-code artifact without re-slicing.
    gcode_path: str | None = None
    # Optional checkpoint at failure time.  Populated either by an
    # explicit save_checkpoint call before the failure or synthesized
    # from telemetry inside detect_failure.  The resume planner reads
    # ``checkpoint.z_height_mm`` for accurate resume Z.
    checkpoint: CheckpointData | None = None
    # Where the readings behind this report came from: "printer" (Kiln read
    # them off the machine) or "caller" (handed in by whoever asked).  Only a
    # printer's own readings are announced as a failure.
    readings_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "failure_id": self.failure_id,
            "readings_from": self.readings_from,
            "failure_type": self.failure_type.value,
            "detected_at": self.detected_at,
            "printer_name": self.printer_name,
            "job_name": self.job_name,
            "failed_layer": self.failed_layer,
            "total_layers": self.total_layers,
            "failure_z_mm": self.failure_z_mm,
            "evidence": self.evidence,
            "severity": self.severity,
            "probable_cause": self.probable_cause,
            "contributing_factors": self.contributing_factors,
            "telemetry_snapshot": self.telemetry_snapshot,
            "material_type": self.material_type,
            "gcode_path": self.gcode_path,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
        }


@dataclass
class RecoveryPlan:
    """A plan for recovering from a print failure.

    ``steps_gcode`` is the plan's own G-code when its planner wrote it (Kiln
    Pro's plan); a safe abort's is built here with the park route the
    caller's machine can prove.  ``floor_note`` marks the free floor and says
    once why it is not Kiln Pro's plan (:func:`kiln._pro_recovery_bridge
    .floor_note`); neither is set on Kiln Pro's plan, whose answer is
    unchanged by them.
    """

    plan_id: str
    failure_id: str
    strategy: RecoveryStrategy
    confidence: RecoveryConfidence
    resume_layer: int | None = None
    layer_overlap: int = 2
    preparation_steps: list[str] = field(default_factory=list)
    parameter_adjustments: dict[str, Any] = field(default_factory=dict)
    estimated_success_pct: float | None = 0.0
    estimated_time_minutes: float | None = None
    risks: list[str] = field(default_factory=list)
    requires_confirmation: bool = True
    reason: str = ""
    steps_gcode: list[str] | None = field(default=None, repr=False, compare=False)
    floor_note: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    #: Why a resume Kiln builds was not offered for this failure, when the
    #: planner held one back: ``strategy``, ``standing`` (whether a Kiln-built
    #: resume has been shown to work on this printer model), ``why`` in plain
    #: words, and ``ask`` -- what the caller can say to change it
    #: (``part_attached`` / ``try_untested_resume``), or ``""``.
    not_offered: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        said = {
            "plan_id": self.plan_id,
            "failure_id": self.failure_id,
            "strategy": self.strategy.value,
            "confidence": self.confidence.value,
            "resume_layer": self.resume_layer,
            "layer_overlap": self.layer_overlap,
            "preparation_steps": self.preparation_steps,
            "parameter_adjustments": self.parameter_adjustments,
            "estimated_success_pct": self.estimated_success_pct,
            "estimated_time_minutes": self.estimated_time_minutes,
            "risks": self.risks,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "not_offered": self.not_offered,
        }
        if self.floor_note:
            said.update(self.floor_note)
        return said


@dataclass
class RecoverySession:
    """An active recovery attempt."""

    session_id: str
    plan: RecoveryPlan
    failure: FailureReport
    status: RecoveryStatus
    started_at: str
    completed_at: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    steps_remaining: list[str] = field(default_factory=list)
    monitoring_checks: int = 0
    monitoring_passed: int = 0
    monitoring_required: int = _MIN_MONITORING_CHECKS
    result_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "session_id": self.session_id,
            "plan": self.plan.to_dict(),
            "failure": self.failure.to_dict(),
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "steps_completed": self.steps_completed,
            "steps_remaining": self.steps_remaining,
            "monitoring_checks": self.monitoring_checks,
            "monitoring_passed": self.monitoring_passed,
            "monitoring_required": self.monitoring_required,
            "result_notes": self.result_notes,
        }


class MonitoringThresholdNotMet(ValueError):
    """Raised when ``complete_recovery(success=True)`` is called before
    ``monitoring_passed >= monitoring_required``.

    Subclasses :class:`ValueError` so existing handlers still catch it,
    but exposes structured fields for the MCP tool wrapper to surface.
    """

    def __init__(
        self,
        *,
        session_id: str,
        monitoring_checks: int,
        monitoring_passed: int,
        monitoring_required: int,
        severity: str,
    ) -> None:
        self.session_id = session_id
        self.monitoring_checks = monitoring_checks
        self.monitoring_passed = monitoring_passed
        self.monitoring_required = monitoring_required
        self.severity = severity
        self.deficit = monitoring_required - monitoring_passed
        plural = "" if self.deficit == 1 else "s"
        # Telling the operator what to DO next is the difference between
        # a useful safety gate and a confusing one.
        self.remediation = (
            f"Record {self.deficit} more passing monitoring check{plural} via "
            f"record_recovery_check before calling complete_recovery again."
        )
        super().__init__(
            f"Cannot complete recovery {session_id!r}: "
            f"{monitoring_passed}/{monitoring_required} passing checks recorded "
            f"({self.deficit} more required for severity={severity!r}). "
            f"{self.remediation}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "MONITORING_THRESHOLD_NOT_MET",
            "session_id": self.session_id,
            "monitoring_checks": self.monitoring_checks,
            "monitoring_passed": self.monitoring_passed,
            "monitoring_required": self.monitoring_required,
            "deficit": self.deficit,
            "severity": self.severity,
            "remediation": self.remediation,
            "message": str(self),
        }


# ---------------------------------------------------------------------------
# Recovery engine
# ---------------------------------------------------------------------------


class PrintRecovery:
    """AI-driven print failure recovery engine.

    All public methods are thread-safe.  The engine maintains failure
    history and active recovery sessions protected by a single lock.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, RecoverySession] = {}
        self._failure_history: list[FailureReport] = []
        # Per-(printer, job) checkpoint stash — the most recent checkpoint
        # for each running job.  Populated explicitly via save_checkpoint
        # OR implicitly from telemetry inside detect_failure.  Read by
        # detect_failure to attach to the FailureReport, and by the
        # resume planner for accurate resume_z calculation.  Folded in
        # from the deprecated kiln.recovery.RecoveryManager.
        self._checkpoints: dict[tuple[str, str], CheckpointData] = {}
        self._max_history: int = 500
        self._lock = threading.Lock()

    # -- public API: checkpoints -------------------------------------------

    def save_checkpoint(
        self,
        *,
        printer_name: str,
        job_id: str,
        z_height_mm: float = 0.0,
        layer_number: int = 0,
        hotend_temp_c: float = 0.0,
        bed_temp_c: float = 0.0,
        filament_used_mm: float = 0.0,
        fan_speed_pct: float = 0.0,
        flow_rate_pct: float = 100.0,
        extra: dict[str, Any] | None = None,
    ) -> CheckpointData:
        """Stash a checkpoint for a running job.

        The checkpoint is keyed by ``(printer_name, job_id)`` and
        replaces any previous checkpoint for the same job.  The next
        :meth:`detect_failure` call for that printer attaches the
        latest checkpoint to the resulting :class:`FailureReport`,
        which the resume planner then reads for accurate resume Z.

        :param printer_name: Identifier of the printer.
        :param job_id: Identifier of the print job.
        :param z_height_mm: Z position in millimetres.
        :param layer_number: Layer index (0-based).
        :param hotend_temp_c: Hotend temperature.
        :param bed_temp_c: Bed temperature.
        :param filament_used_mm: Filament consumed so far.
        :param fan_speed_pct: Cooling fan percentage (0-100).
        :param flow_rate_pct: Flow-rate multiplier percentage.
        :param extra: Optional printer-specific extras.
        :returns: The stored :class:`CheckpointData`.
        """
        if not printer_name or not printer_name.strip():
            raise ValueError("printer_name is required")
        if not job_id or not job_id.strip():
            raise ValueError("job_id is required")

        cp = CheckpointData(
            z_height_mm=float(z_height_mm),
            layer_number=int(layer_number),
            hotend_temp_c=float(hotend_temp_c),
            bed_temp_c=float(bed_temp_c),
            filament_used_mm=float(filament_used_mm),
            fan_speed_pct=float(fan_speed_pct),
            flow_rate_pct=float(flow_rate_pct),
            recorded_at=datetime.now(tz=timezone.utc).isoformat(),
            extra=dict(extra or {}),
        )
        with self._lock:
            self._checkpoints[(printer_name.strip(), job_id.strip())] = cp
        return cp

    def get_latest_checkpoint(
        self,
        printer_name: str,
        job_id: str | None = None,
    ) -> CheckpointData | None:
        """Return the most recent checkpoint for a printer (or printer+job).

        When ``job_id`` is None, returns the freshest checkpoint across
        any job on that printer.  Otherwise returns the checkpoint
        scoped to the specific (printer, job) pair, or ``None``.
        """
        with self._lock:
            if job_id is not None:
                return self._checkpoints.get(
                    (printer_name.strip(), job_id.strip())
                )
            # Fold across all jobs for this printer; pick freshest.
            best: CheckpointData | None = None
            for (p, _j), cp in self._checkpoints.items():
                if p != printer_name.strip():
                    continue
                if best is None or cp.recorded_at > best.recorded_at:
                    best = cp
            return best

    def clear_checkpoints(
        self,
        printer_name: str | None = None,
        job_id: str | None = None,
    ) -> int:
        """Remove stashed checkpoints, optionally scoped.

        Returns the number of checkpoints cleared.
        """
        with self._lock:
            if printer_name is None and job_id is None:
                count = len(self._checkpoints)
                self._checkpoints.clear()
                return count
            keys_to_drop = [
                k for k in self._checkpoints
                if (printer_name is None or k[0] == printer_name.strip())
                and (job_id is None or k[1] == job_id.strip())
            ]
            for k in keys_to_drop:
                del self._checkpoints[k]
            return len(keys_to_drop)

    # -- public API --------------------------------------------------------

    def detect_failure(
        self,
        *,
        printer_name: str,
        telemetry: dict[str, Any],
        telemetry_history: list[dict[str, Any]] | None = None,
        job_info: dict[str, Any] | None = None,
        read_from_printer: bool = False,
    ) -> FailureReport | None:
        """Analyze telemetry to detect and classify a failure.

        Runs each detector in priority order and returns the first
        failure found, or ``None`` if no failure is detected.

        A failure is announced on the event bus (PRINT_FAILED, which ends
        the named printer's print watchdog and starts its heater cooldown)
        only when ``read_from_printer`` is true.  Readings handed in from
        outside are judged and kept in the history, never announced.

        :param printer_name: Identifier of the printer.
        :param telemetry: Current telemetry snapshot.
        :param telemetry_history: Recent telemetry snapshots for trend analysis.
        :param job_info: Current job metadata (file_name, layer, total_layers, z_mm).
        :param read_from_printer: True only when Kiln read ``telemetry`` off
            the printer itself.
        :returns: :class:`FailureReport` or ``None``.
        """
        printer_name = printer_name.strip()
        if not printer_name:
            raise ValueError("printer_name is required")

        history = telemetry_history or []
        info = job_info or {}

        # Run detectors in priority order (most critical first).
        detectors = [
            self._detect_thermal_issue,
            self._detect_communication_loss,
            self._detect_layer_shift,
            self._detect_filament_runout,
            self._detect_adhesion_failure,
            self._detect_nozzle_clog,
            self._detect_spaghetti,
            self._detect_warping,
        ]

        for detector in detectors:
            report = detector(printer_name, telemetry, history, info)
            if report is not None:
                # Capture telemetry snapshot and material for G-code generation.
                report.telemetry_snapshot = {
                    "hotend_temp": telemetry.get("hotend_target"),
                    "bed_temp": telemetry.get("bed_target"),
                    "hotend_actual": telemetry.get("hotend_temp"),
                    "bed_actual": telemetry.get("bed_temp"),
                }
                report.material_type = info.get("material") or telemetry.get("material")
                # Stamp gcode_path so kiln-pro's resume engine can locate the
                # original .gcode without re-slicing.  Accept either
                # ``gcode_path`` (explicit) or ``gcode_file`` (legacy).
                gp = info.get("gcode_path") or info.get("gcode_file")
                if isinstance(gp, str) and gp:
                    report.gcode_path = gp
                # Attach the latest checkpoint for this printer (if any).
                # Prefers an explicitly-stashed checkpoint via
                # save_checkpoint; falls back to a synthetic checkpoint
                # built from current telemetry + job_info so the resume
                # planner always has SOMETHING to read for resume_z.
                report.checkpoint = self._resolve_checkpoint_for_failure(
                    printer_name, telemetry, info,
                )
                report.readings_from = "printer" if read_from_printer else "caller"
                with self._lock:
                    self._failure_history.append(report)
                    if len(self._failure_history) > self._max_history:
                        self._failure_history = self._failure_history[-self._max_history :]
                logger.warning(
                    "Failure detected: type=%s printer=%s severity=%s readings_from=%s",
                    report.failure_type.value,
                    printer_name,
                    report.severity,
                    report.readings_from,
                )
                if read_from_printer:
                    self._emit_event(report)
                return report

        return None

    def plan_recovery(
        self,
        failure: FailureReport,
        *,
        part_attached: bool | None = None,
        try_untested_resume: bool = False,
        printer_capabilities: dict[str, Any] | None = None,
        safety_profile: dict[str, Any] | None = None,
    ) -> RecoveryPlan:
        """The recovery plan for a detected failure.

        A resume Kiln builds (print on from the layer the print stopped at)
        is offered only on a printer model a Kiln-built resume has been shown
        to work on, and only when the half-printed part is still on the
        plate: *part_attached* is the person's word on that (``None``: nobody
        has looked, and the plan asks), and *try_untested_resume* opts a
        model Kiln has not seen one work on in.  The printer's catalogue model
        word goes with the failure's facts; its name never does.  Why a
        resume was not offered is on ``plan.not_offered``.

        Kiln Pro's plan when the caller's plan includes it (asked through
        :mod:`kiln._pro_recovery_bridge`, one failure at a time); otherwise
        the free floor (:mod:`kiln.recovery_floor`), whose ``floor_note``
        says once why.  ``printer_capabilities`` and ``safety_profile`` are
        accepted for the doors that pass them and change nothing.

        :param failure: The failure to recover from.
        :param printer_capabilities: Optional printer capabilities dict.
        :param safety_profile: Optional safety profile dict.
        :returns: :class:`RecoveryPlan`.
        """
        del printer_capabilities, safety_profile
        from kiln import _pro_recovery_bridge as bridge

        asked = bridge.ask("session", _wire_facts(
            failure, part_attached=part_attached, try_untested_resume=try_untested_resume,
        ))
        if asked.answer is not None:
            plan = _plan_from_wire(asked.answer, failure)
            if plan is not None:
                # The floor's safety lines ride on every plan, paid included.
                plan.preparation_steps = recovery_floor.with_safety_lines(
                    failure.failure_type.value, plan.preparation_steps,
                )
                return plan
            from kiln.served_answer import Miss

            asked = bridge.Asked(included=True, miss=Miss("unanswered", detail="a plan this install could not read"))
        plan = _floor_plan(failure)
        plan.floor_note = bridge.floor_note(
            asked, recovery_floor.SESSION_PRO_ADDS.get(failure.failure_type.value, ""),
        )
        return plan

    def start_recovery(
        self,
        plan: RecoveryPlan,
        failure: FailureReport,
        *,
        park_route: Any = None,
    ) -> RecoverySession:
        """Begin executing a recovery plan.

        ``park_route`` is an optional PROVEN park route
        (:func:`kiln.printers.safe_motion.plan_live_park`) computed by a
        caller that can ask the live machine where it homes.  The engine
        itself stays offline and never guesses one: without it, an abort
        simply leaves the nozzle at the homing position.

        If the plan requires confirmation, the session starts in
        AWAITING_CONFIRMATION status.  Otherwise, it starts in
        EXECUTING status.

        :param plan: The recovery plan to execute.
        :param failure: The failure being recovered from.
        :returns: :class:`RecoverySession`.
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        session_id = str(uuid.uuid4())

        initial_status = (
            RecoveryStatus.AWAITING_CONFIRMATION
            if plan.requires_confirmation
            else RecoveryStatus.EXECUTING
        )

        recovery_steps = self._generate_recovery_gcode(
            plan, failure, park_route=park_route
        )

        session = RecoverySession(
            session_id=session_id,
            plan=plan,
            failure=failure,
            status=initial_status,
            started_at=now,
            steps_completed=[],
            steps_remaining=list(recovery_steps),
            monitoring_required=_required_monitoring_checks(failure.severity),
        )

        with self._lock:
            self._sessions[session_id] = session

        logger.info(
            "Recovery session started: id=%s strategy=%s status=%s",
            session_id,
            plan.strategy.value,
            initial_status.value,
        )
        return session

    def confirm_recovery(self, session_id: str) -> RecoverySession:
        """Human confirms recovery can proceed.

        Transitions session from AWAITING_CONFIRMATION to EXECUTING.

        :param session_id: Session to confirm.
        :returns: Updated :class:`RecoverySession`.
        :raises ValueError: If session not found or not awaiting confirmation.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Recovery session not found: {session_id!r}")
            if session.status != RecoveryStatus.AWAITING_CONFIRMATION:
                raise ValueError(
                    f"Session {session_id!r} is in {session.status.value} state, "
                    f"not awaiting_confirmation."
                )
            session.status = RecoveryStatus.EXECUTING
            return session

    def cancel_recovery(self, session_id: str, *, reason: str = "") -> RecoverySession:
        """Cancel an active recovery session.

        :param session_id: Session to cancel.
        :param reason: Why the recovery was cancelled.
        :returns: Updated :class:`RecoverySession`.
        :raises ValueError: If session not found or already terminal.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Recovery session not found: {session_id!r}")
            terminal = {RecoveryStatus.COMPLETED, RecoveryStatus.FAILED, RecoveryStatus.CANCELLED}
            if session.status in terminal:
                raise ValueError(
                    f"Session {session_id!r} is already in terminal state: {session.status.value}"
                )
            session.status = RecoveryStatus.CANCELLED
            session.completed_at = datetime.now(tz=timezone.utc).isoformat()
            session.result_notes = reason or "Cancelled by user"
            return session

    def get_recovery_steps(self, session_id: str) -> list[str]:
        """Get the list of G-code/commands for recovery execution.

        :param session_id: Session to query.
        :returns: List of G-code command strings.
        :raises ValueError: If session not found.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Recovery session not found: {session_id!r}")
            return list(session.steps_remaining)

    def record_monitoring_check(
        self,
        session_id: str,
        *,
        passed: bool,
        notes: str = "",
    ) -> RecoverySession:
        """Record a post-recovery monitoring check result.

        After sufficient passing checks, the session can be completed.

        :param session_id: Session to update.
        :param passed: Whether the check passed.
        :param notes: Optional notes about the check.
        :returns: Updated :class:`RecoverySession`.
        :raises ValueError: If session not found or not in monitoring state.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Recovery session not found: {session_id!r}")
            if session.status not in (RecoveryStatus.EXECUTING, RecoveryStatus.MONITORING):
                raise ValueError(
                    f"Session {session_id!r} is in {session.status.value} state, "
                    f"cannot record monitoring check."
                )
            session.status = RecoveryStatus.MONITORING
            session.monitoring_checks += 1
            if passed:
                session.monitoring_passed += 1
            if notes:
                check_label = f"Check #{session.monitoring_checks}: {'PASS' if passed else 'FAIL'}"
                if notes:
                    check_label += f" - {notes}"
                session.steps_completed.append(check_label)
            return session

    def complete_recovery(
        self,
        session_id: str,
        *,
        success: bool,
        notes: str = "",
    ) -> RecoverySession:
        """Mark recovery as completed (success or failed).

        :param session_id: Session to complete.
        :param success: Whether the recovery succeeded.
        :param notes: Final notes about the recovery.
        :returns: Updated :class:`RecoverySession`.
        :raises ValueError: If session not found or already terminal.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Recovery session not found: {session_id!r}")
            terminal = {RecoveryStatus.COMPLETED, RecoveryStatus.FAILED, RecoveryStatus.CANCELLED}
            if session.status in terminal:
                raise ValueError(
                    f"Session {session_id!r} is already in terminal state: {session.status.value}"
                )
            # Success completion requires the
            # monitoring-checks threshold to be met. Failure completion is
            # always allowed (operator may give up at any time).
            if success and session.monitoring_passed < session.monitoring_required:
                raise MonitoringThresholdNotMet(
                    session_id=session_id,
                    monitoring_checks=session.monitoring_checks,
                    monitoring_passed=session.monitoring_passed,
                    monitoring_required=session.monitoring_required,
                    severity=session.failure.severity,
                )
            session.status = RecoveryStatus.COMPLETED if success else RecoveryStatus.FAILED
            session.completed_at = datetime.now(tz=timezone.utc).isoformat()
            session.result_notes = notes
            return session

    def get_session(self, session_id: str) -> RecoverySession | None:
        """Get a recovery session by ID.

        :param session_id: Session to retrieve.
        :returns: :class:`RecoverySession` or ``None``.
        """
        with self._lock:
            return self._sessions.get(session_id)

    def get_active_sessions(self) -> list[RecoverySession]:
        """Get all non-terminal recovery sessions.

        :returns: List of active :class:`RecoverySession` instances.
        """
        active_statuses = {
            RecoveryStatus.DETECTING,
            RecoveryStatus.PLANNING,
            RecoveryStatus.AWAITING_CONFIRMATION,
            RecoveryStatus.EXECUTING,
            RecoveryStatus.MONITORING,
        }
        with self._lock:
            return [s for s in self._sessions.values() if s.status in active_statuses]

    def get_failure_history(self, *, limit: int = 50) -> list[FailureReport]:
        """Get recent failure reports, newest first.

        :param limit: Maximum number of reports to return.
        :returns: List of :class:`FailureReport`.
        """
        with self._lock:
            history = list(self._failure_history)
        return list(reversed(history))[:limit]

    def get_recovery_statistics(self) -> dict[str, Any]:
        """Get historical recovery success rates and failure type distribution.

        :returns: Dict with statistics about recovery attempts.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            failures = list(self._failure_history)

        total_sessions = len(sessions)
        completed = [s for s in sessions if s.status == RecoveryStatus.COMPLETED]
        failed = [s for s in sessions if s.status == RecoveryStatus.FAILED]
        cancelled = [s for s in sessions if s.status == RecoveryStatus.CANCELLED]
        active = [
            s for s in sessions
            if s.status not in {RecoveryStatus.COMPLETED, RecoveryStatus.FAILED, RecoveryStatus.CANCELLED}
        ]

        success_rate = len(completed) / total_sessions if total_sessions > 0 else 0.0

        # Failure type distribution.
        failure_counts: dict[str, int] = {}
        for f in failures:
            key = f.failure_type.value
            failure_counts[key] = failure_counts.get(key, 0) + 1

        # Strategy success rates.
        strategy_stats: dict[str, dict[str, int]] = {}
        for s in sessions:
            key = s.plan.strategy.value
            if key not in strategy_stats:
                strategy_stats[key] = {"total": 0, "successful": 0, "failed": 0}
            strategy_stats[key]["total"] += 1
            if s.status == RecoveryStatus.COMPLETED:
                strategy_stats[key]["successful"] += 1
            elif s.status == RecoveryStatus.FAILED:
                strategy_stats[key]["failed"] += 1

        return {
            "total_failures_detected": len(failures),
            "total_recovery_sessions": total_sessions,
            "completed": len(completed),
            "failed": len(failed),
            "cancelled": len(cancelled),
            "active": len(active),
            "success_rate": round(success_rate, 3),
            "failure_type_distribution": failure_counts,
            "strategy_statistics": strategy_stats,
        }

    # -- private: failure detectors ----------------------------------------

    def _detect_thermal_issue(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect thermal runaway from temperature readings."""
        hotend_actual = telemetry.get("hotend_temp")
        hotend_target = telemetry.get("hotend_target")

        if hotend_actual is None:
            return None

        evidence: list[str] = []
        is_runaway = False

        # Check for temperature exceeding absolute maximum.
        if hotend_actual > _THERMAL_RUNAWAY_ABS_MAX:
            evidence.append(
                f"Hotend temperature {hotend_actual}C exceeds absolute maximum {_THERMAL_RUNAWAY_ABS_MAX}C"
            )
            is_runaway = True

        # Check for temperature significantly above target.
        if hotend_target is not None and hotend_target > 0:
            delta = hotend_actual - hotend_target
            if delta > _THERMAL_RUNAWAY_DELTA:
                evidence.append(
                    f"Hotend {hotend_actual}C is {delta:.1f}C above target {hotend_target}C "
                    f"(threshold: {_THERMAL_RUNAWAY_DELTA}C)"
                )
                is_runaway = True

        # Check bed temperature if available.
        bed_actual = telemetry.get("bed_temp")
        bed_target = telemetry.get("bed_target")
        if bed_actual is not None and bed_target is not None and bed_target > 0:
            bed_delta = bed_actual - bed_target
            if bed_delta > _THERMAL_RUNAWAY_DELTA:
                evidence.append(
                    f"Bed {bed_actual}C is {bed_delta:.1f}C above target {bed_target}C"
                )
                is_runaway = True

        if not is_runaway:
            return None

        return FailureReport(
            failure_id=str(uuid.uuid4()),
            failure_type=FailureType.THERMAL_RUNAWAY,
            detected_at=datetime.now(tz=timezone.utc).isoformat(),
            printer_name=printer_name,
            job_name=job_info.get("file_name"),
            failed_layer=job_info.get("layer"),
            total_layers=job_info.get("total_layers"),
            failure_z_mm=job_info.get("z_mm"),
            evidence=evidence,
            severity="critical",
            probable_cause="Heater control failure or thermistor malfunction",
            contributing_factors=["PID tuning may be incorrect", "Thermistor wiring issue"],
        )

    def _detect_communication_loss(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect communication loss from connectivity signals."""
        connected = telemetry.get("connected")
        if connected is False:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.COMMUNICATION_LOSS,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Printer reports disconnected state"],
                severity="high",
                probable_cause="Network or USB connection interrupted",
                contributing_factors=["Cable issue", "Wi-Fi instability", "Firmware crash"],
            )
        return None

    def _detect_layer_shift(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect layer shift from position data or history comparison."""
        x_actual = telemetry.get("x_position")
        y_actual = telemetry.get("y_position")
        x_expected = telemetry.get("x_expected")
        y_expected = telemetry.get("y_expected")

        if x_actual is None or x_expected is None:
            # Try history-based detection.
            if len(history) >= 2:
                prev = history[-2]
                curr = history[-1] if history[-1] is not telemetry else telemetry
                prev_x = prev.get("x_position")
                prev_y = prev.get("y_position")
                curr_x = curr.get("x_position")
                curr_y = curr.get("y_position")
                if all(v is not None for v in [prev_x, prev_y, curr_x, curr_y]):
                    dx = abs(curr_x - prev_x)
                    dy = abs(curr_y - prev_y)
                    # Large unexpected jumps indicate a shift.
                    if (dx > 10.0 or dy > 10.0) and telemetry.get("layer_shift_detected"):
                        return FailureReport(
                            failure_id=str(uuid.uuid4()),
                            failure_type=FailureType.LAYER_SHIFT,
                            detected_at=datetime.now(tz=timezone.utc).isoformat(),
                            printer_name=printer_name,
                            job_name=job_info.get("file_name"),
                            failed_layer=job_info.get("layer"),
                            total_layers=job_info.get("total_layers"),
                            failure_z_mm=job_info.get("z_mm"),
                            evidence=[f"Position jump detected: dX={dx:.1f}mm dY={dy:.1f}mm"],
                            severity="high",
                            probable_cause="Stepper motor skip or belt slip",
                            contributing_factors=[
                                "Belt tension too loose",
                                "Excessive print speed",
                                "Mechanical obstruction",
                            ],
                        )
            return None

        dx = abs(x_actual - x_expected)
        dy = abs(y_actual - y_expected)
        total_delta = (dx**2 + dy**2) ** 0.5

        if total_delta < _LAYER_SHIFT_THRESHOLD_MM:
            return None

        return FailureReport(
            failure_id=str(uuid.uuid4()),
            failure_type=FailureType.LAYER_SHIFT,
            detected_at=datetime.now(tz=timezone.utc).isoformat(),
            printer_name=printer_name,
            job_name=job_info.get("file_name"),
            failed_layer=job_info.get("layer"),
            total_layers=job_info.get("total_layers"),
            failure_z_mm=job_info.get("z_mm"),
            evidence=[
                f"Position mismatch: actual=({x_actual:.2f}, {y_actual:.2f}) "
                f"expected=({x_expected:.2f}, {y_expected:.2f}) delta={total_delta:.2f}mm"
            ],
            severity="high",
            probable_cause="Stepper motor skip or belt slip",
            contributing_factors=[
                "Belt tension too loose",
                "Excessive print speed",
                "Mechanical obstruction",
            ],
        )

    def _detect_filament_runout(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect filament runout from sensor signal or flow anomaly."""
        filament_detected = telemetry.get("filament_detected")
        if filament_detected is False:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.FILAMENT_RUNOUT,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Filament runout sensor triggered"],
                severity="medium",
                probable_cause="Filament spool depleted or broken filament",
                contributing_factors=["End of spool", "Filament snag or tangle"],
            )

        # Flow-based detection.
        flow_rate = telemetry.get("flow_rate")
        expected_flow = telemetry.get("expected_flow")
        if flow_rate is not None and expected_flow is not None and expected_flow > 0:
            ratio = flow_rate / expected_flow
            if ratio < _FLOW_ANOMALY_THRESHOLD:
                return FailureReport(
                    failure_id=str(uuid.uuid4()),
                    failure_type=FailureType.FILAMENT_RUNOUT,
                    detected_at=datetime.now(tz=timezone.utc).isoformat(),
                    printer_name=printer_name,
                    job_name=job_info.get("file_name"),
                    failed_layer=job_info.get("layer"),
                    total_layers=job_info.get("total_layers"),
                    failure_z_mm=job_info.get("z_mm"),
                    evidence=[f"Flow rate {flow_rate:.2f} is {ratio:.0%} of expected {expected_flow:.2f}"],
                    severity="medium",
                    probable_cause="Filament depletion or partial clog reducing flow",
                    contributing_factors=["Low spool", "Partial nozzle obstruction"],
                )

        return None

    def _detect_adhesion_failure(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect adhesion failure from bed temperature drop or sensor signal."""
        adhesion_lost = telemetry.get("adhesion_lost")
        if adhesion_lost is True:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.ADHESION_FAILURE,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Adhesion loss sensor triggered"],
                severity="high",
                probable_cause="Part detached from build plate",
                contributing_factors=[
                    "Insufficient bed adhesion",
                    "Bed temperature too low",
                    "No adhesion aid (glue/tape)",
                ],
            )

        # Temperature drop pattern: bed temp falling while target is constant.
        bed_actual = telemetry.get("bed_temp")
        bed_target = telemetry.get("bed_target")
        if (
            bed_actual is not None
            and bed_target is not None
            and bed_target > 0
            and (bed_target - bed_actual) > _TEMP_DROP_ADHESION_THRESHOLD
            and len(history) >= 2
        ):
            prev_bed = history[-1].get("bed_temp")
            if prev_bed is not None and prev_bed > bed_actual:
                return FailureReport(
                    failure_id=str(uuid.uuid4()),
                    failure_type=FailureType.ADHESION_FAILURE,
                    detected_at=datetime.now(tz=timezone.utc).isoformat(),
                    printer_name=printer_name,
                    job_name=job_info.get("file_name"),
                    failed_layer=job_info.get("layer"),
                    total_layers=job_info.get("total_layers"),
                    failure_z_mm=job_info.get("z_mm"),
                    evidence=[
                        f"Bed temp {bed_actual}C dropping from target {bed_target}C "
                        f"(prev: {prev_bed}C) -- possible heater failure causing adhesion loss"
                    ],
                    severity="high",
                    probable_cause="Bed heater failing, causing part to lose adhesion",
                    contributing_factors=["Bed heater malfunction", "Ambient temperature drop"],
                )

        return None

    def _detect_nozzle_clog(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect nozzle clog from pressure/flow anomaly."""
        nozzle_clogged = telemetry.get("nozzle_clogged")
        if nozzle_clogged is True:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.NOZZLE_CLOG,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Nozzle clog sensor triggered"],
                severity="high",
                probable_cause="Partial or full nozzle obstruction",
                contributing_factors=["Carbonized filament", "Foreign debris", "Heat creep"],
            )

        # Pressure-based detection.
        extruder_pressure = telemetry.get("extruder_pressure")
        pressure_threshold = telemetry.get("pressure_threshold")
        if (
            extruder_pressure is not None
            and pressure_threshold is not None
            and extruder_pressure > pressure_threshold
        ):
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.NOZZLE_CLOG,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=[
                    f"Extruder pressure {extruder_pressure} exceeds threshold {pressure_threshold}"
                ],
                severity="high",
                probable_cause="Nozzle obstruction increasing backpressure",
                contributing_factors=["Carbonized material", "Heat creep", "Wrong nozzle size"],
            )

        return None

    def _detect_spaghetti(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect spaghetti (failed extrusion) from telemetry."""
        spaghetti_detected = telemetry.get("spaghetti_detected")
        if spaghetti_detected is True:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.SPAGHETTI,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Spaghetti / failed extrusion detected by vision or sensor"],
                severity="critical",
                probable_cause="Nozzle printing in air after part detachment",
                contributing_factors=[
                    "Adhesion failure",
                    "Support structure failure",
                    "Excessive overhang",
                ],
            )
        return None

    def _detect_warping(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        history: list[dict[str, Any]],
        job_info: dict[str, Any],
    ) -> FailureReport | None:
        """Detect warping from bed temperature gradient or sensor signal."""
        warping_detected = telemetry.get("warping_detected")
        if warping_detected is True:
            return FailureReport(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.WARPING,
                detected_at=datetime.now(tz=timezone.utc).isoformat(),
                printer_name=printer_name,
                job_name=job_info.get("file_name"),
                failed_layer=job_info.get("layer"),
                total_layers=job_info.get("total_layers"),
                failure_z_mm=job_info.get("z_mm"),
                evidence=["Warping detected by sensor or vision system"],
                severity="medium",
                probable_cause="Part corners lifting due to thermal contraction",
                contributing_factors=[
                    "Insufficient bed temperature",
                    "No enclosure",
                    "Large flat part geometry",
                ],
            )
        return None

    # -- private: checkpoint resolution -----------------------------------

    def _resolve_checkpoint_for_failure(
        self,
        printer_name: str,
        telemetry: dict[str, Any],
        job_info: dict[str, Any],
    ) -> CheckpointData | None:
        """Resolve the best-available checkpoint for a freshly-detected failure.

        Resolution order:

        1. If a checkpoint was explicitly stashed via :meth:`save_checkpoint`
           for this (printer, job_id), use it — the operator/agent
           supplied known-good state, which is the highest-fidelity
           source.
        2. Else if telemetry + job_info carry enough state to build a
           synthetic checkpoint (z_mm or z_height_mm + layer + temps),
           build one inline.  This is the common path: no explicit
           save_checkpoint, just the telemetry that came in alongside
           the failure.
        3. Else return None — the resume planner falls back to its
           calculated z_per_layer estimate.

        Always best-effort and never raises.  Failure to resolve a
        checkpoint must NOT block detect_failure — checkpoints are an
        accuracy improvement, not a correctness requirement.
        """
        try:
            job_id = job_info.get("job_id") or job_info.get("file_name")
            if isinstance(job_id, str) and job_id.strip():
                explicit = self.get_latest_checkpoint(printer_name, job_id)
                if explicit is not None:
                    return explicit

            # Synthesize from telemetry + job_info.  Prefer explicit
            # z_height fields; fall back to z_mm (the legacy key).
            z_height = (
                job_info.get("z_height_mm")
                or job_info.get("z_mm")
                or telemetry.get("z_height_mm")
                or telemetry.get("z_mm")
            )
            layer = job_info.get("layer") or job_info.get("layer_number")
            if z_height is None and layer is None:
                return None

            return CheckpointData(
                z_height_mm=float(z_height) if z_height is not None else 0.0,
                layer_number=int(layer) if layer is not None else 0,
                hotend_temp_c=float(telemetry.get("hotend_temp") or 0.0),
                bed_temp_c=float(telemetry.get("bed_temp") or 0.0),
                filament_used_mm=float(
                    job_info.get("filament_used_mm")
                    or telemetry.get("filament_used_mm")
                    or 0.0,
                ),
                fan_speed_pct=float(telemetry.get("fan_speed_pct") or 0.0),
                flow_rate_pct=float(telemetry.get("flow_rate_pct") or 100.0),
                recorded_at=datetime.now(tz=timezone.utc).isoformat(),
                extra={},
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "Failed to resolve checkpoint for %s: %s",
                printer_name,
                exc,
            )
            return None

    # -- private: recovery G-code ------------------------------------------

    def _generate_recovery_gcode(
        self,
        plan: RecoveryPlan,
        failure: FailureReport,
        *,
        park_route: Any = None,
    ) -> list[str]:
        """The G-code commands for a recovery.

        A safe abort is built here, with the park route only the caller's
        machine can prove.  A plan that carries its own G-code (Kiln Pro's)
        runs that.  Anything else sends nothing the printer acts on: the
        floor's wait and leave plans are comments, and a plan that names a
        strategy with no G-code of its own never has motion invented for it.
        """
        if plan.strategy == RecoveryStrategy.SAFE_ABORT:
            return build_safe_abort_sequence(park_route=park_route)
        if plan.steps_gcode is not None:
            return list(plan.steps_gcode)
        if plan.strategy == RecoveryStrategy.NO_RECOVERY:
            return ["; No recovery action needed -- print continues"]
        if failure.failure_type == FailureType.FILAMENT_RUNOUT:
            return ["; Waiting for the printer's own filament change", "; Nothing is sent"]
        if failure.failure_type == FailureType.COMMUNICATION_LOSS:
            return ["; Waiting for the printer to answer again", "; Nothing is sent"]
        return ["; This plan carries no G-code: nothing is sent"]

    # -- private: event emission -------------------------------------------

    def _emit_event(self, failure: FailureReport) -> None:
        """Best-effort event emission for failure detection.  Never raises."""
        try:
            event = Event(
                type=EventType.PRINT_FAILED,
                data={
                    "failure_id": failure.failure_id,
                    "failure_type": failure.failure_type.value,
                    "printer_name": failure.printer_name,
                    "severity": failure.severity,
                    "probable_cause": failure.probable_cause,
                },
                source=f"recovery:{failure.printer_name}",
            )

            bus: EventBus | None = None
            try:
                from kiln.server import _event_bus as server_bus

                bus = server_bus
            except ImportError:
                pass

            if bus is not None:
                bus.publish(event)
        except Exception:
            logger.debug(
                "Failed to emit recovery event for %s",
                failure.printer_name,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# The free floor, and Kiln Pro's plan as it arrives
# ---------------------------------------------------------------------------

#: The floor's safe step for each of the three actions (:mod:`kiln.recovery_floor`).
_FLOOR_STRATEGY = {
    recovery_floor.STOP: RecoveryStrategy.SAFE_ABORT,
    recovery_floor.WAIT: RecoveryStrategy.WAIT_AND_RETRY,
    recovery_floor.LEAVE: RecoveryStrategy.NO_RECOVERY,
}
#: Floor stops that run without asking: waiting on a confirmation while a
#: heater runs away, or while a failed print wraps the hot end, can cost the
#: machine.
_STOPS_WITHOUT_ASKING = frozenset({FailureType.THERMAL_RUNAWAY, FailureType.SPAGHETTI})


def _floor_plan(failure: FailureReport) -> RecoveryPlan:
    """The free floor for *failure*: what happened, the safety lines, and the
    safe thing to do now.  Not scored: the floor makes no estimate."""
    action, happened = recovery_floor.HAPPENED.get(failure.failure_type.value, recovery_floor.HAPPENED["unknown"])
    strategy = _FLOOR_STRATEGY[action]
    without_asking = failure.failure_type in _STOPS_WITHOUT_ASKING or strategy == RecoveryStrategy.NO_RECOVERY
    return RecoveryPlan(
        plan_id=str(uuid.uuid4()),
        failure_id=failure.failure_id,
        strategy=strategy,
        confidence=(
            RecoveryConfidence.HIGH if failure.failure_type in _STOPS_WITHOUT_ASKING else RecoveryConfidence.UNKNOWN
        ),
        preparation_steps=recovery_floor.steps(failure.failure_type.value),
        estimated_success_pct=None,
        risks=["Part is incomplete and may not be usable"] if strategy == RecoveryStrategy.SAFE_ABORT else [],
        requires_confirmation=not without_asking,
        reason=happened,
    )


def _whole(value: Any) -> int | None:
    """A layer count as a whole number, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _reading(value: Any) -> float | None:
    """A height or temperature as a number, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _printer_model_word(printer_name: str | None) -> str | None:
    """The catalogue model word of the printer the failure names (the one
    fact about the printer a plan is allowed to carry), or ``None``."""
    if not printer_name:
        return None
    try:
        from kiln.community_autofire import resolve_adapter_model
        from kiln.registry import get_printer_registry

        adapter = get_printer_registry().get(printer_name)
        model = resolve_adapter_model(adapter) if adapter is not None else None
    except Exception:  # noqa: BLE001 -- no model is an untested model, the safe answer
        return None
    word = str(model or "").strip()
    return word or None


def _wire_facts(
    failure: FailureReport, *, part_attached: bool | None = None, try_untested_resume: bool = False,
) -> dict[str, Any]:
    """The failure's own facts, as Kiln Pro's planner reads them: never the
    printer's name, the job's name, a file path or the telemetry beyond the
    two temperatures a resume heats to.  The printer's catalogue model word
    goes, so the plan can say whether a Kiln-built resume has been shown to
    work on it, with the person's two answers about the resume."""
    snap = failure.telemetry_snapshot or {}
    checkpoint = failure.checkpoint
    return {
        "printer_model": _printer_model_word(failure.printer_name),
        "part_attached": part_attached if isinstance(part_attached, bool) else None,
        "try_untested_resume": bool(try_untested_resume),
        "failure_id": failure.failure_id,
        "failure_type": failure.failure_type.value,
        "failed_layer": _whole(failure.failed_layer),
        "total_layers": _whole(failure.total_layers),
        "failure_z_mm": _reading(failure.failure_z_mm),
        "material_type": failure.material_type if isinstance(failure.material_type, str) else None,
        "checkpoint_z_mm": _reading(checkpoint.z_height_mm) if checkpoint is not None else None,
        "hotend_temp": _reading(snap.get("hotend_temp")),
        "bed_temp": _reading(snap.get("bed_temp")),
    }


def _plan_from_wire(answer: dict[str, Any], failure: FailureReport) -> RecoveryPlan | None:
    """Kiln Pro's plan as this install's :class:`RecoveryPlan`, or ``None``
    when the answer is not one."""
    said = answer.get("plan")
    gcode = answer.get("gcode")
    if not isinstance(said, dict):
        return None
    if gcode is not None and not (isinstance(gcode, list) and all(isinstance(line, str) for line in gcode)):
        return None

    def _strings(value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    try:
        plan = RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy(said.get("strategy")),
            confidence=RecoveryConfidence(said.get("confidence")),
            resume_layer=said.get("resume_layer"),
            layer_overlap=int(said.get("layer_overlap", 2)),
            preparation_steps=_strings(said.get("preparation_steps")),
            parameter_adjustments=dict(said.get("parameter_adjustments") or {}),
            estimated_success_pct=said.get("estimated_success_pct"),
            estimated_time_minutes=said.get("estimated_time_minutes"),
            risks=_strings(said.get("risks")),
            requires_confirmation=bool(said.get("requires_confirmation", True)),
            reason=str(said.get("reason") or ""),
            not_offered=dict(said["not_offered"]) if isinstance(said.get("not_offered"), dict) else None,
        )
    except (TypeError, ValueError):
        return None
    if plan.strategy != RecoveryStrategy.SAFE_ABORT:
        # A plan that is not a safe abort runs only the G-code its planner
        # wrote; one that arrived without it sends nothing.
        plan.steps_gcode = list(gcode) if gcode is not None else []
    return plan


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine: PrintRecovery | None = None
_engine_lock = threading.Lock()


def get_recovery_engine() -> PrintRecovery:
    """Return the module-level :class:`PrintRecovery` singleton.

    The engine is created lazily on first access.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = PrintRecovery()
    return _engine
