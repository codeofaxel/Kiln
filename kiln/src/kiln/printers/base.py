"""Abstract printer adapter interface for the Kiln project."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import re
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from kiln.cli_command import kiln_command
from kiln.printer_backends import need_where
from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

# Guards the one-time, per-instance setup of the idle-release bookkeeping
_IDLE_SETUP_LOCK = threading.Lock()


def _quiet_start_contract(file_name: str, kwargs: dict[str, Any]) -> dict[str, str] | None:
    """Kiln's quiet-start header from the file about to start, or ``None``."""
    try:
        from kiln.plate_state import quiet_start_contract_for

        local = next(
            (
                kwargs[k] for k in ("local_file_path", "source_path", "local_path", "gcode_path", "file_path", "threemf_path")
                if isinstance(kwargs.get(k), str) and kwargs[k]
            ),
            None,
        )
        return quiet_start_contract_for(file_name, local_path=local)
    except Exception:  # noqa: BLE001 — bookkeeping never decides a start
        return None


def _discard_copies(paths: list[str]) -> None:
    """Delete the read-back copies a start held for its adapter."""
    for path in paths:
        with contextlib.suppress(OSError):
            os.unlink(path)
    paths.clear()


def is_resume_mode_3mf(file_name: str) -> bool:
    """Return True if ``file_name`` looks like a mid-print resume 3MF."""
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
    """Base exception for all printer-related errors."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class UnsupportedCommand(PrinterError):
    """The firmware answered a command with "I do not know that one"."""

    def __init__(self, command: str, *, cause: Exception | None = None) -> None:
        super().__init__(f"The printer's firmware does not know the command {command!r}.", cause=cause)
        self.command = command


class WaitingForUser(PrinterError):
    """The printer is parked waiting for a person, and reads no commands."""

    def __init__(self, command: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(
            "The printer is waiting for a click on the printer itself and is not reading commands.",
            cause=cause,
        )
        self.command = command


class PrinterEngagementError(PrinterError):
    """Refused because Kiln is already working with a different machine."""

    def __init__(self, verdict: dict, *, cause: Exception | None = None) -> None:
        super().__init__(str(verdict.get("reason") or "Kiln is working with another printer."), cause=cause)
        self.verdict = verdict


class PrinterLockedError(PrinterEngagementError):
    """Refused because another Kiln session holds this printer (:mod:`kiln.printer_lock`)."""


class FilamentHandlingUnsupported(PrinterError):
    """This backend has no honest way to load, unload, or purge filament."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


_UNREAD = object()


class ModelDeclarationRequired(PrinterError):
    """Raised when a motion needs the catalogue and no printer_model names the row."""


class HomingUnsupported(PrinterError):
    """Raised when a backend cannot home the head the way Kiln trusts."""


class PlateClearRequired(PrinterError):
    """Raised when a motion would press the nozzle onto the plate and no person has said the plate is empty."""

    def __init__(self, message: str, *, snapshot_path: str | None = None) -> None:
        super().__init__(message)
        self.snapshot_path = snapshot_path

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
    # The reading itself has expired
    STALE = "stale"
    UNAUTHORIZED = "unauthorized"
    CONNECTION_LIMIT = "connection_limit"


BUSY_STATES: frozenset[PrinterStatus] = frozenset(
    {
        PrinterStatus.PRINTING,
        PrinterStatus.PAUSED,
        PrinterStatus.BUSY,
        PrinterStatus.CANCELLING,
    }
)
READY_STATES: frozenset[PrinterStatus] = frozenset({PrinterStatus.IDLE})
UNREACHABLE_STATES: frozenset[PrinterStatus] = frozenset(
    {
        PrinterStatus.OFFLINE,
        PrinterStatus.UNAUTHORIZED,
        PrinterStatus.CONNECTION_LIMIT,
    }
)
INDETERMINATE_STATES: frozenset[PrinterStatus] = frozenset(
    {
        PrinterStatus.ERROR,
        PrinterStatus.UNKNOWN,
        PrinterStatus.STALE,
    }
)


def as_status(value: Any) -> PrinterStatus | None:
    """A :class:`PrinterStatus` from a member or its serialised word."""
    if isinstance(value, PrinterStatus):
        return value
    try:
        return PrinterStatus(str(value).strip().lower())
    except (ValueError, AttributeError):
        return None


def effective_state_of(reading: Any) -> PrinterStatus | None:
    """:attr:`PrinterState.effective_state` from anything state-shaped."""
    return _state_attr(reading, "effective_state")


def confirmed_state_of(reading: Any) -> PrinterStatus | None:
    """:attr:`PrinterState.confirmed_state` from anything state-shaped."""
    return _state_attr(reading, "confirmed_state")


def _state_attr(reading: Any, name: str) -> Any:
    """*name* off *reading* when it is a real status, else its ``state``."""
    value = getattr(reading, name, None)
    if isinstance(value, PrinterStatus):
        return value
    return getattr(reading, "state", None)


def row_run_state(row: Any) -> str | None:
    """The run-state WORD from a serialised reading, seen through a headline."""
    if not isinstance(row, dict):
        return None
    last = row.get("last_known_state")
    if isinstance(last, str) and last:
        return last
    state = row.get("state")
    return state if isinstance(state, str) and state else None


def status_is_occupied(status: Any) -> bool:
    """Might a machine in *status* have work in flight?"""
    resolved = as_status(status)
    if resolved is None:
        return False
    return resolved in BUSY_STATES or resolved is PrinterStatus.STALE


def status_is_unreachable(status: Any) -> bool:
    """Is a machine in *status* one Kiln currently cannot see?"""
    resolved = as_status(status)
    return resolved is not None and resolved in UNREACHABLE_STATES


class JobResult(enum.Enum):
    """How the most recent print job ENDED, as the firmware reports it."""

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


STALE_STATE_WARN_AGE: float = 60.0

STALE_STATE_MAX_AGE: float = 300.0
# How many consecutive missed reports it takes before a reading is stale
STALE_CADENCE_MULTIPLIER: float = 3.0
_CADENCE_WINDOW: int = 12


class TelemetryCadence:
    """How often a given printer ACTUALLY reports, measured from its pushes."""

    def __init__(self, window: int = _CADENCE_WINDOW) -> None:
        self._window = max(2, int(window))
        self._last: float | None = None
        self._gaps: list[float] = []

    def record(self, at: float) -> None:
        """Note that a state-bearing report arrived at monotonic time *at*."""
        previous, self._last = self._last, at
        if previous is None:
            return
        gap = at - previous
        if gap <= 0 or gap > STALE_STATE_MAX_AGE:
            return
        self._gaps.append(gap)
        if len(self._gaps) > self._window:
            del self._gaps[: len(self._gaps) - self._window]

    @property
    def observed_interval_seconds(self) -> float | None:
        """This printer's typical gap between reports, or ``None`` if unmeasured."""
        if not self._gaps:
            return None
        ordered = sorted(self._gaps)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def stale_after_seconds(self) -> float:
        """The age past which this printer's cache stops being evidence."""
        interval = self.observed_interval_seconds
        if interval is None:
            return STALE_STATE_WARN_AGE
        return min(
            STALE_STATE_MAX_AGE,
            max(STALE_STATE_WARN_AGE, STALE_CADENCE_MULTIPLIER * interval),
        )


def format_error_code(raw: Any) -> str | None:
    """A firmware error code in the form the printer's own screen shows."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return f"{(value >> 16) & 0xFFFF:04X}-{value & 0xFFFF:04X}"


def describe_stale_remedy(
    state_age_seconds: float, stale_after_seconds: float
) -> str:
    """What to DO about an expired reading, in one sentence."""
    return (
        f"Nothing has arrived for {state_age_seconds:.0f}s, against the "
        f"{stale_after_seconds:.0f}s this printer's own reporting pace "
        f"allows for. Look at the machine before acting on anything below."
    )


def describe_stale_state(
    state_age_seconds: float | None,
    state_label: str,
    max_age: float = STALE_STATE_WARN_AGE,
) -> str | None:
    """One sentence naming a reading's age, or ``None`` when it is fresh."""
    if state_age_seconds is None or state_age_seconds <= max_age:
        return None
    # This sentence says nothing about temperatures ON PURPOSE
    return (
        f"Telemetry is {state_age_seconds:.0f}s old — the printer has not "
        f"reported since, so {str(state_label).upper()} describes then, not "
        f"now. Verify against the machine before acting."
    )


def describe_unknown_temperatures(state_age_seconds: float | None) -> str:
    """Why there is no temperature in this reading, and what to do instead."""
    if state_age_seconds is None:
        since = "the printer has stopped reporting"
    else:
        since = f"the printer has not reported for {state_age_seconds:.0f}s"
    return (
        f"Kiln does not know the hotend or bed temperature: {since}. The "
        "printer's own display is the only authority — read it before "
        "touching anything, and do not act on any earlier number."
    )


def describe_missing_chamber_sensor() -> str:
    """Why there is no chamber temperature from this machine, ever."""
    return (
        "This printer has no chamber temperature sensor, so there is no "
        "chamber reading to report. Any chamber figure its firmware sends is "
        "a placeholder, not a measurement."
    )


def describe_unacknowledged_fault(
    code: str | None, reading: str | None = None
) -> str:
    """What a latched firmware fault means, and the one thing that clears it."""
    shown = f" ({code})" if code else ""
    gloss = f" {reading.strip()}" if reading else ""
    return (
        f"The printer is reporting a fault{shown} that nothing has "
        f"acknowledged.{gloss}"
    )


def describe_fault_remedy(fix: str | None = None) -> str:
    """What CLEARS a latched fault, in one sentence -- after the fix, if any."""
    clear = (
        "Clear it on the printer's own screen, or with clear_printer_error. "
        "Until it is cleared Kiln reports this machine as faulted rather "
        "than ready."
    )
    if not fix or not fix.strip():
        return clear
    return (
        f"{fix.strip()} Once that is done, clear the fault on the printer's "
        "own screen, or with clear_printer_error. Until it is cleared Kiln "
        "reports this machine as faulted rather than ready."
    )


def describe_screen_faults(faults: Any) -> list[str]:
    """One line per fault, as the printer's own screen would put it."""
    lines: list[str] = []
    if not isinstance(faults, list):
        return lines
    for entry in faults:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if not isinstance(code, str) or not code:
            continue
        text = entry.get("screen_text")
        if isinstance(text, str) and text:
            lines.append(f"{code}: {text}")
        elif entry.get("kind") == "hms":
            lines.append(code)
        remedy = entry.get("remedy")
        if entry.get("kind") == "hms" and isinstance(remedy, str) and remedy:
            reading = entry.get("reading")
            if isinstance(reading, str) and reading:
                lines.append(f"  {reading}")
            lines.append(f"  {remedy}")
    return lines


_FAULT_STOPPED_STATES: frozenset[str] = frozenset({"paused", "error"})


def _reading_field(reading: Any, name: str) -> Any:
    """Attribute-or-key read off a state object or its serialised dict."""
    if reading is None:
        return None
    if isinstance(reading, dict):
        return reading.get(name)
    return getattr(reading, name, None)


def fault_banner(
    reading: Any, *, printer_name: str | None = None
) -> dict[str, Any] | None:
    """The one short block every door shows for a fault the printer reports."""
    faults = _reading_field(reading, "faults")
    if not isinstance(faults, list):
        faults = []
    faults = [f for f in faults if isinstance(f, dict) and f.get("code")]
    print_error = _reading_field(reading, "print_error")
    fault_note = _reading_field(reading, "fault_note")
    if not faults:
        pretty = format_error_code(print_error)
        if pretty is None and not fault_note:
            return None
        faults = [{"code": pretty or "unknown", "kind": "print_error"}]

    if isinstance(reading, dict):
        run = row_run_state(reading) or reading.get("state")
    else:
        run = effective_state_of(reading)
    run_word = str(getattr(run, "value", run) or "").lower() or None
    stopped = run_word in _FAULT_STOPPED_STATES

    lead = faults[0]
    code = str(lead.get("code"))
    codes = [str(f.get("code")) for f in faults]
    text = lead.get("screen_text") or lead.get("reading")
    screen_text = text.strip() if isinstance(text, str) and text.strip() else None
    if isinstance(fault_note, str) and fault_note.strip():
        what_happened = fault_note.strip()
    elif screen_text:
        what_happened = f"{code}: {screen_text}"
    else:
        what_happened = f"The printer is reporting {code}."
    remedy = _reading_field(reading, "fault_remedy")
    if not (isinstance(remedy, str) and remedy.strip()):
        own = lead.get("remedy")
        remedy = describe_fault_remedy(own if isinstance(own, str) else None)
    what_to_do = str(remedy).strip()

    where = f" on {printer_name}" if printer_name else ""
    headline = f"{code}: {screen_text}" if screen_text else code
    if stopped:
        acted = "paused" if run_word == "paused" else "stopped"
        note = (
            f"PRINTER FAULT{where}: {headline}. The printer has {acted} for it "
            f"and is waiting for a person at the machine; polling will not "
            f"clear it. Tell the person now. {what_to_do}"
        )
    else:
        note = (
            f"PRINTER FAULT{where}: {headline}. The printer reports it while "
            f"still {run_word or 'running'}. {what_to_do}"
        )
    return {
        "printer_name": printer_name,
        "code": code,
        "codes": codes,
        "state": run_word,
        "needs_person": stopped,
        "screen_text": screen_text,
        "what_happened": what_happened,
        "what_to_do": what_to_do,
        "lines": describe_screen_faults(faults),
        "note": note,
    }


# : The fields a person might act on with their hands
TEMPERATURE_FIELDS: tuple[str, ...] = (
    "tool_temp_actual",
    "tool_temp_target",
    "bed_temp_actual",
    "bed_temp_target",
    "chamber_temp_actual",
    "chamber_temp_target",
)


@dataclass(frozen=True)
class ActiveSlotReading:
    """Which spool a multi-material unit is feeding, as the machine reports it."""

    slot: str | None
    source: str
    verified: bool = False


@dataclass(frozen=True)
class NozzleSetting:
    """The nozzle a printer HOLDS ON RECORD for itself, read off the machine."""

    material: str | None
    diameter_mm: float | None
    source: str
    age_seconds: float | None = None
    stale_after_seconds: float | None = None
    firmware_version: str | None = None
    held_by: str = "machine"

    def is_empty(self) -> bool:
        return self.material is None and self.diameter_mm is None


@dataclass(frozen=True)
class MachineIdentity:
    """An id the machine reports for ITSELF, read off the printer at setup."""

    value: str
    source: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source, "kind": self.kind}


MACHINE_IDENTITY_READ_SECONDS: float = 5.0


def read_machine_identity_bounded(
    read: Callable[[], MachineIdentity | None],
    *,
    timeout_seconds: float = MACHINE_IDENTITY_READ_SECONDS,
) -> MachineIdentity | None:
    """Run *read* with a time limit; ``None`` when it fails or runs over."""
    box: list[MachineIdentity | None] = [None]

    def _run() -> None:
        try:
            found = read()
        except Exception:
            logger.debug("machine identity read failed", exc_info=True)
            return
        if isinstance(found, MachineIdentity) and found.value.strip():
            box[0] = found

    worker = threading.Thread(target=_run, name="kiln-machine-identity", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        logger.debug("machine identity read gave up after %.1fs", timeout_seconds)
        return None
    return box[0]


@dataclass(frozen=True)
class NozzleClumpingDetection:
    """Whether a printer's own "nozzle clumping detection" switch is on, read off the machine."""

    enabled: bool | None
    source: str
    age_seconds: float | None = None
    stale_after_seconds: float | None = None
    firmware_version: str | None = None
    unverified_reason: str | None = None
    supported: bool | None = None
    mode: str | None = None

    def __post_init__(self) -> None:
        if self.mode is not None and self.mode not in ("on", "off", "auto"):
            raise ValueError(f"NozzleClumpingDetection: unknown mode {self.mode!r}")
        if (
            self.enabled is None
            and self.supported is not False
            and not (self.unverified_reason or "").strip()
        ):
            raise ValueError(
                "NozzleClumpingDetection: an undecoded reading must say why "
                "(unverified_reason) -- unknown never travels unexplained"
            )

    def is_decoded(self) -> bool:
        """``True`` when the backend vouches for the on/off value."""
        return self.enabled is not None


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
    state_age_seconds: float | None = None
    last_job_result: JobResult | None = None
    last_known_state: PrinterStatus | None = None
    state_stale_after_seconds: float | None = None
    cause: str | None = None
    remedy: str | None = None
    temperature_note: str | None = None
    fault_note: str | None = None
    # What CLEARS the fault above
    fault_remedy: str | None = None
    faults: list[dict[str, Any]] | None = None
    chamber_sensor: bool | None = None
    chamber_note: str | None = None

    def __post_init__(self) -> None:
        """Promote an expired reading to ``STALE``, whoever built it."""
        if self.chamber_sensor is False:
            self.chamber_temp_actual = None
            self.chamber_temp_target = None
            if self.chamber_note is None:
                self.chamber_note = describe_missing_chamber_sensor()

        if not (
            self.state is PrinterStatus.STALE
            or self.state in UNREACHABLE_STATES
            or self.state_age_seconds is None
            or self.state_stale_after_seconds is None
            or self.state_age_seconds <= self.state_stale_after_seconds
        ):
            self.last_known_state = self.state
            self.state = PrinterStatus.STALE
            if self.cause is None:
                self.cause = CAUSE_SILENT
            if self.remedy is None:
                self.remedy = describe_stale_remedy(
                    self.state_age_seconds, self.state_stale_after_seconds
                )

        if (
            self.print_error_code is not None
            and self.connected
            and self.state is not PrinterStatus.STALE
            and self.state is not PrinterStatus.ERROR
            and self.state not in UNREACHABLE_STATES
        ):
            self.last_known_state = self.state
            self.state = PrinterStatus.ERROR
        if (
            self.state is PrinterStatus.ERROR
            and self.print_error_code is not None
            and self.fault_note is None
        ):
            self.fault_note = describe_unacknowledged_fault(self.print_error_code)
        if (
            self.state is PrinterStatus.ERROR
            and self.fault_note is not None
            and self.fault_remedy is None
        ):
            self.fault_remedy = describe_fault_remedy()

        if not (self.state is PrinterStatus.STALE or not self.connected):
            return
        for name in TEMPERATURE_FIELDS:
            setattr(self, name, None)
        if self.connected and self.temperature_note is None:
            self.temperature_note = describe_unknown_temperatures(
                self.state_age_seconds
            )

    @property
    def print_error_code(self) -> str | None:
        """:attr:`print_error` as the printer's own screen renders it."""
        return format_error_code(self.print_error)

    @property
    def effective_state(self) -> PrinterStatus:
        """What the machine was doing, looking through a displaced headline."""
        if self.last_known_state is not None:
            return self.last_known_state
        return self.state

    @property
    def confirmed_state(self) -> PrinterStatus:
        """The run state, but only from a reading Kiln can vouch for."""
        if self.state is PrinterStatus.STALE:
            return PrinterStatus.STALE
        return self.effective_state

    @property
    def is_occupied(self) -> bool:
        """Might this machine have work in flight?"""
        if self.state is PrinterStatus.STALE and self.last_known_state is None:
            return True
        # Through the headline, whichever promotion set it
        return self.effective_state in BUSY_STATES

    def freshness_budget(self, max_age: float | None = None) -> float:
        """The age past which this reading stops counting as evidence."""
        if max_age is not None:
            return max_age
        if self.state_stale_after_seconds is not None:
            return self.state_stale_after_seconds
        return STALE_STATE_WARN_AGE

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["state"] = self.state.value
        if self.last_job_result is not None:
            data["last_job_result"] = self.last_job_result.value
        if self.last_known_state is not None:
            data["last_known_state"] = self.last_known_state.value
        code = self.print_error_code
        if code is not None:
            data["print_error_code"] = code
        # Omit None extended fields.
        _EXTENDED = (
            "cooling_fan_speed", "aux_fan_speed", "chamber_fan_speed",
            "heatbreak_fan_speed", "wifi_signal", "nozzle_diameter",
            "nozzle_type", "speed_profile", "speed_magnitude", "print_error",
            "state_age_seconds", "last_job_result", "last_known_state",
            "state_stale_after_seconds", "cause", "remedy",
            "temperature_note", "fault_note", "fault_remedy", "faults",
            "chamber_sensor", "chamber_note",
        )
        for key in _EXTENDED:
            if data.get(key) is None:
                data.pop(key, None)
        return data

    def is_stale(self, max_age: float | None = None) -> bool:
        """Whether :attr:`state` is older than its freshness budget."""
        if self.state_age_seconds is None:
            return False
        return self.state_age_seconds > self.freshness_budget(max_age)

    def staleness_note(self, max_age: float | None = None) -> str | None:
        """One sentence naming this reading's age, or ``None`` when fresh."""
        return describe_stale_state(
            self.state_age_seconds,
            self.effective_state.value,
            self.freshness_budget(max_age),
        )


@dataclass
class JobProgress:
    """Progress information for the currently active (or most recent) job."""

    file_name: str | None = None
    completion: float | None = None
    print_time_seconds: int | None = None
    print_time_left_seconds: int | None = None
    # Extended layer tracking (populated by adapters that support it).
    current_layer: int | None = None
    total_layers: int | None = None
    job_id: str | None = None
    ended_as: JobResult | None = None
    # Whether this job is the one the machine is running NOW
    active: bool | None = None

    @property
    def is_active(self) -> bool:
        """Is this a job the machine is running now?"""
        if self.active is not None:
            return self.active
        return self.ended_as is None

    @property
    def progress_pending(self) -> bool:
        """Whether the printer's own progress counter has not started while its layer counter says the print has."""
        if not self.is_active or self.completion is None or self.current_layer is None:
            return False
        try:
            return float(self.completion) == 0.0 and int(self.current_layer) >= 1
        except (TypeError, ValueError):
            return False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data.pop("ended_as", None)
        data.pop("active", None)
        if self.file_name and not self.is_active:
            data["active"] = False
            data.pop("print_time_left_seconds", None)
            if self.ended_as is not None:
                data["ended_as"] = self.ended_as.value
        for key in ("current_layer", "total_layers", "job_id"):
            if data.get(key) is None:
                data.pop(key, None)
        if self.progress_pending:
            data["progress_pending"] = True
        return data


def hardware_stop_waiting(adapter: Any) -> dict[str, Any] | None:
    """The stop *adapter*'s printer is paused at, waiting for hardware, or ``None``."""
    try:
        from kiln.hardware_stops import stop_awaiting_hands

        return stop_awaiting_hands(adapter)
    except Exception:  # noqa: BLE001
        return None


def read_status(adapter: Any) -> tuple[PrinterState, JobProgress]:
    """Read both halves of a printer's status and make them agree."""
    state = adapter.get_state()
    return state, reconcile_job_with_state(state, adapter.get_job())


def reconcile_job_with_state(
    state: PrinterState, job: JobProgress
) -> JobProgress:
    """A job block that cannot contradict the state standing beside it."""
    if not isinstance(state, PrinterState) or not isinstance(job, JobProgress):
        return job
    if state.effective_state in BUSY_STATES:
        if job.ended_as is None and job.active is not False:
            return job
        return replace(job, ended_as=None, active=True)
    if not job.file_name:
        return job
    ending = job.ended_as if job.ended_as is not None else state.last_job_result
    if job.active is False and job.ended_as is ending:
        return job
    return replace(job, ended_as=ending, active=False)


def stuck_job_note(state: PrinterState, job: JobProgress) -> str | None:
    """Name the held-job condition, and the one thing that clears it."""
    if not isinstance(state, PrinterState) or not isinstance(job, JobProgress):
        return None
    if state.state is not PrinterStatus.STALE:
        return None
    if not job.file_name or job.is_active:
        return None
    age = state.state_age_seconds
    aged = f"{age:.0f}s" if isinstance(age, (int, float)) else "some time"
    ended = f" ({job.ended_as.value})" if job.ended_as is not None else ""
    return (
        f"The printer is still holding a job it already finished"
        f"{ended} and has sent no update for {aged}. On the "
        f"machine itself this is what greys out Load and Unload, so filament "
        f"cannot be changed by hand. Power-cycle the printer — switch it off, "
        f"wait about ten seconds, switch it on — and the held job clears. "
        f"Clearing the error code from here does not release it."
    )


@dataclass
class ReadDiagnosis:
    """Why a status read produced no current answer, and what to do about it."""

    state: PrinterStatus
    cause: str
    remedy: str


# The four causes, as stable strings for anything branching on them.
CAUSE_POWERED_OFF = "powered_off_or_off_network"
CAUSE_WRONG_ACCESS_CODE = "wrong_access_code"
CAUSE_CONNECTION_LIMIT = "connection_limit"
CAUSE_SILENT = "reachable_but_silent"
CAUSE_WAITING_FOR_USER = "waiting_for_user"


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool | None:
    """Can this machine open a TCP socket to *host*:*port*?"""
    if not host:
        return None
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 — a probe never raises into a status read
        return None


_AUTH_NEEDLES: tuple[str, ...] = (
    "not authorized",
    "unauthorized",
    "unauthorised",
    "access code",
    "api key",
    "api-key",
    "forbidden",
    "authentication",
    "invalid credentials",
)

def reads_as_credentials_refusal(message: str | None) -> bool:
    """True when *message* names refused credentials, not reachability."""
    text = (message or "").lower()
    return any(needle in text for needle in _AUTH_NEEDLES)


_SLOT_NEEDLES: tuple[str, ...] = (
    "already connected",
    "connections at once",
    "single client",
    "single-client",
    "connection slot",
    "too many connections",
)


def diagnose_read_failure(
    message: str,
    *,
    host: str = "",
    port: int | None = None,
    kiln_slot_holders: int | None = None,
    reachable: bool | None = None,
) -> ReadDiagnosis:
    """Sort a failed printer read into ONE of the four causes, with its fix."""
    text = (message or "").lower()

    if reads_as_credentials_refusal(message):
        return ReadDiagnosis(
            state=PrinterStatus.UNAUTHORIZED,
            cause=CAUSE_WRONG_ACCESS_CODE,
            remedy=(
                "The printer answered and refused Kiln's access code. Find "
                f"it {need_where('bambu', 'access_code')}, turn LAN Only "
                "Mode off and on, then Developer Mode off and on, and copy "
                "the NEW code — a restarted printer issues a fresh one even "
                "though it looks the same. Then run "
                "`kiln config set access_code <new code>`."
            ),
        )

    if reachable is None and host and port:
        reachable = probe_tcp(host, int(port))

    holders = kiln_slot_holders if isinstance(kiln_slot_holders, int) else 0
    if holders > 1 or (reachable and any(n in text for n in _SLOT_NEEDLES)):
        held = (
            f"{holders} copies of Kiln's own server are each holding one. "
            if holders > 1
            else ""
        )
        return ReadDiagnosis(
            state=PrinterStatus.CONNECTION_LIMIT,
            cause=CAUSE_CONNECTION_LIMIT,
            remedy=(
                "The printer is powered on and answering, but it allows only "
                f"a few connections at once and they are taken. {held}"
                "Closing the leftover servers frees them — run "
                f"trim_serve_processes (terminal: `{kiln_command('trim')}`). Power-cycling "
                "the printer will not help, and closing Bambu Studio or the "
                "Handy app frees a slot too."
            ),
        )

    if reachable:
        return ReadDiagnosis(
            state=PrinterStatus.STALE,
            cause=CAUSE_SILENT,
            remedy=(
                "The printer is on the network and accepting connections but "
                "is not reporting anything, so Kiln has nothing current to "
                "show. Check its screen: a printer sitting on a finished or "
                "cancelled job stops publishing until it is power-cycled — "
                "switch it off, wait about ten seconds, switch it on."
            ),
        )

    return ReadDiagnosis(
        state=PrinterStatus.OFFLINE,
        cause=CAUSE_POWERED_OFF,
        remedy=(
            "Nothing answered at the printer's address, which is what a "
            "printer that is switched off or off this network looks like. "
            "Check it is powered on and connected to the same network, and "
            "that the address in your Kiln config still matches the one on "
            "its screen."
        ),
    )


def diagnosed_state(diagnosis: ReadDiagnosis) -> PrinterState:
    """The :class:`PrinterState` a *diagnosis* stands for."""
    return PrinterState(
        connected=diagnosis.state is PrinterStatus.STALE,
        state=diagnosis.state,
        cause=diagnosis.cause,
        remedy=diagnosis.remedy,
    )


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
        """Return a JSON-serialisable dictionary."""
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
    refused_before_send: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        out = asdict(self)
        if not self.refused_before_send:
            out.pop("refused_before_send", None)
        return out


@dataclass
class HardwareStopRefusal(PrintResult):
    """A resume refused because the printer is waiting at a hardware stop."""

    code: str = "HARDWARE_NOT_CONFIRMED"
    hardware: dict[str, Any] | None = None


@dataclass
class CodedPrintResult(PrintResult):
    """A print-control result carrying a stable code an agent can branch on."""

    code: str = ""


# ---------------------------------------------------------------------------
# Filament handling (load / unload / purge)
# ---------------------------------------------------------------------------

# : Cold-extrusion floor
MIN_EXTRUDE_TEMP_C: float = 170.0

# : Longest single purge Kiln will command
MAX_PURGE_LENGTH_MM: float = 150.0

DEFAULT_PURGE_LENGTH_MM: float = 30.0

DEFAULT_LOAD_LENGTH_MM: float = 60.0
DEFAULT_UNLOAD_LENGTH_MM: float = 80.0

# : Slow feed for every extrude Kiln commands (3 mm/s)
FILAMENT_FEED_RATE_MM_MIN: int = 180

#: How long the shared sequence waits for the hotend to reach target.
HOTEND_HEAT_TIMEOUT_S: float = 240.0

_DEFAULT_MAX_HOTEND_C: float = 300.0


@dataclass
class FilamentOpPlan:
    """A validated filament operation, handed to an adapter's ``_impl``."""

    action: str  # "load" | "unload" | "purge" | "wipe"
    temperature: float
    temperature_source: str
    slot: int | None = None
    material: str | None = None
    length_mm: float | None = None
    material_window: tuple[float, float, str] | None = None
    #: Adapter-specific extras forwarded verbatim (e.g. ``wait_seconds``).
    options: dict[str, Any] = field(default_factory=dict)
    printer_paused: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.material_window is not None:
            data["material_window"] = list(self.material_window)
        return data

    def wait_seconds(self, default: float) -> float:
        """How long the backend may watch the printer for its answer."""
        asked = self.options.get("wait_seconds")
        wait = float(default) if asked is None else float(asked)
        ceiling = self.options.get("wait_ceiling_seconds")
        if ceiling is not None and float(ceiling) > 0:
            wait = min(wait, float(ceiling))
        return wait


@dataclass
class FilamentOpResult:
    """Outcome of a load, unload, or purge."""

    success: bool
    action: str
    message: str
    extrusion_verified: bool | None = None
    verification_source: str | None = None
    error_code: str | None = None
    error_hint: str | None = None
    slot: int | None = None
    material: str | None = None
    temperature: float | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    step_sent: int | None = None
    next_step: dict[str, Any] | None = None
    leaves: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class HomeStep:
    """One motion of a homing sequence, described before it runs."""

    number: int
    label: str
    you_will_see: str
    stops_when: str
    gcode: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HomeResult:
    """Outcome of :meth:`PrinterAdapter.home_axes`."""

    success: bool
    outcome: str
    message: str
    axes: str
    action: str = "home"
    homed_axes: list[str] = field(default_factory=list)
    mechanism: str | None = None
    sequence_source: str | None = None
    resting_position: dict[str, Any] = field(default_factory=dict)
    heats_nozzle_to_c: float | None = None
    error_code: str | None = None
    error_hint: str | None = None
    # : The sequence as steps, whenever the backend can describe it
    steps: list[dict[str, Any]] = field(default_factory=list)
    step_sent: int | None = None
    next_step: dict[str, Any] | None = None
    leaves: list[str] = field(default_factory=list)
    plate_witness: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterCapabilities:
    """Declares what a specific adapter is able to do."""

    can_upload: bool = True
    can_set_temp: bool = True
    can_send_gcode: bool = True
    can_pause: bool = True
    can_stream: bool = False
    can_probe_bed: bool = False
    can_update_firmware: bool = False
    can_snapshot: bool = False
    can_detect_filament: bool = False
    can_clear_error: bool = False
    can_handle_filament: bool = False
    cancel_during_calibration_faults: bool = False
    can_report_multi_material: bool = False
    device_type: str = "fdm_printer"
    supported_extensions: tuple[str, ...] = (".gcode", ".gco", ".g")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
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
    """Map a device-reported model name to a ``printer_intelligence.json`` key, or ``None`` when no canonical profile matches."""
    spaced = re.sub(r"[^a-z0-9]+", "_", reported.lower()).strip("_")
    norm = re.sub(r"(?<=[a-z])_(?=\d)", "", spaced)
    if not norm:
        return None
    candidates: list[str] = []
    for spelling in (norm, spaced):
        if vendor_prefix and not spelling.startswith(vendor_prefix):
            candidates.append(f"{vendor_prefix}{spelling}")
        candidates.append(spelling)
    candidates = list(dict.fromkeys(candidates))
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
    """Two or more sources disagree about what a printer is."""

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
            f"Set the correct model with `{kiln_command('set-model --replace')}` (or the `set_printer_model` tool); "
            "if it is already correct, this means one of Kiln's identity "
            "tables is wrong and should be reported."
        )

    def to_dict(self) -> dict[str, Any]:
        return {"claims": dict(self.claims), "models": self.models,
                "summary": self.describe()}


@dataclass(frozen=True)
class PrinterInfo:
    """A printer's self-reported identity, for telemetry and display."""

    model: str | None = None
    raw_model: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


def _raise_if_refused(verdict: dict | None) -> None:
    if verdict is not None:
        raise PrinterEngagementError(verdict)


def _raise_if_held(adapter: Any, action: str) -> None:
    """Refuse a command another Kiln session's hold on this printer forbids."""
    from kiln.printer_lock import check

    verdict = check(adapter, action)
    if verdict is not None:
        raise PrinterLockedError(verdict)


def _observed(adapter: Any, action: str, result: Any) -> Any:
    """Learn from the answer the command already produced, and pass it on."""
    from kiln.printers.engagement import observe

    observe(adapter, action, result)
    return result


def _make_engagement_gated(action: str, original):
    """Wrap *original* so it consults the single-printer engagement first."""
    import functools
    import inspect

    from kiln.printers.engagement import is_look

    looks = is_look(action)
    if inspect.iscoroutinefunction(original):
        if looks:
            @functools.wraps(original)
            async def _gated_async(self, *args, **kwargs):
                from kiln.printers.engagement import check_look

                _raise_if_refused(check_look(self, action))
                return await original(self, *args, **kwargs)
        else:
            @functools.wraps(original)
            async def _gated_async(self, *args, **kwargs):
                from kiln.printers.engagement import check_command

                _raise_if_held(self, action)
                _raise_if_refused(check_command(self, action))
                return await original(self, *args, **kwargs)

        _gated_async._kiln_engagement_wrapped = True  # type: ignore[attr-defined]
        return _gated_async

    if looks:
        @functools.wraps(original)
        def _gated(self, *args, **kwargs):
            from kiln.printers.engagement import check_look

            _raise_if_refused(check_look(self, action))
            return _observed(self, action, original(self, *args, **kwargs))
    else:
        @functools.wraps(original)
        def _gated(self, *args, **kwargs):
            from kiln.printers.engagement import check_command

            _raise_if_held(self, action)
            _raise_if_refused(check_command(self, action))
            return _observed(self, action, original(self, *args, **kwargs))

    _gated._kiln_engagement_wrapped = True  # type: ignore[attr-defined]
    return _gated


def _install_engagement_gate(cls: type, *, own_methods_only: bool) -> None:
    """Gate every printer-directed command on *cls*."""
    import inspect

    from kiln.printers.engagement import is_gated

    names = cls.__dict__ if own_methods_only else dir(cls)
    for action in sorted(names):
        if not is_gated(action):
            continue
        # Only plain methods
        raw = inspect.getattr_static(cls, action, None)
        if not inspect.isfunction(raw):
            continue
        original = cls.__dict__.get(action) if own_methods_only else getattr(cls, action, None)
        if original is None or not callable(original):
            continue
        if getattr(original, "_kiln_engagement_wrapped", False):
            continue
        if getattr(original, "__isabstractmethod__", False):
            continue
        setattr(cls, action, _make_engagement_gated(action, original))


def _wrap_cancel_gives_back(cls: type) -> None:
    """Wrap *cls*'s own ``cancel_print`` so a cancelled print gives filament back."""
    original = cls.__dict__.get("cancel_print")
    if (
        original is None
        or not callable(original)
        or getattr(original, "_kiln_spool_wrapped", False)
        or getattr(original, "__isabstractmethod__", False)
    ):
        return
    import functools

    @functools.wraps(original)
    def _cancel_and_give_back(self, *args, **kwargs):
        reading = None
        try:
            from kiln.spool_usage import before_cancel

            reading = before_cancel(self)
        except Exception:  # noqa: BLE001 — bookkeeping never touches a stop
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "spool charge not read before a cancel", exc_info=True
            )
        result = original(self, *args, **kwargs)
        if reading is not None and getattr(result, "success", False):
            try:
                from kiln.spool_usage import after_cancel

                after_cancel(reading)
            except Exception:  # noqa: BLE001 — bookkeeping never changes a result
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "spool give-back failed", exc_info=True
                )
        return result

    _cancel_and_give_back._kiln_spool_wrapped = True  # type: ignore[attr-defined]
    cls.cancel_print = _cancel_and_give_back


def _wrap_head_moves(cls: type) -> None:
    """Wrap *cls*'s own ``pause_print`` and ``cancel_print`` so a printer that says where its head is."""
    import functools

    for action in ("pause_print", "cancel_print"):
        original = cls.__dict__.get(action)
        if (
            original is None
            or not callable(original)
            or getattr(original, "_kiln_head_moves_wrapped", False)
            or getattr(original, "__isabstractmethod__", False)
        ):
            continue

        def _make(block: str, send):
            @functools.wraps(send)
            def _watched(self, *args, **kwargs):
                try:
                    from kiln.head_moves import around
                except Exception:  # noqa: BLE001 — learning never touches a command
                    return send(self, *args, **kwargs)
                return around(self, block, lambda: send(self, *args, **kwargs))

            _watched._kiln_head_moves_wrapped = True  # type: ignore[attr-defined]
            return _watched

        setattr(cls, action, _make(action.split("_")[0], original))


def _wrap_emergency_reads_nothing(cls: type) -> None:
    """Wrap *cls*'s own ``emergency_stop`` so no spool bookkeeping sits in front of it."""
    original = cls.__dict__.get("emergency_stop")
    if (
        original is None
        or not callable(original)
        or getattr(original, "_kiln_spool_urgent", False)
        or getattr(original, "__isabstractmethod__", False)
    ):
        return
    import functools

    @functools.wraps(original)
    def _emergency_stop(self, *args, **kwargs):
        try:
            from kiln.spool_usage import urgent_stop

            guard = urgent_stop()
        except Exception:  # noqa: BLE001 — bookkeeping never touches a stop
            return original(self, *args, **kwargs)
        with guard:
            return original(self, *args, **kwargs)

    _emergency_stop._kiln_spool_urgent = True  # type: ignore[attr-defined]
    cls.emergency_stop = _emergency_stop


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
EXTERNAL_CAMERA_NOTE = (
    "Frames from a camera you supply are what Kiln looks at for snapshots and "
    "monitoring. They do not switch on the printer's own failure detection "
    "(spaghetti, clumping, air printing): that runs on the printer against its "
    "own cameras and never sees a frame Kiln fetched elsewhere."
)

# : URL schemes a user camera may use
EXTERNAL_CAMERA_SCHEMES: tuple[str, ...] = ("http", "https", "rtsp", "rtsps")

_RTSP_SCHEMES = ("rtsp", "rtsps")
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


def redact_url_credentials(url: str | None) -> str | None:
    """``rtsp://user:secret@host/x`` -> ``rtsp://user:****@host/x``."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username or ''}:****@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def validate_external_camera_url(url: str, *, what: str) -> str:
    """Return *url* stripped, or raise ``ValueError`` naming what is wrong."""
    from urllib.parse import urlsplit

    cleaned = (url or "").strip()
    scheme = urlsplit(cleaned).scheme.lower() if cleaned else ""
    if scheme not in EXTERNAL_CAMERA_SCHEMES or not urlsplit(cleaned).netloc:
        raise ValueError(
            f"{what} must be an http(s) or rtsp(s) URL with a host, "
            f"got {redact_url_credentials(cleaned) or '(empty)'!r}."
        )
    return cleaned


@dataclass(frozen=True)
class ExternalCamera:
    """Where a user-supplied camera can be reached."""

    snapshot_url: str | None = None
    stream_url: str | None = None

    def describe(self) -> dict[str, Any]:
        """The camera as a reply may carry it: credentials redacted, always."""
        return {
            "source": "user_supplied",
            "snapshot_url": redact_url_credentials(self.snapshot_url),
            "stream_url": redact_url_credentials(self.stream_url),
            "note": EXTERNAL_CAMERA_NOTE,
        }


CAMERA_HANDSHAKE_ADDRESS_TIMEOUT = 2.0
CAMERA_HANDSHAKE_BUDGET = 5.0


@dataclass(frozen=True)
class CameraHandshake:
    """Whether a printer's camera answers, learned at setup without a frame."""

    answered: bool | None
    where: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_ffmpeg() -> str | None:
    """The program that cuts a picture from a camera stream, or ``None`` (:func:`kiln.camera_addon.ffmpeg_path`)."""
    from kiln import camera_addon

    return camera_addon.ffmpeg_path()


def capture_rtsp_frame(
    stream_url: str,
    *,
    ffmpeg: str,
    label: str = "Camera RTSP",
    timeout: float = 5.0,
) -> bytes:
    """One JPEG frame from an rtsp(s) stream, via ffmpeg."""
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-rtsp_transport", "tcp",
                "-i", stream_url,
                "-frames:v", "1",
                "-f", "image2",
                "-vcodec", "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PrinterError(
            f"{label} stream timed out after {timeout:g}s. Check camera and network."
        ) from exc
    except Exception as exc:
        raise PrinterError(
            f"Camera snapshot failed: {exc}\n"
            "Camera may be disabled or in use. Check printer camera settings. "
            "Retry with `get_snapshot()`.",
        ) from exc
    if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
        return result.stdout
    raise PrinterError(
        f"{label} snapshot failed (ffmpeg exit {result.returncode}). "
        "Check that the camera is enabled."
    )


def _first_jpeg(chunks) -> bytes | None:
    """The first complete JPEG in a byte stream, or ``None`` when it ends first."""
    buf = b""
    for chunk in chunks:
        if not chunk:
            break
        buf += chunk
        start = buf.find(_JPEG_SOI)
        if start == -1:
            # Keep only a tail that could still hold a split marker.
            buf = buf[-2:]
            continue
        end = buf.find(_JPEG_EOI, start + 3)
        if end != -1:
            return buf[start : end + 2]
    return None


def _cut_from_stream(camera: ExternalCamera) -> bool:
    """Whether a picture from *camera* is cut from its stream, which needs Kiln's camera add-on."""
    from urllib.parse import urlsplit

    url = camera.snapshot_url or camera.stream_url
    return bool(url) and urlsplit(url).scheme.lower() in _RTSP_SCHEMES


def fetch_external_snapshot(camera: ExternalCamera, *, timeout: float = 10.0) -> bytes:
    """One frame from a user-supplied camera; raises :class:`PrinterError`."""
    url = camera.snapshot_url or camera.stream_url
    if not url:
        raise PrinterError("No camera URL is registered for this printer.")
    shown = redact_url_credentials(url)

    if _cut_from_stream(camera):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            from kiln import camera_addon

            raise camera_addon.refusal(yours=True)
        return capture_rtsp_frame(url, ffmpeg=ffmpeg, label="Your camera's RTSP")

    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — user-registered camera URL
            content_type = str(resp.headers.get("Content-Type") or "").lower()
            if camera.snapshot_url or "multipart" not in content_type:
                data = resp.read()
                if content_type.startswith("multipart"):
                    data = _first_jpeg([data]) or b""
                if not data:
                    raise PrinterError(f"Your camera at {shown} answered with no image.")
                return require_picture(
                    data,
                    who=f"Your camera at {shown}",
                    hint="Check that the URL is the camera's snapshot address, not its web page.",
                )
            frame = _first_jpeg(iter(lambda: resp.read(8192), b""))
    except PrinterError:
        raise
    except Exception as exc:
        raise PrinterError(
            f"Your camera at {shown} did not answer ({exc.__class__.__name__}: {exc}). "
            "Check that the camera is on and reachable from this machine."
        ) from exc
    if not frame:
        raise PrinterError(f"Your camera at {shown} sent a stream with no JPEG frame in it.")
    return frame


def _external_stream_url(camera: ExternalCamera | None, printer_stream_url: str | None) -> str | None:
    """The stream to open: the user's camera first, else the printer's own."""
    if camera is None:
        return printer_stream_url
    return camera.stream_url


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class CameraStreamError(PrinterError):
    """A live-video source could not be opened, with the reason in words."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


NOT_A_PICTURE = "CAMERA_NOT_A_PICTURE"


def require_picture(data: bytes | None, *, who: str = "The camera", hint: str = "") -> bytes | None:
    """*data* unchanged when it is a JPEG or PNG (or ``None``: no camera), else refused in words."""
    from kiln.snapshot_analysis import picture_format

    if data is None or picture_format(data):
        return data
    size = len(data) if isinstance(data, (bytes, bytearray)) else 0
    if size == 0:
        message = f"{who} answered with no image."
    else:
        message = (
            f"{who} answered with {size} byte{'' if size == 1 else 's'} that are not a "
            "JPEG or PNG picture, so Kiln did not pass them on as one."
        )
    raise CameraStreamError(f"{message} {hint}".strip(), code=NOT_A_PICTURE)


@dataclass(frozen=True)
class StreamCapability:
    """Whether this printer can feed Kiln's local live-video relay, and why not."""

    available: bool
    channel: str | None
    reason: str | None = None
    requires: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "channel": self.channel,
            "reason": self.reason,
            "requires": list(self.requires),
        }


def _is_rtsp(url: str | None) -> bool:
    return bool(url) and str(url).lower().startswith(("rtsp://", "rtsps://"))


RTSP_NOT_RELAYED_REASON = (
    "Your camera's stream is RTSP, which Kiln's local MJPEG proxy cannot "
    "relay. Open the RTSP URL you registered in a video player directly; "
    "snapshots and monitoring still read frames from it."
)


def adapter_has_camera(adapter: Any) -> bool:
    """Whether *adapter* can produce a frame: its own camera, or the user's."""
    flag = getattr(adapter, "has_camera", None)
    if isinstance(flag, bool):
        return flag
    return bool(getattr(getattr(adapter, "capabilities", None), "can_snapshot", False))


def _wrap_camera_first(cls: type) -> None:
    """Make a subclass's own ``get_snapshot`` / ``get_stream_url`` camera-aware; a snapshot must be a picture."""
    import functools

    snapshot = cls.__dict__.get("get_snapshot")
    if snapshot is not None and not getattr(snapshot, "_kiln_camera_wrapped", False):

        @functools.wraps(snapshot)
        def _camera_first_snapshot(self, *args, **kwargs):
            if self._external_camera is not None:
                return fetch_external_snapshot(self._external_camera)
            return require_picture(snapshot(self, *args, **kwargs))

        _camera_first_snapshot._kiln_camera_wrapped = True  # type: ignore[attr-defined]
        cls.get_snapshot = _camera_first_snapshot

    stream = cls.__dict__.get("get_stream_url")
    if stream is not None and not getattr(stream, "_kiln_camera_wrapped", False):

        @functools.wraps(stream)
        def _camera_first_stream(self, *args, **kwargs):
            if self._external_camera is not None:
                return _external_stream_url(self._external_camera, None)
            return stream(self, *args, **kwargs)

        _camera_first_stream._kiln_camera_wrapped = True  # type: ignore[attr-defined]
        cls.get_stream_url = _camera_first_stream


def apply_external_camera(adapter: PrinterAdapter, entry: Any) -> None:
    """Read ``camera_snapshot_url`` / ``camera_stream_url`` off a config entry."""
    if not isinstance(entry, dict):
        return
    snapshot = str(entry.get("camera_snapshot_url") or "").strip() or None
    stream = str(entry.get("camera_stream_url") or "").strip() or None
    if snapshot or stream:
        adapter.set_external_camera(snapshot_url=snapshot, stream_url=stream)


def reheat_before_resume(adapter: Any) -> tuple[PrintResult | None, bool]:
    """The pause hold's nozzle reheat, for a resume about to be sent."""
    server = sys.modules.get("kiln.server")
    reheat = getattr(server, "_reheat_before_resume", None)
    if reheat is None:
        return None, False
    return reheat(adapter)


class PrinterAdapter(ABC):
    """Abstract base for all printer backend adapters."""

    # -- safety profile --------------------------------------------------

    _safety_profile_id: str | None = None

    # -- the head's own moves ---------------------------------------------

    head_position: str = ""

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("upload_file")
        if original is not None and not getattr(original, "_kiln_safety_wrapped", False):
            import functools

            @functools.wraps(original)
            def _safe_upload_file(self, file_path: str):
                try:
                    file_path = _preflight_upload_or_raise(self, file_path)
                except _UnsafeUpload as exc:
                    from kiln.printers import PrinterError
                    raise PrinterError(str(exc)) from None
                if refusal := _incomplete_upload_reason(self, file_path):
                    from kiln.printers import PrinterError
                    raise PrinterError(refusal)
                return original(self, file_path)

            _safe_upload_file._kiln_safety_wrapped = True  # type: ignore[attr-defined]
            cls.upload_file = _safe_upload_file

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        state_original = cls.__dict__.get("get_state")
        if state_original is not None and not getattr(
            state_original, "_kiln_outcome_wrapped", False
        ):
            import functools

            @functools.wraps(state_original)
            def _observed_get_state(self):
                read_at = time.monotonic()
                state = state_original(self)
                try:
                    _feed_outcome_lifecycle(self, state, read_at=read_at)
                except Exception:  # noqa: BLE001 — bookkeeping never breaks status
                    import logging as _logging

                    _logging.getLogger(__name__).debug(
                        "outcome lifecycle feed failed", exc_info=True
                    )
                try:
                    _feed_slot_observer(self, state)
                except Exception:  # noqa: BLE001 — bookkeeping never breaks status
                    import logging as _logging

                    _logging.getLogger(__name__).debug(
                        "slot observer feed failed", exc_info=True
                    )
                return state

            _observed_get_state._kiln_outcome_wrapped = True  # type: ignore[attr-defined]
            cls.get_state = _observed_get_state

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        _wrap_cancel_gives_back(cls)
        _wrap_emergency_reads_nothing(cls)
        _wrap_head_moves(cls)

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        _install_engagement_gate(cls, own_methods_only=True)

        # ------------------------------------------------------------------
        # A camera the user supplied: the same engine-not-instance shape
        # ------------------------------------------------------------------
        _wrap_camera_first(cls)


    def set_safety_profile(self, profile_id: str) -> None:
        """Bind a printer safety profile for temperature validation."""
        self._safety_profile_id = profile_id
        try:
            from kiln.safety_profiles import warm_profile

            warm_profile(profile_id)
        except Exception:  # noqa: BLE001 -- a setup never fails over the record
            logger.debug("printer record not warmed at setup", exc_info=True)

    _DURATION_SEMANTICS: ClassVar[str] = "stopwatch"


    #: Env var this backend reads for its idle window.  "" = no opt-in.
    _IDLE_RELEASE_ENV: ClassVar[str] = ""
    #: Seconds of caller inactivity before release.  0 = feature off.
    _IDLE_RELEASE_DEFAULT_S: ClassVar[float] = 0.0
    #: How often the reaper wakes to test the window (fraction of it).
    _IDLE_POLL_DIVISOR: ClassVar[float] = 4.0

    def _init_idle_release(self) -> None:
        """Set up idle bookkeeping."""
        if getattr(self, "_idle_stop", None) is not None:
            return
        with _IDLE_SETUP_LOCK:
            if getattr(self, "_idle_stop", None) is None:
                self._last_activity: float = time.monotonic()
                self._idle_reaper: threading.Thread | None = None
                self._idle_stop: threading.Event = threading.Event()

    def _note_activity(self) -> None:
        """Stamp caller demand."""
        self._init_idle_release()
        self._last_activity = time.monotonic()

    def _idle_window(self) -> float:
        """Seconds of inactivity before the connection is released."""
        if not self._IDLE_RELEASE_ENV:
            return 0.0
        raw = os.environ.get(self._IDLE_RELEASE_ENV, "")
        if not raw:
            return self._IDLE_RELEASE_DEFAULT_S
        try:
            return float(raw)
        except ValueError:
            logger.debug(
                "%s=%r is not a number; using the %ss default",
                self._IDLE_RELEASE_ENV,
                raw,
                self._IDLE_RELEASE_DEFAULT_S,
            )
            return self._IDLE_RELEASE_DEFAULT_S

    def _connection_is_live(self) -> bool:
        """True while this adapter holds an open connection."""
        return False

    def _print_in_flight(self) -> bool:
        """True while the printer is mid-job, as of the last status seen."""
        return True

    def _start_idle_reaper(self) -> None:
        """Start the thread that releases the connection once it falls idle."""
        window = self._idle_window()
        if window <= 0:
            return
        self._init_idle_release()
        reaper = getattr(self, "_idle_reaper", None)
        if reaper is not None and reaper.is_alive():
            return
        self._idle_stop.clear()
        self._idle_reaper = threading.Thread(
            target=self._idle_loop,
            args=(window,),
            name=f"kiln-idle-release-{self.name}",
            daemon=True,
        )
        self._idle_reaper.start()

    def _stop_idle_reaper(self) -> None:
        """Signal the reaper to exit."""
        idle_stop = getattr(self, "_idle_stop", None)
        if idle_stop is not None:
            idle_stop.set()

    def _idle_loop(self, window: float) -> None:
        """Release the connection after *window* seconds with no calls."""
        interval = max(1.0, window / self._IDLE_POLL_DIVISOR)
        while not self._idle_stop.wait(interval):
            if not self._connection_is_live():
                return
            if time.monotonic() - self._last_activity < window:
                continue
            if self._print_in_flight():
                continue
            if time.monotonic() - self._last_activity < window:
                continue
            logger.info(
                "Releasing idle connection to %s after %.0fs unused — this "
                "printer allows only a few clients at once, and the next "
                "call will reconnect.",
                getattr(self, "_host", self.name),
                window,
            )
            with contextlib.suppress(Exception):
                self.disconnect()
            return

    def disconnect(self) -> None:  # noqa: B027  (concrete no-op, not abstract)
        """Release any persistent connection this adapter holds."""

    # -- identity & feature discovery -----------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier for this adapter."""

    @property
    @abstractmethod
    def capabilities(self) -> PrinterCapabilities:
        """Return the set of capabilities this adapter supports."""

    # -- state queries --------------------------------------------------

    @abstractmethod
    def get_state(self) -> PrinterState:
        """Retrieve the current printer state and temperatures."""

    @abstractmethod
    def get_job(self) -> JobProgress:
        """Retrieve progress info for the active (or last) print job."""

    def get_status(self) -> tuple[PrinterState, JobProgress]:
        """State and job together, reconciled so they cannot contradict."""
        return read_status(self)

    @abstractmethod
    def list_files(self) -> list[PrinterFile]:
        """Return a list of files available on the printer / print server."""

    # -- file management ------------------------------------------------

    @abstractmethod
    def upload_file(self, file_path: str) -> UploadResult:
        """Upload a local G-code file to the printer."""

    # -- print control --------------------------------------------------

    def start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Begin printing a file that already exists on the printer."""
        quiet_contract = _quiet_start_contract(file_name, kwargs)
        if quiet_contract is not None:
            from kiln.plate_state import quiet_start_flags

            kwargs = {**kwargs, **quiet_start_flags(quiet_contract)}
        kept: list[str] = []
        try:
            from kiln.printers.print_gate import run_adapter_gate

            blocked = run_adapter_gate(self, file_name, kwargs, keep=kept)
        except Exception:  # noqa: BLE001 — a gate failure must never block a print
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "pre-print gate raised; allowing print", exc_info=True
            )
            blocked = None
            if quiet_contract is not None:
                blocked = {
                    "reason": (
                        f"{file_name} was planned beside a part on the plate, and the safety gate that "
                        "judges such a start against the printer could not run; it was not started. "
                        "Try again in a moment, or clear the plate and say so, then print it the ordinary way."
                    ),
                }
        if blocked is not None:
            _discard_copies(kept)
            hint = blocked.get("override_hint", "")
            reason = blocked.get("reason", "Print blocked by the pre-print safety gate.")
            message = reason + (" " + hint if hint else "")
            code = str(blocked.get("code") or "")
            if code:
                return CodedPrintResult(success=False, message=message, refused_before_send=True, code=code)
            return PrintResult(success=False, message=message, refused_before_send=True)
        # The consent backstop
        try:
            from kiln.print_signoff import adapter_verdict

            unsigned = adapter_verdict(self, file_name, kwargs)
        except Exception:  # noqa: BLE001 — bookkeeping must not strand a legitimate print
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "sign-off backstop raised; allowing print", exc_info=True
            )
            unsigned = None
        if unsigned is not None:
            _discard_copies(kept)
            return PrintResult(success=False, message=unsigned["reason"], refused_before_send=True)
        try:
            try:
                from kiln.printers.print_gate import with_judged_plate

                impl_kwargs = with_judged_plate(file_name, kwargs, kept)
            except Exception:  # noqa: BLE001 — the plate is help for routing, never a reason to refuse
                impl_kwargs = kwargs
            result = self._start_print_impl(file_name, **impl_kwargs)
        finally:
            _discard_copies(kept)
        if getattr(result, "success", False):
            try:
                from kiln.machine_repairs import start_warning

                warning = start_warning(self)
                if warning:
                    result.message = f"{result.message} {warning}".strip()
            except Exception:  # noqa: BLE001 — a warning that cannot be read is no warning
                import logging as _logging

                _logging.getLogger(__name__).debug("repair warning not read", exc_info=True)
        if getattr(result, "success", False) and not is_resume_mode_3mf(file_name):
            try:
                from kiln.printers.progress_motion import note_job_start

                note_job_start(self, file_name)
            except Exception:  # noqa: BLE001 — bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "job-start stamp failed", exc_info=True
                )

            # Kiln started this print, so Kiln is now driving this machine
            try:
                from kiln.printers.engagement import engage

                engage(self, None, reason="started")
            except Exception:  # noqa: BLE001 — bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "engagement not recorded", exc_info=True
                )
            try:
                from kiln.daily_stats import record_print_start

                record_print_start(self.name, file_name)
            except Exception:  # noqa: BLE001 — stats must never affect a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "print-start stat recording failed", exc_info=True
                )
            try:
                from kiln.monitor_twin import note_print_started

                note_print_started(self.name, file_name)
            except Exception:  # noqa: BLE001 — the twin never affects a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "monitor-twin print-start note failed", exc_info=True
                )
            try:
                from kiln.heater_check_readings import note_print_started as _note_heater_checks

                _note_heater_checks(self)
            except Exception:  # noqa: BLE001 — a reading never affects a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "heater-check print-start note failed", exc_info=True
                )
            try:
                from kiln.hardware_stops import note_print_started as _note_hardware_plan

                _note_hardware_plan(self, file_name, kwargs)
            except Exception:  # noqa: BLE001 — the plan never affects a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "hardware plan print-start note failed", exc_info=True
                )
            # The plate now holds a part
            try:
                from kiln.plate_state import mark_occupied_by_start

                mark_occupied_by_start(
                    self, file_name, plate_number=kwargs.get("plate_number"),
                    beside=quiet_contract is not None,
                )
            except Exception:  # noqa: BLE001 — the plate record never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "plate-state start note failed", exc_info=True
                )
            try:
                from kiln._pro_nozzle_bridge import record_print_odometer

                record_print_odometer(self.name, file_name)
            except Exception:  # noqa: BLE001 — wear bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "nozzle odometer recording failed", exc_info=True
                )
            try:
                from kiln._pro_cutter_bridge import record_print_cuts

                planned = record_print_cuts(
                    self.name, file_name, printer_model=self.declared_printer_model() or None
                )
                self._cutter_print = {"file": file_name, "planned": planned, "observed": 0}
            except Exception:  # noqa: BLE001 — cut bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "cutter count recording failed", exc_info=True
                )
            try:
                from kiln.spool_usage import charge_print

                charge_print(self, file_name, kwargs)
            except Exception:  # noqa: BLE001 — spool bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "spool count not started", exc_info=True
                )
            # Open the outcome row NOW, while we can still see the print
            try:
                from kiln.auto_record_hook import (
                    clear_cancel_intent,
                    open_pending_outcome,
                    print_identity_at_start,
                )

                # A cancel asked for before this print has nothing to say about this print
                clear_cancel_intent(outcome_printer_name(self))

                commanded_material = kwargs.get("material_type") or kwargs.get("material")
                # Under the name every RESOLVER looks it up by
                opened = open_pending_outcome(
                    outcome_printer_name(self),
                    file_name,
                    material_type=(
                        str(commanded_material) if commanded_material else None
                    ),
                    identity=print_identity_at_start(self, kwargs, file_name),
                )
                from kiln.learned_print_time import note_start_estimate

                note_start_estimate(opened, file_name)
            except Exception:  # noqa: BLE001 — bookkeeping must never affect a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "pending-outcome open failed", exc_info=True
                )
            try:
                from kiln.ending_watch import spawn_ending_watch

                spawn_ending_watch(self, file_name)
            except Exception:  # noqa: BLE001 — never affects a print
                pass
            try:
                from kiln._pro_goal_material_bridge import note_print_started as _note_goal_filament

                _note_goal_filament(self, file_name, kwargs)
            except Exception:  # noqa: BLE001 — a goal's record never affects a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "goal filament note failed", exc_info=True
                )
            _fire_print_started_hooks(self, file_name)
        elif getattr(result, "success", False):
            try:
                from kiln.spool_usage import resume_print as _spools_resume

                _spools_resume(self)
            except Exception:  # noqa: BLE001 — spool bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "spool charge not restored on resume", exc_info=True
                )
        return result

    @abstractmethod
    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        """Adapter-specific print start, called AFTER the pre-print gate passes."""

    @abstractmethod
    def cancel_print(self) -> PrintResult:
        """Cancel the currently running print job."""

    @abstractmethod
    def pause_print(self) -> PrintResult:
        """Pause the currently running print job."""

    def resume_print(self, *, force: bool = False, hardware_confirmed: bool = False) -> PrintResult:
        """Resume a previously paused print job, and CHECK that it took."""
        waiting = hardware_stop_waiting(self)
        if waiting is not None and not hardware_confirmed:
            from kiln.hardware_stops import refusal_message

            return HardwareStopRefusal(success=False, message=refusal_message(waiting), hardware=waiting)
        if not force:
            refusal = self._not_paused_refusal()
            if refusal is not None:
                return refusal
        refusal, reheated = reheat_before_resume(self)
        if refusal is not None:
            return refusal
        if reheated and not force:
            refusal = self._not_paused_refusal()
            if refusal is not None:
                return refusal
        try:
            from kiln.head_moves import around
        except Exception:  # noqa: BLE001 — learning never touches a resume
            result = self._verify_resume_took(self._resume_print_impl())
        else:
            result = around(self, "resume", lambda: self._verify_resume_took(self._resume_print_impl()))
        if waiting is not None and result.success:
            from kiln.hardware_stops import note_resumed

            note_resumed(self, waiting)
        return result

    def _not_paused_refusal(self) -> PrintResult | None:
        """The refusal to return before resuming, or ``None`` to go ahead."""
        try:
            state = self.get_state()
            status = getattr(state, "state", None)
        except Exception:  # noqa: BLE001 — never block a real resume on a read error
            return None

        if status is PrinterStatus.IDLE:
            # Nothing is running to continue, and no progress signal could change that
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
            # Positive evidence: a progress axis advanced
            return self._no_paused_print_result()

        if verdict.motion is Motion.STALLED:
            # The state word is contradicted by the machine's own counters
            return None

        return self._unverified_running_result()

    def _job_or_none(self) -> JobProgress | None:
        """``get_job()``, or ``None`` if it fails."""
        try:
            return self.get_job()
        except Exception:  # noqa: BLE001 — progress detail is optional here
            return None

    _RESUME_VERIFY_TIMEOUT: float = 5.0
    _RESUME_VERIFY_INTERVAL: float = 1.0

    def _verify_resume_took(self, result: PrintResult) -> PrintResult:
        """Re-read the printer and correct *result* if the resume did not take."""
        if not getattr(result, "success", False):
            return result
        try:
            import time as _time

            deadline = _time.monotonic() + self._RESUME_VERIFY_TIMEOUT
            status = None
            while True:
                status = confirmed_state_of(self.get_state())
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
        """Refusal for a printer that says PRINTING with no motion evidence."""
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
        """Adapter-specific resume, called AFTER the not-paused gate passes."""

    def _no_paused_print_result(self) -> PrintResult:
        """The honest result when there is no paused print to resume."""
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
        """Perform an immediate emergency stop on the printer."""

    def clear_error(self) -> PrintResult:
        """Acknowledge a latched firmware error so the printer can print again."""
        return PrintResult(
            success=False,
            message=(
                f"{self.name} has no known way to clear a firmware error from "
                "Kiln. Clear it on the printer's own screen or power-cycle it. "
                "See scripts/adapter_conformance.yaml for what each backend "
                "declares."
            ),
        )

    # -- calibration -----------------------------------------------------

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        """Run printer calibration routines (bed leveling, Z offset, etc.)."""
        return PrintResult(
            success=False,
            message=(
                "Calibration is not supported for this printer type. "
                "Run calibration manually from the printer's touchscreen or web UI."
            ),
        )

    # -- temperature control --------------------------------------------

    def _validate_temp(self, target: float, max_temp: float, heater: str) -> None:
        """Validate a temperature value before sending to the printer."""
        # The printer's own ceiling tightens the adapter's
        try:
            from kiln.safety_profiles import resolve_limits  # noqa: E402

            max_hotend, max_bed = resolve_limits(self._safety_profile_id or None)
            lower_heater = heater.lower()
            if lower_heater in ("hotend", "tool"):
                max_temp = min(max_temp, max_hotend)
            elif lower_heater == "bed":
                max_temp = min(max_temp, max_bed)
        except ImportError:
            pass  # fall back to caller-supplied max_temp

        if target < 0:
            raise PrinterError(f"{heater} temperature {target}°C is negative -- must be >= 0.")
        if target > max_temp:
            if not self._safety_profile_id:
                raise PrinterError(
                    f"{heater} temperature {target}°C exceeds safety limit ({max_temp:g}°C) -- the generic "
                    "floor, because Kiln has not been told this printer's model. Set printer_model to a "
                    "printer Kiln knows, or have the maker's limits confirmed by the person "
                    "(add_safety_profile)."
                )
            raise PrinterError(f"{heater} temperature {target}°C exceeds safety limit ({max_temp}°C).")


    _MAX_HOTEND_C: float = _DEFAULT_MAX_HOTEND_C

    def load_filament(
        self,
        *,
        slot: int | None = None,
        material: str | None = None,
        temperature: float | None = None,
        length_mm: float | None = None,
        **options: Any,
    ) -> FilamentOpResult:
        """Feed filament to the nozzle."""
        plan = self._prepare_filament_op(
            "load",
            slot=slot,
            material=material,
            temperature=temperature,
            length_mm=DEFAULT_LOAD_LENGTH_MM if length_mm is None else length_mm,
            options=options,
        )
        return self._finish_filament_op(plan, self._load_filament_impl(plan))

    def unload_filament(
        self,
        *,
        material: str | None = None,
        temperature: float | None = None,
        length_mm: float | None = None,
        **options: Any,
    ) -> FilamentOpResult:
        """Retract filament out of the hotend (and back to the spool unit where the backend has one)."""
        plan = self._prepare_filament_op(
            "unload",
            slot=None,
            material=material,
            temperature=temperature,
            length_mm=DEFAULT_UNLOAD_LENGTH_MM if length_mm is None else length_mm,
            options=options,
        )
        return self._finish_filament_op(plan, self._unload_filament_impl(plan))

    def purge_filament(
        self,
        *,
        length_mm: float = DEFAULT_PURGE_LENGTH_MM,
        material: str | None = None,
        temperature: float | None = None,
        slot: int | None = None,
        **options: Any,
    ) -> FilamentOpResult:
        """Extrude a short length at temperature — the clog test."""
        plan = self._prepare_filament_op(
            "purge",
            slot=slot,
            material=material,
            temperature=temperature,
            length_mm=length_mm,
            options=options,
        )
        return self._finish_filament_op(plan, self._purge_filament_impl(plan))

    def _finish_filament_op(
        self, plan: FilamentOpPlan, result: FilamentOpResult
    ) -> FilamentOpResult:
        """Add what the caller has to know but the backend cannot say."""
        if plan.printer_paused:
            result.details["printer_paused"] = True
            result.message = (
                f"{result.message} The print is PAUSED, so the nozzle was "
                "parked over the part: check for extruded filament on the "
                "model and wipe the nozzle before resuming."
            )
            result.details["heater"] = f"left at {plan.temperature:g} °C: the print is paused and will need it"
            self._release_heater_hold()
            return result
        if plan.options.get("plan_only"):
            result.details["heater"] = "untouched: nothing was sent"
            return result
        if result.next_step is not None:
            result.details["heater"] = "as the step left it -- see leaves; the finish runs after the last step"
            return result
        # The routine ran to its end on this machine
        if plan.action in ("load", "unload"):
            try:
                from kiln._pro_cutter_bridge import record_command_cut

                record_command_cut(
                    self.name, plan.action, printer_model=self.declared_printer_model() or None
                )
            except Exception:  # noqa: BLE001 -- cut bookkeeping never changes a result
                import logging as _logging

                _logging.getLogger(__name__).debug("cutter count recording failed", exc_info=True)
        # Leave the machine the way a person would: heater off, and say so
        if plan.options.get("keep_hot"):
            result.details["heater"] = f"left ON at {plan.temperature:g} °C (keep_hot was asked for)"
            result.message = f"{result.message} Heater left ON at {plan.temperature:g} °C, as asked."
            self._release_heater_hold()
            return result
        # First the retract that stops the drool
        retracted = False
        own_retract = result.details.get("end_retract_mm")
        cools = result.success and plan.action != "unload" and self.capabilities.can_send_gcode
        if cools and own_retract is None:
            try:
                verdict = self.send_gcode(["M83", "G1 E-0.8 F1800", "M82"])
                retracted = bool(getattr(verdict, "ok", verdict))
            except Exception:  # noqa: BLE001 -- the op already happened; report honestly below
                retracted = False
            result.details["end_retract_mm"] = 0.8 if retracted else None
        elif own_retract is not None:
            retracted = True
        try:
            verdict = self.set_tool_temp(0)
            off = bool(getattr(verdict, "ok", verdict))
        except Exception:  # noqa: BLE001 -- the op already happened; report the heater honestly
            off = False
        if off:
            self._release_heater_hold()
            result.details["heater"] = "off"
            result.message = (
                f"{result.message} "
                + (f"Retracted {float(result.details['end_retract_mm']):g} mm and heater off"
                   if retracted else "Heater off")
                + "."
            )
            if cools:
                result.message = f"{result.message} {self._after_heater_off(result)}"
        else:
            result.details["heater"] = f"could not be switched off -- target may still be {plan.temperature:g} °C; set_temperature(0)"
            result.message = (
                f"{result.message} WARNING: the heater-off command was refused; the nozzle may still be "
                f"at {plan.temperature:g} °C. Send set_temperature(0)."
            )
        return result

    def _hold_heater(self, target: float) -> None:
        """Kiln just set the hotend to *target* for a routine of its own: register the heater-off, so a server stopped mid-routine sends it."""
        from kiln.printers.routine_ledger import hold, printer_key

        self._release_heater_hold()
        self._heater_hold = hold(
            f"hotend at {target:g} °C for a filament routine",
            printer_key(self),
            lambda: self.set_tool_temp(0),
            adapter=self,
            kind="heater",
        )

    def _release_heater_hold(self) -> None:
        held = getattr(self, "_heater_hold", None)
        if held is not None:
            held.release()
            self._heater_hold = None

    def _after_heater_off(self, result: FilamentOpResult) -> str:
        """What happens between "heater off" and the answer, and the sentence for it."""
        from kiln.printers.motion_plan import run_finish

        finish = result.details.get("finish")
        if isinstance(finish, dict) and finish:
            sentence = run_finish(self, result, finish)
            if sentence:
                return sentence
        result.details["fan"] = "not driven"
        result.details["cooled_below_c"] = None
        return (
            "The nozzle is still at working temperature and cools on its own from here -- "
            "keep hands clear of it, and expect a small drip below it as it does."
        )

    def _prepare_filament_op(
        self,
        action: str,
        *,
        slot: int | None,
        material: str | None,
        temperature: float | None,
        length_mm: float | None,
        options: dict[str, Any] | None = None,
    ) -> FilamentOpPlan:
        """The single gate every filament door passes through."""
        what = "the nozzle" if action == "wipe" else "filament"
        if not self.capabilities.can_handle_filament:
            raise FilamentHandlingUnsupported(
                f"{self.name} cannot {action} {what} through Kiln — this "
                "backend declares no filament handling. Use the printer's "
                "own screen or web UI for that step."
            )

        if slot is not None:
            try:
                slot = int(slot)
            except (TypeError, ValueError) as exc:
                raise PrinterError(f"slot must be an integer tray id, got {slot!r}.") from exc
            if slot < 0:
                raise PrinterError(f"slot must be >= 0, got {slot}.")

        if length_mm is not None:
            try:
                length_mm = float(length_mm)
            except (TypeError, ValueError) as exc:
                raise PrinterError(f"length_mm must be a number, got {length_mm!r}.") from exc
            if action == "purge" and not 1.0 <= length_mm <= MAX_PURGE_LENGTH_MM:
                raise PrinterError(
                    f"Purge length {length_mm:g} mm is outside 1–{MAX_PURGE_LENGTH_MM:g} mm. "
                    "A clog test needs tens of millimetres; a longer extrude is a "
                    "runaway, not a purge."
                )
            if action != "purge" and not 1.0 <= length_mm <= 1000.0:
                raise PrinterError(
                    f"{action} length {length_mm:g} mm is outside 1–1000 mm."
                )

        # Not while printing
        try:
            state = self.get_state()
        except PrinterError as exc:
            raise PrinterError(
                f"Cannot {action} {what}: the printer did not answer a status "
                f"request ({exc})."
            ) from exc
        if state.effective_state == PrinterStatus.PRINTING:
            raise PrinterError(
                f"Refusing to {action} {what} while a print is running. "
                "Pause the print first, or wait for it to finish."
            )
        paused = state.effective_state == PrinterStatus.PAUSED

        window = self._filament_material_window(material, slot)
        if temperature is None:
            if window is not None:
                lo, hi, _src = window
                temperature = round((lo + hi) / 2.0)
                temperature_source = f"midpoint of {window[2]}"
            else:
                raise PrinterError(
                    f"Cannot {action} {what}: no temperature. Pass "
                    "temperature=, or name the material (or the spool slot on "
                    "a multi-material unit) so Kiln can look one up."
                )
        else:
            try:
                temperature = float(temperature)
            except (TypeError, ValueError) as exc:
                raise PrinterError(f"temperature must be a number, got {temperature!r}.") from exc
            temperature_source = "caller"

        # Per-printer ceiling (safety profile tightens the adapter fallback).
        self._validate_temp(temperature, self._MAX_HOTEND_C, "Hotend")
        # Cold-extrusion floor.
        if temperature < MIN_EXTRUDE_TEMP_C:
            raise PrinterError(
                f"Hotend temperature {temperature:g}°C is below the "
                f"{MIN_EXTRUDE_TEMP_C:g}°C cold-extrusion floor. Feeding "
                "plastic through a cold nozzle strips the gears; the firmware "
                "would refuse the move anyway."
            )
        if window is not None:
            lo, hi, src = window
            if not lo <= temperature <= hi:
                raise PrinterError(
                    f"Hotend temperature {temperature:g}°C is outside the "
                    f"{lo:g}–{hi:g}°C window {src} reports for "
                    f"{material or 'the loaded material'}. Pass a temperature "
                    "inside the window, or a different material."
                )

        return FilamentOpPlan(
            action=action,
            temperature=temperature,
            temperature_source=temperature_source,
            slot=slot,
            material=material,
            length_mm=length_mm,
            material_window=window,
            options=dict(options or {}),
            printer_paused=paused,
        )

    def _filament_material_window(
        self, material: str | None, slot: int | None
    ) -> tuple[float, float, str] | None:
        """``(nozzle_min, nozzle_max, source)`` for the filament in play."""
        if not material:
            return None
        try:
            from kiln.gcode import _MATERIAL_TEMPS
        except ImportError:  # pragma: no cover
            return None
        key = material.strip().upper()
        for name, (lo, hi, _b_lo, _b_hi) in _MATERIAL_TEMPS.items():
            if name.upper() == key:
                return float(lo), float(hi), "Kiln's material table"
        return None

    @abstractmethod
    def _load_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend load, called AFTER the filament gate passed."""

    @abstractmethod
    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend unload, called AFTER the filament gate passed."""

    @abstractmethod
    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend purge, called AFTER the filament gate passed."""


    def wipe_nozzle(
        self,
        *,
        material: str | None = None,
        temperature: float | None = None,
        slot: int | None = None,
        **options: Any,
    ) -> FilamentOpResult:
        """Clean the nozzle tip on the machine's own wipe pad."""
        step = options.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool) or step < 1):
            raise PrinterError(f"step must be a whole number from 1, got {step!r}.")
        plan = self._prepare_filament_op(
            "wipe",
            slot=slot,
            material=material,
            temperature=temperature,
            length_mm=None,
            options=options,
        )
        return self._finish_filament_op(plan, self._wipe_nozzle_impl(plan))

    def _wipe_nozzle_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend wipe, called AFTER the filament gate passed."""
        raise FilamentHandlingUnsupported(
            f"{self.name} has no nozzle-wipe routine in Kiln. Use the printer's "
            "own screen, or start a print — its start sequence wipes on the pad."
        )

    # ------------------------------------------------------------------
    # Homing -- the one verb every printer screen has
    # ------------------------------------------------------------------

    # : Axes a successful :meth:`home_axes` COMMANDED, in this process
    _homing_commanded_axes: frozenset[str] = frozenset()

    @property
    def homing_commanded(self) -> frozenset[str]:
        """Axes this adapter has sent a homing for since it was built."""
        return self._homing_commanded_axes

    def _plate_witness(self) -> str | None:
        """A camera frame of the plate, saved to disk, or ``None`` without a camera."""
        try:
            if not getattr(self.capabilities, "can_snapshot", False):
                return None
            data = self.get_snapshot()
            if not data:
                return None
            import time as _time

            from kiln.output_paths import kiln_temp_dir

            path = Path(kiln_temp_dir()) / f"kiln_plate_{int(_time.time())}.jpg"
            path.write_bytes(data)
            return str(path)
        except Exception:  # noqa: BLE001 -- a witness is a courtesy; its absence is reported
            return None

    def _plate_raise_block(self, clearance: float | None) -> tuple[Any, float | None, bool]:
        """``(plate_state, raise_clearance_mm, blocked)`` for a raise-and-travel."""
        from kiln.plate_state import plate_occupancy

        state = plate_occupancy(self)
        height = state.job.max_z_mm if (state.occupied and state.job) else None
        blocked = clearance is not None and height is not None and height >= clearance
        return state, clearance, blocked

    def _detour_around_part(
        self,
        state: Any,
        *,
        station: dict[str, Any] | None,
        action: str,
        clearance_mm: float | None,
    ) -> tuple[list[HomeStep] | None, str | None]:
        """kiln-pro's path around the recorded part, read line by line before it may run."""
        from kiln.plate_state import plan_motion_around_plate
        from kiln.printers.home_around_part import _PlanRefused, validate_plan

        motion = self.motion_facts()
        model = motion.printer_id if motion is not None else (self.declared_printer_model() or None)
        plan = plan_motion_around_plate(state, station, action=action, clearance_mm=clearance_mm, printer_model=model)
        if not plan:
            return None, None
        job = state.job
        why: str | None = None
        if motion is None:
            why = ("Kiln has no motion record for this printer to read that path against -- what finds Z and "
                   "what the firmware does with an unhomed move are looked up by model")
        elif job is None or job.max_z_mm is None:
            why = "the record does not say how tall the part is, and the height is the whole question"
        elif not job.footprint_mm or len(job.footprint_mm) != 4:
            why = "the record does not say where on the plate the part stands, so no line of the path can be shown to miss it"
        else:
            try:
                validate_plan(plan, motion, footprint=[float(v) for v in job.footprint_mm], height=float(job.max_z_mm))
            except _PlanRefused as refused:
                why = refused.reason
        if why is None:
            return [HomeStep(number=i + 1, **step) for i, step in enumerate(plan)], None
        logger.warning("%s: refusing the planner's %s path around the part on %s: %s", self.name, action, model, why)
        return None, why

    def _record_plate_clear(self, options: dict[str, Any]) -> None:
        """Write down a person's ``plate_clear=True`` -- for home and park alike."""
        if options.get("plate_clear") is not True or options.get("plan_only"):
            return
        try:
            from kiln.plate_state import mark_clear

            mark_clear(self, "human")
        except Exception:  # noqa: BLE001 -- the record never blocks the motion
            logger.debug("plate_clear could not be recorded", exc_info=True)

    def _plate_gate(
        self,
        options: dict[str, Any],
        *,
        station: dict[str, Any] | None = None,
        clearance_mm: float | None = None,
        action: str,
        touches_plate: bool = False,
        allow_plan: bool = True,
        contact: str | None = None,
        fallback: str | None = None,
        refusal_reason: str | None = None,
    ) -> list[HomeStep] | None:
        """Refuse a motion the plate record says would meet a part."""
        from kiln.plate_state import raise_clearance_mm

        if options.get("plate_clear") is True:
            return None
        if clearance_mm is None:
            clearance_mm = raise_clearance_mm(station)
        state, clearance, blocks_raise = self._plate_raise_block(clearance_mm)
        if state.clear and not touches_plate:
            return None
        model = self.declared_printer_model().lower() or self.name
        height = state.job.max_z_mm if (state.occupied and state.job) else None
        if not touches_plate and not blocks_raise:
            return None
        around = ""
        if state.occupied and allow_plan:
            detour, refused = self._detour_around_part(state, station=station, action=action, clearance_mm=clearance)
            if detour is not None:
                return detour
            around = self._refused_path_clause(refused)
        tool, cli = {
            "wipe": ("wipe_nozzle", kiln_command("filament wipe --plate-clear")),
            "park": ("park_head", kiln_command("park --plate-clear")),
        }.get(action, ("home_axes", kiln_command("home --plate-clear")))
        offer = None
        if blocks_raise and not touches_plate and not options.get("plan_only"):
            from kiln.plate_state import offer_look

            offer = offer_look(self, state, say_so=f"`{kiln_command('plate clear')}`, or plate_clear=true on {tool}")
            witness = offer.snapshot_path
        else:
            witness = None if options.get("plan_only") else self._plate_witness()
        look = (f" -- look at {witness} first" if witness else
                " -- this printer has no camera Kiln can read, so look at the plate yourself")
        contact = contact or (
            "homes Z by pressing the nozzle onto the PLATE (its own sequence: 'find a soft place to home')"
        )
        fallback = fallback or 'Until then, home X and park still work: axes="XY", or park_head.'
        say_so = (
            f"then say so on the call: plate_clear=true on {tool} ({cli}) -- "
            f"the press asks every time; `{kiln_command('plate clear')}` records it for the row checks only. "
            f"{fallback}"
            if touches_plate and not blocks_raise
            else f"then say so: `{kiln_command('plate clear')}`, or plate_clear=true on {tool}."
        )
        if blocks_raise:
            assert clearance is not None and height is not None
            message = (
                f"Refusing to {action} {model}: {state.describe()}, and the first motion lifts the "
                f"head only {clearance:g} mm before it crosses the row -- the part is taller than that, "
                f"and on this family a travel collision raises no fault.{around} "
                + (offer.sentence if offer is not None else f"Clear the plate{look}, {say_so}")
            )
        elif state.occupied:
            message = f"{model} {contact}, and {state.describe()}.{around} Clear the plate{look}, {say_so}"
        elif state.clear:
            message = (
                f"{model} {contact}. {state.describe()[0].upper()}{state.describe()[1:]}, "
                "but that record cannot see a print started from the printer's own screen, so the "
                f"press asks every time: look{look}, then call again with plate_clear=true on {tool} "
                f"({cli}). {fallback.replace('Until then', 'Without asking', 1)}"
            )
        else:
            message = (
                f"{model} {contact}. Kiln has no record of what is on the plate and will "
                f"only do that once a person has confirmed it is empty{look}, then call again with "
                f"plate_clear=true on {tool} ({cli}). {fallback.replace('Until then', 'Without that', 1)}"
            )
        self._count_motion_refusal(
            "PLATE_CLEAR_REQUIRED",
            refusal_reason or ("on_plate" if touches_plate else "plate_occupied"),
        )
        raise PlateClearRequired(message, snapshot_path=witness)

    def declared_printer_model(self) -> str:
        """The printer_model this adapter was built with, as the person wrote it."""
        own = str(getattr(self, "_printer_model", "") or "").strip()
        if own:
            return own
        return str(getattr(self, "_safety_profile_id", "") or "").strip()

    def motion_facts(self) -> Any:
        """The catalogue's motion block for the declared model, or ``None``."""
        declared = self.declared_printer_model()
        if not declared:
            return None
        from kiln.motion_facts import motion_facts_for

        facts = motion_facts_for(declared)
        if facts is None:
            from kiln.printer_profile_ids import map_printer_hint_to_profile_id

            mapped = map_printer_hint_to_profile_id(declared)
            facts = motion_facts_for(mapped) if mapped else None
        if facts is None:
            return None
        return self._fill_motion_from_machine(facts)

    def _fill_motion_from_machine(self, facts: Any) -> Any:
        """Let the connected machine's own config settle what the vendor left per unit."""
        if not facts.needs_machine_fill():
            return facts
        cache_key = "_motion_machine_source"
        source = getattr(self, cache_key, _UNREAD)
        if source is _UNREAD:
            try:
                source = self._read_machine_motion_source()
            except Exception as exc:  # noqa: BLE001 -- a fact Kiln cannot read is a fact it does not have
                logger.debug("motion: %s could not read itself: %s", self.name, exc)
                source = None
            if source is not None:
                # Only an answer is kept
                setattr(self, cache_key, source)
        if source is None:
            return facts
        from kiln.machine_motion import fill_from_machine

        kind, payload = source
        return fill_from_machine(facts, kind, payload)

    def _read_machine_motion_source(self) -> tuple[str, Any] | None:
        """What this machine can say about its own motion, and in which dialect."""
        reader = getattr(self, "get_printer_config", None)
        if reader is None:
            return None
        config = reader()
        return None if config is None else ("klipper_config", config)

    def read_machine_limits(self) -> dict[str, Any] | None:
        """The limits this machine states about itself, as a proposal, or ``None``."""
        reader = getattr(self, "get_printer_config", None)
        if reader is None:
            return None
        from kiln.printers.engagement import internal_read

        try:
            with internal_read():
                config = reader()
        except Exception as exc:  # noqa: BLE001 -- a limit Kiln cannot read is one it does not propose
            logger.debug("limits: %s could not read its config: %s", self.name, exc)
            return None
        from kiln.machine_limits import limits_from_klipper_config

        return limits_from_klipper_config(config)

    def _count_motion_refusal(self, code: str, reason: str) -> None:
        """One tally per refusal, keyed by model, code and why -- the only trace it leaves."""
        try:
            from kiln.daily_stats import record_motion_refusal

            record_motion_refusal(self.declared_printer_model() or None, code, reason)
        except Exception as exc:  # noqa: BLE001 -- a counter never blocks a refusal
            logger.debug("motion refusal count skipped: %s", exc)

    def _declare_model_text(self) -> str:
        """The door for an install that never said which printer this is."""
        model = self.declared_printer_model()
        if model:
            from kiln.printers.bed_fit import owner_stated_build_volume

            if owner_stated_build_volume(model) is not None:
                return (
                    f"{model!r} is a printer outside Kiln's catalogue, set up on this machine. Kiln has no "
                    "motion record for it, and does not move a head it has no record for."
                )
            return (
                f"Kiln has no motion record for {model!r} -- not a catalogue key it knows. "
                f"Set it with `{kiln_command('set-model')}` or the `set_printer_model` tool, which accept the printer's "
                "name or a project saved from your slicer and write the catalogue's own key "
                "(KILN_PRINTER_MODEL for the env door)."
            )
        return (
            "Kiln does not know which printer this is: no printer_model is declared for it. "
            "The facts that decide whether the head may move -- which part moves in Z, how Z is found "
            "and where, whether the firmware refuses an unhomed move -- are looked up by model. Say "
            f"which printer it is with `{kiln_command('set-model')}` or the `set_printer_model` tool -- by name, or "
            "from a project saved from your slicer (KILN_PRINTER_MODEL for the env door) -- and call again."
        )

    @staticmethod
    def _refused_path_clause(why: str | None) -> str:
        """The sentence a plate refusal carries when a plan came back and was refused."""
        if not why:
            return ""
        return (
            " Kiln Pro's planner offered a path around it, and Kiln will not run that path: "
            + why.rstrip(". ") + "."
        )

    def _motion_gate(
        self, options: dict[str, Any], *, axes: str, action: str,
    ) -> tuple[Any, list[HomeStep] | None]:
        """The generic backend's answer to "may the head move?", before any G-code."""
        motion = self.motion_facts()
        if options.get("plate_clear") is True:
            return motion, None
        wants_z = "Z" in axes.upper()
        if motion is None:
            if wants_z or action == "park":
                self._count_motion_refusal(
                    "PRINTER_MODEL_REQUIRED",
                    "unknown_key" if self.declared_printer_model() else "undeclared",
                )
                raise ModelDeclarationRequired(self._declare_model_text())
            return None, None
        detour: list[HomeStep] | None = None
        if wants_z and motion.z_home_descends_onto_plate:
            detour = self._plate_gate(options, station=None, action=action, touches_plate=True,
                                      allow_plan=True, contact="homes Z by " + motion.describe_z_home(),
                                      refusal_reason="on_plate" if motion.z_home_known else "unknown_method")
        if action == "park":
            from kiln.plate_state import plate_occupancy

            state = plate_occupancy(self)
            if state.occupied:
                detour, refused = self._detour_around_part(state, station=None, action="park", clearance_mm=None)
                if detour is not None:
                    return motion, detour
                say_so = f"`{kiln_command('plate clear')}`, or plate_clear=true on park_head"
                if options.get("plan_only"):
                    witness = None
                    settle = ("Clear the plate -- look at it yourself -- "
                              f"then say so: {say_so}.")
                else:
                    from kiln.plate_state import offer_look

                    offer = offer_look(self, state, say_so=say_so)
                    witness, settle = offer.snapshot_path, offer.sentence
                self._count_motion_refusal("PLATE_CLEAR_REQUIRED", "plate_occupied")
                raise PlateClearRequired(
                    f"Refusing to park {motion.printer_id}: {state.describe()}, and on this backend the "
                    f"park is the firmware's own X/Y home, which travels sideways at whatever height the head "
                    f"has now -- Kiln cannot read that height here.{self._refused_path_clause(refused)} "
                    f"{settle}",
                    snapshot_path=witness,
                )
        return motion, detour

    def home_axes(self, *, axes: str = "XYZ", **options: Any) -> HomeResult:
        """Home the head -- what the Home button on the printer's screen does."""
        wanted = "".join(sorted({c for c in axes.upper() if c in "XYZ"}, key="XYZ".index))
        step = options.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool) or step < 1):
            raise PrinterError(f"step must be a whole number from 1, got {step!r}.")
        if not wanted:
            raise PrinterError(f"Nothing to home: axes {axes!r} names none of X, Y, Z.")
        state = self.get_state()
        if state.effective_state == PrinterStatus.PRINTING and not options.get("plan_only"):
            raise PrinterError(
                "Refusing to home while a print is running. Pause or cancel it "
                "first, or wait for it to finish."
            )
        if state.effective_state == PrinterStatus.PAUSED and not options.get("plan_only"):
            raise PrinterError(
                "Refusing to home while a print is paused: the head is parked "
                "over the part and homing travels. Resume or cancel the print "
                "first."
            )
        self._record_plate_clear(options)
        result = self._home_axes_impl(wanted, dict(options))
        if result.success:
            self._homing_commanded_axes = self._homing_commanded_axes | frozenset(result.homed_axes)
        return result

    def _read_homed_axes(self) -> set[str] | None:
        """Which axes the FIRMWARE says are homed, or ``None`` when it cannot say."""
        return None

    def homed_axes_now(self) -> set[str] | None:
        """Which axes the firmware says are homed, asked over the wire NOW."""
        return self._read_homed_axes()

    def homed_axes_field(self) -> str | None:
        """The firmware field :meth:`homed_axes_now` reads, named the way the firmware names it."""
        return getattr(self, "_homed_axes_field", None)

    def _z_lifts_before_home(self) -> bool | None:
        """Whether the firmware's own homing routine lifts Z before X/Y move."""
        return None

    def _home_axes_impl(self, axes: str, options: dict[str, Any]) -> HomeResult:
        """Backend homing, called AFTER the gate passed."""
        if not self.capabilities.can_send_gcode:
            raise HomingUnsupported(
                f"{self.name} cannot home through Kiln: this backend does not "
                "accept G-code. Use the printer's own screen's jog controls instead -- Z UP first, then X and Y, with your eyes on the plate. The screen's Home button descends the nozzle to the bed and is the wrong tool with a part on the plate."
            )
        motion, detour = self._motion_gate(options, axes=axes, action="home")
        if detour is not None:
            from kiln.printers.motion_plan import run_home_plan

            return run_home_plan(
                self, {"printer_id": self.declared_printer_model() or self.name},
                axes=axes, options=options, action="home", steps=detour,
            )
        command = "G28" if axes == "XYZ" else "G28 " + " ".join(axes)
        plan = [HomeStep(
            number=1, label="home " + " ".join(axes),
            you_will_see=self._describe_home_routine(axes, motion),
            stops_when="each axis reaches its endstop or probe; the firmware decides",
            gcode=[command],
        )]
        step = options.get("step")
        if options.get("plan_only"):
            return HomeResult(
                success=True, outcome="accepted", axes=axes, homed_axes=[],
                message=(f"Plan only -- nothing sent. One step: {command}, the firmware's own routine. "
                         + self._describe_home_routine(axes, motion)),
                mechanism="gcode", sequence_source="firmware_home_routine",
                steps=[p.to_dict() for p in plan], step_sent=None, next_step=plan[0].to_dict(),
                details={"gcode": [command], "sent": False,
                         "motion": motion.to_dict() if motion is not None else None},
            )
        if step is not None and step != 1:
            raise PrinterError(f"This backend homes in one step; step {step} does not exist.")
        verdict = CommandVerdict.coerce(self.send_gcode([command]), what="homing")
        if not verdict.ok:
            return HomeResult(
                success=False, outcome="failed", axes=axes,
                message=f"The printer refused {command}: {verdict.message}",
                mechanism="gcode", sequence_source="firmware_home_routine",
                details={"gcode": [command], "verdict": verdict.to_dict()},
            )
        homed = self._read_homed_axes()
        lifts = self._z_lifts_before_home()
        position = self.get_tool_position()
        details: dict[str, Any] = {"gcode": [command], "verdict": verdict.to_dict()}
        if homed is not None:
            details["firmware_homed_axes"] = sorted(homed)
        if lifts is not None:
            details["z_lifts_before_home"] = lifts
        wanted = set(axes)
        if homed is not None and wanted <= homed:
            outcome, source = "confirmed", "firmware_homed_flag"
            verified = f"The firmware reports {', '.join(sorted(homed))} homed."
        elif homed is not None:
            outcome, source = "accepted", "firmware_homed_flag_partial"
            missing = ", ".join(sorted(wanted - homed))
            verified = (
                f"The firmware reports {', '.join(sorted(homed)) or 'no axis'} homed and "
                f"not {missing} -- read printer_status again; the routine may still be running."
            )
        else:
            outcome, source = "accepted", "not_read_back"
            verified = f"{verdict.message}"
        lift_note = {
            True: " Its configuration lifts Z before X and Y move.",
            False: " Its configuration does NOT lift Z before X and Y move.",
            None: " Kiln cannot see whether that routine lifts Z before X and Y move; the printer's own configuration decides.",
        }[lifts]
        if position:
            resting: dict[str, Any] = {**{k: v for k, v in position.items() if k in ("x", "y", "z")}, "source": "the firmware's own position report"}
        else:
            resting = {"described": "the firmware's home position for the axes sent"}
        return HomeResult(
            success=True,
            outcome=outcome,
            message=(
                f"Sent {command} -- the firmware's own homing routine on {self.name}, not a path "
                f"Kiln chose.{lift_note} {verified}"
            ),
            axes=axes,
            homed_axes=list(axes),
            mechanism="gcode",
            sequence_source="firmware_home_routine",
            resting_position=resting,
            steps=[p.to_dict() for p in plan], step_sent=1 if step else None, next_step=None,
            details={**details, "verification_source": source,
                     "motion": motion.to_dict() if motion is not None else None},
        )

    def _describe_home_routine(self, axes: str, motion: Any) -> str:
        """What the person will see when the firmware's own routine runs."""
        parts = ["the firmware runs its own homing routine: each axis travels to its endstop"]
        if motion is None:
            parts.append("Kiln has no motion record for this model, so it assumes the Z home descends onto the plate and that the head may travel sideways before Z is known")
            return "; ".join(parts)
        parts.append(f"the catalogue record for {motion.printer_id} says")
        read = motion.machine_read_fields
        if read:
            parts.append("read off this machine itself: " + ", ".join(read))
        if "Z" in axes.upper():
            parts.append("Z homes by " + motion.describe_z_home())
            if motion.z_carrier_inferred and motion.xy_layout:
                parts.append(f"the vendor calls this a {motion.xy_layout} layout; Kiln infers the {motion.z_carrier} carries Z")
        caveat = motion.blind_travel_caveat()
        if caveat:
            parts.append(caveat)
        if motion.unhomed_move_policy == "refused":
            parts.append("this firmware refuses any move until the axes are homed")
        return "; ".join(parts)

    def park_head(self, **options: Any) -> HomeResult:
        """Move the head somewhere safe, away from the plate -- and stay there."""
        step = options.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool) or step < 1):
            raise PrinterError(f"step must be a whole number from 1, got {step!r}.")
        state = self.get_state()
        if state.effective_state == PrinterStatus.PRINTING and not options.get("plan_only"):
            raise PrinterError(
                "Refusing to park while a print is running. Pause or cancel it "
                "first, or wait for it to finish."
            )
        if state.effective_state == PrinterStatus.PAUSED and not options.get("plan_only"):
            raise PrinterError(
                "Refusing to park while a print is paused: the firmware has "
                "already parked the head for the pause, and Kiln does not travel "
                "over a part mid-print. Resume or cancel the print first."
            )
        self._record_plate_clear(options)
        result = self._park_head_impl(dict(options))
        result.action = "park"
        if result.success and not options.get("plan_only"):
            self._homing_commanded_axes = self._homing_commanded_axes | frozenset(result.homed_axes)
        return result

    def _park_head_impl(self, options: dict[str, Any]) -> HomeResult:
        """Backend park, called AFTER the gate passed."""
        if not self.capabilities.can_send_gcode:
            raise HomingUnsupported(
                f"{self.name} cannot park through Kiln: this backend does not accept G-code. "
                "Use the printer's own screen's jog controls instead -- Z UP first, then X and Y, with your "
                "eyes on the plate."
            )
        motion, detour = self._motion_gate(options, axes="XY", action="park")
        if detour is not None:
            from kiln.printers.motion_plan import run_home_plan

            result = run_home_plan(
                self, {"printer_id": self.declared_printer_model() or self.name},
                axes="XY", options=options, action="park", steps=detour,
            )
            result.action = "park"
            return result
        if motion is not None and motion.z_home_descends_onto_plate and options.get("plate_clear") is not True:
            result = self._home_axes_impl("XY", options)
            result.action = "park"
            if result.success and not options.get("plan_only"):
                result.message = (
                    "Parked at the firmware's own X/Y home position, Z untouched -- on this machine "
                    f"Z homes by {motion.describe_z_home()}, so Kiln did not send it. " + result.message
                )
            return result
        result = self._home_axes_impl("XYZ", options)
        result.action = "park"
        if result.success and not options.get("plan_only"):
            result.message = (
                "Parked at the firmware's own home position -- on this backend the home "
                "IS the park, chosen by whoever configured the machine. " + result.message
            )
        return result

    def purge_station(self) -> dict[str, Any] | None:
        """This machine's verified purge and wipe positions, or ``None``."""
        printer_id = str(getattr(self, "_printer_model", "") or "").strip().lower()
        if not printer_id:
            return None
        try:
            from kiln.printer_intelligence import _profiles_for_caller
            from kiln.printers.bed_fit import _printer_id_candidates

            profiles = _profiles_for_caller()
            for candidate in _printer_id_candidates(printer_id):
                profile = profiles.get(candidate)
                if profile is not None and isinstance(profile.purge_station, dict):
                    station = dict(profile.purge_station)
                    station["printer_id"] = candidate
                    return station
        except Exception:  # noqa: BLE001 -- a missing fact is a refusal downstream, never a crash here
            logger.debug("purge station unavailable for %r", printer_id, exc_info=True)
        return None

    def _station_supports(self, station: dict[str, Any] | None, capability: str) -> tuple[bool, str]:
        """Whether this backend may drive *capability* from *station*, and why not."""
        model = str(getattr(self, "_printer_model", "") or "").strip().lower()
        if station is None:
            return False, (
                f"Kiln has no verified position record for {model}" if model
                else "no printer_model is declared in config.yaml, so Kiln cannot look up a position record"
            )
        return False, (
            f"the {self.name} backend has no motion sequence that reads {model}'s position record; "
            "the figures are on file, the sequence is not written for this backend"
        )

    def _reported_position(self) -> dict[str, float] | None:
        """The head's coordinates as the backend reports them, or ``None``."""
        try:
            pos = self.get_tool_position()
        except Exception:  # noqa: BLE001 -- a position is a courtesy in a purge report, never a blocker
            return None
        return dict(pos) if isinstance(pos, dict) and pos else None

    def _purge_placement(self, plan: FilamentOpPlan) -> dict[str, Any]:
        """Where a purge is about to go, as the caller must be told."""
        model = str(getattr(self, "_printer_model", "") or "").strip().lower()
        station = self.purge_station()
        if plan.printer_paused:
            return {
                "status": "in_place",
                "printer_id": model or None,
                "position": self._reported_position(),
                "wiped": None,
                "reason": (
                    "the print is paused and the head is parked over the part; "
                    "Kiln does not travel mid-print"
                ),
            }
        ok, why = self._station_supports(station, "purge")
        if not ok:
            return {
                "status": "in_place",
                "printer_id": (station or {}).get("printer_id") or model or None,
                "position": self._reported_position(),
                "wiped": None,
                "reason": why.rstrip(". "),
            }
        from kiln.plate_state import raise_clearance_mm

        plate, clearance, blocked = self._plate_raise_block(raise_clearance_mm(station))
        if blocked:
            assert clearance is not None
            return {
                "status": "in_place",
                "printer_id": (station or {}).get("printer_id") or model or None,
                "position": self._reported_position(),
                "wiped": None,
                "plate": plate.to_dict(),
                "reason": (
                    f"{plate.describe()}, taller than the {clearance:g} mm raise the travel to "
                    "the chute starts with, so the head stayed where it is; clear the plate and "
                    f"run `{kiln_command('plate clear')}` to park over the chute again"
                ),
            }
        chute = station.get("chute") or {}
        return {
            "status": "parked",
            "printer_id": station["printer_id"],
            "position": {"x_mm": chute.get("x_park_mm")},
            "wiped": "chute wiper",
            "reason": (
                f"the position {station['printer_id']}'s own start sequence "
                "flushes at, off the bed edge"
            ),
        }

    @staticmethod
    def _placement_sentence(placement: dict[str, Any]) -> str:
        """The one sentence every purge answer carries: where it went."""
        if placement.get("status") == "parked":
            pos = placement.get("position") or {}
            x = pos.get("x_mm")
            where = f"X{x:g} mm" if isinstance(x, (int, float)) else "the purge chute"
            after = placement.get("after") or "the head is still parked there"
            return (
                f"Parked over the purge chute first ({where} — "
                f"{placement.get('reason')}); {after}. No pad wipe — "
                "wipe_nozzle does that."
            )
        pos = placement.get("position")
        if pos:
            coords = " ".join(
                f"{k.removesuffix('_mm').upper()}{v:g}"
                for k, v in pos.items()
                if isinstance(v, (int, float))
            )
            at = f"at the head's current position ({coords})"
        else:
            at = "at the head's current position, which this printer does not report"
        return (
            f"Extruded in place {at} — no travel to a purge position and no "
            f"wipe, because {placement.get('reason')}. Expect a tail hanging "
            "from the nozzle; clear it before printing."
        )

    # -- shared G-code sequence -------------------------------------------

    def _wait_for_hotend_below(
        self, threshold: float, *, timeout: float, poll: float = 2.0
    ) -> tuple[bool, float | None]:
        """Poll ``get_state`` until the hotend reads at or below *threshold*."""
        deadline = time.monotonic() + timeout
        last: float | None = None
        while True:
            try:
                last = self.get_state().tool_temp_actual
            except PrinterError:
                last = None
            if last is not None and last <= threshold:
                return True, last
            if time.monotonic() >= deadline:
                return False, last
            time.sleep(poll)

    def _wait_for_hotend(
        self,
        target: float,
        *,
        timeout: float = HOTEND_HEAT_TIMEOUT_S,
        tolerance: float = 5.0,
        poll: float = 2.0,
    ) -> tuple[bool, float | None]:
        """Poll ``get_state`` until the hotend is within *tolerance* of *target*."""
        deadline = time.monotonic() + timeout
        last: float | None = None
        while True:
            try:
                last = self.get_state().tool_temp_actual
            except PrinterError:
                last = None
            if last is not None and last >= target - tolerance:
                return True, last
            if time.monotonic() >= deadline:
                return False, last
            time.sleep(poll)

    def _gcode_filament_move(
        self,
        plan: FilamentOpPlan,
        *,
        signed_length_mm: float,
        mechanism: str,
        heat_timeout: float = HOTEND_HEAT_TIMEOUT_S,
        pre_move_check: Any | None = None,
        feed_mm_min: float = FILAMENT_FEED_RATE_MM_MIN,
        pre_gcode: list[str] | None = None,
        post_gcode: list[str] | None = None,
        placement: dict[str, Any] | None = None,
    ) -> FilamentOpResult:
        """Heat, wait for the thermistor, then one relative E move."""
        target = plan.temperature
        if placement is None and signed_length_mm > 0:
            placement = self._purge_placement(plan)
        parked = bool(placement and placement.get("status") == "parked")
        base_details: dict[str, Any] = {"mechanism": mechanism}
        if placement is not None:
            base_details["purge_station"] = placement
        if pre_gcode:
            base_details["pre_gcode"] = list(pre_gcode)
            try:
                self.send_gcode(list(pre_gcode))
            except PrinterError as exc:
                return FilamentOpResult(
                    success=False,
                    action=plan.action,
                    message=(
                        f"The printer rejected the move to the purge station: "
                        f"{exc}. Nothing was heated or extruded."
                    ),
                    extrusion_verified=False,
                    verification_source="firmware_rejected_move",
                    error_hint=str(exc),
                    slot=plan.slot,
                    material=plan.material,
                    temperature=target,
                    details=base_details,
                )
        still_parked = " The head was parked over the purge chute first and is still there." if parked else ""
        try:
            self.set_tool_temp(target)
            self._hold_heater(target)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=f"Could not set the hotend to {target:g}°C: {exc}{still_parked}",
                extrusion_verified=False,
                verification_source="heater_command_rejected",
                error_hint=str(exc),
                slot=plan.slot,
                material=plan.material,
                temperature=target,
                details=base_details,
            )
        reached, reading = self._wait_for_hotend(target, timeout=heat_timeout)
        if not reached:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=(
                    f"The hotend did not reach {target:g}°C within "
                    f"{heat_timeout:g}s (last reading "
                    f"{'unknown' if reading is None else f'{reading:g}°C'}). "
                    f"Nothing was extruded.{still_parked}"
                ),
                extrusion_verified=False,
                verification_source="thermistor",
                slot=plan.slot,
                material=plan.material,
                temperature=target,
                details={**base_details, "last_hotend_reading": reading},
            )
        if pre_move_check is not None:
            refusal = pre_move_check()
            if refusal:
                reason, source = refusal
                return FilamentOpResult(
                    success=False,
                    action=plan.action,
                    message=f"Not extruding: {reason}{still_parked}",
                    extrusion_verified=False,
                    verification_source=source,
                    error_hint=reason,
                    slot=plan.slot,
                    material=plan.material,
                    temperature=target,
                    details={**base_details, "hotend_reading": reading},
                )
        commands = [
            "M83",
            f"G1 E{signed_length_mm:g} F{feed_mm_min:g}",
            *(post_gcode or []),
            "M82",
        ]
        try:
            self.send_gcode(commands)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=f"The printer rejected the {plan.action} move: {exc}{still_parked}",
                extrusion_verified=False,
                verification_source="firmware_rejected_move",
                error_hint=str(exc),
                slot=plan.slot,
                material=plan.material,
                temperature=target,
                details={**base_details, "gcode": commands},
            )
        verb = {"load": "fed", "unload": "retracted", "purge": "extruded", "wipe": "retracted"}.get(
            plan.action, "moved"
        )
        message = (
            f"Hotend at {reading:g}°C; the printer accepted a "
            f"{abs(signed_length_mm):g} mm {plan.action} ({verb} at "
            f"{feed_mm_min / 60:g} mm/s). This backend "
            "reports no extruder-flow signal, so whether plastic actually "
            "left the nozzle is not something Kiln can confirm — look at "
            "the nozzle."
        )
        if placement is not None:
            message = f"{message} {self._placement_sentence(placement)}"
        return FilamentOpResult(
            success=True,
            action=plan.action,
            message=message,
            extrusion_verified=None,
            verification_source="command_accepted_only",
            slot=plan.slot,
            material=plan.material,
            temperature=target,
            details={
                **base_details,
                "gcode": commands,
                "hotend_reading": reading,
            },
        )

    # -- fan control ------------------------------------------------------

    # : Aliases accepted for the single generic default part-cooling fan
    _PART_COOLING_FAN_ALIASES: frozenset[str] = frozenset({"part", "part_cooling", "cooling"})

    @staticmethod
    def _gcode_lines(commands: Any) -> list[str]:
        """*commands* as a list of whole G-code lines, or a loud refusal."""
        if isinstance(commands, str):
            raise PrinterError(
                "send_gcode takes a LIST of G-code lines, not a string: "
                f"{commands!r} would be sent one character at a time. "
                f"Pass [{commands!r}] instead."
            )
        lines = [str(c) for c in commands]
        if any("\n" in line for line in lines):
            raise PrinterError(
                "send_gcode: each list item must be ONE G-code line; "
                "split embedded newlines into separate items."
            )
        return lines

    def _validate_part_fan(self, node: str, percent: int) -> int:
        """Validate a generic-adapter ``set_fan`` call; return the 0-255 PWM."""
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
    def set_tool_temp(self, target: float) -> CommandVerdict | bool:
        """Set the hot-end (tool) target temperature in degrees Celsius."""

    @abstractmethod
    def set_bed_temp(self, target: float) -> CommandVerdict | bool:
        """Set the heated-bed target temperature in degrees Celsius."""

    # -- G-code ---------------------------------------------------------

    @abstractmethod
    def send_gcode(self, commands: list[str]) -> CommandVerdict | bool:
        """Send one or more G-code commands to the printer."""

    # -- webcam snapshot (optional) ------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from the printer."""
        if self._external_camera is not None:
            return fetch_external_snapshot(self._external_camera)
        return None

    def camera_needs_addon(self) -> bool:
        """Whether a picture from this printer's camera is cut from a stream, which needs Kiln's camera add-on."""
        camera = self._external_camera
        return camera is not None and _cut_from_stream(camera)

    # -- webcam streaming (optional) -----------------------------------

    def get_stream_url(self) -> str | None:
        """Return the MJPEG stream URL for the printer's webcam."""
        return _external_stream_url(self._external_camera, None)

    # -- live video (optional) -----------------------------------------

    def stream_capability(self) -> StreamCapability:
        """Whether Kiln's local relay can carry this printer's live video."""
        camera = self._external_camera
        if camera is not None:
            if camera.stream_url is None:
                return StreamCapability(
                    False,
                    None,
                    "The camera you registered gives stills only "
                    "(camera_snapshot_url). Register a camera_stream_url "
                    "for live video.",
                )
            if _is_rtsp(camera.stream_url):
                return StreamCapability(False, "rtsp", RTSP_NOT_RELAYED_REASON)
            return StreamCapability(True, "http_mjpeg")
        url = self.get_stream_url()
        if url is None:
            if getattr(self.capabilities, "can_snapshot", False):
                reason = (
                    "This printer backend has no live video stream Kiln can "
                    "relay; snapshots still work."
                )
            else:
                reason = "This printer has no camera Kiln can read."
            return StreamCapability(False, None, reason)
        if _is_rtsp(url):
            return StreamCapability(
                False,
                "rtsp",
                "This printer's stream is RTSP, which Kiln's local relay does "
                "not carry; snapshots still work.",
            )
        return StreamCapability(True, "http_mjpeg")

    def frame_source(self) -> Any | None:
        """The relay's frame source for this printer, or ``None``."""
        if not self.stream_capability().available:
            return None
        from kiln.streaming import HttpMjpegSource

        url = self.get_stream_url()
        return HttpMjpegSource(url) if url else None

    # -- camera check (optional) -----------------------------------------

    camera_check_ids: ClassVar[tuple[str, ...]] = ()

    def camera_probes(self) -> list[Any]:
        """The addresses this printer's own camera may answer on, to check."""
        return []

    # -- camera handshake (setup) ----------------------------------------

    def camera_handshake(self) -> CameraHandshake:
        """Whether this printer's camera answers, learned without a frame."""
        from kiln.camera_check import handshake_external_camera
        from kiln.printers.engagement import internal_read

        camera = self._external_camera
        if camera is not None:
            return handshake_external_camera(
                camera, timeout=min(CAMERA_HANDSHAKE_ADDRESS_TIMEOUT, CAMERA_HANDSHAKE_BUDGET)
            )
        with internal_read():
            return self._own_camera_handshake()

    def _own_camera_handshake(self) -> CameraHandshake:
        """The handshake for the printer's own camera."""
        from kiln.camera_check import NO_CAMERA_HANDSHAKE_DETAIL, handshake_probes

        if not self.camera_check_ids:
            return CameraHandshake(None, "", NO_CAMERA_HANDSHAKE_DETAIL)
        probes = self.camera_probes()
        if not probes:
            return CameraHandshake(
                False,
                ",".join(self.camera_check_ids),
                "The printer named no camera address to ask.",
            )
        return handshake_probes(
            probes,
            budget=CAMERA_HANDSHAKE_BUDGET,
            address_timeout=CAMERA_HANDSHAKE_ADDRESS_TIMEOUT,
        )


    _external_camera: ExternalCamera | None = None

    @property
    def external_camera(self) -> ExternalCamera | None:
        """The user-supplied camera registered for this printer, if any."""
        return self._external_camera

    def set_external_camera(
        self,
        *,
        snapshot_url: str | None = None,
        stream_url: str | None = None,
    ) -> None:
        """Register (or with no URLs, clear) a camera the user supplies."""
        snapshot = (
            validate_external_camera_url(snapshot_url, what="camera_snapshot_url")
            if snapshot_url
            else None
        )
        stream = (
            validate_external_camera_url(stream_url, what="camera_stream_url")
            if stream_url
            else None
        )
        self._external_camera = (
            ExternalCamera(snapshot_url=snapshot, stream_url=stream)
            if snapshot or stream
            else None
        )

    @property
    def has_camera(self) -> bool:
        """Whether :meth:`capture_snapshot` has any camera to ask."""
        return self._external_camera is not None or bool(
            getattr(self.capabilities, "can_snapshot", False)
        )

    camera_fitted_at_factory: bool | None = None

    def camera_catalogue_model(self) -> str:
        """The model Kiln's catalogue may be asked about for THIS machine's camera: the declared model."""
        return self.declared_printer_model()

    @property
    def snapshot_source(self) -> str | None:
        """``"user_supplied"``, ``"printer"``, or ``None`` when there is no camera."""
        if self._external_camera is not None:
            return "user_supplied"
        if getattr(self.capabilities, "can_snapshot", False):
            return "printer"
        return None

    # -- printer identity self-report (optional) ------------------------

    def get_printer_info(self) -> PrinterInfo | None:
        """Return the printer's self-reported model, or ``None``."""
        return None

    def get_identity_channels(self) -> dict[str, str]:
        """Every identity channel this adapter has, and what each claims."""
        return {}


    def reported_firmware_version(self) -> str | None:
        """The firmware version this printer reports, as text, or ``None``."""
        source = getattr(self, "_motion_machine_source", None)
        if isinstance(source, tuple) and len(source) == 2 and source[0] == "marlin_report":
            report = source[1]
            name = str(getattr(report, "firmware_name", "") or "").strip()
            version = str(getattr(report, "firmware_version", "") or "").strip()
            text = " ".join(part for part in (name, version) if part)
            return text or None
        return None

    def get_firmware_status(self) -> FirmwareStatus | None:
        """Check for available firmware/software updates."""
        return None

    def update_firmware(
        self,
        component: str | None = None,
    ) -> FirmwareUpdateResult:
        """Trigger a firmware or software update."""
        raise PrinterError(f"{self.name} adapter does not support firmware updates.")

    def rollback_firmware(self, component: str) -> FirmwareUpdateResult:
        """Roll back a component to its previous version."""
        raise PrinterError(f"{self.name} adapter does not support firmware rollback.")

    # -- bed mesh (optional) --------------------------------------------

    def get_bed_mesh(self) -> dict[str, Any] | None:
        """Return the current bed mesh / probe data."""
        return None

    # -- filament sensor (optional) ----------------------------------------

    def get_filament_status(self) -> dict[str, Any] | None:
        """Query the filament runout sensor status."""
        return None

    def read_active_slot(self) -> ActiveSlotReading | None:
        """Which slot the machine's multi-material unit is feeding, or ``None``."""
        return None

    _slot_observer_native: bool = False

    def _kiln_is_driving(self) -> bool:
        """Is a slot change on this machine something Kiln itself caused?"""
        now = time.monotonic()
        for stamp_name in ("_filament_command_sent_at", "_stop_sent_at"):
            stamp = getattr(self, stamp_name, 0.0) or 0.0
            if stamp and (now - stamp) < OWN_COMMAND_SETTLE_SECONDS:
                return True
        try:
            from kiln.printers.engagement import current, machine_id

            engaged = current()
            return engaged is not None and engaged.machine == machine_id(self)
        except Exception:  # noqa: BLE001 -- no engagement record is "not driving"
            return False

    def _reconcile_cutter_print(self, job: str) -> None:
        """A print Kiln started has ended: hand what it was charged, and what the machine showed, to the cutter bridge."""
        held = getattr(self, "_cutter_print", None)
        self._cutter_print = None
        if not isinstance(held, dict) or held.get("planned") is None:
            return
        try:
            from kiln._pro_cutter_bridge import record_print_reconciliation

            record_print_reconciliation(
                self.name,
                job=str(held.get("file") or job),
                planned=int(held.get("planned") or 0),
                observed=int(held.get("observed") or 0),
                printer_model=self.declared_printer_model() or None,
            )
        except Exception:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).debug("cutter reconciliation not reported", exc_info=True)

    def read_nozzle_setting(self) -> NozzleSetting | None:
        """The nozzle this printer holds on record for itself, or ``None``."""
        return None

    def read_machine_identity(self) -> MachineIdentity | None:
        """The id this machine reports for itself, or ``None``."""
        return None

    def read_nozzle_clumping_detection(self) -> NozzleClumpingDetection | None:
        """Whether this printer's own nozzle-clumping-detection switch is on, or ``None``."""
        return None

    def get_multi_material_status(self) -> Any | None:
        """What multi-material unit this printer carries, read live."""
        return None

    # -- CNC / laser operations (optional) --------------------------------

    def set_spindle_speed(self, rpm: float) -> bool:
        """Set CNC spindle speed."""
        raise PrinterError(f"{self.name} does not support spindle control")

    def set_laser_power(self, power_percent: float) -> bool:
        """Set laser power (0--100 %)."""
        raise PrinterError(f"{self.name} does not support laser control")

    def get_tool_position(self) -> dict[str, float] | None:
        """Return current tool position ``{x, y, z, ...}``."""
        return None

    # -- file deletion --------------------------------------------------

    @abstractmethod
    def delete_file(self, file_path: str) -> bool:
        """Delete a G-code file from the printer's storage."""

    def read_print_file(self, file_name: str) -> bytes | None:
        """The bytes of *file_name* as the printer holds them, or ``None``."""
        return None

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
        """Async wrapper returning temperature data from :meth:`get_state`."""
        return await asyncio.to_thread(self.get_state)

    # -- convenience / dunder helpers -----------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


# Forward-compatible alias for non-printing fabrication devices
DeviceAdapter = PrinterAdapter


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# The base class's own concrete control methods, gated once
_install_engagement_gate(PrinterAdapter, own_methods_only=False)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# : Guards registration only
_PRINT_HOOKS_LOCK = threading.Lock()
_PRINT_STARTED_HOOKS: tuple[Callable[[Any, str], object], ...] = ()
_PRINT_ENDED_HOOKS: tuple[Callable[[str], object], ...] = ()


def register_print_started_hook(hook: Callable[[Any, str], object]) -> None:
    """Call ``hook(adapter, file_name)`` after every print Kiln starts."""
    global _PRINT_STARTED_HOOKS  # noqa: PLW0603
    with _PRINT_HOOKS_LOCK:
        if hook not in _PRINT_STARTED_HOOKS:
            _PRINT_STARTED_HOOKS = (*_PRINT_STARTED_HOOKS, hook)


def register_print_ended_hook(hook: Callable[[str], object]) -> None:
    """Call ``hook(printer_name)`` when a print is seen ending on that printer."""
    global _PRINT_ENDED_HOOKS  # noqa: PLW0603
    with _PRINT_HOOKS_LOCK:
        if hook not in _PRINT_ENDED_HOOKS:
            _PRINT_ENDED_HOOKS = (*_PRINT_ENDED_HOOKS, hook)


def _fire_print_started_hooks(adapter: Any, file_name: str) -> None:
    for hook in _PRINT_STARTED_HOOKS:
        try:
            hook(adapter, file_name)
        except Exception:  # noqa: BLE001 — a hook never fails the print it follows
            logger.warning("print-started hook %r failed", hook, exc_info=True)


def fire_print_ended_hooks(printer_name: str) -> None:
    """Announce that the print on *printer_name* was seen ending."""
    if not printer_name:
        return
    for hook in _PRINT_ENDED_HOOKS:
        try:
            hook(printer_name)
        except Exception:  # noqa: BLE001 — never breaks the status read that saw it
            logger.warning("print-ended hook %r failed", hook, exc_info=True)


def delegate_outcome_lifecycle(backend: PrinterAdapter) -> None:
    """Mark ``backend`` as an inner adapter its owner reports on behalf of."""
    backend._kiln_outcome_delegated = True  # type: ignore[attr-defined]


def name_printer_for_outcomes(adapter: Any, registered_name: str) -> None:
    """Tell an adapter the name its owner registered it under."""
    try:
        adapter._kiln_registered_name = str(registered_name)
    except Exception:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "could not name adapter for outcomes", exc_info=True
        )


def outcome_printer_name(adapter: Any) -> str:
    """The name this printer's outcomes, transitions and cancels are filed under."""
    registered = getattr(adapter, "_kiln_registered_name", None)
    if isinstance(registered, str) and registered:
        return registered
    return getattr(adapter, "name", "") or "printer"


def in_calibration_window(state: Any, job: Any) -> bool:
    """Is this printer still in its pre-extrusion routine — levelling, homing?"""
    layer = getattr(job, "current_layer", None) if job is not None else None
    completion = getattr(job, "completion", None) if job is not None else None
    if isinstance(layer, (int, float)) and layer >= 1:
        return False
    return not (isinstance(completion, (int, float)) and completion > 0)


def _current_job(adapter: PrinterAdapter) -> JobProgress | None:
    """The job the printer is (or was last) running, or ``None``."""
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


# : Longest single print whose duration is credible enough to bank
_MAX_CREDIBLE_PRINT_HOURS: float = 168.0


def _ending_was_watched(
    *,
    observation_gap_seconds: float | None,
    state_age_seconds: float | None,
    stale_after_seconds: float | None = None,
) -> bool:
    """Did Kiln really SEE this ending, or merely find out afterwards?"""
    from kiln.printers.progress_motion import WATCHED_ENDING_MAX_GAP_S

    if (
        observation_gap_seconds is None
        or observation_gap_seconds > WATCHED_ENDING_MAX_GAP_S
    ):
        return False

    # And is the reading itself the present tense?
    budget = (
        stale_after_seconds
        if isinstance(stale_after_seconds, (int, float))
        else STALE_STATE_WARN_AGE
    )
    return not (
        isinstance(state_age_seconds, (int, float)) and state_age_seconds > budget
    )


def _credible_hours(elapsed_seconds: Any) -> float | None:
    """*elapsed_seconds* as hours, or ``None`` when no number can be banked."""
    if not isinstance(elapsed_seconds, (int, float)) or elapsed_seconds <= 0:
        return None
    hours = float(elapsed_seconds) / 3600.0
    if hours > _MAX_CREDIBLE_PRINT_HOURS:
        return None
    return hours


def _record_print_duration(
    *,
    job_label: str,
    elapsed_seconds: Any,
    state_age_seconds: float | None,
    observation_gap_seconds: float | None,
    duration_semantics: str,
    stale_after_seconds: float | None = None,
    printer_name: str | None = None,
    finished: bool = False,
) -> None:
    """Bank this print's duration — if this reading can be TRUSTED."""
    hours = _credible_hours(elapsed_seconds)
    if hours is None:
        return

    watched = _ending_was_watched(
        observation_gap_seconds=observation_gap_seconds,
        state_age_seconds=state_age_seconds,
        stale_after_seconds=stale_after_seconds,
    )
    if not watched and duration_semantics != "frozen":
        return

    from kiln.daily_stats import record_print_hours_for_job

    record_print_hours_for_job(job_label, hours, reported=not watched)
    if finished:
        from kiln.learned_print_time import note_print_seconds

        note_print_seconds(job_label, hours * 3600.0, printer_name)


OWN_COMMAND_SETTLE_SECONDS: float = 180.0

SLOT_OBSERVE_MIN_INTERVAL_S: float = 20.0


def _feed_slot_observer(adapter: PrinterAdapter, state: PrinterState) -> None:
    """Watch a polled backend's feeding slot and report the changes."""
    if getattr(adapter, "_slot_observer_native", False):
        return
    if not getattr(state, "connected", False):
        return
    now = time.monotonic()
    last_asked = getattr(adapter, "_slot_observed_at", 0.0) or 0.0
    if last_asked and (now - last_asked) < SLOT_OBSERVE_MIN_INTERVAL_S:
        return
    adapter._slot_observed_at = now  # type: ignore[attr-defined]
    reading = adapter.read_active_slot()
    if reading is None:
        return
    previous = getattr(adapter, "_slot_last_seen", None)
    adapter._slot_last_seen = reading  # type: ignore[attr-defined]
    if previous is None or previous.slot == reading.slot:
        return
    if adapter._kiln_is_driving():
        held = getattr(adapter, "_cutter_print", None)
        if isinstance(held, dict):
            held["observed"] = int(held.get("observed") or 0) + 1
        return
    try:
        from kiln._pro_cutter_bridge import record_observed_switch

        record_observed_switch(
            adapter.name,
            printer_model=adapter.declared_printer_model() or None,
            from_tray=previous.slot,
            to_tray=reading.slot,
            verified=bool(reading.verified),
        )
    except Exception:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).debug("observed slot change not reported", exc_info=True)


def _feed_outcome_lifecycle(
    adapter: PrinterAdapter, state: PrinterState, *, read_at: float | None = None
) -> None:
    """Feed one ``get_state()`` result into the print-outcome lifecycle."""
    if getattr(adapter, "_kiln_outcome_delegated", False):
        return

    status = confirmed_state_of(state)
    result = getattr(state, "last_job_result", None)
    value = getattr(result, "value", None) or getattr(status, "value", "") or ""
    if not value:
        return

    from kiln.auto_record_hook import (
        ending_is_a_finish,
        fire_terminal_state_hook,
        is_terminal_transition,
        observe_state,
        reconcile_pending_outcomes,
    )
    from kiln.printers.progress_motion import forget_job_start, note_status_read

    read_gap_seconds = note_status_read(adapter)

    name = outcome_printer_name(adapter)
    if not getattr(adapter, "_base_outcomes_reconciled", False):
        adapter._base_outcomes_reconciled = True  # type: ignore[attr-defined]
        from kiln.persistence import get_db

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

    prev = observe_state(name, value, read_at=read_at)
    if is_terminal_transition(prev, value):
        job = _current_job(adapter)
        label = _job_label(job)
        adapter._reconcile_cutter_print(label or "")
        if label:
            fire_terminal_state_hook(
                prev_state=prev,
                new_state=value,
                print_error_code=0,
                printer_name=name,
                job_id=label,
                file_name=label,
            )
            _record_print_duration(
                job_label=label,
                elapsed_seconds=getattr(job, "print_time_seconds", None),
                state_age_seconds=getattr(state, "state_age_seconds", None),
                stale_after_seconds=getattr(
                    state, "state_stale_after_seconds", None
                ),
                observation_gap_seconds=read_gap_seconds,
                duration_semantics=adapter._DURATION_SEMANTICS,
                printer_name=name,
                finished=ending_is_a_finish(value),
            )
        # Stop the elapsed clock: the job it was measuring is over
        forget_job_start(adapter)


# ---------------------------------------------------------------------------
# Pre-upload safety check (called from PrinterAdapter.__init_subclass__)
# ---------------------------------------------------------------------------


class _UnsafeUpload(Exception):
    """Internal sentinel raised by the pre-upload safety check."""


def _incomplete_upload_reason(adapter: PrinterAdapter, file_path: str) -> str | None:
    """Why this file must not leave for this printer — ``None`` when it may."""
    try:
        from kiln.printers.gcode_complete import (
            declared_model_for_adapter,
            family_for_adapter,
            gcode_problems,
        )

        family = family_for_adapter(adapter)
        if family is None:
            return None
        problems = gcode_problems(
            file_path, family, printer_model=declared_model_for_adapter(adapter),
        )
        if not problems:
            return None
        return (
            f"Refused to upload {os.path.basename(file_path)}: "
            + "; ".join(problems) + "."
        )
    except Exception:  # noqa: BLE001 — a check that breaks must not block a print
        logger.debug("gcode completeness check raised; allowing upload", exc_info=True)
        return None


def upload_scan_target(adapter: PrinterAdapter) -> str | None:
    """The printer an upload to *adapter* is judged for, or ``None``."""
    printer_id = None
    with contextlib.suppress(Exception):
        printer_id = adapter.declared_printer_model() or None
    if not printer_id:
        printer_id = getattr(adapter, "_safety_profile_id", None)
    if not printer_id:
        with contextlib.suppress(Exception):
            from kiln.printer_model_resolver import resolve_printer_model
            printer_id = resolve_printer_model()
    if not printer_id:
        # Last-ditch fallback to the frozen module global
        with contextlib.suppress(Exception):
            import kiln.server as _srv
            printer_id = getattr(_srv, "_PRINTER_MODEL", None)
    return printer_id or None


def _preflight_upload_or_raise(adapter: PrinterAdapter, file_path: str) -> str:
    """Run bed-fit + homing validation and the G-code safety scan on a local file before it hits any."""
    import os
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in (".gcode", ".gco", ".g", ".3mf") and not file_path.lower().endswith(".gcode.3mf"):
            return file_path  # only validate printable files; STL/OBJ uploads skip
        printer_id = upload_scan_target(adapter)
        if printer_id:
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
        from kiln.printers.print_file_scan import (
            REFUSAL_CODE,
            dialect_for,
            refusal_sentence,
            scan_for_upload,
        )

        file_path, scan = scan_for_upload(
            file_path,
            printer_id=printer_id,
            dialect=dialect_for(getattr(adapter, "name", None), path=file_path),
        )
        if scan is not None and not scan.valid:
            raise _UnsafeUpload(
                f"Upload refused ({REFUSAL_CODE}): "
                + refusal_sentence(file_path, scan, verb="uploaded", printer_id=printer_id)
            )
        return file_path
    except _UnsafeUpload:
        raise
    except Exception:
        # Any other error — don't block the upload, just skip the check.
        return file_path
