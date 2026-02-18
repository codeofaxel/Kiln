"""USB/serial printer adapter for the Kiln printer abstraction layer.

Implements :class:`~kiln.printers.base.PrinterAdapter` by communicating
directly with Marlin/RepRap firmware over a USB serial connection using
the standard G-code command/response protocol.

Supports any Marlin-based printer connected via USB -- Ender 3, Prusa MK3,
CR-10, and similar FDM machines.  Requires the ``pyserial`` package
(``pip install pyserial``).

The adapter is thread-safe: a :class:`threading.Lock` serialises all
access to the underlying serial port so that concurrent MCP tool calls
cannot interleave G-code commands.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from typing import Any

from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
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

# Maximum safe temperatures (fallback when no safety profile is bound).
_MAX_HOTEND_TEMP: float = 300.0
_MAX_BED_TEMP: float = 130.0

# Number of reconnect attempts on connection loss.
_MAX_RECONNECT_ATTEMPTS: int = 3

# Regex for parsing M105 temperature responses.
# Matches patterns like "T:210.0 /210.0 B:60.0 /60.0" and variants.
_TEMP_RE = re.compile(r"T:(?P<tool_actual>[\d.]+)\s*/(?P<tool_target>[\d.]+)")
_BED_TEMP_RE = re.compile(r"B:(?P<bed_actual>[\d.]+)\s*/(?P<bed_target>[\d.]+)")

# Regex for parsing M27 SD print progress.
# Matches "SD printing byte 1234/5678" or "Not SD printing"
_SD_PROGRESS_RE = re.compile(r"SD printing byte\s+(?P<current>\d+)\s*/\s*(?P<total>\d+)")

# Regex for parsing M115 firmware info.
_FIRMWARE_RE = re.compile(r"FIRMWARE_NAME:(?P<name>[^\s]+)")
_FIRMWARE_VER_RE = re.compile(r"FIRMWARE_VERSION:(?P<version>[^\s]+)")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SerialPrinterAdapter(PrinterAdapter):
    """Concrete :class:`PrinterAdapter` backed by a USB serial connection.

    Communicates with Marlin/RepRap firmware via the standard G-code
    command/response protocol over a serial port.

    Args:
        port: Serial port path, e.g. ``"/dev/ttyUSB0"``, ``"/dev/ttyACM0"``,
            or ``"COM3"`` on Windows.
        baudrate: Serial baud rate (default 115200, standard for most Marlin
            printers).
        timeout: Default read timeout in seconds for G-code responses.
        printer_name: Human-readable name for this adapter instance.

    Raises:
        PrinterError: If ``pyserial`` is not installed or the serial port
            cannot be opened.

    Example::

        adapter = SerialPrinterAdapter("/dev/ttyUSB0")
        state = adapter.get_state()
        print(state.state, state.tool_temp_actual)
    """

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

        # Track pause state (Marlin M27 doesn't distinguish paused from printing).
        self._paused: bool = False

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
            can_upload=True,
            can_set_temp=True,
            can_send_gcode=True,
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
        """Open the serial port and wait for the printer's startup message.

        Raises:
            PrinterError: If the port cannot be opened or the printer does
                not respond within the timeout period.
        """
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

        # Wait for printer startup message (Marlin sends "start" or "echo:"
        # lines followed by an "ok" after reset).
        self._wait_for_startup()
        self._connected = True
        logger.info("Connected to serial printer on %s @ %d baud", self._port, self._baudrate)

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
        """Read lines until we see 'start' or 'ok' from the printer.

        Marlin printers send startup text after a serial connection or reset.
        We consume these lines to clear the buffer and confirm communication.
        """
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
        # If we didn't get the expected startup, still proceed -- some
        # printers don't send "start" text.
        logger.debug("No explicit startup message received; proceeding anyway")

    def _ensure_connected(self) -> None:
        """Verify the serial connection is alive; attempt reconnect if not.

        Raises:
            PrinterError: If reconnection fails after all attempts.
        """
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
        """Send a single G-code command and collect the response.

        Thread-safe: acquires the serial lock before writing.

        Args:
            command: G-code command string (e.g. ``"M105"``).
            timeout: Read timeout override in seconds.
            wait_for_ok: Whether to wait for an ``"ok"`` response line.
                Set to ``False`` for emergency commands like M112.

        Returns:
            The full response text (all lines concatenated).

        Raises:
            PrinterError: On serial errors, timeouts, or firmware error
                responses.
        """
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
        """Send command while already holding the lock.

        This exists so that callers who already hold ``_lock`` (e.g.
        ``upload_file``) can send multiple commands without releasing and
        re-acquiring the lock between each one.
        """
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
                response_lines.append(line)

                lower = line.lower()
                if lower.startswith("ok"):
                    return "\n".join(response_lines)
                if lower.startswith("error:") or lower.startswith("error"):
                    raise PrinterError(f"Firmware error for '{command}': {line}")
        finally:
            self._serial.timeout = old_timeout

        # Timeout exhausted without "ok".
        raise PrinterError(
            f"Timeout ({timeout}s) waiting for response to '{command}'. Received so far: {' | '.join(response_lines)}"
        )

    def _send_and_parse(self, command: str) -> dict[str, Any]:
        """Send a G-code command and parse key:value pairs from the response.

        Useful for commands like M105 that return ``T:210.0 /210.0 B:60.0 /60.0``.

        Returns:
            A dict of parsed key-value pairs from the response text.
        """
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
        """Parse M105 temperature response into a structured dict.

        Handles formats like::

            ok T:210.0 /210.0 B:60.0 /60.0
            T:210.0 /210.0 B:60.0 /60.0 @:127 B@:127

        Returns:
            Dict with keys ``tool_actual``, ``tool_target``, ``bed_actual``,
            ``bed_target`` (values are ``None`` when not present).
        """
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
        """Retrieve the current printer state and temperatures.

        Queries ``M105`` for temperatures and ``M27`` for SD print progress
        to determine the operational state.

        Raises:
            PrinterError: On communication errors (after reconnect attempts).
        """
        if not self.is_connected:
            return PrinterState(
                connected=False,
                state=PrinterStatus.OFFLINE,
            )

        try:
            temp_response = self._send_command("M105")
        except PrinterError as exc:
            # Distinguish unreachable (OFFLINE) from firmware error (ERROR).
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

        # Check if actively printing via SD card.
        status = PrinterStatus.IDLE
        try:
            sd_response = self._send_command("M27")
            sd_match = _SD_PROGRESS_RE.search(sd_response)
            if sd_match:
                current = int(sd_match.group("current"))
                total = int(sd_match.group("total"))
                if total > 0 and current < total:
                    # Marlin M27 doesn't distinguish paused from printing;
                    # use our internal _paused flag.
                    status = PrinterStatus.PAUSED if self._paused else PrinterStatus.PRINTING
                elif total > 0 and current >= total:
                    # Print complete.
                    status = PrinterStatus.IDLE
                    self._paused = False
            elif "not sd printing" in sd_response.lower():
                status = PrinterStatus.IDLE
                self._paused = False
        except PrinterError:
            # M27 failure is not fatal -- we just can't determine print status.
            logger.debug("M27 query failed; defaulting to IDLE", exc_info=True)

        return PrinterState(
            connected=True,
            state=status,
            tool_temp_actual=temps.get("tool_actual"),
            tool_temp_target=temps.get("tool_target"),
            bed_temp_actual=temps.get("bed_actual"),
            bed_temp_target=temps.get("bed_target"),
        )

    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active SD print job.

        Queries ``M27`` for SD card print progress.

        Raises:
            PrinterError: On communication errors.
        """
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
        """Return a list of files on the SD card.

        Sends ``M20`` to list SD card files.  Marlin responds with::

            Begin file list
            BENCHY.GCO
            CALIBRA~1.GCO
            End file list
            ok

        Raises:
            PrinterError: On communication errors or if no SD card is present.
        """
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
        """Parse the M20 file listing response.

        Extracts file names between "Begin file list" and "End file list"
        markers.
        """
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

            # Marlin may include file size: "BENCHY.GCO 12345"
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
        """Upload a local G-code file to the printer's SD card.

        Uses Marlin's SD write protocol: ``M28 filename`` to start writing,
        send each line of G-code, then ``M29`` to stop writing.

        Args:
            file_path: Path to the local G-code file.

        Raises:
            PrinterError: On communication errors.
            FileNotFoundError: If *file_path* does not exist locally.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Local file not found: {abs_path}")

        filename = os.path.basename(abs_path).upper()
        # Marlin SD card filenames are 8.3 format; truncate if needed.
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
                        # Send G-code line without waiting for individual "ok"
                        # responses -- Marlin buffers SD writes.
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
        """Delete a G-code file from the SD card.

        Sends ``M30 filename`` to delete the file.

        Args:
            file_path: SD card file name to delete.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """
        self._send_command(f"M30 {file_path}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- print control
    # ------------------------------------------------------------------

    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file from the SD card.

        Sends ``M23 filename`` (select) then ``M24`` (start).

        Args:
            file_name: Name of the file on the SD card.

        Raises:
            PrinterError: If the printer cannot start the job.
        """
        self._send_command(f"M23 {file_name}")
        self._send_command("M24")
        self._current_file = file_name
        return PrintResult(
            success=True,
            message=f"Started printing {file_name} from SD card.",
        )

    def cancel_print(self) -> PrintResult:
        """Cancel the currently running SD print.

        Sends ``M524`` (abort SD print).  Falls back to ``M0`` if the
        printer does not support M524.

        Raises:
            PrinterError: If the cancellation fails.
        """
        try:
            self._send_command("M524")
        except PrinterError:
            # M524 not supported on all firmware -- fall back to M0.
            logger.debug("M524 not supported; falling back to M0")
            self._send_command("M0")

        self._current_file = None
        self._paused = False
        return PrintResult(
            success=True,
            message="Print cancelled.",
        )

    def pause_print(self) -> PrintResult:
        """Pause the currently running SD print.

        Sends ``M25`` (pause SD print).

        Raises:
            PrinterError: If the printer cannot pause.
        """
        self._send_command("M25")
        self._paused = True
        return PrintResult(
            success=True,
            message="Print paused.",
        )

    def resume_print(self) -> PrintResult:
        """Resume a previously paused SD print.

        Sends ``M24`` (resume SD print).

        Raises:
            PrinterError: If the printer cannot resume.
        """
        self._send_command("M24")
        self._paused = False
        return PrintResult(
            success=True,
            message="Print resumed.",
        )

    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop.

        Sends ``M112`` (emergency stop) which immediately kills heaters
        and stepper motors at the firmware level.  Does **not** wait for
        an ``ok`` response because the printer halts immediately.

        Raises:
            PrinterError: If the M112 command cannot be delivered.
        """
        m112_sent = False
        try:
            self._send_command("M112", wait_for_ok=False)
            m112_sent = True
        except PrinterError as exc:
            # M112 is fire-and-forget: even if the response read fails, the
            # write may have succeeded.  Only flag a real write failure.
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

    def set_tool_temp(self, target: float) -> bool:
        """Set the hotend target temperature in degrees Celsius.

        Sends ``M104 S{target}`` (set hotend temp, non-blocking).

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails or temperature is out of range.
        """
        self._validate_temp(target, _MAX_HOTEND_TEMP, "Hotend")
        self._send_command(f"M104 S{int(target)}")
        return True

    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Sends ``M140 S{target}`` (set bed temp, non-blocking).

        Args:
            target: Target temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted.

        Raises:
            PrinterError: If the command fails or temperature is out of range.
        """
        self._validate_temp(target, _MAX_BED_TEMP, "Bed")
        self._send_command(f"M140 S{int(target)}")
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- G-code
    # ------------------------------------------------------------------

    def send_gcode(self, commands: list[str]) -> bool:
        """Send one or more G-code commands to the printer.

        Args:
            commands: List of G-code command strings.

        Returns:
            ``True`` if all commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """
        for cmd in commands:
            self._send_command(cmd)
        return True

    # ------------------------------------------------------------------
    # PrinterAdapter -- firmware info (optional)
    # ------------------------------------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Query firmware version via ``M115``.

        Returns a :class:`FirmwareStatus` with the firmware name and version,
        or ``None`` if the query fails.
        """
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
    # Tool position (optional -- Marlin M114)
    # ------------------------------------------------------------------

    def get_tool_position(self) -> dict[str, float] | None:
        """Return current tool position via ``M114``.

        Marlin responds with something like::

            X:10.00 Y:20.00 Z:5.00 E:0.00 Count X:800 Y:1600 Z:4000

        Returns:
            Dict with ``x``, ``y``, ``z``, ``e`` keys, or ``None`` if the
            query fails.
        """
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
    # Firmware resume print (Marlin M413 power-loss recovery)
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
        """Send Marlin M413 power-loss recovery positioning G-code.

        Generates and sends a G-code sequence that disables Marlin's built-in
        power-loss recovery, homes X/Y (never Z -- the nozzle would crash into
        the part), heats the bed and hotend, sets Z position without moving,
        raises the nozzle by *z_clearance_mm*, primes the nozzle, and
        configures fan speed and flow rate.

        After this method returns, the printer is positioned and ready.  The
        caller is responsible for starting the actual print file via
        :meth:`start_print`.

        :param z_height_mm: Z height to resume from, in millimetres (> 0).
        :param hotend_temp_c: Hotend target temperature in Celsius (> 0, <= 300).
        :param bed_temp_c: Bed target temperature in Celsius (>= 0, <= 130).
        :param file_name: Original file name (included in the result message).
        :param layer_number: Optional layer number for the result message.
        :param fan_speed_pct: Part-cooling fan speed as 0--100 % (default 100).
        :param flow_rate_pct: Flow rate multiplier as a percentage (default 100).
        :param prime_length_mm: Extrusion length to prime the nozzle (>= 0).
        :param z_clearance_mm: Distance to raise nozzle above the part (> 0, <= 10).

        :returns: :class:`PrintResult` indicating success.
        :raises PrinterError: If any parameter is out of range or if G-code
            delivery fails.
        """
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

        # -- build G-code sequence -----------------------------------------
        fan_pwm = int(fan_speed_pct * 2.55)
        commands = [
            "M413 S0",  # Disable Marlin power-loss recovery
            "G28 X Y",  # Home X/Y only (NEVER Z)
            f"M140 S{bed_temp_c}",  # Start heating bed (non-blocking)
            f"M104 S{hotend_temp_c}",  # Start heating hotend (non-blocking)
            f"M190 S{bed_temp_c}",  # Wait for bed temp
            f"M109 S{hotend_temp_c}",  # Wait for hotend temp
            "G92 E0",  # Reset extruder position
            f"G92 Z{z_height_mm}",  # Set Z position without moving
            "G91",  # Relative positioning
            f"G1 Z{z_clearance_mm} F300",  # Raise nozzle above part
            "G90",  # Absolute positioning
            f"G1 E{prime_length_mm} F200",  # Prime nozzle
            "G92 E0",  # Reset extruder again
            f"M106 S{fan_pwm}",  # Set fan speed (0-255)
            f"M221 S{int(flow_rate_pct)}",  # Set flow rate multiplier
        ]

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

    def __repr__(self) -> str:
        return f"<SerialPrinterAdapter port={self._port!r}>"
