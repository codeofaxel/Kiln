"""Real-time health monitoring for FDM printers during active print jobs."""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from queue import Empty, Queue
from typing import Any, TextIO

from kiln.auto_record_hook import note_cancel_requested

logger = logging.getLogger(__name__)


def _works_the_filament(issue_type: str) -> bool:
    """Is this an issue the person fixes by working the filament (reload, cold pull)?"""
    from kiln.print_recovery import FailureType

    kinds = (FailureType.FILAMENT_RUNOUT, FailureType.NOZZLE_CLOG)
    return issue_type == "filament_out" or issue_type in {f"detect_failure_{k.value}" for k in kinds}


# ---------------------------------------------------------------------------
# FDM print phase definitions
# ---------------------------------------------------------------------------


class PrintPhase(str, enum.Enum):
    """Operational phases during an FDM print job."""

    HEATING = "heating"
    FIRST_LAYER = "first_layer"
    INFILL = "infill"
    PERIMETERS = "perimeters"
    SUPPORTS = "supports"
    TOP_LAYERS = "top_layers"
    COOLING = "cooling"
    IDLE = "idle"
    UNKNOWN = "unknown"


_FDM_PHASE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "first_layer": (0.0, 5.0),
    "infill": (5.0, 70.0),
    "perimeters": (70.0, 90.0),
    "top_layers": (90.0, 100.0),
}


def detect_print_phase(completion: float | None, *, is_heating: bool = False) -> PrintPhase:
    """Classify the FDM print phase from completion percentage."""
    if is_heating:
        return PrintPhase.HEATING

    if completion is None or completion < 0:
        return PrintPhase.UNKNOWN

    for phase_name, (low, high) in _FDM_PHASE_THRESHOLDS.items():
        if low <= completion < high:
            return PrintPhase(phase_name)

    if completion >= 100.0:
        return PrintPhase.TOP_LAYERS

    return PrintPhase.UNKNOWN


# ---------------------------------------------------------------------------
# Monitor status
# ---------------------------------------------------------------------------


class MonitorStatus(str, enum.Enum):
    """Status of a monitoring session."""

    MONITORING = "monitoring"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    STALLED = "stalled"


# ---------------------------------------------------------------------------
# Health metric severity
# ---------------------------------------------------------------------------


class HealthSeverity(str, enum.Enum):
    """Severity level for health metric deviations."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


# Metric names treated as "connection-only" criticals
_CONNECTION_HEALTH_METRICS: frozenset[str] = frozenset(
    {"connection_status", "connection_lost", "connection_health"}
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HealthMetric:
    """A single health measurement for one monitored dimension."""

    metric_name: str
    current_value: float
    expected_value: float
    deviation: float
    is_warning: bool
    timestamp: float
    severity: HealthSeverity = HealthSeverity.OK
    unit: str = ""
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass
class PrinterHealthReport:
    """Aggregated health report for a single printer at a point in time."""

    printer_name: str
    metrics: list[HealthMetric]
    overall_status: HealthSeverity
    checked_at: float
    phase: PrintPhase = PrintPhase.UNKNOWN
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_name": self.printer_name,
            "metrics": [m.to_dict() for m in self.metrics],
            "overall_status": self.overall_status.value,
            "checked_at": self.checked_at,
            "phase": self.phase.value,
            "session_id": self.session_id,
        }


@dataclass
class MonitorPolicy:
    """Configurable policy for printer health monitoring behavior."""

    check_delay_seconds: int = 60
    check_count: int = 5
    check_interval_seconds: int = 30
    auto_pause_on_failure: bool = True
    failure_confidence_threshold: float = 0.8
    require_camera: bool = False
    stall_timeout: int = 600
    temp_drift_threshold: float = 5.0
    history_max_hours: int = 72
    auto_cancel_on_emergency: bool = False
    session_timeout_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorPolicy:
        """Construct a :class:`MonitorPolicy` from a plain dictionary."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @classmethod
    def from_env(cls) -> MonitorPolicy:
        """Load policy overrides from environment variables."""
        policy = cls()

        _int_vars: list[tuple[str, str]] = [
            ("KILN_MONITOR_CHECK_DELAY", "check_delay_seconds"),
            ("KILN_MONITOR_CHECK_COUNT", "check_count"),
            ("KILN_MONITOR_CHECK_INTERVAL", "check_interval_seconds"),
            ("KILN_MONITOR_STALL_TIMEOUT", "stall_timeout"),
            ("KILN_MONITOR_HISTORY_MAX_HOURS", "history_max_hours"),
        ]
        for env_name, attr_name in _int_vars:
            env_val = os.environ.get(env_name)
            if env_val is not None:
                try:
                    setattr(policy, attr_name, int(env_val))
                except ValueError:
                    logger.warning("Invalid %s=%r", env_name, env_val)

        _bool_vars: list[tuple[str, str]] = [
            ("KILN_MONITOR_AUTO_PAUSE", "auto_pause_on_failure"),
            ("KILN_MONITOR_AUTO_CANCEL", "auto_cancel_on_emergency"),
            ("KILN_MONITOR_REQUIRE_CAMERA", "require_camera"),
        ]
        for env_name, attr_name in _bool_vars:
            env_val = os.environ.get(env_name)
            if env_val is not None:
                setattr(policy, attr_name, env_val.lower() in ("true", "1", "yes"))

        env_drift = os.environ.get("KILN_MONITOR_TEMP_DRIFT_THRESHOLD")
        if env_drift is not None:
            try:
                policy.temp_drift_threshold = float(env_drift)
            except ValueError:
                logger.warning("Invalid KILN_MONITOR_TEMP_DRIFT_THRESHOLD=%r", env_drift)

        env_timeout = os.environ.get("KILN_MONITOR_SESSION_TIMEOUT")
        if env_timeout is not None:
            try:
                policy.session_timeout_seconds = float(env_timeout)
            except ValueError:
                logger.warning("Invalid KILN_MONITOR_SESSION_TIMEOUT=%r", env_timeout)

        return policy


@dataclass
class MonitorSnapshot:
    """A single point-in-time snapshot of printer state during monitoring."""

    timestamp: float
    printer_name: str
    phase: str
    completion_pct: float
    hotend_temp: float | None = None
    hotend_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    image_b64: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class MonitorSession:
    """Tracks the lifecycle of a single printer monitoring session."""

    session_id: str
    printer_name: str
    job_id: str
    policy: MonitorPolicy
    snapshots: list[MonitorSnapshot] = field(default_factory=list)
    health_reports: list[PrinterHealthReport] = field(default_factory=list)
    status: MonitorStatus = MonitorStatus.MONITORING
    issues: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    latest_risk_assessment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "session_id": self.session_id,
            "printer_name": self.printer_name,
            "job_id": self.job_id,
            "policy": self.policy.to_dict(),
            "snapshots": [s.to_dict() for s in self.snapshots],
            "health_reports": [r.to_dict() for r in self.health_reports],
            "status": self.status.value,
            "issues": self.issues,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "latest_risk_assessment": self.latest_risk_assessment,
        }


# ---------------------------------------------------------------------------
# Stall tracking
# ---------------------------------------------------------------------------


@dataclass
class _StallTracker:
    """Internal state for per-session stall detection."""

    last_progress: float | None = None
    last_progress_time: float = field(default_factory=time.time)
    stalled: bool = False


# ---------------------------------------------------------------------------
# Emergency tracking for auto-cancel
# ---------------------------------------------------------------------------


@dataclass
class _EmergencyTracker:
    """Internal state for per-session auto-cancel decision making."""

    consecutive_thermal_critical: int = 0
    pause_attempted: bool = False
    cancel_attempted: bool = False


# ---------------------------------------------------------------------------
# Background monitor thread state
# ---------------------------------------------------------------------------


@dataclass
class _BackgroundMonitor:
    """Internal state for a background monitoring thread."""

    thread: threading.Thread
    stop_event: threading.Event
    session_id: str
    printer_name: str
    interval_seconds: float
    report_queue: Queue[PrinterHealthReport | None] | None = None
    output_stream: TextIO | None = None


# ---------------------------------------------------------------------------
# PrintHealthMonitor
# ---------------------------------------------------------------------------


class PrintHealthMonitor:
    """Manages real-time health monitoring sessions for FDM printers."""

    def __init__(self) -> None:
        self._sessions: dict[str, MonitorSession] = {}
        self._stall_state: dict[str, _StallTracker] = {}
        self._emergency_state: dict[str, _EmergencyTracker] = {}
        self._background_monitors: dict[str, _BackgroundMonitor] = {}
        self._health_history: dict[str, list[PrinterHealthReport]] = {}
        self._lock = threading.Lock()

    # -- public API: one-shot health check ---------------------------------

    def check_health(self, printer_name: str) -> PrinterHealthReport:
        """Perform a one-shot health check on a printer."""
        now = time.time()
        metrics: list[HealthMetric] = []
        policy = MonitorPolicy.from_env()

        # Lazy import to avoid circular dependency at module load time
        from kiln.server import _get_registry

        registry = _get_registry()
        adapter = registry.get(printer_name)
        from kiln.printers.engagement import internal_read

        with internal_read():
            state = adapter.get_state()

        # --- Hotend temperature stability ---
        if state.tool_temp_actual is not None and state.tool_temp_target is not None:
            hotend_deviation = abs(state.tool_temp_actual - state.tool_temp_target)
            hotend_warning = hotend_deviation > policy.temp_drift_threshold
            hotend_severity = HealthSeverity.OK
            hotend_detail = None
            if hotend_deviation > policy.temp_drift_threshold * 2:
                hotend_severity = HealthSeverity.CRITICAL
                hotend_detail = (
                    f"Hotend temperature drifted {hotend_deviation:.1f}°C "
                    f"from target {state.tool_temp_target:.0f}°C — possible "
                    "heater or thermistor issue"
                )
            elif hotend_warning:
                hotend_severity = HealthSeverity.WARNING
                hotend_detail = (
                    f"Hotend temperature drifted {hotend_deviation:.1f}°C from target {state.tool_temp_target:.0f}°C"
                )

            metrics.append(
                HealthMetric(
                    metric_name="hotend_temperature",
                    current_value=state.tool_temp_actual,
                    expected_value=state.tool_temp_target,
                    deviation=round(hotend_deviation, 2),
                    is_warning=hotend_warning,
                    timestamp=now,
                    severity=hotend_severity,
                    unit="°C",
                    detail=hotend_detail,
                )
            )

        # --- Bed temperature stability ---
        if state.bed_temp_actual is not None and state.bed_temp_target is not None:
            bed_deviation = abs(state.bed_temp_actual - state.bed_temp_target)
            bed_warning = bed_deviation > policy.temp_drift_threshold
            bed_severity = HealthSeverity.OK
            bed_detail = None
            if bed_deviation > policy.temp_drift_threshold * 2:
                bed_severity = HealthSeverity.CRITICAL
                bed_detail = (
                    f"Bed temperature drifted {bed_deviation:.1f}°C "
                    f"from target {state.bed_temp_target:.0f}°C — possible "
                    "heater fault or thermal runaway risk"
                )
            elif bed_warning:
                bed_severity = HealthSeverity.WARNING
                bed_detail = f"Bed temperature drifted {bed_deviation:.1f}°C from target {state.bed_temp_target:.0f}°C"

            metrics.append(
                HealthMetric(
                    metric_name="bed_temperature",
                    current_value=state.bed_temp_actual,
                    expected_value=state.bed_temp_target,
                    deviation=round(bed_deviation, 2),
                    is_warning=bed_warning,
                    timestamp=now,
                    severity=bed_severity,
                    unit="°C",
                    detail=bed_detail,
                )
            )

        # --- Print progress (layer completion rate) ---
        try:
            progress = adapter.get_job_progress()
            completion = progress.completion if progress.completion is not None else 0.0
            metrics.append(
                HealthMetric(
                    metric_name="print_progress",
                    current_value=completion,
                    expected_value=100.0,
                    deviation=round(100.0 - completion, 2),
                    is_warning=False,
                    timestamp=now,
                    severity=HealthSeverity.OK,
                    unit="%",
                )
            )
        except Exception as exc:
            logger.debug("Could not read print progress for %s: %s", printer_name, exc)

        # --- Filament sensor status ---
        filament_metric = self._check_filament_sensor(printer_name, now)
        if filament_metric is not None:
            metrics.append(filament_metric)

        # --- Power consumption anomalies ---
        power_metric = self._check_power_consumption(printer_name, now)
        if power_metric is not None:
            metrics.append(power_metric)

        # --- Webcam feed quality ---
        webcam_metric = self._check_webcam_quality(printer_name, now)
        if webcam_metric is not None:
            metrics.append(webcam_metric)

        # --- Connection health (unexpected shutdown detection) ---
        connection_severity = HealthSeverity.OK
        connection_warning = not state.connected
        if not state.connected:
            connection_severity = HealthSeverity.CRITICAL
        metrics.append(
            HealthMetric(
                metric_name="connection_status",
                current_value=1.0 if state.connected else 0.0,
                expected_value=1.0,
                deviation=0.0 if state.connected else 1.0,
                is_warning=connection_warning,
                timestamp=now,
                severity=connection_severity,
                unit="bool",
                detail="Printer is offline — possible unexpected shutdown" if not state.connected else None,
            )
        )

        # --- Determine overall status ---
        overall = HealthSeverity.OK
        for m in metrics:
            if m.severity == HealthSeverity.CRITICAL:
                overall = HealthSeverity.CRITICAL
                break
            if m.severity == HealthSeverity.WARNING:
                overall = HealthSeverity.WARNING

        # --- Determine print phase ---
        is_heating = (
            state.tool_temp_target is not None
            and state.tool_temp_actual is not None
            and state.tool_temp_actual < state.tool_temp_target - 10
        )
        completion_for_phase: float | None = None
        try:
            progress = adapter.get_job_progress()
            completion_for_phase = progress.completion
        except Exception as exc:
            logger.debug("Failed to get job progress for phase detection: %s", exc)
        phase = detect_print_phase(completion_for_phase, is_heating=is_heating)

        report = PrinterHealthReport(
            printer_name=printer_name,
            metrics=metrics,
            overall_status=overall,
            checked_at=now,
            phase=phase,
        )

        # Store in history
        self._append_history(printer_name, report)

        return report

    # -- public API: session-based monitoring ------------------------------

    def start_monitoring(
        self,
        printer_name: str,
        interval_seconds: float = 30,
        *,
        job_id: str | None = None,
        policy: MonitorPolicy | None = None,
        callback: Callable[[PrinterHealthReport], None] | None = None,
        output_stream: TextIO | None = None,
        enable_report_queue: bool = False,
    ) -> str:
        """Start background health monitoring for a printer."""
        with self._lock:
            if printer_name in self._background_monitors:
                raise ValueError(f"Printer {printer_name!r} already has an active monitoring session")

            session_id = str(uuid.uuid4())
            resolved_policy = policy or MonitorPolicy.from_env()
            resolved_job_id = job_id or f"auto-{session_id[:8]}"

            session = MonitorSession(
                session_id=session_id,
                printer_name=printer_name,
                job_id=resolved_job_id,
                policy=resolved_policy,
            )
            self._sessions[session_id] = session
            self._stall_state[session_id] = _StallTracker()
            self._emergency_state[session_id] = _EmergencyTracker()

            report_queue: Queue[PrinterHealthReport | None] | None = (
                Queue() if enable_report_queue else None
            )

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._monitor_loop,
                args=(session_id, printer_name, interval_seconds, stop_event, callback),
                daemon=True,
                name=f"kiln-health-monitor-{printer_name}",
            )

            bg = _BackgroundMonitor(
                thread=thread,
                stop_event=stop_event,
                session_id=session_id,
                printer_name=printer_name,
                interval_seconds=interval_seconds,
                report_queue=report_queue,
                output_stream=output_stream,
            )
            self._background_monitors[printer_name] = bg

        thread.start()
        logger.info(
            "Started health monitoring for printer=%s session=%s interval=%.0fs",
            printer_name,
            session_id,
            interval_seconds,
        )
        return session_id

    def stop_monitoring(self, printer_name: str) -> MonitorSession:
        """Stop background health monitoring for a printer."""
        with self._lock:
            bg = self._background_monitors.pop(printer_name, None)
            if bg is None:
                raise KeyError(f"No active monitoring session for printer {printer_name!r}")

        # Signal the thread to stop and wait for it
        bg.stop_event.set()
        bg.thread.join(timeout=bg.interval_seconds + 5)

        session = self._sessions.get(bg.session_id)
        if session is not None and session.status == MonitorStatus.MONITORING:
            session.status = MonitorStatus.COMPLETED
            session.ended_at = time.time()

        if bg.report_queue is not None:
            bg.report_queue.put(None)

        self._stall_state.pop(bg.session_id, None)
        self._emergency_state.pop(bg.session_id, None)
        logger.info(
            "Stopped health monitoring for printer=%s session=%s",
            printer_name,
            bg.session_id,
        )
        return session  # type: ignore[return-value]

    # -- public API: live report iteration ---------------------------------

    def iter_reports(
        self,
        session_id: str,
        *,
        timeout: float | None = None,
    ) -> Iterator[PrinterHealthReport]:
        """Yield health reports as they arrive on the session's queue."""
        # Resolve queue under the lock so we don't race a concurrent stop.
        queue: Queue[PrinterHealthReport | None] | None = None
        with self._lock:
            for bg in self._background_monitors.values():
                if bg.session_id == session_id:
                    queue = bg.report_queue
                    break

        if queue is None:
            raise KeyError(
                f"No active queue-enabled monitoring session for {session_id!r}; "
                "start_monitoring(... enable_report_queue=True) first."
            )

        while True:
            try:
                report = queue.get(timeout=timeout) if timeout is not None else queue.get()
            except Empty:
                return  # timeout — caller can decide whether to retry
            if report is None:
                return  # session ended
            yield report

    # -- public API: history -----------------------------------------------

    def get_health_history(
        self,
        printer_name: str,
        hours: float = 24,
    ) -> list[PrinterHealthReport]:
        """Retrieve health report history for a printer."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            all_reports = self._health_history.get(printer_name, [])
            return [r for r in all_reports if r.checked_at >= cutoff]

    # -- public API: session queries ---------------------------------------

    def get_session(self, session_id: str) -> MonitorSession:
        """Retrieve a monitoring session by ID."""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"Monitoring session {session_id!r} not found") from None

    def list_sessions(
        self,
        *,
        printer_name: str | None = None,
        status: MonitorStatus | None = None,
    ) -> list[MonitorSession]:
        """List monitoring sessions, optionally filtered."""
        results: list[MonitorSession] = []
        for session in self._sessions.values():
            if printer_name is not None and session.printer_name != printer_name:
                continue
            if status is not None and session.status != status:
                continue
            results.append(session)
        return results

    # -- public API: manual snapshot/issue reporting -----------------------

    def capture_snapshot(
        self,
        session_id: str,
        *,
        completion_pct: float | None = None,
        hotend_temp: float | None = None,
        hotend_target: float | None = None,
        bed_temp: float | None = None,
        bed_target: float | None = None,
        image_b64: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MonitorSnapshot:
        """Capture a manual snapshot for an active monitoring session."""
        session = self._get_active_session(session_id)
        pct = completion_pct if completion_pct is not None else 0.0

        is_heating = hotend_target is not None and hotend_temp is not None and hotend_temp < hotend_target - 10
        phase = detect_print_phase(pct, is_heating=is_heating)

        snapshot = MonitorSnapshot(
            timestamp=time.time(),
            printer_name=session.printer_name,
            phase=phase.value,
            completion_pct=pct,
            hotend_temp=hotend_temp,
            hotend_target=hotend_target,
            bed_temp=bed_temp,
            bed_target=bed_target,
            image_b64=image_b64,
            metadata=metadata or {},
        )

        session.snapshots.append(snapshot)
        logger.debug(
            "Captured snapshot %d for session %s (phase=%s, pct=%.1f)",
            len(session.snapshots),
            session_id,
            phase.value,
            pct,
        )

        # Stall detection
        stall_result = self._check_stall(session_id, pct)
        if stall_result is not None:
            snapshot.metadata["stall_alert"] = stall_result

        return snapshot

    def report_issue(
        self,
        session_id: str,
        issue_type: str,
        confidence: float,
        *,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Report a detected issue during a monitoring session."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")

        session = self._get_active_session(session_id)
        auto_pause = session.policy.auto_pause_on_failure and confidence >= session.policy.failure_confidence_threshold

        issue: dict[str, Any] = {
            "issue_type": issue_type,
            "confidence": confidence,
            "detail": detail,
            "auto_pause_triggered": auto_pause,
            "reported_at": time.time(),
            "snapshot_count": len(session.snapshots),
        }

        session.issues.append(issue)
        logger.info(
            "Issue reported for session %s: type=%s confidence=%.2f auto_pause=%s",
            session_id,
            issue_type,
            confidence,
            auto_pause,
        )

        if auto_pause:
            logger.warning(
                "Auto-pause triggered for session %s (issue=%s, confidence=%.2f)",
                session_id,
                issue_type,
                confidence,
            )
            self._honor_auto_pause(session, issue, issue_type)

        return issue

    # -- auto-pause honoring -----------------------------------------------

    @staticmethod
    def _pause_kill_switch_enabled() -> bool:
        """Return ``True`` iff the operator has flipped the kill-switch."""
        raw = os.environ.get("KILN_MONITOR_PAUSE_DISABLED", "")
        return raw.strip().lower() in {"true", "1", "yes"}

    def _honor_auto_pause(
        self,
        session: MonitorSession,
        issue: dict[str, Any],
        issue_type: str,
    ) -> None:
        """Best-effort, idempotent printer pause when auto_pause fires."""
        if self._pause_kill_switch_enabled():
            issue["auto_pause_skipped"] = "kill_switch"
            logger.info(
                "Auto-pause kill-switch active (KILN_MONITOR_PAUSE_DISABLED) — "
                "skipping pause for session %s issue %s",
                session.session_id,
                issue_type,
            )
            return

        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(session.printer_name)
        except Exception as exc:
            issue["auto_pause_error"] = repr(exc)
            logger.warning(
                "Auto-pause skipped for session %s: adapter lookup failed: %s",
                session.session_id,
                exc,
            )
            return

        try:
            from kiln.printers.engagement import internal_read

            with internal_read():
                state = adapter.get_state()
            already_paused = bool(getattr(state, "is_paused", False))
        except Exception as exc:
            already_paused = False
            logger.debug(
                "State probe failed for session %s; attempting pause anyway: %s",
                session.session_id,
                exc,
            )

        if already_paused:
            issue["auto_pause_skipped"] = "already_paused"
            logger.info(
                "Auto-pause skipped for session %s: printer already paused",
                session.session_id,
            )
            return

        try:
            # The shared pause engine
            from kiln.server import _pause_print_on

            filament_work = _works_the_filament(issue_type)
            _pause_print_on(
                adapter, session.printer_name, keep_temps=filament_work, leave_nozzle=filament_work,
            )
        except Exception as exc:
            issue["auto_pause_error"] = repr(exc)
            logger.warning(
                "Auto-pause failed for session %s issue %s: %s",
                session.session_id,
                issue_type,
                exc,
            )
            return

        logger.warning(
            "Auto-paused printer %s for issue %s (session %s)",
            session.printer_name,
            issue_type,
            session.session_id,
        )

    # -- background monitor loop -------------------------------------------

    def _monitor_loop(
        self,
        session_id: str,
        printer_name: str,
        interval_seconds: float,
        stop_event: threading.Event,
        callback: Callable[[PrinterHealthReport], None] | None,
    ) -> None:
        """Background thread loop that periodically checks printer health."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        # Initial delay before first check
        if stop_event.wait(timeout=session.policy.check_delay_seconds):
            return

        loop_started_at = time.time()
        session_timeout = max(0.0, float(session.policy.session_timeout_seconds))
        checks_remaining = session.policy.check_count
        while not stop_event.is_set() and checks_remaining > 0:
            if session_timeout > 0 and (time.time() - loop_started_at) >= session_timeout:
                with self._lock:
                    if session.status == MonitorStatus.MONITORING:
                        session.status = MonitorStatus.COMPLETED
                        session.ended_at = time.time()
                logger.info(
                    "Session %s timed out after %.0fs — stopping monitor loop",
                    session_id,
                    session_timeout,
                )
                break

            try:
                report = self.check_health(printer_name)
                report.session_id = session_id
                session.health_reports.append(report)

                self._fanout_report(session_id, report)

                if callback is not None:
                    try:
                        callback(report)
                    except Exception as cb_err:
                        logger.warning(
                            "Health monitor callback error for %s: %s",
                            printer_name,
                            cb_err,
                        )

                # Stall detection from health report progress metric
                for m in report.metrics:
                    if m.metric_name == "print_progress":
                        stall_result = self._check_stall(session_id, m.current_value)
                        if stall_result is not None:
                            self._publish_stall_event(stall_result)
                        break

                # Auto-pause on critical health
                pause_fired_this_tick = False
                if report.overall_status == HealthSeverity.CRITICAL and session.policy.auto_pause_on_failure:
                    critical_metric_names = {
                        m.metric_name for m in report.metrics if m.severity == HealthSeverity.CRITICAL
                    }
                    connection_only = bool(critical_metric_names) and critical_metric_names.issubset(
                        _CONNECTION_HEALTH_METRICS
                    )
                    if connection_only:
                        self.report_issue(
                            session_id,
                            "health_critical",
                            0.5,
                            detail=(
                                f"Connection-only critical on {printer_name}: "
                                + ", ".join(sorted(critical_metric_names))
                                + " — reconnect handler owns this; not pausing."
                            ),
                        )
                    else:
                        self.report_issue(
                            session_id,
                            "health_critical",
                            1.0,
                            detail=(
                                f"Critical health status detected on "
                                f"{printer_name}: "
                                + ", ".join(sorted(critical_metric_names))
                            ),
                        )
                        pause_fired_this_tick = True

                self._maybe_record_predictive_signals(session)

                self._maybe_detect_failure(session, report)

                # Auto-cancel — fire-class emergency only
                self._maybe_auto_cancel(session, report, pause_fired_this_tick)

            except KeyError:
                logger.error(
                    "Printer %s not found in registry, stopping monitor",
                    printer_name,
                )
                with self._lock:
                    if session.status == MonitorStatus.MONITORING:
                        session.status = MonitorStatus.FAILED
                        session.ended_at = time.time()
                break
            except Exception as exc:
                logger.error(
                    "Health check failed for %s: %s",
                    printer_name,
                    exc,
                )

            checks_remaining -= 1
            if checks_remaining > 0:
                stop_event.wait(timeout=interval_seconds)

        with self._lock:
            if session.status == MonitorStatus.MONITORING:
                session.status = MonitorStatus.COMPLETED
                session.ended_at = time.time()
            bg = self._background_monitors.pop(printer_name, None)
            self._stall_state.pop(session_id, None)
            self._emergency_state.pop(session_id, None)

        # Sentinel to release any iter_reports() consumer that's still blocked
        if bg is not None and bg.report_queue is not None:
            bg.report_queue.put(None)

    # -- live report fanout -------------------------------------------------

    def _fanout_report(
        self,
        session_id: str,
        report: PrinterHealthReport,
    ) -> None:
        """Publish *report* to the session's queue and JSON-Lines stream."""
        # Resolve the bg under the lock so we don't race a stop().
        bg: _BackgroundMonitor | None = None
        with self._lock:
            for candidate in self._background_monitors.values():
                if candidate.session_id == session_id:
                    bg = candidate
                    break
        if bg is None:
            return

        if bg.report_queue is not None:
            try:
                bg.report_queue.put(report)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug(
                    "iter_reports queue push failed for session %s: %s",
                    session_id,
                    exc,
                )

        if bg.output_stream is not None:
            try:
                envelope = self._build_jsonl_envelope(bg.printer_name, report)
                bg.output_stream.write(json.dumps(envelope, default=str) + "\n")
                bg.output_stream.flush()
            except Exception as exc:
                logger.debug(
                    "JSON-Lines stream write failed for session %s: %s",
                    session_id,
                    exc,
                )

    # -- JSON-Lines envelope builder ---------------------------------------

    def _build_jsonl_envelope(
        self,
        printer_name: str,
        report: PrinterHealthReport,
    ) -> dict[str, Any]:
        """Compose the per-tick JSON-Lines envelope written to ``output_stream``."""
        signals = self.get_latest_signals(printer_name)
        envelope: dict[str, Any] = {
            "ts": time.time(),
            "printer": printer_name,
            "session_id": report.session_id,
            "report": report.to_dict(),
            "signals": signals,
            "auto_recover": None,
            "reroute": None,
        }

        return envelope

    # -- auto-cancel honoring ----------------------------------------------

    _FIRE_CLASS_METRICS: frozenset[str] = frozenset(
        {"hotend_temperature", "bed_temperature"}
    )

    _AUTO_CANCEL_PERSISTENCE_TICKS: int = 2

    def _maybe_auto_cancel(
        self,
        session: MonitorSession,
        report: PrinterHealthReport,
        pause_fired_this_tick: bool,
    ) -> None:
        """Decide whether the session has earned an auto-cancel."""
        tracker = self._emergency_state.get(session.session_id)
        if tracker is None:
            return

        thermal_critical_now = any(
            m.severity == HealthSeverity.CRITICAL
            and m.metric_name in self._FIRE_CLASS_METRICS
            for m in report.metrics
        )

        if pause_fired_this_tick:
            tracker.pause_attempted = True

        if thermal_critical_now:
            tracker.consecutive_thermal_critical += 1
        else:
            tracker.consecutive_thermal_critical = 0

        if not session.policy.auto_cancel_on_emergency:
            return

        if tracker.cancel_attempted:
            return

        if (
            tracker.consecutive_thermal_critical < self._AUTO_CANCEL_PERSISTENCE_TICKS
            or not tracker.pause_attempted
        ):
            return

        # Conditions met — execute cancel best-effort.
        tracker.cancel_attempted = True
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(session.printer_name)
        except Exception as exc:
            logger.warning(
                "Auto-cancel skipped for session %s: adapter lookup failed: %s",
                session.session_id,
                exc,
            )
            return

        note_cancel_requested(adapter)
        try:
            adapter.cancel_print()
        except Exception as exc:
            logger.warning(
                "Auto-cancel failed for session %s: %s",
                session.session_id,
                exc,
            )
            return

        logger.warning(
            "Auto-cancelled printer %s — sustained thermal critical "
            "for %d consecutive checks after pause (session %s)",
            session.printer_name,
            tracker.consecutive_thermal_critical,
            session.session_id,
        )

        try:
            session.issues.append(
                {
                    "issue_type": "auto_cancel_emergency",
                    "confidence": 1.0,
                    "detail": (
                        "Sustained thermal critical for "
                        f"{tracker.consecutive_thermal_critical} consecutive checks "
                        "after pause attempt — printer cancelled."
                    ),
                    "auto_pause_triggered": False,
                    "auto_cancel_triggered": True,
                    "reported_at": time.time(),
                    "snapshot_count": len(session.snapshots),
                }
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "Failed to record auto-cancel issue on session %s: %s",
                session.session_id,
                exc,
            )

    # -- health check helpers ----------------------------------------------

    def _check_filament_sensor(self, printer_name: str, timestamp: float) -> HealthMetric | None:
        """Check filament sensor status if available."""
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)

            # Some adapters expose filament_detected via get_state metadata
            from kiln.printers.engagement import internal_read

            with internal_read():
                state = adapter.get_state()
            state_dict = state.to_dict()
            filament_detected = state_dict.get("filament_detected")
            if filament_detected is None:
                return None

            is_warning = not filament_detected
            return HealthMetric(
                metric_name="filament_sensor",
                current_value=1.0 if filament_detected else 0.0,
                expected_value=1.0,
                deviation=0.0 if filament_detected else 1.0,
                is_warning=is_warning,
                timestamp=timestamp,
                severity=HealthSeverity.CRITICAL if is_warning else HealthSeverity.OK,
                unit="bool",
                detail="Filament not detected — runout or sensor fault" if is_warning else None,
            )
        except Exception as exc:
            logger.debug("Filament sensor check failed for %s: %s", printer_name, exc)
            return None

    def _check_power_consumption(self, printer_name: str, timestamp: float) -> HealthMetric | None:
        """Check power consumption if telemetry is available."""
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)
            from kiln.printers.engagement import internal_read

            with internal_read():
                state = adapter.get_state()
            state_dict = state.to_dict()
            power_watts = state_dict.get("power_watts")
            if power_watts is None:
                return None

            expected_watts = 200.0
            deviation = abs(power_watts - expected_watts)
            is_anomaly = power_watts < 10.0 or power_watts > 600.0

            severity = HealthSeverity.OK
            detail = None
            if power_watts < 10.0:
                severity = HealthSeverity.CRITICAL
                detail = f"Power consumption anomaly: {power_watts:.0f}W — printer may have lost power"
            elif power_watts > 600.0:
                severity = HealthSeverity.WARNING
                detail = f"Power consumption anomaly: {power_watts:.0f}W — unusually high draw, check heater PIDs"

            return HealthMetric(
                metric_name="power_consumption",
                current_value=power_watts,
                expected_value=expected_watts,
                deviation=round(deviation, 2),
                is_warning=is_anomaly,
                timestamp=timestamp,
                severity=severity,
                unit="W",
                detail=detail,
            )
        except Exception as exc:
            logger.debug("Power consumption check failed for %s: %s", printer_name, exc)
            return None

    def _check_webcam_quality(self, printer_name: str, timestamp: float) -> HealthMetric | None:
        """Check webcam feed availability and quality."""
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)

            if not hasattr(adapter, "get_webcam_snapshot"):
                return None

            snapshot = adapter.get_webcam_snapshot()  # type: ignore[attr-defined]
            if snapshot is None:
                return HealthMetric(
                    metric_name="webcam_quality",
                    current_value=0.0,
                    expected_value=1.0,
                    deviation=1.0,
                    is_warning=True,
                    timestamp=timestamp,
                    severity=HealthSeverity.WARNING,
                    unit="bool",
                    detail="Webcam snapshot unavailable — feed may be offline",
                )

            return HealthMetric(
                metric_name="webcam_quality",
                current_value=1.0,
                expected_value=1.0,
                deviation=0.0,
                is_warning=False,
                timestamp=timestamp,
                severity=HealthSeverity.OK,
                unit="bool",
            )
        except Exception as exc:
            logger.debug("Webcam quality check failed for %s: %s", printer_name, exc)
            return None

    # -- stall detection ---------------------------------------------------

    def _check_stall(
        self,
        session_id: str,
        completion_pct: float,
    ) -> dict[str, Any] | None:
        """Check whether a session's print progress has stalled."""
        tracker = self._stall_state.get(session_id)
        if tracker is None or tracker.stalled:
            return None

        session = self._sessions.get(session_id)
        if session is None:
            return None

        stall_timeout = session.policy.stall_timeout
        if stall_timeout <= 0:
            return None  # stall detection disabled

        now = time.time()

        if tracker.last_progress is None or abs(completion_pct - tracker.last_progress) > 0.1:
            tracker.last_progress = completion_pct
            tracker.last_progress_time = now
            return None

        stall_duration = now - tracker.last_progress_time
        if stall_duration <= stall_timeout:
            return None

        # Stall detected
        tracker.stalled = True
        stall_duration_rounded = round(stall_duration, 1)

        session.status = MonitorStatus.STALLED
        session.ended_at = now

        alert_data: dict[str, Any] = {
            "alert_type": "stall",
            "printer_name": session.printer_name,
            "session_id": session_id,
            "completion_pct": completion_pct,
            "stall_duration_seconds": stall_duration_rounded,
            "stall_timeout": stall_timeout,
            "message": (
                f"Print job appears stalled at {completion_pct:.1f}% "
                f"for {stall_duration_rounded:.0f}s on printer "
                f"{session.printer_name!r}. "
                "Consider checking the printer or cancelling the job."
            ),
        }

        self._publish_stall_event(alert_data)

        session.issues.append(
            {
                "issue_type": "stall_detected",
                "confidence": 1.0,
                "detail": alert_data["message"],
                "auto_pause_triggered": session.policy.auto_pause_on_failure,
                "reported_at": now,
                "snapshot_count": len(session.snapshots),
            }
        )

        logger.warning(
            "Stall detected for session %s: printer=%s completion=%.1f%% stalled for %.0fs",
            session_id,
            session.printer_name,
            completion_pct,
            stall_duration_rounded,
        )

        return alert_data

    def _publish_stall_event(self, alert_data: dict[str, Any]) -> None:
        """Best-effort publish of a stall detection event."""
        try:
            import kiln.server as _srv
            from kiln.events import Event, EventType

            event = Event(
                type=EventType.PRINTER_ERROR,
                data=alert_data,
                source="print_health_monitor",
            )
            _srv._get_event_bus().publish(event)
            logger.info("Stall event published for printer=%s", alert_data.get("printer_name"))
        except Exception as exc:
            logger.debug("Failed to publish stall event: %s", exc)  # event delivery is best-effort

    # -- history management ------------------------------------------------

    def _append_history(self, printer_name: str, report: PrinterHealthReport) -> None:
        """Append a health report to history, pruning old entries."""
        with self._lock:
            if printer_name not in self._health_history:
                self._health_history[printer_name] = []

            history = self._health_history[printer_name]
            history.append(report)

            # Prune entries older than history_max_hours
            policy = MonitorPolicy.from_env()
            cutoff = time.time() - (policy.history_max_hours * 3600)
            self._health_history[printer_name] = [r for r in history if r.checked_at >= cutoff]

    # -- internal helpers --------------------------------------------------

    def _get_active_session(self, session_id: str) -> MonitorSession:
        """Retrieve a session and verify it is actively monitoring."""
        session = self.get_session(session_id)
        if session.status != MonitorStatus.MONITORING:
            raise ValueError(f"Session {session_id!r} is not actively monitoring (status={session.status.value})")
        return session

    @staticmethod
    def _telemetry_from_health_report(
        report: PrinterHealthReport,
    ) -> dict[str, Any]:
        """Translate a health report into a telemetry dict."""
        telemetry: dict[str, Any] = {"timestamp": report.checked_at}
        for m in report.metrics:
            if m.metric_name == "hotend_temperature":
                telemetry["hotend_temp"] = m.current_value
                telemetry["hotend_target"] = m.expected_value
            elif m.metric_name == "bed_temperature":
                telemetry["bed_temp"] = m.current_value
                telemetry["bed_target"] = m.expected_value
            elif m.metric_name == "connection_status":
                telemetry["connected"] = m.current_value >= 0.5
            elif m.metric_name == "filament_sensor":
                # Same shape: 1.0 means filament detected.
                telemetry["filament_detected"] = m.current_value >= 0.5
        return telemetry

    def _maybe_record_predictive_signals(
        self,
        session: MonitorSession,
    ) -> None:
        """Run the kiln-pro predictive risk heuristics against the accumulated health-report history; record red signals as issues."""
        try:
            from kiln_pro.recovery.predictive import predict_risk
        except ImportError:
            self._record_served_warning(session)
            return

        try:
            history = [
                self._telemetry_from_health_report(r)
                for r in session.health_reports
            ]
            if not history:
                return
            current = history[-1]
            assessment = predict_risk(
                telemetry=current,
                telemetry_history=history,
                printer_id=session.printer_name,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "Predictive risk scoring failed for session %s: %s",
                session.session_id,
                exc,
            )
            return

        session.latest_risk_assessment = assessment

        for signal in assessment.get("signals", []):
            if signal.get("severity") != "red":
                continue
            kind = signal.get("kind", "predictive")
            try:
                self.report_issue(
                    session.session_id,
                    f"predictive_red_{kind}",
                    1.0,
                    detail=signal.get("message"),
                )
            except (KeyError, ValueError) as exc:
                logger.debug(
                    "Could not record predictive issue for session %s: %s",
                    session.session_id,
                    exc,
                )

    def _record_served_warning(self, session: MonitorSession) -> None:
        """The failure warning from Kiln's servers, for an install without kiln-pro (:mod:`kiln._pro_print_warning_bridge`)."""
        if not session.health_reports:
            return
        from kiln import _pro_print_warning_bridge as served

        try:
            reading = self._telemetry_from_health_report(session.health_reports[-1])
            assessment = served.warning_for(session.printer_name, session.job_id, reading)
        except Exception as exc:  # noqa: BLE001 -- the monitor loop outlives a warning
            logger.debug("Served print warning failed for session %s: %s", session.session_id, exc)
            return
        if assessment is None:
            return
        session.latest_risk_assessment = assessment
        recorded = {i.get("issue_type") for i in session.issues}
        for issue_type, detail in served.issues_in(assessment):
            if issue_type in recorded:
                continue
            try:
                self.report_issue(session.session_id, issue_type, 1.0, detail=detail)
            except (KeyError, ValueError) as exc:
                logger.debug(
                    "Could not record predictive issue for session %s: %s",
                    session.session_id,
                    exc,
                )

    _DETECTIVE_SEVERITY_TO_CONFIDENCE: dict[str, float] = {
        "critical": 1.0,
        "high": 0.85,
        "medium": 0.6,
        "low": 0.4,
    }

    def _maybe_detect_failure(
        self,
        session: MonitorSession,
        report: PrinterHealthReport,
    ) -> None:
        """Run the reactive failure detector on the latest health report."""
        try:
            from kiln.print_recovery import get_recovery_engine
        except ImportError:
            return  # recovery engine unavailable — clean no-op

        try:
            telemetry = self._telemetry_from_health_report(report)
            job_info: dict[str, Any] = {
                "printer_name": session.printer_name,
                "file_name": session.job_id,
            }
            engine = get_recovery_engine()
            failure = engine.detect_failure(
                printer_name=session.printer_name,
                telemetry=telemetry,
                job_info=job_info,
                read_from_printer=True,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "Detective failure detection failed for session %s: %s",
                session.session_id,
                exc,
            )
            return

        if failure is None:
            return

        confidence = self._DETECTIVE_SEVERITY_TO_CONFIDENCE.get(
            (failure.severity or "").strip().lower(),
            0.4,
        )
        detail = failure.probable_cause or ", ".join(failure.evidence)

        try:
            self.report_issue(
                session.session_id,
                f"detect_failure_{failure.failure_type.value}",
                confidence,
                detail=detail or None,
            )
        except (KeyError, ValueError) as exc:
            logger.debug(
                "Could not record detective issue for session %s: %s",
                session.session_id,
                exc,
            )
            return

        if session.issues:
            session.issues[-1]["failure_id"] = failure.failure_id

    def get_latest_signals(self, printer_name: str) -> dict[str, Any]:
        """Return the most recent monitoring summary for a printer."""
        now = time.time()
        active_sessions = self.list_sessions(
            printer_name=printer_name,
            status=MonitorStatus.MONITORING,
        )
        if not active_sessions:
            return {
                "monitoring_active": False,
                "session_id": None,
                "session_started_at": None,
                "issue_count": 0,
                "report_count": 0,
                "risk": None,
                "predictive": None,
                "detective": None,
                "auto_pause": None,
                "as_of": now,
            }

        session = max(active_sessions, key=lambda s: s.started_at)

        # ---- Risk summary from the cached assessment ----
        risk: dict[str, Any] | None = None
        if session.latest_risk_assessment:
            assessment = session.latest_risk_assessment
            kinds = sorted({
                str(s.get("kind"))
                for s in assessment.get("signals", [])
                if s.get("severity") in ("amber", "red")
                and s.get("kind") is not None
            })
            risk = {
                "severity": assessment.get("severity", "clear"),
                "kinds": kinds,
            }
            if assessment.get("said"):
                risk["said"] = assessment["said"]

        predictive: dict[str, Any] | None = None
        detective: dict[str, Any] | None = None
        auto_pause: dict[str, Any] | None = None

        for issue in reversed(session.issues):
            issue_type = issue.get("issue_type", "")

            if predictive is None and issue_type.startswith("predictive_red_"):
                kind = issue_type[len("predictive_red_"):]
                predictive = {
                    "kind": kind,
                    "severity": "red",
                    "detail": issue.get("detail"),
                    "confidence": issue.get("confidence"),
                    "reported_at": issue.get("reported_at"),
                }
            elif detective is None and issue_type.startswith("detect_failure_"):
                failure_type = issue_type[len("detect_failure_"):]
                conf = issue.get("confidence")
                severity = _confidence_to_detective_severity(conf)
                detective = {
                    "failure_id": issue.get("failure_id"),
                    "failure_type": failure_type,
                    "severity": severity,
                    "detail": issue.get("detail"),
                    "confidence": conf,
                    "reported_at": issue.get("reported_at"),
                }

            if auto_pause is None and issue.get("auto_pause_triggered"):
                triggered_at = float(issue.get("reported_at") or now)
                auto_pause = {
                    "issue_type": issue_type or None,
                    "triggered_at": triggered_at,
                    "age_seconds": max(0.0, now - triggered_at),
                    "skipped": issue.get("auto_pause_skipped"),
                    "error": issue.get("auto_pause_error"),
                }

            if (
                predictive is not None
                and detective is not None
                and auto_pause is not None
            ):
                break

        return {
            "monitoring_active": True,
            "session_id": session.session_id,
            "session_started_at": session.started_at,
            "issue_count": len(session.issues),
            "report_count": len(session.health_reports),
            "risk": risk,
            "predictive": predictive,
            "detective": detective,
            "auto_pause": auto_pause,
            "as_of": now,
        }


def _confidence_to_detective_severity(confidence: float | None) -> str | None:
    """Inverse of ``_DETECTIVE_SEVERITY_TO_CONFIDENCE`` for surfacing."""
    if confidence is None:
        return None
    for severity, mapped in PrintHealthMonitor._DETECTIVE_SEVERITY_TO_CONFIDENCE.items():
        if abs(confidence - mapped) < 1e-6:
            return severity
    return None


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_print_health_monitor: PrintHealthMonitor | None = None
_singleton_lock = threading.Lock()


def get_print_health_monitor() -> PrintHealthMonitor:
    """Return the lazily-initialised global :class:`PrintHealthMonitor` instance."""
    global _print_health_monitor
    if _print_health_monitor is None:
        with _singleton_lock:
            if _print_health_monitor is None:
                _print_health_monitor = PrintHealthMonitor()
    return _print_health_monitor
