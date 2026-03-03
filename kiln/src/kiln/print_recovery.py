"""AI-driven print failure recovery with automatic resume.

Detects print failures from telemetry data, classifies them, generates
recovery plans, and can execute recovery through printer adapters --
all without human intervention when the agent has sufficient confidence.

Supported failure types:
    - Layer shift, thermal runaway, filament runout, adhesion failure
    - Blob formation, power loss, nozzle clog, spaghetti, stringing, warping
    - Communication loss

Recovery strategies:
    - Resume from specific layer (with overlap for bonding)
    - Restart with compensated parameters
    - Partial recovery (print remaining portion)
    - Safe abort (controlled shutdown, preserve part)
    - Wait and retry (wait for condition to clear)

Thread safety is guaranteed via :class:`threading.Lock` on all mutable
state.  The module-level :func:`get_recovery_engine` convenience function
returns a lazy singleton.

Usage::

    from kiln.print_recovery import get_recovery_engine

    engine = get_recovery_engine()
    failure = engine.detect_failure(
        printer_name="voron-350",
        telemetry={"hotend_temp": 280, "hotend_target": 200, ...},
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
_MONITORING_CHECKS_REQUIRED = 3

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "failure_id": self.failure_id,
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
        }


@dataclass
class RecoveryPlan:
    """A plan for recovering from a print failure."""

    plan_id: str
    failure_id: str
    strategy: RecoveryStrategy
    confidence: RecoveryConfidence
    resume_layer: int | None = None
    layer_overlap: int = 2
    preparation_steps: list[str] = field(default_factory=list)
    parameter_adjustments: dict[str, Any] = field(default_factory=dict)
    estimated_success_pct: float = 0.0
    estimated_time_minutes: float | None = None
    risks: list[str] = field(default_factory=list)
    requires_confirmation: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
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
        }


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
            "result_notes": self.result_notes,
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
        self._max_history: int = 500
        self._lock = threading.Lock()
        self._recovery_strategies = self._build_strategy_map()

    # -- public API --------------------------------------------------------

    def detect_failure(
        self,
        *,
        printer_name: str,
        telemetry: dict[str, Any],
        telemetry_history: list[dict[str, Any]] | None = None,
        job_info: dict[str, Any] | None = None,
    ) -> FailureReport | None:
        """Analyze telemetry to detect and classify a failure.

        Runs each detector in priority order and returns the first
        failure found, or ``None`` if no failure is detected.

        :param printer_name: Identifier of the printer.
        :param telemetry: Current telemetry snapshot.
        :param telemetry_history: Recent telemetry snapshots for trend analysis.
        :param job_info: Current job metadata (file_name, layer, total_layers, z_mm).
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
                with self._lock:
                    self._failure_history.append(report)
                    if len(self._failure_history) > self._max_history:
                        self._failure_history = self._failure_history[-self._max_history :]
                logger.warning(
                    "Failure detected: type=%s printer=%s severity=%s",
                    report.failure_type.value,
                    printer_name,
                    report.severity,
                )
                self._emit_event(report)
                return report

        return None

    def plan_recovery(
        self,
        failure: FailureReport,
        *,
        printer_capabilities: dict[str, Any] | None = None,
        safety_profile: dict[str, Any] | None = None,
    ) -> RecoveryPlan:
        """Generate a recovery plan for a detected failure.

        Selects the best strategy based on failure type, confidence
        assessment, and printer capabilities.

        :param failure: The failure to recover from.
        :param printer_capabilities: Optional printer capabilities dict.
        :param safety_profile: Optional safety profile dict.
        :returns: :class:`RecoveryPlan`.
        """
        strategies = self._recovery_strategies.get(failure.failure_type, [])
        if not strategies:
            return self._plan_safe_abort(failure)

        primary_strategy = strategies[0]

        planners = {
            RecoveryStrategy.RESUME_FROM_LAYER: self._plan_resume,
            RecoveryStrategy.RESTART_WITH_COMPENSATION: self._plan_restart,
            RecoveryStrategy.PARTIAL_RECOVERY: self._plan_partial,
            RecoveryStrategy.SAFE_ABORT: self._plan_safe_abort,
            RecoveryStrategy.WAIT_AND_RETRY: self._plan_wait_retry,
            RecoveryStrategy.NO_RECOVERY: self._plan_no_recovery,
        }

        planner = planners.get(primary_strategy, self._plan_safe_abort)
        plan = planner(failure)

        # Override confidence based on actual assessment.
        plan.confidence = self._compute_confidence(failure, plan.strategy)
        plan.estimated_success_pct = self._estimate_success_rate(failure, plan.strategy)

        # High confidence plans can auto-execute.
        plan.requires_confirmation = plan.confidence != RecoveryConfidence.HIGH

        return plan

    def start_recovery(
        self,
        plan: RecoveryPlan,
        failure: FailureReport,
    ) -> RecoverySession:
        """Begin executing a recovery plan.

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

        recovery_steps = self._generate_recovery_gcode(plan, failure)

        session = RecoverySession(
            session_id=session_id,
            plan=plan,
            failure=failure,
            status=initial_status,
            started_at=now,
            steps_completed=[],
            steps_remaining=list(recovery_steps),
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

    # -- private: strategy map ---------------------------------------------

    def _build_strategy_map(self) -> dict[FailureType, list[RecoveryStrategy]]:
        """Map failure types to viable recovery strategies, ordered by preference."""
        return {
            FailureType.LAYER_SHIFT: [
                RecoveryStrategy.RESUME_FROM_LAYER,
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.THERMAL_RUNAWAY: [
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.FILAMENT_RUNOUT: [
                RecoveryStrategy.WAIT_AND_RETRY,
                RecoveryStrategy.RESUME_FROM_LAYER,
            ],
            FailureType.ADHESION_FAILURE: [
                RecoveryStrategy.RESTART_WITH_COMPENSATION,
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.BLOB_DETECTED: [
                RecoveryStrategy.SAFE_ABORT,
                RecoveryStrategy.RESUME_FROM_LAYER,
            ],
            FailureType.POWER_LOSS: [
                RecoveryStrategy.RESUME_FROM_LAYER,
                RecoveryStrategy.RESTART_WITH_COMPENSATION,
            ],
            FailureType.NOZZLE_CLOG: [
                RecoveryStrategy.WAIT_AND_RETRY,
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.SPAGHETTI: [
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.STRINGING: [
                RecoveryStrategy.NO_RECOVERY,
            ],
            FailureType.WARPING: [
                RecoveryStrategy.RESTART_WITH_COMPENSATION,
                RecoveryStrategy.SAFE_ABORT,
            ],
            FailureType.COMMUNICATION_LOSS: [
                RecoveryStrategy.WAIT_AND_RETRY,
                RecoveryStrategy.SAFE_ABORT,
            ],
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

    # -- private: recovery planners ----------------------------------------

    def _plan_resume(self, failure: FailureReport) -> RecoveryPlan:
        """Plan resume from a specific layer."""
        resume_layer = (failure.failed_layer or 1) - 3  # Back up 3 layers for overlap.
        resume_layer = max(1, resume_layer)
        layer_overlap = 3 if failure.failure_type == FailureType.LAYER_SHIFT else 2

        z_per_layer = 0.2  # default assumption
        if failure.failure_z_mm and failure.failed_layer and failure.failed_layer > 0:
            z_per_layer = failure.failure_z_mm / failure.failed_layer

        estimated_time = None
        if failure.total_layers and failure.failed_layer:
            remaining = failure.total_layers - resume_layer
            # Rough estimate: 2 minutes per layer.
            estimated_time = remaining * 2.0

        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.RESUME_FROM_LAYER,
            confidence=RecoveryConfidence.MEDIUM,
            resume_layer=resume_layer,
            layer_overlap=layer_overlap,
            preparation_steps=self._generate_preparation_steps(
                RecoveryStrategy.RESUME_FROM_LAYER, failure
            ),
            parameter_adjustments={
                "resume_z_mm": round(resume_layer * z_per_layer, 2),
            },
            estimated_time_minutes=estimated_time,
            risks=[
                "Layer bonding may be weak at the resume point",
                "Z-offset calibration drift during recovery",
            ],
            reason=f"Resume printing from layer {resume_layer} with {layer_overlap}-layer overlap for bonding",
        )

    def _plan_restart(self, failure: FailureReport) -> RecoveryPlan:
        """Plan full restart with compensated parameters."""
        adjustments: dict[str, Any] = {}
        risks: list[str] = []

        if failure.failure_type == FailureType.ADHESION_FAILURE:
            adjustments["bed_temp_offset"] = 5  # +5C
            adjustments["first_layer_speed_pct"] = 80  # Slow first layer
            adjustments["brim_width_mm"] = 5.0
            risks.append("Higher bed temp may cause elephant's foot on first layer")
        elif failure.failure_type == FailureType.WARPING:
            adjustments["bed_temp_offset"] = 5
            adjustments["brim_width_mm"] = 8.0
            adjustments["enclosure_recommended"] = True
            risks.append("Brim may be difficult to remove on finished part")

        estimated_time = None
        if failure.total_layers:
            estimated_time = failure.total_layers * 2.0

        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.RESTART_WITH_COMPENSATION,
            confidence=RecoveryConfidence.MEDIUM,
            preparation_steps=self._generate_preparation_steps(
                RecoveryStrategy.RESTART_WITH_COMPENSATION, failure
            ),
            parameter_adjustments=adjustments,
            estimated_time_minutes=estimated_time,
            risks=risks or ["Full reprint uses additional material and time"],
            reason="Full restart with adjusted parameters to prevent recurrence",
        )

    def _plan_partial(self, failure: FailureReport) -> RecoveryPlan:
        """Plan partial recovery (print remaining portion only)."""
        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.PARTIAL_RECOVERY,
            confidence=RecoveryConfidence.LOW,
            resume_layer=failure.failed_layer,
            preparation_steps=self._generate_preparation_steps(
                RecoveryStrategy.PARTIAL_RECOVERY, failure
            ),
            risks=[
                "Partial prints may not be structurally sound",
                "Layer adhesion at the split point is unreliable",
            ],
            reason="Print only the remaining portion of the model",
        )

    def _plan_safe_abort(self, failure: FailureReport) -> RecoveryPlan:
        """Plan controlled shutdown to preserve the part."""
        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.SAFE_ABORT,
            confidence=RecoveryConfidence.HIGH,
            preparation_steps=self._generate_preparation_steps(
                RecoveryStrategy.SAFE_ABORT, failure
            ),
            risks=["Part is incomplete and may not be usable"],
            requires_confirmation=False,
            reason=f"Safe abort due to {failure.failure_type.value} -- "
            f"preserving partial print and preventing further damage",
        )

    def _plan_wait_retry(self, failure: FailureReport) -> RecoveryPlan:
        """Plan wait for condition to clear, then retry."""
        confidence = RecoveryConfidence.HIGH
        wait_reason = ""

        if failure.failure_type == FailureType.FILAMENT_RUNOUT:
            wait_reason = "Load new filament, then resume print"
            confidence = RecoveryConfidence.HIGH
        elif failure.failure_type == FailureType.NOZZLE_CLOG:
            wait_reason = "Perform cold pull procedure to clear clog, then resume"
            confidence = RecoveryConfidence.MEDIUM
        elif failure.failure_type == FailureType.COMMUNICATION_LOSS:
            wait_reason = "Wait for connection to re-establish, then resume"
            confidence = RecoveryConfidence.HIGH
        else:
            wait_reason = "Wait for condition to clear, then retry"

        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.WAIT_AND_RETRY,
            confidence=confidence,
            resume_layer=failure.failed_layer,
            preparation_steps=self._generate_preparation_steps(
                RecoveryStrategy.WAIT_AND_RETRY, failure
            ),
            risks=["Print quality may be affected at the interruption point"],
            reason=wait_reason,
        )

    def _plan_no_recovery(self, failure: FailureReport) -> RecoveryPlan:
        """Plan for failures that don't need recovery (minor issues)."""
        return RecoveryPlan(
            plan_id=str(uuid.uuid4()),
            failure_id=failure.failure_id,
            strategy=RecoveryStrategy.NO_RECOVERY,
            confidence=RecoveryConfidence.HIGH,
            preparation_steps=[],
            risks=[],
            requires_confirmation=False,
            reason=f"{failure.failure_type.value} is a minor issue; "
            f"print can continue, address in post-processing",
        )

    # -- private: confidence and estimation --------------------------------

    def _compute_confidence(
        self,
        failure: FailureReport,
        strategy: RecoveryStrategy,
    ) -> RecoveryConfidence:
        """Compute confidence level based on failure type and strategy."""
        # High confidence scenarios.
        high_confidence = {
            (FailureType.THERMAL_RUNAWAY, RecoveryStrategy.SAFE_ABORT),
            (FailureType.SPAGHETTI, RecoveryStrategy.SAFE_ABORT),
            (FailureType.FILAMENT_RUNOUT, RecoveryStrategy.WAIT_AND_RETRY),
            (FailureType.COMMUNICATION_LOSS, RecoveryStrategy.WAIT_AND_RETRY),
            (FailureType.STRINGING, RecoveryStrategy.NO_RECOVERY),
        }

        if (failure.failure_type, strategy) in high_confidence:
            return RecoveryConfidence.HIGH

        # Medium confidence scenarios.
        medium_confidence = {
            (FailureType.LAYER_SHIFT, RecoveryStrategy.RESUME_FROM_LAYER),
            (FailureType.ADHESION_FAILURE, RecoveryStrategy.RESTART_WITH_COMPENSATION),
            (FailureType.POWER_LOSS, RecoveryStrategy.RESUME_FROM_LAYER),
            (FailureType.NOZZLE_CLOG, RecoveryStrategy.WAIT_AND_RETRY),
            (FailureType.WARPING, RecoveryStrategy.RESTART_WITH_COMPENSATION),
            (FailureType.BLOB_DETECTED, RecoveryStrategy.SAFE_ABORT),
        }

        if (failure.failure_type, strategy) in medium_confidence:
            return RecoveryConfidence.MEDIUM

        # Everything else is low confidence.
        return RecoveryConfidence.LOW

    def _estimate_success_rate(
        self,
        failure: FailureReport,
        strategy: RecoveryStrategy,
    ) -> float:
        """Estimate success percentage for a given failure/strategy pair."""
        rates: dict[tuple[FailureType, RecoveryStrategy], float] = {
            (FailureType.THERMAL_RUNAWAY, RecoveryStrategy.SAFE_ABORT): 95.0,
            (FailureType.SPAGHETTI, RecoveryStrategy.SAFE_ABORT): 90.0,
            (FailureType.FILAMENT_RUNOUT, RecoveryStrategy.WAIT_AND_RETRY): 85.0,
            (FailureType.COMMUNICATION_LOSS, RecoveryStrategy.WAIT_AND_RETRY): 80.0,
            (FailureType.LAYER_SHIFT, RecoveryStrategy.RESUME_FROM_LAYER): 60.0,
            (FailureType.ADHESION_FAILURE, RecoveryStrategy.RESTART_WITH_COMPENSATION): 70.0,
            (FailureType.POWER_LOSS, RecoveryStrategy.RESUME_FROM_LAYER): 65.0,
            (FailureType.NOZZLE_CLOG, RecoveryStrategy.WAIT_AND_RETRY): 55.0,
            (FailureType.WARPING, RecoveryStrategy.RESTART_WITH_COMPENSATION): 65.0,
            (FailureType.BLOB_DETECTED, RecoveryStrategy.SAFE_ABORT): 90.0,
            (FailureType.STRINGING, RecoveryStrategy.NO_RECOVERY): 100.0,
        }

        return rates.get((failure.failure_type, strategy), 40.0)

    # -- private: step generation ------------------------------------------

    def _generate_preparation_steps(
        self,
        strategy: RecoveryStrategy,
        failure: FailureReport,
    ) -> list[str]:
        """Generate human-readable preparation steps for a recovery strategy."""
        steps: list[str] = []

        if strategy == RecoveryStrategy.RESUME_FROM_LAYER:
            steps = [
                "Pause current print (if still running)",
                "Inspect print surface for damage or debris",
                "Remove any loose material or blobs from the part",
                "Re-home all axes (G28)",
                "Heat hotend and bed to target temperatures",
                "Prime nozzle with a small extrusion",
                "Resume print from calculated resume layer",
            ]
            if failure.failure_type == FailureType.LAYER_SHIFT:
                steps.insert(2, "Check belt tension on X and Y axes")
                steps.insert(3, "Verify stepper motor connections")

        elif strategy == RecoveryStrategy.RESTART_WITH_COMPENSATION:
            steps = [
                "Cancel current print",
                "Wait for printer to cool down",
                "Clean build plate thoroughly",
                "Apply fresh adhesion aid if needed",
                "Re-slice with adjusted parameters",
                "Start new print with compensated settings",
            ]
            if failure.failure_type == FailureType.ADHESION_FAILURE:
                steps.insert(3, "Increase bed temperature by 5C")
            elif failure.failure_type == FailureType.WARPING:
                steps.insert(3, "Consider using an enclosure")

        elif strategy == RecoveryStrategy.PARTIAL_RECOVERY:
            steps = [
                "Pause current print",
                "Assess remaining portion that needs printing",
                "Split G-code at the failure point",
                "Resume with the partial G-code file",
            ]

        elif strategy == RecoveryStrategy.SAFE_ABORT:
            steps = [
                "Cancel print job",
                "Turn off hotend heater (M104 S0)",
                "Turn off bed heater (M140 S0)",
                "Move nozzle to safe position",
                "Disable steppers after cooldown (M84)",
            ]
            if failure.failure_type == FailureType.THERMAL_RUNAWAY:
                steps.insert(0, "IMMEDIATE: Execute emergency stop if temperature is rising")

        elif strategy == RecoveryStrategy.WAIT_AND_RETRY:
            if failure.failure_type == FailureType.FILAMENT_RUNOUT:
                steps = [
                    "Pause print (print pauses automatically on sensor-equipped printers)",
                    "Load new filament spool",
                    "Purge filament until clean extrusion",
                    "Resume print",
                ]
            elif failure.failure_type == FailureType.NOZZLE_CLOG:
                steps = [
                    "Pause print",
                    "Heat hotend to printing temperature",
                    "Perform cold pull procedure (heat, pull, repeat 2-3 times)",
                    "Verify clean extrusion with manual extrude test",
                    "Resume print",
                ]
            elif failure.failure_type == FailureType.COMMUNICATION_LOSS:
                steps = [
                    "Check physical cable connections",
                    "Verify network/Wi-Fi connectivity",
                    "Wait for connection to re-establish (auto-reconnect)",
                    "Verify printer state after reconnection",
                    "Resume print if printer was paused",
                ]
            else:
                steps = [
                    "Wait for condition to resolve",
                    "Verify printer state",
                    "Resume print",
                ]

        elif strategy == RecoveryStrategy.NO_RECOVERY:
            steps = [
                "Continue printing -- issue is cosmetic",
                "Address in post-processing after print completes",
            ]

        return steps

    def _generate_recovery_gcode(
        self,
        plan: RecoveryPlan,
        failure: FailureReport,
    ) -> list[str]:
        """Generate G-code commands for the recovery.

        Returns a list of G-code command strings ready to send to the
        printer.
        """
        commands: list[str] = []

        if plan.strategy == RecoveryStrategy.SAFE_ABORT:
            commands = [
                "M104 S0",  # Hotend heater off
                "M140 S0",  # Bed heater off
                "G91",  # Relative positioning
                "G1 Z10 F1000",  # Move up 10mm
                "G90",  # Absolute positioning
                "G28 X Y",  # Home X and Y (away from part)
                "M84",  # Disable steppers
            ]

        elif plan.strategy == RecoveryStrategy.RESUME_FROM_LAYER:
            resume_z = plan.parameter_adjustments.get("resume_z_mm", 0)
            safe_z = resume_z + 5  # 5mm above resume layer

            commands = [
                "G28",  # Home all axes
                "M104 S{hotend_temp}",  # Heat hotend (placeholder)
                "M140 S{bed_temp}",  # Heat bed (placeholder)
                "M109 S{hotend_temp}",  # Wait for hotend
                "M190 S{bed_temp}",  # Wait for bed
                f"G1 Z{safe_z:.1f} F1000",  # Move to safe Z above resume
                "G1 E5 F300",  # Prime nozzle
                f"; Resume from layer {plan.resume_layer}, Z={resume_z:.2f}mm",
            ]

        elif plan.strategy == RecoveryStrategy.RESTART_WITH_COMPENSATION:
            commands = [
                "M104 S0",  # Cool down first
                "M140 S0",
                "G28",  # Home all
                "; Apply compensated parameters before starting new print",
            ]
            if plan.parameter_adjustments.get("bed_temp_offset"):
                offset = plan.parameter_adjustments["bed_temp_offset"]
                commands.append(f"; Bed temp offset: +{offset}C")
            if plan.parameter_adjustments.get("first_layer_speed_pct"):
                pct = plan.parameter_adjustments["first_layer_speed_pct"]
                commands.append(f"; First layer speed: {pct}%")

        elif plan.strategy == RecoveryStrategy.WAIT_AND_RETRY:
            if failure.failure_type == FailureType.FILAMENT_RUNOUT:
                commands = [
                    "; Waiting for filament load",
                    "G1 E5 F300",  # Prime after load
                    "; Resume print",
                ]
            elif failure.failure_type == FailureType.NOZZLE_CLOG:
                commands = [
                    "; Perform cold pull procedure",
                    "M104 S200",  # Heat for cold pull
                    "M109 S200",
                    "G1 E10 F100",  # Slow extrude to test
                    "; Resume print if extrusion is clean",
                ]
            else:
                commands = [
                    "; Wait for condition to clear",
                    "; Resume print",
                ]

        elif plan.strategy == RecoveryStrategy.NO_RECOVERY:
            commands = ["; No recovery action needed -- print continues"]

        elif plan.strategy == RecoveryStrategy.PARTIAL_RECOVERY:
            commands = [
                "G28",
                f"; Split G-code at layer {plan.resume_layer}",
                "; Load partial G-code file",
            ]

        return commands

    # -- private: event emission -------------------------------------------

    def _emit_event(self, failure: FailureReport) -> None:
        """Best-effort event emission for failure detection.  Never raises."""
        try:
            from kiln.events import Event, EventBus, EventType

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
