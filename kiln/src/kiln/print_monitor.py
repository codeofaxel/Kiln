"""First-layer print monitor — structured snapshot collection for agent inspection.

Captures webcam snapshots during the critical first-layer phase of a 3D print
and returns them for the agent to analyze via a vision model.  The monitor
itself does NOT perform ML-based defect detection; it handles the timing,
state tracking, and edge cases so the agent can focus on visual assessment.

Configure via environment variables or ``~/.kiln/config.yaml``:

    KILN_MONITOR_FIRST_LAYER_DELAY   — seconds before first check (default 120)
    KILN_MONITOR_FIRST_LAYER_CHECKS  — number of snapshots to capture (default 3)
    KILN_MONITOR_FIRST_LAYER_INTERVAL — seconds between snapshots (default 60)
    KILN_MONITOR_AUTO_PAUSE          — auto-pause on failure (default true)
    KILN_MONITOR_REQUIRE_CAMERA      — refuse to start without camera (default false)
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from kiln.events import EventType
from kiln.printers.base import PrinterError, PrinterStatus

if TYPE_CHECKING:
    from kiln.events import EventBus
    from kiln.printers.base import PrinterAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase detection (mirrors server.py _detect_phase, kept local to avoid
# circular imports)
# ---------------------------------------------------------------------------

_PHASE_THRESHOLDS = {"first_layers": 10.0, "final_layers": 90.0}


def _detect_phase(completion: Optional[float]) -> str:
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
    """Configurable policy for print monitoring behavior.

    :param first_layer_delay_seconds: Wait time after print start before
        the first snapshot.
    :param first_layer_check_count: Number of snapshots to capture.
    :param first_layer_interval_seconds: Seconds between snapshots.
    :param auto_pause_on_failure: Whether to auto-pause when a failure
        is reported back by the agent.
    :param failure_confidence_threshold: Minimum confidence score (0.0--1.0)
        to trigger auto-pause.
    :param require_camera: If *True*, refuse to start monitoring when the
        adapter has no snapshot capability.
    :param max_snapshot_failures: Max consecutive snapshot failures before
        the monitor emits an alert and stops.
    """

    first_layer_delay_seconds: int = 120
    first_layer_check_count: int = 3
    first_layer_interval_seconds: int = 60
    auto_pause_on_failure: bool = True
    failure_confidence_threshold: float = 0.8
    require_camera: bool = False
    max_snapshot_failures: int = 3

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


@dataclass
class MonitorResult:
    """Outcome of a first-layer monitoring session.

    :param success: *True* if snapshots were captured without fatal errors.
    :param outcome: One of ``"passed"``, ``"failed"``, ``"no_camera"``,
        ``"print_ended"``, ``"timeout"``, ``"error"``.
    :param snapshots: List of snapshot dicts with ``phase``, ``completion``,
        ``captured_at``, and ``image_base64`` keys.
    :param snapshot_failures: Count of transient snapshot capture failures.
    :param duration_seconds: Wall-clock duration of the monitoring session.
    :param auto_paused: Whether the monitor auto-paused the print.
    :param failure_type: If auto-paused, the type of failure detected.
    :param message: Human-readable summary.
    """

    success: bool
    outcome: str
    snapshots: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_failures: int = 0
    duration_seconds: float = 0.0
    auto_paused: bool = False
    failure_type: Optional[str] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# FirstLayerMonitor
# ---------------------------------------------------------------------------

# Terminal printer states that mean the print is no longer running.
_TERMINAL_STATES = frozenset({
    PrinterStatus.IDLE,
    PrinterStatus.ERROR,
    PrinterStatus.OFFLINE,
    PrinterStatus.CANCELLING,
})

# Minimum image size in bytes to consider a snapshot valid.
_MIN_SNAPSHOT_BYTES = 100


class FirstLayerMonitor:
    """Orchestrates first-layer snapshot collection for agent inspection.

    Usage::

        monitor = FirstLayerMonitor(adapter, "voron-350", policy=policy)
        result = monitor.monitor()
        # result.snapshots contains base64-encoded images for the agent

    The :meth:`monitor` call is **blocking** — run it in a background
    thread when called from the MCP layer.
    """

    def __init__(
        self,
        adapter: PrinterAdapter,
        printer_name: str,
        *,
        policy: Optional[MonitorPolicy] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self._adapter = adapter
        self._printer_name = printer_name
        self._policy = policy or MonitorPolicy()
        self._event_bus = event_bus

    # -- public API --------------------------------------------------------

    def monitor(self) -> MonitorResult:
        """Run the first-layer monitoring session (blocking).

        Call this AFTER starting the print.  The method will:

        1. Validate camera capability (if ``require_camera`` is set).
        2. Wait for :attr:`~MonitorPolicy.first_layer_delay_seconds`.
        3. Confirm the print is actually running.
        4. Capture snapshots at the configured interval.
        5. Return collected snapshots for agent inspection.

        :returns: A :class:`MonitorResult` summarising the session.
        """
        start_time = time.time()

        # Camera capability gate
        can_snap = getattr(self._adapter.capabilities, "can_snapshot", False)
        if not can_snap:
            if self._policy.require_camera:
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
                        "Monitoring requires a camera when require_camera is enabled."
                    ),
                )
            logger.info(
                "Printer %s has no camera — monitoring will skip snapshots",
                self._printer_name,
            )

        # --- Initial delay ------------------------------------------------
        logger.info(
            "Waiting %d s before first-layer check on %s",
            self._policy.first_layer_delay_seconds,
            self._printer_name,
        )
        if not self._wait_printing(self._policy.first_layer_delay_seconds, start_time):
            return self._build_early_exit(start_time)

        # --- Snapshot collection ------------------------------------------
        snapshots: List[Dict[str, Any]] = []
        consecutive_failures = 0

        for i in range(self._policy.first_layer_check_count):
            # Wait for interval (skip on first iteration)
            if i > 0:
                if not self._wait_printing(
                    self._policy.first_layer_interval_seconds, start_time
                ):
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
                    self._printer_name, exc,
                )
                return MonitorResult(
                    success=False,
                    outcome="error",
                    snapshots=snapshots,
                    snapshot_failures=consecutive_failures,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=f"Could not read printer state: {exc}",
                )

            if state.state in _TERMINAL_STATES:
                logger.info(
                    "Printer %s entered %s during monitoring — stopping",
                    self._printer_name, state.state.value,
                )
                return MonitorResult(
                    success=True,
                    outcome="print_ended",
                    snapshots=snapshots,
                    snapshot_failures=consecutive_failures,
                    duration_seconds=round(time.time() - start_time, 1),
                    message=(
                        f"Print ended (state={state.state.value}) during first-layer "
                        "monitoring.  Collected snapshots returned."
                    ),
                )

            # Capture snapshot
            if can_snap:
                snap = self._capture_snapshot(i + 1)
                if snap is not None:
                    snapshots.append(snap)
                    consecutive_failures = 0
                    self._publish_vision_check(snap)
                else:
                    consecutive_failures += 1
                    logger.warning(
                        "Snapshot %d/%d failed on %s (consecutive: %d)",
                        i + 1, self._policy.first_layer_check_count,
                        self._printer_name, consecutive_failures,
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
                snapshots.append({
                    "captured_at": time.time(),
                    "completion_percent": completion,
                    "print_phase": _detect_phase(completion),
                    "image_base64": None,
                    "note": "no camera available",
                })

        elapsed = round(time.time() - start_time, 1)
        logger.info(
            "First-layer monitoring complete on %s: %d snapshots in %.1f s",
            self._printer_name, len(snapshots), elapsed,
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

    def _wait_printing(self, seconds: float, session_start: float) -> bool:
        """Sleep in short increments, checking printer state each tick.

        :returns: *True* if the printer was still printing after the wait,
            *False* if it entered a terminal state.
        """
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

            if state.state in _TERMINAL_STATES:
                return False

        return True

    def _build_early_exit(
        self,
        start_time: float,
        *,
        snapshots: Optional[List[Dict[str, Any]]] = None,
        snapshot_failures: int = 0,
    ) -> MonitorResult:
        """Build a result for when the print ends during the delay/interval."""
        try:
            state = self._adapter.get_state()
            state_label = state.state.value
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

    def _capture_snapshot(self, index: int) -> Optional[Dict[str, Any]]:
        """Capture a single snapshot with one retry on failure.

        :param index: 1-based snapshot index for logging.
        :returns: Snapshot dict or *None* on failure.
        """
        for attempt in range(2):  # initial + 1 retry
            try:
                image_data = self._adapter.get_snapshot()
            except PrinterError as exc:
                logger.warning(
                    "Snapshot %d attempt %d failed on %s: %s",
                    index, attempt + 1, self._printer_name, exc,
                )
                if attempt == 0:
                    time.sleep(2)
                continue

            if image_data is None or len(image_data) < _MIN_SNAPSHOT_BYTES:
                logger.warning(
                    "Snapshot %d attempt %d on %s returned insufficient data (%d bytes)",
                    index, attempt + 1, self._printer_name,
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
                    index, self._printer_name, analysis.get("warnings"),
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

    def _publish_vision_check(self, snap: Dict[str, Any]) -> None:
        """Publish a ``VISION_CHECK`` event for the captured snapshot."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(
                EventType.VISION_CHECK,
                {
                    "printer_name": self._printer_name,
                    "completion": snap.get("completion_percent"),
                    "phase": snap.get("print_phase"),
                    "snapshot_index": snap.get("snapshot_index"),
                },
                source="print_monitor",
            )
        except Exception:
            logger.debug("Failed to publish VISION_CHECK event", exc_info=True)

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


def load_monitor_policy(*, config_path: Optional[Any] = None) -> MonitorPolicy:
    """Load monitoring policy from environment or config file.

    Precedence: env vars > config file > defaults.

    Env vars (highest precedence):

    - ``KILN_MONITOR_FIRST_LAYER_DELAY`` — seconds (int)
    - ``KILN_MONITOR_FIRST_LAYER_CHECKS`` — count (int)
    - ``KILN_MONITOR_FIRST_LAYER_INTERVAL`` — seconds (int)
    - ``KILN_MONITOR_AUTO_PAUSE`` — ``"true"``/``"false"``
    - ``KILN_MONITOR_REQUIRE_CAMERA`` — ``"true"``/``"false"``

    Config file (``~/.kiln/config.yaml``)::

        monitoring:
          first_layer_delay_seconds: 120
          first_layer_check_count: 3
          first_layer_interval_seconds: 60
          auto_pause_on_failure: true
          require_camera: false
    """
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

    return policy


# ---------------------------------------------------------------------------
# Basic snapshot analysis (stdlib only, no PIL/OpenCV)
# ---------------------------------------------------------------------------

# JPEG magic bytes: FF D8 FF
_JPEG_MAGIC = b"\xff\xd8\xff"

# PNG magic bytes: 89 50 4E 47 0D 0A 1A 0A
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Minimum brightness (0.0--1.0) below which we warn about camera being off.
_MIN_BRIGHTNESS = 0.05

# Minimum variance below which image is likely uniform (blocked/off camera).
_MIN_VARIANCE = 0.01


def analyze_snapshot_basic(image_data: bytes) -> Dict[str, Any]:
    """Run basic heuristic checks on a snapshot image.

    Uses only stdlib -- no PIL or OpenCV dependency.  Checks for obvious
    problems like corrupted images, blocked cameras, or cameras that are
    turned off.

    :param image_data: Raw image bytes (JPEG or PNG).
    :returns: Analysis dict with keys:

        - ``valid`` (bool) -- image is a valid JPEG/PNG
        - ``brightness`` (float) -- 0.0--1.0, low means camera off/blocked
        - ``variance`` (float) -- low means uniform image
        - ``size_bytes`` (int) -- raw byte count
        - ``warnings`` (list[str]) -- human-readable issues

    For actual print-quality defect detection, the agent should use a
    vision model on the base64-encoded image.
    """
    warnings: List[str] = []
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

    # Brightness and variance estimation from raw bytes.
    # For JPEG: sample from the compressed payload (not pixel-accurate, but
    # sufficient for detecting all-black / all-white / uniform images).
    # For PNG: skip the 8-byte header and sample from the data stream.
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
        # Normalise variance to 0.0--1.0 (max raw variance is ~16256 for
        # uniform distribution of 0..255).
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
