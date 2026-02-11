"""Abstract printer adapter interface for the Kiln project.

Every printer backend (OctoPrint, Klipper/Moonraker, Bambu, Prusa Connect,
etc.) must subclass :class:`PrinterAdapter` and implement every abstract
method so that the rest of the Kiln stack can interact with any printer
through a single, uniform API.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PrinterError(Exception):
    """Base exception for all printer-related errors.

    Adapter implementations should raise subclasses (or this class directly)
    whenever an operation fails in a way that the caller can reasonably
    handle -- e.g. connection timeouts, authentication failures, or
    unexpected responses from the printer firmware.
    """

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PrinterStatus(enum.Enum):
    """High-level operational state of a printer."""

    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"
    BUSY = "busy"
    CANCELLING = "cancelling"
    UNKNOWN = "unknown"


class DeviceType(enum.Enum):
    """Classification of physical fabrication devices."""
    FDM_PRINTER = "fdm_printer"
    SLA_PRINTER = "sla_printer"
    CNC_ROUTER = "cnc_router"
    LASER_CUTTER = "laser_cutter"
    GENERIC = "generic"


# ---------------------------------------------------------------------------
# Dataclasses -- structured return types
# ---------------------------------------------------------------------------

@dataclass
class PrinterState:
    """Snapshot of the printer's current state and temperatures."""

    connected: bool
    state: PrinterStatus
    tool_temp_actual: Optional[float] = None
    tool_temp_target: Optional[float] = None
    bed_temp_actual: Optional[float] = None
    bed_temp_target: Optional[float] = None
    chamber_temp_actual: Optional[float] = None
    chamber_temp_target: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`state` enum is converted to its string value so the
        result can be passed directly to ``json.dumps``.
        """
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass
class JobProgress:
    """Progress information for the currently active (or most recent) job."""

    file_name: Optional[str] = None
    completion: Optional[float] = None  # 0.0 -- 100.0
    print_time_seconds: Optional[int] = None
    print_time_left_seconds: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class PrinterFile:
    """Metadata for a single file stored on the printer / print server."""

    name: str
    path: str
    size_bytes: Optional[int] = None
    date: Optional[int] = None  # Unix timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class UploadResult:
    """Outcome of a file-upload operation."""

    success: bool
    file_name: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class PrintResult:
    """Outcome of a print-control operation (start / cancel / pause / resume)."""

    success: bool
    message: str
    job_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class PrinterCapabilities:
    """Declares what a specific adapter is able to do.

    Not every printer backend supports every operation.  Adapters override
    the defaults here to accurately describe their feature set.
    """

    can_upload: bool = True
    can_set_temp: bool = True
    can_send_gcode: bool = True
    can_pause: bool = True
    can_stream: bool = False
    can_probe_bed: bool = False
    can_update_firmware: bool = False
    can_snapshot: bool = False
    device_type: str = "fdm_printer"
    supported_extensions: Tuple[str, ...] = (".gcode", ".gco", ".g")

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`supported_extensions` tuple is converted to a list for
        JSON compatibility.
        """
        data = asdict(self)
        data["supported_extensions"] = list(self.supported_extensions)
        return data


@dataclass
class FirmwareComponent:
    """A single updatable software/firmware component."""

    name: str
    current_version: str
    remote_version: Optional[str] = None
    update_available: bool = False
    rollback_version: Optional[str] = None
    component_type: str = ""  # e.g. "git_repo", "system", "web"
    channel: str = ""  # e.g. "stable", "dev"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FirmwareStatus:
    """Firmware/software update status for a printer."""

    busy: bool = False
    components: List[FirmwareComponent] = field(default_factory=list)
    updates_available: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["components"] = [c.to_dict() for c in self.components]
        return data


@dataclass
class FirmwareUpdateResult:
    """Outcome of a firmware update or rollback operation."""

    success: bool
    message: str
    component: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class PrinterAdapter(ABC):
    """Abstract base for all printer backend adapters.

    Concrete subclasses must implement **every** abstract method and
    property listed below.  The Kiln orchestration layer relies on this
    contract to drive any supported printer without knowledge of the
    underlying protocol.

    Example minimal implementation::

        class MyPrinter(PrinterAdapter):

            @property
            def name(self) -> str:
                return "my-printer"

            @property
            def capabilities(self) -> PrinterCapabilities:
                return PrinterCapabilities()

            def get_state(self) -> PrinterState:
                ...

            # ... remaining abstract methods ...
    """

    # -- identity & feature discovery -----------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier for this adapter (e.g. ``"octoprint"``)."""

    @property
    @abstractmethod
    def capabilities(self) -> PrinterCapabilities:
        """Return the set of capabilities this adapter supports."""

    # -- state queries --------------------------------------------------

    @abstractmethod
    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures.

        Raises:
            PrinterError: If communication with the printer fails.
        """

    @abstractmethod
    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job.

        Raises:
            PrinterError: If communication with the printer fails.
        """

    @abstractmethod
    def list_files(self) -> List[PrinterFile]:
        """Return a list of files available on the printer / print server.

        Raises:
            PrinterError: If communication with the printer fails.
        """

    # -- file management ------------------------------------------------

    @abstractmethod
    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the printer.

        Args:
            file_path: Absolute or relative path to the local file.

        Raises:
            PrinterError: If the upload fails.
            FileNotFoundError: If *file_path* does not exist locally.
        """

    # -- print control --------------------------------------------------

    @abstractmethod
    def start_print(self, file_name: str) -> PrintResult:
        """Begin printing a file that already exists on the printer.

        Args:
            file_name: Name (or path) of the file as known by the printer.

        Raises:
            PrinterError: If the printer cannot start the job.
        """

    @abstractmethod
    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job.

        Raises:
            PrinterError: If the cancellation fails.
        """

    @abstractmethod
    def pause_print(self) -> PrintResult:
        """Pause the currently running print job.

        Raises:
            PrinterError: If the printer cannot pause.
        """

    @abstractmethod
    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job.

        Raises:
            PrinterError: If the printer cannot resume.
        """

    @abstractmethod
    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop on the printer.

        Sends a firmware-level halt (M112 or equivalent) that immediately
        cuts power to heaters and stepper motors.  Unlike
        :meth:`cancel_print`, this does **not** allow a graceful cooldown.

        Raises:
            PrinterError: If the e-stop command cannot be delivered.
        """

    # -- temperature control --------------------------------------------

    def _validate_temp(self, target: float, max_temp: float, heater: str) -> None:
        """Validate a temperature value before sending to the printer.

        Args:
            target: Desired temperature in Celsius.
            max_temp: Maximum safe temperature for this heater.
            heater: Human-readable heater name for error messages.

        Raises:
            PrinterError: If the temperature is out of safe range.
        """
        if target < 0:
            raise PrinterError(
                f"{heater} temperature {target}°C is negative -- must be >= 0."
            )
        if target > max_temp:
            raise PrinterError(
                f"{heater} temperature {target}°C exceeds safety limit ({max_temp}°C)."
            )

    @abstractmethod
    def set_tool_temp(self, target: float) -> bool:
        """Set the hot-end (tool) target temperature in degrees Celsius.

        Args:
            target: Desired temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted, ``False`` otherwise.

        Raises:
            PrinterError: If the command fails.
        """

    @abstractmethod
    def set_bed_temp(self, target: float) -> bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Args:
            target: Desired temperature.  Pass ``0`` to turn the heater off.

        Returns:
            ``True`` if the command was accepted, ``False`` otherwise.

        Raises:
            PrinterError: If the command fails.
        """

    # -- G-code ---------------------------------------------------------

    @abstractmethod
    def send_gcode(self, commands: List[str]) -> bool:
        """Send one or more G-code commands to the printer.

        Args:
            commands: List of G-code command strings, e.g.
                ``["G28", "G1 X10 Y10 Z5 F1200"]``.

        Returns:
            ``True`` if all commands were accepted.

        Raises:
            PrinterError: If sending fails.
        """

    # -- webcam snapshot (optional) ------------------------------------

    def get_snapshot(self) -> Optional[bytes]:
        """Capture a webcam snapshot from the printer.

        Returns raw JPEG/PNG image bytes, or ``None`` if webcam is not
        available or not supported by this adapter.  This is an optional
        method -- the default implementation returns ``None``.
        """
        return None

    # -- webcam streaming (optional) -----------------------------------

    def get_stream_url(self) -> Optional[str]:
        """Return the MJPEG stream URL for the printer's webcam.

        Returns the full URL to the live video stream, or ``None`` if
        streaming is not available.  This is an optional method -- the
        default implementation returns ``None``.
        """
        return None

    # -- firmware updates (optional) ------------------------------------

    def get_firmware_status(self) -> Optional["FirmwareStatus"]:
        """Check for available firmware/software updates.

        Returns a :class:`FirmwareStatus` describing each updatable
        component and whether updates are available, or ``None`` if
        firmware updates are not supported by this adapter.
        """
        return None

    def update_firmware(
        self,
        component: Optional[str] = None,
    ) -> "FirmwareUpdateResult":
        """Trigger a firmware or software update.

        Args:
            component: Specific component to update (e.g. ``"klipper"``,
                ``"moonraker"``, ``"system"``).  If ``None``, updates all
                available components.

        Returns:
            Result describing whether the update was accepted.

        Raises:
            PrinterError: If the printer is busy, printing, or the
                update cannot be started.
        """
        raise PrinterError(
            f"{self.name} adapter does not support firmware updates."
        )

    def rollback_firmware(self, component: str) -> "FirmwareUpdateResult":
        """Roll back a component to its previous version.

        Args:
            component: Component to roll back (required).

        Returns:
            Result describing whether the rollback was accepted.

        Raises:
            PrinterError: If rollback is not available or cannot be started.
        """
        raise PrinterError(
            f"{self.name} adapter does not support firmware rollback."
        )

    # -- bed mesh (optional) --------------------------------------------

    def get_bed_mesh(self) -> Optional[Dict[str, Any]]:
        """Return the current bed mesh / probe data.

        Returns a dict with mesh information (points, variance, etc.),
        or ``None`` if bed mesh data is not available.  This is an optional
        method -- the default implementation returns ``None``.
        """
        return None

    # -- CNC / laser operations (optional) --------------------------------

    def set_spindle_speed(self, rpm: float) -> bool:
        """Set CNC spindle speed.  Only for CNC-type devices."""
        raise PrinterError(f"{self.name} does not support spindle control")

    def set_laser_power(self, power_percent: float) -> bool:
        """Set laser power (0--100 %).  Only for laser-type devices."""
        raise PrinterError(f"{self.name} does not support laser control")

    def get_tool_position(self) -> Optional[Dict[str, float]]:
        """Return current tool position ``{x, y, z, ...}``.  Optional."""
        return None

    # -- file deletion --------------------------------------------------

    @abstractmethod
    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the printer's storage.

        Args:
            file_path: Path (or name) of the file as known by the printer.

        Returns:
            ``True`` if the file was deleted.

        Raises:
            PrinterError: If deletion fails.
        """

    # -- convenience / dunder helpers -----------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


# Forward-compatible alias for non-printing fabrication devices.
# PrinterAdapter remains the canonical name for backward compatibility.
DeviceAdapter = PrinterAdapter
