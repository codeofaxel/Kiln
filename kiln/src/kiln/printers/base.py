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
import logging
import os
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

# Guards the one-time, per-instance setup of the idle-release bookkeeping.
# Module-level because the state it protects is what would otherwise have to
# hold its own lock — an adapter cannot lazily create a lock to guard its own
# lazy creation.  Contended only on an adapter's first connection.
_IDLE_SETUP_LOCK = threading.Lock()


def _quiet_start_contract(file_name: str, kwargs: dict[str, Any]) -> dict[str, str] | None:
    """Kiln's quiet-start header from the file about to start, or ``None``.

    The local copy is whichever the door named (``local_file_path`` and
    its siblings), the name itself when it is a path, or the slice
    ledger's wrap behind the printer-side name.  Never raises: a file
    that cannot be read carries no contract, and is judged as any other.
    """
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


class PrinterEngagementError(PrinterError):
    """Refused because Kiln is already working with a different machine.

    A subclass of :class:`PrinterError` on purpose: every caller already
    handles that, so the refusal reaches a user as a message rather than a
    traceback, on every surface, without one of them being updated first.
    ``verdict`` carries the structured form for surfaces that render.
    """

    def __init__(self, verdict: dict, *, cause: Exception | None = None) -> None:
        super().__init__(str(verdict.get("reason") or "Kiln is working with another printer."), cause=cause)
        self.verdict = verdict


class FilamentHandlingUnsupported(PrinterError):
    """This backend has no honest way to load, unload, or purge filament.

    Raised by an adapter's ``_load_filament_impl`` / ``_unload_filament_impl``
    / ``_purge_filament_impl`` INSTEAD of pretending: a backend with no
    G-code door (Prusa Link) or an unverified one (Elegoo SDCP) says so, by
    name, with what the user can do instead.  A subclass of
    :class:`PrinterError` so every existing caller renders it as a message;
    a distinct class so a test can tell "refused honestly" from "broke".
    """


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------



#: The per-adapter config cache's "not read yet" marker (``None`` means read and unusable).
_UNREAD = object()


class ModelDeclarationRequired(PrinterError):
    """Raised when a motion needs the catalogue and no printer_model names the row.

    The declaration door: most installations with a printer adapter never
    say which printer it is, and the facts that decide whether the head may
    move are looked up by model.  The message names the fix; Kiln never
    guesses a model to get past it.
    """


class HomingUnsupported(PrinterError):
    """Raised when a backend cannot home the head the way Kiln trusts.

    Not a stub that returns success: the message names what the user does
    instead (the printer's own screen, a print's start sequence).
    """


class PlateClearRequired(PrinterError):
    """Raised when a motion would press the nozzle onto the plate and no
    person has said the plate is empty.

    Carries ``snapshot_path`` when the printer's camera could be read, so
    the person looks at the plate before answering.  The answer is
    ``plate_clear=True`` on the next call -- a human's word, never a model's.
    """

    def __init__(self, message: str, *, snapshot_path: str | None = None) -> None:
        super().__init__(message)
        self.snapshot_path = snapshot_path

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
    # The reading itself has expired.  A push-cache adapter answers from the
    # last thing the printer said, and once that is older than the printer's
    # own measured reporting interval it has stopped being evidence about
    # now -- so the AGE becomes the headline rather than a footnote under a
    # confident ``idle``.  What the machine was last seen doing is not lost:
    # it moves to :attr:`PrinterState.last_known_state`.
    STALE = "stale"
    # The three ways "offline" used to be one word.  Each has a different
    # remedy, and collapsing them sent people to check a power switch for a
    # credentials problem or a connection-slot problem.
    #
    # UNAUTHORIZED: the printer answered and refused our credentials.
    # CONNECTION_LIMIT: the printer rations LAN connections and they are all
    #   taken -- most often by leftover ``kiln serve`` processes, which
    #   ``trim_serve_processes`` closes.
    # OFFLINE keeps its original, now narrower meaning: nothing answered at
    #   all, which is what a powered-off or off-network printer looks like.
    UNAUTHORIZED = "unauthorized"
    CONNECTION_LIMIT = "connection_limit"


# Every :class:`PrinterStatus` sorted into exactly one bucket, so that the
# gates which read this enum ask a named question instead of listing members
# inline.  The buckets are disjoint and their union is the whole enum --
# ``test_printer_state_vocabulary`` pins both, which is what makes adding a
# member a loud failure rather than a silent fallthrough at a dozen call
# sites.
#
# BUSY: the machine has work in flight.
# READY: free to accept work.  Only IDLE qualifies -- nothing else may.
# UNREACHABLE: Kiln cannot talk to it, and each member names its own cause.
# INDETERMINATE: reachable, or last seen reachable, but not something that
#   can be called free.  A gate that must not start a second print treats
#   these as occupied.
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
    """A :class:`PrinterStatus` from a member or its serialised word.

    Fleet listings and relayed payloads carry the state as a string, and
    each such surface used to keep its own hand-written set of which words
    mean "busy".  Those sets are how a new member silently fails to reach
    half the product; this is the one conversion they all go through.
    ``None`` for anything that is not a state Kiln knows.
    """
    if isinstance(value, PrinterStatus):
        return value
    try:
        return PrinterStatus(str(value).strip().lower())
    except (ValueError, AttributeError):
        return None


def effective_state_of(reading: Any) -> PrinterStatus | None:
    """:attr:`PrinterState.effective_state` from anything state-shaped.

    The call sites asking this hold a reading an adapter handed them, which
    may be a duck-typed stand-in without the property.  Retyping the
    ``getattr`` fallback at each of them is how one of them ends up spelling
    it differently, which is the same drift the two properties exist to stop.
    """
    return _state_attr(reading, "effective_state")


def confirmed_state_of(reading: Any) -> PrinterStatus | None:
    """:attr:`PrinterState.confirmed_state` from anything state-shaped."""
    return _state_attr(reading, "confirmed_state")


def _state_attr(reading: Any, name: str) -> Any:
    """*name* off *reading* when it is a real status, else its ``state``.

    The ``isinstance`` is load-bearing and not defensive noise.  A test
    double or a duck-typed adapter answers ANY attribute -- a ``Mock``
    hands back a fresh truthy child object -- so a plain
    ``getattr(x, name, None) or getattr(x, "state", None)`` silently
    returns that child instead of the state, and every comparison against
    it is then False.  A pre-existing resume test caught exactly this.
    """
    value = getattr(reading, name, None)
    if isinstance(value, PrinterStatus):
        return value
    return getattr(reading, "state", None)


def row_run_state(row: Any) -> str | None:
    """The run-state WORD from a serialised reading, seen through a headline.

    The dict twin of :attr:`PrinterState.effective_state`, for the readers
    that hold a payload instead of an object -- a fleet row, a relayed
    snapshot, the panel wire.  ``state`` is the HEADLINE, which two promotions
    may have taken over; ``last_known_state`` is what the machine was doing
    underneath, and only those two promotions ever set it.

    It exists because the cost of a classifier reading the bare headline is
    not cosmetic.  Neither ``stale`` nor ``error`` appears in any
    hand-written busy-word set in this codebase, so a printer whose reading
    expired mid-job, or which raised a fault mid-job, drops out of every
    "this machine is working" listing at once -- and the servers watching
    that print get trimmed.  A reader asking "is anything wrong" still wants
    the headline; this is only for readers asking what the machine is doing.
    """
    if not isinstance(row, dict):
        return None
    last = row.get("last_known_state")
    if isinstance(last, str) and last:
        return last
    state = row.get("state")
    return state if isinstance(state, str) and state else None


def status_is_occupied(status: Any) -> bool:
    """Might a machine in *status* have work in flight?

    The conservative reading, for a caller holding only the status -- as a
    member or as its word.  ``STALE`` counts as occupied because a reading
    that has expired cannot prove the bed is clear, and the cost of being
    wrong runs one way: a refused print is a retry, a second print onto an
    occupied bed is a crash.  Callers holding the whole
    :class:`PrinterState` should prefer :attr:`PrinterState.is_occupied`,
    which can consult what the printer was last seen doing.
    """
    resolved = as_status(status)
    if resolved is None:
        return False
    return resolved in BUSY_STATES or resolved is PrinterStatus.STALE


def status_is_unreachable(status: Any) -> bool:
    """Is a machine in *status* one Kiln currently cannot see?

    True for every member of :data:`UNREACHABLE_STATES` -- powered off,
    refusing our credentials, or out of connection slots.  Deliberately
    False for ``STALE``: a printer whose reading has expired is still
    connected, and calling it unreachable would send the user to the wrong
    fix.
    """
    resolved = as_status(status)
    return resolved is not None and resolved in UNREACHABLE_STATES


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


# The FLOOR of the freshness budget, and the whole budget until there is a
# cadence to measure.  Adapters that query the printer on every call are
# current by construction; adapters that answer from a push cache (Bambu over
# MQTT, Elegoo over websocket) are only as current as the last push they were
# sent, and a push cache that stops advancing keeps answering confidently.
#
# One minute is where that stops being credible at the fastest cadence a
# printer reports at, so nothing is ever called stale sooner than this -- the
# rule Kiln shipped in 1.4.0, kept as the noise floor.
#
# Both push adapters read this constant rather than restating the number, for
# their cold-start budget AND for the cooldown ceiling that decides whether a
# cache is still worth serving, so the two cannot drift into disagreeing
# about when a cache stops being trustworthy.
STALE_STATE_WARN_AGE: float = 60.0

# ...but a fixed minute is a guess about a cadence that is not fixed.  A
# Bambu pushes roughly once a second while a print runs and far more slowly
# when it is sitting idle, so one constant is either noisy at one end or deaf
# at the other.  The budget in force is therefore MEASURED per printer, from
# that printer's own reporting interval (:class:`TelemetryCadence`), and
# these two numbers are only the guard-rails around the measurement:
#
#   floor  -- STALE_STATE_WARN_AGE.  Never warn sooner than the rule Kiln
#             already shipped, so a fast cadence cannot make this noisy.
#   ceiling -- past this, no measured cadence excuses a reading.  Fixed at
#             five minutes by a measurement: on 2026-09-03 a set_temperature
#             was accepted and the nozzle was visibly heating while the
#             freshest reading Kiln held was 435 s old and still reported
#             target 0 with the fans cooling.  A reading that cannot tell
#             whether a heater Kiln just commanded is on is not evidence,
#             whatever the printer's usual pace.
STALE_STATE_MAX_AGE: float = 300.0
# How many consecutive missed reports it takes before a reading is stale.
# Three is the ordinary "we have missed a beat, and another, and another"
# threshold; one missed push is a dropped packet, not a silent printer.
STALE_CADENCE_MULTIPLIER: float = 3.0
# How many recent intervals the cadence is measured over.  Long enough that
# one hiccup cannot move the median, short enough to follow a printer moving
# between idle and printing.
_CADENCE_WINDOW: int = 12


class TelemetryCadence:
    """How often a given printer ACTUALLY reports, measured from its pushes.

    A push-transport adapter (Bambu over MQTT, Elegoo over websocket) calls
    :meth:`record` each time a message carrying the run state arrives.  The
    gaps between those calls are this printer's real reporting interval, and
    :meth:`stale_after_seconds` turns them into the age past which the
    adapter stops presenting its cache as the present tense.

    Nothing is assumed before there is something to measure: with no samples
    the budget is :data:`STALE_STATE_WARN_AGE`, exactly the fixed rule this
    replaces.  Instances are guarded by their owner's state lock; the class
    holds no lock of its own.
    """

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
        # A gap longer than the ceiling IS the outage this measurement exists
        # to catch.  Feeding it back in would widen the budget by exactly the
        # failure, so the next outage has to be longer still to be noticed.
        if gap <= 0 or gap > STALE_STATE_MAX_AGE:
            return
        self._gaps.append(gap)
        if len(self._gaps) > self._window:
            del self._gaps[: len(self._gaps) - self._window]

    @property
    def observed_interval_seconds(self) -> float | None:
        """This printer's typical gap between reports, or ``None`` if unmeasured.

        The median rather than the mean or the maximum: one slow push should
        not move the answer, and the question being asked is what this
        printer's ordinary pace is.
        """
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
    """A firmware error code in the form the printer's own screen shows.

    Bambu reports ``print_error`` as a 32-bit decimal; the machine's screen
    and Bambu's HMS documentation both render the same value as two
    four-hex-digit groups -- ``302022663`` is ``1200-8007``, which is what a
    user can actually search for.  Handing back the decimal gave them a
    number nobody can look up.

    ``None`` for a missing, unparseable or zero code: zero is the firmware's
    way of saying "no error", and formatting it would invent one.
    """
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
    """What to DO about an expired reading, in one sentence.

    Deliberately says nothing :func:`describe_stale_state` already says.
    That one names the age and whose tense it is; this one names the
    evidence -- how long this printer usually goes between reports -- and
    the single next step.  Two sentences that restate each other in one
    payload read as a system arguing with itself.
    """
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
    # This sentence says nothing about temperatures ON PURPOSE.  It fires on
    # a bare age against a fallback budget, which is a weaker thing than the
    # temperature floor's verdict: an idle Klipper's run-state clock passes
    # this threshold while its temperatures keep arriving every few seconds,
    # so a temperature claim here would be false exactly where it is loudest.
    # :attr:`PrinterState.temperature_note` is the honest carrier and travels
    # beside this one only when the readings really were withheld.
    return (
        f"Telemetry is {state_age_seconds:.0f}s old — the printer has not "
        f"reported since, so {str(state_label).upper()} describes then, not "
        f"now. Verify against the machine before acting."
    )


def describe_unknown_temperatures(state_age_seconds: float | None) -> str:
    """Why there is no temperature in this reading, and what to do instead.

    The sentence beside every blanked temperature field.  It names the
    display as the ONLY authority rather than "the latest number" because
    the failure it exists for is a person deciding a hotend is cool from
    a number Kiln could not vouch for (2026-09-06: 38°C quoted, 110°C on
    the screen, no gloves).  It deliberately carries no last-known value:
    a dated number is still a number a reader will act on, and nothing
    that keeps a person safe needs one -- unknown already means "assume
    hot".

    ``None`` age is the adapter naming the reading stale without measuring
    how stale; the sentence then says only that reporting has stopped.
    """
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
    """Why there is no chamber temperature from this machine, ever.

    The sentence beside the two blanked chamber fields on a model with no
    chamber sensor.  It says what the machine LACKS rather than what the
    reading lacks, because the two blanks look identical and mean opposite
    things: the trust floor's blank clears when the printer reports again,
    this one never will.  It deliberately names no number and no model --
    the firmware's placeholder is exactly the number a reader must not
    act on, and the model is already beside it in ``printer_model``.
    """
    return (
        "This printer has no chamber temperature sensor, so there is no "
        "chamber reading to report. Any chamber figure its firmware sends is "
        "a placeholder, not a measurement."
    )


def describe_unacknowledged_fault(
    code: str | None, reading: str | None = None
) -> str:
    """What a latched firmware fault means, and the one thing that clears it.

    The sentence that travels with :attr:`PrinterState.fault_note`, beside a
    headline that now reads ``error`` rather than the run state underneath.

    *code* is the fault in the form the printer's own screen shows it, and
    *reading* the adapter's plain-language gloss for that code when it has
    one -- Bambu's ``describe_bambu_filament_fault`` supplies it, and an
    adapter with no table for its firmware supplies nothing rather than a
    guess.  The remedy is named either way, because a fault whose meaning
    Kiln cannot look up still clears the same way.
    """
    shown = f" ({code})" if code else ""
    gloss = f" {reading.strip()}" if reading else ""
    return (
        f"The printer is reporting a fault{shown} that nothing has "
        f"acknowledged.{gloss}"
    )


def describe_fault_remedy(fix: str | None = None) -> str:
    """What CLEARS a latched fault, in one sentence -- after the fix, if any.

    Split from :func:`describe_unacknowledged_fault` for the reason
    :attr:`PrinterState.cause` and :attr:`PrinterState.remedy` are two fields
    and not one: what happened and what to do about it are read by different
    people at different moments, and a surface that can only show one line
    should be able to choose.  Joined, they were a single 380-character
    string that ended by telling a person reading a web page to call a tool
    they do not have.

    *fix* is the adapter's own remedy for this code when it has one -- the
    thing a person must do BEFORE clearing the fault means anything (a
    cutter blade back in its slot, a hot end unclogged).  It leads, and the
    clearing sentence follows it; without one the clearing sentence stands
    alone, as it always has.
    """
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
    """One line per fault, as the printer's own screen would put it.

    Reads :attr:`PrinterState.faults` and hands back the lines a text
    surface (the CLI, the monitor report) prints under the fault headline,
    so the two cannot spell the same fault two ways.  A code with the
    vendor's sentence on record reads ``"1200-8001: Cutting the filament
    failed. ..."``; an ``hms`` code with none reads as the bare code,
    because nothing else on the surface names it; a ``print_error`` with
    none yields no line at all, because ``fault_note`` already names that
    code and a second line saying only the number would say nothing new.
    When Kiln has a fix for an ``hms`` code (the entry carries ``remedy``),
    its ``reading`` and the fix follow the code line, indented: an HMS
    code's reading lives nowhere else on a text surface, where a
    ``print_error``'s is already the headline.  A family line is not a fix
    and earns no second line.  Empty for ``None`` and for anything that is
    not a list of entries.
    """
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


#: The fields a person might act on with their hands.  Blanked together, by
#: one rule, in :meth:`PrinterState.__post_init__`.
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
    """Which spool a multi-material unit is feeding, as the machine reports it.

    :param slot: The unit's own id for the feeding slot (``"1"``, ``"A2"``),
        or ``None`` when the machine reports that nothing is feeding.
    :param source: Where it was read (``"mqtt"``, ``"moonraker_cfs"``, ...).
    :param verified: Whether that field's meaning has been confirmed on
        hardware for this backend.  A discovered field is ``False``: its
        changes are observed and reported as unverified, and only a
        machine whose own prints reconcile against them earns a count.
    """

    slot: str | None
    source: str
    verified: bool = False


@dataclass(frozen=True)
class NozzleSetting:
    """The nozzle a printer HOLDS ON RECORD for itself, read off the machine.

    This is the machine's own setting -- what a person entered on its screen
    or in its configuration -- never a measurement: consumer printers do not
    sense the nozzle fitted to them.  Kiln keeps a separate record of what
    you told Kiln is fitted, and kiln-pro compares the two so a swapped
    nozzle the printer was never told about is caught before it prints
    (https://kiln3d.com).

    :param material: The alloy word exactly as the machine reports it
        (``"stainless_steel"``), or ``None`` when this backend holds no
        material setting at all.
    :param diameter_mm: The nozzle diameter, or ``None`` when not held.
    :param source: Which of the backend's own channels it was read from,
        e.g. ``"bambu_mqtt_report"`` / ``"klipper_configfile"``.
    :param age_seconds: How long ago the machine stated this, when the
        backend answers from a cache; ``None`` for a read made just now.
    :param stale_after_seconds: The backend's own budget for how long that
        cache is worth serving; ``None`` when there is no cache.
    :param firmware_version: The firmware the reading was taken under, when
        the backend reports one.
    """

    material: str | None
    diameter_mm: float | None
    source: str
    age_seconds: float | None = None
    stale_after_seconds: float | None = None
    firmware_version: str | None = None

    def is_empty(self) -> bool:
        return self.material is None and self.diameter_mm is None


@dataclass(frozen=True)
class NozzleClumpingDetection:
    """Whether a printer's own "nozzle clumping detection" switch is on, read
    off the machine.

    Some printers can feel for a blob on the nozzle by driving the toolhead
    off the bed and probing.  The probe is a screen switch on the machine,
    and it has a cost the printer's own screen states when it is turned on:
    the nozzle leaks a little during each probe, and that ooze lands on the
    model unless the slice carries a purge (prime) tower to absorb it.  So
    Kiln reads the switch and says so wherever a user meets printer state.
    The detector is a convenience the printer offers, not a fail-safe, and
    nothing built on this reading may present it as one.

    :param enabled: ``True`` when the probe is armed (the setting is on, or
        automatic), ``False`` when it is off; ``None`` when the printer has
        no such setting (``supported=False``) or reported a value the backend
        cannot decode.  A ``None`` is never "off": a reading nobody could
        verify must not be served as a verdict.
    :param source: Which of the backend's channels it was read from.
    :param age_seconds: How long ago the machine stated this, when the
        backend answers from a cache; ``None`` for a read made just now.
    :param stale_after_seconds: The backend's own freshness budget for
        that cache; ``None`` when there is no cache.
    :param firmware_version: The firmware the reading was taken under,
        when the backend reports one.
    :param unverified_reason: Why ``enabled`` is ``None`` -- required
        whenever it is, unless the printer reported it has no such setting,
        so an unknown always says why it is unknown.
    :param supported: Whether the printer reports that it has the setting at
        all; ``None`` when the backend cannot say.
    :param mode: ``"on"`` / ``"off"`` / ``"auto"`` (a printer that offers an
        automatic setting decides per print when to probe), or ``None``.
    """

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
    # What the printer was last seen DOING, whenever the headline answers a
    # different question.  Two promotions set it and nothing else does:
    # ``STALE`` (can this reading be trusted at all) and the fault promotion
    # (is the machine reporting something nobody has cleared).  Both take the
    # headline because both decide what a reader should do next -- but the run
    # state underneath is never discarded, because it is what keeps the
    # concurrency gates conservative and what :attr:`effective_state` hands
    # back to anything asking the "what is it doing" question.
    # ``None`` when the headline IS the run state.
    last_known_state: PrinterStatus | None = None
    # The freshness budget in force for this reading, measured from this
    # printer's own reporting cadence (:class:`TelemetryCadence`).  Reported
    # rather than kept private so a reader sees the rule and not only its
    # verdict -- and so no surface has to guess which number was applied.
    state_stale_after_seconds: float | None = None
    # Why Kiln has no current reading for this printer, and what to do
    # about it.  Set on the unreachable states and on ``STALE`` -- which is
    # why it is not called `unreachable_cause`: a stale printer is
    # connected, and a field naming it unreachable would contradict
    # :func:`status_is_unreachable` in the same payload.  Two fields because
    # the cause is for a machine to branch on and the remedy is for a person
    # to read; ``offline`` on its own was neither.
    cause: str | None = None
    remedy: str | None = None
    # Why the six temperature fields are empty, when they are empty for a
    # trust reason rather than because the printer has no such sensor.
    # Set beside the blanking in ``__post_init__``; ``None`` whenever the
    # temperatures above can be acted on.
    temperature_note: str | None = None
    # The fault in :attr:`print_error` in plain language, when the printer is
    # reporting one nobody has cleared.  Set beside the promotion to ``ERROR``
    # in ``__post_init__`` -- an adapter may supply its own firmware's reading
    # and the promotion keeps it, or supply nothing and get the generic
    # sentence, but the promotion never happens WITHOUT a sentence: a headline
    # that says "error" and nothing else is the same shrug as the "idle" it
    # replaced.  ``None`` whenever no fault is being reported.
    fault_note: str | None = None
    # What CLEARS the fault above.  Its own field, not the tail of
    # ``fault_note``, so a surface with room for one line shows what happened
    # rather than an instruction naming a tool its reader has no access to.
    # Same split, and the same reason, as ``cause`` beside ``remedy``.
    fault_remedy: str | None = None
    # Every fault code the firmware is reporting, each in the spelling the
    # printer's OWN SCREEN uses, with the vendor's sentence for it where the
    # vendor publishes one.  One entry per code: ``code`` (the screen's
    # spelling), ``kind`` (the firmware's namespace for it), ``raw`` (exactly
    # what came over the wire, under the wire's own field names), and --
    # only when on record -- ``screen_text`` with its ``source`` class.
    # A list because a printer can report several at once (Bambu latches
    # one ``print_error`` and publishes an ``hms`` array beside it), and
    # ``print_error_code`` above renders only the first of those.  ``None``
    # whenever no fault is being reported, and on every adapter that has
    # not composed one -- an empty list would claim "nothing reported" on
    # a firmware Kiln never asked.  Composed by the adapter, which is the
    # only place that knows its firmware's spelling; never invented here.
    faults: list[dict[str, Any]] | None = None
    # Whether this MACHINE has a chamber temperature sensor, as far as the
    # adapter can tell.  ``True``: a sensor produced the chamber fields.
    # ``False``: the model has none, so any number in them is not a
    # measurement and is blanked below.  ``None``: not established -- the
    # honest default for every adapter whose protocol only fills the field
    # when a named sensor exists (Klipper's ``temperature_sensor chamber``,
    # a Duet chamber heater), and for a model nobody has judged.  Only an
    # adapter whose protocol publishes the field for EVERY model regardless
    # of hardware has to say anything here, and it says so from the
    # catalogue (``has_chamber_sensor``), never from the value.
    chamber_sensor: bool | None = None
    # Why the two chamber fields are empty, when they are empty because the
    # machine has nothing to measure them with.  Its own sentence rather
    # than a clause of ``temperature_note``, because that one is about a
    # reading Kiln cannot vouch for and this one is about the hardware:
    # the first passes when the printer reports again, this one never does.
    # ``None`` whenever :attr:`chamber_sensor` is not ``False``.
    chamber_note: str | None = None

    def __post_init__(self) -> None:
        """Promote an expired reading to ``STALE``, whoever built it.

        Here rather than in each adapter so it is ONE rule.  Two push-cache
        adapters (Bambu over MQTT, Elegoo over websocket) had the same shape
        of failure and would otherwise each grow their own copy of the fix,
        which is how the tool surface and the web Monitor came to disagree
        about one printer in the first place.

        It fires only when the adapter has supplied BOTH an age and a budget
        measured for that printer.  An adapter that queries the printer on
        every call sets neither and is untouched: it is current by
        construction, and warning about it would make the signal noise.

        Then, a fault nobody has cleared becomes the HEADLINE.  Same shape
        as the promotion above and for the same reason: a fact that decides
        what the reader should do next cannot sit in a field underneath a
        state word that contradicts it.

        Then, whatever the run state: a reading Kiln cannot vouch for
        carries NO temperatures.  Not a caveat beside the number -- the
        caveats already existed on 2026-09-06 and the number was quoted
        anyway -- the field is empty, so no door can format it.  The run
        state is kept through staleness because a gate needs it to fail
        closed; a temperature has no such consumer, and the only thing a
        stale one can do is be believed.

        Before any of that: a chamber temperature from a machine with no
        chamber sensor is not a reading, whatever the firmware put in the
        field.  Measured on a Bambu A1 (2026-09-14): ``chamber_temper: 5``
        beside a 22.5 C bed, idle, freshly powered on -- an open-frame
        bed-slinger reporting a fridge-cold chamber it does not have, and
        every door quoting it.  About the MACHINE rather than this reading,
        so it runs first and independently of the trust floor below: a
        sensorless model is sensorless whether or not the cache is fresh.
        """
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

        # A fault the printer is reporting and nobody has cleared is a STATE,
        # not a field.  Measured on an A1 (2026-09-07): ``state: "idle"``
        # beside ``print_error: 302022663`` -- 1200-8007, "failed to extrude
        # the filament" -- while the machine's own screen held a modal error
        # dialog.  Every door led with the healthy word, and the contradiction
        # underneath was visible only to a reader who already knew to look for
        # it.  A headline that reads "idle" over a live fault is the same
        # failure of honesty as the confident "printing" over a frozen cache,
        # and it gets the same answer: the fact that decides what to do next
        # takes the headline, and the fact it displaced is kept.
        #
        # ERROR rather than a new member, deliberately.  The vocabulary
        # already HAS the word for "this machine is reporting something wrong"
        # -- it is not READY, it is INDETERMINATE, the pre-flight gate refuses
        # it, the CLI colours it red and the Monitor gives it the
        # needs-attention layout.  Every one of those is the behaviour a
        # latched fault should get, so a second word for the same condition
        # would buy no new meaning and cost a fresh set of switches to forget
        # to update.  ``STALE`` earned its member because no existing state
        # could carry "the reading itself expired"; this one is not a new
        # axis, it is the axis ERROR is already on.
        #
        # AFTER the staleness promotion, never before.  "Kiln cannot vouch for
        # this reading" outranks anything the reading says, including a fault
        # code inside it -- and promoting first would let the stale promotion
        # overwrite ``last_known_state`` with ERROR, dropping the run state
        # that keeps ``is_occupied`` conservative on exactly the machine that
        # can least afford it.  A stale reading's fault survives in
        # ``print_error`` and in a headline that already says to go and look.
        # ``print_error_code``, not ``print_error``: one function decides what
        # counts as a real firmware error, and both halves of the payload have
        # to agree with it.  ``format_error_code`` calls anything <= 0 "no
        # error" -- so a bogus negative value, which is merely truthy, would
        # otherwise promote the headline to ``error`` while the code field
        # beside it stayed empty and the sentence named no code at all.
        if (
            self.print_error_code is not None
            and self.connected
            and self.state is not PrinterStatus.STALE
            and self.state is not PrinterStatus.ERROR
            and self.state not in UNREACHABLE_STATES
        ):
            self.last_known_state = self.state
            self.state = PrinterStatus.ERROR
        # Beside every ERROR carrying a code, however it got there -- the
        # promotion above, or an adapter that mapped the firmware's own error
        # state directly.  Both are the same fact to a reader.
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

        # The floor, and it fires on a VERDICT rather than on an age:
        #   * the reading is STALE -- which the promotion above only reaches
        #     with a budget the adapter measured for this printer, and which
        #     on the push adapters means the printer was ASKED and did not
        #     answer (Bambu's ``_get_cached_status`` republishes a pushall at
        #     budget expiry and waits for the reply before a state is built,
        #     so a machine that answers never arrives here);
        #   * there is no connection at all.
        #
        # Deliberately NOT ``is_stale()``.  That is a bare age against a
        # fallback budget, and on an adapter with no measured budget and no
        # re-ask it is not evidence of anything: Moonraker stamps its clock
        # only on a push carrying ``print_stats``, Klipper subscriptions send
        # deltas, so an IDLE Klipper's run-state clock climbs past the 60s
        # fallback for ever while its temperatures keep arriving every few
        # seconds.  Blanking there hides live readings on a healthy machine,
        # and a floor that cries wolf teaches people to ignore the one
        # blanking that matters.  Measured on a Bambu A1 (2026-09-06): one
        # reading carried a run state 200s old beside a bed temperature 2s
        # old.  The run state and the temperatures are not one stream.
        #
        # Those adapters keep saying so in prose (``staleness_note``) until
        # they supply a measured budget and an ask, which is what promotes
        # them into the first clause with no change here.  A dead websocket
        # is already covered: their push path bails to an HTTP query, which
        # asks the printer on every call.
        if not (self.state is PrinterStatus.STALE or not self.connected):
            return
        for name in TEMPERATURE_FIELDS:
            setattr(self, name, None)
        # The sentence only where there was something to be tempted by: a
        # printer Kiln cannot reach has no numbers to mistake for current
        # ones, and its ``cause``/``remedy`` already say what is wrong.
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
        """What the machine was doing, looking through a displaced headline.

        ``STALE`` answers "can this reading be trusted" and a promoted
        ``ERROR`` answers "is this machine reporting a fault" -- neither
        answers "what is the printer doing", so anything asking the second
        question reads this and gets the run state, displaced but not erased.

        Reads :attr:`last_known_state` whenever it is set, because it is set
        by those two promotions and by nothing else.  A gate that special-
        cased one promotion would be a gate that silently missed the next.
        """
        if self.last_known_state is not None:
            return self.last_known_state
        return self.state

    @property
    def confirmed_state(self) -> PrinterStatus:
        """The run state, but only from a reading Kiln can vouch for.

        The other half of :attr:`effective_state`, and the difference is not
        academic.  Both look through a displaced headline, but they answer
        opposite questions and must fail in opposite directions:

        * "might this machine be busy" wants to see through EVERYTHING and
          assume the worst -- :attr:`effective_state`, which reads a stale
          reading's run state because a refused print is a retry and a second
          print onto an occupied bed is a crash;
        * "has this print ENDED" must not see through staleness at all.  An
          expired reading is not evidence that anything finished, and a watch
          closed on one is a watch closed on a print that is still running.
          That is the whole reason ``STALE`` is absent from every terminal
          set in this codebase.

        So this sees through a FAULT -- a fresh reading whose headline a
        latched code took over, where the run state underneath is current and
        trustworthy -- and never through ``STALE``, which it returns as
        itself so it matches no terminal state.
        """
        if self.state is PrinterStatus.STALE:
            return PrinterStatus.STALE
        return self.effective_state

    @property
    def is_occupied(self) -> bool:
        """Might this machine have work in flight?

        The question every gate that must not start a second print is really
        asking.  A stale reading answers from what the printer was last seen
        doing, and from "yes" when even that is unknown: the costs are not
        symmetric -- a refused print is a retry, a second print onto an
        occupied bed is a crash.
        """
        if self.state is PrinterStatus.STALE and self.last_known_state is None:
            return True
        # Through the headline, whichever promotion set it.  A machine that
        # raised a fault mid-print is still mid-print: the bed is not clear,
        # and a router reading the bare ERROR would have called it free.
        return self.effective_state in BUSY_STATES

    def freshness_budget(self, max_age: float | None = None) -> float:
        """The age past which this reading stops counting as evidence.

        An explicit *max_age* wins; otherwise the budget the adapter measured
        for this printer; otherwise :data:`STALE_STATE_WARN_AGE`, the fixed
        rule that applies until there is a cadence to measure.
        """
        if max_age is not None:
            return max_age
        if self.state_stale_after_seconds is not None:
            return self.state_stale_after_seconds
        return STALE_STATE_WARN_AGE

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`state`, :attr:`last_job_result` and
        :attr:`last_known_state` enums are converted to their string values so
        the result can be passed directly to ``json.dumps``.  Extended
        monitoring fields that are ``None`` are omitted for compactness.
        """
        data = asdict(self)
        data["state"] = self.state.value
        if self.last_job_result is not None:
            data["last_job_result"] = self.last_job_result.value
        if self.last_known_state is not None:
            data["last_known_state"] = self.last_known_state.value
        # The looked-up form of the error code travels WITH the raw one,
        # never instead of it: the formatted string is what a person can
        # search for, the decimal is what the firmware said.
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
        """Whether :attr:`state` is older than its freshness budget.

        ``False`` when the adapter reports no age: a missing measurement is
        not evidence of staleness, and treating it as stale would put a
        warning on every polling adapter's output.
        """
        if self.state_age_seconds is None:
            return False
        return self.state_age_seconds > self.freshness_budget(max_age)

    def staleness_note(self, max_age: float | None = None) -> str | None:
        """One sentence naming this reading's age, or ``None`` when fresh.

        Every surface that reports printer state in prose leads with this when
        it is not ``None``.  It exists because the failure it names is silent
        otherwise: a frozen push cache answers "printing" in exactly the tone
        it would use for a live reading, and a confidently wrong answer costs
        more than an error.

        The sentence names what the printer was last seen DOING, not the
        ``STALE`` headline -- "PRINTING describes then, not now" is the fact;
        "STALE describes then" would be a tautology.
        """
        return describe_stale_state(
            self.state_age_seconds,
            self.effective_state.value,
            self.freshness_budget(max_age),
        )


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
    # The BACKEND's own id for this job, when it issues one that is really
    # unique -- Prusa Link's ``job.id`` is the same handle its pause/resume/
    # cancel endpoints take.  Left None by every backend that issues nothing
    # (Moonraker, OctoPrint, Duet, Elegoo) and by Bambu, whose task_id /
    # subtask_id are the literal "0" on every LAN print.  Consumers must not
    # invent one here: ``kiln.printers.job_identity`` owns the fallback.
    job_id: str | None = None
    # How THIS job ended, when it has.  ``None`` means the job is running,
    # or that the backend reports no ending -- never that it ended well.
    #
    # It exists because a job block with no ending on it reads as current,
    # and a push cache goes on serving the last job long after it stopped.
    # Measured on an A1 (2026-09-03): layer 1 of 225 with 3h 57m remaining,
    # for a print cancelled hours earlier.  Every number in that block was
    # the firmware's, and the block as a whole was a lie -- not because any
    # field was wrong, but because nothing on it said the job was over.
    ended_as: JobResult | None = None
    # Whether this job is the one the machine is running NOW.  Distinct from
    # :attr:`ended_as` because the two facts have different sources and a
    # printer can supply one without the other: a Bambu sitting idle still
    # carries the last print's file name in its cache, so the block needs to
    # be markable as not-current even when the firmware never said how that
    # print ended.  ``None`` means the backend does not report it, and
    # :attr:`is_active` then falls back to the ending.
    active: bool | None = None

    @property
    def is_active(self) -> bool:
        """Is this a job the machine is running now?"""
        if self.active is not None:
            return self.active
        return self.ended_as is None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        Extended fields that are ``None`` are omitted for compactness.

        A job that has ENDED is serialised as such, and loses the fields
        that only mean something about a running one: a "time remaining"
        for a cancelled print is a forecast of a future that is not coming.
        The fields that describe what happened -- the file, how far it got,
        which layer it stopped on -- are kept, because those are true.
        """
        data = asdict(self)
        data.pop("ended_as", None)
        data.pop("active", None)
        if self.file_name and not self.is_active:
            data["active"] = False
            # A "time remaining" for a job that is not running is a forecast
            # of a future that is not coming.
            data.pop("print_time_left_seconds", None)
            if self.ended_as is not None:
                data["ended_as"] = self.ended_as.value
        for key in ("current_layer", "total_layers", "job_id"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


def read_status(adapter: Any) -> tuple[PrinterState, JobProgress]:
    """Read both halves of a printer's status and make them agree.

    A module-level function rather than only a method, so it works on
    anything that answers ``get_state`` and ``get_job`` — a duck-typed
    adapter, a kiln-pro subclass, a test double — and not only on subclasses
    of :class:`PrinterAdapter`.  :meth:`PrinterAdapter.get_status` is the
    method form of this.
    """
    state = adapter.get_state()
    return state, reconcile_job_with_state(state, adapter.get_job())


def reconcile_job_with_state(
    state: PrinterState, job: JobProgress
) -> JobProgress:
    """A job block that cannot contradict the state standing beside it.

    The single place the two halves of a status read are made to agree, so
    every door that reports both -- the tool surface, the web Monitor, the
    CLI -- gets the same answer instead of each deciding for itself.

    Two rules, and only two:

    * A machine that is running a job has not ended one.  Any ending on the
      block is dropped, because the firmware's ``last_job_result`` describes
      the job BEFORE this one.
    * A machine that is not running a job, and whose firmware reports how the
      last one ended, has that ending stamped onto the block -- which is what
      stops "layer 1 of 225, 3h 57m remaining" from being served for a print
      that was cancelled hours ago.

    Returns the job unchanged when neither applies; never mutates the input.

    Anything that is not a real :class:`PrinterState` / :class:`JobProgress`
    pair is passed straight through.  A duck-typed adapter -- or a test
    double -- can answer these calls with objects this function has no way
    to rebuild, and a status read must not fail because the reconciliation
    could not run.
    """
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
    """Name the held-job condition, and the one thing that clears it.

    Measured on an A1 (2026-09-03): a print cancelled hours earlier stayed in
    the printer's telemetry as though it were current, and the pushes stopped
    arriving.  The visible cost was not in Kiln at all -- the held job greyed
    out Load and Unload on the printer's own screen, so a filament jam could
    not be cleared by hand.  A power cycle fixed it: the reading's age fell to
    69 s, the job block emptied, and Load became pressable again.

    Fires only when all three hold, because any two of them are ordinary:
    the reading has expired, the block still names a job, and that job has
    already ended.
    """
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
    """Why a status read produced no current answer, and what to do about it.

    "Offline" used to be one word for four different situations with four
    different fixes, so the advice attached to it was wrong three times out
    of four -- most expensively when a printer that was powered on, on the
    network and perfectly healthy was reported offline because its LAN
    connection slots were held by leftover ``kiln serve`` processes, and the
    user power-cycled hardware that was never at fault.
    """

    state: PrinterStatus
    cause: str
    remedy: str


# The four causes, as stable strings for anything branching on them.
CAUSE_POWERED_OFF = "powered_off_or_off_network"
CAUSE_WRONG_ACCESS_CODE = "wrong_access_code"
CAUSE_CONNECTION_LIMIT = "connection_limit"
CAUSE_SILENT = "reachable_but_silent"


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool | None:
    """Can this machine open a TCP socket to *host*:*port*?

    The one fact that separates "powered off" from "answering but refusing":
    a printer that is on and on the network completes the TCP handshake even
    when it will not let us any further in.

    Deliberately a bare connect-and-close.  No protocol bytes are sent, so
    this does not open an MQTT session and cannot itself consume one of the
    scarce connection slots it is helping to diagnose.  ``None`` when there
    is nothing to probe.
    """
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
    """True when *message* names refused credentials, not reachability.

    The one reading of :data:`_AUTH_NEEDLES`, shared by the adapter-layer
    diagnosis and by printer status, so the two cannot drift into calling
    one failure by two names.  It reads words, so an adapter must keep
    credential words OUT of any message that is not a refusal -- advice such
    as "check the access code" inside a no-answer message is exactly what
    once turned a full printer into a wrong access code.
    """
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
    """Sort a failed printer read into ONE of the four causes, with its fix.

    Named for the CALL SITE -- a read that failed -- rather than for one of
    its verdicts, because one of the four is that the printer is reachable
    and simply silent.  Calling that "unreachable" is the conflation this
    function exists to undo.

    *message* is the adapter's own exception text, *kiln_slot_holders* the
    number of this machine's own Kiln servers currently holding a connection
    to the printer (from :func:`kiln.serve_siblings.printer_slot_report`),
    and *reachable* the result of :func:`probe_tcp` when it has already been
    taken -- otherwise it is taken here, given a host and port.

    Order matters.  Credentials first, because a printer that refuses our
    access code says so and nothing else needs checking.  Then the
    connection ceiling, on evidence rather than on the adapter's guess: the
    timeout message names a busy slot as the likely cause, which is exactly
    the assumption that sent people to power-cycle a healthy printer.  Only
    then, with the machine not answering at all, is "it is off" the answer.
    """
    text = (message or "").lower()

    if reads_as_credentials_refusal(message):
        return ReadDiagnosis(
            state=PrinterStatus.UNAUTHORIZED,
            cause=CAUSE_WRONG_ACCESS_CODE,
            remedy=(
                "The printer answered and refused Kiln's access code. On the "
                "printer's screen go to Settings → Network, turn LAN Only "
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
                "trim_serve_processes (terminal: `kiln trim`). Power-cycling "
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
    """The :class:`PrinterState` a *diagnosis* stands for.

    One constructor so every adapter's failure path reports the cause and
    the remedy in the same shape, instead of each building its own bare
    ``connected=False, state=OFFLINE``.

    ``connected`` follows the verdict rather than the call site: three of
    the four causes mean no connection, but a printer that is reachable and
    merely silent IS connected, and saying otherwise would send the reader
    to the power switch.
    """
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


# ---------------------------------------------------------------------------
# Filament handling (load / unload / purge)
# ---------------------------------------------------------------------------

#: Cold-extrusion floor.  Marlin ships ``EXTRUDE_MINTEMP 170`` and Klipper's
#: ``min_extrude_temp`` defaults to 170; Bambu firmware refuses an extrude
#: below its own floor too.  Kiln refuses earlier, with a reason, rather than
#: sending a move the firmware will drop (or, on a firmware with the guard
#: disabled, grind cold plastic through the gears).
MIN_EXTRUDE_TEMP_C: float = 170.0

#: Longest single purge Kiln will command.  A clog test needs a few tens of
#: millimetres; anything longer is a runaway extrusion, not a purge.
MAX_PURGE_LENGTH_MM: float = 150.0

#: Default purge for the clog test: enough to see a clean stream, short
#: enough to be harmless when the nozzle is blocked.
DEFAULT_PURGE_LENGTH_MM: float = 30.0

#: Generic G-code feed for a load — the distance Marlin's own ``M701``
#: default covers on a direct-drive head — and the retract for an unload.
#: Bowden machines pass their own ``length_mm``.
DEFAULT_LOAD_LENGTH_MM: float = 60.0
DEFAULT_UNLOAD_LENGTH_MM: float = 80.0

#: Slow feed for every extrude Kiln commands (3 mm/s).  Fast enough to be
#: over quickly, slow enough that a partial clog shows as an under-stream
#: rather than a skipped stepper.
FILAMENT_FEED_RATE_MM_MIN: int = 180

#: How long the shared sequence waits for the hotend to reach target.
HOTEND_HEAT_TIMEOUT_S: float = 240.0

#: Fallback hotend ceiling for the filament template when no adapter or
#: safety profile tightens it.  Mirrors the literal every G-code adapter
#: passes to ``_validate_temp`` from ``set_tool_temp``.
_DEFAULT_MAX_HOTEND_C: float = 300.0


@dataclass
class FilamentOpPlan:
    """A validated filament operation, handed to an adapter's ``_impl``.

    Built by :meth:`PrinterAdapter._prepare_filament_op` and never by an
    adapter, so the temperature an ``_impl`` receives has already cleared
    the safety profile, the material window, and the cold-extrusion floor.
    """

    action: str  # "load" | "unload" | "purge" | "wipe"
    temperature: float
    temperature_source: str
    slot: int | None = None
    material: str | None = None
    length_mm: float | None = None
    #: ``(nozzle_temp_min, nozzle_temp_max, source)`` when a window was
    #: known — from an AMS tray report or Kiln's material table.
    material_window: tuple[float, float, str] | None = None
    #: Adapter-specific extras forwarded verbatim (e.g. ``wait_seconds``).
    options: dict[str, Any] = field(default_factory=dict)
    #: The print was paused rather than finished when this was prepared.
    #: Allowed on purpose -- clearing a clog and resuming is the case this
    #: exists for -- but the nozzle is parked over the part.
    printer_paused: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.material_window is not None:
            data["material_window"] = list(self.material_window)
        return data

    def wait_seconds(self, default: float) -> float:
        """How long the backend may watch the printer for its answer.

        ``options["wait_seconds"]`` is what the caller asked for, *default*
        the backend's own figure when nothing was asked.  Either is bounded
        by ``options["wait_ceiling_seconds"]`` when the door that built this
        plan declared one: an MCP tool call lives inside the host's request
        window, and a watch that outlasts it finishes on the server after
        the client has given up, so the answer is lost (measured 2026-09-15:
        a 180 s load watch on an A1 returned "Request timed out" while the
        load itself completed).  The backend assumes no window -- the CLI
        has none -- so the ceiling is the door's to set, and this is the one
        place every backend reads it.
        """
        asked = self.options.get("wait_seconds")
        wait = float(default) if asked is None else float(asked)
        ceiling = self.options.get("wait_ceiling_seconds")
        if ceiling is not None and float(ceiling) > 0:
            wait = min(wait, float(ceiling))
        return wait


@dataclass
class FilamentOpResult:
    """Outcome of a load, unload, or purge.

    ``extrusion_verified`` is three-valued on purpose.  ``True`` and
    ``False`` are only ever set from a signal the printer genuinely
    produced (an AMS reporting the tray now feeding the nozzle, a firmware
    rejecting the move, a fault code raised during the purge).  ``None``
    means the command was accepted and nothing the printer reports can
    say whether plastic left the nozzle — which is the honest answer on
    every backend without a flow sensor.  ``verification_source`` names
    the signal so a caller can weigh it.
    """

    success: bool
    action: str
    message: str
    extrusion_verified: bool | None = None
    verification_source: str | None = None
    #: The printer's own fault code, when it raised one (Bambu HMS /
    #: ``print_error`` in ``XXXX_XXXX`` form, a Klipper error line, …).
    error_code: str | None = None
    #: Plain-language reading of ``error_code`` — what it means and what to
    #: do — or the firmware's own text when Kiln has no translation.
    error_hint: str | None = None
    slot: int | None = None
    material: str | None = None
    temperature: float | None = None
    #: The op as steps, whenever its plan describes it (today: a served
    #: wipe).  In step mode only ``steps[step-1]`` was sent; ``next_step``
    #: describes the one to send next, ``None`` once the sequence is
    #: complete -- and only then does the finish (heater off, the cool-down)
    #: run.  ``leaves`` names what the step left armed.
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
    """One motion of a homing sequence, described before it runs.

    ``you_will_see`` is written for the person standing at the machine;
    ``stops_when`` answers the question that makes people reach for the
    power switch: does it know where to stop?  ``leaves`` names anything
    the step leaves armed (a heater on, soft endstops off) so an abandoned
    step-mode run is never a silent hazard.
    """

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
    """Outcome of :meth:`PrinterAdapter.home_axes`.

    ``outcome`` is the field to branch on, the same three words
    ``set_temperature`` and ``start_print`` use: ``confirmed`` (the printer
    was seen at home), ``accepted`` (the homing was sent and not refused
    -- the honest answer on every backend Kiln cannot read a homed flag
    from), ``failed`` (the gate or the printer refused, or a fault was
    raised).  ``homed_axes`` names the axes the sequence's own homing
    commands addressed, which is not always every axis asked for: a
    vendor sequence may home X and Z and only *send the bed* to Y=0.
    ``resting_position`` says where the head was left, every time.
    """

    success: bool
    outcome: str
    message: str
    axes: str
    #: ``"home"`` (find zero: endstops, and on some machines a nozzle touch)
    #: or ``"park"`` (get out of the way: raise, home X, travel to a known
    #: off-plate spot -- never a Z touch).  Same result shape, different verb.
    action: str = "home"
    homed_axes: list[str] = field(default_factory=list)
    #: How the homing was sent (``"gcode"``, ``"bambu_mqtt_gcode_line"``).
    mechanism: str | None = None
    #: ``"vendor_start_sequence"`` when every line is the printer maker's
    #: own, cited in the catalogue; ``"firmware_home_routine"`` when Kiln
    #: sent the firmware's own home, gated by the catalogue's motion record
    #: (what descends and where, in the vendor's words), and the firmware
    #: chose the path.
    sequence_source: str | None = None
    resting_position: dict[str, Any] = field(default_factory=dict)
    #: Set when the sequence heats the nozzle (a printer that homes Z by
    #: nozzle contact heats first, as its own sequence does).
    heats_nozzle_to_c: float | None = None
    error_code: str | None = None
    error_hint: str | None = None
    #: The sequence as steps, whenever the backend can describe it.  In
    #: step mode only ``steps[step-1]`` was sent; ``next_step`` describes
    #: the one to send next, ``None`` once the sequence is complete.
    steps: list[dict[str, Any]] = field(default_factory=list)
    step_sent: int | None = None
    next_step: dict[str, Any] | None = None
    #: What is armed on the machine after this call (heater target,
    #: soft endstops) -- said every time, so an abandoned run is visible.
    leaves: list[str] = field(default_factory=list)
    #: Path of the camera frame taken before a plate-touching motion, when
    #: the printer has a camera -- the witness for the person's ``plate_clear``.
    plate_witness: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
    #: Whether :meth:`PrinterAdapter.clear_error` can acknowledge a latched
    #: firmware error.  Defaults to False so a backend that has not been
    #: taught its firmware's acknowledgement advertises the truth — a caller
    #: offering the user a button that cannot work is worse than no button.
    can_clear_error: bool = False
    #: Whether :meth:`PrinterAdapter.load_filament` / ``unload_filament`` /
    #: ``purge_filament`` do something real on this backend.  False means
    #: the adapter's ``_impl`` hooks raise
    #: :class:`FilamentHandlingUnsupported` — declared here so ``kiln
    #: doctor`` and the MCP tools can say so before a heater moves.
    can_handle_filament: bool = False
    #: Whether cancelling DURING a calibration routine (bed levelling, Z
    #: homing) trips a firmware fault on this backend.  Measured on an A1
    #: (2026-08-13): a cancel mid-levelling aborts the homing move and the
    #: firmware latches "Z axis homing failed" — every subsequent print
    #: refused until a power cycle.  Pausing first turns that same fault
    #: transient: it self-clears in about fifteen seconds and the job lands
    #: as "cancelled".  Default False: a backend nobody has measured is not
    #: assumed to share the hazard, because the guard costs a real command.
    cancel_during_calibration_faults: bool = False
    #: Whether :meth:`PrinterAdapter.get_multi_material_status` can ASK the
    #: machine what multi-material unit it carries (an AMS, a Klipper MMU).
    #: This is "can look", not "has one": the answer is a live read, so it
    #: lives on the method, never on this static declaration.  Default
    #: False — a backend nobody has taught to look says so, and the shared
    #: reader (:func:`kiln.multi_material.multi_material_status`) reports
    #: ``none`` rather than guessing.
    can_report_multi_material: bool = False
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
    spaced = re.sub(r"[^a-z0-9]+", "_", reported.lower()).strip("_")
    norm = re.sub(r"(?<=[a-z])_(?=\d)", "", spaced)
    if not norm:
        return None
    # The collapsed spelling first (``ender3_v2``), then the spelling that
    # keeps the underscore before a bare generation digit -- the catalogue
    # keys ``elegoo_centauri_carbon_2`` that way, and collapsing it would
    # leave a Carbon 2 reporting itself verbatim with a row waiting for it.
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



def _make_engagement_gated(action: str, original):
    """Wrap *original* so it consults the single-printer engagement first."""
    import functools

    @functools.wraps(original)
    def _gated(self, *args, **kwargs):
        from kiln.printers.engagement import check_command, observe

        verdict = check_command(self, action)
        if verdict is not None:
            raise PrinterEngagementError(verdict)
        result = original(self, *args, **kwargs)
        # Learn from the answer the command already produced.  Claiming the
        # free slot from here rather than from the gate is what keeps the
        # rule free: asking the printer up front cost a second round trip on
        # the first status call of every engagement.
        observe(self, action, result)
        return result

    _gated._kiln_engagement_wrapped = True  # type: ignore[attr-defined]
    return _gated


def _install_engagement_gate(cls: type, *, own_methods_only: bool) -> None:
    """Gate every printer-directed command on *cls*.

    ``own_methods_only`` is the load-bearing argument.  The base class is
    wrapped once with it False, so an adapter that INHERITS a control method
    is gated by that.  Each subclass is then wrapped with it True, so only a
    method the subclass really overrides gets its own wrapper.

    The distinction is not tidiness.  Writing a wrapper into every subclass's
    ``__dict__`` would make each adapter LOOK like it overrides the base
    template, and several adapters are pinned by tests asserting they do not
    (``"resume_print" not in DuetAdapter.__dict__``) precisely because
    overriding one is how the base safety gate gets bypassed.  Gating must not
    cost the suite its ability to see that.
    """
    from kiln.printers.engagement import GATED_ACTIONS

    for action in sorted(GATED_ACTIONS):
        original = cls.__dict__.get(action) if own_methods_only else getattr(cls, action, None)
        if original is None or not callable(original):
            continue
        if getattr(original, "_kiln_engagement_wrapped", False):
            continue
        if getattr(original, "__isabstractmethod__", False):
            # Wrapping an abstract method would return a concrete function and
            # quietly switch OFF the ABC check that forces every adapter to
            # implement it.  The subclass that implements it gets gated instead.
            continue
        setattr(cls, action, _make_engagement_gated(action, original))

# ---------------------------------------------------------------------------
# A camera the user supplies — frame plumbing only
# ---------------------------------------------------------------------------
#
# Plenty of printers have no camera, or a poor one, and the obvious fix is a
# camera the user points at the bed themselves.  What lives here is the
# plumbing for that: a place to record the source, one fetch that every door
# calls, and the redaction that keeps a stream password out of every reply
# and log.  Nothing here reasons about what the frames show.
#
# HONESTY, stated once so every door can point at it: a camera the user
# supplies gives Kiln frames to look at.  It does not switch on a printer's
# own detection — spaghetti, clumping and the like run on the printer's own
# hardware against its own cameras, and a frame Kiln fetches from elsewhere
# never reaches them.
EXTERNAL_CAMERA_NOTE = (
    "Frames from a camera you supply are what Kiln looks at for snapshots and "
    "monitoring. They do not switch on the printer's own failure detection "
    "(spaghetti, clumping, air printing): that runs on the printer against its "
    "own cameras and never sees a frame Kiln fetched elsewhere."
)

#: URL schemes a user camera may use.  ``http(s)`` returns a still (or an
#: MJPEG stream a still can be cut from); ``rtsp(s)`` needs ffmpeg for a frame.
EXTERNAL_CAMERA_SCHEMES: tuple[str, ...] = ("http", "https", "rtsp", "rtsps")

_RTSP_SCHEMES = ("rtsp", "rtsps")
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


def redact_url_credentials(url: str | None) -> str | None:
    """``rtsp://user:secret@host/x`` -> ``rtsp://user:****@host/x``.

    A camera URL is the one printer setting that routinely embeds a password,
    so every place a URL is shown or logged goes through here.  Anything that
    is not a parseable URL comes back unchanged.
    """
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
    """Return *url* stripped, or raise ``ValueError`` naming what is wrong.

    Only the scheme is judged.  Whether the camera answers is learned the
    first time a frame is asked for, and reported then.
    """
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
    """Where a user-supplied camera can be reached.

    ``snapshot_url`` answers with one image per request (a webcam's
    ``?action=snapshot`` endpoint, an IP camera's still URL).  ``stream_url``
    is a live feed — MJPEG over http(s), or rtsp(s).  Either alone is enough:
    a still is cut from the stream when no snapshot URL is given, and the
    stream is what a viewer opens.
    """

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


def find_ffmpeg() -> str | None:
    """Find an ffmpeg binary on PATH or in the usual install locations."""
    import shutil

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


def capture_rtsp_frame(
    stream_url: str,
    *,
    ffmpeg: str,
    label: str = "Camera RTSP",
    timeout: float = 5.0,
) -> bytes:
    """One JPEG frame from an rtsp(s) stream, via ffmpeg.

    *stream_url* may carry credentials; nothing here echoes it.  Raises
    :class:`PrinterError` with a message a user can act on when ffmpeg
    fails or the stream does not answer.
    """
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


def fetch_external_snapshot(camera: ExternalCamera, *, timeout: float = 10.0) -> bytes:
    """One frame from a user-supplied camera; raises :class:`PrinterError`.

    The snapshot URL is used when given.  Otherwise a still is cut from the
    stream: the first whole JPEG of an MJPEG feed, or one ffmpeg frame of an
    rtsp(s) feed.  Every message names the camera by its redacted URL.
    """
    from urllib.parse import urlsplit

    url = camera.snapshot_url or camera.stream_url
    if not url:
        raise PrinterError("No camera URL is registered for this printer.")
    shown = redact_url_credentials(url)
    scheme = urlsplit(url).scheme.lower()

    if scheme in _RTSP_SCHEMES:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise PrinterError(
                f"Your camera at {shown} is an RTSP stream, and cutting a frame "
                "from it needs ffmpeg. Install ffmpeg, or register an http "
                "snapshot URL for the camera instead."
            )
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
                return data
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
    """The stream to open: the user's camera first, else the printer's own.

    A still-only user camera has nothing to stream, and the answer is then
    ``None`` rather than the printer's own feed under the user's camera's
    name — a stream from the wrong camera is worse than no stream.
    """
    if camera is None:
        return printer_stream_url
    return camera.stream_url


# ---------------------------------------------------------------------------
# Live video — the capability contract, and the reasons a source can refuse
# ---------------------------------------------------------------------------


class CameraStreamError(PrinterError):
    """A live-video source could not be opened, with the reason in words.

    ``code`` is ``CAMERA_REFUSED`` when the printer answered and said no (the
    LAN access code, or the camera's own video setting on the printer) and
    ``CAMERA_UNREACHABLE`` when nothing answered on the camera port at all.
    A relay reports the message on its status rather than retrying in
    silence, so a viewer is never shown a black frame with no explanation.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StreamCapability:
    """Whether this printer can feed Kiln's local live-video relay, and why not.

    The interface contract for live video, the way ``can_snapshot`` and
    ``snapshot_source`` are for stills: a backend answers from what it can
    OBSERVE (its own protocol, a camera the user registered, whether ffmpeg
    is on the path) — ``channel`` names the transport the relay would read,
    ``reason`` says in plain words why there is nothing to relay, and
    ``requires`` lists what the printer side must have on for the channel to
    answer.  ``available`` is about the RELAY: a printer whose only stream is
    RTSP can still take snapshots, and the reason says so.
    """

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


#: The reason a user-registered RTSP camera cannot be relayed — one wording,
#: shared by the capability answer and the ``webcam_stream`` refusal.
RTSP_NOT_RELAYED_REASON = (
    "Your camera's stream is RTSP, which Kiln's local MJPEG proxy cannot "
    "relay. Open the RTSP URL you registered in a video player directly; "
    "snapshots and monitoring still read frames from it."
)


def adapter_has_camera(adapter: Any) -> bool:
    """Whether *adapter* can produce a frame: its own camera, or the user's.

    The capability flag describes only the printer's own camera, so every
    reader that used to test ``capabilities.can_snapshot`` asks this instead.
    Tolerates a duck-typed or mocked adapter: only a real ``bool`` from
    :attr:`PrinterAdapter.has_camera` is trusted, everything else falls back
    to the capability flag exactly as before.
    """
    flag = getattr(adapter, "has_camera", None)
    if isinstance(flag, bool):
        return flag
    return bool(getattr(getattr(adapter, "capabilities", None), "can_snapshot", False))


def _wrap_camera_first(cls: type) -> None:
    """Make a subclass's own ``get_snapshot`` / ``get_stream_url`` camera-aware.

    Called from ``PrinterAdapter.__init_subclass__``: an adapter overriding
    either method keeps its printer-camera code as the fallback, and the
    user's camera is consulted first — the engine, not each adapter.
    """
    import functools

    snapshot = cls.__dict__.get("get_snapshot")
    if snapshot is not None and not getattr(snapshot, "_kiln_camera_wrapped", False):

        @functools.wraps(snapshot)
        def _camera_first_snapshot(self, *args, **kwargs):
            if self._external_camera is not None:
                return fetch_external_snapshot(self._external_camera)
            return snapshot(self, *args, **kwargs)

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
    """Read ``camera_snapshot_url`` / ``camera_stream_url`` off a config entry.

    Every place that turns a saved printer record into a live adapter calls
    this, so a camera survives whichever door built the adapter.  A record
    with neither key leaves the adapter untouched.
    """
    if not isinstance(entry, dict):
        return
    snapshot = str(entry.get("camera_snapshot_url") or "").strip() or None
    stream = str(entry.get("camera_stream_url") or "").strip() or None
    if snapshot or stream:
        adapter.set_external_camera(snapshot_url=snapshot, stream_url=stream)


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
                if refusal := _incomplete_upload_reason(self, file_path):
                    from kiln.printers import PrinterError
                    raise PrinterError(refusal)
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
                # Taken before the read: a reading is as old as the moment
                # it was asked for, and the outcome table orders readings by
                # it (see ``auto_record_hook.observe_state``).
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
        # Single-printer engagement: every printer-directed command asks
        # whether Kiln is already working with a DIFFERENT machine.  Same
        # engine-not-instance shape as the two wraps above, and the same
        # reason -- the tier rule used to live only on start_print, so the
        # eight sibling commands that actually operate a second machine were
        # never asked.  Resolved with getattr rather than cls.__dict__ so an
        # adapter that INHERITS a control method is gated too: reading only
        # the subclass's own dict is exactly how a door gets missed.
        # ------------------------------------------------------------------
        _install_engagement_gate(cls, own_methods_only=True)

        # ------------------------------------------------------------------
        # A camera the user supplied: the same engine-not-instance shape.
        # Every adapter's own get_snapshot / get_stream_url is wrapped so
        # the user's camera is asked first, and the eleven doors that read
        # a frame keep calling the method they always called.
        # ------------------------------------------------------------------
        _wrap_camera_first(cls)


    def set_safety_profile(self, profile_id: str) -> None:
        """Bind a printer safety profile for temperature validation.

        When set, :meth:`_validate_temp` will use the profile's limits
        instead of the caller-supplied default.

        Args:
            profile_id: Profile identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
        """
        self._safety_profile_id = profile_id

    # -- print-duration semantics ----------------------------------------
    #
    # What this backend's ``JobProgress.print_time_seconds`` means AFTER the
    # print ends — the fact that decides whether a late reading can be
    # trusted (see ``_record_print_duration``):
    #
    #   "frozen"     the printer reports its own job clock and freezes it at
    #                the ending, so a late read is merely late and still
    #                correct;
    #   "stopwatch"  the number is a Kiln-side stopwatch nothing stops on
    #                its own, so a late read keeps counting and inflates;
    #   "none"       the backend has no job clock at all (direct USB), so
    #                its hours are unknowable rather than zero.
    #
    # The default is the STRICT one on purpose: an adapter that never
    # declares is treated as a stopwatch, whose late readings are refused —
    # forgetting to declare can cost real hours, never invent them.  Every
    # concrete adapter declares explicitly (pinned by
    # test_print_duration_capture, alongside the documentation copy in
    # scripts/adapter_conformance.yaml — which is NOT shipped in the pip
    # package, which is why runtime reads this attribute and not that file).
    _DURATION_SEMANTICS: ClassVar[str] = "stopwatch"

    # -- idle connection release ----------------------------------------
    #
    # Some printers ration connections: a Bambu accepts only a few LAN MQTT
    # clients, an Elegoo only a few websockets.  Kiln runs one ``kiln serve``
    # per MCP session and hosts do not reliably reap them, so an adapter that
    # holds its connection for the life of its process turns "sessions I once
    # opened" into "slots the printer no longer has" — the user meets that as
    # a printer that is powered on, pingable, and unreachable (2026-08-14).
    #
    # The machinery lives here, once, rather than in each push-based adapter,
    # so the two cannot drift on the part that is subtle: when NOT to release.
    # A backend opts in by setting the two class attributes below and
    # overriding :meth:`_connection_is_live`.

    #: Env var this backend reads for its idle window.  "" = no opt-in.
    _IDLE_RELEASE_ENV: ClassVar[str] = ""
    #: Seconds of caller inactivity before release.  0 = feature off.
    _IDLE_RELEASE_DEFAULT_S: ClassVar[float] = 0.0
    #: How often the reaper wakes to test the window (fraction of it).
    _IDLE_POLL_DIVISOR: ClassVar[float] = 4.0

    def _init_idle_release(self) -> None:
        """Set up idle bookkeeping.  Safe to call more than once.

        Adapters in this package do not chain to a base ``__init__``, so this
        is called explicitly from each opted-in adapter's constructor — and
        every accessor below still tolerates its absence, so a backend that
        opts in and forgets the call degrades to "never releases" rather than
        raising ``AttributeError`` from a printer operation.

        Double-checked so the common case takes no lock: ``_note_activity``
        runs on EVERY read and write, and ``_IDLE_SETUP_LOCK`` is shared by
        the whole process, so locking unconditionally here would funnel every
        printer operation on every adapter through one mutex to re-answer a
        question settled at construction.
        """
        if getattr(self, "_idle_stop", None) is not None:
            return
        with _IDLE_SETUP_LOCK:
            if getattr(self, "_idle_stop", None) is None:
                self._last_activity: float = time.monotonic()
                self._idle_reaper: threading.Thread | None = None
                self._idle_stop: threading.Event = threading.Event()

    def _note_activity(self) -> None:
        """Stamp caller demand.  Call from the adapter's connection funnel.

        Deliberately measures calls INTO the adapter, never traffic arriving
        from the printer: a printer pushes status whether or not anyone is
        listening, so stamping on inbound frames would keep every slot alive
        forever — precisely the condition the release exists to end.
        """
        self._init_idle_release()
        self._last_activity = time.monotonic()

    def _idle_window(self) -> float:
        """Seconds of inactivity before the connection is released.

        ``0`` or negative disables the release for this adapter.  An
        unparseable env value falls back to the default rather than failing a
        printer operation over a malformed setting.
        """
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
        """True while this adapter holds an open connection.

        Overridden by push-based backends; the default ``False`` stops the
        reaper immediately for anything that never opted in.
        """
        return False

    def _print_in_flight(self) -> bool:
        """True while the printer is mid-job, as of the last status seen.

        The reaper defers to this, and the default is the safe answer for a
        backend that cannot tell: a job might be running, so keep the
        connection.  Overriding it is what lets an idle printer's slot go
        back while a printing one's is held.
        """
        return True

    def _start_idle_reaper(self) -> None:
        """Start the thread that releases the connection once it falls idle.

        Call after every successful connect.  The thread exits as soon as it
        releases, so an idle-disconnected adapter costs no thread at all —
        only a connected one is worth watching.
        """
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
        """Signal the reaper to exit.  Call from ``disconnect``.

        Tolerates an adapter whose idle state was never initialised, so a
        ``disconnect`` on a half-built adapter cannot raise ``AttributeError``
        — that path runs during shutdown and error handling, where a new
        exception is the last thing anyone needs.
        """
        idle_stop = getattr(self, "_idle_stop", None)
        if idle_stop is not None:
            idle_stop.set()

    def _idle_loop(self, window: float) -> None:
        """Release the connection after *window* seconds with no calls.

        The checks run newest-cheapest-first and are all re-read each tick,
        so a printer that starts a job, or a caller that turns up, defers the
        release rather than racing it.

        One residual race is accepted rather than engineered away: a caller
        can enter the adapter's funnel in the instant between the last check
        here and ``disconnect`` taking the backend's lock, and would then hold
        a reference to a connection that is being closed underneath it.  It
        costs that one call a retryable connection error, it cannot happen
        until a printer has gone a full window untouched, and closing it
        properly would mean a release protocol spanning the reaper and every
        backend's connect lock — more deadlock surface than the failure is
        worth.  The final activity re-read below narrows it to microseconds.
        """
        interval = max(1.0, window / self._IDLE_POLL_DIVISOR)
        while not self._idle_stop.wait(interval):
            if not self._connection_is_live():
                return
            if time.monotonic() - self._last_activity < window:
                continue
            if self._print_in_flight():
                # Deferred, never cancelled: reassess on the next tick so the
                # slot goes back once the job it was serving is over.
                continue
            # Re-read after the state checks above, which are not free: asking
            # a backend whether it is printing can take a lock, and a call
            # arriving during that answer must still win.
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
        """Release any persistent connection this adapter holds.

        A no-op for the HTTP-polling backends, which hold nothing between
        calls.  The push-based ones override it: Bambu's MQTT and Elegoo's
        websocket each occupy a connection slot the printer rations, so for
        those "still constructed" must not mean "still connected".

        Defined here so callers that clean up — process exit, an idle sweep,
        a printer being deregistered — can release whatever they were handed
        without asking what kind of printer it is.  Implementations must be
        idempotent and must reconnect on demand.

        Deliberately concrete rather than abstract: "I hold nothing, so there
        is nothing to release" is the correct behaviour for most backends,
        and making it abstract would force every one of them to write that
        sentence out as an empty override.
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

    def get_status(self) -> tuple[PrinterState, JobProgress]:
        """State and job together, reconciled so they cannot contradict.

        The door every surface that reports BOTH halves should come through.
        :meth:`get_state` and :meth:`get_job` each answer honestly about
        their own half; it is only when the two are printed side by side
        that "idle" next to "layer 1 of 225, 3h 57m remaining" becomes a
        claim neither of them made.  Concrete rather than abstract, so every
        adapter gets it without writing anything: the reconciliation is in
        :func:`reconcile_job_with_state`, once.

        Raises:
            PrinterError: If communication with the printer fails.
        """
        return read_status(self)

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
        # A quiet start -- a file planned beside a part still on the plate
        # -- decides its own start switches: the contract in the file names
        # every routine that must be off (levelling, calibration, timelapse,
        # inspection, the clog probe), and this template sends them off
        # whichever door started it, so no door has to know.  The gate below
        # then judges the file against the machine, live; for such a file a
        # gate that cannot run is a refusal, never a pass.
        quiet_contract = _quiet_start_contract(file_name, kwargs)
        if quiet_contract is not None:
            from kiln.plate_state import quiet_start_flags

            kwargs = {**kwargs, **quiet_start_flags(quiet_contract)}
        try:
            from kiln.printers.print_gate import run_adapter_gate

            blocked = run_adapter_gate(self, file_name, kwargs)
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
            hint = blocked.get("override_hint", "")
            reason = blocked.get("reason", "Print blocked by the pre-print safety gate.")
            return PrintResult(
                success=False,
                message=reason + (" " + hint if hint else ""),
            )
        # The consent backstop.  An adapter a door handed out (a printer
        # registry, the CLI) starts nothing no clearance covers: the gate
        # every door calls grants one, and a door that forgot to call it
        # is refused here rather than trusted.  See kiln.print_signoff.
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
            return PrintResult(success=False, message=unsigned["reason"])
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

            # Kiln started this print, so Kiln is now driving this machine.
            # Anchored to the same moment and for the same reason: it is the
            # one event Kiln causes and therefore cannot miss.  A resume 3MF
            # is excluded above -- it continues a print that already has an
            # engagement, and re-recording it would reset the return budget.
            try:
                from kiln.printers.engagement import engage

                # Recorded WITHOUT asking the printer which job this is: a
                # status call right after a start is an extra round trip on
                # the one path that must stay lean, and the identity arrives
                # for free on the next get_job (engagement.observe fills it).
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
            # Retain the sliced file for the web Monitor's layer viewer —
            # joined to the slice ledger by the exact name this adapter was
            # handed.  Same single-chokepoint reasoning as the counters
            # above: every door that starts a print passes through here.
            try:
                from kiln.monitor_twin import note_print_started

                note_print_started(self.name, file_name)
            except Exception:  # noqa: BLE001 — the twin never affects a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "monitor-twin print-start note failed", exc_info=True
                )
            # The plate now holds a part.  Recorded here, at the one door
            # every print passes through, with the file's height when Kiln
            # can read it, so home_axes and park_head can refuse a travel
            # that would cross the part -- see kiln.plate_state.
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
            # The filament cutter counts at START too: the sliced file says
            # how many filament changes it plans, and each is a cut on a
            # machine that has a cutter.  Same chokepoint, same over-count
            # rule as the nozzle odometer.  No-op without kiln-pro or an
            # account; never blocks a print.
            try:
                from kiln._pro_cutter_bridge import record_print_cuts

                planned = record_print_cuts(
                    self.name, file_name, printer_model=self.declared_printer_model() or None
                )
                # Held for the print's end: a backend that can watch the
                # wire reconciles what was charged against what it saw.
                self._cutter_print = {"file": file_name, "planned": planned, "observed": 0}
            except Exception:  # noqa: BLE001 — cut bookkeeping never blocks a print
                import logging as _logging

                _logging.getLogger(__name__).debug(
                    "cutter count recording failed", exc_info=True
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
            # Whatever must follow EVERY print Kiln starts attaches here,
            # through register_print_started_hook, rather than being wired
            # into each door that starts one and missing the next door.  The
            # print watchdog is the reason this exists.  Last, so nothing a
            # hook does -- a watchdog waiting for the one a previous print
            # left behind to stop -- delays the stamps and the pending row.
            _fire_print_started_hooks(self, file_name)
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
                # ``confirmed_state``: it looks through a FAULT headline, so a
                # fault raised while the machine kept working still matches here,
                # and it is as strict about staleness as the bare state word was:
                # an expired reading is not evidence that anything ended.
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

    def clear_error(self) -> PrintResult:
        """Acknowledge a latched firmware error so the printer can print again.

        Deliberately NOT abstract, and it refuses by default.  A printer whose
        error nobody knows how to clear must say so, because the alternative —
        a default that pretends to work — is a button that reports success and
        leaves the machine exactly as stuck as it was.  Adapters that know
        their firmware's acknowledgement override this and set
        :attr:`PrinterCapabilities.can_clear_error`.

        This exists because a latched error is a DEAD END, not an
        inconvenience.  Measured on an A1 (2026-08-13): a print cancelled
        during bed levelling left the firmware reporting ``gcode_state=failed``
        with a non-zero ``print_error``, which maps to
        :attr:`PrinterStatus.ERROR`; the pre-flight check then refused every
        subsequent print.  Dismissing the message on the printer's own screen
        cleared the notification but NOT the reported state, so the machine
        showed "ready" while Kiln — correctly — would not start a job.  There
        was no way back through Kiln at all; only a power cycle cleared it.

        The rule this restores is the one the rest of the status stack already
        keeps: Kiln may refuse to act on what a printer reports, but it must
        never leave the user with no way to reconcile the two.

        :returns: A :class:`PrintResult` whose ``success`` says whether the
            acknowledgement was DELIVERED, not whether the printer has since
            gone idle — the caller re-reads state for that, and some firmware
            takes a moment.
        """
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

    # -- filament handling (load / unload / purge) -----------------------
    #
    # TEMPLATE METHODS — adapters must NOT override the three public
    # methods.  Each runs the one shared safety gate (``_prepare_filament_op``:
    # not mid-print, temperature inside the safety profile AND the
    # material's own window AND above the cold-extrusion floor, purge
    # length capped) and only then hands a validated ``FilamentOpPlan`` to
    # the adapter's ``_impl``.  Same shape as start_print/_start_print_impl,
    # for the same reason: no entry point — MCP tool, CLI, recovery flow —
    # can reach a heater or a stepper around the gate, and no adapter can
    # forget it.  Measured need (2026-09-03): a print failed at layer 1 with
    # a clogged hotend and Kiln could only watch the touchscreen wizard fail
    # at its purge step, because nothing composed set_tool_temp and
    # send_gcode into "load this slot" or "is the melt zone clear".

    #: Fallback hotend ceiling for the filament gate; a bound safety
    #: profile tightens it (``_validate_temp`` takes the min).  Bambu
    #: overrides to its hottest hotend; every other backend's
    #: ``set_tool_temp`` already passes this same literal.
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
        """Feed filament to the nozzle.

        Args:
            slot: Which spool to feed on a multi-material unit (the Bambu
                printer's own tray id: ``unit * 4 + slot`` on a chained
                unit, the unit id on an AMS HT).  ``None`` means the external
                / single spool the user has already pushed into the extruder.
            material: Material name, used to choose a temperature when
                *temperature* is omitted and no spool report supplies one.
            temperature: Hotend target in °C.  Checked against the
                printer's safety profile, the material's own window, and
                the cold-extrusion floor; refused outside any of them.
            length_mm: How far a generic G-code backend feeds.  Ignored by
                a backend whose own filament-change routine decides.
            **options: Adapter-specific extras (e.g. ``wait_seconds``).

        Raises:
            PrinterError: If the gate refuses, or the backend cannot do it.
        """
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
        """Retract filament out of the hotend (and back to the spool unit
        where the backend has one).

        Args mirror :meth:`load_filament`.  The hotend must be hot for the
        retract to free the melt zone, so the same temperature gate runs.
        """
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
        """Extrude a short length at temperature — the clog test.

        The result's ``extrusion_verified`` says what the printer could
        honestly tell: ``False`` with a plain-language ``error_hint`` when
        the firmware refused the move or raised an extrusion fault,
        ``True`` only when a real signal confirmed flow, ``None`` when the
        move was accepted and the machine reports nothing either way.

        Args:
            length_mm: Extrusion length, 1–``MAX_PURGE_LENGTH_MM`` mm.
            material / temperature / slot: as :meth:`load_filament`.
        """
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
        """Add what the caller has to know but the backend cannot say.

        Today that is one thing: the printer was PAUSED, so the nozzle is
        parked over the part and whatever came out landed on it.  The gate
        allows a paused printer on purpose -- clearing a clog and resuming is
        the case this exists for -- and saying nothing about the ooze would
        let someone resume onto a blob.
        """
        if plan.printer_paused:
            result.details["printer_paused"] = True
            result.message = (
                f"{result.message} The print is PAUSED, so the nozzle was "
                "parked over the part: check for extruded filament on the "
                "model and wipe the nozzle before resuming."
            )
            result.details["heater"] = f"left at {plan.temperature:g} °C: the print is paused and will need it"
            # The paused print owns its heater from here; a shutdown must not cool it.
            self._release_heater_hold()
            return result
        # A plan sent nothing; a step that is not the last one is the middle
        # of a sequence a person is walking through -- the heater stays as
        # the step's ``leaves`` says, and the finish runs after the last step.
        if plan.options.get("plan_only"):
            result.details["heater"] = "untouched: nothing was sent"
            return result
        if result.next_step is not None:
            result.details["heater"] = "as the step left it -- see leaves; the finish runs after the last step"
            return result
        # The routine ran to its end on this machine.  On a machine with a
        # filament cutter a load or an unload is a cut; which machines those
        # are, and which of the two cut on each, is kiln-pro's table, read
        # when the count is asked for.  Reported here, the one door every
        # backend's load and unload pass through.  A purge or a wipe cuts
        # nothing.
        if plan.action in ("load", "unload"):
            try:
                from kiln._pro_cutter_bridge import record_command_cut

                record_command_cut(
                    self.name, plan.action, printer_model=self.declared_printer_model() or None
                )
            except Exception:  # noqa: BLE001 -- cut bookkeeping never changes a result
                import logging as _logging

                _logging.getLogger(__name__).debug("cutter count recording failed", exc_info=True)
        # Leave the machine the way a person would: heater off, and say so.
        # Measured 2026-09-16 on an A1: a purge parked over the chute, pushed
        # its 30 mm, reported success -- and left the nozzle at 215 °C with
        # nothing in the answer about it.  A hot idle nozzle oozes, cooks the
        # filament in the melt zone, and is the burn hazard the person was
        # told to keep their hands away from.  ``keep_hot=True`` is for a
        # caller about to print; it has to be asked for.
        if plan.options.get("keep_hot"):
            result.details["heater"] = f"left ON at {plan.temperature:g} °C (keep_hot was asked for)"
            result.message = f"{result.message} Heater left ON at {plan.temperature:g} °C, as asked."
            # Asked for, like set_temperature: the caller owns it, the watchdog covers it.
            self._release_heater_hold()
            return result
        # First the retract that stops the drool.  A nozzle at print
        # temperature with the melt zone still pressurised oozes for the
        # whole minute it takes to cool, and the vendor never leaves one that
        # way: Bambu's end-of-print sequence pulls back 0.8 mm at 1800 mm/min
        # before anything cools (bambu_a1_end_gcode.gcode line 15,
        # 'G1 E-0.8 F1800 ; retract'; the same line in the P1/X1 end files).
        # Measured 2026-09-16 on an A1: purge and wipe both ended with the
        # heater off and the nozzle still dripping into the chute.  Not
        # after an unload -- there is nothing left to pull back.
        retracted = False
        # Only after a SUCCESSFUL op: a purge the firmware refused, or a heat
        # that never arrived, has nothing pressurised to pull back, and a
        # retract on a cold extruder is the cold-extrusion move every gate
        # here exists to prevent.  The heater still goes off either way.
        # A backend whose own sequence already pulled back (the wipe snaps
        # its tail with the vendor's retract and ends cold) reports that in
        # ``end_retract_mm`` and is not retracted twice.
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
        """Kiln just set the hotend to *target* for a routine of its own:
        register the heater-off, so a server stopped mid-routine sends it.

        Measured 2026-09-18: a purge's heat, extrude and watch run longer
        than a client waits, and a host that restarts the server in that
        window leaves whatever the routine had switched on.  The heater is
        the hazard; this is the memory of it outside the routine's frame.
        Released by :meth:`_finish_filament_op` once the heater is off, or
        when the caller asked to keep it hot.
        """
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
        """What happens between "heater off" and the answer, and the sentence for it.

        A heater switched off is not a nozzle that has stopped: the melt
        zone keeps draining for the minute it takes to cool.  An op that ran
        from a served plan carries the plan's ``finish`` block -- the
        cool-down the machine's own start sequence uses: fan on, wait for the
        hand-off temperature, fan off -- and :func:`kiln.printers.motion_plan.run_finish`
        runs it and reports the reading it answered at.  The public floor is
        the honest alternative: say the nozzle is still hot and keep hands
        away, never claim a cool-down that did not run.  ``M106`` is the
        standard part-fan G-code on every backend here.
        """
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
        """The single gate every filament door passes through.

        Refuses (``PrinterError``) rather than adjusting: a caller who asked
        for 300 °C on a PLA tray is told why, not quietly given 220.
        """
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

        # Not while printing.  A pause is allowed: purging through a clog
        # and resuming is exactly the mid-print recovery this exists for.
        try:
            state = self.get_state()
        except PrinterError as exc:
            raise PrinterError(
                f"Cannot {action} {what}: the printer did not answer a status "
                f"request ({exc})."
            ) from exc
        # ``effective_state``: a fault takes the headline while the machine
        # goes on doing what it was doing, and this asks what it is doing.
        # The bare state word would let a faulted print through this refusal
        # -- the one direction it must never fail in, because the extruder is
        # parked over the part.  Reading through staleness as well is
        # deliberate: an expired reading cannot show a print has stopped.
        if state.effective_state == PrinterStatus.PRINTING:
            raise PrinterError(
                f"Refusing to {action} {what} while a print is running. "
                "Pause the print first, or wait for it to finish."
            )
        # Allowed, and the whole point of the paused case -- but the extruder
        # is parked over the part, so whatever comes out lands on it.  The
        # caller is told rather than left to find out.
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
        # The material's own window, when the spool or table gave one.
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
        """``(nozzle_min, nozzle_max, source)`` for the filament in play.

        Default: Kiln's material table by name.  A backend that can READ
        the spool — Bambu's AMS reports ``nozzle_temp_min`` / ``max`` per
        tray — overrides this so the spool's own numbers win.  ``None``
        means no window is known and the caller must pass a temperature.
        """
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
        """Backend load, called AFTER the filament gate passed.

        Never call directly — callers use :meth:`load_filament`.  A backend
        with no honest way to do this raises
        :class:`FilamentHandlingUnsupported` naming what the user can do
        instead; it must not return a ``success=True`` it cannot stand
        behind.
        """

    @abstractmethod
    def _unload_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend unload, called AFTER the filament gate passed.  See
        :meth:`_load_filament_impl` for the honesty rule."""

    @abstractmethod
    def _purge_filament_impl(self, plan: FilamentOpPlan) -> FilamentOpResult:
        """Backend purge, called AFTER the filament gate passed.

        Set ``extrusion_verified`` only from a signal the printer produced;
        leave it ``None`` when it produced none.
        """

    # -- nozzle wipe, and where a purge goes -------------------------------
    #
    # Measured on an A1 (2026-09-15): purge_filament sent ``M83 / G1 E30 /
    # M82`` with the head sitting at home, 30 mm of molten PLA hung off the
    # nozzle over the machine, and the answer said only that no fault was
    # raised.  The printer's own wizard parks over the purge chute and wipes
    # on the pad.  The fix is one shared position record per model
    # (``purge_station`` in the printer catalogue, copied from the vendor's
    # own start G-code and cited line by line), read by ONE helper below,
    # used by every door -- purge, load, unload, and the wipe -- and an
    # answer that always says where the plastic went.  A model with no
    # verified record gets a refusal (wipe) or an in-place purge that says
    # so; never a coordinate Kiln inferred, and never a sibling's.

    def wipe_nozzle(
        self,
        *,
        material: str | None = None,
        temperature: float | None = None,
        slot: int | None = None,
        **options: Any,
    ) -> FilamentOpResult:
        """Clean the nozzle tip on the machine's own wipe pad.

        Runs the same gate as the other filament doors (not mid-print, the
        safety ceiling, the material's own window, the 170 °C floor) so the
        tip is soft when it meets the pad, then hands the plan to
        :meth:`_wipe_nozzle_impl`.  A backend with no verified pad position
        for the connected model refuses and names what to use instead — the
        printer's own screen, or a print's start sequence, which wipes on
        the pad — rather than moving the head to a guessed coordinate.

        Args mirror :meth:`purge_filament`; there is no length.  Two more
        are read where the wipe's plan describes its steps:
        ``plan_only=True`` returns the steps and sends NOTHING; ``step=N``
        sends only step N and describes step N+1, and the finish (heater
        off, the cool-down) runs only after the last step.  ``plate_clear``
        is a PERSON's statement that the plate is empty, read on every
        call by a wipe whose plan presses the plate
        (:class:`PlateClearRequired` without it).
        """
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
        """Backend wipe, called AFTER the filament gate passed.

        Deliberately not abstract: the honest default is a refusal, and it
        stays the answer on every backend until one reads a verified pad
        position for the model out of the catalogue (see
        :meth:`purge_station`).  Never a stub that returns success.
        """
        raise FilamentHandlingUnsupported(
            f"{self.name} has no nozzle-wipe routine in Kiln. Use the printer's "
            "own screen, or start a print — its start sequence wipes on the pad."
        )

    # ------------------------------------------------------------------
    # Homing -- the one verb every printer screen has
    # ------------------------------------------------------------------

    #: Axes a successful :meth:`home_axes` COMMANDED, in this process.  Not a
    #: claim the printer confirmed them; a later reader that needs the
    #: machine's own word must ask the machine.
    _homing_commanded_axes: frozenset[str] = frozenset()

    @property
    def homing_commanded(self) -> frozenset[str]:
        """Axes this adapter has sent a homing for since it was built."""
        return self._homing_commanded_axes

    def _plate_witness(self) -> str | None:
        """A camera frame of the plate, saved to disk, or ``None`` without a camera.

        Taken before any motion that presses the nozzle onto the plate and
        handed back with the refusal, so the person answering ``plate_clear``
        has looked.  Never a gate on its own: the camera cannot prove a plate
        empty (angle, shadow, a plate-coloured part), and Kiln does not
        pretend it can.  Failures return ``None`` -- a missing witness is
        reported, never invented.
        """
        try:
            if not getattr(self.capabilities, "can_snapshot", False):
                return None
            data = self.get_snapshot()
            if not data:
                return None
            import tempfile
            import time as _time

            path = Path(tempfile.gettempdir()) / f"kiln_plate_{int(_time.time())}.jpg"
            path.write_bytes(data)
            return str(path)
        except Exception:  # noqa: BLE001 -- a witness is a courtesy; its absence is reported
            return None

    def _plate_raise_block(self, clearance: float | None) -> tuple[Any, float | None, bool]:
        """``(plate_state, raise_clearance_mm, blocked)`` for a raise-and-travel.

        *clearance* is how high the machine's own first raise lifts the head
        before it travels -- from the served plan, or
        :func:`kiln.plate_state.raise_clearance_mm` of a record.  *blocked*
        is True when the record names a part at least as tall as that: the
        head would cross its row lower than the part.  One reading, shared
        by the homing gate and the purge placement, so the two never
        disagree.
        """
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
        """kiln-pro's path around the recorded part, read line by line before it may run.

        The one place a plan enters any door: the generic home and park and
        the Bambu emitter's home and park all come through here, so every
        plan a door runs has passed
        :func:`~kiln.printers.home_around_part.validate_plan` against the
        part on record -- nothing crosses the part's row before a proven
        lift, nothing descends over its footprint, no line Kiln cannot
        read, any Z reference off the part -- and the planner is asked for
        the model this adapter is declared as NOW, not the one the print was
        recorded on.  Nothing here moves the head.

        Returns ``(steps, None)``: a plan to run instead of the door's own
        sequence.  ``(None, None)``: no plan, and the caller refuses as it
        would without kiln-pro.  ``(None, why)``: a plan came back that
        Kiln will not run -- it failed the validator, or the record has no
        footprint or no height to read it against, or the catalogue has no
        motion record for this model -- and the caller's refusal carries
        *why*, in the validator's words.  A plan Kiln cannot judge is a
        plan it does not run; the planner's word is never the permission.
        """
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
        """Refuse a motion the plate record says would meet a part.

        The one gate every backend consults before a raise-and-travel, and
        before a Z home that presses the nozzle onto the plate.  It reads
        :func:`kiln.plate_state.plate_occupancy` -- the record written when
        Kiln starts a print, re-asserted when one is seen ending, and
        cleared only by a person -- and decides:

        * ``plate_clear=True`` in *options*: a person's word, given now.
          Proceed.  (The template has already written it down.)
        * record ``clear`` before a raise-and-travel: proceed without
          asking -- the person said so once and Kiln has seen no print
          since.
        * record ``occupied`` with a known height at or above the vendor
          raise (:func:`~kiln.plate_state.raise_clearance_mm`): the head
          would cross its row lower than the part is tall.  Refuse, for a
          park and a home alike, naming the file, the time and the height.
        * *touches_plate* (a Z home ON the plate) without ``plate_clear``
          on this call: refuse, whatever the record says -- ``occupied``
          names the part, ``clear`` says why the record is not enough,
          ``unknown`` asks the person to look.  The record cannot see a
          print started from the printer's own screen, and the press is
          the one motion a stale "clear" must never answer for.
        * anything else (unknown plate, or a part of unknown height, before
          a raise-and-travel): proceed as before this record existed; the
          step text carries the row caveat.

        Before refusing an occupied plate, kiln-pro's planner is asked once
        (:meth:`_detour_around_part`, which reads every line of the answer
        against the part before handing it over); a plan comes back as
        steps the backend runs instead of its own sequence, and a plan
        the validator refuses is refused here, in its words.  A caller
        whose motion a plan cannot stand in for (a wipe needs the pad)
        passes ``allow_plan=False`` and gets the refusal.  The refusal is
        :class:`PlateClearRequired`, carrying a camera frame where one
        exists, so the person looks before answering.
        """
        from kiln.plate_state import raise_clearance_mm

        if options.get("plate_clear") is True:
            return None
        if clearance_mm is None:
            clearance_mm = raise_clearance_mm(station)
        state, clearance, blocks_raise = self._plate_raise_block(clearance_mm)
        # A recorded "clear" answers the ROW question -- home X and park stop
        # asking -- and nothing more.  It may not stand in for a Z home that
        # presses the nozzle onto the plate: nothing marks the plate occupied
        # when a print is seen STARTING, so a print run from the printer's
        # own screen while no Kiln process watched its end leaves "clear" on
        # file with a part on the plate.  A stale "clear" before a travel
        # restores the behaviour every idle move had before this record
        # existed; a stale "clear" before the press drives a nozzle into a
        # part.  So the press asks every time, and the record is only ever
        # the person's word for the row.
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
        witness = None if options.get("plan_only") else self._plate_witness()
        look = (f" -- look at {witness} first" if witness else
                " -- this printer has no camera Kiln can read, so look at the plate yourself")
        # What the motion does to the plate, and what still works without a
        # person's word: the plan's own words for a wipe (its record names
        # the datum it takes and where the head has to cross), the homing
        # sentence otherwise.  A wipe's refusal names the wipe's tool and
        # command, never home_axes.
        contact = contact or (
            "homes Z by pressing the nozzle onto the PLATE (its own sequence: 'find a soft place to home')"
        )
        fallback = fallback or 'Until then, home X and park still work: axes="XY", or park_head.'
        tool, cli = (("wipe_nozzle", "kiln filament wipe --plate-clear") if action == "wipe"
                     else ("home_axes", "kiln home --plate-clear"))
        say_so = (
            f"then say so on the call: plate_clear=true on {tool} ({cli}) -- "
            f"the press asks every time; `kiln plate clear` records it for the row checks only. {fallback}"
            if touches_plate and not blocks_raise
            else f"then say so: `kiln plate clear`, or plate_clear=true on {tool}."
        )
        if blocks_raise:
            assert clearance is not None and height is not None
            message = (
                f"Refusing to {action} {model}: {state.describe()}, and the first motion lifts the "
                f"head only {clearance:g} mm before it crosses the row -- the part is taller than that, "
                f"and on this family a travel collision raises no fault.{around} Clear the plate{look}, {say_so}"
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
        # Every door's plate refusal passes here -- the Bambu emitter's home,
        # park and wipe as much as the generic gate -- so this is where it is
        # tallied: a Z touch on the vendor's own descent, one decided by a
        # blank cell (the caller says which), or a sideways move over a part.
        self._count_motion_refusal(
            "PLATE_CLEAR_REQUIRED",
            refusal_reason or ("on_plate" if touches_plate else "plate_occupied"),
        )
        raise PlateClearRequired(message, snapshot_path=witness)

    def declared_printer_model(self) -> str:
        """The printer_model this adapter was built with, as the person wrote it.

        Every door that builds an adapter -- the env variables, a
        config.yaml entry, the register tool -- hands the declared model to
        :meth:`set_safety_profile`; the Bambu adapter also keeps it as
        ``_printer_model`` for its own emitter.  This is the ONE accessor
        the motion facts, the plate gate and the refusals read, so a generic
        backend built from config.yaml is looked up by the same string a
        Bambu one is.  Empty when nobody declared a model.  Never the global
        resolver: with two machines registered that answers for the default
        printer, and a fact borrowed from the default printer is how the
        second machine gets the first one's homing.
        """
        own = str(getattr(self, "_printer_model", "") or "").strip()
        if own:
            return own
        return str(getattr(self, "_safety_profile_id", "") or "").strip()

    def motion_facts(self) -> Any:
        """The catalogue's motion block for the declared model, or ``None``.

        The declared string is resolved the way every other door resolves a
        printer hint: the catalogue key itself (a vendor prefix tolerated),
        then the shared hint table (``creality_k1`` → ``k1``, ``Voron
        Trident 300`` → ``voron_trident``).  ``None`` means "Kiln does not
        know this machine", and every caller treats that as a reason to
        ask, never to guess.
        """
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
        """Let the connected machine's own config settle what the vendor left per unit.

        Only a backend that can read something about its own motion takes
        part -- the Klipper family through Moonraker's ``configfile`` object,
        a Marlin machine on USB through its M115 / M211 / M119 reports --
        and only when the catalogue row has a null in a cell such a read can
        fix: a home spot that is a placeholder in the reference config, a Z
        ceiling that differs by build, whether a bare G28 lifts first.  The
        machine is read once per adapter and nothing is written anywhere; a
        machine that cannot be asked leaves the catalogue's answer as it
        was, so a transport fault can only keep a refusal, never lift one.
        """
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
                # Only an answer is kept.  A machine that could not be asked
                # (offline, mid-print) is asked again on the next call.
                setattr(self, cache_key, source)
        if source is None:
            return facts
        from kiln.machine_motion import fill_from_machine

        kind, payload = source
        return fill_from_machine(facts, kind, payload)

    def _read_machine_motion_source(self) -> tuple[str, Any] | None:
        """What this machine can say about its own motion, and in which dialect.

        ``("klipper_config", <configfile.config mapping>)`` from a backend
        that exposes Moonraker's ``configfile`` object; a Marlin backend
        overrides this to hand over its firmware reports as
        ``("marlin_report", MarlinMotionReport)``.  ``None`` from a machine
        that publishes nothing about itself (Bambu, Elegoo, Prusa Link), or
        one that could not be asked just now.  An answer is cached for the
        adapter's life; ``None`` is asked again next time.
        """
        reader = getattr(self, "get_printer_config", None)
        if reader is None:
            return None
        config = reader()
        return None if config is None else ("klipper_config", config)

    def _count_motion_refusal(self, code: str, reason: str) -> None:
        """One tally per refusal, keyed by model, code and why -- the only trace it leaves.

        Every door (the tool, the CLI, the doctor's probe) and every backend
        (the generic gate, the Bambu emitter's home, park and wipe) reaches
        a refusal through this class, so the count is taken here rather
        than where each door words its error.  It is the only evidence that a model
        people own is missing from the catalogue, or that a blank cell is
        what refused them, without anyone filing a report.
        """
        try:
            from kiln.daily_stats import record_motion_refusal

            record_motion_refusal(self.declared_printer_model() or None, code, reason)
        except Exception as exc:  # noqa: BLE001 -- a counter never blocks a refusal
            logger.debug("motion refusal count skipped: %s", exc)

    def _declare_model_text(self) -> str:
        """The door for an install that never said which printer this is.

        Most installations with a printer adapter never declare a
        ``printer_model`` (27 of 66 in the 2026-09-16 usage count), and no
        catalogue fact reaches them.  The refusal says so once, in the
        tool's own reply, and names the fix -- never a guess at the model.
        """
        model = self.declared_printer_model()
        if model:
            return (
                f"Kiln has no motion record for {model!r} -- not a catalogue key it knows. "
                "Run `kiln setup` (it asks which printer this is and writes the answer), or set "
                "printer_model for this printer in config.yaml (KILN_PRINTER_MODEL for the env door) to a "
                "catalogue key from printer_intelligence.json."
            )
        return (
            "Kiln does not know which printer this is: no printer_model is declared for it. "
            "The facts that decide whether the head may move -- which part moves in Z, how Z is found "
            "and where, whether the firmware refuses an unhomed move -- are looked up by model. Run "
            "`kiln setup` (it asks which printer this is and writes the answer), or set printer_model "
            "for this printer in config.yaml (KILN_PRINTER_MODEL for the env door) to a catalogue key "
            "such as bambu_a1, bambu_p1s, prusa_mk4, k1 or ender3_v3_ke, and call again."
        )

    @staticmethod
    def _refused_path_clause(why: str | None) -> str:
        """The sentence a plate refusal carries when a plan came back and was refused.

        Empty when there was no plan: the refusal then reads exactly as it
        did before the planner existed.  The person is told that a path
        was offered and why Kiln would not run it, in the validator's own
        words, so "clear the plate" is not the only thing they hear.
        """
        if not why:
            return ""
        return (
            " Kiln Pro's planner offered a path around it, and Kiln will not run that path: "
            + why.rstrip(". ") + "."
        )

    def _motion_gate(
        self, options: dict[str, Any], *, axes: str, action: str,
    ) -> tuple[Any, list[HomeStep] | None]:
        """The generic backend's answer to "may the head move?", before any G-code.

        Reads the catalogue's motion block for the declared model and the
        plate record, and decides:

        * ``plate_clear=True`` in *options*: a person's word, given now.
          Proceed with whatever the person asked for.
        * no motion record (no ``printer_model``, or a key the catalogue
          does not know): refuse with the declaration door -- Kiln will not
          home Z or park a machine it cannot describe.  Homing X and Y only
          is allowed, with the blind-travel caveat.
        * a Z home whose method lands on the plate (every method but a top
          switch, a switch off the print surface, or a dedicated strip --
          and an unknown method counts as landing): the plate gate, with
          the vendor's own description of the descent in the refusal.
        * a park while the plate record says a part is there: ask kiln-pro's
          planner, as home does, for a path that lifts first and never
          descends over the part -- run in place of the firmware's own X/Y
          home (:meth:`_park_head_impl`) -- and refuse without one: a
          generic backend cannot say how high the head is before it
          travels sideways.

        Returns ``(motion facts or None, detour steps or None)`` -- the facts
        for the caller's plan text, and the served path around a recorded
        part for it to run instead of its own sequence.  Every detour has
        been read against the part by :meth:`_detour_around_part` -- the
        one seam both actions share -- before it is handed back.
        """
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
            # The plan is asked for, not assumed: public Kiln refuses to press
            # a nozzle onto a recorded part, and kiln-pro's planner may know a
            # path that homes around it.  What comes back is run INSTEAD of
            # this backend's own G28 (see :meth:`_home_axes_impl`), so a plan
            # is accepted here only because there is somewhere to run it --
            # the reason this asked for none before the run path existed.
            detour = self._plate_gate(options, station=None, action=action, touches_plate=True,
                                      allow_plan=True, contact="homes Z by " + motion.describe_z_home(),
                                      refusal_reason="on_plate" if motion.z_home_known else "unknown_method")
        if action == "park":
            from kiln.plate_state import plate_occupancy

            state = plate_occupancy(self)
            if state.occupied:
                # The same question home asks, through the same seam: a
                # served path lifts before it travels and never descends
                # over the part, which is exactly what this backend's own
                # X/Y home cannot promise.  No station and no vendor raise
                # here, as for the generic home: the firmware's routine
                # lifts nothing before it travels.
                detour, refused = self._detour_around_part(state, station=None, action="park", clearance_mm=None)
                if detour is not None:
                    return motion, detour
                witness = None if options.get("plan_only") else self._plate_witness()
                look = (f" -- look at {witness} first" if witness else
                        " -- this printer has no camera Kiln can read, so look at the plate yourself")
                self._count_motion_refusal("PLATE_CLEAR_REQUIRED", "plate_occupied")
                raise PlateClearRequired(
                    f"Refusing to park {motion.printer_id}: {state.describe()}, and on this backend the "
                    f"park is the firmware's own X/Y home, which travels sideways at whatever height the head "
                    f"has now -- Kiln cannot read that height here.{self._refused_path_clause(refused)} "
                    f"Clear the plate{look}, then say so: `kiln plate clear`, or plate_clear=true on park_head.",
                    snapshot_path=witness,
                )
        return motion, detour

    def home_axes(self, *, axes: str = "XYZ", **options: Any) -> HomeResult:
        """Home the head -- what the Home button on the printer's screen does.

        The template every backend shares.  It refuses while a print is
        running or paused (the head is over the part, and homing travels),
        then hands the request to :meth:`_home_axes_impl`.  Backends do not
        override this method; the contract test forbids it, because the
        refusal here is the one safety check homing carries.

        The answer says how the homing was sent, which axes its commands
        addressed, and where the head was left -- every time.  A backend
        with a vendor-cited sequence for the connected model runs that
        (``sequence_source: "vendor_start_sequence"``); one without sends
        the firmware's own home and says so (``"firmware_home_routine"``),
        after the catalogue's motion record has answered how that home
        finds Z (:meth:`_motion_gate`); one that cannot home the way Kiln
        trusts raises :class:`HomingUnsupported` naming what to use
        instead, and one with no declared model raises
        :class:`ModelDeclarationRequired`.

        Args:
            axes: Any of ``X``, ``Y``, ``Z`` (default all three).  A backend
                whose sequence cannot address one of them says so in
                ``homed_axes`` rather than pretending.
            options: Adapter-specific extras (``wait_seconds``,
                ``wait_ceiling_seconds``), forwarded verbatim.  Two are
                read by every backend that can describe its sequence:
                ``plan_only=True`` returns the steps and sends NOTHING;
                ``step=N`` sends only step N and describes step N+1.
                Step mode is how a sequence is run the first time on a
                machine with a person beside it: one motion, one report,
                then the next ``go`` -- a single script cannot be paused
                between motions, however well it was announced.
                ``plate_clear=True`` is a PERSON's statement that the plate
                is empty.  A backend whose Z home presses the nozzle onto
                the plate refuses without it (:class:`PlateClearRequired`,
                carrying a camera frame where one exists) -- the vendor's own
                sequence assumes an empty plate at print start, and an idle
                printer may hold a part.
        """
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
        if options.get("plate_clear") is True and not options.get("plan_only"):
            # A person's word, written down: the plate stays clear until the
            # next print starts, so home X and park stop asking about the
            # row.  (A Z home onto the plate still asks on every call; see
            # _plate_gate.)  Recorded on every backend, whether or not this
            # one needed to ask.
            try:
                from kiln.plate_state import mark_clear

                mark_clear(self, "human")
            except Exception:  # noqa: BLE001 -- the record never blocks the motion
                logger.debug("plate_clear could not be recorded", exc_info=True)
        result = self._home_axes_impl(wanted, dict(options))
        if result.success:
            self._homing_commanded_axes = self._homing_commanded_axes | frozenset(result.homed_axes)
        return result

    def _read_homed_axes(self) -> set[str] | None:
        """Which axes the FIRMWARE says are homed, or ``None`` when it cannot say.

        The signal that turns a homing from ``accepted`` into ``confirmed``.
        Klipper reports it (``toolhead.homed_axes``), RepRapFirmware reports
        it (``move.axes[].homed``); Marlin over OctoPrint does not.  The
        default knows nothing and says so -- never a guess.
        """
        return None

    def homed_axes_now(self) -> set[str] | None:
        """Which axes the firmware says are homed, asked over the wire NOW.

        Lowercase letters (``{"x", "y", "z"}``, or fewer), or ``None`` when
        this backend cannot say.  A FRESH read on every call, never a
        cached status: a cached reading is a guess, and the one caller of
        this -- the pre-print gate deciding whether a same-bed retry may
        start without homing, next to the failed part still on the bed --
        cannot start a print on a guess.

        Backends that can read it (Klipper's ``toolhead.homed_axes``,
        RepRapFirmware's object model, Creality's Klipper backend) answer
        with the firmware's word and let a transport failure RAISE
        :class:`PrinterError` rather than answer ``None``: "the read failed,
        retry" and "this backend cannot say" are different refusals.  The
        read uses the adapter's own request timeout and retry budget; no
        caller of this waits longer than one status read would.

        The default knows nothing and says so.
        """
        return self._read_homed_axes()

    def homed_axes_field(self) -> str | None:
        """The firmware field :meth:`homed_axes_now` reads, named the way the
        firmware names it (``"toolhead.homed_axes"``), or ``None`` when this
        backend has no such read.  Written into the gate's evidence so the
        audit line says where the answer came from."""
        return getattr(self, "_homed_axes_field", None)

    def _z_lifts_before_home(self) -> bool | None:
        """Whether the firmware's own homing routine lifts Z before X/Y move.

        ``True`` / ``False`` only from the machine's own configuration
        (Klipper's ``safe_z_home`` section, read off the printer); ``None``
        when Kiln cannot see the setting.
        """
        return None

    def _home_axes_impl(self, axes: str, options: dict[str, Any]) -> HomeResult:
        """Backend homing, called AFTER the gate passed.

        Deliberately not abstract.  The default hands the job to the
        FIRMWARE'S OWN homing routine -- ``G28`` on Marlin, Klipper, and
        RepRapFirmware runs the routine the printer maker or the owner
        configured, safe-Z lift included where they set one -- and then
        reports what the machine can honestly say back: ``confirmed`` when
        the firmware reports the axes homed (:meth:`_read_homed_axes`),
        ``accepted`` when it reports nothing.  Where the firmware exposes
        its own position afterwards (:meth:`get_tool_position`) that is
        the resting position, in numbers.  A backend that cannot send
        G-code raises :class:`HomingUnsupported`.  A backend with a cited
        vendor sequence overrides this and runs it.
        """
        if not self.capabilities.can_send_gcode:
            raise HomingUnsupported(
                f"{self.name} cannot home through Kiln: this backend does not "
                "accept G-code. Use the printer's own screen's jog controls instead -- Z UP first, then X and Y, with your eyes on the plate. The screen's Home button descends the nozzle to the bed and is the wrong tool with a part on the plate."
            )
        motion, detour = self._motion_gate(options, axes=axes, action="home")
        if detour is not None:
            # A served path around the part on the plate, run in place of this
            # backend's own G28.  It claims no axis homed -- the firmware's own
            # read is the only thing that may (see ``homed_axes_now``).
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
        """What the person will see when the firmware's own routine runs.

        Built from the catalogue's motion block: what the Z home does and
        where (the vendor's words), whether the routine travels sideways
        before Z is known, and what the firmware does with an unhomed move.
        Without a record the text says so and assumes the worst.
        """
        parts = ["the firmware runs its own homing routine: each axis travels to its endstop"]
        if motion is None:
            parts.append("Kiln has no motion record for this model, so it assumes the Z home descends onto the plate and that the head may travel sideways before Z is known")
            return "; ".join(parts)
        # Name the record, so a declared model that resolved to the wrong
        # machine is visible in the plan, not only in the catalogue.
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
        """Move the head somewhere safe, away from the plate -- and stay there.

        The retreat, as distinct from :meth:`home_axes`, the measurement.
        A park never homes Z: on the machines where Z is found by pressing
        the nozzle onto a plate Kiln cannot see is clear, that is the one
        step a nervous person is right to distrust, and a park does not
        need it.  It raises the head the vendor's way, homes X (an endstop,
        no plate involved), and travels to the model's own off-plate spot.
        A backend with no vendor spot hands the job to the firmware's own
        home, whose position is the park, and says so.

        Same gate as homing (not while printing or paused), same
        ``plan_only`` / ``step`` options, same described steps.
        """
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
        result = self._park_head_impl(dict(options))
        result.action = "park"
        if result.success and not options.get("plan_only"):
            self._homing_commanded_axes = self._homing_commanded_axes | frozenset(result.homed_axes)
        return result

    def _park_head_impl(self, options: dict[str, Any]) -> HomeResult:
        """Backend park, called AFTER the gate passed.

        The default is the firmware's own home: on Marlin, Klipper and
        RepRapFirmware the home position IS the machine's safe park, chosen
        by whoever configured it, so ``G28`` is the honest park -- reported
        as such, never as a position Kiln chose.  With a part on the plate
        the gate may instead hand back a served path around it (lift, home
        X and Y at that height, travel to the vendor's park spot around the
        footprint), run in place of the firmware's own home exactly as the
        home door runs its detour, and claiming no axis homed.  A backend
        with a cited vendor spot overrides this; one that cannot send
        G-code raises :class:`HomingUnsupported`.
        """
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
            # The vendor's Z home would press onto a plate nobody has vouched
            # for: park is the firmware's X/Y home only, Z untouched.
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
        """This machine's verified purge and wipe positions, or ``None``.

        Read from the printer catalogue at the depth this caller is served
        (``kiln.printer_intelligence``, where kiln-pro's overlay supplies the
        ``purge_station`` block; the public file carries none), keyed by the
        CONFIG-DECLARED model only -- the adapter's own ``_printer_model``.
        Never the global resolver, which answers for the default printer:
        with two machines registered that is how the second would be driven
        to the first one's chute.  Never a self-report either (see
        :meth:`get_printer_info`): a wrong guess here is a head driven into a
        frame.  ``None`` means "Kiln has no verified position for this
        model", and every caller treats it as a reason to say so, not to
        infer one.
        """
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
        """Whether this backend may drive *capability* from *station*, and why not.

        The base knows two things: no record, no motion; and no emitter, no
        motion either.  A record is a set of figures, not a sequence -- the
        base class has nothing that reads one, so a backend that inherits
        this refuses even when the catalogue carries a record for the
        declared model (a Klipper machine declared as ``bambu_a1`` is not
        parked over a chute by a base class that never sends the park).  A
        backend with a real emitter overrides this to check the record's
        geometry and the figures the capability needs, and to quote the
        record's own reason.
        """
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
        """Where a purge is about to go, as the caller must be told.

        ``status`` is ``"parked"`` when a verified station exists and the
        printer is idle, else ``"in_place"``, with ``reason`` saying why and
        ``position`` the head's reported coordinates where the backend has
        any (most do not).  A paused print stays in place on purpose: the
        head is parked over the part and Kiln does not travel mid-print.
        """
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
        # The travel to the chute starts with the vendor's raise and crosses
        # the head's row; a recorded part taller than that raise is in its
        # path.  The head stays where it is -- an in-place purge moves
        # nothing -- and the answer says why.
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
                    "run `kiln plate clear` to park over the chute again"
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
        """Poll ``get_state`` until the hotend reads at or below *threshold*.

        The cooling twin of :meth:`_wait_for_hotend`; ``(reached,
        last_reading)``.  A reading the backend cannot produce counts as
        not reached, never as reached.
        """
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
        """Poll ``get_state`` until the hotend is within *tolerance* of
        *target*.  Returns ``(reached, last_reading)``.

        A thermistor reading is a genuine signal, and the one every backend
        has, so the shared sequence uses it before any extrude.
        """
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
        """Heat, wait for the thermistor, then one relative E move.

        *pre_move_check* is an optional callable run after the hotend is at
        temperature and before the move; it returns ``(refusal_reason,
        source)`` to stop the sequence on a genuine printer signal (Klipper's
        ``extruder.can_extrude``) or ``None`` to proceed.

        *pre_gcode* is sent BEFORE the heater command — a backend that knows
        a purge position parks there first, so the melt that oozes while
        heating goes where the flush goes.  *post_gcode* rides in the same
        script as the E move, after it and before ``M82`` (a tail snap and a
        shake, still in relative E).  *placement* is the
        :meth:`_purge_placement` record for the answer; when the move
        extrudes and none is given, the in-place one is built here, so no
        backend can extrude without saying where.

        The generic feed/retract/purge every G-code backend shares: the
        ``M104`` / ``M83`` / ``G1 E`` / ``M82`` sequence is the same on
        Marlin, Klipper, RepRapFirmware and Bambu.  What differs per
        backend is what it can report back, so a firmware refusal (raised
        by the adapter's own transport as ``PrinterError``) becomes
        ``success=False`` with the firmware's words in ``error_hint``, and
        an accepted move is reported as exactly that — accepted, flow
        unverified — unless the adapter layers a real signal on top.
        """
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

    #: Aliases accepted for the single generic default part-cooling fan.
    #: Unlike Bambu's fixed part/aux/chamber layout (a protocol Bambu itself
    #: controls end-to-end), generic Marlin/Klipper firmware has no
    #: standardized auxiliary or chamber fan -- a machine may have neither,
    #: or expose one only through a printer-specific macro Kiln has no way
    #: to discover automatically.  So a generic ``set_fan`` supports ONLY
    #: this one fan; adapters reject anything else rather than guess.
    _PART_COOLING_FAN_ALIASES: frozenset[str] = frozenset({"part", "part_cooling", "cooling"})

    @staticmethod
    def _gcode_lines(commands: Any) -> list[str]:
        """*commands* as a list of whole G-code lines, or a loud refusal.

        Every adapter consumes this argument by iterating or joining it, so a
        bare string is not a one-line script — it is eight commands, one per
        character.  Measured on 2026-09-06: ``send_gcode("M220 S50")`` put
        ``'M\\n2\\n2\\n0\\n \\nS\\n5\\n0'`` on the Klipper and Duet wires and eight
        separate writes on a USB serial link, and every layer above reported
        success.  Two callers had it (a calibration pipeline and a fleet speed
        tool), and nothing caught either.

        Refusing here is the engine fix: a caller that passes a string learns
        immediately instead of silently corrupting what the printer executes.
        """
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

    # Every write below answers with a :class:`CommandVerdict` (see
    # kiln.printers.command_verdict): ``confirmed`` when the adapter read the
    # effect back from the printer, ``accepted`` when the transport took the
    # command and nothing more is known.  Adapters not yet migrated may still
    # return a bool; callers lift it with ``CommandVerdict.coerce`` and a bare
    # ``True`` reads as ``accepted``, never ``confirmed``.  Refusal is a
    # ``PrinterError``, never a quiet ``False``.

    @abstractmethod
    def set_tool_temp(self, target: float) -> CommandVerdict | bool:
        """Set the hot-end (tool) target temperature in degrees Celsius.

        Args:
            target: Desired temperature.  Pass ``0`` to turn the heater off.

        Returns:
            A :class:`CommandVerdict` — ``confirmed`` if the adapter saw the
            target change in a report that postdates the command,
            ``accepted`` if it was sent and not refused.

        Raises:
            PrinterError: If the command could not be sent.
        """

    @abstractmethod
    def set_bed_temp(self, target: float) -> CommandVerdict | bool:
        """Set the heated-bed target temperature in degrees Celsius.

        Args:
            target: Desired temperature.  Pass ``0`` to turn the heater off.

        Returns:
            A :class:`CommandVerdict`; see :meth:`set_tool_temp`.

        Raises:
            PrinterError: If the command could not be sent.
        """

    # -- G-code ---------------------------------------------------------

    @abstractmethod
    def send_gcode(self, commands: list[str]) -> CommandVerdict | bool:
        """Send one or more G-code commands to the printer.

        Args:
            commands: List of G-code command strings, e.g.
                ``["G28", "G1 X10 Y10 Z5 F1200"]``.

        Returns:
            A :class:`CommandVerdict`.  Raw G-code has no general read-back,
            so an adapter answers ``accepted`` unless its transport reports
            execution (a synchronous request/response link may).

        Raises:
            PrinterError: If sending fails.
        """

    # -- webcam snapshot (optional) ------------------------------------

    def get_snapshot(self) -> bytes | None:
        """Capture a webcam snapshot from the printer.

        Returns raw JPEG/PNG image bytes, or ``None`` if webcam is not
        available or not supported by this adapter.  This is an optional
        method -- the default implementation returns ``None``.

        A camera the user registered (:meth:`set_external_camera`) is asked
        FIRST, on every adapter: an override of this method is wrapped at
        class creation (see ``__init_subclass__``), so the user's camera is
        honoured by every door that reads a frame without any door knowing
        the camera exists.
        """
        if self._external_camera is not None:
            return fetch_external_snapshot(self._external_camera)
        return None

    # -- webcam streaming (optional) -----------------------------------

    def get_stream_url(self) -> str | None:
        """Return the MJPEG stream URL for the printer's webcam.

        Returns the full URL to the live video stream, or ``None`` if
        streaming is not available.  This is an optional method -- the
        default implementation returns ``None``.  A user-registered camera's
        stream wins, the same way as for :meth:`get_snapshot`; the URL may
        carry credentials and is for opening a stream, never for a reply or
        a log (see :func:`redact_url_credentials`).
        """
        return _external_stream_url(self._external_camera, None)

    # -- live video (optional) -----------------------------------------

    def stream_capability(self) -> StreamCapability:
        """Whether Kiln's local relay can carry this printer's live video.

        The default answers from the stream URL: an http(s) MJPEG URL (the
        user's registered camera first, then the backend's own) can be
        relayed; an RTSP one cannot (the relay re-serves MJPEG over HTTP and
        does not re-mux); no URL means no live video from this backend.
        Adapters whose camera speaks its own protocol override this.
        """
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
        """The relay's frame source for this printer, or ``None``.

        ``None`` exactly when :meth:`stream_capability` says the relay has
        nothing to carry — the reason lives there, not here.
        """
        if not self.stream_capability().available:
            return None
        from kiln.streaming import HttpMjpegSource

        url = self.get_stream_url()
        return HttpMjpegSource(url) if url else None

    # -- camera check (optional) -----------------------------------------

    #: Ids of the addresses :meth:`camera_probes` checks for this printer
    #: type.  Declared on the class and read without contacting the printer,
    #: so a refused start can say a check exists.  Empty means no check.
    camera_check_ids: ClassVar[tuple[str, ...]] = ()

    def camera_probes(self) -> list[Any]:
        """The addresses this printer's own camera may answer on, to check.

        Each is a :class:`kiln.camera_check.CameraProbe` on the printer's own
        host, carrying the basis for trying it, with an id from
        :attr:`camera_check_ids`.  Called only for a check the user asked
        for, and it may contact the printer.  A camera the user registered
        does not change the answer: the check is about the printer's own
        camera.  The default is no addresses.
        """
        return []

    # -- a camera the user supplied ------------------------------------
    #
    # Class-level default so no adapter's __init__ has to know about it.

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
        """Register (or with no URLs, clear) a camera the user supplies.

        Raises ``ValueError`` when a URL is not http(s) or rtsp(s).
        """
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
        """Whether :meth:`capture_snapshot` has any camera to ask.

        The capability flag describes the printer's own camera; a camera
        the user supplied makes a camera-less printer watchable.
        """
        return self._external_camera is not None or bool(
            getattr(self.capabilities, "can_snapshot", False)
        )

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

    def reported_firmware_version(self) -> str | None:
        """The firmware version this printer reports, as text, or ``None``.

        The one accessor the heartbeat reads, so Kiln can tell which
        firmware the printers out in the wild actually run -- the version a
        maker's published code is read for is not always the one shipped.
        Each backend answers from what it already holds (a Bambu's cached
        module list, Klipper's own version line, a Marlin machine's M115
        report read for its motion facts); nothing here sends a command
        the print could feel.  Never a serial number, never an address.
        """
        source = getattr(self, "_motion_machine_source", None)
        if isinstance(source, tuple) and len(source) == 2 and source[0] == "marlin_report":
            report = source[1]
            name = str(getattr(report, "firmware_name", "") or "").strip()
            version = str(getattr(report, "firmware_version", "") or "").strip()
            text = " ".join(part for part in (name, version) if part)
            return text or None
        return None

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

    def read_active_slot(self) -> ActiveSlotReading | None:
        """Which slot the machine's multi-material unit is feeding, or ``None``.

        Optional.  A backend whose protocol shows the feeding slot returns an
        :class:`ActiveSlotReading`; the default knows nothing and says so.
        ``None`` means "this backend cannot say" -- never "nothing changed".
        A change between two readings is a filament switch the machine made
        on its own; the base class watches for it (:func:`_feed_slot_observer`)
        and reports it, so a backend only ever reads.  Sends no command and
        changes nothing on the printer.  Must be cheap: it is asked at most
        once per :data:`SLOT_OBSERVE_MIN_INTERVAL_S` from the status path.
        """
        return None

    #: Backends with their own push stream observe slot changes natively on
    #: that stream and set this so the polled observer stays out of the way.
    _slot_observer_native: bool = False

    def _kiln_is_driving(self) -> bool:
        """Is a slot change on this machine something Kiln itself caused?

        True for the settle window after Kiln's own load / unload / cancel,
        and while a print Kiln started is engaged (its planned changes were
        charged at the start; changes during it are counted against that
        charge, not reported again).
        """
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
        """A print Kiln started has ended: hand what it was charged, and what the
        machine showed, to the cutter bridge.  Never raises; never blocks."""
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
        """The nozzle this printer holds on record for itself, or ``None``.

        Optional.  A backend whose protocol exposes the machine's own nozzle
        setting returns it as a :class:`NozzleSetting`; the default knows
        nothing and says so.  ``None`` means "this backend cannot say", which
        is a different fact from "the machine agrees with anything" and must
        never be read as one.  Sends no command and changes nothing on the
        printer.
        """
        return None

    def read_nozzle_clumping_detection(self) -> NozzleClumpingDetection | None:
        """Whether this printer's own nozzle-clumping-detection switch is on,
        or ``None``.

        Optional.  A backend whose protocol exposes the machine's own switch
        returns a :class:`NozzleClumpingDetection`; the default knows nothing
        and says so.  ``None`` means "this backend cannot say" -- a different
        fact from "off", and never to be read as one.  A backend that can see
        the channel but has not verified its meaning for this model returns
        a reading with ``enabled=None`` and the reason.  Sends no command and
        changes nothing on the printer.
        """
        return None

    def get_multi_material_status(self) -> Any | None:
        """What multi-material unit this printer carries, read live.

        Returns a :class:`kiln.multi_material.MultiMaterialStatus` — the
        one record every door that cares about filament changes reads —
        or ``None`` when this backend knows no multi-material path at
        all.  A backend that CAN look but the read fails should RAISE
        :class:`PrinterError`: the shared reader turns that into
        ``kind="unknown"`` carrying the reason, which is a different fact
        from ``None`` and must not be reported as one.  Optional; the
        default knows nothing and says so.  Backends that implement it
        advertise :attr:`PrinterCapabilities.can_report_multi_material`.
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

    def read_print_file(self, file_name: str) -> bytes | None:
        """The bytes of *file_name* as the printer holds them, or ``None``.

        The pre-print gate (:func:`kiln.printers.print_gate.run_adapter_gate`)
        calls this when a print is started BY NAME and no local copy of the
        file exists to inspect -- a file uploaded in an earlier session, or
        by another client.  The upload door already refuses a file with no
        homing before its first print move (incident #0); this is how the
        start-by-name door gets the same look at the same bytes, so the two
        doors cannot disagree about one file.

        ``None`` means this backend cannot read a file back, and the gate
        soft-passes as it always did.  A backend that CAN read back returns
        the whole file (a ``.gcode`` body or a ``.3mf`` archive) and raises
        :class:`PrinterError` when the read fails; the gate logs that and
        soft-passes rather than blocking a print on a transfer fault.

        Args:
            file_name: Name (or path) of the file as known by the printer --
                exactly what :meth:`start_print` was given.
        """
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


# The base class's own concrete control methods, gated once.  Every adapter
# that INHERITS one is covered by this; an adapter that overrides one is
# covered by __init_subclass__.  Abstract methods are skipped there, so the
# implementing subclass is what gets wrapped.
_install_engagement_gate(PrinterAdapter, own_methods_only=False)


# ---------------------------------------------------------------------------
# Print lifecycle hooks
# ---------------------------------------------------------------------------
#
# A rule that must follow EVERY print Kiln starts cannot live in the doors
# that start prints.  There are many -- the MCP tools, the scheduler, the
# pipelines, recovery, kiln-pro's own -- and the print watchdog was wired
# into exactly one of them while the promise it backs was made for all.  So
# such a rule attaches here, at the two points every door already passes
# through: :meth:`PrinterAdapter.start_print` announces a start, and the
# previous-state table both status doors write
# (:func:`kiln.auto_record_hook.observe_state`) announces an ending.
#
# A registry rather than an import, because the rules belong to the layers
# above: the print watchdog is the server's, and an adapter must not import
# the server to start a print.

#: Guards registration only.  Firing reads whichever tuple is current, and a
#: registration replaces the tuple rather than mutating it.
_PRINT_HOOKS_LOCK = threading.Lock()
_PRINT_STARTED_HOOKS: tuple[Callable[[Any, str], object], ...] = ()
_PRINT_ENDED_HOOKS: tuple[Callable[[str], object], ...] = ()


def register_print_started_hook(hook: Callable[[Any, str], object]) -> None:
    """Call ``hook(adapter, file_name)`` after every print Kiln starts.

    Fired by :meth:`PrinterAdapter.start_print` under exactly the conditions
    that stamp a job start: the adapter reported success and the file is not
    a resume 3MF continuing a print already running.  A start the pre-print
    gate blocked, or the printer refused, calls nothing.

    Registering the same callable twice registers it once.  A hook that
    raises is logged and skipped; it never fails, or undoes, the print.
    """
    global _PRINT_STARTED_HOOKS  # noqa: PLW0603
    with _PRINT_HOOKS_LOCK:
        if hook not in _PRINT_STARTED_HOOKS:
            _PRINT_STARTED_HOOKS = (*_PRINT_STARTED_HOOKS, hook)


def register_print_ended_hook(hook: Callable[[str], object]) -> None:
    """Call ``hook(printer_name)`` when a print is seen ending on that printer.

    Fired on the active-to-terminal edge of the previous-state table that
    both status doors write -- the adapter-generic ``get_state`` wrap and a
    push adapter's own callback -- so an ending is announced once, by
    whichever door saw it first, whether or not the job carried a name.
    *printer_name* is :func:`outcome_printer_name`.

    Not fired from :func:`_feed_outcome_lifecycle`'s own edge: a connected
    Bambu's MQTT callback writes the same table as each frame lands, so the
    polled wrap behind it usually finds no edge at all, and an ending
    announced there would almost never arrive for that printer.

    Same contract as :func:`register_print_started_hook`: idempotent, and a
    raising hook never reaches the read that saw the ending.
    """
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
    """Announce that the print on *printer_name* was seen ending.

    Called on the edge by :func:`kiln.auto_record_hook.observe_state`; not
    something a door calls for itself.  An empty name announces nothing, so
    no hook is ever left to guess which printer was meant.
    """
    if not printer_name:
        return
    for hook in _PRINT_ENDED_HOOKS:
        try:
            hook(printer_name)
        except Exception:  # noqa: BLE001 — never breaks the status read that saw it
            logger.warning("print-ended hook %r failed", hook, exc_info=True)


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


def in_calibration_window(state: Any, job: Any) -> bool:
    """Is this printer still in its pre-extrusion routine — levelling, homing?

    The discriminator is the JOB, not the machine state, because the state
    word does not distinguish them: an A1 reports ``printing`` throughout bed
    levelling, exactly as it does mid-part.  What separates them is that
    nothing has been laid down yet.

    Measured across four cancels on an A1 (2026-08-13).  The three that
    faulted all read ``current_layer=0`` with ``completion=0``; the one that
    cancelled cleanly read ``completion=1.0``.  So a job that has reported
    ANY progress is past the routine and out of the hazard.

    Unknown reads as IN the window.  A printer that has not said where it is
    yet is most likely still starting up, and what this gates is a sentence,
    so an unnecessary one costs nothing.

    Nothing ACTS on this.  Kiln knows the window is hazardous and does not
    know what to do about it: pausing first was tried and, across six cancels
    on an A1, changed nothing about whether the fault stuck.  What it gates is
    telling the user what to expect, which is the part the evidence supports.
    """
    layer = getattr(job, "current_layer", None) if job is not None else None
    completion = getattr(job, "completion", None) if job is not None else None
    if isinstance(layer, (int, float)) and layer >= 1:
        return False
    return not (isinstance(completion, (int, float)) and completion > 0)


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


def _ending_was_watched(
    *,
    observation_gap_seconds: float | None,
    state_age_seconds: float | None,
    stale_after_seconds: float | None = None,
) -> bool:
    """Did Kiln really SEE this ending, or merely find out afterwards?

    True only when both halves of "watched" hold: the last time we had
    current knowledge of this printer was recent, and the reading itself is
    the present tense rather than a stale cache.  The two doors that reach
    an ending each measure *observation_gap_seconds* the only way they
    honestly can (see :func:`_record_print_duration`); neither decides for
    itself what counts as watched — the thresholds live here, once.
    """
    from kiln.printers.progress_motion import WATCHED_ENDING_MAX_GAP_S

    # Was our last current knowledge recent enough for "we saw it end" to be
    # true?  Unknown — a first read, or a printer that has never spoken to
    # this process — counts as no: it never watched anything.
    if (
        observation_gap_seconds is None
        or observation_gap_seconds > WATCHED_ENDING_MAX_GAP_S
    ):
        return False

    # And is the reading itself the present tense?  A push-cache answer that
    # is minutes old dates the transition we just "saw", by exactly the same
    # amount and for the same reason.  Absent age means the caller learned
    # this from the printer on this call, which is current by construction.
    budget = (
        stale_after_seconds
        if isinstance(stale_after_seconds, (int, float))
        else STALE_STATE_WARN_AGE
    )
    return not (
        isinstance(state_age_seconds, (int, float)) and state_age_seconds > budget
    )


def _credible_hours(elapsed_seconds: Any) -> float | None:
    """*elapsed_seconds* as hours, or ``None`` when no number can be banked.

    Refuses a missing or non-positive reading — a printer with no clock to
    report (direct USB: M27 gives SD-card byte progress, not time) falls out
    here and stays honestly unknown, as
    :file:`scripts/adapter_conformance.yaml` already declares — and anything
    past :data:`_MAX_CREDIBLE_PRINT_HOURS`, the absurdity floor documented
    on the constant itself.
    """
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
) -> None:
    """Bank this print's duration — if this reading can be TRUSTED.

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

    *duration_semantics* — the adapter's own ``_DURATION_SEMANTICS``
    declaration — is which family this reading came from, and it decides
    what a late one is worth:

    * an ending :func:`_ending_was_watched` banks on every backend, exactly
      as before;
    * an ending noticed LATE banks only when the reading is ``"frozen"`` —
      the printer's own clock stopped with the print, so late is merely
      late — and is tagged ``reported`` so the daily total says how much of
      itself arrived that way (``prints_hours_reported``, the late subset
      of ``prints_hours_known``);
    * a late ``"stopwatch"`` reading still banks NOTHING, because it kept
      counting after the ending and would quietly inflate; ``"none"`` never
      has a number to offer in the first place.

    Anything refused stays an honest absence rather than a confident wrong
    number — ``prints - prints_hours_known`` is what makes that absence
    visible instead of reading as zero hours printed.

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

    All three are the same quantity, so the thresholds apply unchanged to
    either door, and neither decides for itself what counts as watched.

    Never raises — this runs inside a status read and inside an MQTT callback.
    """
    hours = _credible_hours(elapsed_seconds)
    if hours is None:
        return

    watched = _ending_was_watched(
        observation_gap_seconds=observation_gap_seconds,
        state_age_seconds=state_age_seconds,
        # The budget this printer's own cadence earns, so "was the reading
        # current" is asked here with the same number every other surface
        # asks it with.  Absent, the floor applies — which is what it did
        # before any budget was measured.
        stale_after_seconds=stale_after_seconds,
    )
    # A late reading is only worth banking when the printer's own clock
    # froze with the print.  Comparing against "frozen" — never against
    # "stopwatch" — is the fail-safe direction: an adapter that forgot to
    # declare inherits the strict default and its late readings are
    # refused, which can cost real hours but never invent them.
    if not watched and duration_semantics != "frozen":
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
    record_print_hours_for_job(job_label, hours, reported=not watched)


#: How long after Kiln's own load / unload / cancel a slot change still
#: belongs to that command rather than to a person at the screen.  A
#: firmware load-and-purge takes a couple of minutes.
OWN_COMMAND_SETTLE_SECONDS: float = 180.0

#: The polled slot observer asks a backend's :meth:`read_active_slot` at most
#: this often.  A status poll can be every few seconds; a slot reading is
#: usually its own request, and a filament change takes longer than this.
SLOT_OBSERVE_MIN_INTERVAL_S: float = 20.0


def _feed_slot_observer(adapter: PrinterAdapter, state: PrinterState) -> None:
    """Watch a polled backend's feeding slot and report the changes.

    The same three moves the MQTT push path makes natively, for every
    backend that can only be polled: read the slot (rate-limited), compare
    with the last reading, and on a change either count it against the
    print Kiln started (when Kiln is driving) or report it to the cutter
    bridge as a switch the machine made on its own -- flagged unverified
    when the backend's field has not been proven on hardware, so nothing
    is counted from it until this machine's own prints have agreed with
    it.  The first reading is a baseline, never a change.
    """
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
    """Feed one ``get_state()`` result into the print-outcome lifecycle.

    *read_at* is when the read was asked for (``time.monotonic()``), so the
    outcome table can tell this reading from a newer one filed while it was
    in flight -- see :func:`kiln.auto_record_hook.observe_state`.

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

    # ``confirmed_state``: this asks whether the JOB ended, and a fault is
    # not an ending.  The bare state word made "printing -> error" a terminal
    # transition the moment a machine raised a code mid-print, which recorded
    # the running print as FAILED and then, through the idempotency ledger,
    # blocked the real ending from ever being written.  A print that faults
    # and recovers was filed as a failure for good.
    #
    # It stays exactly as strict about staleness as the bare word was --
    # ``confirmed_state`` returns STALE for an expired reading, which is a
    # word no transition set contains, so a printer going quiet still records
    # nothing rather than guessing.
    status = confirmed_state_of(state)
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

    prev = observe_state(name, value, read_at=read_at)
    # Job identity may cost a network round trip — pay it only for an
    # edge that could actually record something.
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
                # The id this door just gave the hook, so the hours row and
                # the outcome row name one job.
                job_label=label,
                elapsed_seconds=getattr(job, "print_time_seconds", None),
                state_age_seconds=getattr(state, "state_age_seconds", None),
                stale_after_seconds=getattr(
                    state, "state_stale_after_seconds", None
                ),
                observation_gap_seconds=read_gap_seconds,
                duration_semantics=adapter._DURATION_SEMANTICS,
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


def _incomplete_upload_reason(adapter: PrinterAdapter, file_path: str) -> str | None:
    """Why this file must not leave for this printer — ``None`` when it may.

    The one door every non-Bambu upload passes.  A file that leaves Kiln
    for a printer carries the preview that printer's surface draws and a
    weight that is not a lie, or it does not leave: the same rule the Bambu
    adapter applies to its archives, applied here to raw G-code so that
    Mainsail, Fluidd, OctoPrint, PrusaLink and Duet Web Control all get a
    tile instead of a placeholder.  Lives in the shared wrapper rather than
    in each adapter for the same reason the bed-fit check above it does: a
    ninth backend inherits it without knowing it exists.

    Soft-passes everything it cannot establish — an unmapped backend, a
    file that is not G-code, an unreadable file, any internal error.  See
    :mod:`kiln.printers.gcode_complete` for what each surface reads.
    """
    try:
        from kiln.printers.gcode_complete import (
            declared_model_for_adapter,
            family_for_adapter,
            gcode_problems,
        )

        family = family_for_adapter(adapter)
        if family is None:
            return None
        # The declared model alone: a refusal keys off config, never off a
        # self-report and never the global resolver.
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
