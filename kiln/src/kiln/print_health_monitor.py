"""Real-time health monitoring for FDM printers during active print jobs.

Captures periodic health snapshots during printing, tracks thermal
stability (hotend and bed), print progress (layer completion rate),
filament sensor status, power consumption anomalies, webcam feed
quality, and unexpected shutdown detection.  The monitor itself does
NOT run ML-based defect detection -- it provides structured health
reports that agents can inspect via vision models or heuristic checks.

Configure via environment variables:

    KILN_MONITOR_CHECK_DELAY       -- seconds before first check (default 60)
    KILN_MONITOR_CHECK_COUNT       -- number of snapshots per session (default 5)
    KILN_MONITOR_CHECK_INTERVAL    -- seconds between snapshots (default 30)
    KILN_MONITOR_AUTO_PAUSE        -- auto-pause on failure (default true)
    KILN_MONITOR_REQUIRE_CAMERA    -- refuse to start without camera (default false)
    KILN_MONITOR_STALL_TIMEOUT     -- seconds of no progress before stall (default 600)
    KILN_MONITOR_TEMP_DRIFT_THRESHOLD -- degrees C of acceptable temp drift (default 5.0)
    KILN_MONITOR_HISTORY_MAX_HOURS -- max hours of history to retain (default 72)
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FDM print phase definitions
# ---------------------------------------------------------------------------

class PrintPhase(str, enum.Enum):
    """Operational phases during an FDM print job.

    Phases are determined heuristically from completion percentage
    and detected printer behavior.
    """

    HEATING = "heating"
    FIRST_LAYER = "first_layer"
    INFILL = "infill"
    PERIMETERS = "perimeters"
    SUPPORTS = "supports"
    TOP_LAYERS = "top_layers"
    COOLING = "cooling"
    IDLE = "idle"
    UNKNOWN = "unknown"


_FDM_PHASE_THRESHOLDS: Dict[str, tuple[float, float]] = {
    "first_layer": (0.0, 5.0),
    "infill": (5.0, 70.0),
    "perimeters": (70.0, 90.0),
    "top_layers": (90.0, 100.0),
}


def detect_print_phase(completion: Optional[float], *, is_heating: bool = False) -> PrintPhase:
    """Classify the FDM print phase from completion percentage.

    :param completion: Completion percentage (0.0--100.0), or ``None``.
    :param is_heating: Whether the printer is still heating to target temps.
    :returns: The detected :class:`PrintPhase`.
    """
    if is_heating:
        return PrintPhase.HEATING

    if completion is None or completion < 0:
        return PrintPhase.UNKNOWN

    for phase_name, (low, high) in _FDM_PHASE_THRESHOLDS.items():
        if low <= completion < high:
            return PrintPhase(phase_name)

    # completion >= 100.0 -- return last phase
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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class HealthMetric:
    """A single health measurement for one monitored dimension.

    :param metric_name: Human-readable metric identifier
        (e.g. ``"hotend_temperature"``, ``"bed_temperature"``).
    :param current_value: The observed value at check time.
    :param expected_value: The target or baseline value.
    :param deviation: Absolute difference between current and expected.
    :param is_warning: Whether the deviation exceeds the warning threshold.
    :param timestamp: Unix timestamp when the metric was captured.
    :param severity: Overall severity classification.
    :param unit: Unit of measurement (e.g. ``"°C"``, ``"%"``, ``"W"``).
    :param detail: Optional human-readable context.
    """

    metric_name: str
    current_value: float
    expected_value: float
    deviation: float
    is_warning: bool
    timestamp: float
    severity: HealthSeverity = HealthSeverity.OK
    unit: str = ""
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass
class PrinterHealthReport:
    """Aggregated health report for a single printer at a point in time.

    :param printer_name: Name of the printer in the registry.
    :param metrics: List of individual health metrics.
    :param overall_status: Worst-case severity across all metrics.
    :param checked_at: Unix timestamp of the health check.
    :param phase: Detected print phase at check time.
    :param session_id: ID of the monitoring session that produced this report.
    """

    printer_name: str
    metrics: List[HealthMetric]
    overall_status: HealthSeverity
    checked_at: float
    phase: PrintPhase = PrintPhase.UNKNOWN
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
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
    """Configurable policy for printer health monitoring behavior.

    :param check_delay_seconds: Wait time after job start before
        the first snapshot.
    :param check_count: Number of snapshots to capture per session.
    :param check_interval_seconds: Seconds between snapshots.
    :param auto_pause_on_failure: Whether to auto-pause when a failure
        is reported back by the agent.
    :param failure_confidence_threshold: Minimum confidence score (0.0--1.0)
        to trigger auto-pause.
    :param require_camera: If *True*, refuse to start monitoring when the
        printer has no snapshot capability.
    :param stall_timeout: Seconds of no progress before declaring a stall
        (default 600 = 10 min).  Set to 0 to disable stall detection.
    :param temp_drift_threshold: Maximum acceptable temperature deviation
        in degrees Celsius before flagging a warning (default 5.0).
    :param history_max_hours: Maximum number of hours of health history
        to retain in memory (default 72).
    """

    check_delay_seconds: int = 60
    check_count: int = 5
    check_interval_seconds: int = 30
    auto_pause_on_failure: bool = True
    failure_confidence_threshold: float = 0.8
    require_camera: bool = False
    stall_timeout: int = 600
    temp_drift_threshold: float = 5.0
    history_max_hours: int = 72

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MonitorPolicy:
        """Construct a :class:`MonitorPolicy` from a plain dictionary.

        Unknown keys are silently ignored so forward-compatible config
        files don't break older code.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @classmethod
    def from_env(cls) -> MonitorPolicy:
        """Load policy overrides from environment variables.

        Env vars (highest precedence):

        - ``KILN_MONITOR_CHECK_DELAY``
        - ``KILN_MONITOR_CHECK_COUNT``
        - ``KILN_MONITOR_CHECK_INTERVAL``
        - ``KILN_MONITOR_AUTO_PAUSE``
        - ``KILN_MONITOR_REQUIRE_CAMERA``
        - ``KILN_MONITOR_STALL_TIMEOUT``
        - ``KILN_MONITOR_TEMP_DRIFT_THRESHOLD``
        - ``KILN_MONITOR_HISTORY_MAX_HOURS``
        """
        policy = cls()

        _int_vars: List[tuple[str, str]] = [
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

        _bool_vars: List[tuple[str, str]] = [
            ("KILN_MONITOR_AUTO_PAUSE", "auto_pause_on_failure"),
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

        return policy


@dataclass
class MonitorSnapshot:
    """A single point-in-time snapshot of printer state during monitoring.

    :param timestamp: Unix timestamp when the snapshot was captured.
    :param printer_name: Name of the monitored printer.
    :param phase: Detected print phase at capture time.
    :param completion_pct: Job completion percentage (0.0--100.0).
    :param hotend_temp: Hotend temperature in degrees Celsius.
    :param hotend_target: Hotend target temperature.
    :param bed_temp: Bed temperature in degrees Celsius.
    :param bed_target: Bed target temperature.
    :param image_b64: Optional base64-encoded webcam image.
    :param metadata: Arbitrary extra data (filament sensor, power, etc.).
    """

    timestamp: float
    printer_name: str
    phase: str
    completion_pct: float
    hotend_temp: Optional[float] = None
    hotend_target: Optional[float] = None
    bed_temp: Optional[float] = None
    bed_target: Optional[float] = None
    image_b64: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class MonitorSession:
    """Tracks the lifecycle of a single printer monitoring session.

    :param session_id: Unique identifier for this session.
    :param printer_name: Name of the monitored printer.
    :param job_id: Identifier of the job being monitored.
    :param policy: The monitoring policy governing this session.
    :param snapshots: Captured snapshots in chronological order.
    :param health_reports: Health reports generated during this session.
    :param status: Current session status.
    :param issues: Reported issues during this session.
    :param started_at: Unix timestamp when monitoring began.
    :param ended_at: Unix timestamp when monitoring ended (or ``None``).
    """

    session_id: str
    printer_name: str
    job_id: str
    policy: MonitorPolicy
    snapshots: List[MonitorSnapshot] = field(default_factory=list)
    health_reports: List[PrinterHealthReport] = field(default_factory=list)
    status: MonitorStatus = MonitorStatus.MONITORING
    issues: List[Dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
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
        }


# ---------------------------------------------------------------------------
# Stall tracking
# ---------------------------------------------------------------------------

@dataclass
class _StallTracker:
    """Internal state for per-session stall detection.

    Tracks the last observed progress value and the timestamp when
    progress last changed.
    """

    last_progress: Optional[float] = None
    last_progress_time: float = field(default_factory=time.time)
    stalled: bool = False


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


# ---------------------------------------------------------------------------
# PrintHealthMonitor
# ---------------------------------------------------------------------------

class PrintHealthMonitor:
    """Manages real-time health monitoring sessions for FDM printers.

    Maintains a registry of active and completed sessions, captures
    thermal snapshots, tracks print progress, and detects stalls,
    temperature drift, filament issues, and other anomalies.

    Usage::

        monitor = PrintHealthMonitor()

        # One-shot health check
        report = monitor.check_health("voron-350")

        # Session-based monitoring
        sid = monitor.start_monitoring("voron-350", interval_seconds=30)
        monitor.stop_monitoring("voron-350")

        # History
        history = monitor.get_health_history("voron-350", hours=24)
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, MonitorSession] = {}
        self._stall_state: Dict[str, _StallTracker] = {}
        self._background_monitors: Dict[str, _BackgroundMonitor] = {}
        self._health_history: Dict[str, List[PrinterHealthReport]] = {}
        self._lock = threading.Lock()

    # -- public API: one-shot health check ---------------------------------

    def check_health(self, printer_name: str) -> PrinterHealthReport:
        """Perform a one-shot health check on a printer.

        Queries the printer adapter for current state and temperatures,
        then evaluates thermal stability, progress rate, and sensor
        status.

        :param printer_name: Name of the printer in the registry.
        :returns: A health report with all evaluated metrics.
        :raises KeyError: If *printer_name* is not in the registry.
        """
        now = time.time()
        metrics: List[HealthMetric] = []
        policy = MonitorPolicy.from_env()

        # Lazy import to avoid circular dependency at module load time
        from kiln.registry import get_printer_registry

        registry = get_printer_registry()
        adapter = registry.get(printer_name)
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
                    f"Hotend temperature drifted {hotend_deviation:.1f}°C "
                    f"from target {state.tool_temp_target:.0f}°C"
                )

            metrics.append(HealthMetric(
                metric_name="hotend_temperature",
                current_value=state.tool_temp_actual,
                expected_value=state.tool_temp_target,
                deviation=round(hotend_deviation, 2),
                is_warning=hotend_warning,
                timestamp=now,
                severity=hotend_severity,
                unit="°C",
                detail=hotend_detail,
            ))

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
                bed_detail = (
                    f"Bed temperature drifted {bed_deviation:.1f}°C "
                    f"from target {state.bed_temp_target:.0f}°C"
                )

            metrics.append(HealthMetric(
                metric_name="bed_temperature",
                current_value=state.bed_temp_actual,
                expected_value=state.bed_temp_target,
                deviation=round(bed_deviation, 2),
                is_warning=bed_warning,
                timestamp=now,
                severity=bed_severity,
                unit="°C",
                detail=bed_detail,
            ))

        # --- Print progress (layer completion rate) ---
        try:
            progress = adapter.get_job_progress()
            completion = progress.completion if progress.completion is not None else 0.0
            metrics.append(HealthMetric(
                metric_name="print_progress",
                current_value=completion,
                expected_value=100.0,
                deviation=round(100.0 - completion, 2),
                is_warning=False,
                timestamp=now,
                severity=HealthSeverity.OK,
                unit="%",
            ))
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
        metrics.append(HealthMetric(
            metric_name="connection_status",
            current_value=1.0 if state.connected else 0.0,
            expected_value=1.0,
            deviation=0.0 if state.connected else 1.0,
            is_warning=connection_warning,
            timestamp=now,
            severity=connection_severity,
            unit="bool",
            detail="Printer is offline — possible unexpected shutdown" if not state.connected else None,
        ))

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
        completion_for_phase: Optional[float] = None
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
        job_id: Optional[str] = None,
        policy: Optional[MonitorPolicy] = None,
        callback: Optional[Callable[[PrinterHealthReport], None]] = None,
    ) -> str:
        """Start background health monitoring for a printer.

        Spawns a daemon thread that periodically calls :meth:`check_health`
        and stores the results.

        :param printer_name: Name of the printer in the registry.
        :param interval_seconds: Seconds between health checks.
        :param job_id: Optional job identifier to associate with the session.
        :param policy: Optional custom monitoring policy.
        :param callback: Optional function invoked with each health report.
        :returns: The session ID.
        :raises ValueError: If the printer already has an active monitor.
        """
        with self._lock:
            if printer_name in self._background_monitors:
                raise ValueError(
                    f"Printer {printer_name!r} already has an active "
                    "monitoring session"
                )

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
            )
            self._background_monitors[printer_name] = bg

        thread.start()
        logger.info(
            "Started health monitoring for printer=%s session=%s interval=%.0fs",
            printer_name, session_id, interval_seconds,
        )
        return session_id

    def stop_monitoring(self, printer_name: str) -> MonitorSession:
        """Stop background health monitoring for a printer.

        :param printer_name: Name of the printer to stop monitoring.
        :returns: The final session state.
        :raises KeyError: If no active monitor exists for the printer.
        """
        with self._lock:
            bg = self._background_monitors.pop(printer_name, None)
            if bg is None:
                raise KeyError(
                    f"No active monitoring session for printer {printer_name!r}"
                )

        # Signal the thread to stop and wait for it
        bg.stop_event.set()
        bg.thread.join(timeout=bg.interval_seconds + 5)

        session = self._sessions.get(bg.session_id)
        if session is not None and session.status == MonitorStatus.MONITORING:
            session.status = MonitorStatus.COMPLETED
            session.ended_at = time.time()

        self._stall_state.pop(bg.session_id, None)
        logger.info(
            "Stopped health monitoring for printer=%s session=%s",
            printer_name, bg.session_id,
        )
        return session  # type: ignore[return-value]

    # -- public API: history -----------------------------------------------

    def get_health_history(
        self,
        printer_name: str,
        hours: float = 24,
    ) -> List[PrinterHealthReport]:
        """Retrieve health report history for a printer.

        :param printer_name: Name of the printer.
        :param hours: How many hours of history to return (default 24).
        :returns: List of health reports within the time window,
            ordered chronologically (oldest first).
        """
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            all_reports = self._health_history.get(printer_name, [])
            return [r for r in all_reports if r.checked_at >= cutoff]

    # -- public API: session queries ---------------------------------------

    def get_session(self, session_id: str) -> MonitorSession:
        """Retrieve a monitoring session by ID.

        :param session_id: The session to look up.
        :returns: The session.
        :raises KeyError: If *session_id* is not found.
        """
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"Monitoring session {session_id!r} not found")

    def list_sessions(
        self,
        *,
        printer_name: Optional[str] = None,
        status: Optional[MonitorStatus] = None,
    ) -> List[MonitorSession]:
        """List monitoring sessions, optionally filtered.

        :param printer_name: If provided, only return sessions for this printer.
        :param status: If provided, only return sessions with this status.
        :returns: List of matching sessions.
        """
        results: List[MonitorSession] = []
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
        completion_pct: Optional[float] = None,
        hotend_temp: Optional[float] = None,
        hotend_target: Optional[float] = None,
        bed_temp: Optional[float] = None,
        bed_target: Optional[float] = None,
        image_b64: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MonitorSnapshot:
        """Capture a manual snapshot for an active monitoring session.

        :param session_id: The active session to capture for.
        :param completion_pct: Job completion percentage (0.0--100.0).
        :param hotend_temp: Current hotend temperature in °C.
        :param hotend_target: Target hotend temperature in °C.
        :param bed_temp: Current bed temperature in °C.
        :param bed_target: Target bed temperature in °C.
        :param image_b64: Optional base64-encoded webcam image.
        :param metadata: Optional extra data dict.
        :returns: The captured snapshot.
        :raises KeyError: If *session_id* is not found.
        :raises ValueError: If the session is not actively monitoring.
        """
        session = self._get_active_session(session_id)
        pct = completion_pct if completion_pct is not None else 0.0

        is_heating = (
            hotend_target is not None
            and hotend_temp is not None
            and hotend_temp < hotend_target - 10
        )
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
            len(session.snapshots), session_id, phase.value, pct,
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
        detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Report a detected issue during a monitoring session.

        If the session policy has ``auto_pause_on_failure`` enabled and
        the confidence exceeds the threshold, the issue is flagged for
        auto-pause (the caller is responsible for actually pausing the
        printer).

        :param session_id: The session to report against.
        :param issue_type: Category of the issue
            (e.g. ``"thermal_runaway"``, ``"filament_out"``,
            ``"layer_shift"``, ``"adhesion_failure"``).
        :param confidence: Confidence score (0.0--1.0).
        :param detail: Optional human-readable description.
        :returns: Issue record dict including ``auto_pause_triggered``.
        :raises KeyError: If *session_id* is not found.
        :raises ValueError: If the session is not actively monitoring,
            or if confidence is outside 0.0--1.0.
        """
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"Confidence must be between 0.0 and 1.0, got {confidence}"
            )

        session = self._get_active_session(session_id)
        auto_pause = (
            session.policy.auto_pause_on_failure
            and confidence >= session.policy.failure_confidence_threshold
        )

        issue: Dict[str, Any] = {
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
            session_id, issue_type, confidence, auto_pause,
        )

        if auto_pause:
            logger.warning(
                "Auto-pause triggered for session %s (issue=%s, confidence=%.2f)",
                session_id, issue_type, confidence,
            )

        return issue

    # -- background monitor loop -------------------------------------------

    def _monitor_loop(
        self,
        session_id: str,
        printer_name: str,
        interval_seconds: float,
        stop_event: threading.Event,
        callback: Optional[Callable[[PrinterHealthReport], None]],
    ) -> None:
        """Background thread loop that periodically checks printer health.

        Runs until the stop event is set or an unrecoverable error occurs.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return

        # Initial delay before first check
        if stop_event.wait(timeout=session.policy.check_delay_seconds):
            return

        checks_remaining = session.policy.check_count
        while not stop_event.is_set() and checks_remaining > 0:
            try:
                report = self.check_health(printer_name)
                report.session_id = session_id
                session.health_reports.append(report)

                if callback is not None:
                    try:
                        callback(report)
                    except Exception as cb_err:
                        logger.warning(
                            "Health monitor callback error for %s: %s",
                            printer_name, cb_err,
                        )

                # Stall detection from health report progress metric
                for m in report.metrics:
                    if m.metric_name == "print_progress":
                        stall_result = self._check_stall(
                            session_id, m.current_value
                        )
                        if stall_result is not None:
                            self._publish_stall_event(stall_result)
                        break

                # Auto-pause on critical health
                if report.overall_status == HealthSeverity.CRITICAL:
                    if session.policy.auto_pause_on_failure:
                        self.report_issue(
                            session_id,
                            "health_critical",
                            1.0,
                            detail=(
                                f"Critical health status detected on "
                                f"{printer_name}: "
                                + ", ".join(
                                    m.metric_name
                                    for m in report.metrics
                                    if m.severity == HealthSeverity.CRITICAL
                                )
                            ),
                        )

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
                    "Health check failed for %s: %s", printer_name, exc,
                )

            checks_remaining -= 1
            if checks_remaining > 0:
                stop_event.wait(timeout=interval_seconds)

        # Session completed naturally if it wasn't stopped or failed
        with self._lock:
            if session.status == MonitorStatus.MONITORING:
                session.status = MonitorStatus.COMPLETED
                session.ended_at = time.time()
            self._background_monitors.pop(printer_name, None)
            self._stall_state.pop(session_id, None)

    # -- health check helpers ----------------------------------------------

    def _check_filament_sensor(
        self, printer_name: str, timestamp: float
    ) -> Optional[HealthMetric]:
        """Check filament sensor status if available.

        Returns a metric if the adapter exposes filament sensor data,
        otherwise returns ``None``.
        """
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)

            # Some adapters expose filament_detected via get_state metadata
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

    def _check_power_consumption(
        self, printer_name: str, timestamp: float
    ) -> Optional[HealthMetric]:
        """Check power consumption if telemetry is available.

        Returns a metric if the adapter or plugin reports wattage,
        otherwise returns ``None``.  Power anomalies are detected by
        comparing against a baseline range (50W-500W for typical FDM).
        """
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)
            state = adapter.get_state()
            state_dict = state.to_dict()
            power_watts = state_dict.get("power_watts")
            if power_watts is None:
                return None

            # Heuristic baseline for FDM printers: 50-500W under load
            expected_watts = 200.0
            deviation = abs(power_watts - expected_watts)
            is_anomaly = power_watts < 10.0 or power_watts > 600.0

            severity = HealthSeverity.OK
            detail = None
            if power_watts < 10.0:
                severity = HealthSeverity.CRITICAL
                detail = (
                    f"Power consumption anomaly: {power_watts:.0f}W — "
                    "printer may have lost power"
                )
            elif power_watts > 600.0:
                severity = HealthSeverity.WARNING
                detail = (
                    f"Power consumption anomaly: {power_watts:.0f}W — "
                    "unusually high draw, check heater PIDs"
                )

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

    def _check_webcam_quality(
        self, printer_name: str, timestamp: float
    ) -> Optional[HealthMetric]:
        """Check webcam feed availability and quality.

        Returns a metric if the printer has a camera configured.
        Quality is assessed by whether a snapshot can be retrieved
        (not by image content analysis, which is left to agents).
        """
        try:
            from kiln.registry import get_printer_registry

            registry = get_printer_registry()
            adapter = registry.get(printer_name)

            # Check if adapter supports camera snapshots
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
    ) -> Optional[Dict[str, Any]]:
        """Check whether a session's print progress has stalled.

        Compares the current progress value against the last recorded
        value.  If the value has not changed by more than 0.1% for
        longer than the policy's ``stall_timeout``, the session is
        marked as stalled and an alert dict is returned.

        :returns: Alert dict if stalled, else ``None``.
        """
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

        # Check if progress has advanced
        if (
            tracker.last_progress is None
            or abs(completion_pct - tracker.last_progress) > 0.1
        ):
            tracker.last_progress = completion_pct
            tracker.last_progress_time = now
            return None

        # Progress unchanged -- check if stall timeout exceeded
        stall_duration = now - tracker.last_progress_time
        if stall_duration <= stall_timeout:
            return None

        # Stall detected
        tracker.stalled = True
        stall_duration_rounded = round(stall_duration, 1)

        session.status = MonitorStatus.STALLED
        session.ended_at = now

        alert_data: Dict[str, Any] = {
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

        session.issues.append({
            "issue_type": "stall_detected",
            "confidence": 1.0,
            "detail": alert_data["message"],
            "auto_pause_triggered": session.policy.auto_pause_on_failure,
            "reported_at": now,
            "snapshot_count": len(session.snapshots),
        })

        logger.warning(
            "Stall detected for session %s: printer=%s completion=%.1f%% "
            "stalled for %.0fs",
            session_id, session.printer_name, completion_pct,
            stall_duration_rounded,
        )

        return alert_data

    def _publish_stall_event(self, alert_data: Dict[str, Any]) -> None:
        """Best-effort publish of a stall detection event."""
        try:
            from kiln.events import EventType, get_event_bus, Event

            bus = get_event_bus()
            event = Event(
                type=EventType.PRINTER_ERROR,
                data=alert_data,
                source="print_health_monitor",
            )
            bus.publish(event)
            logger.info("Stall event published for printer=%s", alert_data.get("printer_name"))
        except Exception as exc:
            logger.debug("Failed to publish stall event: %s", exc)  # event delivery is best-effort

    # -- history management ------------------------------------------------

    def _append_history(
        self, printer_name: str, report: PrinterHealthReport
    ) -> None:
        """Append a health report to history, pruning old entries."""
        with self._lock:
            if printer_name not in self._health_history:
                self._health_history[printer_name] = []

            history = self._health_history[printer_name]
            history.append(report)

            # Prune entries older than history_max_hours
            policy = MonitorPolicy.from_env()
            cutoff = time.time() - (policy.history_max_hours * 3600)
            self._health_history[printer_name] = [
                r for r in history if r.checked_at >= cutoff
            ]

    # -- internal helpers --------------------------------------------------

    def _get_active_session(self, session_id: str) -> MonitorSession:
        """Retrieve a session and verify it is actively monitoring.

        :raises KeyError: If not found.
        :raises ValueError: If not in ``monitoring`` status.
        """
        session = self.get_session(session_id)
        if session.status != MonitorStatus.MONITORING:
            raise ValueError(
                f"Session {session_id!r} is not actively monitoring "
                f"(status={session.status.value})"
            )
        return session


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_print_health_monitor: Optional[PrintHealthMonitor] = None
_singleton_lock = threading.Lock()


def get_print_health_monitor() -> PrintHealthMonitor:
    """Return the lazily-initialised global :class:`PrintHealthMonitor` instance.

    Thread-safe via double-checked locking.
    """
    global _print_health_monitor
    if _print_health_monitor is None:
        with _singleton_lock:
            if _print_health_monitor is None:
                _print_health_monitor = PrintHealthMonitor()
    return _print_health_monitor
