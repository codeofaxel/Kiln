"""Elegoo SDCP adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

import requests

from kiln.cli_command import kiln_command
from kiln.printers.base import (
    STALE_STATE_WARN_AGE,
    ActiveSlotReading,
    CameraHandshake,
    FilamentHandlingUnsupported,
    FilamentOpPlan,
    FilamentOpResult,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    StreamCapability,
    TelemetryCadence,
    UploadResult,
    canonical_model_key,
)
from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_PORT = 3030
_UDP_PORT = 3000
_UDP_DISCOVER_MAGIC = "M99999"
_PING_INTERVAL: float = 30.0
_RECONNECT_INTERVAL: float = 5.0
_STALE_STATE_MAX_AGE: float = STALE_STATE_WARN_AGE

# SDCP keys that carry the print state itself
_STATE_KEYS: tuple[str, ...] = ("CurrentStatus", "Status")

# SDCP command codes
_CMD_STATUS_REQUEST = 0
_CMD_GET_ATTRIBUTES = 1
_CMD_START_PRINT = 128
_CMD_PAUSE_PRINT = 129
_CMD_CANCEL_PRINT = 130
_CMD_RESUME_PRINT = 131
_CMD_UPLOAD_FILE = 256
_CMD_DELETE_FILE = 257
_CMD_LIST_FILES = 258
_CMD_PRINT_HISTORY = 320
_CMD_CAMERA_STREAM = 386
_CMD_TOGGLE_LIGHT = 403
_CMD_UPDATE_SETTINGS = 403
_CMD_SET_TIMING = 512

# SDCP ack codes
_ACK_SUCCESS = 0
_ACK_FAILURE = 1
_ACK_FILE_NOT_FOUND = 2

_FDM_MACHINE_NAME_SUBSTRINGS: tuple[str, ...] = ("centauri",)

_ELEGOO_PART_FAN_ALIASES: frozenset[str] = frozenset({"part", "part_cooling", "cooling"})

_PRINT_STATUS_MAP: dict[int, PrinterStatus] = {
    0: PrinterStatus.IDLE,
    5: PrinterStatus.BUSY,       # pausing
    8: PrinterStatus.BUSY,       # preparing to print
    9: PrinterStatus.BUSY,       # starting print
    10: PrinterStatus.PAUSED,
    13: PrinterStatus.PRINTING,  # actively printing
    20: PrinterStatus.BUSY,      # resuming
}

# Statuses that mean "a job is in flight, keep the socket"
_ACTIVE_PRINT_STATUSES: frozenset[PrinterStatus] = frozenset(
    {PrinterStatus.PRINTING, PrinterStatus.PAUSED, PrinterStatus.BUSY}
)

# Idle connection release
_DEFAULT_IDLE_DISCONNECT_S: float = 120.0
_IDLE_DISCONNECT_ENV = "KILN_ELEGOO_IDLE_DISCONNECT_S"

_FLOW_ANOMALY_CAUSE_CODES: tuple[int, ...] = (
    3,   # SDCP_PRINT_CAUSE_FILAMENT_RUNOUT — feed exhausted / out
    6,   # SDCP_PRINT_CAUSE_FILAMENT_JAM    — feed blocked / clogged
)

# Severity mapping for flow-anomaly cause codes
_FLOW_ANOMALY_SEVERITY: dict[int, str] = {
    3: "medium",  # runout — spool-side, but flow is interrupted
    6: "high",    # jam — strong indicator of nozzle / path failure
}


def _classify_flow_anomaly(cause_code: int | None) -> tuple[str, str] | None:
    """Return ``(event_type, severity)`` for an Elegoo SDCP cause code."""
    if not cause_code:
        return None
    if cause_code not in _FLOW_ANOMALY_CAUSE_CODES:
        return None
    event_type = "filament_jam" if cause_code == 6 else "under_extrusion"
    severity = _FLOW_ANOMALY_SEVERITY.get(cause_code, "medium")
    return event_type, severity


# Backoff parameters for WebSocket reconnection.
_BACKOFF_INITIAL_DELAY: float = 1.0
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_MAX_DELAY: float = 30.0


# ---------------------------------------------------------------------------
# Backoff tracking
# ---------------------------------------------------------------------------


@dataclass
class _BackoffState:
    """Tracks exponential backoff for WebSocket reconnection attempts."""

    attempt_count: int = 0
    last_attempt_time: float = 0.0
    next_retry_time: float = 0.0

    def record_failure(self) -> None:
        """Record a failed connection attempt and advance the backoff window."""
        now = time.monotonic()
        self.attempt_count += 1
        self.last_attempt_time = now
        delay = min(
            _BACKOFF_INITIAL_DELAY * (_BACKOFF_MULTIPLIER ** (self.attempt_count - 1)),
            _BACKOFF_MAX_DELAY,
        )
        self.next_retry_time = now + delay
        logger.debug(
            "WebSocket backoff: attempt #%d, next retry in %.1fs",
            self.attempt_count,
            delay,
        )

    def record_success(self) -> None:
        """Reset backoff state after a successful connection."""
        self.attempt_count = 0
        self.last_attempt_time = time.monotonic()
        self.next_retry_time = 0.0

    def in_cooldown(self) -> bool:
        """Return ``True`` if the backoff cooldown period has not yet elapsed."""
        return time.monotonic() < self.next_retry_time


# ---------------------------------------------------------------------------
# Temporary HTTP file server for uploads
# ---------------------------------------------------------------------------


class _UploadHTTPHandler(http.server.SimpleHTTPRequestHandler):
    """Serve a single file for SDCP upload, then shut down."""

    _file_path: str = ""
    _file_name: str = ""
    _served = False

    def do_GET(self) -> None:  # noqa: N802
        """Serve the upload file."""
        if self.path.lstrip("/") != self._file_name:
            self.send_error(404)
            return
        try:
            with open(self._file_path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            _UploadHTTPHandler._served = True
        except Exception:
            self.send_error(500)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        logger.debug("Upload HTTP: " + fmt, *args)


def _get_local_ip(target_host: str) -> str:
    """Determine the local IP address reachable from *target_host*."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target_host, _UDP_PORT))
            return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ElegooAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` for Elegoo SDCP printers."""

    _DURATION_SEMANTICS: ClassVar[str] = "frozen"

    _IDLE_RELEASE_ENV = _IDLE_DISCONNECT_ENV
    _IDLE_RELEASE_DEFAULT_S = _DEFAULT_IDLE_DISCONNECT_S

    #: How Kiln learns this printer's own pause and cancel moves.
    head_position = ("Kiln reads no head position from an Elegoo SDCP printer, so its owner teaches "
                     "Kiln the moves through the guided session's questions")

    def __init__(
        self,
        host: str,
        mainboard_id: str = "",
        timeout: int = 10,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")

        self._host = host.strip()
        self._mainboard_id = mainboard_id.strip()
        self._timeout = timeout

        # State cache — updated by WebSocket messages.
        self._state_lock = threading.Lock()
        self._last_status: dict[str, Any] = {}
        self._last_state_time: float = 0.0
        # Monotonic time of the last push that actually carried a state key
        self._print_state_time: float = 0.0
        self._cadence = TelemetryCadence()
        self._connected = False

        # WebSocket state.
        self._ws: Any = None  # websocket.WebSocket instance
        self._ws_lock = threading.RLock()  # RLock: _ensure_ws can be called from _send_command
        self._listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Pending response tracking.
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        # Exponential backoff for reconnection attempts.
        self._backoff = _BackoffState()

        # Idle-release bookkeeping (PrinterAdapter)
        self._init_idle_release()

    # -- idle connection release --------------------------------------------

    def _connection_is_live(self) -> bool:
        """True while the websocket is open (idle-reaper hook)."""
        with self._ws_lock:
            return self._ws is not None and self._connected

    def _print_in_flight(self) -> bool:
        """True while the printer is mid-job, as of the last status seen."""
        with self._state_lock:
            status = dict(self._last_status)
        raw = status.get("CurrentStatus", status.get("Status", 0))
        # SDCP V3 (e.g. Centauri Carbon) reports CurrentStatus as a list.
        if isinstance(raw, list):
            raw = raw[0] if raw else 0
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return True
        return _PRINT_STATUS_MAP.get(code) in _ACTIVE_PRINT_STATUSES

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "elegoo"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Elegoo SDCP backend."""
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_snapshot=False,
            can_stream=True,
            supported_extensions=(".gcode", ".gco", ".ctb", ".3mf"),
        )

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display."""
        machine, device_name = self._read_identity_fields()
        return self._identity_from_fields(machine, device_name)

    def _identity_from_fields(
        self, machine: str, device_name: str
    ) -> PrinterInfo | None:
        """Resolve a :class:`PrinterInfo` from the two SDCP name fields."""
        if machine:
            key = canonical_model_key(machine, vendor_prefix="elegoo_")
            return PrinterInfo(
                model=key or machine, raw_model=machine, source="sdcp"
            )
        if device_name:
            key = canonical_model_key(device_name, vendor_prefix="elegoo_")
            if key:
                return PrinterInfo(
                    model=key, raw_model=device_name, source="sdcp"
                )
        return None

    def get_identity_channels(self) -> dict[str, str]:
        """The SDCP model field, and the device-name field when it maps."""
        machine, device_name = self._read_identity_fields()
        channels: dict[str, str] = {}
        if machine:
            info = self._identity_from_fields(machine, "")
            if info and info.model:
                channels["sdcp_machine_name"] = info.model
        if device_name:
            key = canonical_model_key(device_name, vendor_prefix="elegoo_")
            if key:
                channels["sdcp_device_name"] = key
        return channels

    # ------------------------------------------------------------------
    # Internal: WebSocket
    # ------------------------------------------------------------------

    def _ensure_ws(self) -> Any:
        """Ensure the WebSocket connection is established."""
        self._note_activity()
        with self._ws_lock:
            if self._ws is not None and self._connected:
                return self._ws

        if self._backoff.in_cooldown():
            raise PrinterError(
                f"WebSocket reconnection to {self._host} is in backoff cooldown "
                f"(attempt #{self._backoff.attempt_count}, "
                f"retry in {self._backoff.next_retry_time - time.monotonic():.1f}s)"
            )

        try:
            import websocket
        except ImportError as exc:
            raise PrinterError(
                "Elegoo SDCP support requires the websocket-client package.  "
                "Install it with: pip install 'kiln3d[elegoo]' or pip install websocket-client",
            ) from exc

        with self._ws_lock:
            # Tear down stale connection.
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    self._ws.close()
                self._ws = None

            try:
                ws = websocket.WebSocket()
                ws.settimeout(self._timeout)
                ws.connect(f"ws://{self._host}:{_WS_PORT}/websocket")
                self._ws = ws
                self._connected = True
                self._backoff.record_success()

                if self._listener_thread is None or not self._listener_thread.is_alive():
                    self._stop_event.clear()
                    self._listener_thread = threading.Thread(
                        target=self._ws_listener,
                        daemon=True,
                        name=f"elegoo-ws-{self._host}",
                    )
                    self._listener_thread.start()

                self._start_idle_reaper()

                if not self._mainboard_id:
                    self._discover_mainboard_id()

                # Request initial status.
                self._send_command(_CMD_STATUS_REQUEST)

                return ws
            except Exception as exc:
                self._backoff.record_failure()
                raise PrinterError(
                    f"Failed to connect WebSocket to {self._host}:{_WS_PORT}: {exc}\n"
                    "  Checklist:\n"
                    "  1) Printer is powered on and on the same network\n"
                    "  2) Port 3030 is not blocked by a firewall\n"
                    "  3) Printer firmware supports SDCP (Centauri Carbon)\n"
                    f"  Try: {kiln_command('verify')}",
                    cause=exc,
                ) from exc

    def _ws_listener(self) -> None:
        """Background thread that receives WebSocket messages."""
        while not self._stop_event.is_set():
            try:
                ws = self._ws
                if ws is None:
                    time.sleep(0.5)
                    continue
                ws.settimeout(1.0)
                try:
                    raw = ws.recv()
                except Exception:
                    # Timeout or connection lost — check stop event and retry.
                    if not self._connected:
                        break
                    continue

                if not raw:
                    continue

                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                self._handle_message(msg)

            except Exception as exc:
                logger.debug("WebSocket listener error: %s", exc)
                with self._ws_lock:
                    self._connected = False
                break

    def _handle_message(self, msg: dict[str, Any]) -> None:
        """Process an incoming SDCP message."""
        data = msg.get("Data", msg)
        if not isinstance(data, dict):
            return

        request_id = data.get("RequestID", "")

        if request_id:
            with self._pending_lock:
                event = self._pending.get(request_id)
                if event:
                    self._responses[request_id] = data
                    event.set()

        # Extract status fields from push updates.
        status_data = data.get("Data", data.get("Status", {}))
        if isinstance(status_data, dict):
            with self._state_lock:
                self._last_status.update(status_data)
                self._last_state_time = time.monotonic()
                self._stamp_state_vintage_locked(status_data)

        for key in ("CurrentStatus", "PrintInfo", "Attributes"):
            section = data.get(key)
            if isinstance(section, dict):
                with self._state_lock:
                    self._last_status.update(section)
                    self._last_state_time = time.monotonic()
                    self._stamp_state_vintage_locked(section)

        mainboard = data.get("MainboardID", "")
        if mainboard and not self._mainboard_id:
            self._mainboard_id = str(mainboard)
            logger.info("Auto-discovered Elegoo mainboard ID: %s", self._mainboard_id)

        with self._state_lock:
            cause_code = self._last_status.get("ErrorStatusReason")
        if isinstance(cause_code, (int, str)):
            try:
                code_int = int(cause_code)
            except (TypeError, ValueError):
                code_int = 0
            flow = _classify_flow_anomaly(code_int)
            if flow is not None:
                event_type, severity = flow
                from kiln._pro_nozzle_bridge import report_flow_anomaly

                report_flow_anomaly(self.name, event_type, severity)

    def _discover_mainboard_id(self) -> None:
        """Attempt to discover the mainboard ID via get-attributes command."""
        try:
            resp = self._send_command(_CMD_GET_ATTRIBUTES, timeout=5.0)
            if resp and isinstance(resp, dict):
                mb_id = resp.get("Data", {}).get("MainboardID", "")
                if not mb_id:
                    mb_id = resp.get("MainboardID", "")
                if mb_id:
                    self._mainboard_id = str(mb_id)
                    logger.info("Discovered mainboard ID: %s", self._mainboard_id)
        except Exception as exc:
            logger.debug("Could not discover mainboard ID: %s", exc)

    def _send_command(
        self,
        cmd: int,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """Send an SDCP command and optionally wait for a response."""
        ws = self._ensure_ws()
        request_id = str(uuid.uuid4())

        payload: dict[str, Any] = {
            "Id": request_id,
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": request_id,
                "MainboardID": self._mainboard_id,
                "TimeStamp": int(time.time()),
                "From": 1,
            },
        }

        wait_timeout = timeout if timeout is not None else self._timeout

        # Set up response tracking.
        event = threading.Event()
        with self._pending_lock:
            self._pending[request_id] = event

        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise PrinterError(
                f"Failed to send SDCP command {cmd}: {exc}",
                cause=exc,
            ) from exc

        if timeout is None:
            # Fire-and-forget: clean up after a brief wait.
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return None

        # Wait for response.
        if not event.wait(timeout=wait_timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
                self._responses.pop(request_id, None)
            logger.debug("SDCP command %d timed out after %.1fs", cmd, wait_timeout)
            return None

        with self._pending_lock:
            self._pending.pop(request_id, None)
            return self._responses.pop(request_id, None)

    def _send_command_checked(
        self,
        cmd: int,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a command and raise on failure or no response."""
        effective_timeout = timeout if timeout is not None else float(self._timeout)
        resp = self._send_command(cmd, data, timeout=effective_timeout)
        if resp is None:
            raise PrinterError(f"No response from printer for SDCP command {cmd}")
        ack = resp.get("Data", resp).get("Ack", _ACK_SUCCESS)
        if ack != _ACK_SUCCESS:
            raise PrinterError(
                f"SDCP command {cmd} failed with ack code {ack}"
            )
        return resp

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def _stamp_state_vintage_locked(self, payload: dict[str, Any]) -> None:
        """Record the arrival time of a payload that carries the print state."""
        if any(key in payload for key in _STATE_KEYS):
            self._print_state_time = self._last_state_time
            self._cadence.record(self._last_state_time)

    def _print_state_age_locked(self) -> float | None:
        """Seconds since a push last carried the print state, or ``None``."""
        if not self._print_state_time:
            return None
        return max(0.0, time.monotonic() - self._print_state_time)

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
        if self._backoff.in_cooldown():
            with self._state_lock:
                age = time.monotonic() - self._last_state_time
                if self._last_status and age < self._cadence.stale_after_seconds():
                    return self._build_state_from_cache(
                        dict(self._last_status),
                        age=self._print_state_age_locked(),
                        stale_after=self._cadence.stale_after_seconds(),
                    )
            return PrinterState(connected=False, state=PrinterStatus.OFFLINE)

        try:
            self._ensure_ws()
            # Request fresh status.
            self._send_command(_CMD_STATUS_REQUEST)
            # Brief wait for push update.
            time.sleep(min(1.0, self._timeout / 4))
        except PrinterError:
            return PrinterState(connected=False, state=PrinterStatus.OFFLINE)

        with self._state_lock:
            if not self._last_status:
                return PrinterState(connected=True, state=PrinterStatus.IDLE)
            return self._build_state_from_cache(
                dict(self._last_status),
                age=self._print_state_age_locked(),
                stale_after=self._cadence.stale_after_seconds(),
            )

    def _build_state_from_cache(
        self,
        status: dict[str, Any],
        *,
        age: float | None = None,
        stale_after: float | None = None,
    ) -> PrinterState:
        """Convert cached SDCP status to :class:`PrinterState`."""
        print_status = status.get("CurrentStatus", status.get("Status", 0))
        # SDCP V3 (e.g. Centauri Carbon) returns CurrentStatus as a list.
        if isinstance(print_status, list):
            print_status = print_status[0] if print_status else 0
        if isinstance(print_status, str):
            try:
                print_status = int(print_status)
            except (ValueError, TypeError):
                print_status = 0

        mapped = _PRINT_STATUS_MAP.get(print_status, PrinterStatus.UNKNOWN)

        # Extract temperatures — SDCP uses various field names.
        tool_actual = _safe_float(status.get("TempOfNozzle", status.get("NozzleTemp")))
        tool_target = _safe_float(status.get("TempOfNozzleTarget", status.get("NozzleTempTarget")))
        bed_actual = _safe_float(status.get("TempOfHotbed", status.get("BedTemp")))
        bed_target = _safe_float(status.get("TempOfHotbedTarget", status.get("BedTempTarget")))
        chamber_actual = _safe_float(status.get("TempOfBox", status.get("ChamberTemp")))

        return PrinterState(
            connected=True,
            state=mapped,
            tool_temp_actual=tool_actual,
            tool_temp_target=tool_target,
            bed_temp_actual=bed_actual,
            bed_temp_target=bed_target,
            chamber_temp_actual=chamber_actual,
            state_age_seconds=round(age, 1) if age is not None else None,
            state_stale_after_seconds=round(
                stale_after if stale_after is not None else STALE_STATE_WARN_AGE,
                1,
            ),
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job."""
        with self._state_lock:
            status = dict(self._last_status)

        if not status:
            return JobProgress()

        file_name = status.get("Filename", status.get("PrintFilename"))
        progress = _safe_float(status.get("Progress", status.get("PrintProgress")))

        # SDCP reports current/total ticks (seconds).
        current_ticks = _safe_int(status.get("CurrentTicks", status.get("PrintTime")))
        total_ticks = _safe_int(status.get("TotalTicks", status.get("PrintTimeTotal")))

        completion: float | None = None
        if progress is not None:
            completion = min(100.0, max(0.0, progress))

        print_time_seconds: int | None = None
        print_time_left_seconds: int | None = None

        if current_ticks is not None:
            print_time_seconds = current_ticks
        if total_ticks is not None and current_ticks is not None:
            print_time_left_seconds = max(0, total_ticks - current_ticks)

        # Is this the job the machine is running NOW?
        raw_code = _safe_int(status.get("CurrentStatus", status.get("Status")))
        running: bool | None = None
        if raw_code is not None:
            running = _PRINT_STATUS_MAP.get(raw_code) in _ACTIVE_PRINT_STATUSES

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
            active=running,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return files stored on the printer's internal storage."""
        try:
            resp = self._send_command_checked(
                _CMD_LIST_FILES,
                {"Url": "/local"},
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        resp_data = resp.get("Data", resp)
        file_list_raw = resp_data.get("FileList", resp_data.get("Data", {}).get("FileList", []))
        if not isinstance(file_list_raw, list):
            return []

        entries: list[PrinterFile] = []
        for item in file_list_raw:
            if not isinstance(item, dict):
                continue
            fname = item.get("name", item.get("Name", ""))
            if not fname:
                continue
            entries.append(
                PrinterFile(
                    name=fname,
                    path=item.get("path", item.get("Path", f"/local/{fname}")),
                    size_bytes=_safe_int(item.get("size", item.get("Size"))),
                    date=_safe_int(item.get("date", item.get("Date"))),
                )
            )
        return entries

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    # SDCP V3 upload constants
    _SDCP_V3_UPLOAD_PORT = 3030
    _SDCP_V3_UPLOAD_PATH = "/uploadFile/upload"
    _SDCP_V3_CHUNK_SIZE = 1024 * 1024  # 1 MB per SDCP V3 spec

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        file_size = os.path.getsize(abs_path)

        # Compute MD5 once — used by both upload paths.
        md5_hash = hashlib.md5()  # noqa: S324
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                md5_hash.update(chunk)
        md5_hex = md5_hash.hexdigest()

        # Try SDCP V3 HTTP POST push first.
        v3_result = self._upload_file_v3(abs_path, filename, file_size, md5_hex)
        if v3_result is not None:
            return v3_result
        logger.info(
            "SDCP V3 HTTP upload not available on %s; falling back to V2 pull method.",
            self._host,
        )

        # Fallback: SDCP V2 — start local HTTP server, tell printer to pull.
        return self._upload_file_v2(abs_path, filename, file_size, md5_hex)

    def _upload_file_v3(
        self,
        abs_path: str,
        filename: str,
        file_size: int,
        md5_hex: str,
    ) -> UploadResult | None:
        """Upload via SDCP V3 HTTP POST push (Centauri Carbon / SDCP V3+)."""
        upload_url = f"http://{self._host}:{self._SDCP_V3_UPLOAD_PORT}{self._SDCP_V3_UPLOAD_PATH}"
        file_uuid = uuid.uuid4().hex
        chunk_size = self._SDCP_V3_CHUNK_SIZE

        logger.debug(
            "SDCP V3 upload: %s → %s  (size=%d, md5=%s, uuid=%s)",
            filename, upload_url, file_size, md5_hex, file_uuid,
        )

        try:
            with open(abs_path, "rb") as fh:
                offset = 0
                chunk_num = 0
                while offset < file_size:
                    chunk_data = fh.read(chunk_size)
                    if not chunk_data:
                        break
                    chunk_num += 1

                    files = {
                        "File": (filename, chunk_data, "application/octet-stream"),
                    }
                    data = {
                        "S-File-MD5": md5_hex,
                        "Check": "1",
                        "Offset": str(offset),
                        "Uuid": file_uuid,
                        "TotalSize": str(file_size),
                    }

                    try:
                        resp = requests.post(
                            upload_url, data=data, files=files, timeout=60,
                        )
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                        if chunk_num == 1:
                            # Endpoint not available — signal fallback to V2.
                            logger.debug("SDCP V3 endpoint not reachable: %s", exc)
                            return None
                        # Mid-upload failure — report as a real error.
                        return UploadResult(
                            success=False,
                            file_name=filename,
                            message=f"SDCP V3 upload lost connection at chunk {chunk_num} "
                                    f"(offset {offset}): {exc}",
                        )

                    try:
                        resp_json = resp.json()
                    except Exception:
                        return UploadResult(
                            success=False,
                            file_name=filename,
                            message=f"SDCP V3 upload: unexpected response at chunk {chunk_num}: {resp.text!r}",
                        )

                    if not resp_json.get("success", False):
                        messages = resp_json.get("messages", [])
                        detail = "; ".join(
                            f"{m.get('field', '?')}: {m.get('message', '?')}"
                            for m in messages
                        )
                        return UploadResult(
                            success=False,
                            file_name=filename,
                            message=f"SDCP V3 upload rejected at chunk {chunk_num} "
                                    f"(offset {offset}): {detail or resp.text}",
                        )

                    offset += len(chunk_data)
                    pct = min(100, int(offset / file_size * 100))
                    logger.debug("SDCP V3 upload: chunk %d OK  (%d%%)", chunk_num, pct)

        except OSError as exc:
            raise PrinterError(f"SDCP V3 upload I/O error: {exc}", cause=exc) from exc

        logger.info("SDCP V3 upload complete: %s", filename)
        return UploadResult(
            success=True,
            file_name=filename,
            message=f"Uploaded {filename} to Elegoo printer via SDCP V3 HTTP push.",
        )

    def _upload_file_v2(
        self,
        abs_path: str,
        filename: str,
        file_size: int,
        md5_hex: str,
    ) -> UploadResult:
        """Upload via SDCP V2 — start a local HTTP server, tell the printer to pull."""
        local_ip = _get_local_ip(self._host)
        _UploadHTTPHandler._file_path = abs_path
        _UploadHTTPHandler._file_name = filename
        _UploadHTTPHandler._served = False

        server = http.server.HTTPServer(
            (local_ip, 0),
            _UploadHTTPHandler,
        )
        server_port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            download_url = f"http://{local_ip}:{server_port}/{filename}"
            self._send_command_checked(
                _CMD_UPLOAD_FILE,
                {
                    "Filename": filename,
                    "FileSize": file_size,
                    "MD5": md5_hex,
                    "URL": download_url,
                },
                timeout=max(60.0, float(self._timeout)),
            )

            # Wait for the printer to actually fetch the file.
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                if _UploadHTTPHandler._served:
                    break
                time.sleep(0.5)

            if not _UploadHTTPHandler._served:
                logger.warning("Printer did not fetch file within 120s; upload may have failed.")
                return UploadResult(
                    success=False,
                    file_name=filename,
                    message="Upload command sent but printer did not download the file within timeout.",
                )

            return UploadResult(
                success=True,
                file_name=filename,
                message=f"Uploaded {filename} to Elegoo printer via SDCP V2 pull.",
            )
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Upload failed: {exc}",
                cause=exc,
            ) from exc
        finally:
            server.shutdown()

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file on the Elegoo printer."""
        basename = os.path.basename(file_name)
        try:
            self._send_command_checked(
                _CMD_START_PRINT,
                {
                    "Filename": basename,
                    "StartLayer": 0,
                },
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        return PrintResult(
            success=True,
            message=f"Started printing {basename} on Elegoo printer.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        try:
            self._send_command_checked(
                _CMD_CANCEL_PRINT,
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        return PrintResult(success=True, message="Print cancelled.")

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        try:
            self._send_command_checked(
                _CMD_PAUSE_PRINT,
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        return PrintResult(success=True, message="Print paused.")

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused print job."""
        try:
            self._send_command_checked(
                _CMD_RESUME_PRINT,
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        return PrintResult(success=True, message="Print resumed.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop."""
        with contextlib.suppress(PrinterError):
            self._send_command(_CMD_CANCEL_PRINT)

        # Attempt G-code emergency stop as well.
        with contextlib.suppress(PrinterError):
            self.send_gcode(["M112"])

        return PrintResult(
            success=True,
            message="Emergency stop triggered (cancel + M112 sent).",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hotend target temperature via G-code."""
        self._validate_temp(target, 350.0, "Hotend")
        return self.send_gcode([f"M104 S{int(target)}"])

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature via G-code."""
        self._validate_temp(target, 130.0, "Bed")
        return self.send_gcode([f"M140 S{int(target)}"])

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send G-code commands to the printer."""
        commands = self._gcode_lines(commands)
        for cmd in commands:
            try:
                self._send_command(
                    0xFF,  # Raw G-code command (vendor-specific)
                    {"GCode": cmd},
                )
            except PrinterError:
                raise
        return CommandVerdict.accepted_only(
            f"Sent {len(commands)} raw G-code line(s) over SDCP. SDCP does not "
            "report execution, so the effect is not confirmed — read "
            "printer_status to check.",
            corroboration="sdcp_sent",
        )

    # ------------------------------------------------------------------
    # Fan control
    # ------------------------------------------------------------------

    def _read_identity_fields(self) -> tuple[str, str]:
        """``(machine_name, device_name)`` from the SDCP attribute fields — cached frames first, one bounded fetch otherwise."""
        with self._state_lock:
            machine = str(self._last_status.get("MachineName") or "").strip()
            device = str(self._last_status.get("Name") or "").strip()
        if machine or device:
            return machine, device
        try:
            resp = self._send_command(_CMD_GET_ATTRIBUTES, timeout=5.0)
        except PrinterError:
            return "", ""
        if not isinstance(resp, dict):
            return "", ""
        data = resp.get("Data", resp)
        if not isinstance(data, dict):
            return "", ""
        return (
            str(data.get("MachineName") or "").strip(),
            str(data.get("Name") or "").strip(),
        )

    def _resolve_machine_name(self) -> str:
        """Return the printer's reported Name/MachineName, fetching fresh attributes if nothing is cached yet."""
        machine, device = self._read_identity_fields()
        return device or machine

    def set_fan(self, node: str, percent: int) -> CommandVerdict:
        """Set the part-cooling fan speed via SDCP's settings command."""
        key = node.strip().lower()
        if key not in _ELEGOO_PART_FAN_ALIASES:
            raise PrinterError(
                f"Fan node {node!r} isn't supported here. This printer only "
                "exposes a single default part-cooling fan (node='part')."
            )
        try:
            pct = int(percent)
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"set_fan: percent must be an integer 0-100 ({exc}).") from exc
        if not 0 <= pct <= 100:
            raise PrinterError(f"set_fan: percent must be 0-100, got {pct}.")

        machine_name = self._resolve_machine_name()
        if not any(s in machine_name.lower() for s in _FDM_MACHINE_NAME_SUBSTRINGS):
            raise PrinterError(
                "Fan control isn't available on this printer. Kiln only "
                "supports it on Elegoo's FDM line (Centauri Carbon) -- "
                f"this machine reports as {machine_name or 'unknown'!r}, "
                "and some Elegoo models have no part-cooling fan at all."
            )

        self._send_command_checked(
            _CMD_UPDATE_SETTINGS,
            {"TargetFanSpeed": {"ModelFan": pct}},
        )
        return CommandVerdict.accepted_only(
            f"Part fan {int(percent)}%: the printer acknowledged the SDCP settings "
            "command. The fan level is not read back on this call.",
            corroboration="sdcp_ack",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from the printer's storage."""
        basename = os.path.basename(file_path)
        try:
            self._send_command_checked(
                _CMD_DELETE_FILE,
                {"Filename": basename},
                timeout=float(self._timeout),
            )
        except PrinterError:
            raise

        return True

    # ------------------------------------------------------------------
    # Webcam (optional)
    # ------------------------------------------------------------------

    _VIDEO_ACK_REASONS: ClassVar[dict[int, str]] = {
        1: (
            "The printer refused another video stream: it has reached its "
            "limit of simultaneous streaming connections. Close its web "
            "page or another viewer and try again."
        ),
        2: "The printer reports no camera fitted.",
        3: "The printer reported an unknown error when asked to enable video.",
    }

    def _read_capabilities(self) -> list[str] | None:
        """The printer's advertised sub-protocols, or ``None`` when unknown."""
        with self._state_lock:
            caps = self._last_status.get("Capabilities")
        if isinstance(caps, list):
            return [str(c) for c in caps]
        return None

    def get_stream_url(self) -> str | None:
        """Ask the printer to enable its video stream and return its address."""
        self._last_video_refusal = None
        try:
            resp = self._send_command(_CMD_CAMERA_STREAM, {"Enable": 1}, timeout=5.0)
        except PrinterError as exc:
            self._last_video_refusal = f"The printer did not answer the video request: {exc}"
            return None
        data = resp.get("Data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            self._last_video_refusal = "The printer did not answer the video request."
            return None
        ack = data.get("Ack")
        if isinstance(ack, int) and ack != 0:
            self._last_video_refusal = self._VIDEO_ACK_REASONS.get(
                ack, f"The printer refused to enable video (code {ack})."
            )
            return None
        url = str(data.get("VideoUrl") or "").strip()
        if not url:
            self._last_video_refusal = (
                "The printer enabled video but reported no stream address."
            )
            return None
        if "://" not in url:
            url = f"http://{url}"
        self._last_video_url = url
        return url

    _last_video_refusal: str | None = None
    _last_video_url: str | None = None

    camera_check_ids: ClassVar[tuple[str, ...]] = ("elegoo_video_url",)

    def camera_probes(self) -> list[Any]:
        """The address this printer gives for its own camera, to check."""
        from kiln.camera_check import CameraProbe

        ask = type(self).get_stream_url
        while getattr(ask, "_kiln_camera_wrapped", False) and hasattr(ask, "__wrapped__"):
            ask = ask.__wrapped__
        url = ask(self)
        if not url:
            return []
        return [
            CameraProbe(
                "elegoo_video_url",
                url,
                "The address this printer gave when asked to enable video: command 386 in "
                "Elegoo's published SDCP V3.0.0 protocol returns it as VideoUrl, and Elegoo's "
                "Centauri Carbon web interface opens the same address. Source: the maker.",
                note=(
                    "The printer allows a limited number of simultaneous video connections "
                    "(the MaximumVideoStreamAllowed attribute in its protocol), so this check "
                    "can take a connection an open viewer needs, or be refused while one is open."
                ),
            )
        ]

    def _own_camera_handshake(self) -> CameraHandshake:
        """Not checked: asking this printer where its camera is switches video on."""
        return CameraHandshake(
            None,
            "elegoo_video_url",
            "This printer names its camera address only when asked to switch video on "
            "(SDCP command 386), so Kiln did not ask; the camera check does, when you "
            "ask for it.",
        )

    def stream_capability(self) -> StreamCapability:
        """Whether Kiln's relay can carry this printer's video, from what the printer has said about itself."""
        if self._external_camera is not None:
            return super().stream_capability()
        caps = self._read_capabilities()
        if caps is not None and "VIDEO_WEBRTC" in caps:
            return StreamCapability(
                False,
                "webrtc",
                "This printer streams its camera over WebRTC, which Kiln's "
                "local relay does not carry. Watch it on the printer's own "
                "web page for now.",
            )
        if caps is not None and "VIDEO_STREAM" not in caps:
            return StreamCapability(
                False, None, "This printer does not advertise a video stream."
            )
        if self._last_video_refusal:
            return StreamCapability(False, None, self._last_video_refusal)
        last = self._last_video_url or ""
        if last.lower().startswith(("rtsp://", "rtsps://")):
            return StreamCapability(
                False,
                "rtsp",
                "This printer's stream is RTSP, which Kiln's local relay does "
                "not carry.",
            )
        return StreamCapability(
            True,
            "http_mjpeg",
            requires=("a camera fitted to the printer",),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(timeout: float = 5.0) -> list[dict[str, Any]]:
        """Discover Elegoo SDCP printers on the local network via UDP."""
        results: list[dict[str, Any]] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            sock.bind(("", 0))

            # Send discovery broadcast.
            sock.sendto(
                _UDP_DISCOVER_MAGIC.encode("utf-8"),
                ("<broadcast>", _UDP_PORT),
            )

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except TimeoutError:
                    break

                try:
                    parsed = json.loads(data.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        result: dict[str, Any] = {
                            "host": addr[0],
                            "mainboard_id": parsed.get("MainboardID", parsed.get("Id", "")),
                            "name": parsed.get("Name", parsed.get("MachineName", "Elegoo Printer")),
                            "firmware": parsed.get("FirmwareVersion", ""),
                            "model": parsed.get("MachineName", parsed.get("Name", "")),
                            "type": "elegoo",
                        }
                        # De-duplicate by host.
                        if not any(r["host"] == result["host"] for r in results):
                            results.append(result)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

            sock.close()
        except Exception as exc:
            logger.debug("SDCP discovery failed: %s", exc)

        return results

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Disconnect the WebSocket, releasing the printer's client slot."""
        self._stop_event.set()
        self._stop_idle_reaper()
        with self._ws_lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception as exc:
                    logger.debug("Failed to close WebSocket: %s", exc)
                self._ws = None
                self._connected = False

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    _ACTIVE_SLOT_SOURCE: ClassVar[str] = "sdcp:active_tray_id"

    def read_active_slot(self) -> ActiveSlotReading | None:
        """Which slot the multi-colour unit is feeding, read from the cache."""
        try:
            with self._state_lock:
                status = dict(self._last_status)
        except Exception:  # noqa: BLE001 -- a reading is never worth an error
            return None
        block = status.get("canvas_info")
        if not isinstance(block, dict):
            block = status
        tray = _slot_index(block.get("active_tray_id"))
        if tray is None:
            return None
        if tray < 0:
            return ActiveSlotReading(slot=None, source=self._ACTIVE_SLOT_SOURCE, verified=False)
        unit = _slot_index(block.get("active_canvas_id"))
        slot = f"{unit}:{tray}" if unit is not None and unit > 0 else str(tray)
        return ActiveSlotReading(slot=slot, source=self._ACTIVE_SLOT_SOURCE, verified=False)

    # ------------------------------------------------------------------
    # Filament handling — not available on this backend, said plainly
    # ------------------------------------------------------------------

    _FILAMENT_UNSUPPORTED_REASON = (
        "the SDCP raw G-code passthrough this adapter uses is vendor-specific "
        "and unverified on real hardware, so Kiln will not drive the extruder "
        "through it and report a result it cannot stand behind. Use the "
        "printer's own screen to load, unload, or purge."
    )

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot load filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot unload filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        raise FilamentHandlingUnsupported(
            f"{self.name} cannot purge filament through Kiln: " + self._FILAMENT_UNSUPPORTED_REASON
        )

    def __repr__(self) -> str:
        return f"<ElegooAdapter host={self._host!r} mainboard_id={self._mainboard_id!r}>"

    def __del__(self) -> None:
        if hasattr(self, "_ws"):
            self.disconnect()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    """Safely convert a value to float, returning ``None`` on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning ``None`` on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slot_index(value: Any) -> int | None:
    """A multi-colour unit's slot or unit index from a cached field, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
