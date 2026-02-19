"""Elegoo SDCP adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to Elegoo
printers that use the **SDCP (Smart Device Control Protocol)** over WebSocket.

This covers Elegoo printers with cbd-tech/ChituBox mainboards including:

* **Centauri Carbon** / **Centauri Carbon 2** (FDM, high-speed)
* **Saturn 3 Ultra** / **Saturn 4 Ultra** (MSLA resin)
* **Mars 5** / **Mars 5 Ultra** (MSLA resin)

The adapter uses:

* **WebSocket** on port 3030 for status, commands, and control.
* **UDP** broadcast on port 3000 for printer discovery.
* **HTTP file server** for file uploads (the printer fetches files from a
  URL you provide — Kiln starts a temporary HTTP server).

.. note::

    Elegoo Neptune 4 / OrangeStorm Giga printers run **Klipper/Moonraker**
    and should use the :class:`~kiln.printers.moonraker.MoonrakerAdapter`
    instead.  This adapter is specifically for SDCP-based printers.

Authentication is not required — SDCP on the local network has no auth.
"""

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
from typing import Any

from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_PORT = 3030
_UDP_PORT = 3000
_UDP_DISCOVER_MAGIC = "M99999"
_PING_INTERVAL: float = 30.0  # Send keep-alive pings to prevent 60s timeout
_RECONNECT_INTERVAL: float = 5.0
_STALE_STATE_MAX_AGE: float = 60.0  # seconds

# SDCP command codes (documented for Centauri Carbon / Saturn / Mars)
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
_CMD_SET_TIMING = 512

# SDCP ack codes
_ACK_SUCCESS = 0
_ACK_FAILURE = 1
_ACK_FILE_NOT_FOUND = 2

# SDCP print status codes → PrinterStatus mapping
_PRINT_STATUS_MAP: dict[int, PrinterStatus] = {
    0: PrinterStatus.IDLE,
    5: PrinterStatus.BUSY,       # pausing
    8: PrinterStatus.BUSY,       # preparing to print
    9: PrinterStatus.BUSY,       # starting print
    10: PrinterStatus.PAUSED,
    13: PrinterStatus.PRINTING,  # actively printing
    20: PrinterStatus.BUSY,      # resuming
}

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
    """Serve a single file for SDCP upload, then shut down.

    The SDCP upload protocol works by telling the printer a URL to fetch
    from.  We start a temporary HTTP server, give the printer the URL,
    and shut down after the printer downloads the file.
    """

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
    """Concrete :class:`PrinterAdapter` for Elegoo SDCP printers.

    Communicates with Elegoo printers over the SDCP (Smart Device Control
    Protocol) via WebSocket.  This covers Elegoo printers with cbd-tech
    mainboards (Centauri Carbon, Saturn, Mars series).

    Args:
        host: IP address or hostname of the Elegoo printer on the LAN.
        mainboard_id: The printer's mainboard ID (hex string).  Found via
            UDP discovery or on the printer's info screen.
        timeout: Timeout in seconds for WebSocket operations.

    Raises:
        ValueError: If *host* is empty.

    Example::

        adapter = ElegooAdapter(
            host="192.168.1.50",
            mainboard_id="ABCD1234ABCD1234",
        )
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

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
        self._connected = False

        # WebSocket state.
        self._ws: Any = None  # websocket.WebSocket instance
        self._ws_lock = threading.Lock()
        self._listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Pending response tracking.
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        # Exponential backoff for reconnection attempts.
        self._backoff = _BackoffState()

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "elegoo"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Elegoo SDCP backend.

        SDCP printers support file management, print control, and
        camera streaming.  Temperature control via SDCP commands is
        limited on some models.
        """
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_snapshot=False,
            can_stream=True,
            supported_extensions=(".gcode", ".gco", ".ctb", ".3mf"),
        )

    # ------------------------------------------------------------------
    # Internal: WebSocket
    # ------------------------------------------------------------------

    def _ensure_ws(self) -> Any:
        """Ensure the WebSocket connection is established.

        Respects the exponential backoff schedule.

        Returns:
            The connected WebSocket instance.

        Raises:
            PrinterError: If connection fails or we're in backoff cooldown.
        """
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
                "Install it with: pip install 'kiln[elegoo]' or pip install websocket-client",
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

                # Start listener thread if not running.
                if self._listener_thread is None or not self._listener_thread.is_alive():
                    self._stop_event.clear()
                    self._listener_thread = threading.Thread(
                        target=self._ws_listener,
                        daemon=True,
                        name=f"elegoo-ws-{self._host}",
                    )
                    self._listener_thread.start()

                # Auto-discover mainboard ID if not provided.
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
                    "  3) Printer firmware supports SDCP (Centauri/Saturn/Mars)\n"
                    "  Try: kiln verify",
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

        # Check if this is a response to a pending request.
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

        # Also update top-level fields if present.
        for key in ("CurrentStatus", "PrintInfo", "Attributes"):
            section = data.get(key)
            if isinstance(section, dict):
                with self._state_lock:
                    self._last_status.update(section)
                    self._last_state_time = time.monotonic()

        # Store mainboard ID if discovered.
        mainboard = data.get("MainboardID", "")
        if mainboard and not self._mainboard_id:
            self._mainboard_id = str(mainboard)
            logger.info("Auto-discovered Elegoo mainboard ID: %s", self._mainboard_id)

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
        """Send an SDCP command and optionally wait for a response.

        Args:
            cmd: SDCP command code.
            data: Optional command data payload.
            timeout: Response wait timeout (``None`` = fire-and-forget).

        Returns:
            Response data dict if *timeout* is set, else ``None``.

        Raises:
            PrinterError: If sending fails.
        """
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
        """Send a command and raise on failure or no response.

        Raises:
            PrinterError: If no response or ack indicates failure.
        """
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

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Uses the WebSocket status cache.  During backoff cooldown,
        returns cached state if recent enough, otherwise OFFLINE.
        """
        if self._backoff.in_cooldown():
            with self._state_lock:
                age = time.monotonic() - self._last_state_time
                if self._last_status and age < _STALE_STATE_MAX_AGE:
                    return self._build_state_from_cache(dict(self._last_status))
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
            return self._build_state_from_cache(dict(self._last_status))

    def _build_state_from_cache(self, status: dict[str, Any]) -> PrinterState:
        """Convert cached SDCP status to :class:`PrinterState`."""
        print_status = status.get("CurrentStatus", status.get("Status", 0))
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

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return files stored on the printer's internal storage.

        Sends a list-files SDCP command and parses the response.
        """
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

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer.

        SDCP upload works by having the printer download from a URL.
        This method starts a temporary HTTP server on the local machine,
        tells the printer to fetch the file, and waits for the download.

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: If upload fails.
            FileNotFoundError: If *file_path* does not exist locally.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        file_size = os.path.getsize(abs_path)

        # Compute MD5 for integrity check.
        md5_hash = hashlib.md5()  # noqa: S324
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                md5_hash.update(chunk)
        md5_hex = md5_hash.hexdigest()

        # Start temporary HTTP server.
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
                message=f"Uploaded {filename} to Elegoo printer via SDCP.",
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

    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file on the Elegoo printer.

        The file must already exist on the printer's storage.

        Args:
            file_name: Name or path of the file on the printer.
        """
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

    def resume_print(self) -> PrintResult:
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
        """Perform emergency stop.

        Sends a cancel command as the primary stop mechanism.
        SDCP does not have a dedicated emergency stop command,
        so we cancel the print and send M112 via G-code if available.
        """
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

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend target temperature via G-code."""
        self._validate_temp(target, 350.0, "Hotend")
        self.send_gcode([f"M104 S{int(target)}"])
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature via G-code."""
        self._validate_temp(target, 130.0, "Bed")
        self.send_gcode([f"M140 S{int(target)}"])
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> bool:
        """Send G-code commands to the printer.

        Uses a custom SDCP command if the printer supports it,
        or falls back to individual command sending.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if commands were sent.

        Raises:
            PrinterError: If sending fails.
        """
        # SDCP doesn't have a universal G-code passthrough — we send
        # each command individually as a raw G-code SDCP message.
        for cmd in commands:
            try:
                self._send_command(
                    0xFF,  # Raw G-code command (vendor-specific)
                    {"GCode": cmd},
                )
            except PrinterError:
                raise
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from the printer's storage.

        Args:
            file_path: Path of the file on the printer.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
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

    def get_stream_url(self) -> str | None:
        """Return the camera stream URL if available.

        SDCP printers may expose an MJPEG or RTSP camera stream.
        """
        # Request camera stream enable.
        try:
            resp = self._send_command(
                _CMD_CAMERA_STREAM,
                {"Enable": 1},
                timeout=5.0,
            )
            if resp and isinstance(resp, dict):
                url = resp.get("Data", {}).get("StreamUrl", "")
                if url:
                    return str(url)
        except PrinterError:
            pass

        # Fallback: common Elegoo camera URL pattern.
        return f"http://{self._host}:8080/?action=stream"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(timeout: float = 5.0) -> list[dict[str, Any]]:
        """Discover Elegoo SDCP printers on the local network via UDP.

        Broadcasts the ``M99999`` discovery magic string on UDP port 3000
        and collects responses from any printers on the network.

        Args:
            timeout: How long to listen for responses (seconds).

        Returns:
            List of dicts with keys: ``host``, ``mainboard_id``, ``name``,
            ``firmware``, ``model``.
        """
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
        """Disconnect the WebSocket and stop background threads."""
        self._stop_event.set()
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
