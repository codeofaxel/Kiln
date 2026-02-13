"""Bambu Lab adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by talking to a
Bambu Lab printer (X1C, P1S, P1P, A1, A1 Mini, etc.) over the local-LAN
MQTT protocol and FTPS for file management.

Bambu printers expose:
* **MQTT** on port 8883 (TLS) for status, commands, and G-code.
* **FTPS** on port 990 (implicit TLS) for file upload/download/delete.

Authentication uses the printer's **LAN Access Code** (found on the
printer's LCD under Network settings) as both the MQTT password and
the FTPS password.  The username is always ``"bblp"``.

The adapter mirrors the retry and error-handling patterns established by
the OctoPrint and Moonraker adapters.
"""

from __future__ import annotations

import ftplib
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

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

_MQTT_PORT = 8883
_FTPS_PORT = 990
_MQTT_USERNAME = "bblp"
_FTPS_USERNAME = "bblp"

# Mapping from Bambu ``gcode_state`` strings to :class:`PrinterStatus`.
_STATE_MAP: Dict[str, PrinterStatus] = {
    "idle": PrinterStatus.IDLE,
    "finish": PrinterStatus.IDLE,
    "running": PrinterStatus.PRINTING,
    "prepare": PrinterStatus.BUSY,
    "slicing": PrinterStatus.BUSY,
    "init": PrinterStatus.BUSY,
    "pause": PrinterStatus.PAUSED,
    "failed": PrinterStatus.ERROR,
    "cancelling": PrinterStatus.CANCELLING,
    "offline": PrinterStatus.OFFLINE,
    "unknown": PrinterStatus.UNKNOWN,
}

# States that indicate a print job is active or starting.
_PRINT_ACTIVE_STATES: frozenset[str] = frozenset({
    "running", "prepare", "slicing", "init",
})


def _find_ffmpeg() -> Optional[str]:
    """Find ffmpeg binary on PATH or common install locations."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    for candidate in (
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Implicit FTPS helper (Bambu printers use port-990 implicit TLS)
# ---------------------------------------------------------------------------


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS subclass for implicit TLS (port 990).

    Standard :class:`ftplib.FTP_TLS` only supports explicit STARTTLS.
    Bambu Lab printers require the socket to be wrapped in TLS immediately
    upon connection (implicit mode), and data channels must reuse the
    control-channel TLS session to satisfy the printer's session-reuse
    requirement.
    """

    def connect(
        self,
        host: str = "",
        port: int = 0,
        timeout: float = -999,
        source_address: Any = None,
    ) -> str:
        """Connect and immediately wrap socket in TLS."""
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        self.sock = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            source_address=self.source_address,
        )
        self.af = self.sock.family
        # Wrap in TLS immediately (implicit mode).
        self.sock = self.context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd: str, rest: Any = None) -> Any:
        """Override to reuse TLS session on data channels.

        Bambu printers reject data connections whose TLS session does not
        match the control channel's session.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,  # type: ignore[union-attr]
            )
        return conn, size


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BambuAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by Bambu Lab MQTT + FTPS.

    Args:
        host: IP address or hostname of the Bambu printer on the LAN.
        access_code: LAN Access Code from the printer's LCD.
        serial: Printer serial number (used in MQTT topics).  Found on the
            printer's LCD under Device Info.
        timeout: Timeout in seconds for MQTT operations and FTP connections.

    Raises:
        ValueError: If *host*, *access_code*, or *serial* are empty.

    Example::

        adapter = BambuAdapter(
            host="192.168.1.100",
            access_code="12345678",
            serial="01P00A000000001",
        )
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

    def __init__(
        self,
        host: str,
        access_code: str,
        serial: str,
        timeout: int = 10,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not access_code:
            raise ValueError("access_code must not be empty")
        if not serial:
            raise ValueError("serial must not be empty")

        self._host = host
        self._access_code = access_code
        self._serial = serial
        self._timeout = timeout

        # MQTT topic names.
        self._topic_report = f"device/{serial}/report"
        self._topic_request = f"device/{serial}/request"

        # State cache -- updated by MQTT messages.
        self._state_lock = threading.Lock()
        self._last_status: Dict[str, Any] = {}
        self._connected = False
        self._sequence_id = 0
        self._last_update_time: float = 0.0

        # MQTT client.
        self._mqtt_client: Optional[mqtt.Client] = None
        self._mqtt_connected = threading.Event()

        # Reconnection backoff tracking.
        self._reconnect_attempt: int = 0
        self._last_reconnect_time: float = 0.0

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return "bambu"

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by the Bambu backend.

        Bambu printers use 3MF files (which contain G-code inside) and
        can also accept raw G-code commands via MQTT.  File management
        is done via FTPS, not the MQTT channel.
        """
        return PrinterCapabilities(
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_pause=True,
            can_snapshot=_find_ffmpeg() is not None,
            can_stream=True,
            supported_extensions=(".3mf", ".gcode", ".gco"),
        )

    # ------------------------------------------------------------------
    # Internal: MQTT
    # ------------------------------------------------------------------

    def _next_seq(self) -> str:
        """Return the next sequence ID as a string."""
        with self._state_lock:
            self._sequence_id += 1
            return str(self._sequence_id)

    def _ensure_mqtt(self) -> mqtt.Client:
        """Ensure the MQTT client is connected, creating it if needed.

        Returns:
            The connected MQTT client.

        Raises:
            PrinterError: If connection fails within the timeout.
        """
        if self._mqtt_client is not None and self._mqtt_connected.is_set():
            return self._mqtt_client

        # Tear down stale client that lost its connection.
        if self._mqtt_client is not None:
            logger.debug("MQTT client exists but disconnected; tearing down stale client")
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None

        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"kiln-{self._serial[:8]}",
                protocol=mqtt.MQTTv311,
            )
            client.username_pw_set(_MQTT_USERNAME, self._access_code)

            # TLS -- Bambu uses self-signed certs.
            tls_context = ssl.create_default_context()
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
            client.tls_set_context(tls_context)

            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect

            self._mqtt_connected.clear()
            client.connect(self._host, _MQTT_PORT, keepalive=60)
            client.loop_start()

            # Wait for the connection to be established.
            if not self._mqtt_connected.wait(timeout=self._timeout):
                client.loop_stop()
                raise PrinterError(
                    f"MQTT connection to {self._host}:{_MQTT_PORT} "
                    f"timed out after {self._timeout}s.\n"
                    "  Check:\n"
                    "  1) Printer is powered on and on the same network\n"
                    "  2) LAN Access Code is correct (printer → Settings → Network)\n"
                    "  3) LAN Mode is enabled on the printer\n"
                    "  4) Port 8883 is not blocked by a firewall\n"
                    "  Try: kiln verify"
                )

            self._mqtt_client = client
            return client

        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Failed to connect MQTT to {self._host}:{_MQTT_PORT}: {exc}",
                cause=exc,
            ) from exc

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        """MQTT on_connect callback."""
        client.subscribe(self._topic_report)
        self._mqtt_connected.set()
        with self._state_lock:
            self._connected = True

        # Request a full status dump.
        self._publish_command({"pushing": {
            "sequence_id": "0",
            "command": "pushall",
        }}, client=client)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any = None,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        """MQTT on_disconnect callback."""
        self._mqtt_connected.clear()
        with self._state_lock:
            self._connected = False

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """MQTT on_message callback -- update cached state."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Merge print status fields into our cache.
        # A1/A1 mini may send command as "push_status" or "PUSH_STATUS".
        print_data = payload.get("print", {})
        if isinstance(print_data, dict):
            cmd = str(print_data.get("command", "")).lower()
            if cmd == "push_status":
                with self._state_lock:
                    self._last_status.update(print_data)

    def _publish_command(
        self,
        payload: Dict[str, Any],
        *,
        client: Optional[mqtt.Client] = None,
    ) -> None:
        """Publish an MQTT command to the printer.

        Args:
            payload: The JSON command dict.
            client: Optional pre-connected client (used during on_connect).

        Raises:
            PrinterError: If publishing fails.
        """
        c = client or self._ensure_mqtt()
        try:
            result = c.publish(
                self._topic_request,
                json.dumps(payload),
                qos=1,
            )
            result.wait_for_publish(timeout=self._timeout)
        except Exception as exc:
            raise PrinterError(
                f"Failed to publish MQTT command: {exc}",
                cause=exc,
            ) from exc

    def _send_print_command(self, command: str) -> None:
        """Send a print-category command (pause/resume/stop).

        Raises:
            PrinterError: If the command fails.
        """
        self._publish_command({
            "print": {
                "sequence_id": self._next_seq(),
                "command": command,
            }
        })

    def _get_cached_status(self) -> Dict[str, Any]:
        """Get the latest status from the cache, requesting a refresh if stale.

        Returns a copy of the cached status dict.
        """
        self._ensure_mqtt()

        # If cache is empty, request a full dump and wait briefly.
        with self._state_lock:
            if not self._last_status:
                need_refresh = True
            else:
                need_refresh = False

        if need_refresh:
            self._publish_command({
                "pushing": {
                    "sequence_id": self._next_seq(),
                    "command": "pushall",
                }
            })
            # Give the printer a moment to respond.
            time.sleep(min(2.0, self._timeout / 2))

        with self._state_lock:
            return dict(self._last_status)

    # ------------------------------------------------------------------
    # Internal: FTPS
    # ------------------------------------------------------------------

    def _ftp_connect(self) -> ftplib.FTP_TLS:
        """Open an FTPS connection to the printer.

        Returns:
            A connected and authenticated :class:`ftplib.FTP_TLS` instance.

        Raises:
            PrinterError: If connection fails.
        """
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            ftp = _ImplicitFTP_TLS(context=ctx)
            ftp.connect(self._host, _FTPS_PORT, timeout=self._timeout)
            ftp.login(_FTPS_USERNAME, self._access_code)
            ftp.prot_p()  # Enable data channel encryption.
            return ftp
        except Exception as exc:
            raise PrinterError(
                f"FTPS connection to {self._host}:{_FTPS_PORT} failed: {exc}",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Uses the MQTT status cache, which is updated by periodic pushes
        from the printer and explicit ``pushall`` requests.

        Returns an OFFLINE state when MQTT is unreachable.
        """
        try:
            status = self._get_cached_status()
        except PrinterError:
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        gcode_state = status.get("gcode_state", "unknown")
        if not isinstance(gcode_state, str):
            gcode_state = "unknown"
        # A1/A1 mini sends uppercase state values (e.g. "RUNNING", "IDLE").
        gcode_state = gcode_state.lower()

        mapped = _STATE_MAP.get(gcode_state, PrinterStatus.UNKNOWN)

        return PrinterState(
            connected=True,
            state=mapped,
            tool_temp_actual=status.get("nozzle_temper"),
            tool_temp_target=status.get("nozzle_target_temper"),
            bed_temp_actual=status.get("bed_temper"),
            bed_temp_target=status.get("bed_target_temper"),
            chamber_temp_actual=status.get("chamber_temper"),
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job.

        Uses the MQTT status cache.
        """
        try:
            status = self._get_cached_status()
        except PrinterError:
            return JobProgress()

        file_name = status.get("gcode_file") or status.get("subtask_name")
        mc_percent = status.get("mc_percent")
        mc_remaining = status.get("mc_remaining_time")  # minutes

        completion: Optional[float] = None
        if mc_percent is not None:
            completion = float(mc_percent)

        # Estimate elapsed time from completion and remaining.
        print_time_seconds: Optional[int] = None
        print_time_left_seconds: Optional[int] = None

        if mc_remaining is not None:
            print_time_left_seconds = int(mc_remaining) * 60

        if completion is not None and completion > 0 and print_time_left_seconds is not None:
            # total_est = remaining / (1 - completion/100)
            fraction_left = 1.0 - (completion / 100.0)
            if fraction_left > 0:
                total_est = print_time_left_seconds / fraction_left
                print_time_seconds = max(0, int(total_est - print_time_left_seconds))

        return JobProgress(
            file_name=file_name if file_name else None,
            completion=completion,
            print_time_seconds=print_time_seconds,
            print_time_left_seconds=print_time_left_seconds,
        )

    def list_files(self) -> List[PrinterFile]:
        """Return a list of files stored on the printer's SD card.

        Uses FTPS to list the ``/sdcard/`` directory.  Tries MLSD first
        for rich metadata, falling back to NLST then LIST if the printer's
        FTP server returns a 502 (command not implemented).
        """
        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        try:
            # Try MLSD first (rich metadata: name, size, modify time).
            try:
                return self._list_via_mlsd(ftp)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("502"):
                    raise
                logger.info("MLSD not supported (502), falling back to NLST")

            # Fallback: NLST (filenames only).
            try:
                return self._list_via_nlst(ftp)
            except Exception:
                logger.info("NLST failed, falling back to LIST")

            # Last resort: LIST (raw text parsing).
            return self._list_via_list(ftp)
        except PrinterError:
            raise
        except Exception as exc:
            raise PrinterError(
                f"Failed to list files via FTPS: {exc}",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    def _list_via_mlsd(self, ftp: ftplib.FTP_TLS) -> List[PrinterFile]:
        """List files using MLSD (rich metadata: name, size, modify time)."""
        entries: List[PrinterFile] = []
        for name, facts in ftp.mlsd("/sdcard/"):
            if name in (".", ".."):
                continue
            if facts.get("type") == "dir":
                continue

            size_str = facts.get("size")
            size = int(size_str) if size_str else None

            modify = facts.get("modify")
            date_ts: Optional[int] = None
            if modify:
                try:
                    import datetime

                    dt = datetime.datetime.strptime(modify, "%Y%m%d%H%M%S")
                    date_ts = int(dt.timestamp())
                except (ValueError, OSError):
                    pass

            entries.append(PrinterFile(
                name=name,
                path=f"/sdcard/{name}",
                size_bytes=size,
                date=date_ts,
            ))
        return entries

    def _list_via_nlst(self, ftp: ftplib.FTP_TLS) -> List[PrinterFile]:
        """List files using NLST (filenames only, no metadata)."""
        names = ftp.nlst("/sdcard/")
        entries: List[PrinterFile] = []
        for raw_name in names:
            name = raw_name.rsplit("/", 1)[-1] if "/" in raw_name else raw_name
            if name in (".", ".."):
                continue
            entries.append(PrinterFile(
                name=name,
                path=f"/sdcard/{name}",
                size_bytes=None,
                date=None,
            ))
        return entries

    def _list_via_list(self, ftp: ftplib.FTP_TLS) -> List[PrinterFile]:
        """List files using LIST (raw text, parse filenames from output)."""
        lines: List[str] = []
        ftp.retrlines("LIST /sdcard/", lines.append)
        entries: List[PrinterFile] = []
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            name = parts[-1]
            if name in (".", ".."):
                continue
            # Skip directories (Unix-style listing: first char is 'd').
            if line.startswith("d"):
                continue
            size: Optional[int] = None
            if len(parts) >= 5:
                try:
                    size = int(parts[4])
                except ValueError:
                    pass
            entries.append(PrinterFile(
                name=name,
                path=f"/sdcard/{name}",
                size_bytes=size,
                date=None,
            ))
        return entries

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a file to the printer via FTPS.

        Uploads to ``/sdcard/`` on the printer.

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: On FTP errors.
            FileNotFoundError: If *file_path* does not exist locally.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path)

        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        try:
            with open(abs_path, "rb") as fh:
                ftp.storbinary(f"STOR /sdcard/{filename}", fh)
            return UploadResult(
                success=True,
                file_name=filename,
                message=f"Uploaded {filename} to Bambu printer via FTPS.",
            )
        except PermissionError as exc:
            raise PrinterError(
                f"Permission denied reading file: {abs_path}",
                cause=exc,
            ) from exc
        except Exception as exc:
            raise PrinterError(
                f"FTPS upload failed: {exc}",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _wait_for_print_start(
        self, timeout: float = 15.0, poll_interval: float = 1.0,
    ) -> bool:
        """Poll MQTT cache until printer enters a print-active state.

        Returns ``True`` if the printer transitioned to an active state
        within *timeout* seconds, ``False`` on timeout or error state.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                state = str(self._last_status.get("gcode_state", "")).lower()
            if state in _PRINT_ACTIVE_STATES:
                return True
            if state == "failed":
                return False
            time.sleep(poll_interval)
        return False

    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file on the Bambu printer.

        The file must already exist on the printer's SD card (uploaded
        via ``upload_file``).  For 3MF files, this sends the
        ``project_file`` command; for raw G-code, ``gcode_file``.

        After sending the command, polls MQTT for an actual state
        transition to confirm the printer accepted the job.

        Args:
            file_name: Name or path of the file on the printer.
        """
        # Normalise: strip leading path components if user passes full path.
        basename = os.path.basename(file_name)

        # Check if already in a print-active state (skip wait).
        with self._state_lock:
            already_active = str(
                self._last_status.get("gcode_state", "")
            ).lower() in _PRINT_ACTIVE_STATES

        if basename.lower().endswith(".3mf"):
            self._publish_command({
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "project_file",
                    "param": "Metadata/plate_1.gcode",
                    "subtask_name": basename,
                    "url": f"file:///sdcard/{basename}",
                    "bed_type": "auto",
                    "timelapse": False,
                    "bed_leveling": True,
                    "flow_cali": True,
                    "vibration_cali": True,
                    "layer_inspect": False,
                    "use_ams": False,
                    "ams_mapping": [0],
                    "profile_id": "0",
                    "project_id": "0",
                    "subtask_id": "0",
                    "task_id": "0",
                }
            })
        else:
            # Raw G-code file.
            if file_name.startswith("/"):
                path = os.path.normpath(file_name)
                if not (path.startswith("/sdcard/") or path.startswith("/cache/")):
                    raise PrinterError(
                        f"File path must be under /sdcard/ or /cache/, got: {file_name!r}"
                    )
            else:
                path = f"/sdcard/{basename}"
            self._publish_command({
                "print": {
                    "sequence_id": self._next_seq(),
                    "command": "gcode_file",
                    "param": path,
                }
            })

        # Wait for MQTT confirmation unless already active.
        if not already_active:
            if not self._wait_for_print_start():
                return PrintResult(
                    success=False,
                    message=(
                        f"Print command sent for {basename} but printer did not "
                        f"transition to an active state within timeout. "
                        f"Check printer LCD for errors."
                    ),
                )

        return PrintResult(
            success=True,
            message=f"Started printing {basename}.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""
        self._send_print_command("stop")
        return PrintResult(success=True, message="Print cancelled.")

    def emergency_stop(self) -> PrintResult:
        """Perform emergency stop via M112 G-code over MQTT."""
        self.send_gcode(["M112"])
        return PrintResult(
            success=True,
            message="Emergency stop triggered (M112 sent).",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""
        self._send_print_command("pause")
        return PrintResult(success=True, message="Print paused.")

    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job."""
        self._send_print_command("resume")
        return PrintResult(success=True, message="Print resumed.")

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend target temperature via G-code over MQTT."""
        self._validate_temp(target, 300.0, "Hotend")
        self.send_gcode([f"M104 S{int(target)}"])
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature via G-code over MQTT."""
        self._validate_temp(target, 130.0, "Bed")
        self.send_gcode([f"M140 S{int(target)}"])
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: List[str]) -> bool:
        """Send G-code commands to the Bambu printer via MQTT.

        Joins commands with newlines and sends as a ``gcode_line`` command.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if the commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """
        script = "\n".join(commands)
        self._publish_command({
            "print": {
                "sequence_id": self._next_seq(),
                "command": "gcode_line",
                "param": script,
            }
        })
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from the printer's SD card via FTPS.

        Args:
            file_path: Path of the file on the printer (e.g.
                ``"/sdcard/model.3mf"``).

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
        try:
            ftp = self._ftp_connect()
        except PrinterError:
            raise

        # Sanitise path — only allow files under /sdcard/ or /cache/
        safe_path = os.path.normpath(file_path)
        if not safe_path.startswith("/sdcard/") and not safe_path.startswith("/cache/"):
            raise PrinterError(
                f"File path must be under /sdcard/ or /cache/, got: {file_path!r}"
            )

        try:
            ftp.delete(safe_path)
            return True
        except Exception as exc:
            raise PrinterError(
                f"Failed to delete {file_path} via FTPS: {exc}",
                cause=exc,
            ) from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Webcam (optional)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Optional[bytes]:
        """Capture a webcam snapshot via the RTSP stream.

        Uses ``ffmpeg`` to grab a single JPEG frame from the printer's
        RTSP stream (``rtsps://<host>:322/streaming/live/1``).  Returns
        ``None`` if ffmpeg is not installed or the camera is unreachable.
        """
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            logger.debug("ffmpeg not found — cannot capture Bambu snapshot")
            return None

        stream_url = self.get_stream_url()
        if not stream_url:
            return None

        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-rtsp_transport", "tcp",
                    "-i", stream_url,
                    "-frames:v", "1",
                    "-f", "image2",
                    "-vcodec", "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
                return result.stdout
        except subprocess.TimeoutExpired:
            logger.debug("ffmpeg snapshot timed out for %s", self._host)
        except Exception as exc:
            logger.debug("ffmpeg snapshot failed for %s: %s", self._host, exc)

        return None

    def get_stream_url(self) -> Optional[str]:
        """Return the RTSP stream URL for the Bambu printer's camera.

        Bambu printers expose a TLS-encrypted RTSP stream at
        ``rtsps://<host>:322/streaming/live/1``.  Requires the LAN
        Access Code for RTSP authentication.
        """
        return f"rtsps://{self._host}:322/streaming/live/1"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Disconnect the MQTT client and release resources."""
        if self._mqtt_client is not None:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
            self._mqtt_connected.clear()
            with self._state_lock:
                self._connected = False

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<BambuAdapter host={self._host!r} serial={self._serial!r}>"

    def __del__(self) -> None:
        if hasattr(self, "_mqtt_client"):
            self.disconnect()
