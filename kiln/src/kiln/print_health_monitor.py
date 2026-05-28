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


_FDM_PHASE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "first_layer": (0.0, 5.0),
    "infill": (5.0, 70.0),
    "perimeters": (70.0, 90.0),
    "top_layers": (90.0, 100.0),
}


def detect_print_phase(completion: float | None, *, is_heating: bool = False) -> PrintPhase:
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


# Metric names treated as "connection-only" criticals.  When the only
# CRITICAL metrics in a health report are in this set, the monitor
# downgrades the issue confidence below the auto-pause threshold —
# brief disconnects belong to the reconnect path, not the pause path.
# Future-proofed against rename / alias drift in connection metrics.
_CONNECTION_HEALTH_METRICS: frozenset[str] = frozenset(
    {"connection_status", "connection_lost", "connection_health"}
)


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
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
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
    :param auto_cancel_on_emergency: When *True* AND a "fire-class"
        emergency is detected (sustained thermal runaway after at
        least one prior pause attempt), the adapter's
        :meth:`cancel_print` is invoked.  Conservative — only fires
        after pause already failed to bring the printer back into
        spec.  Default *False* so existing behavior is unchanged.
    :param session_timeout_seconds: Optional wall-clock cap on a
        monitoring session.  When > 0, the background loop exits
        after this many seconds even if ``check_count`` is not
        exhausted.  Default 0 = unlimited.
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
    auto_cancel_on_emergency: bool = False
    session_timeout_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorPolicy:
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
        - ``KILN_MONITOR_AUTO_CANCEL``
        - ``KILN_MONITOR_REQUIRE_CAMERA``
        - ``KILN_MONITOR_STALL_TIMEOUT``
        - ``KILN_MONITOR_TEMP_DRIFT_THRESHOLD``
        - ``KILN_MONITOR_HISTORY_MAX_HOURS``
        - ``KILN_MONITOR_SESSION_TIMEOUT``
        """
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
    snapshots: list[MonitorSnapshot] = field(default_factory=list)
    health_reports: list[PrinterHealthReport] = field(default_factory=list)
    status: MonitorStatus = MonitorStatus.MONITORING
    issues: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    # Cached most recent predictive RiskAssessment dict (kiln-pro
    # predict_risk output).  Populated by _maybe_record_predictive_signals
    # so monitor_print one-shot can surface the headline risk_score +
    # severity without re-running the predictor.  None until the first
    # tick scores a non-empty assessment.
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
    """Internal state for per-session stall detection.

    Tracks the last observed progress value and the timestamp when
    progress last changed.
    """

    last_progress: float | None = None
    last_progress_time: float = field(default_factory=time.time)
    stalled: bool = False


# ---------------------------------------------------------------------------
# Emergency tracking for auto-cancel
# ---------------------------------------------------------------------------


@dataclass
class _EmergencyTracker:
    """Internal state for per-session auto-cancel decision making.

    Auto-cancel only fires when a thermal critical metric persists
    across consecutive ticks AND a pause has already been attempted
    on the session.  This prevents single-tick noise from cancelling
    a print and lets pause act as the first line of defense.

    :param consecutive_thermal_critical: Number of back-to-back ticks
        with a CRITICAL hotend or bed temperature metric.
    :param pause_attempted: Whether the session has already issued at
        least one pause attempt (regardless of outcome).
    :param cancel_attempted: Whether the auto-cancel path has already
        fired on this session (idempotency guard).
    """

    consecutive_thermal_critical: int = 0
    pause_attempted: bool = False
    cancel_attempted: bool = False


# ---------------------------------------------------------------------------
# Background monitor thread state
# ---------------------------------------------------------------------------


@dataclass
class _BackgroundMonitor:
    """Internal state for a background monitoring thread.

    :param thread: The daemon thread running :meth:`_monitor_loop`.
    :param stop_event: Set to request a clean shutdown.
    :param session_id: ID of the session this monitor backs.
    :param printer_name: Printer name for registry lookup.
    :param interval_seconds: Wall-clock seconds between checks.
    :param report_queue: When set, every :class:`PrinterHealthReport`
        produced by the loop is enqueued here so consumers (the CLI,
        future event hooks) can stream reports in order without
        polling session state.  When *None*, the loop runs without
        the queueing overhead — agent / MCP callers don't need it.
    :param output_stream: When set, every report is also serialised
        as a JSON line to this stream (one ``json.dumps(report.to_dict())``
        per line, flushed after each write).  Used by the CLI's
        ``--json`` mode and by any consumer that wants a raw NDJSON
        feed without subscribing to the queue.
    """

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
        self._sessions: dict[str, MonitorSession] = {}
        self._stall_state: dict[str, _StallTracker] = {}
        self._emergency_state: dict[str, _EmergencyTracker] = {}
        self._background_monitors: dict[str, _BackgroundMonitor] = {}
        self._health_history: dict[str, list[PrinterHealthReport]] = {}
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
        metrics: list[HealthMetric] = []
        policy = MonitorPolicy.from_env()

        # Lazy import to avoid circular dependency at module load time
        from kiln.server import _get_registry

        registry = _get_registry()
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
        """Start background health monitoring for a printer.

        Spawns a daemon thread that periodically calls :meth:`check_health`
        and stores the results.

        :param printer_name: Name of the printer in the registry.
        :param interval_seconds: Seconds between health checks.
        :param job_id: Optional job identifier to associate with the session.
        :param policy: Optional custom monitoring policy.
        :param callback: Optional function invoked with each health report.
        :param output_stream: Optional text stream to receive a JSON line
            per report (NDJSON / JSON-Lines format).  Each line is a
            ``json.dumps`` of :meth:`PrinterHealthReport.to_dict`.  The
            stream is flushed after every write.  Used by the
            ``kiln monitor --json`` CLI mode and any agent that wants a
            raw report feed.  Best-effort: errors writing to the stream
            are logged at debug level and do not interrupt monitoring.
        :param enable_report_queue: When *True*, attaches a thread-safe
            queue to the session so :meth:`iter_reports` can yield each
            report as it arrives.  When *False* (default), reports are
            still appended to ``session.health_reports`` but no live
            iterator is exposed.  Defaults to off so existing MCP
            callers don't pay the queue overhead.
        :returns: The session ID.
        :raises ValueError: If the printer already has an active monitor.
        """
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
        """Stop background health monitoring for a printer.

        :param printer_name: Name of the printer to stop monitoring.
        :returns: The final session state.
        :raises KeyError: If no active monitor exists for the printer.
        """
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

        # Sentinel to release any iter_reports consumer that's still
        # blocked waiting for the next report.  Only needed when the
        # session was started with enable_report_queue=True.
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
        """Yield health reports as they arrive on the session's queue.

        Requires the session to have been started with
        ``enable_report_queue=True``.  Yields each report in the order
        the monitor loop produced it; terminates when the loop signals
        completion (by enqueueing ``None``) or when the optional
        per-yield ``timeout`` elapses without a new report.

        Example::

            sid = monitor.start_monitoring(name, enable_report_queue=True)
            for report in monitor.iter_reports(sid, timeout=60):
                render(report)

        :param session_id: ID of the active session.
        :param timeout: Maximum seconds to wait for the next report.
            ``None`` blocks indefinitely until a report arrives or the
            session ends.
        :yields: :class:`PrinterHealthReport` objects in chronological
            order.
        :raises KeyError: If *session_id* has no active background
            monitor or wasn't started with ``enable_report_queue=True``.
        """
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
            raise KeyError(f"Monitoring session {session_id!r} not found") from None

    def list_sessions(
        self,
        *,
        printer_name: str | None = None,
        status: MonitorStatus | None = None,
    ) -> list[MonitorSession]:
        """List monitoring sessions, optionally filtered.

        :param printer_name: If provided, only return sessions for this printer.
        :param status: If provided, only return sessions with this status.
        :returns: List of matching sessions.
        """
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
        """Report a detected issue during a monitoring session.

        If the session policy has ``auto_pause_on_failure`` enabled and
        the confidence exceeds the threshold, the printer adapter's
        ``pause_print()`` is invoked best-effort.  The pause call is
        idempotent (skipped when the adapter already reports paused),
        gated by the ``KILN_MONITOR_PAUSE_DISABLED`` env-var
        kill-switch (set to ``"true"``, ``"1"``, or ``"yes"`` to
        disable), and never raises out — failures are surfaced via
        the ``auto_pause_error`` field on the returned issue dict.

        :param session_id: The session to report against.
        :param issue_type: Category of the issue
            (e.g. ``"thermal_runaway"``, ``"filament_out"``,
            ``"layer_shift"``, ``"adhesion_failure"``).
        :param confidence: Confidence score (0.0--1.0).
        :param detail: Optional human-readable description.
        :returns: Issue record dict including ``auto_pause_triggered``
            and (when relevant) ``auto_pause_skipped`` /
            ``auto_pause_error``.
        :raises KeyError: If *session_id* is not found.
        :raises ValueError: If the session is not actively monitoring,
            or if confidence is outside 0.0--1.0.
        """
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
        """Return ``True`` iff the operator has flipped the kill-switch.

        Honors ``KILN_MONITOR_PAUSE_DISABLED`` set to any of
        ``"true"``, ``"1"``, ``"yes"`` (case-insensitive).  Anything
        else (including unset) means the auto-pause path is live.
        """
        raw = os.environ.get("KILN_MONITOR_PAUSE_DISABLED", "")
        return raw.strip().lower() in {"true", "1", "yes"}

    def _honor_auto_pause(
        self,
        session: MonitorSession,
        issue: dict[str, Any],
        issue_type: str,
    ) -> None:
        """Best-effort, idempotent printer pause when auto_pause fires.

        Mutates the *issue* dict to surface the outcome:

        - ``auto_pause_skipped="kill_switch"`` when the env var is set
        - ``auto_pause_skipped="already_paused"`` when state reports paused
        - ``auto_pause_error=<repr>`` when the adapter or pause call raises

        Never re-raises; the monitor loop must keep running even when
        the pause attempt fails.
        """
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

        # Idempotency check: if the adapter already reports paused,
        # don't issue another pause.  Adapter shapes vary, so be
        # defensive — any failure during the state probe falls back
        # to "unknown, attempt pause."
        try:
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
            adapter.pause_print()
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
        """Background thread loop that periodically checks printer health.

        Runs until the stop event is set or an unrecoverable error occurs.
        """
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
            # Wall-clock cap (separate from check_count); fires even when
            # check_count would otherwise carry the loop further.
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

                # Push the freshly-built report onto the live consumer
                # surfaces (in-memory queue + JSON-Lines stream) before
                # any of the auto-pause / auto-cancel branches mutate
                # session state.  Best-effort: failures here are
                # debug-logged so they never break the loop.
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

                # Auto-pause on critical health.  Connection-only
                # criticals downgrade to confidence 0.5 so they stay
                # visible in the issue stream but don't trip the pause
                # threshold — a brief disconnect is the reconnect
                # machinery's job, not the pause path's.
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

                # Pro-tier predictive risk — score the recent health
                # reports against KILN-003 thermal/flow/layer-time
                # heuristics.  Red signals get surfaced into the issue
                # stream so they sit alongside vision-based and
                # health-based detections.  Best-effort and signal-only:
                # missing kiln-pro is a clean no-op.  Insufficient
                # telemetry history (< 6 snapshots) returns
                # ``severity=clear`` so this is also a no-op until
                # enough data has accumulated.
                self._maybe_record_predictive_signals(session)

                # Reactive (detective) failure detection — runs the
                # threshold-based detectors against the current health
                # report so conditions that have ALREADY crossed a
                # critical line (thermal runaway, communication loss,
                # filament runout, etc.) produce a FailureReport the
                # recovery pipeline can act on.  See
                # :meth:`_maybe_detect_failure` for the rationale.
                self._maybe_detect_failure(session, report)

                # Auto-cancel — fire-class emergency only.  Tracks
                # consecutive thermal-critical ticks and only cancels
                # AFTER at least one pause has been attempted on this
                # session.  This keeps cancel as a true last-resort
                # action; pause is the first response.
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

        # Session completed naturally if it wasn't stopped or failed
        with self._lock:
            if session.status == MonitorStatus.MONITORING:
                session.status = MonitorStatus.COMPLETED
                session.ended_at = time.time()
            bg = self._background_monitors.pop(printer_name, None)
            self._stall_state.pop(session_id, None)
            self._emergency_state.pop(session_id, None)

        # Sentinel to release any iter_reports() consumer that's still
        # blocked.  Done outside the lock to avoid holding it during
        # any consumer wakeup work.
        if bg is not None and bg.report_queue is not None:
            bg.report_queue.put(None)

    # -- live report fanout -------------------------------------------------

    def _fanout_report(
        self,
        session_id: str,
        report: PrinterHealthReport,
    ) -> None:
        """Publish *report* to the session's queue and JSON-Lines stream.

        Both surfaces are best-effort — exceptions writing to the
        stream or pushing to the queue are debug-logged and never
        propagate.  The MCP / agent paths don't enable the queue, so
        this is a no-op for them.
        """
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
        """Compose the per-tick JSON-Lines envelope written to ``output_stream``.

        Schema mirrors what the MCP ``monitor_print`` one-shot surfaces in
        its 5 Tier-1 lines, plus the kiln-pro-side auto-recover / reroute
        hints when the engine is installed.  Stable shape: every key is
        present even when its underlying state is missing (None) so
        consumers can rely on the contract.
        """
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

        # kiln-pro side — best-effort.  ImportError = clean skip on free
        # tier; any other exception debug-logs and leaves the fields null.
        try:
            from kiln_pro.recovery.auto_recover_engine import (
                AutoRecoverStatus as _AR_Status,
            )
            from kiln_pro.recovery.auto_recover_engine import (
                list_sessions as _ar_list_sessions,
            )
        except ImportError:
            return envelope
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "auto_recover_engine import failed for envelope: %s", exc,
            )
            return envelope

        try:
            ar_sessions = _ar_list_sessions(printer_name=printer_name)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "auto_recover list_sessions failed for envelope: %s", exc,
            )
            return envelope

        if not ar_sessions:
            return envelope

        terminal_states = {
            _AR_Status.DONE_SUCCESS,
            _AR_Status.DONE_FAILURE,
            _AR_Status.NO_FAILURE,
            _AR_Status.CANCELLED,
            _AR_Status.ERRORED,
        }
        active = [s for s in ar_sessions if s.status not in terminal_states]
        if active:
            latest_active = max(active, key=lambda s: s.started_at)
            envelope["auto_recover"] = {
                "stage": latest_active.status.value,
                "auto_recover_id": latest_active.auto_recover_id,
            }

        with_reroute = [s for s in ar_sessions if s.reroute_recommendation]
        if with_reroute:
            latest_rr = max(with_reroute, key=lambda s: s.started_at)
            r = latest_rr.reroute_recommendation or {}
            envelope["reroute"] = {
                "target_printer_id": r.get("target_printer_id"),
                "should_reroute": bool(r.get("should_reroute")),
                "reason": r.get("reason"),
                "blocked_by_rule": r.get("blocked_by_rule"),
            }

        return envelope

    # -- auto-cancel honoring ----------------------------------------------

    # Metric names treated as "fire-class": sustained criticality on
    # one of these warrants an auto-cancel after pause didn't help.
    # Connection / filament / power / webcam don't qualify — those are
    # recoverable through the existing pause/reconnect path.
    _FIRE_CLASS_METRICS: frozenset[str] = frozenset(
        {"hotend_temperature", "bed_temperature"}
    )

    # Minimum number of consecutive thermal-critical ticks before
    # auto-cancel can fire.  Two means: one tick triggers a pause,
    # the next tick (still critical) becomes the cancel trigger.
    _AUTO_CANCEL_PERSISTENCE_TICKS: int = 2

    def _maybe_auto_cancel(
        self,
        session: MonitorSession,
        report: PrinterHealthReport,
        pause_fired_this_tick: bool,
    ) -> None:
        """Decide whether the session has earned an auto-cancel.

        Persistence rules:

        * Auto-cancel is gated on ``policy.auto_cancel_on_emergency``.
        * Only fire-class metrics (hotend / bed temperature CRITICAL)
          count toward the ticker; connection blips never escalate.
        * The ticker must have crossed
          :attr:`_AUTO_CANCEL_PERSISTENCE_TICKS` AND at least one
          pause must have been attempted on this session — pause is
          the first line of defense, cancel is the second.
        * Each session is cancelled at most once
          (``cancel_attempted=True`` after a successful invocation).

        Best-effort and idempotent — never raises.
        """
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

        # Record as an issue so the iter_reports / event consumer sees
        # the cancellation in the same stream as pause issues.
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

    def _check_power_consumption(self, printer_name: str, timestamp: float) -> HealthMetric | None:
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
    ) -> dict[str, Any] | None:
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
        if tracker.last_progress is None or abs(completion_pct - tracker.last_progress) > 0.1:
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
        """Retrieve a session and verify it is actively monitoring.

        :raises KeyError: If not found.
        :raises ValueError: If not in ``monitoring`` status.
        """
        session = self.get_session(session_id)
        if session.status != MonitorStatus.MONITORING:
            raise ValueError(f"Session {session_id!r} is not actively monitoring (status={session.status.value})")
        return session

    @staticmethod
    def _telemetry_from_health_report(
        report: PrinterHealthReport,
    ) -> dict[str, Any]:
        """Translate a health report into a telemetry dict.

        Used by both the predictive (predict_risk) and detective
        (PrintRecovery.detect_failure) pipelines.  Both consumers read
        the same key shape — ``hotend_temp`` / ``hotend_target`` /
        ``bed_temp`` / ``bed_target`` for thermal logic, ``connected``
        for comms loss, ``filament_detected`` for runout, and
        ``timestamp`` for slope calculations.

        Health reports carry the same data under :class:`HealthMetric`
        objects keyed by ``metric_name``; this helper flattens the
        relevant ones into the consumers' expected shape.  Note: temp
        metrics surface ``current_value`` (the raw observed value) —
        detect_failure compares against ``hotend_target`` to decide if
        the delta crosses runaway thresholds, not against the deviation
        itself.

        Missing metrics are silently skipped — both consumers already
        handle None / missing values.
        """
        telemetry: dict[str, Any] = {"timestamp": report.checked_at}
        for m in report.metrics:
            if m.metric_name == "hotend_temperature":
                telemetry["hotend_temp"] = m.current_value
                telemetry["hotend_target"] = m.expected_value
            elif m.metric_name == "bed_temperature":
                telemetry["bed_temp"] = m.current_value
                telemetry["bed_target"] = m.expected_value
            elif m.metric_name == "connection_status":
                # current_value is 1.0 if connected else 0.0; the
                # detector reads ``connected is False`` so map to bool.
                telemetry["connected"] = m.current_value >= 0.5
            elif m.metric_name == "filament_sensor":
                # Same shape: 1.0 means filament detected.
                telemetry["filament_detected"] = m.current_value >= 0.5
        return telemetry

    def _maybe_record_predictive_signals(
        self,
        session: MonitorSession,
    ) -> None:
        """Run the kiln-pro predictive risk heuristics against the
        accumulated health-report history; record red signals as issues.

        Best-effort and signal-only:

        * ``ImportError`` (kiln-pro not installed) → silent no-op.
        * Insufficient history (< 6 snapshots) → predictor returns
          ``severity=clear`` and we record nothing.
        * Amber signals → noted in session metadata but NOT recorded as
          issues (would inflate the issue stream with watch-this signals
          that haven't yet asked the operator to act).
        * Red signals → recorded as issues via
          :meth:`report_issue` with confidence 1.0 so they sit alongside
          health-critical detections in the agent's view.

        Any exception inside the predictor is caught and debug-logged —
        a busted heuristic must not break the monitor loop.
        """
        try:
            from kiln_pro.recovery.predictive import predict_risk
        except ImportError:
            return  # kiln-pro not installed — free tier skip

        try:
            history = [
                self._telemetry_from_health_report(r)
                for r in session.health_reports
            ]
            if not history:
                return
            current = history[-1]
            # ``session.printer_name`` is the stable registry key
            # matching ``NozzleState.printer_id``; passing it lets the
            # Pro+ predictor consult lifetime nozzle wear and add a
            # bounded ``nozzle_wear`` signal.  Free-tier callers (no
            # nozzle store reachable) see no behavioural change — the
            # predictor falls through silently when consultation fails.
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

        # Cache the assessment so monitor_print one-shot can surface
        # the headline risk_score + severity without re-running the
        # predictor against the same history.
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
                # Session may have transitioned out of MONITORING
                # between the predictor call and the report — that's
                # fine, log and move on.
                logger.debug(
                    "Could not record predictive issue for session %s: %s",
                    session.session_id,
                    exc,
                )

    # Maps PrintRecovery's severity scale (critical/high/medium/low) onto
    # the report_issue confidence scale.  ``critical`` and ``high`` sit
    # above the default 0.8 auto-pause threshold so a real-time runaway
    # or comms loss drives a pause; ``medium`` and ``low`` stay visible
    # in the issue stream but don't trip pause.
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
        """Run the reactive failure detector on the latest health report.

        Detective alongside predictive — every monitor tick runs both
        engines so we catch (a) trends approaching failure (predictive)
        and (b) conditions that have already crossed thresholds
        (detective).  Both surface as issues; both feed the same
        auto-pause path.

        The recovery engine returns a :class:`FailureReport` when a
        threshold is crossed.  This helper:

        * Translates the latest health metrics into a telemetry dict
          shape ``PrintRecovery.detect_failure`` accepts (raw current
          temps, connected/filament booleans).
        * Calls the singleton recovery engine with a job_info dict
          synthesised from the session.
        * Maps ``failure.severity`` → ``report_issue`` confidence via
          :attr:`_DETECTIVE_SEVERITY_TO_CONFIDENCE` so critical/high
          findings clear the auto-pause threshold while medium/low
          stay visible but non-pausing.
        * Annotates the recorded issue with ``failure_id`` so
          downstream consumers can correlate the issue back to the
          ``FailureReport`` record in the recovery engine.

        Best-effort and signal-only:

        * ``ImportError`` (recovery engine unavailable) → silent no-op.
        * Detector returns ``None`` (no failure crossed) → no issue.
        * Any exception inside the detector is caught and debug-logged
          — a busted heuristic must not break the monitor loop.
        """
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
            # Best-effort material fill-in — health reports don't carry
            # material directly; leave it absent rather than guessing.
            engine = get_recovery_engine()
            failure = engine.detect_failure(
                printer_name=session.printer_name,
                telemetry=telemetry,
                job_info=job_info,
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
            # Session may have transitioned out of MONITORING between
            # the detector call and the report — log and move on.
            logger.debug(
                "Could not record detective issue for session %s: %s",
                session.session_id,
                exc,
            )
            return

        # Patch the just-recorded issue with the failure_id so callers
        # can correlate the issue back to the FailureReport.  Guard
        # against an empty issue list in case report_issue changed
        # ordering semantics under us.
        if session.issues:
            session.issues[-1]["failure_id"] = failure.failure_id

    def get_latest_signals(self, printer_name: str) -> dict[str, Any]:
        """Return the most recent monitoring summary for a printer.

        Returns a flat dict that the MCP ``monitor_print`` one-shot
        consumes for status rendering.  Single source of truth for
        the user-facing "what has the smart monitoring caught?"
        question::

            {
              "monitoring_active": bool,
              "session_id": str | None,
              "session_started_at": float | None,
              "issue_count": int,                 # total issues so far
              "report_count": int,                # health reports captured
              "risk": {                           # most recent predictive
                "score": 0.55,                    # assessment from predict_risk
                "severity": "amber",              # (None when monitoring
                "kinds": ["thermal_drift"],       # has not yet scored)
              } | None,
              "predictive": {                     # most recent RED predictive
                "severity": "red",                # issue (raised to issue
                "kind": "thermal_drift",          # stream — actionable level)
                "detail": "...",
                "reported_at": float,
              } | None,
              "detective": {                      # most recent detect_failure
                "failure_id": "...",              # match
                "failure_type": "...",
                "severity": "...",
                "reported_at": float,
              } | None,
              "auto_pause": {                     # most recent issue that
                "issue_type": "...",              # tripped the auto-pause
                "triggered_at": float,            # threshold (paused IF
                "age_seconds": float,             # the pause helper succeeded)
                "skipped": str | None,            # "kill_switch"
                                                  # "already_paused" | None
                "error": str | None,
              } | None,
              "as_of": float,
            }

        ``risk`` reflects the LATEST predict_risk assessment, not just
        red issues — so a session at amber-severity (which doesn't fire
        an issue) still shows up here for the headline score.
        """
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

        # Pick the session with the most recent start time when
        # multiple are active (defensive — should be one in practice).
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
                "score": assessment.get("risk_score", 0.0),
                "severity": assessment.get("severity", "clear"),
                "kinds": kinds,
            }

        # ---- Predictive RED issue (most recent) ----
        # ---- Detective failure issue (most recent) ----
        # ---- Auto-pause issue (most recent that flagged the threshold) ----
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
    """Inverse of ``_DETECTIVE_SEVERITY_TO_CONFIDENCE`` for surfacing.

    Returns the severity bucket whose mapped confidence equals the
    recorded value (within a small tolerance).  Returns ``None`` when
    the confidence doesn't match any known bucket.
    """
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
    """Return the lazily-initialised global :class:`PrintHealthMonitor` instance.

    Thread-safe via double-checked locking.
    """
    global _print_health_monitor
    if _print_health_monitor is None:
        with _singleton_lock:
            if _print_health_monitor is None:
                _print_health_monitor = PrintHealthMonitor()
    return _print_health_monitor
