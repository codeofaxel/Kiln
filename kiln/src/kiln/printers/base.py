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
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# Guards the one-time, per-instance setup of the idle-release bookkeeping.
# Module-level because the state it protects is what would otherwise have to
# hold its own lock — an adapter cannot lazily create a lock to guard its own
# lazy creation.  Contended only on an adapter's first connection.
_IDLE_SETUP_LOCK = threading.Lock()


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
    # What the printer was last seen doing, when :attr:`state` is ``STALE``.
    # The age takes the headline because it decides whether anything else in
    # the reading can be acted on -- but the run state underneath is not
    # discarded, because it is what keeps the concurrency gates conservative.
    # ``None`` on every state that is not ``STALE``.
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
        """
        if (
            self.state is PrinterStatus.STALE
            or self.state in UNREACHABLE_STATES
            or self.state_age_seconds is None
            or self.state_stale_after_seconds is None
            or self.state_age_seconds <= self.state_stale_after_seconds
        ):
            return
        self.last_known_state = self.state
        self.state = PrinterStatus.STALE
        if self.cause is None:
            self.cause = CAUSE_SILENT
        if self.remedy is None:
            self.remedy = describe_stale_remedy(
                self.state_age_seconds, self.state_stale_after_seconds
            )

    @property
    def print_error_code(self) -> str | None:
        """:attr:`print_error` as the printer's own screen renders it."""
        return format_error_code(self.print_error)

    @property
    def effective_state(self) -> PrinterStatus:
        """What the machine was doing, looking through a stale reading.

        ``STALE`` answers "can this reading be trusted", not "what is the
        printer doing" -- so anything asking the second question reads this
        and gets the run state, aged but not erased.
        """
        if self.state is PrinterStatus.STALE and self.last_known_state is not None:
            return self.last_known_state
        return self.state

    @property
    def is_occupied(self) -> bool:
        """Might this machine have work in flight?

        The question every gate that must not start a second print is really
        asking.  A stale reading answers from what the printer was last seen
        doing, and from "yes" when even that is unknown: the costs are not
        symmetric -- a refused print is a retry, a second print onto an
        occupied bed is a crash.
        """
        if self.state is PrinterStatus.STALE:
            if self.last_known_state is None:
                return True
            return self.last_known_state in BUSY_STATES
        return self.state in BUSY_STATES

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

    if any(needle in text for needle in _AUTH_NEEDLES):
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

    action: str  # "load" | "unload" | "purge"
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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.material_window is not None:
            data["material_window"] = list(self.material_window)
        return data


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
    details: dict[str, Any] = field(default_factory=dict)

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
            slot: Which spool to feed on a multi-material unit (Bambu AMS
                tray id, 0-based across units).  ``None`` means the external
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
        return self._load_filament_impl(plan)

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
        return self._unload_filament_impl(plan)

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
        return self._purge_filament_impl(plan)

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
        if not self.capabilities.can_handle_filament:
            raise FilamentHandlingUnsupported(
                f"{self.name} cannot {action} filament through Kiln — this "
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
                f"Cannot {action} filament: the printer did not answer a status "
                f"request ({exc})."
            ) from exc
        if state.state == PrinterStatus.PRINTING:
            raise PrinterError(
                f"Refusing to {action} filament while a print is running. "
                "Pause the print first, or wait for it to finish."
            )

        window = self._filament_material_window(material, slot)
        if temperature is None:
            if window is not None:
                lo, hi, _src = window
                temperature = round((lo + hi) / 2.0)
                temperature_source = f"midpoint of {window[2]}"
            else:
                raise PrinterError(
                    f"Cannot {action} filament: no temperature. Pass "
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

    # -- shared G-code sequence -------------------------------------------

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
    ) -> FilamentOpResult:
        """Heat, wait for the thermistor, then one relative E move.

        *pre_move_check* is an optional callable run after the hotend is at
        temperature and before the move; it returns ``(refusal_reason,
        source)`` to stop the sequence on a genuine printer signal (Klipper's
        ``extruder.can_extrude``) or ``None`` to proceed.

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
        try:
            self.set_tool_temp(target)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=f"Could not set the hotend to {target:g}°C: {exc}",
                extrusion_verified=False,
                verification_source="heater_command_rejected",
                error_hint=str(exc),
                slot=plan.slot,
                material=plan.material,
                temperature=target,
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
                    "Nothing was extruded."
                ),
                extrusion_verified=False,
                verification_source="thermistor",
                slot=plan.slot,
                material=plan.material,
                temperature=target,
                details={"last_hotend_reading": reading},
            )
        if pre_move_check is not None:
            refusal = pre_move_check()
            if refusal:
                reason, source = refusal
                return FilamentOpResult(
                    success=False,
                    action=plan.action,
                    message=f"Not extruding: {reason}",
                    extrusion_verified=False,
                    verification_source=source,
                    error_hint=reason,
                    slot=plan.slot,
                    material=plan.material,
                    temperature=target,
                    details={"hotend_reading": reading, "mechanism": mechanism},
                )
        commands = [
            "M83",
            f"G1 E{signed_length_mm:g} F{FILAMENT_FEED_RATE_MM_MIN}",
            "M82",
        ]
        try:
            self.send_gcode(commands)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False,
                action=plan.action,
                message=f"The printer rejected the {plan.action} move: {exc}",
                extrusion_verified=False,
                verification_source="firmware_rejected_move",
                error_hint=str(exc),
                slot=plan.slot,
                material=plan.material,
                temperature=target,
                details={"gcode": commands, "mechanism": mechanism},
            )
        verb = {"load": "fed", "unload": "retracted", "purge": "extruded"}.get(
            plan.action, "moved"
        )
        return FilamentOpResult(
            success=True,
            action=plan.action,
            message=(
                f"Hotend at {reading:g}°C; the printer accepted a "
                f"{abs(signed_length_mm):g} mm {plan.action} ({verb} at "
                f"{FILAMENT_FEED_RATE_MM_MIN / 60:g} mm/s). This backend "
                "reports no extruder-flow signal, so whether plastic actually "
                "left the nozzle is not something Kiln can confirm — look at "
                "the nozzle."
            ),
            extrusion_verified=None,
            verification_source="command_accepted_only",
            slot=plan.slot,
            material=plan.material,
            temperature=target,
            details={
                "gcode": commands,
                "mechanism": mechanism,
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
