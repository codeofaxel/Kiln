"""First-layer print monitor — structured snapshot collection for agent inspection."""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from kiln.events import EventType
from kiln.printers.base import PrinterError, PrinterStatus

if TYPE_CHECKING:
    from kiln.events import EventBus
    from kiln.printers.base import PrinterAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_PHASE_THRESHOLDS = {"first_layers": 10.0, "final_layers": 90.0}


def _detect_phase(completion: float | None) -> str:
    """Classify print phase from completion percentage."""
    if completion is None or completion < 0:
        return "unknown"
    if completion < _PHASE_THRESHOLDS["first_layers"]:
        return "first_layers"
    if completion > _PHASE_THRESHOLDS["final_layers"]:
        return "final_layers"
    return "mid_print"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MonitorPolicy:
    """Configurable policy for print monitoring behavior."""

    first_layer_delay_seconds: int = 120
    first_layer_check_count: int = 3
    first_layer_interval_seconds: int = 60
    auto_pause_on_failure: bool = True
    failure_confidence_threshold: float = 0.8
    require_camera: bool = False
    max_snapshot_failures: int = 3
    stall_timeout_seconds: int = 600
    monitoring_mode: str = "auto"  # "vision", "telemetry", or "auto"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorPolicy:
        """Construct a :class:`MonitorPolicy` from a plain dictionary."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class MonitorResult:
    """Outcome of a first-layer monitoring session."""

    success: bool
    outcome: str
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    snapshot_failures: int = 0
    duration_seconds: float = 0.0
    auto_paused: bool = False
    failure_type: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# FirstLayerMonitor
# ---------------------------------------------------------------------------

# Terminal printer states that mean the print is no longer running
_TERMINAL_STATES = frozenset(
    {
        PrinterStatus.IDLE,
        PrinterStatus.ERROR,
        PrinterStatus.OFFLINE,
        PrinterStatus.UNAUTHORIZED,
        PrinterStatus.CONNECTION_LIMIT,
        PrinterStatus.CANCELLING,
    }
)

# Minimum image size in bytes to consider a snapshot valid.
_MIN_SNAPSHOT_BYTES = 100


class FirstLayerMonitor:
    """Orchestrates first-layer snapshot collection for agent inspection."""

    def __init__(
        self,
        adapter: PrinterAdapter,
        printer_name: str,
        *,
        policy: MonitorPolicy | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._adapter = adapter
        self._printer_name = printer_name
        self._policy = policy or MonitorPolicy()
        self._event_bus = event_bus
        # Background session state (see :meth:`start`)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: MonitorResult | None = None

    # -- public API --------------------------------------------------------

    def start(self, *, monitoring_mode: str | None = None) -> None:
        """Run :meth:`monitor` on a daemon thread; read it with :meth:`result`."""
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run_in_background,
                kwargs={"monitoring_mode": monitoring_mode},
                name=f"kiln-first-layer-{self._printer_name}",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._thread = None
            raise

    def result(self) -> MonitorResult | None:
        """The finished session's result, or ``None`` while none has finished."""
        with self._lock:
            return self._result

    @property
    def running(self) -> bool:
        """Whether a background session is still in progress."""
        with self._lock:
            thread = self._thread
            finished = self._result is not None
        return thread is not None and thread.is_alive() and not finished

    def _run_in_background(self, *, monitoring_mode: str | None) -> None:
        """Thread body: run the session, and always leave a result behind."""
        try:
            outcome = self.monitor(monitoring_mode=monitoring_mode)
        except Exception as exc:  # noqa: BLE001 — a crash must end as a result, not as silence
            logger.exception(
                "First-layer monitor on %s stopped with an error", self._printer_name
            )
            outcome = MonitorResult(
                success=False,
                outcome="error",
                message=f"The first-layer monitor stopped with an error: {exc}",
            )
        with self._lock:
            self._result = outcome

    def monitor(self, *, monitoring_mode: str | None = None) -> MonitorResult:
        """Run the first-layer monitoring session (blocking)."""
        start_time = time.time()

        # Resolve effective monitoring mode
        effective_mode = (monitoring_mode or self._policy.monitoring_mode or "auto").lower().strip()
        if effective_mode not in ("vision", "telemetry", "auto"):
            effective_mode = "auto"

        from kiln.printers.base import adapter_has_camera

        can_snap = adapter_has_camera(self._adapter)

        if self._policy.require_camera and not can_snap:
            logger.warning(
                "Printer %s has no camera — aborting monitor (require_camera=True)",
                self._printer_name,
            )
            return MonitorResult(
                success=False,
                outcome="no_camera",
                duration_seconds=0.0,
                message=(
                    f"Printer {self._printer_name} does not support snapshots. "
                    "Vision monitoring requires a camera when require_camera is enabled."
                ),
            )

        if effective_mode == "auto":
            effective_mode = "vision" if can_snap else "telemetry"

        if effective_mode == "vision" and not can_snap:
            logger.info(
                "Printer %s has no camera — degrading from vision to telemetry mode",
                self._printer_name,
            )
            effective_mode = "telemetry"

        # Telemetry-only mode
        if effective_mode == "telemetry":
            return self._monitor_telemetry(start_time)

        # --- Initial delay ------------------------------------------------
        logger.info(
            "Waiting %d s before first-layer check on %s",
            self._policy.first_layer_delay_seconds,
            self._printer_name,
        )
        if not self._wait_printing(self._policy.first_layer_delay_seconds, start_time):
            return self._build_early_exit(start_time)

        # --- Snapshot collection ------------------------------------------
        snapshots: list[dict[str, Any]] = []
        consecutive_failures = 0

        for i in range(self._policy.first_layer_check_count):
            # Wait for interval (skip on first iteration)
            if i > 0 and not self._wait_printing(self._policy.first_layer_interval_seconds, start_time):
                return self._build_early_exit(
                    start_time,
                    snapshots=snapshots,
                    snapshot_failures=consecutive_failures,
                )

            # Verify printer is still printing
            try:
                state = self._adapter.get_state()
            except PrinterError as exc:
                logger.error(
                    "Failed to read printer state on %s: %s",
                    self._printer_name,
                    exc,
                )
                return MonitorResult(
                    success=False,
                    outcome="error",
                    snapshots=snapshots,
                    snapshot_failures=consecutive_failures,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=f"Could not read printer state: {exc}",
                )

            if state.confirmed_state in _TERMINAL_STATES:
                logger.info(
                    "Printer %s entered %s during monitoring — stopping",
                    self._printer_name,
                    state.confirmed_state.value,
                )
                return MonitorResult(
                    success=True,
                    outcome="print_ended",
                    snapshots=snapshots,
                    snapshot_failures=consecutive_failures,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=(
                        f"Print ended (state={state.confirmed_state.value}) during first-layer "
                        "monitoring.  Collected snapshots returned."
                    ),
                )

            # Capture snapshot
            if can_snap:
                snap = self._capture_snapshot(i + 1)
                if snap is not None:
                    snapshots.append(snap)
                    consecutive_failures = 0
                    self._publish_vision_frame_captured(snap)
                else:
                    consecutive_failures += 1
                    logger.warning(
                        "Snapshot %d/%d failed on %s (consecutive: %d)",
                        i + 1,
                        self._policy.first_layer_check_count,
                        self._printer_name,
                        consecutive_failures,
                    )
                    if consecutive_failures >= self._policy.max_snapshot_failures:
                        self._publish_vision_alert(
                            "snapshot_failure",
                            f"{consecutive_failures} consecutive snapshot failures",
                        )
                        return MonitorResult(
                            success=False,
                            outcome="error",
                            snapshots=snapshots,
                            snapshot_failures=consecutive_failures,
                            duration_seconds=round(time.time() - start_time, 1),
                            message=(
                                f"Exceeded max consecutive snapshot failures "
                                f"({self._policy.max_snapshot_failures}).  "
                                "Camera may be offline."
                            ),
                        )
            else:
                # No camera — record a placeholder entry
                try:
                    job = self._adapter.get_job()
                    completion = job.completion
                except PrinterError:
                    completion = None
                snapshots.append(
                    {
                        "captured_at": time.time(),
                        "completion_percent": completion,
                        "print_phase": _detect_phase(completion),
                        "image_base64": None,
                        "note": "no camera available",
                    }
                )

        elapsed = round(time.time() - start_time, 1)
        logger.info(
            "First-layer monitoring complete on %s: %d snapshots in %.1f s",
            self._printer_name,
            len(snapshots),
            elapsed,
        )
        return MonitorResult(
            success=True,
            outcome="passed",
            snapshots=snapshots,
            snapshot_failures=consecutive_failures,
            duration_seconds=elapsed,
            message=(
                f"Captured {len(snapshots)} snapshot(s) during first-layer monitoring.  "
                "Review the images for print quality issues."
            ),
        )

    # -- internal helpers --------------------------------------------------

    def _monitor_telemetry(self, start_time: float) -> MonitorResult:
        """Telemetry-only monitoring: poll printer state and temps."""
        logger.info(
            "Telemetry-only monitoring on %s (delay=%ds, checks=%d, interval=%ds)",
            self._printer_name,
            self._policy.first_layer_delay_seconds,
            self._policy.first_layer_check_count,
            self._policy.first_layer_interval_seconds,
        )

        # --- Initial delay ---
        if not self._wait_printing(self._policy.first_layer_delay_seconds, start_time):
            return self._build_early_exit(start_time)

        telemetry_snapshots: list[dict[str, Any]] = []
        state_changes: list[str] = []
        prev_state: str | None = None

        for i in range(self._policy.first_layer_check_count):
            if i > 0 and not self._wait_printing(self._policy.first_layer_interval_seconds, start_time):
                return self._build_early_exit(
                    start_time,
                    snapshots=telemetry_snapshots,
                )

            # Read printer state
            try:
                state = self._adapter.get_state()
            except PrinterError as exc:
                logger.error(
                    "Failed to read printer state on %s: %s",
                    self._printer_name,
                    exc,
                )
                return MonitorResult(
                    success=False,
                    outcome="error",
                    snapshots=telemetry_snapshots,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=f"Could not read printer state: {exc}",
                )

            if state.confirmed_state in _TERMINAL_STATES:
                logger.info(
                    "Printer %s entered %s during telemetry monitoring — stopping",
                    self._printer_name,
                    state.confirmed_state.value,
                )
                return MonitorResult(
                    success=True,
                    outcome="print_ended",
                    snapshots=telemetry_snapshots,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=(f"Print ended (state={state.confirmed_state.value}) during telemetry monitoring."),
                )

            # Track state transitions
            current_state = state.state.value
            if prev_state is not None and current_state != prev_state:
                state_changes.append(f"{prev_state} -> {current_state}")
            prev_state = current_state

            # Read job progress
            try:
                job = self._adapter.get_job()
                completion = job.completion
            except PrinterError:
                completion = None

            # Collect temperature data
            temps: dict[str, Any] = {}
            if state.tool_temp_actual is not None:
                temps["hotend_actual"] = state.tool_temp_actual
            if state.tool_temp_target is not None:
                temps["hotend_target"] = state.tool_temp_target
            if state.bed_temp_actual is not None:
                temps["bed_actual"] = state.bed_temp_actual
            if state.bed_temp_target is not None:
                temps["bed_target"] = state.bed_temp_target

            telemetry_snapshots.append(
                {
                    "captured_at": time.time(),
                    "completion_percent": completion,
                    "print_phase": _detect_phase(completion),
                    "image_base64": None,
                    "monitoring_mode": "telemetry",
                    "printer_state": current_state,
                    "temperatures": temps,
                    "state_changes": list(state_changes),
                    "note": "telemetry-only monitoring (no camera)",
                }
            )

        elapsed = round(time.time() - start_time, 1)
        logger.info(
            "Telemetry monitoring complete on %s: %d checks in %.1f s",
            self._printer_name,
            len(telemetry_snapshots),
            elapsed,
        )
        return MonitorResult(
            success=True,
            outcome="passed",
            snapshots=telemetry_snapshots,
            duration_seconds=elapsed,
            message=(
                f"Telemetry monitoring complete: {len(telemetry_snapshots)} state check(s) "
                f"over {elapsed}s. Print is progressing normally. "
                "No camera available — visual inspection was not performed."
            ),
        )

    def _wait_printing(self, seconds: float, session_start: float) -> bool:
        """Sleep in short increments, checking printer state each tick."""
        poll_interval = min(15.0, seconds)
        deadline = time.time() + seconds

        while time.time() < deadline:
            sleep_time = min(poll_interval, deadline - time.time())
            if sleep_time > 0:
                time.sleep(sleep_time)

            try:
                state = self._adapter.get_state()
            except PrinterError:
                # Transient error — keep waiting
                continue

            if state.confirmed_state in _TERMINAL_STATES:
                return False

        return True

    def _build_early_exit(
        self,
        start_time: float,
        *,
        snapshots: list[dict[str, Any]] | None = None,
        snapshot_failures: int = 0,
    ) -> MonitorResult:
        """Build a result for when the print ends during the delay/interval."""
        try:
            state = self._adapter.get_state()
            state_label = state.confirmed_state.value
        except PrinterError:
            state_label = "unknown"

        return MonitorResult(
            success=True,
            outcome="print_ended",
            snapshots=snapshots or [],
            snapshot_failures=snapshot_failures,
            duration_seconds=round(time.time() - start_time, 1),
            message=(
                f"Print ended (state={state_label}) before monitoring "
                "could complete.  Any collected snapshots are returned."
            ),
        )

    def _capture_snapshot(self, index: int) -> dict[str, Any] | None:
        """Capture a single snapshot with one retry on failure."""
        for attempt in range(2):  # initial + 1 retry
            try:
                image_data = self._adapter.get_snapshot()
            except PrinterError as exc:
                logger.warning(
                    "Snapshot %d attempt %d failed on %s: %s",
                    index,
                    attempt + 1,
                    self._printer_name,
                    exc,
                )
                if attempt == 0:
                    time.sleep(2)
                continue

            if image_data is None or len(image_data) < _MIN_SNAPSHOT_BYTES:
                logger.warning(
                    "Snapshot %d attempt %d on %s returned insufficient data (%d bytes)",
                    index,
                    attempt + 1,
                    self._printer_name,
                    len(image_data) if image_data else 0,
                )
                if attempt == 0:
                    time.sleep(2)
                continue

            # Basic sanity: run heuristic analysis
            analysis = analyze_snapshot_basic(image_data)
            if not analysis["valid"]:
                logger.warning(
                    "Snapshot %d on %s failed validation: %s",
                    index,
                    self._printer_name,
                    analysis.get("warnings"),
                )
                if attempt == 0:
                    time.sleep(2)
                continue

            # Collect job progress at capture time
            try:
                job = self._adapter.get_job()
                completion = job.completion
            except PrinterError:
                completion = None

            return {
                "captured_at": time.time(),
                "completion_percent": completion,
                "print_phase": _detect_phase(completion),
                "image_base64": base64.b64encode(image_data).decode("ascii"),
                "analysis": analysis,
                "snapshot_index": index,
            }

        return None

    def _publish_vision_frame_captured(self, snap: dict[str, Any]) -> None:
        """Publish a ``VISION_FRAME_CAPTURED`` event for the snapshot."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(
                EventType.VISION_FRAME_CAPTURED,
                {
                    "printer_name": self._printer_name,
                    "completion": snap.get("completion_percent"),
                    "phase": snap.get("print_phase"),
                    "snapshot_index": snap.get("snapshot_index"),
                    "snapshot_b64": snap.get("image_base64"),
                },
                source="print_monitor",
            )
        except Exception:
            logger.debug(
                "Failed to publish VISION_FRAME_CAPTURED event", exc_info=True
            )

    def _publish_vision_alert(self, alert_type: str, detail: str) -> None:
        """Publish a ``VISION_ALERT`` event."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(
                EventType.VISION_ALERT,
                {
                    "printer_name": self._printer_name,
                    "alert_type": alert_type,
                    "detail": detail,
                },
                source="print_monitor",
            )
        except Exception:
            logger.debug("Failed to publish VISION_ALERT event", exc_info=True)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_monitor_policy(*, config_path: Any | None = None) -> MonitorPolicy:
    """Load monitoring policy from environment or config file."""
    import os

    policy = MonitorPolicy()

    # --- Config file (low precedence) ---
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        path = config_path or get_config_path()
        raw = _read_config_file(path)
        section = raw.get("monitoring", {})
        if isinstance(section, dict):
            if "first_layer_delay_seconds" in section:
                policy.first_layer_delay_seconds = int(section["first_layer_delay_seconds"])
            if "first_layer_check_count" in section:
                policy.first_layer_check_count = int(section["first_layer_check_count"])
            if "first_layer_interval_seconds" in section:
                policy.first_layer_interval_seconds = int(section["first_layer_interval_seconds"])
            if "auto_pause_on_failure" in section:
                policy.auto_pause_on_failure = bool(section["auto_pause_on_failure"])
            if "failure_confidence_threshold" in section:
                policy.failure_confidence_threshold = float(section["failure_confidence_threshold"])
            if "require_camera" in section:
                policy.require_camera = bool(section["require_camera"])
            if "max_snapshot_failures" in section:
                policy.max_snapshot_failures = int(section["max_snapshot_failures"])
            if "stall_timeout_seconds" in section:
                policy.stall_timeout_seconds = int(section["stall_timeout_seconds"])
            if "mode" in section:
                _mode_raw = str(section["mode"]).lower().strip()
                if _mode_raw in ("vision", "telemetry", "auto"):
                    policy.monitoring_mode = _mode_raw
    except Exception:
        logger.debug("Could not load monitoring config from file", exc_info=True)

    # --- Env vars (highest precedence) ---
    env_delay = os.environ.get("KILN_MONITOR_FIRST_LAYER_DELAY")
    if env_delay is not None:
        try:
            policy.first_layer_delay_seconds = int(env_delay)
        except ValueError:
            logger.warning("Invalid KILN_MONITOR_FIRST_LAYER_DELAY=%r", env_delay)

    env_checks = os.environ.get("KILN_MONITOR_FIRST_LAYER_CHECKS")
    if env_checks is not None:
        try:
            policy.first_layer_check_count = int(env_checks)
        except ValueError:
            logger.warning("Invalid KILN_MONITOR_FIRST_LAYER_CHECKS=%r", env_checks)

    env_interval = os.environ.get("KILN_MONITOR_FIRST_LAYER_INTERVAL")
    if env_interval is not None:
        try:
            policy.first_layer_interval_seconds = int(env_interval)
        except ValueError:
            logger.warning("Invalid KILN_MONITOR_FIRST_LAYER_INTERVAL=%r", env_interval)

    env_pause = os.environ.get("KILN_MONITOR_AUTO_PAUSE")
    if env_pause is not None:
        policy.auto_pause_on_failure = env_pause.lower() in ("true", "1", "yes")

    env_camera = os.environ.get("KILN_MONITOR_REQUIRE_CAMERA")
    if env_camera is not None:
        policy.require_camera = env_camera.lower() in ("true", "1", "yes")

    env_stall = os.environ.get("KILN_MONITOR_STALL_TIMEOUT")
    if env_stall is not None:
        try:
            policy.stall_timeout_seconds = int(env_stall)
        except ValueError:
            logger.warning("Invalid KILN_MONITOR_STALL_TIMEOUT=%r", env_stall)

    env_mode = os.environ.get("KILN_MONITOR_MODE")
    if env_mode is not None:
        _mode_val = env_mode.lower().strip()
        if _mode_val in ("vision", "telemetry", "auto"):
            policy.monitoring_mode = _mode_val
        else:
            logger.warning("Invalid KILN_MONITOR_MODE=%r, ignoring", env_mode)

    return policy


# ---------------------------------------------------------------------------
# Basic snapshot analysis (stdlib only, no PIL/OpenCV)
# ---------------------------------------------------------------------------

# JPEG magic bytes: FF D8 FF
_JPEG_MAGIC = b"\xff\xd8\xff"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Minimum brightness (0.0--1.0) below which we warn about camera being off.
_MIN_BRIGHTNESS = 0.05

_MIN_VARIANCE = 0.01


def analyze_snapshot_basic(image_data: bytes) -> dict[str, Any]:
    """Run basic heuristic checks on a snapshot image."""
    warnings: list[str] = []
    size_bytes = len(image_data) if image_data else 0

    # Size check
    if size_bytes < _MIN_SNAPSHOT_BYTES:
        return {
            "valid": False,
            "brightness": 0.0,
            "variance": 0.0,
            "size_bytes": size_bytes,
            "warnings": ["image too small — likely corrupt or empty"],
        }

    # Format validation
    is_jpeg = image_data[:3] == _JPEG_MAGIC
    is_png = image_data[:8] == _PNG_MAGIC

    if not is_jpeg and not is_png:
        return {
            "valid": False,
            "brightness": 0.0,
            "variance": 0.0,
            "size_bytes": size_bytes,
            "warnings": ["unrecognised image format — expected JPEG or PNG"],
        }

    # Brightness and variance estimation from raw bytes
    offset = 8 if is_png else 20  # skip headers
    sample = image_data[offset:]

    if len(sample) < 64:
        return {
            "valid": True,
            "brightness": 0.0,
            "variance": 0.0,
            "size_bytes": size_bytes,
            "warnings": ["image too small to estimate brightness"],
        }

    # Sample evenly-spaced bytes from the payload
    step = max(1, len(sample) // 1024)
    sampled_bytes = sample[::step][:1024]

    if len(sampled_bytes) == 0:
        brightness = 0.0
        variance = 0.0
    else:
        total = 0
        sq_total = 0
        count = len(sampled_bytes)
        for b in sampled_bytes:
            total += b
            sq_total += b * b
        mean = total / count
        brightness = mean / 255.0
        variance_raw = (sq_total / count) - (mean * mean)
        variance = max(0.0, variance_raw) / 16256.0

    if brightness < _MIN_BRIGHTNESS:
        warnings.append("too dark — camera may be off or lens blocked")

    if variance < _MIN_VARIANCE:
        warnings.append("very low variance — image may be uniform (camera off or blocked)")

    return {
        "valid": True,
        "brightness": round(brightness, 4),
        "variance": round(variance, 4),
        "size_bytes": size_bytes,
        "warnings": warnings,
    }
