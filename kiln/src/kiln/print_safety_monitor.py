"""Persistent print safety monitor — rule-based anomaly detection for full print duration.

Runs as a blocking process for the lifetime of a print job, polling printer
state at a configurable interval.  Detects temperature drift, stalls, error
states, connection loss, and thermal runaway using tiered severity rules.
Optionally captures periodic webcam snapshots and can auto-pause or
auto-cancel based on alert severity.

Designed to run independently of any AI agent session — if the agent dies,
the monitor keeps protecting the print.  Agents can consume the JSON Lines
output for smarter decisions on top of the rule-based safety layer.

Usage (CLI)::

    kiln monitor --auto-pause --json

Usage (programmatic)::

    monitor = PrintSafetyMonitor(adapter, "my-printer", MonitorConfig())
    exit_code = monitor.run()
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from kiln.events import EventType
from kiln.printers.base import JobProgress, PrinterError, PrinterStatus

if TYPE_CHECKING:
    from kiln.events import EventBus
    from kiln.printers.base import PrinterAdapter

logger = logging.getLogger(__name__)

# Terminal states — print is done (successfully or not).
_TERMINAL_STATES = frozenset({
    PrinterStatus.IDLE,
    PrinterStatus.ERROR,
    PrinterStatus.OFFLINE,
    PrinterStatus.CANCELLING,
})

# Active states — print is in progress.
_ACTIVE_STATES = frozenset({
    PrinterStatus.PRINTING,
    PrinterStatus.PAUSED,
    PrinterStatus.BUSY,
})

# Minimum image size (bytes) to consider a snapshot valid.
_MIN_SNAPSHOT_BYTES = 100

# How long to wait for a print to start when printer is idle at launch.
_WAIT_FOR_PRINT_TIMEOUT = 300  # 5 minutes

# Debounce interval — same rule won't re-alert within this window.
_ALERT_DEBOUNCE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class MonitorConfig:
    """Configuration for a print safety monitoring session."""

    poll_interval: float = 10.0
    snapshot_interval: int = 300  # 0 to disable snapshots
    snapshot_dir: Path = field(default_factory=lambda: Path.home() / ".kiln" / "snapshots")
    auto_pause: bool = True
    auto_cancel: bool = False
    timeout: float = 0.0  # 0 = unlimited

    def to_dict(self) -> dict[str, Any]:
        return {
            "poll_interval": self.poll_interval,
            "snapshot_interval": self.snapshot_interval,
            "snapshot_dir": str(self.snapshot_dir),
            "auto_pause": self.auto_pause,
            "auto_cancel": self.auto_cancel,
            "timeout": self.timeout,
        }


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """A single detected anomaly."""

    timestamp: float
    severity: str  # "emergency", "critical", "warning"
    rule: str  # e.g. "error_state", "temp_drift", "stall"
    detail: str
    action: str  # "emergency_stop", "cancel", "pause", "log"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "detail": self.detail,
            "action": self.action,
        }


# ---------------------------------------------------------------------------
# Temperature sample for trend analysis
# ---------------------------------------------------------------------------


@dataclass
class _TempSample:
    """A single temperature reading."""

    timestamp: float
    hotend_actual: float | None
    hotend_target: float | None
    bed_actual: float | None
    bed_target: float | None


# ---------------------------------------------------------------------------
# PrintSafetyMonitor
# ---------------------------------------------------------------------------


class PrintSafetyMonitor:
    """Persistent print safety monitor with tiered anomaly detection.

    :param adapter: The printer adapter to poll.
    :param printer_name: Human-readable printer name for output.
    :param config: Monitoring configuration.
    :param json_mode: If True, emit JSON Lines to stdout.  Otherwise, emit
        human-readable Rich terminal output.
    :param event_bus: Optional event bus for publishing alerts.
    """

    def __init__(
        self,
        adapter: PrinterAdapter,
        printer_name: str,
        config: MonitorConfig,
        *,
        json_mode: bool = False,
        event_bus: EventBus | None = None,
    ) -> None:
        self._adapter = adapter
        self._printer_name = printer_name
        self._config = config
        self._json_mode = json_mode
        self._event_bus = event_bus

        # Camera capability (checked at run time).
        self._can_snapshot: bool = False

        # State tracking.
        self._temp_history: deque[_TempSample] = deque(maxlen=60)
        self._last_completion: float | None = None
        self._last_completion_time: float = 0.0
        self._connection_lost_since: float | None = None
        self._print_was_active: bool = False
        self._active_state_start: float | None = None  # When print became active

        # Temperature drift trackers (when drift first exceeded threshold).
        self._hotend_drift_since: float | None = None
        self._bed_drift_since: float | None = None

        # Alerts.
        self._alerts: list[Alert] = []
        self._last_alert_by_rule: dict[str, float] = {}

        # Snapshots.
        self._snapshot_count: int = 0
        self._last_snapshot_time: float = 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Blocking main loop.  Returns 0 on successful print completion, 1 on failure."""
        start_time = time.time()
        self._can_snapshot = getattr(
            self._adapter, "capabilities", None
        ) is not None and getattr(self._adapter.capabilities, "can_snapshot", False)

        if not self._can_snapshot and self._config.snapshot_interval > 0:
            self._emit_line(
                "info",
                detail="Camera not available; snapshots disabled (telemetry-only monitoring).",
            )

        # --- Initial state check ---
        try:
            state = self._adapter.get_state()
        except PrinterError as exc:
            self._emit_line(
                "alert",
                severity="emergency",
                rule="connection_failed",
                detail=f"Cannot connect to printer: {exc}",
                action="abort",
            )
            return 1

        # If not printing, wait for a print to start.
        if state.state not in _ACTIVE_STATES:
            self._emit_line("info", detail=f"Printer is {state.state.value}. Waiting for print to start...")
            if not self._wait_for_print_start():
                self._emit_complete("no_print", start_time)
                return 1

        self._print_was_active = True
        self._active_state_start = time.time()
        self._last_completion_time = time.time()

        # --- Main monitoring loop ---
        while True:
            cycle_start = time.time()
            connected = True

            # Poll state.
            try:
                state = self._adapter.get_state()
                connected = state.connected
            except PrinterError:
                state = None
                connected = False

            # Poll job.
            try:
                job = self._adapter.get_job() if connected else JobProgress()
            except PrinterError:
                job = JobProgress()

            # Check for successful completion.
            if state and state.state == PrinterStatus.IDLE and self._print_was_active:
                completion = job.completion if job else None
                if completion is not None and completion >= 99.0:
                    self._emit_complete("success", start_time, job)
                    return 0
                # Idle but low completion — might have been cancelled externally.
                self._emit_complete("ended", start_time, job)
                return 0

            # Check for cancellation.
            if state and state.state == PrinterStatus.CANCELLING:
                self._emit_complete("cancelled", start_time, job)
                return 1

            # Record temperature history.
            if state:
                self._record_temps(state)

            # Reset stall timer when print is paused (don't count paused time).
            if state and state.state == PrinterStatus.PAUSED:
                self._last_completion_time = time.time()

            # --- Run detection rules (priority order) ---
            cycle_alerts: list[Alert] = []

            alert = self._check_tier1_emergency(state, connected)
            if alert:
                cycle_alerts.append(alert)

            if state:
                alert = self._check_tier2_critical(state, job)
                if alert:
                    cycle_alerts.append(alert)

                alert = self._check_tier3_warning(state)
                if alert:
                    cycle_alerts.append(alert)

            # Process alerts (highest severity first).
            acted = False
            for a in cycle_alerts:
                if self._should_debounce(a):
                    continue
                self._alerts.append(a)
                self._last_alert_by_rule[a.rule] = a.timestamp
                self._emit_alert(a)
                if not acted:
                    self._execute_action(a)
                    acted = True

                    if a.action == "emergency_stop" and self._config.auto_cancel:
                        self._emit_complete("emergency_stopped", start_time, job)
                        return 1
                    if a.action == "cancel":
                        self._emit_complete("cancelled_by_monitor", start_time, job)
                        return 1

            # Emit status line.
            if state:
                self._emit_status(state, job)

            # Maybe capture snapshot.
            snap_meta = self._maybe_capture_snapshot(job.completion if job else None)
            if snap_meta:
                self._emit_snapshot(snap_meta, job.completion if job else None)

            # Timeout check.
            if self._config.timeout > 0 and (time.time() - start_time) >= self._config.timeout:
                self._emit_complete("timeout", start_time, job)
                return 1

            # Sleep until next cycle.
            elapsed = time.time() - cycle_start
            sleep_time = max(0, self._config.poll_interval - elapsed)
            time.sleep(sleep_time)

    # ------------------------------------------------------------------
    # Wait for print to start
    # ------------------------------------------------------------------

    def _wait_for_print_start(self) -> bool:
        """Wait up to 5 minutes for printer to enter an active state."""
        deadline = time.time() + _WAIT_FOR_PRINT_TIMEOUT
        while time.time() < deadline:
            time.sleep(5.0)
            try:
                state = self._adapter.get_state()
                if state.state in _ACTIVE_STATES:
                    return True
            except PrinterError:
                continue
        return False

    # ------------------------------------------------------------------
    # Temperature tracking
    # ------------------------------------------------------------------

    def _record_temps(self, state: Any) -> None:
        """Append a temperature sample to history."""
        self._temp_history.append(
            _TempSample(
                timestamp=time.time(),
                hotend_actual=state.tool_temp_actual,
                hotend_target=state.tool_temp_target,
                bed_actual=state.bed_temp_actual,
                bed_target=state.bed_temp_target,
            )
        )

    def _is_temp_rising(self, heater: str) -> bool:
        """Check if a heater's actual temp shows a rising trend over recent samples.

        Requires at least 3 samples with each successive one higher than the
        previous to confirm a rising trend.
        """
        if len(self._temp_history) < 3:
            return False

        recent = list(self._temp_history)[-3:]
        values: list[float] = []
        for sample in recent:
            val = sample.hotend_actual if heater == "hotend" else sample.bed_actual
            if val is None:
                return False
            values.append(val)

        return values[0] < values[1] < values[2]

    def _is_in_preheat_phase(self, state: Any) -> bool:
        """Return True if the printer is still heating up to target temperature.

        Suppresses nuisance tier-3 warnings during the normal preheat ramp.
        A printer is "preheating" when either heater's actual temp is more
        than 10°C below its target AND the active state started recently
        (within 5 minutes).
        """
        if self._active_state_start is None:
            return False
        # Only suppress during the first 5 minutes of an active print.
        if (time.time() - self._active_state_start) > 300.0:
            return False
        # Check if hotend is still ramping.
        if (
            state.tool_temp_actual is not None
            and state.tool_temp_target is not None
            and state.tool_temp_target > 0
            and state.tool_temp_actual < (state.tool_temp_target - 10)
        ):
            return True
        # Check if bed is still ramping.
        return (
            state.bed_temp_actual is not None
            and state.bed_temp_target is not None
            and state.bed_temp_target > 0
            and state.bed_temp_actual < (state.bed_temp_target - 10)
        )

    # ------------------------------------------------------------------
    # Tier 1 — Emergency
    # ------------------------------------------------------------------

    def _check_tier1_emergency(
        self, state: Any | None, connected: bool
    ) -> Alert | None:
        now = time.time()

        # Rule: error_state
        if state and state.state == PrinterStatus.ERROR:
            detail = "Printer entered ERROR state"
            if state.print_error:
                detail += f" (code: {state.print_error}, hex: {state.print_error:08X})"
            return Alert(now, "emergency", "error_state", detail, "emergency_stop")

        # Rule: connection_lost
        if not connected and self._print_was_active:
            if self._connection_lost_since is None:
                self._connection_lost_since = now
            elif (now - self._connection_lost_since) > 60.0:
                duration = now - self._connection_lost_since
                return Alert(
                    now, "emergency", "connection_lost",
                    f"Connection lost for {duration:.0f}s while print was active",
                    "emergency_stop",
                )
        else:
            self._connection_lost_since = None

        # Rule: thermal_runaway — hotend
        if state and state.tool_temp_actual is not None and state.tool_temp_target is not None and state.tool_temp_target > 0:
                overshoot = state.tool_temp_actual - state.tool_temp_target
                if overshoot > 20.0 and self._is_temp_rising("hotend"):
                    return Alert(
                        now, "emergency", "thermal_runaway",
                        f"Hotend {state.tool_temp_actual:.1f}°C is {overshoot:.1f}°C above "
                        f"target {state.tool_temp_target:.0f}°C and still rising",
                        "emergency_stop",
                    )

        # Rule: thermal_runaway — bed
        if state and state.bed_temp_actual is not None and state.bed_temp_target is not None and state.bed_temp_target > 0:
                overshoot = state.bed_temp_actual - state.bed_temp_target
                if overshoot > 20.0 and self._is_temp_rising("bed"):
                    return Alert(
                        now, "emergency", "thermal_runaway_bed",
                        f"Bed {state.bed_temp_actual:.1f}°C is {overshoot:.1f}°C above "
                        f"target {state.bed_temp_target:.0f}°C and still rising",
                        "emergency_stop",
                    )

        return None

    # ------------------------------------------------------------------
    # Tier 2 — Critical
    # ------------------------------------------------------------------

    def _check_tier2_critical(self, state: Any, job: Any) -> Alert | None:
        now = time.time()

        # Rule: temp_drift — hotend sustained ±15°C for >60s
        if state.tool_temp_actual is not None and state.tool_temp_target is not None:
            if state.tool_temp_target > 0:
                drift = abs(state.tool_temp_actual - state.tool_temp_target)
                if drift > 15.0:
                    if self._hotend_drift_since is None:
                        self._hotend_drift_since = now
                    elif (now - self._hotend_drift_since) > 60.0:
                        return Alert(
                            now, "critical", "temp_drift",
                            f"Hotend {state.tool_temp_actual:.1f}°C vs target "
                            f"{state.tool_temp_target:.0f}°C for "
                            f"{now - self._hotend_drift_since:.0f}s",
                            "pause",
                        )
                else:
                    self._hotend_drift_since = None
            else:
                self._hotend_drift_since = None
        else:
            self._hotend_drift_since = None

        # Rule: temp_drift_bed — bed sustained ±10°C for >60s
        if state.bed_temp_actual is not None and state.bed_temp_target is not None:
            if state.bed_temp_target > 0:
                drift = abs(state.bed_temp_actual - state.bed_temp_target)
                if drift > 10.0:
                    if self._bed_drift_since is None:
                        self._bed_drift_since = now
                    elif (now - self._bed_drift_since) > 60.0:
                        return Alert(
                            now, "critical", "temp_drift_bed",
                            f"Bed {state.bed_temp_actual:.1f}°C vs target "
                            f"{state.bed_temp_target:.0f}°C for "
                            f"{now - self._bed_drift_since:.0f}s",
                            "pause",
                        )
                else:
                    self._bed_drift_since = None
            else:
                self._bed_drift_since = None
        else:
            self._bed_drift_since = None

        # Rule: stall — no progress change for >10min (skip when paused)
        if state.state == PrinterStatus.PRINTING:
            completion = job.completion if job else None
            if completion is not None:
                if self._last_completion is not None and completion == self._last_completion:
                    stall_duration = now - self._last_completion_time
                    if stall_duration > 600.0:
                        return Alert(
                            now, "critical", "stall",
                            f"Progress stalled at {completion:.1f}% for "
                            f"{stall_duration:.0f}s",
                            "pause",
                        )
                else:
                    self._last_completion = completion
                    self._last_completion_time = now

        # Rule: print_error — Bambu HMS codes (nonzero)
        if state.print_error is not None and state.print_error != 0:
            return Alert(
                now, "critical", "print_error",
                f"Printer error code: {state.print_error} "
                f"(hex: {state.print_error:08X})",
                "pause",
            )

        return None

    # ------------------------------------------------------------------
    # Tier 3 — Warning
    # ------------------------------------------------------------------

    def _check_tier3_warning(self, state: Any) -> Alert | None:
        now = time.time()

        # Suppress nuisance temp warnings during normal preheat ramp.
        preheat = self._is_in_preheat_phase(state)

        # Rule: temp_fluctuation — transient >5°C deviation
        if not preheat and state.tool_temp_actual is not None and state.tool_temp_target is not None and state.tool_temp_target > 0:
                drift = abs(state.tool_temp_actual - state.tool_temp_target)
                if drift > 5.0:
                    return Alert(
                        now, "warning", "temp_fluctuation",
                        f"Hotend drifted {drift:.1f}°C from target "
                        f"({state.tool_temp_actual:.1f}°C vs {state.tool_temp_target:.0f}°C)",
                        "log",
                    )

        # Rule: wifi_degraded
        if state.wifi_signal is not None:
            try:
                dbm = int(str(state.wifi_signal).replace("dBm", "").strip())
                if dbm < -80:
                    return Alert(
                        now, "warning", "wifi_degraded",
                        f"WiFi signal weak: {state.wifi_signal}",
                        "log",
                    )
            except (ValueError, AttributeError):
                pass

        return None

    # ------------------------------------------------------------------
    # Alert debouncing
    # ------------------------------------------------------------------

    def _should_debounce(self, alert: Alert) -> bool:
        """Return True if this alert should be suppressed (same rule fired recently)."""
        last = self._last_alert_by_rule.get(alert.rule)
        return last is not None and (alert.timestamp - last) < _ALERT_DEBOUNCE_SECONDS

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _execute_action(self, alert: Alert) -> None:
        """Execute the action associated with an alert."""
        if alert.action == "emergency_stop" and self._config.auto_cancel:
            try:
                self._adapter.emergency_stop()
            except PrinterError as exc:
                logger.warning("Emergency stop failed: %s", exc)

        elif alert.action == "pause" and self._config.auto_pause:
            try:
                self._adapter.pause_print()
            except PrinterError as exc:
                logger.warning("Auto-pause failed: %s", exc)

        # "log" action — no printer interaction needed.

        # Publish to event bus if available.
        if self._event_bus:
            with contextlib.suppress(Exception):
                self._event_bus.publish(
                    EventType.MONITOR_ALERT,
                    {
                        "printer_name": self._printer_name,
                        "severity": alert.severity,
                        "rule": alert.rule,
                        "detail": alert.detail,
                        "action": alert.action,
                    },
                    source="print_safety_monitor",
                )

    # ------------------------------------------------------------------
    # Snapshot capture
    # ------------------------------------------------------------------

    def _maybe_capture_snapshot(self, completion: float | None) -> dict[str, Any] | None:
        """Capture and save a snapshot if interval has elapsed."""
        if not self._can_snapshot or self._config.snapshot_interval <= 0:
            return None

        now = time.time()
        if (now - self._last_snapshot_time) < self._config.snapshot_interval:
            return None

        try:
            image_data = self._adapter.get_snapshot()
        except PrinterError:
            return None

        if image_data is None or len(image_data) < _MIN_SNAPSHOT_BYTES:
            return None

        # Validate using existing heuristic checks.
        try:
            from kiln.print_monitor import analyze_snapshot_basic
            analysis = analyze_snapshot_basic(image_data)
            if not analysis.get("valid", False):
                return None
        except ImportError:
            pass  # If import fails, save anyway.

        # Save to disk.
        self._config.snapshot_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self._printer_name}_{ts_str}_{self._snapshot_count:04d}.jpg"
        filepath = self._config.snapshot_dir / filename
        filepath.write_bytes(image_data)

        self._snapshot_count += 1
        self._last_snapshot_time = now

        # Publish event.
        if self._event_bus:
            with contextlib.suppress(Exception):
                self._event_bus.publish(
                    EventType.MONITOR_SNAPSHOT,
                    {"printer_name": self._printer_name, "path": str(filepath), "completion": completion},
                    source="print_safety_monitor",
                )

        return {
            "path": str(filepath),
            "completion": completion,
            "size_bytes": len(image_data),
        }

    # ------------------------------------------------------------------
    # Output emission
    # ------------------------------------------------------------------

    def _emit_line(self, event_type: str, **kwargs: Any) -> None:
        """Emit a single line to stdout (JSON or human-readable)."""
        ts = datetime.now(timezone.utc).isoformat()
        line = {"ts": ts, "type": event_type, **kwargs}

        if self._json_mode:
            click.echo(json.dumps(line, default=str))
        else:
            self._emit_rich_line(line)

    def _emit_status(self, state: Any, job: Any) -> None:
        """Emit a status line."""
        ts = datetime.now(timezone.utc).isoformat()
        line: dict[str, Any] = {
            "ts": ts,
            "type": "status",
            "state": state.state.value if state else "unknown",
            "completion": job.completion if job else None,
        }

        if job and job.current_layer is not None:
            line["layer"] = [job.current_layer, job.total_layers]
        if job and job.print_time_left_seconds is not None:
            line["eta_seconds"] = job.print_time_left_seconds

        line["temps"] = {}
        if state.tool_temp_actual is not None:
            line["temps"]["hotend"] = [
                round(state.tool_temp_actual, 1),
                state.tool_temp_target,
            ]
        if state.bed_temp_actual is not None:
            line["temps"]["bed"] = [
                round(state.bed_temp_actual, 1),
                state.bed_temp_target,
            ]

        line["alerts"] = []
        line["action"] = "continue"

        if self._json_mode:
            click.echo(json.dumps(line, default=str))
        else:
            self._emit_rich_status(state, job)

    def _emit_alert(self, alert: Alert) -> None:
        """Emit an alert line."""
        ts = datetime.now(timezone.utc).isoformat()
        line = {
            "ts": ts,
            "type": "alert",
            "severity": alert.severity,
            "rule": alert.rule,
            "detail": alert.detail,
            "action": alert.action,
        }

        if self._json_mode:
            click.echo(json.dumps(line, default=str))
        else:
            severity_upper = alert.severity.upper()
            action_str = ""
            if alert.action == "pause" and self._config.auto_pause:
                action_str = " → auto-paused"
            elif alert.action == "emergency_stop" and self._config.auto_cancel:
                action_str = " → emergency stopped"
            click.echo(f"  [ALERT] {severity_upper}: {alert.detail}{action_str}")

    def _emit_snapshot(self, meta: dict[str, Any], completion: float | None) -> None:
        """Emit a snapshot line."""
        ts = datetime.now(timezone.utc).isoformat()
        line = {
            "ts": ts,
            "type": "snapshot",
            "path": meta["path"],
            "completion": completion,
            "size_bytes": meta.get("size_bytes"),
        }

        if self._json_mode:
            click.echo(json.dumps(line, default=str))
        else:
            filename = Path(meta["path"]).name
            pct = f" at {completion:.1f}%" if completion is not None else ""
            click.echo(f"  [SNAPSHOT] saved {filename}{pct}")

    def _emit_complete(self, result: str, start_time: float, job: Any = None) -> None:
        """Emit a completion line."""
        ts = datetime.now(timezone.utc).isoformat()
        duration = round(time.time() - start_time, 1)
        line: dict[str, Any] = {
            "ts": ts,
            "type": "complete",
            "result": result,
            "duration_s": duration,
            "alerts_total": len(self._alerts),
            "snapshots_total": self._snapshot_count,
        }
        if job:
            if job.completion is not None:
                line["completion"] = job.completion
            if job.current_layer is not None:
                line["layers"] = [job.current_layer, job.total_layers]

        if self._json_mode:
            click.echo(json.dumps(line, default=str))
        else:
            click.echo(f"\n  Print {result}. Duration: {duration:.0f}s, "
                       f"Alerts: {len(self._alerts)}, Snapshots: {self._snapshot_count}")

        # Publish event.
        if self._event_bus:
            with contextlib.suppress(Exception):
                self._event_bus.publish(
                    EventType.MONITOR_COMPLETE,
                    {"printer_name": self._printer_name, "result": result, "duration_s": duration},
                    source="print_safety_monitor",
                )

    # ------------------------------------------------------------------
    # Rich terminal output helpers
    # ------------------------------------------------------------------

    def _emit_rich_line(self, line: dict[str, Any]) -> None:
        """Emit a generic info/warning line in Rich mode."""
        detail = line.get("detail", "")
        event_type = line.get("type", "info")
        if event_type == "info":
            click.echo(f"  [INFO] {detail}")
        elif event_type == "alert":
            severity = line.get("severity", "").upper()
            click.echo(f"  [ALERT] {severity}: {detail}")
        else:
            click.echo(f"  [{event_type.upper()}] {detail}")

    def _emit_rich_status(self, state: Any, job: Any) -> None:
        """Emit a compact status line for Rich terminal mode."""
        parts: list[str] = []

        # Temperatures.
        if state.tool_temp_actual is not None:
            target = state.tool_temp_target or 0
            parts.append(f"Hotend: {state.tool_temp_actual:.1f}°C → {target:.0f}°C")
        if state.bed_temp_actual is not None:
            target = state.bed_temp_target or 0
            parts.append(f"Bed: {state.bed_temp_actual:.1f}°C → {target:.0f}°C")

        # Progress bar.
        completion = job.completion if job else None
        if completion is not None:
            filled = int(completion / 10)
            bar = "█" * filled + "░" * (10 - filled)
            parts.append(f"[{bar}] {completion:.1f}%")

        # Layer info.
        if job and job.current_layer is not None:
            parts.append(f"Layer {job.current_layer}/{job.total_layers}")

        # ETA.
        if job and job.print_time_left_seconds is not None:
            minutes = job.print_time_left_seconds // 60
            parts.append(f"~{minutes}min")

        # Status indicator.
        parts.append("✓ OK")

        line = "  " + "  ".join(parts)
        click.echo(f"\r{line}", nl=False)
