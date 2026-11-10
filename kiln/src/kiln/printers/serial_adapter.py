"""USB/serial printer adapter for the Kiln printer abstraction layer."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from typing import Any, ClassVar

from kiln.printers.base import (
    CAUSE_WAITING_FOR_USER,
    DEFAULT_LOAD_LENGTH_MM,
    DEFAULT_PURGE_LENGTH_MM,
    DEFAULT_UNLOAD_LENGTH_MM,
    CodedPrintResult,
    FilamentOpPlan,
    FilamentOpResult,
    FirmwareComponent,
    FirmwareStatus,
    HardwareStopRefusal,
    JobProgress,
    JobResult,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterInfo,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UnsupportedCommand,
    UploadResult,
    WaitingForUser,
    canonical_model_key,
    hardware_stop_waiting,
    reheat_before_resume,
)
from kiln.printers.command_verdict import CommandVerdict
from kiln.printers.safe_motion import build_firmware_resume_positioning

logger = logging.getLogger(__name__)

_MAX_HOTEND_TEMP: float = 300.0
_MAX_BED_TEMP: float = 130.0

# Number of reconnect attempts on connection loss.
_MAX_RECONNECT_ATTEMPTS: int = 3

_TEMP_RE = re.compile(r"T:(?P<tool_actual>[\d.]+)\s*/(?P<tool_target>[\d.]+)")
_BED_TEMP_RE = re.compile(r"B:(?P<bed_actual>[\d.]+)\s*/(?P<bed_target>[\d.]+)")

_SD_PROGRESS_RE = re.compile(r"SD printing byte\s+(?P<current>\d+)\s*/\s*(?P<total>\d+)")

_FIRMWARE_RE = re.compile(r"FIRMWARE_NAME:(?P<name>[^\s]+)")
_FIRMWARE_VER_RE = re.compile(r"FIRMWARE_VERSION:(?P<version>[^\s]+)")
_MACHINE_TYPE_RE = re.compile(
    r"MACHINE_TYPE:(?P<machine>.*?)(?=\s+[A-Z_]+:|\s*$)", re.MULTILINE
)

_GENERIC_MACHINE_TYPES = frozenset({"3d printer", "marlin", "unknown", "reprap"})


_UNKNOWN_COMMAND_MARK = "unknown command:"
# The host keepalive
_WAITING_FOR_USER_MARKS = ("busy: paused for user", "busy: paused for input")
_BUSY_PROCESSING_MARK = "busy: processing"

_WAITING_FOR_USER_REMEDY = (
    "The printer is waiting for a click on the printer: the file told it to "
    "stop here. Press the knob or Resume on the printer to continue."
)

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SerialPrinterAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by a USB serial connection."""

    _DURATION_SEMANTICS: ClassVar[str] = "none"

    head_position = "head_and_extruder"

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 10,
        *,
        printer_name: str = "serial",
    ) -> None:
        try:
            import serial as _serial  # noqa: F401
        except ImportError as exc:
            raise PrinterError(
                "pyserial is required for USB/serial printers: pip install pyserial",
                cause=exc,
            ) from exc

        if not port:
            raise ValueError("port must not be empty")

        self._port: str = port
        self._baudrate: int = baudrate
        self._timeout: float = timeout
        self._printer_name: str = printer_name

        self._serial: Any | None = None  # serial.Serial instance
        self._lock: threading.Lock = threading.Lock()
        self._connected: bool = False

        # Track active SD print file name (set by start_print).
        self._current_file: str | None = None

        self._paused: bool = False

        self._waiting_for_user: bool = False

        self._machine_type: str | None = None
        self._capturing_machine_type: bool = False

        # Open the connection.
        self.connect()

    # -- PrinterAdapter identity properties ---------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Human-readable identifier for this adapter."""
        return self._printer_name

    @property
    def capabilities(self) -> PrinterCapabilities:
        """Capabilities supported by a serial/USB printer."""
        return PrinterCapabilities(
            can_clear_error=True,
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
            can_handle_filament=True,
            can_pause=True,
            can_stream=False,
            can_probe_bed=True,
            can_update_firmware=False,
            can_snapshot=False,
            can_detect_filament=False,
            supported_extensions=(".gcode", ".gco", ".g"),
        )

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether the serial port is currently open and connected."""
        return self._connected and self._serial is not None and self._serial.is_open

    def connect(self) -> None:
        """Open the serial port and wait for the printer's startup message."""
        import serial

        if self.is_connected:
            return

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
            )
        except serial.SerialException as exc:
            msg = str(exc).lower()
            if "permission" in msg:
                raise PrinterError(
                    f"Permission denied opening {self._port}. "
                    "Add your user to the 'dialout' group: "
                    "sudo usermod -a -G dialout $USER",
                    cause=exc,
                ) from exc
            if "no such file" in msg or "not found" in msg or "filenotfounderror" in msg:
                raise PrinterError(
                    f"Serial port {self._port} not found. Check USB cable and port path.",
                    cause=exc,
                ) from exc
            raise PrinterError(
                f"Failed to open serial port {self._port}: {exc}",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise PrinterError(
                f"OS error opening serial port {self._port}: {exc}",
                cause=exc,
            ) from exc

        self._wait_for_startup()
        self._connected = True
        logger.info("Connected to serial printer on %s @ %d baud", self._port, self._baudrate)

        self._capture_machine_type()

    def _capture_machine_type(self) -> None:
        """One best-effort M115 to learn the firmware's MACHINE_TYPE."""
        if self._machine_type is not None or self._capturing_machine_type:
            return
        self._capturing_machine_type = True
        try:
            self._capture_machine_type_locked()
        finally:
            self._capturing_machine_type = False

    def _capture_machine_type_locked(self) -> None:
        """Body of :meth:`_capture_machine_type`, guard already held."""
        try:
            response = self._send_command("M115", timeout=min(self._timeout, 3.0))
        except PrinterError:
            logger.debug("M115 machine-type capture failed", exc_info=True)
            return
        match = _MACHINE_TYPE_RE.search(response)
        if match:
            machine = match.group("machine").strip()
            if machine and machine.lower() not in _GENERIC_MACHINE_TYPES:
                self._machine_type = machine

    def disconnect(self) -> None:
        """Close the serial port."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:
                logger.debug("Failed to close serial port on disconnect: %s", exc)
        self._connected = False
        self._serial = None
        logger.info("Disconnected from serial printer on %s", self._port)

    def _wait_for_startup(self) -> None:
        """Read lines until we see 'start' or 'ok' from the printer."""
        if self._serial is None:
            return

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                line = self._serial.readline().decode("utf-8", errors="replace").strip()
            except Exception as exc:
                logger.debug("Failed to read startup line from serial port: %s", exc)
                break
            if not line:
                continue
            logger.debug("Startup: %s", line)
            lower = line.lower()
            if "start" in lower or lower == "ok":
                return
        logger.debug("No explicit startup message received; proceeding anyway")

    def _ensure_connected(self) -> None:
        """Verify the serial connection is alive; attempt reconnect if not."""
        if self.is_connected:
            return

        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            logger.warning(
                "Serial connection lost; reconnect attempt %d/%d",
                attempt,
                _MAX_RECONNECT_ATTEMPTS,
            )
            try:
                self._connected = False
                if self._serial is not None:
                    try:
                        self._serial.close()
                    except Exception as exc:
                        logger.debug("Failed to close stale serial port during reconnect: %s", exc)
                    self._serial = None
                self.connect()
                return
            except PrinterError:
                if attempt < _MAX_RECONNECT_ATTEMPTS:
                    time.sleep(1.0 * attempt)  # Linear backoff

        raise PrinterError(
            f"Lost connection to serial printer on {self._port} after {_MAX_RECONNECT_ATTEMPTS} reconnect attempts."
        )

    # ------------------------------------------------------------------
    # Internal serial communication
    # ------------------------------------------------------------------

    def _send_command(
        self,
        command: str,
        *,
        timeout: float | None = None,
        wait_for_ok: bool = True,
    ) -> str:
        """Send a single G-code command and collect the response."""
        self._ensure_connected()
        assert self._serial is not None

        effective_timeout = timeout if timeout is not None else self._timeout

        with self._lock:
            return self._send_command_locked(command, effective_timeout, wait_for_ok)

    def _send_command_locked(
        self,
        command: str,
        timeout: float,
        wait_for_ok: bool,
    ) -> str:
        """Send command while already holding the lock."""
        assert self._serial is not None

        try:
            # Flush any stale data in the input buffer.
            self._serial.reset_input_buffer()

            cmd_line = command.strip() + "\n"
            self._serial.write(cmd_line.encode("utf-8"))
            self._serial.flush()
            logger.debug("TX: %s", command.strip())
        except Exception as exc:
            self._connected = False
            raise PrinterError(
                f"Failed to send command '{command}': {exc}",
                cause=exc,
            ) from exc

        if not wait_for_ok:
            return ""

        # Collect response lines until we see "ok" or an error.
        response_lines: list[str] = []
        old_timeout = self._serial.timeout
        self._serial.timeout = timeout
        deadline = time.monotonic() + timeout
        refused_as_unknown = False

        try:
            while time.monotonic() < deadline:
                try:
                    raw = self._serial.readline()
                except Exception as exc:
                    self._connected = False
                    raise PrinterError(
                        f"Serial read error after sending '{command}': {exc}",
                        cause=exc,
                    ) from exc

                if not raw:
                    # Timeout on readline -- check deadline.
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                logger.debug("RX: %s", line)
                lower = line.lower()

                if _BUSY_PROCESSING_MARK in lower:
                    deadline = time.monotonic() + timeout
                    continue
                if any(mark in lower for mark in _WAITING_FOR_USER_MARKS):
                    self._waiting_for_user = True
                    raise WaitingForUser(command.strip())

                response_lines.append(line)

                if _UNKNOWN_COMMAND_MARK in lower:
                    refused_as_unknown = True
                    continue
                if lower.startswith("ok"):
                    self._waiting_for_user = False
                    if refused_as_unknown:
                        raise UnsupportedCommand(command.strip())
                    return "\n".join(response_lines)
                if lower.startswith("error:") or lower.startswith("error"):
                    raise PrinterError(f"Firmware error for '{command}': {line}")
        finally:
            self._serial.timeout = old_timeout

        if refused_as_unknown:
            raise UnsupportedCommand(command.strip())

        raise PrinterError(
            f"Timeout ({timeout}s) waiting for response to '{command}'. Received so far: {' | '.join(response_lines)}"
        )

    def _send_and_parse(self, command: str) -> dict[str, Any]:
        """Send a G-code command and parse key:value pairs from the response."""
        response = self._send_command(command)
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: str) -> dict[str, Any]:
        """Extract key:value pairs from a firmware response string."""
        result: dict[str, Any] = {}
        # Parse temperature-style "KEY:VALUE" pairs.
        for token in response.split():
            if ":" in token and not token.startswith("ok"):
                key, _, value = token.partition(":")
                try:
                    result[key] = float(value)
                except ValueError:
                    result[key] = value
        return result

    # ------------------------------------------------------------------
    # Temperature parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_temps(response: str) -> dict[str, float | None]:
        """Parse M105 temperature response into a structured dict."""
        result: dict[str, float | None] = {
            "tool_actual": None,
            "tool_target": None,
            "bed_actual": None,
            "bed_target": None,
        }

        match = _TEMP_RE.search(response)
        if match:
            result["tool_actual"] = float(match.group("tool_actual"))
            result["tool_target"] = float(match.group("tool_target"))

        bed_match = _BED_TEMP_RE.search(response)
        if bed_match:
            result["bed_actual"] = float(bed_match.group("bed_actual"))
            result["bed_target"] = float(bed_match.group("bed_target"))

        return result

    # ------------------------------------------------------------------
    # PrinterAdapter -- state queries
    # ------------------------------------------------------------------

    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""
        if not self.is_connected:
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        try:
            temp_response = self._send_command("M105")
        except WaitingForUser:
            return PrinterState(
                connected=True,
                state=PrinterStatus.PAUSED,
                cause=CAUSE_WAITING_FOR_USER,
                remedy=_WAITING_FOR_USER_REMEDY,
            )
        except PrinterError as exc:
            msg = str(exc).lower()
            if "firmware error" in msg or "printer halted" in msg or "thermal runaway" in msg:
                return PrinterState(
                    connected=True,
                    state=PrinterStatus.ERROR,
                )
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        temps = self._parse_temps(temp_response)

        status = PrinterStatus.IDLE
        job_result: JobResult | None = None
        try:
            sd_response = self._send_command("M27")
            sd_match = _SD_PROGRESS_RE.search(sd_response)
            if sd_match:
                current = int(sd_match.group("current"))
                total = int(sd_match.group("total"))
                if total > 0 and current < total:
                    status = PrinterStatus.PAUSED if self._paused else PrinterStatus.PRINTING
                elif total > 0 and current >= total:
                    # Print complete: the byte counter has reached the file size
                    status = PrinterStatus.IDLE
                    job_result = JobResult.COMPLETED
                    self._paused = False
            elif "not sd printing" in sd_response.lower():
                status = PrinterStatus.IDLE
                self._paused = False
        except PrinterError:
            logger.debug("M27 query failed; defaulting to IDLE", exc_info=True)

        return PrinterState(
            connected=True,
            state=status,
            last_job_result=job_result,
            tool_temp_actual=temps.get("tool_actual"),
            tool_temp_target=temps.get("tool_target"),
            bed_temp_actual=temps.get("bed_actual"),
            bed_temp_target=temps.get("bed_target"),
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active SD print job."""
        if not self.is_connected:
            return JobProgress()

        try:
            response = self._send_command("M27")
        except PrinterError:
            return JobProgress()

        sd_match = _SD_PROGRESS_RE.search(response)
        if sd_match:
            current = int(sd_match.group("current"))
            total = int(sd_match.group("total"))
            completion = round((current / total) * 100.0, 2) if total > 0 else 0.0
            return JobProgress(
                file_name=self._current_file,
                completion=completion,
            )

        return JobProgress(
            file_name=self._current_file,
        )

    def list_files(self) -> list[PrinterFile]:
        """Return a list of files on the SD card."""
        try:
            response = self._send_command("M20", timeout=15.0)
        except PrinterError as exc:
            if "no sd card" in str(exc).lower() or "volume.init" in str(exc).lower():
                raise PrinterError(
                    "No SD card detected. Insert an SD card and try again.",
                    cause=exc,
                ) from exc
            raise

        return self._parse_file_list(response)

    @staticmethod
    def _parse_file_list(response: str) -> list[PrinterFile]:
        """Parse the M20 file listing response."""
        files: list[PrinterFile] = []
        in_list = False

        for line in response.split("\n"):
            stripped = line.strip()
            lower = stripped.lower()

            if "begin file list" in lower:
                in_list = True
                continue
            if "end file list" in lower:
                break
            if not in_list or not stripped:
                continue
            if lower.startswith("ok"):
                continue

            parts = stripped.split()
            name = parts[0]
            size = None
            if len(parts) >= 2:
                with contextlib.suppress(ValueError):
                    size = int(parts[1])

            files.append(
                PrinterFile(
                    name=name,
                    path=name,
                    size_bytes=size,
                )
            )

        return files

    # ------------------------------------------------------------------
    # PrinterAdapter -- file management
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the printer's SD card."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path).upper()
        if len(filename) > 12:
            base, _, ext = filename.rpartition(".")
            filename = base[:8] + "." + ext[:3] if ext else base[:12]

        self._ensure_connected()
        assert self._serial is not None

        with self._lock:
            try:
                # Start SD write.
                self._send_command_locked(f"M28 {filename}", self._timeout, True)
            except PrinterError as exc:
                raise PrinterError(
                    f"Failed to start SD write for {filename}: {exc}",
                    cause=exc,
                ) from exc

            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    line_count = 0
                    for line in fh:
                        stripped = line.strip()
                        if not stripped or stripped.startswith(";"):
                            continue
                        cmd = stripped + "\n"
                        self._serial.write(cmd.encode("utf-8"))
                        line_count += 1
                        if line_count % 500 == 0:
                            logger.debug("Uploaded %d lines to SD card", line_count)
                            # Small delay to prevent buffer overflow.
                            time.sleep(0.01)

                    self._serial.flush()
                    logger.info("Uploaded %d lines to SD card as %s", line_count, filename)
            except PermissionError as exc:
                raise PrinterError(
                    f"Permission denied reading file: {abs_path}",
                    cause=exc,
                ) from exc
            except PrinterError:
                raise
            except Exception as exc:
                raise PrinterError(
                    f"Error during SD upload: {exc}",
                    cause=exc,
                ) from exc
            finally:
                # Always close the SD write, even on error.
                try:
                    self._send_command_locked("M29", self._timeout, True)
                except PrinterError:
                    logger.warning("M29 (stop SD write) failed", exc_info=True)

        return UploadResult(
            success=True,
            file_name=filename,
            message=f"Uploaded {filename} to SD card.",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- file deletion
    # ------------------------------------------------------------------

    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the SD card."""
        self._send_command(f"M30 {file_path}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def _start_print_impl(self, file_name: str, **_kwargs: Any) -> PrintResult:
        """Begin printing a file from the SD card."""
        self._send_command(f"M23 {file_name}")
        self._send_command("M24")
        self._current_file = file_name
        return PrintResult(
            success=True,
            message=f"Started printing {file_name} from SD card.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running SD print."""
        try:
            self._send_command("M524")
        except UnsupportedCommand:
            return self._stop_without_abort()

        self._current_file = None
        self._paused = False
        return PrintResult(
            success=True,
            message="Print cancelled.",
        )

    def _stop_without_abort(self) -> PrintResult:
        """Hold a print this firmware cannot abort: pause it, heaters off."""
        try:
            self._send_command("M25")
        except PrinterError:
            return CodedPrintResult(
                success=False,
                message=(
                    "This printer's firmware cannot abort a print over USB, and Kiln could not pause it "
                    "either, so the print may still be printing. Press Stop print on the printer's screen, "
                    "or switch the printer off if that is not safe to wait for."
                ),
                code="CANCEL_UNSUPPORTED_NOT_PAUSED",
            )
        # The job is held: still loaded, still the current one.
        self._paused = True

        stuck = []
        for command, heater in (("M104 S0", "hotend"), ("M140 S0", "bed")):
            try:
                self._send_command(command)
            except PrinterError:
                stuck.append(heater)
        if stuck:
            return CodedPrintResult(
                success=False,
                message=(
                    "This printer's firmware cannot abort a print over USB. Kiln paused it, but switching "
                    f"the {' and '.join(stuck)} heater off failed, so the heaters may still be on; turn "
                    "them off at the printer and press Stop print on the printer's screen to end it."
                ),
                code="CANCEL_UNSUPPORTED_PAUSED_INSTEAD",
            )
        return CodedPrintResult(
            success=False,
            message=(
                "This printer's firmware cannot abort a print over USB. Kiln paused it and switched the "
                "heaters off; press Stop print on the printer's screen to end it."
            ),
            code="CANCEL_UNSUPPORTED_PAUSED_INSTEAD",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running SD print."""
        self._send_command("M25")
        self._paused = True
        return PrintResult(
            success=True,
            message="Print paused.",
        )

    def resume_print(self, *, force: bool = False, hardware_confirmed: bool = False) -> PrintResult:
        """Resume a paused SD print — OVERRIDE of the base template."""
        waiting = hardware_stop_waiting(self)
        if waiting is not None and not hardware_confirmed:
            from kiln.hardware_stops import refusal_message

            return HardwareStopRefusal(success=False, message=refusal_message(waiting), hardware=waiting)
        if self._waiting_for_user:
            still_waiting = self._refusal_while_waiting_for_user()
            if still_waiting is not None:
                return still_waiting
        if not (self._paused or force):
            return self._no_paused_print_result()
        refusal, _reheated = reheat_before_resume(self)
        if refusal is not None:
            return refusal
        result = self._resume_print_impl()
        if waiting is not None and result.success:
            from kiln.hardware_stops import note_resumed

            note_resumed(self, waiting)
        return result

    def _refusal_while_waiting_for_user(self) -> PrintResult | None:
        """The refusal to give while the printer waits for a click, or ``None``."""
        try:
            self._send_command("M105")
        except WaitingForUser:
            return CodedPrintResult(
                success=False,
                message=(
                    "Kiln cannot release this wait over USB: the file told the printer to stop and wait "
                    "for a click, and it reads no commands until it gets one. Press the knob or Resume "
                    "on the printer to continue the print."
                ),
                code="WAITING_FOR_CLICK_ON_PRINTER",
            )
        return None

    def _resume_print_impl(self) -> PrintResult:
        """Resume a previously paused SD print."""
        self._send_command("M24")
        self._paused = False
        return PrintResult(
            success=True,
            message="Print resumed.",
        )

    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop."""
        m112_sent = False
        try:
            self._send_command("M112", wait_for_ok=False)
            m112_sent = True
        except PrinterError as exc:
            if "failed to send" in str(exc).lower():
                logger.error("Emergency stop: M112 write failed: %s", exc)
            else:
                # Write succeeded but read failed (expected -- printer halts).
                m112_sent = True

        self._current_file = None
        self._paused = False
        self._connected = False
        return PrintResult(
            success=m112_sent,
            message=(
                "Emergency stop triggered (M112 sent). Printer will need to be reset."
                if m112_sent
                else "Emergency stop failed: could not deliver M112 to printer."
            ),
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- temperature control
    # ------------------------------------------------------------------

    def set_tool_temp(self, target: float) -> CommandVerdict:
        """Set the hotend target temperature in degrees Celsius."""
        self._validate_temp(target, _MAX_HOTEND_TEMP, "Hotend")
        self._send_command(f"M104 S{int(target)}")
        return CommandVerdict.accepted_only(
            f"Hotend target {int(target)}°C: the firmware answered ok. The resulting state is not "
            "read back on this call — read printer_status to confirm.",
            corroboration="firmware_ok",
        )

    def set_bed_temp(self, target: float) -> CommandVerdict:
        """Set the heated-bed target temperature in degrees Celsius."""
        self._validate_temp(target, _MAX_BED_TEMP, "Bed")
        self._send_command(f"M140 S{int(target)}")
        return CommandVerdict.accepted_only(
            f"Bed target {int(target)}°C: the firmware answered ok. The resulting state is not "
            "read back on this call — read printer_status to confirm.",
            corroboration="firmware_ok",
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def clear_error(self) -> PrintResult:
        """Restart Marlin after a kill, with ``M999``."""
        self.send_gcode(["M999"])
        return PrintResult(
            success=True,
            message=(
                "Sent M999 to restart the firmware. Re-read printer_status to "
                "confirm it came back ready."
            ),
        )

    def send_gcode(self, commands: list[str]) -> CommandVerdict:
        """Send one or more G-code commands to the printer."""
        commands = self._gcode_lines(commands)
        for cmd in commands:
            self._send_command(cmd)
        return CommandVerdict.accepted_only(
            f"{len(commands)} G-code line(s): the firmware answered ok. The resulting state is not "
            "read back on this call — read printer_status to confirm.",
            corroboration="firmware_ok",
        )

    # ------------------------------------------------------------------
    # Fan control
    # ------------------------------------------------------------------

    def set_fan(self, node: str, percent: int) -> CommandVerdict:
        """Set the part-cooling fan speed via ``M106``/``M107`` G-code."""
        speed = self._validate_part_fan(node, percent)
        return self.send_gcode([f"M106 S{speed}" if speed else "M107"])

    def skip_objects(self, object_ids: list[int]) -> CommandVerdict:
        """Abandon objects on a live multi-object print via ``M486``."""
        if not object_ids:
            raise PrinterError("skip_objects requires at least one object id.")
        try:
            ids = [int(x) for x in object_ids]
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"skip_objects: object ids must be integers ({exc}).") from exc
        return self.send_gcode([f"M486 P{i}" for i in ids])

    # ------------------------------------------------------------------
    # PrinterAdapter -- calibration
    # ------------------------------------------------------------------

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run calibration routines via raw G-code over the serial link."""
        if options is None:
            options = ["bed_leveling"]

        supported = {"bed_leveling", "all"}
        unsupported = [opt for opt in options if opt not in supported]
        if unsupported:
            return PrintResult(
                success=False,
                message=(
                    f"Calibration option(s) {', '.join(repr(o) for o in unsupported)} "
                    "not supported over serial. Only bed_leveling is available."
                ),
            )

        self._send_command("G28")
        self._send_command("G29")
        return PrintResult(
            success=True,
            message=(
                "Bed leveling started (G28 + G29 sent). "
                "The printer will home and probe the bed."
            ),
        )

    # ------------------------------------------------------------------
    # PrinterAdapter -- printer identity self-report (optional)
    # ------------------------------------------------------------------

    def get_printer_info(self) -> PrinterInfo | None:
        """The printer's self-reported model, for telemetry and display."""
        machine = (self._machine_type or "").strip()
        if not machine:
            return None
        key = canonical_model_key(machine)
        return PrinterInfo(model=key or machine, raw_model=machine, source="m115")

    def get_identity_channels(self) -> dict[str, str]:
        """The single M115 channel, when it identified anything."""
        info = self.get_printer_info()
        return {"m115_machine_type": info.model} if info and info.model else {}

    # ------------------------------------------------------------------
    # PrinterAdapter -- what the machine says about its own motion
    # ------------------------------------------------------------------

    def _read_machine_motion_source(self) -> tuple[str, Any] | None:
        """The firmware's own M115 / M211 / M119 reports, for the motion gate."""
        from kiln.printers.marlin_report import read_marlin_motion_report

        state = self.get_state()
        if state.state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
            return None
        report = read_marlin_motion_report(
            lambda cmd: self._send_command(cmd, timeout=min(self._timeout, 3.0)),
        )
        return None if report is None else ("marlin_report", report)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Query firmware version via ``M115``."""
        try:
            response = self._send_command("M115")
        except PrinterError:
            return None

        fw_name = "Unknown"
        fw_version = "Unknown"

        name_match = _FIRMWARE_RE.search(response)
        if name_match:
            fw_name = name_match.group("name")

        ver_match = _FIRMWARE_VER_RE.search(response)
        if ver_match:
            fw_version = ver_match.group("version")

        return FirmwareStatus(
            busy=False,
            components=[
                FirmwareComponent(
                    name=fw_name,
                    current_version=fw_version,
                    component_type="firmware",
                ),
            ],
            updates_available=0,
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def get_tool_position(self) -> dict[str, float] | None:
        """Return current tool position via ``M114``."""
        try:
            response = self._send_command("M114")
        except PrinterError:
            return None

        result: dict[str, float] = {}
        for axis in ("X", "Y", "Z", "E"):
            match = re.search(rf"{axis}:(-?[\d.]+)", response)
            if match:
                result[axis.lower()] = float(match.group(1))

        return result if result else None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    def firmware_resume_print(
        self,
        *,
        z_height_mm: float,
        hotend_temp_c: float,
        bed_temp_c: float,
        file_name: str,
        layer_number: int | None = None,
        fan_speed_pct: float = 100.0,
        flow_rate_pct: float = 100.0,
        prime_length_mm: float = 30.0,
        z_clearance_mm: float = 2.0,
    ) -> PrintResult:
        """Send Marlin M413 power-loss recovery positioning G-code."""
        # -- parameter validation ------------------------------------------
        if z_height_mm <= 0:
            raise PrinterError(f"z_height_mm must be > 0, got {z_height_mm}.")
        if hotend_temp_c <= 0:
            raise PrinterError(f"Hotend temperature must be > 0\u00b0C, got {hotend_temp_c}\u00b0C.")
        self._validate_temp(hotend_temp_c, _MAX_HOTEND_TEMP, "Hotend")
        self._validate_temp(bed_temp_c, _MAX_BED_TEMP, "Bed")
        if prime_length_mm < 0:
            raise PrinterError(f"prime_length_mm must be >= 0, got {prime_length_mm}.")
        if z_clearance_mm <= 0 or z_clearance_mm > 10:
            raise PrinterError(f"z_clearance_mm must be > 0 and <= 10, got {z_clearance_mm}.")

        fan_pwm = int(fan_speed_pct * 2.55)
        commands = build_firmware_resume_positioning(
            z_height_mm=z_height_mm,
            hotend_temp_c=hotend_temp_c,
            bed_temp_c=bed_temp_c,
            fan_pwm=fan_pwm,
            flow_rate_pct=flow_rate_pct,
            prime_length_mm=prime_length_mm,
            z_clearance_mm=z_clearance_mm,
        )

        self.send_gcode(commands)

        layer_info = f" at layer {layer_number}" if layer_number is not None else ""
        return PrintResult(
            success=True,
            message=(
                f"Firmware resume positioning complete for {file_name}{layer_info}. "
                f"Z={z_height_mm}mm, hotend={hotend_temp_c}\u00b0C, bed={bed_temp_c}\u00b0C. "
                f"Printer is positioned and ready \u2014 start the resume file via start_print."
            ),
        )

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Filament handling — the shared G-code sequence
    # ------------------------------------------------------------------

    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then feed ``length_mm`` — the manual-load the firmware's own Extrude button performs."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_LOAD_LENGTH_MM),
            mechanism="serial_gcode",
        )

    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then retract ``length_mm`` to free the melt zone."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=-float(plan.length_mm or DEFAULT_UNLOAD_LENGTH_MM),
            mechanism="serial_gcode",
        )

    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Heat, then extrude ``length_mm`` — the clog test."""
        return self._gcode_filament_move(
            plan,
            signed_length_mm=float(plan.length_mm or DEFAULT_PURGE_LENGTH_MM),
            mechanism="serial_gcode",
        )

    def __repr__(self) -> str:
        return f"<SerialPrinterAdapter port={self._port!r}>"
