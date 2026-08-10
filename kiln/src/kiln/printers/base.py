"""Abstract printer adapter interface for the Kiln project.

Every printer backend (OctoPrint, Klipper/Moonraker, Bambu, Prusa Link,
etc.) must subclass :class:`PrinterAdapter` and implement every abstract
method so that the rest of the Kiln stack can interact with any printer
through a single, uniform API.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


def is_resume_mode_3mf(file_name: str) -> bool:
    """Return True if ``file_name`` looks like a mid-print resume 3MF.

    Resume 3MFs are produced by ``decorate_during_print`` and
    ``revert_mid_print``.  They strip Bambu's proprietary start-gcode
    (homing, bed probe, AMS load, calibration, M140/M190 pre-heat) and
    carry their own resume preamble that picks up where the paused
    print left off.

    Detection is filename-based for now (no in-3MF marker exists yet).
    The convention from kiln-pro's mid_print_engine is:

        ``transformed_resume_<sid>.3mf``  — user's modification applied
        ``original_resume_<sid>.3mf``     — unchanged remainder

    Both contain the substring ``_resume_`` (case-insensitive).  We
    also match files whose basename starts with ``transformed_resume``
    or ``original_resume`` for older sessions that didn't carry a sid.

    Lives here rather than in the server so both the tool layer (which
    relaxes its idle pre-flight for these) and the adapter layer (which
    must not count a resumed print as a second print) read one
    definition.
    """
    if not file_name:
        return False
    base = os.path.basename(str(file_name)).lower()
    if not base.endswith(".3mf"):
        return False
    if "_resume_" in base:
        return True
    return base.startswith(("transformed_resume", "original_resume"))


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

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
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
    tool_temp_actual: float | None = None
    tool_temp_target: float | None = None
    bed_temp_actual: float | None = None
    bed_temp_target: float | None = None
    chamber_temp_actual: float | None = None
    chamber_temp_target: float | None = None
    # Extended monitoring fields (populated by adapters that support them).
    cooling_fan_speed: int | None = None
    aux_fan_speed: int | None = None
    chamber_fan_speed: int | None = None
    heatbreak_fan_speed: int | None = None
    wifi_signal: str | None = None
    nozzle_diameter: str | None = None
    nozzle_type: str | None = None
    speed_profile: str | None = None
    speed_magnitude: int | None = None
    print_error: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`state` enum is converted to its string value so the
        result can be passed directly to ``json.dumps``.  Extended
        monitoring fields that are ``None`` are omitted for compactness.
        """
        data = asdict(self)
        data["state"] = self.state.value
        # Omit None extended fields.
        _EXTENDED = (
            "cooling_fan_speed", "aux_fan_speed", "chamber_fan_speed",
            "heatbreak_fan_speed", "wifi_signal", "nozzle_diameter",
            "nozzle_type", "speed_profile", "speed_magnitude", "print_error",
        )
        for key in _EXTENDED:
            if data.get(key) is None:
                data.pop(key, None)
        return data


@dataclass
class JobProgress:
    """Progress information for the currently active (or most recent) job."""

    file_name: str | None = None
    completion: float | None = None  # 0.0 -- 100.0
    print_time_seconds: int | None = None
    print_time_left_seconds: int | None = None
    # Extended layer tracking (populated by adapters that support it).
    current_layer: int | None = None
    total_layers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        Extended fields that are ``None`` are omitted for compactness.
        """
        data = asdict(self)
        for key in ("current_layer", "total_layers"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


@dataclass
class PrinterFile:
    """Metadata for a single file stored on the printer / print server."""

    name: str
    path: str
    size_bytes: int | None = None
    date: int | None = None  # Unix timestamp
    # G-code metadata fields (populated by gcode_metadata.enrich_printer_file)
    material: str | None = None
    estimated_time_seconds: int | None = None
    tool_temp: float | None = None
    bed_temp: float | None = None
    slicer: str | None = None
    layer_height: float | None = None
    filament_used_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        Omits metadata fields that are ``None`` to keep output compact
        when metadata has not been extracted.
        """
        data = asdict(self)
        # Strip None metadata fields for cleaner output
        _METADATA_KEYS = (
            "material",
            "estimated_time_seconds",
            "tool_temp",
            "bed_temp",
            "slicer",
            "layer_height",
            "filament_used_mm",
        )
        for key in _METADATA_KEYS:
            if data.get(key) is None:
                data.pop(key, None)
        return data


@dataclass
class UploadResult:
    """Outcome of a file-upload operation."""

    success: bool
    file_name: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class PrintResult:
    """Outcome of a print-control operation (start / cancel / pause / resume)."""

    success: bool
    message: str
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
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
    can_detect_filament: bool = False
    device_type: str = "fdm_printer"
    supported_extensions: tuple[str, ...] = (".gcode", ".gco", ".g")

    def to_dict(self) -> dict[str, Any]:
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
    remote_version: str | None = None
    update_available: bool = False
    rollback_version: str | None = None
    component_type: str = ""  # e.g. "git_repo", "system", "web"
    channel: str = ""  # e.g. "stable", "dev"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FirmwareStatus:
    """Firmware/software update status for a printer."""

    busy: bool = False
    components: list[FirmwareComponent] = field(default_factory=list)
    updates_available: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["components"] = [c.to_dict() for c in self.components]
        return data


@dataclass
class FirmwareUpdateResult:
    """Outcome of a firmware update or rollback operation."""

    success: bool
    message: str
    component: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_model_key(reported: str, *, vendor_prefix: str = "") -> str | None:
    """Map a device-reported model name to a ``printer_intelligence.json``
    key, or ``None`` when no canonical profile matches.

    One normalizer for every adapter that self-reports free-text model
    names (Elegoo SDCP, serial/Marlin ``MACHINE_TYPE``), so the spelling
    rules can't drift between them: lower-case, non-alphanumeric runs
    collapse to ``_``, and a lone ``_`` between a letter and a digit is
    dropped so ``"Ender-3 V2"`` lands on ``ender3_v2`` and
    ``"Neptune 4"`` (with ``vendor_prefix="elegoo_"``) lands on
    ``elegoo_neptune4`` — the way the canonical keys are spelled.

    Matching is strict membership against the intelligence profile list;
    a new model becomes mappable the moment its key is added there.
    """
    norm = re.sub(r"[^a-z0-9]+", "_", reported.lower()).strip("_")
    norm = re.sub(r"(?<=[a-z])_(?=\d)", "", norm)
    if not norm:
        return None
    candidates = [norm]
    if vendor_prefix and not norm.startswith(vendor_prefix):
        candidates.insert(0, f"{vendor_prefix}{norm}")
    try:
        from kiln.printer_intelligence import list_intel_profiles

        profiles = set(list_intel_profiles())
    except Exception:  # noqa: BLE001 — intelligence lookup is optional
        return None
    for candidate in candidates:
        if candidate in profiles:
            return candidate
    return None


@dataclass(frozen=True)
class PrinterInfo:
    """A printer's self-reported identity, for telemetry and display.

    ``model`` is Kiln's canonical model key (``"bambu_a1"``,
    ``"prusa_mk4"``, ...) when the self-report maps to one, otherwise
    the device's own model string verbatim — still exact grain, just
    not a key ``printer_intelligence.json`` knows yet.  ``raw_model``
    preserves what the device actually said before normalization, and
    ``source`` names the channel it said it through (``"mqtt"``,
    ``"http"``, ``"serial_prefix"``, ``"config"``).

    Never carries serial numbers, hostnames, or addresses — instances
    flow into the telemetry heartbeat and community aggregation, which
    are model-grain by design.
    """

    model: str | None = None
    raw_model: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
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

    # -- safety profile --------------------------------------------------

    _safety_profile_id: str | None = None

    # ------------------------------------------------------------------
    # Safety interposition: wrap every concrete subclass's upload_file
    # with a bed-fit + homing-sequence pre-check.  This ensures no code
    # path — MCP tool, marketplace download, CLI, pipeline — can push
    # an unsafe file to the printer's filesystem.  Incident #0
    # (2026-04-15) showed that gating only the MCP upload_file tool
    # leaves download_and_upload / slice_and_print / CLI paths open.
    # See kiln/printers/bed_fit.py for the validation logic.
    # ------------------------------------------------------------------
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("upload_file")
        if original is not None and not getattr(original, "_kiln_safety_wrapped", False):
            import functools

            @functools.wraps(original)
            def _safe_upload_file(self, file_path: str):
                try:
                    _preflight_upload_or_raise(self, file_path)
                except _UnsafeUpload as exc:
                    # Raise PrinterError so callers get a consistent exception
                    # type across adapters.
                    from kiln.printers import PrinterError
                    raise PrinterError(str(exc)) from None
                return original(self, file_path)

            _safe_upload_file._kiln_safety_wrapped = True  # type: ignore[attr-defined]
            cls.upload_file = _safe_upload_file

        # ------------------------------------------------------------------
        # Outcome-lifecycle interposition: wrap every concrete subclass's
        # get_state so EVERY adapter — not just the one with push wiring —
        # observes state transitions, resolves pending outcome rows on the
        # first status after (re)connect, and records watched endings.
        # Before this, six of seven adapters opened a pending row at print
        # start that nothing ever resolved: the loop stayed honest (rows
        # sat 'pending', excluded from the math) but learned nothing.
        # Same engine-not-instance shape as the upload_file safety wrap:
        # a new adapter inherits the wiring without knowing it exists.
        # ------------------------------------------------------------------
        state_original = cls.__dict__.get("get_state")
        if state_original is not None and not getattr(
            state_original, "_kiln_outcome_wrapped", False
        ):
            import functools

            @functools.wraps(state_original)
            def _observed_get_state(self):
                state = state_original(self)
                try:
                    _feed_outcome_lifecycle(self, state)
                except Exception:  # noqa: BLE001 — bookkeeping never breaks status
                    import logging as _logging

                    _logging.getLogger(__name__).debug(
                        "outcome lifecycle feed failed", exc_info=True
                    )
                return state

            _observed_get_state._kiln_outcome_wrapped = True  # type: ignore[attr-defined]
            cls.get_state = _observed_get_state

    def set_safety_profile(self, profile_id: str) -> None:
        """Bind a printer safety profile for temperature validation.

        When set, :meth:`_validate_temp` will use the profile's limits
        instead of the caller-supplied default.

        Args:
            profile_id: Profile identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
        """
        self._safety_profile_id = profile_id

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
    def list_files(self) -> list[PrinterFile]:
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

    def start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Begin printing a file that already exists on the printer.

        TEMPLATE METHOD — adapters must NOT override this.  It runs the
        universal pre-print impossibility gate (build-volume + hotend-temp
        ceilings) that no entry point — MCP tool, scheduler, CLI, recovery —
        can bypass, then delegates to the adapter's :meth:`_start_print_impl`,
        and counts the print for local usage stats on the way out.

        Counting here is deliberate: this is the single point every entry
        point and every adapter passes through, so all backends are counted
        the same way.  The previous signal — an agent remembering to call
        ``record_print_outcome`` — reported prints for whichever adapter had
        the auto-record hook wired and zero for the other seven.

        The gate soft-passes whenever fit/temperature can't be determined, so
        it never false-blocks a valid print; it refuses only prints that are
        *certain* to fail or damage hardware, and only a human-confirmed
        ``force_print_oversize`` grant can override it.

        Args:
            file_name: Name (or path) of the file as known by the printer.
            **kwargs: Adapter-specific print parameters (e.g. Bambu AMS
                settings).  Adapters that don't support extra parameters
                silently ignore them.

        Raises:
            PrinterError: If the printer cannot start the job.
        """
        try:
            from kiln.printers.print_gate import run_adapter_gate

            blocked = run_adapter_gate(self, file_name, kwargs)
        except Exception:  # noqa: BLE001 — a gate failure must never block a print
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "pre-print gate raised; allowing print", exc_info=True
            )
            blocked = None
        if blocked is not None:
            hint = blocked.get("override_hint", "")
            reason = blocked.get("reason", "Print blocked by the pre-print safety gate.")
            return PrintResult(
                success=False,
                message=reason + (" " + hint if hint else ""),
            )
        result = self._start_print_impl(file_name, **kwargs)
        if getattr(result, "success", False) and not is_resume_mode_3mf(file_name):
            # A resume 3MF continues the print that's already running (a
            # mid-print swap), so it isn't a new print to count.
            try:
                from kiln.daily_stats import record_print_start

                record_print_start(self.name, file_name)
            except Exception:  # noqa: BLE001 — stats must never affect a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "print-start stat recording failed", exc_info=True
                )
            # Nozzle wear counts at START — every print wears the nozzle,
            # success or failure, and an end-hook only sees the prints
            # something watched to completion.  No-op without kiln-pro.
            try:
                from kiln._pro_nozzle_bridge import record_print_odometer

                record_print_odometer(self.name, file_name)
            except Exception:  # noqa: BLE001 — wear bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "nozzle odometer recording failed", exc_info=True
                )
            # Open the outcome row NOW, while we can still see the print.
            # The start is the one event Kiln is guaranteed to witness (it
            # initiates it); if no process is alive when the print ends,
            # this pending row is what lets the next session notice the
            # print existed and settle how it went, instead of the print
            # vanishing from history entirely.
            try:
                from kiln.auto_record_hook import open_pending_outcome

                # The material Kiln COMMANDED at start is the strongest
                # honest source — it survives even when the outcome is
                # settled days later, when today's loaded spool is no
                # longer evidence.  Adapter kwargs carry it under either
                # generic key; absent both, the record-time backfill
                # (job metadata, live AMS on watched endings) covers it.
                commanded_material = kwargs.get("material_type") or kwargs.get("material")
                open_pending_outcome(
                    self.name,
                    file_name,
                    material_type=(
                        str(commanded_material) if commanded_material else None
                    ),
                )
            except Exception:  # noqa: BLE001 — bookkeeping must never affect a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "pending-outcome open failed", exc_info=True
                )
        return result

    @abstractmethod
    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Adapter-specific print start, called AFTER the pre-print gate passes.

        Each adapter puts its real start logic here (M23/M24 over serial,
        FTPS + MQTT for Bambu, REST for OctoPrint/Moonraker/PrusaLink, etc.).
        Never call this directly — callers use :meth:`start_print`, which
        gates first.

        Args:
            file_name: Name (or path) of the file as known by the printer.
            **kwargs: Adapter-specific print parameters.

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

    def resume_print(self) -> PrintResult:
        """Resume a previously paused print job.

        TEMPLATE METHOD — adapters must NOT override this; they implement
        :meth:`_resume_print_impl` instead.  It first checks the live printer
        state and refuses to claim success when there is no paused print to
        resume.  "Resume" only continues a *currently-paused* print, so firing
        it on an idle printer (e.g. after a power loss) or a running one is at
        best a firmware no-op, often a cryptic firmware error ("Print is not
        paused, resume aborted"), and on fire-and-forget transports (Bambu
        MQTT, serial M24) a FALSE ``"Print resumed."`` success.  Either way the
        user is misled.

        We block only on a CONFIDENT not-paused state (idle / printing).
        Uncertain states (offline, busy, error, unknown) and any state-read
        failure fail OPEN — delegate to :meth:`_resume_print_impl` and let the
        real resume surface its own result — so a transient read never blocks
        a legitimate resume.

        Raises:
            PrinterError: If the printer cannot resume.
        """
        try:
            status = self.get_state().state
        except Exception:  # noqa: BLE001 — never block a real resume on a state-read error
            status = None
        if status in (PrinterStatus.IDLE, PrinterStatus.PRINTING):
            return self._no_paused_print_result()
        return self._resume_print_impl()

    @abstractmethod
    def _resume_print_impl(self) -> PrintResult:
        """Adapter-specific resume, called AFTER the not-paused gate passes.

        Each adapter puts its real resume logic here (MQTT for Bambu, REST for
        OctoPrint/Moonraker/PrusaLink, SDCP for Elegoo, M24 over serial).
        Never call this directly — callers use :meth:`resume_print`, which
        gates first.

        Raises:
            PrinterError: If the printer cannot resume.
        """

    def _no_paused_print_result(self) -> PrintResult:
        """The honest result when there is no paused print to resume.

        Shared by the :meth:`resume_print` template and any adapter that must
        gate resume on a different signal — e.g. the serial adapter, which
        tracks pause via a local flag that ``get_state()`` can't reliably
        surface.  Keep the wording here so there is one source of truth.
        """
        return PrintResult(
            success=False,
            message=(
                "No paused print to resume — the printer isn't paused. "
                "Resume only continues a print that's currently paused; to "
                "pick up a print that stopped or lost power, use Kiln's print "
                "recovery instead of resume."
            ),
        )

    @abstractmethod
    def emergency_stop(self) -> PrintResult:
        """Perform an immediate emergency stop on the printer.

        Sends a firmware-level halt (M112 or equivalent) that immediately
        cuts power to heaters and stepper motors.  Unlike
        :meth:`cancel_print`, this does **not** allow a graceful cooldown.

        Raises:
            PrinterError: If the e-stop command cannot be delivered.
        """

    # -- calibration -----------------------------------------------------

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run printer calibration routines (bed leveling, Z offset, etc.).

        Calibration capabilities vary by printer.  Subclasses that support
        remote calibration should override this method.  The default
        implementation returns a failure indicating no support.

        Args:
            options: Which calibration routines to run.  Valid values are
                printer-specific but common ones include:

                * ``"bed_leveling"`` — auto bed mesh / Z offset
                * ``"vibration"`` — input shaper / vibration compensation
                * ``"flow"`` — extrusion flow calibration
                * ``"all"`` — run all available routines

                When ``None``, defaults to ``["bed_leveling"]``.

        Returns:
            PrintResult indicating success or failure.
        """
        return PrintResult(
            success=False,
            message=(
                "Calibration is not supported for this printer type. "
                "Run calibration manually from the printer's touchscreen or web UI."
            ),
        )

    # -- temperature control --------------------------------------------

    def _validate_temp(self, target: float, max_temp: float, heater: str) -> None:
        """Validate a temperature value before sending to the printer.

        When a safety profile is bound via :meth:`set_safety_profile`, the
        profile's limit overrides *max_temp* for defense-in-depth.

        Args:
            target: Desired temperature in Celsius.
            max_temp: Maximum safe temperature for this heater (fallback).
            heater: Human-readable heater name for error messages.

        Raises:
            PrinterError: If the temperature is out of safe range.
        """
        # Use per-printer profile limits when available (defense-in-depth).
        if self._safety_profile_id:
            try:
                from kiln.safety_profiles import get_profile  # noqa: E402

                profile = get_profile(self._safety_profile_id)
                lower_heater = heater.lower()
                if lower_heater in ("hotend", "tool"):
                    max_temp = min(max_temp, profile.max_hotend_temp)
                elif lower_heater == "bed":
                    max_temp = min(max_temp, profile.max_bed_temp)
            except (KeyError, ImportError):
                pass  # fall back to caller-supplied max_temp

        if target < 0:
            raise PrinterError(f"{heater} temperature {target}°C is negative -- must be >= 0.")
        if target > max_temp:
            raise PrinterError(f"{heater} temperature {target}°C exceeds safety limit ({max_temp}°C).")

    # -- fan control ------------------------------------------------------

    #: Aliases accepted for the single generic default part-cooling fan.
    #: Unlike Bambu's fixed part/aux/chamber layout (a protocol Bambu itself
    #: controls end-to-end), generic Marlin/Klipper firmware has no
    #: standardized auxiliary or chamber fan -- a machine may have neither,
    #: or expose one only through a printer-specific macro Kiln has no way
    #: to discover automatically.  So a generic ``set_fan`` supports ONLY
    #: this one fan; adapters reject anything else rather than guess.
    _PART_COOLING_FAN_ALIASES: frozenset[str] = frozenset({"part", "part_cooling", "cooling"})

    def _validate_part_fan(self, node: str, percent: int) -> int:
        """Validate a generic-adapter ``set_fan`` call; return the 0-255 PWM.

        Only the part-cooling fan (:data:`_PART_COOLING_FAN_ALIASES`) is
        accepted -- see the class attribute for why auxiliary/chamber names
        can't be supported generically.

        Raises:
            PrinterError: If *node* isn't the part-cooling fan, or *percent*
                is outside 0-100.
        """
        key = node.strip().lower()
        if key not in self._PART_COOLING_FAN_ALIASES:
            raise PrinterError(
                f"Fan node {node!r} isn't supported here. This printer only "
                "exposes a single default part-cooling fan (node='part') -- "
                "unlike Bambu, there's no standard auxiliary or chamber fan "
                "command Kiln can send without knowing your machine's own "
                "G-code macros."
            )
        try:
            pct = int(percent)
        except (TypeError, ValueError) as exc:
            raise PrinterError(f"set_fan: percent must be an integer 0-100 ({exc}).") from exc
        if not 0 <= pct <= 100:
            raise PrinterError(f"set_fan: percent must be 0-100, got {pct}.")
        return round(pct / 100 * 255)

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
    def send_gcode(self, commands: list[str]) -> bool:
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

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from the printer.

        Returns raw JPEG/PNG image bytes, or ``None`` if webcam is not
        available or not supported by this adapter.  This is an optional
        method -- the default implementation returns ``None``.
        """
        return None

    # -- webcam streaming (optional) -----------------------------------

    def get_stream_url(self) -> str | None:
        """Return the MJPEG stream URL for the printer's webcam.

        Returns the full URL to the live video stream, or ``None`` if
        streaming is not available.  This is an optional method -- the
        default implementation returns ``None``.
        """
        return None

    # -- printer identity self-report (optional) ------------------------

    def get_printer_info(self) -> "PrinterInfo | None":
        """Return the printer's self-reported model, or ``None``.

        Adapters whose protocol carries a model identity (Bambu MQTT,
        PrusaLink HTTP, Elegoo SDCP) override this so installs that
        never set ``printer_model`` in config.yaml still report exact
        hardware to the telemetry heartbeat instead of adapter-family
        grain.  The default returns ``None``, which every caller treats
        as "not reported" and falls through to its config path.

        SAFETY BOUNDARY: this is a telemetry/display report, never a
        behavior input.  Safety ceilings, temperature clamps, and
        bed-fit decisions key off the config-declared model
        (``printer_model`` in config.yaml, read live by
        ``printer_model_resolver``) — a self-report must never
        override that declaration where the two disagree; it may fill
        in only where config is silent.  That split is why
        printer-model *inference* was scrapped for safety use
        (commit a19e665b): a wrong guess silently applies wrong
        limits.  A wrong telemetry row, by contrast, is just a wrong
        row.  Implementations must therefore not write their probe
        result into any attribute that behavior reads (e.g. Bambu's
        ``_printer_model``, which selects AMS interpretation), and
        must not report build volume or temperature data here.

        Implementations should also stay cheap and bounded: prefer
        cached protocol state where the transport already carries it,
        keep any fresh probe to a single short request, and fail fast
        to ``None`` when the printer is unreachable — callers treat
        this as best-effort and must keep working without it.
        """
        return None

    # -- firmware updates (optional) ------------------------------------

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Check for available firmware/software updates.

        Returns a :class:`FirmwareStatus` describing each updatable
        component and whether updates are available, or ``None`` if
        firmware updates are not supported by this adapter.
        """
        return None

    def update_firmware(
        self,
        component: str | None = None,
    ) -> FirmwareUpdateResult:
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
        raise PrinterError(f"{self.name} adapter does not support firmware updates.")

    def rollback_firmware(self, component: str) -> FirmwareUpdateResult:
        """Roll back a component to its previous version.

        Args:
            component: Component to roll back (required).

        Returns:
            Result describing whether the rollback was accepted.

        Raises:
            PrinterError: If rollback is not available or cannot be started.
        """
        raise PrinterError(f"{self.name} adapter does not support firmware rollback.")

    # -- bed mesh (optional) --------------------------------------------

    def get_bed_mesh(self) -> dict[str, Any] | None:
        """Return the current bed mesh / probe data.

        Returns a dict with mesh information (points, variance, etc.),
        or ``None`` if bed mesh data is not available.  This is an optional
        method -- the default implementation returns ``None``.
        """
        return None

    # -- filament sensor (optional) ----------------------------------------

    def get_filament_status(self) -> dict[str, Any] | None:
        """Query the filament runout sensor status.

        Returns a dict with sensor information (e.g. ``{"detected": True,
        "sensor_enabled": True}``), or ``None`` if no filament sensor is
        available.  This is an optional method -- the default implementation
        returns ``None``.
        """
        return None

    # -- CNC / laser operations (optional) --------------------------------

    def set_spindle_speed(self, rpm: float) -> bool:
        """Set CNC spindle speed.  Only for CNC-type devices."""
        raise PrinterError(f"{self.name} does not support spindle control")

    def set_laser_power(self, power_percent: float) -> bool:
        """Set laser power (0--100 %).  Only for laser-type devices."""
        raise PrinterError(f"{self.name} does not support laser control")

    def get_tool_position(self) -> dict[str, float] | None:
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

    # -- async wrappers (hot-path methods) --------------------------------

    async def async_get_state(self) -> PrinterState:
        """Async wrapper for :meth:`get_state` via :func:`asyncio.to_thread`."""
        return await asyncio.to_thread(self.get_state)

    async def async_start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Async wrapper for :meth:`start_print` via :func:`asyncio.to_thread`."""
        return await asyncio.to_thread(self.start_print, file_name, **kwargs)

    async def async_cancel_print(self) -> PrintResult:
        """Async wrapper for :meth:`cancel_print` via :func:`asyncio.to_thread`."""
        return await asyncio.to_thread(self.cancel_print)

    async def async_get_job_status(self) -> JobProgress:
        """Async wrapper for :meth:`get_job` via :func:`asyncio.to_thread`."""
        return await asyncio.to_thread(self.get_job)

    async def async_get_temperatures(self) -> PrinterState:
        """Async wrapper returning temperature data from :meth:`get_state`.

        Returns the full :class:`PrinterState` (which includes all temperature
        fields) without an HTTP round-trip beyond what :meth:`get_state` already
        does.
        """
        return await asyncio.to_thread(self.get_state)

    # -- convenience / dunder helpers -----------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


# Forward-compatible alias for non-printing fabrication devices.
# PrinterAdapter remains the canonical name for backward compatibility.
DeviceAdapter = PrinterAdapter


# ---------------------------------------------------------------------------
# Outcome-lifecycle feed (called from the get_state wrap installed in
# PrinterAdapter.__init_subclass__)
# ---------------------------------------------------------------------------


def _current_job_label(adapter: PrinterAdapter) -> str | None:
    """Best-effort name of the job the printer is (or was last) running.

    Used only on the rare paths that need identity — a terminal
    transition or the once-per-process reconcile — never on every poll:
    ``get_job()`` may cost a network round trip on some adapters.
    """
    try:
        job = adapter.get_job()
        label = getattr(job, "file_name", None)
        return str(label) if label else None
    except Exception:  # noqa: BLE001 — identity is optional, status is not
        return None


def _feed_outcome_lifecycle(adapter: PrinterAdapter, state: PrinterState) -> None:
    """Feed one ``get_state()`` result into the print-outcome lifecycle.

    This is what makes outcome capture ADAPTER-GENERIC: every adapter's
    normalized status stream — polled by the scheduler, the status
    tools, monitoring — drives the same three moves the Bambu push path
    performs natively:

    1. once per process, reconcile pending rows against the first
       status the printer reports (a terminal state still naming the
       job settles it; merely idle resolves to ``unknown``, never
       success);
    2. observe the state so the NEXT call sees the transition;
    3. on an active→terminal edge, record the watched ending (idle
       after watched printing = success; error = failed; a cancel in
       flight = cancelled) — but only when the job has a name to
       attribute it to; an unnamed ending stays pending for the
       reconcile/user path rather than being guessed onto a row.

    Adapters with their own push wiring (Bambu MQTT) keep it — both
    layers resolve only rows that are still pending and dedupe per
    (printer, job), so whichever sees the ending first wins and the
    other no-ops.
    """
    status = getattr(state, "state", None)
    value = getattr(status, "value", "") or ""
    if not value:
        return

    from kiln.auto_record_hook import (
        fire_terminal_state_hook,
        is_terminal_transition,
        observe_state,
        reconcile_pending_outcomes,
    )

    name = adapter.name
    if not getattr(adapter, "_base_outcomes_reconciled", False):
        adapter._base_outcomes_reconciled = True  # type: ignore[attr-defined]
        # Only pay for job identity (get_job may be a network round trip)
        # when there is actually a pending row to settle.
        from kiln.persistence import get_db

        if get_db().list_print_outcomes(
            printer_name=name, outcome="pending", limit=1,
        ):
            reconcile_pending_outcomes(
                printer_name=name,
                gcode_state=value,
                current_job_label=_current_job_label(adapter),
            )

    prev = observe_state(name, value)
    # Job identity may cost a network round trip — pay it only for an
    # edge that could actually record something.
    if is_terminal_transition(prev, value):
        label = _current_job_label(adapter)
        if label:
            fire_terminal_state_hook(
                prev_state=prev,
                new_state=value,
                print_error_code=0,
                printer_name=name,
                job_id=label,
                file_name=label,
            )


# ---------------------------------------------------------------------------
# Pre-upload safety check (called from PrinterAdapter.__init_subclass__)
# ---------------------------------------------------------------------------


class _UnsafeUpload(Exception):
    """Internal sentinel raised by the pre-upload safety check."""


def _preflight_upload_or_raise(adapter: PrinterAdapter, file_path: str) -> None:
    """Run bed-fit + homing validation on a local file before it hits
    any adapter's upload_file.  Raises :class:`_UnsafeUpload` on hard
    failures (OFF_BED_GEOMETRY / EXCEEDS_BED / NO_HOMING_SEQUENCE).

    Soft-passes on unknown printer, unknown bbox, or any internal
    exception — we'd rather allow a print on an obscure printer than
    block it based on incomplete data.  All upstream gates
    (slice_model, slice_and_print, MCP upload_file) still run their
    own checks; this is defence in depth, not the only line.
    """
    import os
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in (".gcode", ".gco", ".g", ".3mf") and not file_path.lower().endswith(".gcode.3mf"):
            return  # only validate printable files; STL/OBJ uploads skip
        # Resolve printer_id from the adapter.  Most adapters expose
        # _safety_profile_id or adapter.name — prefer explicit profile.
        printer_id = getattr(adapter, "_safety_profile_id", None)
        if not printer_id:
            # Use the live resolver (config.yaml → serial inference → env)
            with contextlib.suppress(Exception):
                from kiln.printer_model_resolver import resolve_printer_model
                printer_id = resolve_printer_model()
        if not printer_id:
            # Last-ditch fallback to the frozen module global
            with contextlib.suppress(Exception):
                import kiln.server as _srv
                printer_id = getattr(_srv, "_PRINTER_MODEL", None)
        if not printer_id:
            return  # unknown printer — soft-pass
        from kiln.printers.bed_fit import (
            validate_3mf_for_printer,
            validate_gcode_for_printer,
        )
        if ext in (".gcode", ".gco", ".g"):
            result = validate_gcode_for_printer(file_path, printer_id)
        else:
            result = validate_3mf_for_printer(file_path, printer_id)
        if not result.get("ok", True):
            code = result.get("error_code")
            if code in ("OFF_BED_GEOMETRY", "EXCEEDS_BED", "NO_HOMING_SEQUENCE"):
                raise _UnsafeUpload(
                    f"Upload refused ({code}): "
                    f"{result.get('error_message', 'unsafe file')}. "
                    f"This would have been the incident #0 class of crash."
                )
    except _UnsafeUpload:
        raise
    except Exception:
        # Any other error — don't block the upload, just skip the check.
        return
