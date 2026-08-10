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

import requests

from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
    canonical_model_key,
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
# Cmd 403 is a shared "update settings" command: the effect depends on
# which top-level key the Data.Data payload carries -- LightStatus (the
# unused _CMD_TOGGLE_LIGHT alias below), TargetFanSpeed, or PrintSpeedPct
# are all documented under this same command number, not three different
# commands.  Source: the OpenCentauri SDCP v3 reference
# (docs.opencentauri.cc/software/api/), cross-checked against two
# independent reverse-engineering projects (github.com/WalkerFrederick/
# sdcp-centauri-carbon, github.com/JoergSH/elegoocc) that document the
# same TargetFanSpeed.{ModelFan,AuxiliaryFan,BoxFan} shape.
_CMD_TOGGLE_LIGHT = 403
_CMD_UPDATE_SETTINGS = 403
_CMD_SET_TIMING = 512

# SDCP ack codes
_ACK_SUCCESS = 0
_ACK_FAILURE = 1
_ACK_FILE_NOT_FOUND = 2

# This adapter also talks to resin/MSLA printers (Saturn, Mars) that have
# no part-cooling fan concept at all, and SDCP has no machine-type enum to
# gate on -- only the free-text Name/MachineName the printer reports at
# connect time.  set_fan() refuses unless that name matches one of these
# substrings (case-insensitive), so an unrecognized or undetermined machine
# fails closed rather than risk sending a fan command to a resin printer.
# The only FDM family this adapter documents today is Centauri Carbon.
_FDM_MACHINE_NAME_SUBSTRINGS: tuple[str, ...] = ("centauri",)

# Aliases accepted for the single part-cooling fan -- mirrors
# PrinterAdapter._PART_COOLING_FAN_ALIASES, kept local here because SDCP's
# TargetFanSpeed takes a 0-100 percent directly (no 0-255 PWM scaling), so
# this adapter can't reuse _validate_part_fan's PWM-scaled return value.
_ELEGOO_PART_FAN_ALIASES: frozenset[str] = frozenset({"part", "part_cooling", "cooling"})

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

# SDCP_PRINT_CAUSE_* codes that should feed the kiln-pro nozzle
# wear cross-check.  These appear on the wire as the integer
# ``ErrorStatusReason`` field in SDCP V3 status / history messages
# on Centauri Carbon FDM printers (and successors that share the
# cbd-tech firmware family).  Source: OpenCentauri SDCP v3 reference
# at github.com/OpenCentauri/OpenCentauri/blob/main/docs/software/api.md
# — see "Status.PrintInfo / HistoryDetailList[].ErrorStatusReason".
#
# Conservative selection: only codes that the firmware itself
# attributes to filament-path behaviour (jam, runout).  Generic
# print-error codes (move-abnormal, home-failed, bed-adhesion,
# temp-error) are NOT flow signals and feeding them into the
# wear cross-check would poison the gram-count correlation.
_FLOW_ANOMALY_CAUSE_CODES: tuple[int, ...] = (
    3,   # SDCP_PRINT_CAUSE_FILAMENT_RUNOUT — feed exhausted / out
    6,   # SDCP_PRINT_CAUSE_FILAMENT_JAM    — feed blocked / clogged
)

# Severity mapping for flow-anomaly cause codes.  Used when the
# wire fires record_extrusion_event so the wear cross-check
# weights strong signals (jam = head can't feed at all) over
# weaker ones (runout = spool empty, mechanically distinct from
# nozzle bore widening but still a flow interruption worth
# logging).
_FLOW_ANOMALY_SEVERITY: dict[int, str] = {
    3: "medium",  # runout — spool-side, but flow is interrupted
    6: "high",    # jam — strong indicator of nozzle / path failure
}


def _classify_flow_anomaly(cause_code: int | None) -> tuple[str, str] | None:
    """Return ``(event_type, severity)`` for an Elegoo SDCP cause code.

    Maps the SDCP ``ErrorStatusReason`` integer (a.k.a.
    ``SDCP_PRINT_CAUSE_*``) into the kiln-pro
    :func:`record_extrusion_event` parameter shape.  Returns
    ``None`` for healthy / unrelated codes so callers can fall
    through without firing the wire.

    Args:
        cause_code: Integer ``ErrorStatusReason`` from a Centauri
            Carbon SDCP V3 status push.  ``0`` and ``None`` are
            both treated as "no anomaly."

    Returns:
        ``("filament_jam", "high")`` for code 6,
        ``("under_extrusion", "medium")`` for code 3,
        ``None`` for any other code (including unrelated errors
        like ``LEVEL_FAILED`` or ``HOME_FAILED`` that aren't flow
        signals).
    """
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
        self._ws_lock = threading.RLock()  # RLock: _ensure_ws can be called from _send_command
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

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display.

        SDCP attribute frames carry two identity fields with different
        trust levels (semantics per the SDCP client ecosystem —
        ``MachineName`` is the machine model, ``Name`` is the
        user-assignable device name):

        * ``MachineName`` — the model.  Reported as the canonical Kiln
          key when it maps (``elegoo_centauri_carbon``, ...), verbatim
          otherwise.
        * ``Name`` — consulted only when ``MachineName`` is absent, and
          only when it maps to a canonical key.  Never verbatim: a
          user-renamed machine ("garage printer") must not show up as
          a model in the fleet table.

        Cached attributes are read first; one bounded fetch otherwise.

        SAFETY BOUNDARY: telemetry/display only.  The config-declared
        model (``printer_model`` in config.yaml) owns every safety and
        behavior decision; this self-report must never override it
        where the two disagree — it fills in only where config is
        silent (see ``PrinterAdapter.get_printer_info`` and commit
        a19e665b).
        """
        machine, device_name = self._read_identity_fields()
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

        # Flow-anomaly cross-check — when the SDCP push carries an
        # ``ErrorStatusReason`` that the firmware classifies as a
        # filament-path failure (jam or runout), feed the signal
        # into the kiln-pro nozzle wear cross-check.
        #
        # The cause code can ride in any of the nested sections
        # the firmware uses (Data, Status, PrintInfo, etc.) and we
        # already mirrored them into _last_status above — so we
        # read the merged cache instead of re-walking the message.
        # Free-tier installs without kiln-pro silently skip via
        # try/except ImportError.
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
                try:
                    from kiln_pro.nozzle_intelligence.sensor_signal import (
                        record_extrusion_event_for_printer,
                    )
                    record_extrusion_event_for_printer(
                        printer_id=self.name,
                        event_type=event_type,
                        severity=severity,
                    )
                except ImportError:
                    # Free tier — kiln-pro nozzle module not
                    # installed.  Drop the signal silently.
                    pass
                except Exception as exc:  # pragma: no cover
                    logger.debug(
                        "Flow-anomaly cross-check raised (non-fatal): %s",
                        exc,
                    )

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

    # SDCP V3 upload constants
    _SDCP_V3_UPLOAD_PORT = 3030
    _SDCP_V3_UPLOAD_PATH = "/uploadFile/upload"
    _SDCP_V3_CHUNK_SIZE = 1024 * 1024  # 1 MB per SDCP V3 spec

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer.

        Supports two upload protocols depending on the SDCP version:

        **SDCP V3 — HTTP POST push (tried first)**
            Used by newer Elegoo printers (e.g. Centauri Carbon).  The file
            is split into 1 MB chunks and POSTed directly to the printer.
            Falls back to V2 if the endpoint is unreachable.

        **SDCP V2 — WebSocket command + pull (fallback)**
            Used by older Elegoo printers (Saturn, Mars series).  Kiln
            starts a temporary HTTP server and tells the printer to fetch
            the file via SDCP command 256.

        Args:
            file_path: Absolute or relative path to the local file.

        Returns:
            :class:`~kiln.printers.base.UploadResult` indicating success
            or failure.

        Raises:
            PrinterError: If the upload fails at the protocol level.
            FileNotFoundError: If *file_path* does not exist locally.
        """
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
        """Upload via SDCP V3 HTTP POST push (Centauri Carbon / SDCP V3+).

        Returns an :class:`UploadResult` on success or definitive failure,
        or ``None`` if the printer's HTTP upload endpoint is not reachable
        (caller should fall back to the V2 pull method).
        """
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
        """Upload via SDCP V2 — start a local HTTP server, tell the printer to pull.

        Legacy method for older Elegoo SDCP printers (Saturn, Mars series).
        """
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
    # Fan control
    # ------------------------------------------------------------------

    def _read_identity_fields(self) -> tuple[str, str]:
        """``(machine_name, device_name)`` from the SDCP attribute
        fields — cached frames first, one bounded fetch otherwise.
        Empty strings when unavailable.  ``MachineName`` is the machine
        model; ``Name`` is the user-assignable device name.
        """
        with self._state_lock:
            machine = str(self._last_status.get("MachineName") or "").strip()
            device = str(self._last_status.get("Name") or "").strip()
        if machine or device:
            return machine, device
        try:
            resp = self._send_command(_CMD_GET_ATTRIBUTES, timeout=5.0)
        except PrinterError:
            return "", ""
        data = (resp or {}).get("Data", resp or {})
        return (
            str(data.get("MachineName") or "").strip(),
            str(data.get("Name") or "").strip(),
        )

    def _resolve_machine_name(self) -> str:
        """Return the printer's reported Name/MachineName, fetching fresh
        attributes if nothing is cached yet.  Returns ``""`` if it can't be
        determined -- callers must treat that as "unknown", never as FDM.
        """
        machine, device = self._read_identity_fields()
        return device or machine

    def set_fan(self, node: str, percent: int) -> bool:
        """Set the part-cooling fan speed via SDCP's settings command.

        Only the single default part-cooling fan is supported. Refuses on
        any machine that doesn't report an FDM-family name (this adapter
        also talks to resin/MSLA printers with no part-cooling fan at all,
        and SDCP has no machine-type field to gate on structurally --
        see :data:`_FDM_MACHINE_NAME_SUBSTRINGS`).

        Args:
            node: Must be ``"part"`` (or the aliases ``"part_cooling"`` /
                ``"cooling"``) — the part-cooling fan (SDCP's ``ModelFan``).
            percent: Fan speed 0-100 (0 turns the fan off, 100 is full speed;
                SDCP takes this percentage directly, no PWM scaling).

        Returns:
            ``True`` once the command is sent.

        Raises:
            PrinterError: If *node* is not the part-cooling fan, *percent* is
                outside 0-100, or the machine isn't a recognized FDM model.
        """
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
                "and Elegoo's resin/MSLA printers have no part-cooling fan."
            )

        self._send_command_checked(
            _CMD_UPDATE_SETTINGS,
            {"TargetFanSpeed": {"ModelFan": pct}},
        )
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
            # Wildcard bind on an ephemeral port so broadcast replies
            # from printers on any local interface reach us; the socket
            # lives only for the discovery window below.
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
