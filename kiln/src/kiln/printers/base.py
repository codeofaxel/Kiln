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
    """High-level operational state of a printer.

    This answers "what is the machine doing right now", and nothing else.
    A printer that has just finished a print is doing nothing, so it is
    :attr:`IDLE` — exactly as ready for the next job as one that has been
    sitting cold all week.  How the *last job* ended is a different
    question with a different answer: see :class:`JobResult`.
    """

    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"
    BUSY = "busy"
    CANCELLING = "cancelling"
    UNKNOWN = "unknown"


class JobResult(enum.Enum):
    """How the most recent print job ENDED, as the firmware reports it.

    Deliberately a separate axis from :class:`PrinterStatus` rather than
    extra members on it.  Every adapter used to fold "the print finished"
    into ``IDLE`` (Bambu ``finish``, Moonraker ``complete``, Prusa Link
    ``FINISHED``, Marlin's M27 at 100 %), and folded a *cancel* into the
    same value, so a completed print, a cancelled print and a printer
    nobody had touched all reported the identical thing.  A user watching
    a print run to 100 % was told the printer was ``idle``.

    Widening :class:`PrinterStatus` instead would have fixed the report by
    breaking the machine: ``IDLE`` is load-bearing as "ready to print" in
    the pre-print gate, the CLI preflight, ``registry.get_idle_printers``
    and the fleet routers — several of which compare the raw string, where
    no type checker can see them.  A printer that just finished IS ready,
    so it must keep reading ``IDLE``.  This field adds the missing fact
    without moving the one every gate already depends on.

    ``None`` means "no information", which is the honest answer for a
    printer that is mid-print, and for a protocol whose polled status
    carries no completion signal at all (OctoPrint's state flags, RRF's
    object model).  ``None`` is never a claim that a job ended well.
    """

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


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


# A reading older than this is no longer evidence about now.  Adapters that
# query the printer on every call are current by construction; adapters that
# answer from a push cache (Bambu, over MQTT) are only as current as the last
# push they were sent, and a push cache that stops advancing keeps answering
# confidently.  One minute is the point past which a cached print state is
# reported as stale rather than presented as the present tense.
#
# Bambu's own cooldown ceiling reads this constant rather than restating the
# number, so the two cannot drift into disagreeing about when a cache stops
# being trustworthy.
STALE_STATE_WARN_AGE: float = 60.0


def describe_stale_state(
    state_age_seconds: float | None,
    state_label: str,
    max_age: float = STALE_STATE_WARN_AGE,
) -> str | None:
    """One sentence naming a reading's age, or ``None`` when it is fresh.

    The single implementation behind :meth:`PrinterState.staleness_note` and
    every reporting surface.  It takes plain values rather than a state object
    so a caller holding the serialised form -- ``state.to_dict()``, a relayed
    payload, a duck-typed adapter's shim -- reports staleness identically
    instead of writing its own sentence, which is how two surfaces end up
    disagreeing about the same reading.

    ``None`` age means the adapter does not measure it, which is not evidence
    of staleness: adapters that query the printer on every call are current by
    construction, and warning about them would make the signal noise.
    """
    if state_age_seconds is None or state_age_seconds <= max_age:
        return None
    return (
        f"Telemetry is {state_age_seconds:.0f}s old — the printer has not "
        f"reported since, so {str(state_label).upper()} describes then, not "
        f"now. Verify against the machine before acting."
    )


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
    # How long ago the printer last reported the value in :attr:`state`,
    # in seconds.  ``None`` means the adapter does not measure it -- absence
    # of an age is not a claim of freshness, and it is the honest answer for
    # a transport that asks the printer on every call.  An adapter answering
    # from a push cache sets it, because "the last thing the printer said"
    # and "what the printer is doing right now" are not the same sentence.
    state_age_seconds: float | None = None
    # How the most recent job ENDED, when the printer says so — the axis
    # :attr:`state` cannot carry, because a finished printer and an
    # untouched one are both genuinely idle.  ``None`` means the printer
    # is not reporting an ended job (it is mid-print, or its protocol has
    # no completion signal); it never means "ended fine".
    last_job_result: JobResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`state` and :attr:`last_job_result` enums are converted
        to their string values so the result can be passed directly to
        ``json.dumps``.  Extended monitoring fields that are ``None`` are
        omitted for compactness.
        """
        data = asdict(self)
        data["state"] = self.state.value
        if self.last_job_result is not None:
            data["last_job_result"] = self.last_job_result.value
        # Omit None extended fields.
        _EXTENDED = (
            "cooling_fan_speed", "aux_fan_speed", "chamber_fan_speed",
            "heatbreak_fan_speed", "wifi_signal", "nozzle_diameter",
            "nozzle_type", "speed_profile", "speed_magnitude", "print_error",
            "state_age_seconds", "last_job_result",
        )
        for key in _EXTENDED:
            if data.get(key) is None:
                data.pop(key, None)
        return data

    def is_stale(self, max_age: float = STALE_STATE_WARN_AGE) -> bool:
        """Whether :attr:`state` is older than *max_age* seconds.

        ``False`` when the adapter reports no age: a missing measurement
        is not evidence of staleness, and treating it as stale would put a
        warning on every polling adapter's output.
        """
        return self.state_age_seconds is not None and self.state_age_seconds > max_age

    def staleness_note(self, max_age: float = STALE_STATE_WARN_AGE) -> str | None:
        """One sentence naming this reading's age, or ``None`` when fresh.

        Every surface that reports printer state in prose leads with this when
        it is not ``None``.  It exists because the failure it names is silent
        otherwise: a frozen push cache answers "printing" in exactly the tone
        it would use for a live reading, and a confidently wrong answer costs
        more than an error.  The state itself is never rewritten -- callers
        downstream read the enum to decide whether a printer is busy, and
        demoting a stale PRINTING to UNKNOWN would let a second concurrent
        print start.
        """
        return describe_stale_state(self.state_age_seconds, self.state.value, max_age)


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
class IdentityConflict:
    """Two or more sources disagree about what a printer is.

    ``claims`` maps a source label to the model it asserts — the
    config-declared model appears as ``"config"``, and each adapter
    identity channel under its own name (``"serial_prefix"``,
    ``"firmware_product_name"``, ``"m115_machine_type"``, ...).

    A conflict is diagnostic gold: either the config is stale (printer
    replaced, model corrected) or one of Kiln's identity tables is
    wrong.  The second is what made printer-model inference unsafe in
    2026-04, and it stayed invisible for months because a disagreement
    could only be expressed by reporting nothing at all.
    """

    claims: dict[str, str]

    @property
    def models(self) -> list[str]:
        """The distinct models being claimed, order-stable."""
        seen: list[str] = []
        for model in self.claims.values():
            if model not in seen:
                seen.append(model)
        return seen

    def describe(self) -> str:
        """One line a human can act on."""
        parts = ", ".join(f"{src} says {model}" for src, model in self.claims.items())
        return (
            f"Printer identity is ambiguous — {parts}. "
            "Set printer_model in ~/.kiln/config.yaml to the correct value; "
            "if it is already correct, this means one of Kiln's identity "
            "tables is wrong and should be reported."
        )

    def to_dict(self) -> dict[str, Any]:
        return {"claims": dict(self.claims), "models": self.models,
                "summary": self.describe()}


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
            #
            # Stamp the elapsed clock here, for the same reason the pending
            # outcome row opens here: this is the one moment Kiln is
            # guaranteed to witness, because it is the one Kiln causes.  An
            # adapter that cannot measure elapsed any other way reads this
            # instead of extrapolating one from a percentage.
            try:
                from kiln.printers.progress_motion import note_job_start

                note_job_start(self, file_name)
            except Exception:  # noqa: BLE001 — bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "job-start stamp failed", exc_info=True
                )
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
                from kiln.auto_record_hook import (
                    clear_cancel_intent,
                    open_pending_outcome,
                )

                # A cancel asked for before this print has nothing to say
                # about this print.  Dropping it HERE is what lets the intent
                # outlive a slow stop sequence safely: the mechanism no longer
                # has to guess how many seconds a printer takes to stop
                # moving, retract, park and report idle, because the event
                # that guess was standing in for is this one, exactly.
                clear_cancel_intent(outcome_printer_name(self))

                # The material Kiln COMMANDED at start is the strongest
                # honest source — it survives even when the outcome is
                # settled days later, when today's loaded spool is no
                # longer evidence.  Adapter kwargs carry it under either
                # generic key; absent both, the record-time backfill
                # (job metadata, live AMS on watched endings) covers it.
                commanded_material = kwargs.get("material_type") or kwargs.get("material")
                # Under the name every RESOLVER looks it up by.  self.name
                # is the backend family — identical for every printer of a
                # brand — while both reconcile doors and save_print_outcome's
                # pending-row adoption key on outcome_printer_name.  Opened
                # under the family name, the row could never be found again:
                # each print left one more forever-pending row and its real
                # ending was inserted as a second row beside it.
                open_pending_outcome(
                    outcome_printer_name(self),
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

    def resume_print(self, *, force: bool = False) -> PrintResult:
        """Resume a previously paused print job, and CHECK that it took.

        TEMPLATE METHOD — adapters must NOT override this; they implement
        :meth:`_resume_print_impl` instead.  Two halves:

        **The gate.**  "Resume" only continues a *currently-paused* print, so
        firing it on an idle printer (e.g. after a power loss) or a running one
        is at best a firmware no-op, often a cryptic firmware error, and on
        fire-and-forget transports (Bambu MQTT, serial M24) a FALSE
        ``"Print resumed."``.  So a confident not-paused state refuses.

        But "confident" has to mean something.  On 2026-08-11 a Bambu A1 sat
        frozen at layer 2 for twenty minutes while ``gcode_state`` said
        ``RUNNING`` with two-second-fresh telemetry, and this gate read that
        word, called it PRINTING, and refused the user's second resume — so
        the lie did not merely misreport the print, it **disabled the recovery
        path**.  The gate now asks the machine whether it is actually MOVING
        (:mod:`kiln.printers.progress_motion`) before it treats ``PRINTING`` as
        grounds to refuse anybody.  Observed motion refuses; observed stall
        does not; and where Kiln cannot tell, it refuses but names the way
        through, because a user staring at a paused screen must never be left
        without one.

        *force* skips the gate entirely.  It exists so the answer to "Kiln is
        wrong about my printer" is one argument rather than a dead end.  It
        cannot make the result dishonest: the read-back below reports what
        actually happened either way.

        **The read-back.**  ``_resume_print_impl`` on a fire-and-forget
        transport returns success because the *command was published*, which is
        not the same sentence as "the print resumed".  So the state is re-read
        afterwards: a printer still reporting PAUSED turns that success into an
        honest failure.  Bounded, early-exiting, and wrapped — a verification
        step may never become a way for a resume to fail.

        Honest bound, stated because it is the whole point: the read-back
        confirms the printer's *state word* changed, and the state word is
        exactly what lied that night.  It catches a resume the firmware
        silently rejected; it cannot catch a resume the firmware accepts and
        then does nothing about.  That second failure is what the stall
        detector is for, which is why the success message declines to claim
        the print is progressing and says how to find out.

        Raises:
            PrinterError: If the printer cannot resume.
        """
        if not force:
            refusal = self._not_paused_refusal()
            if refusal is not None:
                return refusal
        return self._verify_resume_took(self._resume_print_impl())

    def _not_paused_refusal(self) -> PrintResult | None:
        """The refusal to return before resuming, or ``None`` to go ahead.

        Fails OPEN on anything uncertain — an unreadable state, an offline or
        busy or unknown printer, or any exception in here at all — because a
        transient read must never stand between a user and their own print.
        """
        try:
            state = self.get_state()
            status = getattr(state, "state", None)
        except Exception:  # noqa: BLE001 — never block a real resume on a read error
            return None

        if status is PrinterStatus.IDLE:
            # Nothing is running to continue, and no progress signal could
            # change that.
            return self._no_paused_print_result()

        if status is not PrinterStatus.PRINTING:
            return None

        # PRINTING is the word that lied.  Do not act on it alone.
        try:
            from kiln.printers.progress_motion import Motion, observe_progress

            verdict = observe_progress(self, state, self._job_or_none())
        except Exception:  # noqa: BLE001 — the detector never blocks a resume
            return None

        if verdict.motion is Motion.MOVING:
            # Positive evidence: a progress axis advanced.  This really is a
            # running print, and resume is not the verb for it.
            return self._no_paused_print_result()

        if verdict.motion is Motion.STALLED:
            # The state word is contradicted by the machine's own counters.
            # Refusing here is what cost twenty minutes.
            return None

        return self._unverified_running_result()

    def _job_or_none(self) -> JobProgress | None:
        """``get_job()``, or ``None`` if it fails.

        Called only on the rare, user-initiated resume path — never per poll —
        so the round trip some adapters pay for it is bought once, at the
        moment its answer decides whether a user can recover their print.
        """
        try:
            return self.get_job()
        except Exception:  # noqa: BLE001 — progress detail is optional here
            return None

    #: How long :meth:`_verify_resume_took` will wait for the printer to stop
    #: reporting PAUSED, and how often it looks.  Short and early-exiting: a
    #: resume typically confirms on the first or second look, and this is a
    #: rare user-initiated action, not a polling loop.
    _RESUME_VERIFY_TIMEOUT: float = 5.0
    _RESUME_VERIFY_INTERVAL: float = 1.0

    def _verify_resume_took(self, result: PrintResult) -> PrintResult:
        """Re-read the printer and correct *result* if the resume did not take.

        NEVER converts a failure into a success, never raises, and returns
        *result* untouched on any problem of its own.
        """
        if not getattr(result, "success", False):
            return result
        try:
            import time as _time

            deadline = _time.monotonic() + self._RESUME_VERIFY_TIMEOUT
            status = None
            while True:
                status = getattr(self.get_state(), "state", None)
                if status is not PrinterStatus.PAUSED:
                    break
                if _time.monotonic() >= deadline:
                    break
                _time.sleep(self._RESUME_VERIFY_INTERVAL)

            if status is PrinterStatus.PAUSED:
                return PrintResult(
                    success=False,
                    message=(
                        "Resume was sent but the printer still reports paused "
                        f"{self._RESUME_VERIFY_TIMEOUT:.0f}s later — the "
                        "command was not accepted. Check the printer's screen "
                        "for a prompt it is waiting on (filament, door, a "
                        "confirmation), then try again."
                    ),
                    job_id=getattr(result, "job_id", None),
                )
            if status is PrinterStatus.PRINTING:
                return PrintResult(
                    success=True,
                    message=(
                        "Resume accepted — the printer now reports printing. "
                        "That is the printer's word, not yet observed motion: "
                        "check that the layer number climbs over the next few "
                        "minutes, and Kiln will say so if it does not."
                    ),
                    job_id=getattr(result, "job_id", None),
                )
            return result
        except Exception:  # noqa: BLE001 — verification never breaks a resume
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "resume verification failed; returning the adapter's own result",
                exc_info=True,
            )
            return result

    def _unverified_running_result(self) -> PrintResult:
        """Refusal for a printer that says PRINTING with no motion evidence.

        Distinct wording from :meth:`_no_paused_print_result` on purpose.  That
        one is a statement of fact — the printer is demonstrably running.  This
        one is a statement about what Kiln can and cannot see, and it must not
        dead-end: the state word alone has been wrong before, so the user gets
        told how to overrule it in the same breath they are refused.
        """
        return PrintResult(
            success=False,
            message=(
                "The printer reports that it is printing, so there is nothing "
                "to resume — but Kiln has not seen it advance a layer or a "
                "percent yet, so it cannot confirm that. If the printer's own "
                "screen says paused, the reported state is wrong: use "
                "resume_print(force=True) to send the resume anyway."
            ),
        )

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

    def get_printer_info(self) -> PrinterInfo | None:
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

        An adapter with more than one identity channel returns ``None``
        when its channels disagree — naming a model on a coin flip is
        the 2026-04 failure.  Expose the individual channels via
        :meth:`get_identity_channels` so the disagreement stays
        diagnosable instead of vanishing into this ``None``.
        """
        return None

    def get_identity_channels(self) -> dict[str, str]:
        """Every identity channel this adapter has, and what each claims.

        Maps a channel label to the model it resolves to, e.g.
        ``{"serial_prefix": "bambu_a1", "firmware_product_name":
        "bambu_a1"}``.  Channels that resolve to nothing are omitted;
        the default is an empty dict for adapters with no self-report.

        This exists so a disagreement BETWEEN channels stays visible.
        :meth:`get_printer_info` collapses a disagreement to ``None``
        (correctly — it must not guess), which on its own is
        indistinguishable from "the printer didn't answer".  Diagnostics
        read this instead and can say which channel claims what.

        Like the probe, this may cost a bounded network round-trip, so
        it belongs in diagnostics rather than polling loops.
        """
        return {}

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


def delegate_outcome_lifecycle(backend: PrinterAdapter) -> None:
    """Mark ``backend`` as an inner adapter its owner reports on behalf of.

    An adapter that fulfils the protocol by holding ANOTHER adapter — today
    only :class:`~kiln.printers.creality.CrealityAdapter`, which speaks to a
    Moonraker backend — has two wrapped ``get_state`` methods on one call:
    the inner one runs first, then the outer.  Both would feed the lifecycle,
    and the hook's idempotency key is ``(adapter.name, job_id)``, so the two
    names ("creality", "moonraker") do not dedupe each other and one print
    lands twice.

    The OUTER adapter is the one that reports, because its name is the one
    the user registered and the one every other surface attributes the print
    to.  Call this on the backend at the seam where the delegation is built,
    so the next delegating adapter inherits the fix by using the same helper
    rather than growing a second opinion about it.
    """
    backend._kiln_outcome_delegated = True  # type: ignore[attr-defined]


def name_printer_for_outcomes(adapter: Any, registered_name: str) -> None:
    """Tell an adapter the name its owner registered it under.

    Called from :meth:`~kiln.registry.PrinterRegistry.register`, the only
    place that knows it.  An adapter cannot work it out for itself:
    ``adapter.name`` is the BACKEND FAMILY — ``"bambu"`` for every Bambu ever
    plugged in, and the same story for the other seven — while the registry
    holds the name its owner chose, which is what every other surface
    attributes prints to.

    Best-effort by design.  An adapter that refuses attributes still works;
    its outcomes are simply filed under the family name, exactly as before.
    """
    try:
        adapter._kiln_registered_name = str(registered_name)
    except Exception:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "could not name adapter for outcomes", exc_info=True
        )


def outcome_printer_name(adapter: Any) -> str:
    """The name this printer's outcomes, transitions and cancels are filed under.

    ONE answer for every part of the lifecycle that keys on a printer: the
    previous-state table that detects a terminal transition, the idempotency
    ledger that stops one ending being recorded twice, the cancel-intent table
    that tells a cancel from a finish, and the ``printer_name`` written onto
    the outcome row.  They have to agree, because they are the same question.

    The family name was standing in for this, and it collides.  Two Bambus on
    one bench were ONE machine to all four: the same file printed on both
    recorded a single outcome, because the second ending read as a replay of
    the first under the shared key.

    It also quietly unpicked the cancel path.  ``cancel_print`` files intent
    under the registry name and the hook consumed it under the family name, so
    the two never met and a print the user cancelled through Kiln's own tool
    was recorded as a success — on every install, single-printer benches
    included.  :func:`~kiln.printers.progress_motion.observation_key` refused
    this same trade for the motion samples and its docstring names the hazard;
    the lifecycle never got the same treatment.

    Falls back to the family name for an adapter no registry ever saw — one
    built directly in a test or a script — which is what it reported before
    and is still a better thing to file under than nothing.
    """
    registered = getattr(adapter, "_kiln_registered_name", None)
    if isinstance(registered, str) and registered:
        return registered
    return getattr(adapter, "name", "") or "printer"


def _current_job(adapter: PrinterAdapter) -> JobProgress | None:
    """The job the printer is (or was last) running, or ``None``.

    Used only on the rare paths that need it — a terminal transition or the
    once-per-process reconcile — never on every poll: ``get_job()`` may cost
    a network round trip on some adapters.  One call serves every question
    asked at the transition (identity AND elapsed), so noticing an ending
    still costs exactly one round trip.
    """
    try:
        return adapter.get_job()
    except Exception:  # noqa: BLE001 — identity is optional, status is not
        return None


def _job_label(job: JobProgress | None) -> str | None:
    """Best-effort name of ``job``, or ``None`` when it has none."""
    label = getattr(job, "file_name", None) if job is not None else None
    return str(label) if label else None


def _current_job_label(adapter: PrinterAdapter) -> str | None:
    """Best-effort name of the job the printer is (or was last) running."""
    return _job_label(_current_job(adapter))


#: Longest single print whose duration is credible enough to bank.
#:
#: Not a limit on what a printer may do — it is an absurdity floor under a
#: number nothing downstream can sanity-check.  The longest real prints run a
#: few days; a week means a clock artifact or a counter that is measuring
#: something other than this job, and one such reading would outweigh every
#: honest print in the daily total.
_MAX_CREDIBLE_PRINT_HOURS: float = 168.0


def _record_watched_duration(
    *,
    job_label: str,
    elapsed_seconds: Any,
    state_age_seconds: float | None,
    observation_gap_seconds: float | None,
) -> None:
    """Bank this print's duration — but only if Kiln really WATCHED it end.

    ``print_hours`` means the printer was RUNNING, not that parts shipped: a
    print cancelled at ten minutes really did run for ten minutes, and this
    records it as such.  The outcome lives beside it on the print's own row,
    so "successful hours" stays a derivation and nobody can quote this total
    as parts-shipped.

    Everything here is about refusing to guess.  The elapsed number is read
    when Kiln NOTICES the ending, which is not when the print ended, and the
    two adapter families fail in opposite directions:

    * a printer-reported duration (Moonraker, OctoPrint, PrusaLink, Duet,
      Elegoo) freezes at the ending, so a late read is merely late;
    * Bambu's is a Kiln-side stopwatch (:func:`note_job_start` at print start,
      subtracted here) that NOTHING stops on its own, so a late read keeps
      counting: a print that ended at 31 minutes and is noticed an hour later
      reads ~91.  Monotonic and plausible, so it would never look wrong — it
      would just quietly inflate every Bambu install's total.

    One rule covers both, and it is the rule the design asks for: a duration
    is recorded only when this ending was WATCHED — the last time we had
    current knowledge of this printer was recent, and the reading itself is
    not a stale cache.  Anything else is finding out afterwards, and an honest
    absence beats a confident wrong number.  ``prints - prints_hours_known``
    is what makes that absence visible instead of reading as zero hours
    printed.

    TWO DOORS reach an ending, and this is the only place the rule lives.
    Each measures *observation_gap_seconds* — how long since we last had
    current knowledge of this printer — the only way it honestly can:

    * the ``get_state`` wrap asks, so its gap is :func:`note_status_read`'s
      "how long since we last asked", and ``state_age_seconds`` is what
      catches an answer served from a cache rather than the machine;
    * Bambu's MQTT callback is TOLD, so its gap is the age of the run state it
      held before this frame — the same quantity ``state_age_seconds`` carries
      above, read one frame earlier.

    The push door is fenced off from the two cheaper answers, and both fences
    were measured rather than reasoned about.  It cannot borrow the look-clock:
    a Bambu ``get_state()`` is answered from the push cache, so a monitor
    polling through an MQTT outage keeps that clock warm while nothing is being
    watched at all.  And it cannot ask merely when the printer last SPOKE,
    because partial frames — a temperature, a fan step — carry no run state,
    so one landing between a reconnect and the full dump would present an
    hour-old ending as a one-second-old one.  Either mistake lets the reconnect
    dump, whose ``prev`` predates the outage, sail through this guard carrying
    the whole outage in its elapsed.

    All three are the same quantity, so the thresholds below apply unchanged to
    either door, and neither decides for itself what counts as watched.

    A printer with no clock to report (direct USB: M27 gives SD-card byte
    progress, not time) falls out at the first check and stays honestly
    unknown, as :file:`scripts/adapter_conformance.yaml` already declares.

    Never raises — this runs inside a status read and inside an MQTT callback.
    """
    if not isinstance(elapsed_seconds, (int, float)) or elapsed_seconds <= 0:
        return

    from kiln.printers.progress_motion import WATCHED_ENDING_MAX_GAP_S

    # Was our last current knowledge recent enough for "we saw it end" to be
    # true?  Unknown — a first read, or a printer that has never spoken to
    # this process — counts as no: it never watched anything.
    if (
        observation_gap_seconds is None
        or observation_gap_seconds > WATCHED_ENDING_MAX_GAP_S
    ):
        return

    # And is the reading itself the present tense?  A push-cache answer that
    # is minutes old dates the transition we just "saw", by exactly the same
    # amount and for the same reason.  Absent age means the caller learned
    # this from the printer on this call, which is current by construction.
    if (
        isinstance(state_age_seconds, (int, float))
        and state_age_seconds > STALE_STATE_WARN_AGE
    ):
        return

    hours = float(elapsed_seconds) / 3600.0
    if hours > _MAX_CREDIBLE_PRINT_HOURS:
        return

    from kiln.daily_stats import record_print_hours_for_job

    # Keyed by job so the two layers that can both witness one ending — the
    # adapter-generic wrap and Bambu's own push wiring — cannot bank it twice.
    #
    # *job_label* must be the SAME string its caller hands
    # ``fire_terminal_state_hook`` as ``job_id``.  That is the whole dedupe
    # contract: ``record_print_hours_for_job`` keys on the job id alone, and
    # the other writer in the system — ``record_print_outcome``, banking from
    # the job record when an agent later refines an auto-recorded outcome —
    # keys on the hook's ``job_id``.  Bank under a second spelling of the same
    # print and nothing collapses them; the hours row and the outcome row also
    # stop naming the same job.
    record_print_hours_for_job(job_label, hours)


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

    The token this feeds the loop is :attr:`PrinterState.last_job_result`
    when the printer named one, and the operational status otherwise.
    That ordering is the whole point: the loop's own vocabulary already
    distinguishes a finish from a cancel, but until the adapters carried
    the distinction, every ending arrived here as the single word
    ``"idle"`` — which :func:`_infer_outcome` resolves to ``success``.  A
    print cancelled anywhere except Kiln's own ``cancel_print`` tool was
    therefore recorded as a success, and a finished print nobody watched
    could only ever reconcile to ``unknown``, because the machine's
    testimony had been flattened before the loop could read it.
    """
    # An adapter that delegates to another adapter would otherwise feed the
    # loop twice per call, under two names that do not dedupe each other.
    if getattr(adapter, "_kiln_outcome_delegated", False):
        return

    status = getattr(state, "state", None)
    # The job's ending outranks the machine's current state: "completed"
    # and "cancelled" are facts about the print, and both live inside the
    # same IDLE the printer reports afterwards.
    result = getattr(state, "last_job_result", None)
    value = getattr(result, "value", None) or getattr(status, "value", "") or ""
    if not value:
        return

    from kiln.auto_record_hook import (
        fire_terminal_state_hook,
        is_terminal_transition,
        observe_state,
        reconcile_pending_outcomes,
    )
    from kiln.printers.progress_motion import forget_job_start, note_status_read

    # Stamp the look on EVERY status read — it is the only record of how
    # long ago we last saw this printer, and a duration is only honest if
    # that gap is short.  Dict touch, no round trip.
    read_gap_seconds = note_status_read(adapter)

    # The name its owner registered, not the backend family — see
    # outcome_printer_name.  Everything below keys on this: the transition
    # table, the idempotency ledger, the cancel intent, the outcome row.
    name = outcome_printer_name(adapter)
    if not getattr(adapter, "_base_outcomes_reconciled", False):
        adapter._base_outcomes_reconciled = True  # type: ignore[attr-defined]
        # Only pay for job identity (get_job may be a network round trip)
        # when there is actually a pending row to settle.
        from kiln.persistence import get_db

        # The family name is where rows opened before the identity fix
        # live; the gate must see them or the sweep never even fires.
        family = getattr(adapter, "name", "") or ""
        has_pending = bool(
            get_db().list_print_outcomes(
                printer_name=name, outcome="pending", limit=1,
            )
        ) or (
            family != name
            and bool(
                get_db().list_print_outcomes(
                    printer_name=family, outcome="pending", limit=1,
                )
            )
        )
        if has_pending:
            reconcile_pending_outcomes(
                printer_name=name,
                gcode_state=value,
                current_job_label=_current_job_label(adapter),
                legacy_printer_name=family or None,
            )

    prev = observe_state(name, value)
    # Job identity may cost a network round trip — pay it only for an
    # edge that could actually record something.
    if is_terminal_transition(prev, value):
        job = _current_job(adapter)
        label = _job_label(job)
        if label:
            fire_terminal_state_hook(
                prev_state=prev,
                new_state=value,
                print_error_code=0,
                printer_name=name,
                job_id=label,
                file_name=label,
            )
            _record_watched_duration(
                # The id this door just gave the hook, so the hours row and
                # the outcome row name one job.
                job_label=label,
                elapsed_seconds=getattr(job, "print_time_seconds", None),
                state_age_seconds=getattr(state, "state_age_seconds", None),
                observation_gap_seconds=read_gap_seconds,
            )
        # Stop the elapsed clock: the job it was measuring is over.  This is
        # the first caller ``forget_job_start`` has ever had, and without it
        # the stamp outlives its print — so a NEXT print started from the
        # touchscreen (never passing ``start_print``, never restamping)
        # would inherit it and report the age of the previous job.  The
        # label guard cannot save that case: Bambu's file name comes from
        # the push cache, which keeps naming the finished job.
        forget_job_start(adapter)


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
