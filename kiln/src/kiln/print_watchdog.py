"""In-process background watchdog for active prints.

Agent-driven polling (~60s between MCP calls) is not fast enough to catch
a clog, a thermal runaway, or a nozzle crash before damage occurs.  The
:class:`PrintWatchdog` runs inside the Kiln MCP server as a daemon thread,
polls the printer every few seconds, and triggers an immediate
:meth:`emergency_stop` when any of the configured red flags fire.

Design notes:

* Sync threading — the MCP server is sync-threaded, so we use
  ``threading.Thread(daemon=True)`` with a ``threading.Event`` for stop
  signalling.  No asyncio.
* Testable — all time reads go through an injectable clock
  (:attr:`time_fn`), and :meth:`step` performs one poll cycle
  synchronously so unit tests never spawn real threads.
* A trip latches on a stop the printer CONFIRMED.  Once ``emergency_stop()``
  says the job ended, the watchdog puts itself to sleep and :meth:`status`
  keeps reporting the trip.  A stop that raised, returned nothing, or came
  back unconfirmed is commanded again on each following poll, up to
  :data:`MAX_ESTOP_ATTEMPTS`, until a poll on which no red flag fires AND the
  printer's own state shows the job ended (idle, error, cancelling); that
  poll latches without another stop.  A red flag going quiet is not that
  observation: the heater cut alone drops the targets below
  :data:`MIN_ACTIVE_TARGET_C`, which silences the temperature rules while the
  job keeps moving, and a paused job can resume.  Latching on a stop nobody
  saw land would leave a running print with its watchdog asleep.

Red flags (any triggers e-stop):

* A non-zero ``print_error`` the printer is printing through: the same code
  on polls at least :data:`PRINT_ERROR_PERSIST_S` apart, from a reading that
  still says printing, with no stop of Kiln's own in flight
* ``state.hms_code`` (or ``print_error`` as hex) matches the user's HMS
  blocklist, in any state
* Tool temperature drops > 30°C below setpoint, after it first reaches it
* Bed temperature drops > 15°C below setpoint, after it first reaches it
* A heater stops climbing while still below its setpoint

A fault code alone is deliberately NOT a red flag.  Bambu firmware reports
``print_error`` once it has ALREADY acted: it pauses the job (filament
runout, an AMS problem, a clog or inspection pause) or ends it, and a cancel
walks the firmware through a real code of its own.  An emergency stop on
those would cancel a recoverable pause -- a ten-hour print paused for
runout, killed by the thing meant to protect it -- or land on a print that
is already over, which is what the stop on 2026-08-13 did: the printer had
already reported ``failed`` with 50348044 when the watchdog read the code.
So when the machine has acted (paused, failed, idle, cancelling, busy) the
watchdog stops nothing and announces nothing new; the adapter already
publishes the fault's leading edge.  The blocklist keeps its any-state,
first-poll behaviour because it is the user's explicit instruction, and it
is empty unless configured.

Yellow flags (logged and passed to ``on_anomaly``, no e-stop):

* WiFi signal weaker than -80 dBm
* Chamber fan stalled (speed reported as 0 while printing)
* The print has stopped moving -- judged by
  :mod:`kiln.printers.progress_motion`, the one stall detector every
  surface reads (measured threshold, held quiet while the printer's own
  countdown still moves)

A stall is deliberately NOT a red flag.  It used to be: 90 seconds with no
layer or percent change sent an emergency stop.  Nothing measured supported
that number; the counters it watched freeze on every long print (a Bambu
reports whole percents, so past 2.5 hours one percent alone takes longer
than that) and do NOT freeze on a clog, where the firmware keeps executing
moves.  Worse, a trip is idempotent, so a false stall trip switched off the
thermal and fault rules for the rest of the print.  The machine's own
faults and its temperatures stop it; a stall is told to a person.

A watchdog armed for a print Kiln started belongs to that print (see
``started_file``).  The ending edge Kiln sees on any status read retires it,
as before.  Two ways out cover the prints that leave no ending to see, and
both stop nothing:

* The printer takes up no job within :data:`DEFAULT_NEVER_ACTIVE_TIMEOUT_S`
  of arming, judged on a report it made: the print it accepted never began.
* The watchdog binds to the job the printer reports under the name of the
  file Kiln sent (or, for a printer that names no file, under its real job
  id).  Once Kiln has lost sight of that job -- no readable report for longer
  than ``progress_motion.WATCHED_ENDING_MAX_GAP_S``, or a report that no job
  is running -- a job that is POSITIVELY a different one retires it, on the
  same poll, before any red flag or retried stop.  ``job_identity.compare``
  must answer ``DIFFERENT`` against every id and name the bound job was seen
  under; an unnamed job, an id against a name, or a name that never matched
  keeps the watchdog watching.

Honest bounds.  A job reported under a name that does not match the file
Kiln sent is never bound, so only its ending retires the watchdog.  A reprint
of the same file begun while Kiln could not see the printer, with no start
time to tell it apart, reads as the same print.  A job that replaced the
bound one inside a single poll, with no stop in between that any read saw, is
absorbed as the same print.  A merged status (Bambu's) can pair a fresh run
state with a name left from before Kiln lost sight, until the full report it
requests on reconnecting lands; a read inside that window takes the old name
for the print.  And a brief pass through idle mid-print retires
the watchdog on the ending edge with nothing to re-attach it; the one claim
that a Bambu does that (``auto_record_hook._TERMINAL_STATE_DEBOUNCE_S``) is a
comment with no measurement behind it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiln.auto_record_hook import cancel_intent_pending, note_cancel_requested

if TYPE_CHECKING:
    from kiln.printers.job_identity import JobIdentity

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunable thresholds.  Named constants — no magic numbers inline.
# --------------------------------------------------------------------------

#: Tool-temp drop (°C below setpoint) that triggers e-stop.  30°C is well
#: outside normal PID wobble but well inside "nozzle clogged, heat creep,
#: or thermistor disconnected" territory.
DEFAULT_TOOL_DROP_C: float = 30.0

#: Bed-temp drop (°C below setpoint) that triggers e-stop.  Beds have
#: more thermal mass so the threshold is tighter.
DEFAULT_BED_DROP_C: float = 15.0


#: How often the watchdog polls the printer, in seconds.
DEFAULT_POLL_INTERVAL: float = 2.5

#: WiFi threshold for yellow-flag logging (dBm, more negative == weaker).
WIFI_WARN_DBM: int = -80

#: Minimum target temperature considered "actually heating" — below this
#: the heater is off or cooling, and drops below setpoint are expected.
MIN_ACTIVE_TARGET_C: float = 30.0

#: How close to setpoint a heater must come to count as having reached it.
#: Comfortably wider than steady-state PID wobble.
REACHED_MARGIN_C: float = 5.0

#: Temperature rise that counts as a heater still climbing.  Wider than
#: sensor noise and than the resolution printers report in.
HEATING_RISE_C: float = 1.0

#: How long a heater may show no such rise, while further below setpoint
#: than the drop threshold, before the gap counts as a fault.  A heater
#: climbing slowly is fine at any speed; one that has stopped is not.
DEFAULT_NO_RISE_TIMEOUT_S: float = 120.0

#: How long a heater may warm toward a target it has never reached before
#: the gap is judged on its own merits.
#:
#: "Still climbing" alone leaves one state with no detector: a heater that
#: keeps rising, arbitrarily slowly, toward a target it never arrives at.
#: A rise of 1°C per 119s satisfies the rule above forever, and the stall
#: detector sees a machine that is not yet in a state expected to move, so
#: nothing reports it.
#:
#: This ends the AMBIGUITY rather than delivering a verdict.  "Below
#: setpoint early in a print" is genuinely ambiguous; thirty minutes in it
#: is not, so the already-calibrated drop threshold takes over.  A heater
#: within that threshold of its target is still never flagged — that is
#: the asymptotic final approach, where patience is correct.  Getting this
#: number wrong by a factor of two therefore shifts WHEN ambiguity ends,
#: never WHAT counts as broken.  It is a policy choice about how long to
#: tolerate not knowing; it is not a physical constant.
DEFAULT_WARMUP_TIMEOUT_S: float = 1800.0

#: Fraction of that ceiling at which a warning is raised.  Logged and sent
#: to ``on_anomaly``; it stops nothing.
WARMUP_WARN_FRACTION: float = 0.5

#: How long the SAME non-zero ``print_error`` must stand, across polls of a
#: reading that still says printing, before it counts as a fault the printer
#: is printing through.  Measured on an A1 (2026-08-14): a cancel walks the
#: firmware through ``failed`` carrying print_error 50348044 for about four
#: seconds.  Status pushes are merges, so a code can reach the cache a frame
#: before the state word that explains it; a code younger than that transient
#: is still news in transit, not a machine ignoring its own fault.
PRINT_ERROR_PERSIST_S: float = 5.0

#: How many emergency stops one trip may command while the printer does not
#: confirm them.  Bounded so a printer that never confirms -- unreachable, or
#: ignoring the command -- is not commanded forever; past it the watchdog
#: latches and says why, and the person at the machine is the remedy every
#: unconfirmed stop has already named.
MAX_ESTOP_ATTEMPTS: int = 3

#: The run states that show a stopped job: it ended (idle, error) or is
#: ending (cancelling).  After a stop the printer did not confirm, only one of
#: these lets a poll with no red flag count as the stop having landed.
#: ``paused`` is not one -- a paused job can resume -- and neither is a reading
#: that shows nothing (stale, offline, unknown).
_JOB_ENDED_STATES: frozenset[str] = frozenset({"idle", "error", "cancelling"})

#: How long a watchdog armed for a print waits for the printer to take up ANY
#: job before it concludes the print it was armed for never began.
#:
#: A start the printer accepted and never ran leaves no ending to see, so
#: without a bound the watchdog would stay to police whatever that machine
#: runs next -- a print started at its own screen included.  The Bambu
#: adapter's comment on a start it could not confirm
#: (``BambuAdapter._start_print_impl``, the ``timeout`` branch) gives "5-8+
#: minutes" of homing, AMS load and calibration before ``gcode_state`` even
#: flips to ``prepare``.  That comment cites no measurement and is open at the
#: top, so it is not a number to cut close to.
#: The warm-up ceiling is this watchdog's existing policy for how long "still
#: starting" may last, and it is reused rather than a second guess made: it is
#: several times the comment's range.  Too short, and a print that took longer
#: to begin runs without its watchdog; too long, and one that never began keeps
#: a watchdog attached for longer.  Either way it moves only WHEN such a
#: watchdog leaves.
DEFAULT_NEVER_ACTIVE_TIMEOUT_S: float = DEFAULT_WARMUP_TIMEOUT_S

#: The run states that show the printer took up a job.  The ending edge can
#: follow any of them, so seeing one ends the wait for the print to begin.
_TOOK_UP_A_JOB: frozenset[str] = frozenset({"printing", "paused", "busy", "cancelling"})

#: The run states in which a job is on the machine and can say which job it
#: is.  Binding, matching and "a different print" are judged only on these.
_JOB_ON_THE_MACHINE: frozenset[str] = frozenset({"printing", "paused"})

#: Every run state a reading can vouch for.  Anything else -- stale, offline,
#: unknown, a refused login -- is no contact with the printer at all.
_READABLE_RUN_STATES: frozenset[str] = _TOOK_UP_A_JOB | _JOB_ENDED_STATES


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass
class Flag:
    """A single red or yellow flag event observed by the watchdog."""

    kind: str  #: "red" or "yellow"
    rule: str  #: short identifier, e.g. "tool_drop", "stalled_layer"
    message: str
    timestamp: float
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule": self.rule,
            "message": self.message,
            "timestamp": self.timestamp,
            "context": dict(self.context),
        }


@dataclass
class _Verdict:
    """What one heater has to say this poll."""

    red: Flag | None = None
    warning: Flag | None = None
    #: True while the heater is climbing toward a target it has not reached
    #: AND the ceiling has not expired.  The layer-stall timer is held for
    #: exactly that long: heating blocks the G-code stream, so no layer can
    #: finish, but once the ceiling ends the grace the stall rule resumes.
    warming: bool = False


class _HeaterWatch:
    """One heater's warmup state, and every verdict that depends on it.

    The hotend and the bed run the SAME rules; only the labels, the context
    keys and the drop threshold differ.  Two copies is how a fix to one
    silently misses the other, and it is why adding the ceiling to a
    copy-pasted pair would have tripled forty lines instead of adding ten.

    Honest bound, because it is easy to read more into this than it does:
    every rule here reads the temperature the PRINTER REPORTS.  If that
    number is wrong the watchdog is wrong with it, so this is not thermal
    runaway protection and cannot be — a thermistor that under-reports
    fools the firmware's protection in exactly the same way.  What the
    ceiling adds is that a reported temperature which never arrives is
    eventually treated as a fault instead of tolerated forever.
    """

    def __init__(
        self,
        *,
        prefix: str,
        label: str,
        drop_c: float,
        no_rise_timeout_s: float,
        warmup_timeout_s: float,
        drop_tail: str = "",
    ) -> None:
        self._prefix = prefix  # "tool" / "bed" — rule names and context keys
        self._label = label  # "Hotend" / "Bed" — user-facing prose
        self._drop_c = drop_c
        self._no_rise_timeout_s = no_rise_timeout_s
        self._warmup_timeout_s = warmup_timeout_s
        self._drop_tail = drop_tail
        self.reset()

    def reset(self) -> None:
        """Forget everything: a new print judges its heaters afresh."""
        self._reached = False
        self._target_prev: float | None = None
        self._rise_ref: tuple[float, float] | None = None
        self._warming_since: float | None = None
        self._warned = False
        self._grace_expired = False

    def _flag(
        self, rule: str, message: str, now: float, *, kind: str = "red", **context: float
    ) -> Flag:
        # kind is passed, never inferred from the rule name: inferring it read
        # the UNPREFIXED name and quietly minted the warning as a red flag,
        # which would have filed an incident for every slow warmup.
        return Flag(
            kind=kind,
            rule=f"{self._prefix}_{rule}",
            message=message,
            timestamp=now,
            context={
                f"{self._prefix}_temp_actual": context["actual"],
                f"{self._prefix}_temp_target": context["target"],
                **{k: v for k, v in context.items() if k not in ("actual", "target")},
            },
        )

    def evaluate(self, actual: Any, target: Any, now: float) -> _Verdict:
        if target is None:
            return _Verdict()

        if target < MIN_ACTIVE_TARGET_C:
            # Heater switched off, as filament-change macros do with M104 S0.
            # The rise reference and the warmup clock both STAY: a target
            # toggling through zero must not restart either, or a dead heater
            # is never reported and the ceiling never arrives.
            self._reached = False
            self._target_prev = None
            return _Verdict()

        if actual is None:
            # A missing reading neither arms nor disarms anything.
            return _Verdict()

        if self._target_prev != target:
            # New setpoint: the heater has to climb to it again.  The clock
            # keeps running for the same anti-evasion reason as above.
            self._target_prev = target
            self._reached = False

        if actual >= target - REACHED_MARGIN_C:
            self._reached = True
            self._rise_ref = None
            self._warming_since = None
            self._warned = False
            self._grace_expired = False

        drop = target - actual

        # The drop rule is armed once the heater has ARRIVED — or once the
        # ceiling has decided the question is no longer ambiguous.
        if drop >= self._drop_c and (self._reached or self._grace_expired):
            return _Verdict(
                red=self._flag(
                    "drop",
                    f"{self._label} dropped {drop:.1f}°C below setpoint "
                    f"({actual:.1f}°C vs {target:.0f}°C target){self._drop_tail}",
                    now,
                    actual=float(actual),
                    target=float(target),
                    drop_c=float(drop),
                )
            )

        if self._reached or self._grace_expired:
            return _Verdict()

        # --- still warming -------------------------------------------
        if self._warming_since is None:
            self._warming_since = now
        warmed_for = now - self._warming_since

        if self._rise_ref is None:
            self._rise_ref = (actual, now)
        ref_c, ref_at = self._rise_ref
        if actual >= ref_c + HEATING_RISE_C:
            self._rise_ref = (actual, now)
        elif now - ref_at >= self._no_rise_timeout_s:
            return _Verdict(
                red=self._flag(
                    "not_heating",
                    f"{self._label} stopped climbing {drop:.1f}°C below setpoint "
                    f"({actual:.1f}°C vs {target:.0f}°C target) "
                    f"— heater failure or thermistor fault",
                    now,
                    actual=float(actual),
                    target=float(target),
                    no_rise_seconds=float(now - ref_at),
                )
            )

        if warmed_for >= self._warmup_timeout_s:
            self._grace_expired = True
            if drop >= self._drop_c:
                # Its own rule and its own words: a heater that never started
                # is a different fault from one that died mid-print, and the
                # user's next move differs.
                return _Verdict(
                    red=self._flag(
                        "warmup_timeout",
                        f"{self._label} never reached setpoint: {actual:.1f}°C vs "
                        f"{target:.0f}°C target after {warmed_for / 60:.1f} min "
                        f"— heater, thermistor, or a fan cooling it faster "
                        f"than it heats",
                        now,
                        actual=float(actual),
                        target=float(target),
                        warming_seconds=float(warmed_for),
                    )
                )
            # Close enough that the ordinary drop rule can take it from here.
            return _Verdict()

        warning = None
        if not self._warned and warmed_for >= self._warmup_timeout_s * WARMUP_WARN_FRACTION:
            self._warned = True
            warning = self._flag(
                "warmup_slow",
                f"{self._label} has been warming for {warmed_for / 60:.0f} min "
                f"and is still {drop:.1f}°C below its {target:.0f}°C target",
                now,
                kind="yellow",
                actual=float(actual),
                target=float(target),
                warming_seconds=float(warmed_for),
            )
        return _Verdict(warning=warning, warming=True)


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------


class PrintWatchdog:
    """Background thread that polls a printer and triggers e-stop on anomalies.

    Args:
        adapter: Any object with ``get_state()``, ``get_job()``, and
            ``emergency_stop()`` methods (see :class:`PrinterAdapter`).
            ``emergency_stop()`` answers whether the printer confirmed the
            stop -- a result whose ``success`` is True -- and any other
            answer is treated as a stop that did not land.
        poll_interval_sec: Seconds between polls when running as a thread.
        on_anomaly: Optional callback invoked with the triggering
            :class:`Flag` when a red flag fires.  Exceptions in the
            callback are logged and swallowed.
        hms_blocklist: HMS codes that trigger e-stop when the printer
            reports them.  Compared case-insensitively against
            ``state.hms_code`` and ``state.print_error`` (formatted hex).
            Matched in any state, on the first poll: the list is the user's
            explicit instruction, so it does not wait to see whether the
            machine acts on the fault itself.
        tool_drop_c: Override for tool-temp drop threshold.
        bed_drop_c: Override for bed-temp drop threshold.
        no_rise_timeout_s: Override for how long a warming heater may
            show no temperature rise before the gap counts as a failure.
        warmup_timeout_s: Override for how long a heater may warm toward a
            target it has never reached before the gap is judged.  Raise it
            for a large enclosed machine in a cold room, which legitimately
            takes longer than a desktop printer.
        started_file: The file Kiln started, which arms this watchdog for that
            print: it binds to the job the printer reports under that file's
            name, and retires itself -- stopping nothing -- when the printer
            takes up no job within ``never_active_timeout_s``, or is running a
            job positively different from the bound one after Kiln lost sight
            of it.  ``None`` arms nothing: the watchdog watches the printer
            until it is stopped, as it always has.
        on_retired: Called with this watchdog once it has retired itself, so
            its owner can drop it.  Exceptions are logged and swallowed.
        never_active_timeout_s: Override for how long a watchdog armed for a
            print waits for the printer to take up any job.
        time_fn: Injectable clock for deterministic testing.  Defaults
            to :func:`time.monotonic`.
    """

    def __init__(
        self,
        adapter: Any,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL,
        on_anomaly: Callable[[Flag], None] | None = None,
        hms_blocklist: list[str] | None = None,
        *,
        tool_drop_c: float = DEFAULT_TOOL_DROP_C,
        bed_drop_c: float = DEFAULT_BED_DROP_C,
        no_rise_timeout_s: float = DEFAULT_NO_RISE_TIMEOUT_S,
        warmup_timeout_s: float = DEFAULT_WARMUP_TIMEOUT_S,
        started_file: str | None = None,
        on_retired: Callable[[PrintWatchdog], None] | None = None,
        never_active_timeout_s: float = DEFAULT_NEVER_ACTIVE_TIMEOUT_S,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._poll_interval = max(0.1, float(poll_interval_sec))
        self._on_anomaly = on_anomaly
        self._hms_blocklist = {c.strip().upper() for c in (hms_blocklist or []) if c}
        self._tool_drop_c = float(tool_drop_c)
        self._bed_drop_c = float(bed_drop_c)
        self._no_rise_timeout_s = float(no_rise_timeout_s)
        self._time = time_fn

        # Threading primitives.
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Observed state.
        self.anomaly_triggered: bool = False
        self._last_state: Any = None
        self._last_job: Any = None
        self._flags: list[Flag] = []
        # Yellow rules already reported for the current print, so a condition
        # that holds for hours is reported once rather than every poll.
        self._yellow_seen: set[str] = set()
        # The print_error rule's evidence: the code a printing reading carried
        # and when it was first seen.  Dropped whenever the conditions lapse.
        self._error_streak: tuple[int, float] | None = None
        # Emergency stops commanded for the current trip that the printer has
        # not confirmed.  0 while no trip is outstanding.
        self._estop_attempts: int = 0
        # The rule the outstanding stop was commanded for, so a follow-up that
        # finds no flag firing can still say what it is retrying.
        self._tripped_rule: str | None = None

        # The print this watchdog was armed for.  Consulted only when
        # ``started_file`` armed it.
        from kiln.printers.progress_motion import normalize_job_label

        self._armed_for_start = started_file is not None
        self._started_label = normalize_job_label(started_file)
        self._on_retired = on_retired
        self._never_active_timeout_s = float(never_active_timeout_s)
        self._retired = False
        # The clock at the first poll; the wait for the print to begin runs
        # from there.
        self._armed_at: float | None = None
        self._took_up_a_job = False
        # Every real id and every name the bound job has been seen under while
        # Kiln kept watching it run.  Both empty until the printer names it.
        self._job_ids: set[str] = set()
        self._job_names: dict[str, JobIdentity] = {}
        # When a reading last vouched for a run state; and, once Kiln has lost
        # sight of the bound job, how -- until that job is seen again.
        self._last_contact_at: float | None = None
        self._lost_sight: str | None = None

        # One object per heater, same rules in both — a drop only counts once
        # its heater has arrived, and a heater that never arrives is judged
        # when the ceiling says the question has stopped being ambiguous.
        self._tool = _HeaterWatch(
            prefix="tool",
            label="Hotend",
            drop_c=self._tool_drop_c,
            no_rise_timeout_s=self._no_rise_timeout_s,
            warmup_timeout_s=float(warmup_timeout_s),
            drop_tail=" — likely clog or heater failure",
        )
        self._bed = _HeaterWatch(
            prefix="bed",
            label="Bed",
            drop_c=self._bed_drop_c,
            no_rise_timeout_s=self._no_rise_timeout_s,
            warmup_timeout_s=float(warmup_timeout_s),
        )

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.debug("PrintWatchdog already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="kiln-print-watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "PrintWatchdog started (poll=%.1fs, tool_drop=%.0f°C, "
            "bed_drop=%.0f°C, hms_blocklist=%d)",
            self._poll_interval,
            self._tool_drop_c,
            self._bed_drop_c,
            len(self._hms_blocklist),
        )

    def stop(self, timeout: float | None = None) -> None:
        """Signal the thread to exit and join it.

        Safe to call even if :meth:`start` was never invoked, and safe from
        the watchdog's own thread, where it only signals: a thread cannot
        join itself, and the loop exits once the step it is in returns.  That
        call is ordinary rather than exotic -- the watchdog polls its printer,
        so its own poll is usually the read that sees its print end.
        """
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout if timeout is not None else self._poll_interval * 2.0)
        self._thread = None
        logger.info("PrintWatchdog stopped")

    def status(self) -> dict[str, Any]:
        """Return the latest poll snapshot and a history of flags seen."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "anomaly_triggered": self.anomaly_triggered,
                "flags": [f.to_dict() for f in self._flags],
                "red_flags": [f.to_dict() for f in self._flags if f.kind == "red"],
                "yellow_flags": [f.to_dict() for f in self._flags if f.kind == "yellow"],
            }

    # ------------------------------------------------------------------
    # Single-step entry point — the core of the watchdog.
    # ------------------------------------------------------------------

    def step(self) -> Flag | None:
        """Perform one poll cycle.  Returns the red flag that fired, if any.

        Factored out of :meth:`_run_loop` so tests can drive the watchdog
        deterministically without spawning threads.
        """
        # Once latched, do nothing — don't spam e-stop.  Once retired, the
        # print this watchdog was armed for is not on the machine, and nothing
        # here is its to act on.
        if self.anomaly_triggered or self._retired:
            return None
        if self._armed_for_start and self._armed_at is None:
            self._armed_at = self._time()

        try:
            from kiln.printers.engagement import internal_read

            # Kiln's own polling of a machine it is already responsible for, not a
            # person commanding a printer.  Exempt so a background loop can never
            # spin on a refusal — a health check that errors every tick is worse
            # than one that does not run.
            with internal_read():
                state = self._adapter.get_state()
        except Exception:
            logger.exception("PrintWatchdog: get_state() failed; skipping tick")
            return None

        try:
            job = self._adapter.get_job()
        except Exception:
            # Job info is optional — a stall check just won't fire without it.
            job = None

        if self._stop_event.is_set():
            # Stopped while this poll was reading -- most often retired by the
            # very read that saw its print end, which announces the ending
            # inside get_state().  A stopped watchdog acts on nothing it read.
            return None

        with self._lock:
            self._last_state = state
            self._last_job = job

        # --- Is this still the print it was armed for? -----------------
        # Before the red flags and before any retried stop: a watchdog that
        # can see its print has gone must not stop the machine on a reading
        # of another job.
        if self._armed_for_start and self._follow_job(state, job):
            return None

        # --- Red flags ------------------------------------------------
        red = self._evaluate_red_flags(state, job)
        if self._estop_attempts:
            # A stop this watchdog sent is still unconfirmed, so this poll is
            # its follow-up: send it again, or see that it landed.
            self._follow_up(red, state)
            return red
        if red is not None:
            self._trip(red)
            return red

        # --- Yellow flags ---------------------------------------------
        # Once per rule per print.  A weak-WiFi condition holds for as long as
        # it holds, so notifying every poll would mean thousands of callbacks
        # (and thousands of recorded flags) for one fact the caller already
        # knows.  The condition is reported when it appears; it is not
        # re-reported until the next print.
        for yellow in self._evaluate_yellow_flags(state, job):
            if yellow.rule in self._yellow_seen:
                continue
            self._yellow_seen.add(yellow.rule)
            logger.warning("PrintWatchdog yellow: %s", yellow.message)
            self._notify(yellow)

        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Daemon-thread entry point."""
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("PrintWatchdog: unexpected error in step()")
            # If tripped, idle out until stop() is called — don't spam.
            interval = self._poll_interval if not self.anomaly_triggered else max(
                self._poll_interval, 5.0
            )
            self._stop_event.wait(timeout=interval)

    def _follow_job(self, state: Any, job: Any) -> bool:
        """Follow the print this watchdog was armed for.  ``True`` once it has retired.

        Only proof retires it, and there are two kinds:

        * the printer has taken up no job within the never-active bound, as
          told by a report it made -- a printer Kiln cannot read has told it
          nothing, so time without a readable report does not count;
        * the printer is running a job positively different from the bound one
          after Kiln lost sight of that job -- a gap between readable reports
          longer than ``WATCHED_ENDING_MAX_GAP_S``, or a report that no job is
          running.

        Anything short of that keeps it watching.  While Kiln keeps the bound
        job in sight, a name or id that changes is still that job: a Bambu
        status is a merge of partial frames, so one print can arrive under more
        than one name, and every one it is seen under is remembered.
        """
        from kiln.printers.job_identity import DIFFERENT, SAME, clean_native_id, resolve_label
        from kiln.printers.progress_motion import WATCHED_ENDING_MAX_GAP_S

        word = _confirmed_state_word(state)
        if word not in _READABLE_RUN_STATES:
            return False  # no contact: nothing seen, so nothing concluded
        now = self._time()
        if self._last_contact_at is not None:
            gap = now - self._last_contact_at
            if gap > WATCHED_ENDING_MAX_GAP_S:
                self._lost_sight = f"{gap:.0f}s without a readable report"
        self._last_contact_at = now

        if word in _TOOK_UP_A_JOB:
            self._took_up_a_job = True
        elif (
            not self._took_up_a_job
            and self._armed_at is not None
            and now - self._armed_at >= self._never_active_timeout_s
        ):
            self._retire(
                f"the printer accepted {self._started_name()} "
                f"{(now - self._armed_at) / 60:.0f} minutes ago and has taken up no "
                f"job since; it reads {word}"
            )
            return True

        if word in _JOB_ENDED_STATES:
            self._lost_sight = f"the printer reported {word}"
            return False
        if word not in _JOB_ON_THE_MACHINE:
            return False

        job_id = clean_native_id(getattr(job, "job_id", None))
        named = resolve_label(job)
        if not self._job_ids and not self._job_names:
            if self._is_the_started_file(job_id, named):
                self._remember(job_id, named)
                self._lost_sight = None
            return False

        relation = self._relation(job_id, named)
        if relation == SAME or self._lost_sight is None:
            self._remember(job_id, named)
            self._lost_sight = None
            return False
        if relation == DIFFERENT:
            self._retire(
                f"it was watching {self._bound_names()}; after {self._lost_sight}, the "
                f"printer is running {self._job_name(job_id, named)}, a different print"
            )
            return True
        return False

    def _is_the_started_file(self, job_id: str | None, named: JobIdentity | None) -> bool:
        """Does this job answer to the file Kiln started, as far as its report can say?

        A name must match the file Kiln sent.  One that does not is not proof of
        another print -- a printer may report a plate name -- but it is no reason
        to adopt that job either, so the watchdog stays unbound and only the
        ending edge retires it.  A job with no name at all (Prusa Link's status
        names no file) is taken at its real job id.
        """
        if named is not None:
            return named.label == self._started_label
        return job_id is not None

    def _relation(self, job_id: str | None, named: JobIdentity | None) -> str:
        """``compare`` against everything the bound job has been seen as.

        The strongest axis both sides carry decides: ids when both have one (a
        reprint started from a vendor's cloud gets a new id), names otherwise.
        ``SAME`` when anything remembered matches; ``DIFFERENT`` only when
        everything remembered on that axis is different.
        """
        from kiln.printers.job_identity import DIFFERENT, SAME, UNKNOWN, JobIdentity, compare

        if job_id is not None and self._job_ids:
            current = JobIdentity(native=job_id)
            answers = {compare(current, JobIdentity(native=known)) for known in self._job_ids}
        elif named is not None and self._job_names:
            answers = {compare(named, known) for known in self._job_names.values()}
        else:
            return UNKNOWN
        if SAME in answers:
            return SAME
        return DIFFERENT if answers == {DIFFERENT} else UNKNOWN

    def _remember(self, job_id: str | None, named: JobIdentity | None) -> None:
        """Add what this report calls the bound job to what the job is known by."""
        if job_id is not None:
            self._job_ids.add(job_id)
        if named is not None:
            known = self._job_names.get(named.label)
            # The latest start estimate for a name, so a drift Kiln watches
            # happen -- firmware that stops counting while paused -- is followed
            # rather than accumulated.  Never a known start traded for none.
            if known is None or named.started_at is not None:
                self._job_names[named.label] = named

    def _retire(self, reason: str) -> None:
        """Leave, stopping nothing: the print this was armed for is not on the machine."""
        self._retired = True
        logger.warning("PrintWatchdog retired, stopping nothing: %s", reason)
        self.stop(timeout=0.0)
        if self._on_retired is not None:
            try:
                self._on_retired(self)
            except Exception:
                logger.exception("PrintWatchdog: on_retired callback raised")

    def _started_name(self) -> str:
        return repr(self._started_label) if self._started_label else "the print Kiln started"

    def _bound_names(self) -> str:
        names = [repr(name) for name in sorted(self._job_names)]
        names += [f"job {job_id}" for job_id in sorted(self._job_ids)]
        return " / ".join(names)

    @staticmethod
    def _job_name(job_id: str | None, named: JobIdentity | None) -> str:
        parts = [repr(named.label)] if named is not None else []
        if job_id:
            parts.append(f"job {job_id}")
        return " / ".join(parts) or "an unnamed job"

    def _evaluate_red_flags(self, state: Any, job: Any) -> Flag | None:
        """Return the first red flag that fires this tick, or ``None``."""
        now = self._time()

        # --- print_error: a fault the printer is printing through ----
        # Judged on the CONFIRMED run state: an uncleared code takes the
        # headline as ``error`` while the state underneath still says
        # ``printing``, and a stale reading is evidence of nothing.  Any other
        # state means the firmware has acted -- paused the job, ended it, or
        # is cancelling -- and the fault is for the person it stopped for.
        print_error = _getattr(state, "print_error")
        code = _as_code(print_error)
        if code and _confirmed_state_word(state) == "printing":
            streak = self._error_streak
            if streak is None or streak[0] != code:
                self._error_streak = (code, now)
            elif now - streak[1] >= PRINT_ERROR_PERSIST_S and not self._kiln_stop_in_flight():
                stood = now - streak[1]
                return Flag(
                    kind="red",
                    rule="print_error",
                    message=(
                        f"Printer reported print_error={code} (hex: {code:08X}) "
                        f"and kept printing through it for {stood:.0f}s"
                    ),
                    timestamp=now,
                    context={"print_error": code, "persisted_seconds": float(stood)},
                )
        else:
            # Gone, or answered by the firmware.  The adapter announced the
            # fault's leading edge already; there is nothing to add here.
            self._error_streak = None

        # --- HMS blocklist match -------------------------------------
        hms_code = _getattr(state, "hms_code")
        if hms_code and self._hms_blocklist:
            as_str = str(hms_code).strip().upper()
            if as_str in self._hms_blocklist:
                return Flag(
                    kind="red",
                    rule="hms_blocklist",
                    message=f"Printer reported HMS code {as_str} (on blocklist)",
                    timestamp=now,
                    context={"hms_code": as_str},
                )
        # Also match numeric print_error formatted as hex (Bambu style).
        if print_error is not None and self._hms_blocklist:
            as_hex = f"{int(print_error):08X}"
            if as_hex in self._hms_blocklist:
                return Flag(
                    kind="red",
                    rule="hms_blocklist",
                    message=f"Printer print_error hex {as_hex} is on HMS blocklist",
                    timestamp=now,
                    context={"hms_code": as_hex},
                )

        # Only run the remaining checks if the printer is actively printing.
        if not _is_printing(state):
            # A new print gets to hear about a condition again — "the WiFi was
            # weak on your last print" is not a useful thing to withhold.
            self._yellow_seen.clear()
            # Clear warmup tracking so the next print checks afresh.
            self._tool.reset()
            self._bed.reset()
            return None

        # --- Heater temperature and warmup ---------------------------
        # Both heaters, same rules, one implementation.  A red flag returns
        # immediately; a warning is recorded and passed on without stopping
        # anything, which it can only do because reporting and stopping are
        # separate acts.
        for watch, actual_key, target_key in (
            (self._tool, "tool_temp_actual", "tool_temp_target"),
            (self._bed, "bed_temp_actual", "bed_temp_target"),
        ):
            verdict = watch.evaluate(
                _getattr(state, actual_key), _getattr(state, target_key), now
            )
            if verdict.red is not None:
                return verdict.red
            if verdict.warning is not None and verdict.warning.rule not in self._yellow_seen:
                self._yellow_seen.add(verdict.warning.rule)
                logger.warning("PrintWatchdog yellow: %s", verdict.warning.message)
                self._notify(verdict.warning)

        return None

    def _kiln_stop_in_flight(self) -> bool:
        """Has Kiln asked this printer to stop, with the ending not yet seen?

        A stop Kiln asked for walks Bambu firmware through a real code, and
        that code is the stop's own noise.  False while this watchdog's own
        stop is outstanding, though: the intent it filed would otherwise read
        as somebody else's stop and silence the very fault it is retrying.
        """
        if self._estop_attempts:
            return False
        try:
            from kiln.printers.base import outcome_printer_name

            return cancel_intent_pending(outcome_printer_name(self._adapter))
        except Exception:  # noqa: BLE001 — an unreadable intent is no stop in flight
            logger.debug("PrintWatchdog: cancel-intent read failed", exc_info=True)
            return False

    def _evaluate_yellow_flags(self, state: Any, job: Any = None) -> list[Flag]:
        """Return all yellow flags firing this tick."""
        now = self._time()
        flags: list[Flag] = []

        # --- Stopped moving ------------------------------------------
        # The shared detector's verdict; this watchdog keeps no progress
        # ledger of its own.  Reported once per stall episode: the
        # ``_yellow_seen`` mark is dropped the moment a primary axis moves,
        # so a second stall on the same print is reported again.
        from kiln.printers.progress_motion import Motion, observe_progress

        verdict = observe_progress(self._adapter, state, job, now=now)
        if verdict.stalled:
            flags.append(
                Flag(
                    kind="yellow",
                    rule="stalled",
                    message=verdict.note() or "The print has stopped moving.",
                    timestamp=now,
                    context={
                        "frozen_for_seconds": float(verdict.frozen_for_seconds or 0.0),
                        "layer": verdict.layer,
                        "percent": verdict.percent,
                    },
                )
            )
        elif verdict.motion is Motion.MOVING:
            self._yellow_seen.discard("stalled")

        # --- WiFi signal ----------------------------------------------
        wifi = _getattr(state, "wifi_signal")
        if wifi is not None:
            dbm = _parse_dbm(wifi)
            if dbm is not None and dbm < WIFI_WARN_DBM:
                flags.append(
                    Flag(
                        kind="yellow",
                        rule="wifi_weak",
                        message=f"WiFi signal weak: {wifi} (< {WIFI_WARN_DBM} dBm)",
                        timestamp=now,
                        context={"wifi_signal": str(wifi), "dbm": dbm},
                    )
                )

        # --- Chamber fan stalled -------------------------------------
        # Only flag if the printer is printing — a 0-speed chamber fan on
        # an idle printer is normal.
        if _is_printing(state):
            chamber_fan = _getattr(state, "chamber_fan_speed")
            if chamber_fan is not None and int(chamber_fan) == 0:
                flags.append(
                    Flag(
                        kind="yellow",
                        rule="chamber_fan_stalled",
                        message="Chamber fan reported 0 while printing",
                        timestamp=now,
                        context={"chamber_fan_speed": 0},
                    )
                )

        return flags

    def _notify(self, flag: Flag) -> None:
        """Record a flag and hand it to the caller.  Stops nothing.

        Reporting and stopping are separate acts.  A yellow flag needs the
        first without the second: before this split the only route to
        ``on_anomaly`` was :meth:`_trip`, which also fires the e-stop, so
        every yellow flag went to an in-memory list that nothing reads.  A
        warning nobody receives is not a warning.
        """
        self._record_flag(flag)
        self._dispatch(flag)

    def _dispatch(self, flag: Flag) -> None:
        """Hand a flag to ``on_anomaly``; a raising callback never propagates."""
        if self._on_anomaly is not None:
            try:
                self._on_anomaly(flag)
            except Exception:
                logger.exception("PrintWatchdog: on_anomaly callback raised")

    def _trip(self, flag: Flag) -> None:
        """A red flag's first stop: log, record, e-stop, callback.

        Latches only when the printer confirmed the stop.  An unconfirmed
        stop leaves the watchdog awake, and :meth:`_follow_up` handles each
        poll after it.
        """
        logger.error(
            "PrintWatchdog RED FLAG [%s]: %s | context=%s",
            flag.rule,
            flag.message,
            flag.context,
        )
        self._record_flag(flag)

        # This door bypasses the EmergencyCoordinator (it holds the adapter
        # and halts it directly, which is the point — no lookups between a
        # red flag and the M112), so it files its own this-job-is-ending
        # intent.  Without one, the idle the printer lands on after the halt
        # reads as a natural finish and a watchdog-stopped print is recorded
        # a SUCCESS — the exact print the learning DB most needs to know
        # went wrong.  Filed before the halt: the terminal transition can
        # arrive the moment the command lands.
        try:
            from kiln.auto_record_hook import register_cancel_intent
            from kiln.printers.base import outcome_printer_name

            register_cancel_intent(outcome_printer_name(self._adapter))
        except Exception:  # noqa: BLE001 — never delay the halt
            logger.debug(
                "PrintWatchdog: cancel-intent registration failed", exc_info=True
            )

        self._estop_attempts = 1
        self._tripped_rule = flag.rule
        try:
            confirmed, said = self._command_stop()
        finally:
            # An e-stop ends the print as surely as a cancel does, and it is
            # the ending we are most certain was not a clean finish.  Noted
            # AFTER the halt is dispatched: this touches the database, and
            # nothing queues in front of stopping the machine.  In
            # ``finally`` so a failed e-stop still records why the print
            # ended — that is the case worth learning from.  Once per trip:
            # a retried stop is the same ending, not another one.
            note_cancel_requested(self._adapter)

        # The stop's own answer rides with the flag, so whatever the callback
        # files says whether the machine was actually stopped.
        with self._lock:
            flag.context["estop_confirmed"] = confirmed
            if said:
                flag.context["estop_result"] = said
        self._settle(confirmed, flag.rule)

        # Recording stays above the e-stop and dispatch stays below it, so a
        # red flag's ordering is exactly what it has always been.
        self._dispatch(flag)

    def _follow_up(self, red: Flag | None, state: Any) -> None:
        """A poll after a stop the printer has not confirmed.

        Recording and dispatch happened once, on the trip; a follow-up only
        decides whether to command the stop again.  It latches without one
        only when no red flag fires AND the printer's own state shows the job
        ended.  A red flag going quiet is not evidence of that: once the
        watchdog has decided a print must stop, the heater cut alone silences
        the temperature rules while the job keeps moving cold.
        """
        observed = _confirmed_state_word(state)
        if red is None and observed in _JOB_ENDED_STATES:
            logger.error(
                "PrintWatchdog: the printer reads %s after the unconfirmed "
                "emergency stop, so the job has ended; the stop is confirmed by "
                "observation and no further stop is sent",
                observed,
            )
            self._latch()
            return
        if red is None:
            logger.error(
                "PrintWatchdog: no red flag after the unconfirmed emergency stop, "
                "but the printer reads %s, which does not show the job ended; "
                "commanding the stop again",
                observed or "nothing readable",
            )
        self._estop_attempts += 1
        confirmed, _said = self._command_stop()
        self._settle(confirmed, red.rule if red is not None else (self._tripped_rule or "unknown"))

    def _command_stop(self) -> tuple[bool, str | None]:
        """Send one emergency stop: ``(confirmed, what it said)``.  Never raises."""
        try:
            result = self._adapter.emergency_stop()
        except Exception as exc:
            logger.exception("PrintWatchdog: emergency_stop() FAILED")
            return False, f"emergency_stop() raised: {exc}"
        said = getattr(result, "message", None)
        return _stop_confirmed(result), (str(said) if said else None)

    def _settle(self, confirmed: bool, rule: str) -> None:
        """Latch on a confirmed stop; otherwise say so, and latch at the ceiling."""
        attempt = self._estop_attempts
        if confirmed:
            logger.error(
                "PrintWatchdog: emergency stop confirmed for [%s] (attempt %d/%d)",
                rule,
                attempt,
                MAX_ESTOP_ATTEMPTS,
            )
            self._latch()
            return
        logger.error(
            "Emergency stop NOT confirmed (attempt %d/%d) — stop the printer at the machine",
            attempt,
            MAX_ESTOP_ATTEMPTS,
        )
        if attempt >= MAX_ESTOP_ATTEMPTS:
            logger.error(
                "PrintWatchdog: stopped retrying after %d emergency stops for [%s] "
                "that the printer never confirmed, so a printer that never "
                "confirms is not commanded forever. It may still be running — "
                "stop it at the machine.",
                attempt,
                rule,
            )
            self._latch()

    def _latch(self) -> None:
        with self._lock:
            self.anomaly_triggered = True

    def _record_flag(self, flag: Flag) -> None:
        with self._lock:
            self._flags.append(flag)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _getattr(obj: Any, name: str) -> Any:
    """Attribute-or-key lookup — tolerant of dataclasses, objects, dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _is_printing(state: Any) -> bool:
    """Return True if the printer is actively printing."""
    s = _getattr(state, "state")
    if s is None:
        return False
    # PrinterStatus enum has .value == "printing"; strings or enum-likes work.
    value = getattr(s, "value", s)
    return str(value).lower() == "printing"


def _confirmed_state_word(state: Any) -> str | None:
    """The run state from a reading Kiln can vouch for, as a lowercase word.

    :func:`~kiln.printers.base.confirmed_state_of` looks through a fault
    headline and never through staleness; a dict reading has no headline to
    look through and gives its ``state`` as it stands.
    """
    if isinstance(state, dict):
        value = state.get("state")
    else:
        from kiln.printers.base import confirmed_state_of

        value = confirmed_state_of(state)
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _as_code(value: Any) -> int:
    """A fault field as an int; 0 when it is absent or unreadable."""
    if not value:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _stop_confirmed(result: Any) -> bool:
    """Did an ``emergency_stop()`` answer say the printer confirmed the stop?

    A result carrying ``success`` counts only when that is literally True.  A
    bare truthy answer with no ``success`` at all is what older test doubles
    return, and counts.  ``None``, ``False`` or ``success=False`` is a stop
    nobody saw land.
    """
    if result is None:
        return False
    if hasattr(result, "success"):
        return result.success is True
    return bool(result)


def _parse_dbm(signal: Any) -> int | None:
    """Parse a wifi signal strength like ``'-72dBm'`` or ``-72`` into an int."""
    if isinstance(signal, (int, float)):
        return int(signal)
    try:
        return int(str(signal).lower().replace("dbm", "").strip())
    except (ValueError, AttributeError):
        return None
