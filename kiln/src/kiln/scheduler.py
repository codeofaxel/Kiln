"""Job scheduler — dispatches queued jobs to available printers.

The scheduler runs in a background thread, periodically checking for:
1. Queued jobs that need to be dispatched
2. Idle printers that can accept work
3. Running jobs that need progress monitoring

It bridges the gap between the job queue (where agents submit work)
and the printer registry (where physical printers live).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiln.events import EventBus, EventType
from kiln.print_signoff import grant_from_record, record_refusal
from kiln.print_start_verdict import resolve_print_start
from kiln.printers.base import PrinterEngagementError, PrinterError, PrinterStatus, status_is_unreachable
from kiln.printers.progress_motion import (
    WATCHED_ENDING_MAX_GAP_S,
    Motion,
    MotionVerdict,
    observe_progress,
    stall_threshold_seconds,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What the scheduler will, and will not, conclude about a print it watches
# ---------------------------------------------------------------------------
#
# THE QUEUE NEVER ENDS A PRINT THE PRINTER HAS NOT ENDED.  It used to: any
# job over two hours of wall-clock was failed while the printer was still
# reporting PRINTING, then handed to the retry path, which reset it to
# QUEUED and dispatched the same file again the moment the machine went
# idle.  A 2h05m print on a Snapmaker U1 was printed three times that way,
# the second plate dropping onto the first one's finished part (reported
# 2026-09-13).  A long print is a long print.
#
# The tempting replacement -- give up after N hours of no progress or no
# contact -- is the same mistake with a longer fuse.  A printer whose Wi-Fi
# dropped keeps printing.  A machine paused at its own screen while
# reporting RUNNING (measured on an A1, 2026-08-11) is waiting for a person,
# not failed.  Any N picks a moment to write "failed" over a print that may
# finish, and that record feeds the learning data.
#
# So the scheduler SAYS what it sees, the moment it sees it, and keeps the
# printer reserved:
#
#   moving      a progress axis advanced -- judged by ``progress_motion``,
#               the same detector the status tools and the resume gate read,
#               so the queue cannot disagree with them about whether a
#               machine is moving;
#   stalled     fresh telemetry, PRINTING, nothing moved past the measured
#               threshold -- the detector itself announces the edge once,
#               as PRINT_STALLED, whoever was looking; the queue carries it
#               on job_status / queue_summary;
#   no contact  no trustworthy reading (unreachable, stale cache, a read
#               that raised) for longer than that same threshold -- announced
#               once per episode as JOB_NO_CONTACT and carried the same way.
#
# The job stays PRINTING, the printer stays busy so nothing is dispatched
# onto an occupied bed, and the only doors out are the machine's own -- an
# idle reading ends the job -- and a person who knows the print is over
# cancelling it.
#
# What Kiln saw still shapes the ENDING.  Idle after continuous, moving
# contact with no named result is recorded as success (an inference, never
# federated).  Idle right after a stall, or after a contact gap longer than
# the watched-ending bound, with no named result is recorded as UNKNOWN and
# the user is asked -- a stalled print that was power-cycled must not be
# banked as a success.


@dataclass
class JobWatch:
    """What the scheduler currently knows about one job it is watching.

    All times are the scheduler's monotonic clock.
    """

    printer_name: str
    #: Last reading Kiln could vouch for: connected, not stale, not an
    #: unreachable state, and the read did not raise.
    last_contact: float
    #: The motion verdict of the last trustworthy reading, if any.
    last_verdict: MotionVerdict | None = None
    #: Why the ending Kiln is about to record should be doubted, set on each
    #: trustworthy reading from what came BEFORE it -- ``None`` when the
    #: previous reading was moving and recent.
    ending_doubt: str | None = None
    #: Loss of contact is the queue's own knowledge (the detector never sees
    #: a reading that did not arrive), so the queue announces it -- once per
    #: episode, not once per poll.
    no_contact_announced: bool = False
    #: Why an ending is doubted because Kiln was not running while it
    #: happened (a restart).  Unlike ``ending_doubt`` it is not judged from
    #: the gap before a reading -- the first reading after a restart has no
    #: gap -- and only a reading of the printer at work clears it.
    restart_doubt: str | None = None

    def stalled(self) -> bool:
        return self.last_verdict is not None and self.last_verdict.stalled

    def moving(self) -> bool:
        return self.last_verdict is not None and self.last_verdict.motion is Motion.MOVING

    def silent_for(self, now: float) -> float:
        return max(0.0, now - self.last_contact)


def _no_contact_note(printer_name: str, silent_seconds: float, cause: str) -> str:
    minutes = int(silent_seconds // 60)
    what = {
        "stale": "its last reading is stale",
        "unreachable": "it is unreachable",
        "read_failed": "every read is failing",
    }.get(cause, "it is not answering")
    return (
        f"Kiln has had no trustworthy reading from {printer_name} for "
        f"{minutes} minutes -- {what}. The print may still be running; Kiln "
        f"cannot tell from here. The job stays open and the printer stays "
        f"reserved until the machine answers again. If you can see the print "
        f"is over, cancel this job."
    )


# ---------------------------------------------------------------------------
# A job that waits, and what it waits for
# ---------------------------------------------------------------------------
#
# A queued job does not start just because a printer went idle.  Two things
# hold it, and both are a person's or the plan's to settle, never a clock's:
#
# THE PLATE.  The last print is still on it.  The file the next job starts
# from carries the maker's own start sequence, which drives the head across
# the plate a few millimetres up, so starting it is a collision.  Every door
# a person starts a print from asks the plate record first
# (``plate_state.start_refusal``); the queue starts prints with nobody there
# to ask, and asks the same record before every start.  The job keeps its
# place until the record changes -- a person says the plate is clear, or a
# camera look does -- and then starts on the next pass.  A file sliced to
# start beside what is on the plate (a quiet start for this very plate)
# starts as it would from any door.
#
# THE TURN.  Below Kiln Business, Kiln runs one printer at a time.  A job for
# a second printer while the first is printing is not refused and not
# failed: it keeps its place and starts once the first is done.  The start
# template's gates say so in a code (``TIER_CONCURRENT_PRINT_LIMIT``, or the
# engagement rule's ``TIER_SINGLE_PRINTER_LIMIT``); nothing was sent, so
# asking again is harmless, and the queue asks again when a printer that was
# busy goes idle, or after :data:`TURN_RECHECK_S` in case the answer changed
# some other way (a plan upgraded, a print started outside Kiln ended).
#
# Neither spends a retry.  A retry is for a start that went wrong; this is a
# start that was right not to happen yet.  Each wait is announced once
# (``JOB_WAITING``) and written on the job (``metadata["waiting"]``) so every
# reader -- the running server, the command line, a server restarted since --
# says the same sentence and the same way through it.

# AFTER A RESTART.  A job Kiln was starting or printing when it stopped
# comes back as it was (``PrintQueue._reload_from_db``) and is settled here,
# once, from what the printer and the plate record say -- never put back in
# line on a guess, because a print that finished while Kiln was away would
# start again the moment its printer went idle:
#
#   * the printer is still printing or paused on it: watched again, as if
#     Kiln had never stopped;
#   * the printer is idle and the print had started (it was PRINTING, or the
#     plate record holds its part from after it was sent): watched for one
#     reading, which closes it with whatever ending the printer names --
#     and when it names none, ``unknown``, which asks the person how it went
#     the way every unwatched ending does;
#   * the printer is busy with a different file: it ended while Kiln was
#     away; closed as ``unknown`` the same way;
#   * it was STARTING and nothing shows a printer took it: back in line,
#     where the plate gate still stands in front of its start;
#   * the printer is not answering: it waits, and says so, until it does.
#
# Only one case asks the person directly: a job from before Kiln recorded
# where a start was sent, that named no printer, was printing, and left no
# part on any plate record.  Kiln cannot tell where it ran or whether it
# finished, so it is not printed again; the job ends saying so and how to
# queue it again.

#: How long a job waiting its turn goes before the queue asks the start
#: gate again even though no printer it watched went idle.  The gate refuses
#: before anything is sent, so asking costs one peer check, never a print.
TURN_RECHECK_S = 300.0

#: The start-gate codes that mean "not yet, it is another printer's turn".
_TURN_CODES = frozenset({"TIER_CONCURRENT_PRINT_LIMIT", "TIER_SINGLE_PRINTER_LIMIT"})

WAIT_PLATE = "plate"
WAIT_TURN = "turn"
WAIT_RECOVERING = "recovering"

#: Slack between the queue stamping a start and the plate record writing it.
_START_RECORD_SLACK_S = 120.0


@dataclass
class TurnWait:
    """The queue is waiting for another printer's print, one at a time."""

    busy: frozenset[str]        # the printers not idle when the wait began
    started: float              # the scheduler's clock then
    note: dict[str, Any]        # what the waiting jobs say


def _listed(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else "another printer"
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _file_label(file_name: str) -> str:
    from kiln.plate_state import pretty_job_name

    return pretty_job_name(file_name) or "the next job"


class _Label(str):
    """Kiln's own words for a job (the rest of a fill), as opposed to a
    file's name: only these are capitalised or made plural, so a file
    called ``the other half.stl`` is never read as several copies."""

    many: bool = False


def _job_label(job: Any) -> str:
    """What a person calls a queued job: its file, or for the rest of a fill
    ``the other 14 copies of bracket``."""
    fill = (getattr(job, "metadata", None) or {}).get("fill")
    name = _file_label(getattr(job, "file_name", "") or "")
    if isinstance(fill, dict) and fill.get("copies"):
        n = int(fill["copies"])
        label = _Label(f"the other {n} cop{'y' if n == 1 else 'ies'} of {name}")
        label.many = n != 1
        return label
    return name


def _same_file(a: str | None, b: str | None) -> bool:
    """Whether two names a printer and the queue use are one file: compared
    by base name, then by the name a person reads (a printer may drop the
    folder or the ``.gcode.3mf`` the queue kept)."""
    import os

    from kiln.plate_state import pretty_job_name

    if not a or not b:
        return False
    if os.path.basename(str(a)) == os.path.basename(str(b)):
        return True
    return bool(pretty_job_name(str(a))) and pretty_job_name(str(a)) == pretty_job_name(str(b))


def _cap(text: str) -> str:
    """Kiln's own words for a job (``the other 14 copies of ...``) opening a
    sentence, capitalised.  A file's own name is left exactly as it is."""
    return text[:1].upper() + text[1:] if isinstance(text, _Label) else text


def _many(label: str) -> bool:
    """Whether a job's label names several things (the rest of a fill)."""
    return isinstance(label, _Label) and label.many


def _verb(label: str, one: str, several: str) -> str:
    return several if _many(label) else one


def plate_wait_sentence(
    file_name: str, printers: list[str], state: Any, *, code: str, camera: bool, label: str | None = None,
) -> str:
    """What a job waiting for a plate says, in one breath: what is there,
    that this file is next, and the one thing that starts it.  *label* is
    what the person calls the job (:func:`_job_label`), when not its file."""
    name = label or _file_label(file_name)
    from kiln.plate_state import PLATE_CHANGED_CODE

    if code == PLATE_CHANGED_CODE:
        where = printers[0] if printers else "the printer"
        return (
            f"{_cap(name)} {_verb(name, 'was', 'were')} sliced to start beside what was on {where}'s plate, and the "
            f"plate has changed since, so {_verb(name, 'it waits', 'they wait')} rather than start there. Slice "
            f"{_verb(name, 'it', 'them')} again for the plate as it is now, or clear the plate and say so."
        )
    look = " Kiln can also look through the printer's camera (look_at_plate)." if camera else ""
    if len(printers) > 1:
        return (
            f"{_cap(name)} {_verb(name, 'is', 'are')} waiting for a printer with a clear plate: {_listed(printers)} each "
            f"still hold their last "
            f"print. Take the part off any one of them and say so (tell your assistant the plate is clear, or run "
            f"`kiln plate clear`), and Kiln {_verb(name, 'starts ' + name, 'prints the next plate of them')} there.{look}"
        )
    where = printers[0] if printers else "the printer"
    holds = state.holds_sentence() if state is not None else "The last print is still on the plate."
    return (
        f"{_cap(name)} {_verb(name, 'is', 'are')} next on {where}. {holds} Take it off and say so (tell your assistant "
        f"the plate is clear, or run "
        f"`kiln plate clear`), and Kiln {_verb(name, 'starts ' + name, 'prints the next plate of them')} on its own.{look}"
    )


def turn_wait_sentence(file_name: str, printer_name: str | None, busy: list[str], *, label: str | None = None) -> str:
    """What a job waiting its turn says: whose print it waits for, and that
    it keeps its place.  The plan that runs printers side by side is
    :data:`TURN_UPGRADE`, said once by whoever shows the wait, never inside
    every job's sentence."""
    name = label or _file_label(file_name)
    other = _listed(busy)
    on = f" on {printer_name}" if printer_name else ""
    return (
        f"Kiln runs one printer at a time on this plan, and {other} is printing. {_cap(name)} "
        f"{_verb(name, 'keeps its', 'keep their')} place in line and {_verb(name, 'starts', 'start')}{on} once "
        f"{other} is done."
    )


#: Said once beside a turn wait: what lifts it, as a fact.
TURN_UPGRADE = "Kiln Business runs your printers side by side: https://kiln3d.com/pricing"


def describe_wait(
    job: Any, queue: Any, *, running: dict[str, str] | None = None, idle: frozenset[str] | None = None,
    retry_at: float | None = None, registered: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Why a QUEUED job has not started yet, for a person: ``{"kind",
    "sentence", "ahead"}`` plus whatever the wait on the job carries, or
    ``None`` for a job that is not queued.

    The wait written on the job (the plate, the turn) comes first: it is the
    one a person has to act on.  Otherwise the reason is read from what the
    running queue knows -- *running* (printer -> job printing there), *idle*
    (printers last read idle) and *retry_at* -- each ``None`` where no queue
    is running in this process (the command line), and then only the place
    in line is said.  ``ahead`` counts the queued jobs that go first.
    """
    from kiln.queue import JobStatus

    written = (job.metadata or {}).get("waiting")
    if getattr(job, "status", None) != JobStatus.QUEUED:
        # A job a restart left mid-print, waiting for its printer to answer.
        if isinstance(written, dict) and written.get("kind") == WAIT_RECOVERING:
            return {**written, "ahead": 0}
        return None
    try:
        line = [j.id for j in queue.queued_for(job.printer_name)]
        ahead = line.index(job.id) if job.id in line else 0
    except Exception:  # noqa: BLE001
        ahead = 0
    name = _job_label(job)
    place = "next in line" if ahead == 0 else f"in line behind {ahead} other job{'s' if ahead != 1 else ''}"
    if isinstance(written, dict) and written.get("sentence"):
        return {**written, "ahead": ahead}
    if retry_at is not None and retry_at > time.time():
        when = time.strftime("%H:%M", time.localtime(retry_at))
        return {"kind": "retry", "ahead": ahead,
                "sentence": f"{_cap(name)} did not start on the last try; Kiln tries {_verb(name, 'it', 'them')} "
                            f"again at {when}."}
    target = job.printer_name
    if target and registered is not None and target not in registered:
        return {"kind": "no_printer", "ahead": ahead, "printers": [target],
                "sentence": f"{_cap(name)} {_verb(name, 'is', 'are')} for {target}, which is not set up in Kiln. "
                            f"{_verb(name, 'It waits', 'They wait')} until {target} is added, or cancel "
                            f"{_verb(name, 'it', 'them')} and queue {_verb(name, 'it', 'them')} for another printer."}
    if target and running is not None and target in running:
        current = None
        try:
            current = _file_label(queue.get_job(running[target]).file_name)
        except Exception:  # noqa: BLE001
            current = None
        doing = f"is printing {current}" if current else "is printing"
        return {"kind": "printing", "ahead": ahead, "printers": [target],
                "sentence": f"{target} {doing}. {_cap(name)} {_verb(name, 'is', 'are')} {place} and "
                            f"{_verb(name, 'starts', 'start')} when it finishes and the plate is clear."}
    if target and idle is not None and target not in idle:
        return {"kind": "busy", "ahead": ahead, "printers": [target],
                "sentence": f"{target} is busy or not answering right now. {_cap(name)} {_verb(name, 'is', 'are')} "
                            f"{place} and {_verb(name, 'starts', 'start')} when it is free."}
    if not target and idle is not None and not idle:
        return {"kind": "busy", "ahead": ahead,
                "sentence": f"Every printer is busy or not answering right now. {_cap(name)} "
                            f"{_verb(name, 'is', 'are')} {place} for the first one free."}
    return {"kind": "in_line", "ahead": ahead, "sentence": f"{_cap(name)} {_verb(name, 'is', 'are')} {place}."}


class JobScheduler:
    """Background scheduler that dispatches print jobs to printers.

    Lifecycle:
        scheduler = JobScheduler(queue, registry, event_bus)
        scheduler.start()   # launches background thread
        ...
        scheduler.stop()    # graceful shutdown

    The scheduler polls every ``poll_interval`` seconds (default 5).  It
    never ends a print the printer has not ended: a job that stops moving
    or stops answering is announced (PRINT_STALLED from the detector,
    JOB_NO_CONTACT from here, and on ``job_status``) and stays PRINTING
    with its printer reserved until the machine reports idle or a person
    cancels it.  See the module comment.
    """

    def __init__(
        self,
        queue: PrintQueue,
        registry: PrinterRegistry,
        event_bus: EventBus,
        poll_interval: float = 5.0,
        max_retries: int = 2,
        retry_backoff_base: float = 30.0,
        persistence: object | None = None,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._persistence = persistence
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_jobs: dict[str, str] = {}  # job_id -> printer_name
        self._retry_counts: dict[str, int] = {}  # job_id -> attempts so far
        # Jobs this scheduler has actually SEEN printing.  An idle printer
        # only proves a job it was watched running has ended cleanly; a job
        # that was dispatched but never observed printing may have failed to
        # start, and claiming success for it would be a guess.
        self._seen_printing: set[str] = set()
        self._retry_not_before: dict[str, float] = {}  # job_id -> earliest retry timestamp
        # job_id -> what Kiln currently knows about the print it dispatched.
        # Opened at dispatch, so a job whose printer never answers still has
        # a last-contact time to measure silence from.
        self._watch: dict[str, JobWatch] = {}
        # job_id -> why the latest reading could not be trusted
        # ("unreachable" / "stale" / "read_failed"), for the note's wording.
        self._contact_cause: dict[str, str] = {}
        # Monotonic clock, overridable so tests can drive hours in a second.
        self._clock = time.monotonic
        self._lock = threading.Lock()
        # printer -> the plate record version whose wait was announced, so a
        # finished part on the plate is announced once, not every pass.
        self._plate_announced: dict[str, tuple[str, str]] = {}
        # (job_id, printer) -> (plate version, start_hold answer): a job's
        # file is read for its start contract once per plate, not per pass.
        self._plate_answers: dict[tuple[str, str], tuple[tuple[str, str], Any]] = {}
        # One printer at a time: the wait for another printer's print.
        self._turn_wait: TurnWait | None = None
        # The printers last read as idle, for a reader asking why a job waits.
        self._last_idle: frozenset[str] = frozenset()
        # Fill jobs being laid out, sliced and started off this thread: their
        # printer is taken, and nothing judges them until that finishes.
        self._preparing: set[str] = set()
        # How background work is started; a test runs it in line.
        self._spawn: Any = lambda work: threading.Thread(target=work, name="kiln-fill", daemon=True).start()

    @property
    def is_running(self) -> bool:
        """Whether the scheduler background thread is running."""
        return self._running

    @property
    def active_jobs(self) -> dict[str, str]:
        """Return a copy of the active job->printer mapping."""
        with self._lock:
            return dict(self._active_jobs)

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="kiln-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Job scheduler started (poll every %.1fs)", self._poll_interval)

    def stop(self) -> None:
        """Stop the scheduler gracefully.

        Wakes the loop's doze instead of waiting it out — stop() sits on
        the server's SIGTERM path, and an un-wakeable
        ``time.sleep(poll_interval)`` there reads as a wedged shutdown.
        The join keeps its timeout as the bound for a tick that is
        blocked mid network call (an unreachable printer's status query
        can hold the doze's whole budget).
        """
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            self._thread = None
        logger.info("Job scheduler stopped")

    def _requeue_or_fail(
        self,
        job_id: str,
        error_msg: str,
        failed_list: list[dict[str, str]],
        printer_name: str | None = None,
        machine_reported: bool = False,
    ) -> bool:
        """Try to re-queue a failed job if retries remain.

        Returns ``True`` if the job was re-queued, ``False`` if it was
        permanently marked as failed (appended to *failed_list*).

        *machine_reported* marks the exhausting failure as the PRINTER's own
        verdict (error state observed while the job was being watched) rather
        than the queue's (unregistered printer, dispatch error).  Only a machine verdict is eligible for community
        contribution — see :meth:`_auto_record_outcome`.
        """
        count = self._retry_counts.get(job_id, 0)
        if count < self._max_retries:
            self._retry_counts[job_id] = count + 1
            # Exponential backoff: 30s, 60s, 120s, ...
            delay = self._retry_backoff_base * (2**count)
            self._retry_not_before[job_id] = time.time() + delay
            # Reset the job back to QUEUED so a future tick can redispatch it.
            # The retry is a fresh physical print — the next attempt must
            # earn its own "seen printing" observation and its own clock.
            self._seen_printing.discard(job_id)
            self._watch.pop(job_id, None)
            with self._lock:
                job = self._queue.get_job(job_id)
                job.status = JobStatus.QUEUED
                job.started_at = None
                job.error = None
            self._event_bus.publish(
                EventType.JOB_SUBMITTED,
                {
                    "job_id": job_id,
                    "retry": count + 1,
                    "max_retries": self._max_retries,
                    "reason": error_msg,
                    "retry_delay_seconds": delay,
                },
                source="scheduler",
            )
            logger.info(
                "Re-queued job %s (retry %d/%d, backoff %.0fs): %s",
                job_id,
                count + 1,
                self._max_retries,
                delay,
                error_msg,
            )
            return True

        # Retries exhausted — mark permanently failed
        self._fail_permanently(
            job_id, error_msg, failed_list, printer_name=printer_name,
            contribute=machine_reported,
        )
        return False

    def _fail_permanently(
        self,
        job_id: str,
        error_msg: str,
        failed_list: list[dict[str, str]],
        printer_name: str | None = None,
        *,
        contribute: bool = False,
        determined_by: str = "observed",
    ) -> None:
        """Mark *job_id* FAILED for good and tell everyone who listens.

        The one ending shared by the retry path (retries exhausted) and the
        safety-latch path, so the JOB_FAILED event, the outcome row and the
        tick report cannot drift apart.
        """
        self._forget_watch(job_id)
        self._queue.mark_failed(job_id, error_msg)
        self._event_bus.publish(
            EventType.JOB_FAILED,
            {"job_id": job_id, "error": error_msg},
            source="scheduler",
        )
        if printer_name:
            self._auto_record_outcome(
                job_id, printer_name, "failed", error_msg=error_msg,
                determined_by=determined_by, contribute=contribute,
            )
        failed_list.append({"job_id": job_id, "error": error_msg})

    def _forget_watch(self, job_id: str) -> None:
        """Drop every per-watch record for a job that is no longer active."""
        self._retry_counts.pop(job_id, None)
        self._retry_not_before.pop(job_id, None)
        self._seen_printing.discard(job_id)
        self._watch.pop(job_id, None)
        self._contact_cause.pop(job_id, None)

    # ------------------------------------------------------------------
    # Watching: say what is seen, decide nothing the printer has not
    # ------------------------------------------------------------------

    def watch_note(self, job_id: str) -> dict[str, Any] | None:
        """What Kiln currently knows about a job it is watching, for the
        queue tools -- or ``None`` for a job it is not watching.

        ``state`` is one of ``moving`` / ``stalled`` / ``no_contact`` /
        ``watching`` (contact, but no verdict yet).  ``note`` is the one
        plain-English sentence to show a person, present only when there is
        something to say.
        """
        watch = self._watch.get(job_id)
        if watch is None:
            return None
        now = self._clock()
        silent = watch.silent_for(now)
        note: dict[str, Any] = {"printer_name": watch.printer_name}
        if silent >= stall_threshold_seconds():
            cause = self._contact_cause.get(job_id, "unreachable")
            note.update(
                state="no_contact", since_seconds=round(silent),
                note=_no_contact_note(watch.printer_name, silent, cause),
            )
        elif watch.stalled():
            assert watch.last_verdict is not None
            note.update(
                state="stalled",
                since_seconds=round(watch.last_verdict.frozen_for_seconds or 0.0),
                note=watch.last_verdict.note(),
            )
        elif watch.moving():
            note.update(state="moving")
        else:
            note.update(state="watching")
        return note

    def watch_alerts(self) -> list[dict[str, Any]]:
        """Every watched job that currently needs a person: stalled or out
        of contact.  Empty when everything is moving."""
        alerts = []
        for job_id in list(self._watch):
            note = self.watch_note(job_id)
            if note and note["state"] in ("stalled", "no_contact"):
                alerts.append({"job_id": job_id, **note})
        return alerts

    def _observe(
        self, job_id: str, printer_name: str, adapter: Any, state: Any, job: Any, now: float,
    ) -> None:
        """Record one trustworthy-or-not reading of a watched job.

        Feeds every reading to the progress-motion detector (which announces
        a stall's edge itself), refreshes the last-contact time on readings
        Kiln can vouch for, and announces a loss of contact ONCE per
        episode.  Decides nothing about the job itself.
        """
        watch = self._watch.get(job_id)
        if watch is None:
            # Watched before this process dispatched it (a restart); start
            # the record here rather than have no record at all.
            watch = self._watch[job_id] = JobWatch(printer_name, last_contact=now)
        verdict = observe_progress(adapter, state, job, now=now)

        connected = bool(getattr(state, "connected", False))
        headline = getattr(state, "state", None)
        if not connected or status_is_unreachable(headline):
            self._observe_silence(job_id, printer_name, now, cause="unreachable")
            return
        if headline is PrinterStatus.STALE:
            self._observe_silence(job_id, printer_name, now, cause="stale")
            return

        # A trustworthy reading.  Before overwriting, judge what came before
        # it: that is what decides whether an ending seen on THIS reading
        # was actually watched.
        gap = watch.silent_for(now)
        if watch.stalled():
            watch.ending_doubt = (
                f"the printer had not moved for "
                f"{int((watch.last_verdict.frozen_for_seconds or 0) // 60)} minutes "
                f"right before it went idle"
            )
        elif gap > WATCHED_ENDING_MAX_GAP_S:
            watch.ending_doubt = (
                f"Kiln had no trustworthy reading for {int(gap // 60)} minutes "
                f"right before the printer went idle"
            )
        else:
            watch.ending_doubt = None

        if getattr(state, "is_occupied", False) is True:
            # Seen at work since the restart: its ending will be watched.
            watch.restart_doubt = None
        if watch.no_contact_announced:
            logger.info("Job %s: %s is answering again", job_id, printer_name)
            watch.no_contact_announced = False
        watch.last_contact = now
        watch.last_verdict = verdict
        self._contact_cause.pop(job_id, None)

    def _observe_silence(self, job_id: str, printer_name: str, now: float, *, cause: str) -> None:
        """A reading Kiln cannot vouch for.  The clock keeps running; once
        the silence outlasts the stall threshold it is announced once."""
        watch = self._watch.get(job_id)
        if watch is None:
            watch = self._watch[job_id] = JobWatch(printer_name, last_contact=now)
        self._contact_cause[job_id] = cause
        silent = watch.silent_for(now)
        if silent >= stall_threshold_seconds() and not watch.no_contact_announced:
            watch.no_contact_announced = True
            note = _no_contact_note(printer_name, silent, cause)
            logger.warning("Job %s on %s: %s", job_id, printer_name, note)
            self._event_bus.publish(
                EventType.JOB_NO_CONTACT,
                {
                    "job_id": job_id,
                    "printer_name": printer_name,
                    "silent_for_seconds": round(silent),
                    "cause": cause,
                    "note": note,
                },
                source="scheduler",
            )

    def _ending_doubt(self, job_id: str) -> str | None:
        watch = self._watch.get(job_id)
        return (watch.restart_doubt or watch.ending_doubt) if watch else None

    def _rank_printers(self, available: list[str], job) -> list[str]:
        """Reorder available printers by historical success rate for this job.

        When a persistence layer is configured and the job metadata contains
        ``file_hash`` or ``material_type``, printers are sorted so that those
        with the highest historical success rate for the given criteria come
        first.  Printers without history are placed last (original order
        preserved among them).

        If no persistence is configured or no ranking data is available, the
        list is returned unchanged.
        """
        if not self._persistence:
            return available
        file_hash = job.metadata.get("file_hash") if job.metadata else None
        material_type = job.metadata.get("material_type") if job.metadata else None
        if not file_hash and not material_type:
            return available
        rankings = self._persistence.suggest_printer_for_outcome(
            file_hash=file_hash,
            material_type=material_type,
        )
        if not rankings:
            return available
        # Build a score map: printer_name -> success_rate
        score = {r["printer_name"]: r["success_rate"] for r in rankings}
        # Sort available printers by score (highest first); unknown printers
        # sort last (score -1) but preserve their relative order via the
        # enumerate index as a tiebreaker.
        indexed = list(enumerate(available))
        indexed.sort(key=lambda pair: (-score.get(pair[1], -1), pair[0]))
        return [name for _, name in indexed]

    def _auto_record_outcome(
        self,
        job_id: str,
        printer_name: str,
        outcome: str,
        error_msg: str | None = None,
        determined_by: str = "observed",
        contribute: bool = False,
    ) -> None:
        """Best-effort auto-record a print outcome to the learning database.

        *contribute* federates the resolution to the community pool (opt-in
        gated, best-effort).  Call sites set it ONLY for machine-testimony
        verdicts about prints this scheduler watched: a job seen printing
        that ended idle (success), or one whose printer reported an error
        state mid-watch (failed).  The queue's own words — unregistered
        printer, safety latch, an ending it doubts — are queue events, not
        verdicts on the model, and contributing them would
        poison a corpus keyed by the model's geometry.  ``unknown`` and
        ``cancelled`` never contribute (the helper refuses non-verdicts, and
        the call sites don't ask).
        """
        if not self._persistence:
            return
        try:
            # Try to get job metadata for richer outcome data
            job = self._queue.get_job(job_id)

            # Check if a DECIDED outcome is already recorded (an agent may
            # have beaten us).  An unresolved row (pending — opened at
            # print start) is exactly what this call should settle.
            existing = self._persistence.get_print_outcome(job_id)
            if existing is not None and existing.get("outcome") not in ("pending", "unknown"):
                return  # decided already — don't overwrite
            if existing is None:
                # The scheduler is a RESOLVER, never a second author.  The
                # adapter layer (start_print + the get_state wiring) owns
                # the row: it opens 'pending' at start and may already have
                # recorded the watched ending under the printer's own job
                # label.  Writing here without an unresolved row to settle
                # would author a duplicate of an ending someone else
                # recorded — so if nothing is owed, say nothing.
                from kiln.persistence import _file_stem_token

                unresolved = self._persistence.list_unresolved_outcomes(
                    printer_name=printer_name, limit=50,
                )
                tokens = {
                    _file_stem_token(job.file_name if job else None),
                    _file_stem_token(job_id),
                } - {""}
                claimable = [
                    row for row in unresolved
                    if _file_stem_token(row.get("file_name")) in tokens
                ] or (unresolved if len(unresolved) == 1 else [])
                if not claimable:
                    return

            self._persistence.save_print_outcome(
                {
                    "job_id": job_id,
                    "printer_name": printer_name,
                    "file_name": job.file_name if job else None,
                    "file_hash": job.metadata.get("file_hash") if job and job.metadata else None,
                    "material_type": job.metadata.get("material_type") if job and job.metadata else None,
                    "outcome": outcome,
                    "quality_grade": None,  # Only agents can assess quality
                    "failure_mode": None,  # Only agents can classify failure mode
                    "settings": None,
                    "environment": None,
                    "notes": f"Auto-recorded by scheduler. {error_msg}" if error_msg else "Auto-recorded by scheduler.",
                    "agent_id": "auto",
                    "determined_by": determined_by,
                    "created_at": time.time(),
                }
            )
            logger.debug("Auto-recorded %s outcome for job %s", outcome, job_id)
            # Federate the resolution.  The adapter layer's own doors
            # (watched terminal edge, reconcile-on-reconnect) federate the
            # endings THEY resolve; a row this scheduler settles — via its
            # queue knowledge, when the adapter couldn't attribute the
            # ending — reached only the local DB until 2026-08-05, so
            # queue-managed prints were systematically missing from the
            # shared corpus.  Best-effort in its own try: a federation
            # hiccup must never disturb the local record above.
            if contribute and outcome in ("success", "failed"):
                try:
                    from kiln import community_autofire

                    community_autofire.contribute_resolved_outcome(
                        outcome=outcome,
                        printer_file_name=job.file_name if job else None,
                        job_id=job_id,
                        printer_name=printer_name,
                        material=(
                            job.metadata.get("material_type")
                            if job and job.metadata else None
                        ),
                    )
                except Exception:
                    logger.debug(
                        "scheduler community contribution skipped (best-effort)",
                        exc_info=True,
                    )
        except Exception:
            logger.debug("Failed to auto-record outcome for job %s (non-fatal)", job_id, exc_info=True)

    def _emergency_block_reason(self, printer_name: str) -> str | None:
        """Return dispatch block reason when a printer is emergency-latched."""
        try:
            from kiln.emergency import get_emergency_coordinator

            status = get_emergency_coordinator().get_latch_status(printer_name)
        except Exception as exc:
            # Best effort: if safety status can't be read, do not block dispatch.
            logger.debug("Emergency status lookup failed for %s: %s", printer_name, exc)
            return None

        if not bool(status.get("latched")):
            return None
        blockers = status.get("critical_interlocks_pending") or []
        if blockers:
            return (
                "Emergency latch is active; critical interlocks pending: "
                + ", ".join(str(x) for x in blockers)
            )
        return "Emergency latch is active; operator acknowledgement + clear required."

    # ------------------------------------------------------------------
    # The rest of a fill (see the module comment above)
    # ------------------------------------------------------------------

    def _turn_refusal(self, adapter: Any) -> str | None:
        """The one-printer-at-a-time code a start on *adapter* would meet
        now, or ``None``.  The start gate's own check, asked before any work:
        it reads the other printers and sends nothing."""
        try:
            from kiln.printers.print_gate import _concurrent_fleet_verdict

            verdict = _concurrent_fleet_verdict(adapter)
        except Exception:  # noqa: BLE001 -- the start itself is the gate; this only saves a slice
            return None
        code = str((verdict or {}).get("code") or "")
        return code if code in _TURN_CODES else None

    def _start_fill_job(self, job: Any, printer_name: str) -> None:
        """Take *printer_name* for *job* and lay out, slice and start its
        plate off this thread (:meth:`_run_fill_job`)."""
        with self._lock:
            self._active_jobs[job.id] = printer_name
            self._preparing.add(job.id)
        self._queue.note_waiting(job.id, None)
        logger.info("Job %s: laying out the next plate of copies on %s", job.id, printer_name)
        self._spawn(lambda: self._run_fill_job(job, printer_name))

    def _run_fill_job(self, job: Any, printer_name: str) -> None:
        """The print pipeline every fill runs -- validate, lay out as many
        copies as fit, slice, check the sliced file against the plate,
        upload, start -- for the plate as it is now."""
        fill = dict((job.metadata or {}).get("fill") or {})
        result, error = None, None
        try:
            from kiln.pipelines import reslice_and_print

            result = reslice_and_print(
                model_path=str(fill.get("model_path") or ""),
                printer_name=printer_name,
                printer_id=fill.get("printer_id"),
                overrides=fill.get("overrides"),
                slicer_path=fill.get("slicer_path"),
                copies=int(fill.get("copies") or 1),
                spacing_mm=float(fill.get("spacing_mm") or 10.0),
                signoff=(job.metadata or {}).get("preview_signoff"),
            )
        except Exception as exc:  # noqa: BLE001 -- said on the job, never lost
            error = exc
        try:
            self._finish_fill_job(job, printer_name, result, error)
        except Exception:  # noqa: BLE001
            logger.exception("Job %s: could not record how its plate of copies went", job.id)
            with self._lock:
                self._preparing.discard(job.id)
                self._active_jobs.pop(job.id, None)

    def _finish_fill_job(self, job: Any, printer_name: str, result: Any, error: BaseException | None) -> None:
        steps = {getattr(st, "name", ""): st for st in (getattr(result, "steps", None) or [])}
        start = steps.get("start_print")
        sliced = steps.get("slice")
        start_data = (getattr(start, "data", None) or {}) if start is not None else {}
        block = ((getattr(sliced, "data", None) or {}).get("copies") or {}) if sliced is not None else {}
        placed = block.get("placed") if isinstance(block.get("placed"), int) else None
        with self._lock:
            self._preparing.discard(job.id)
        if result is not None and getattr(result, "success", False):
            started_file = start_data.get("file_name")
            if started_file:
                self._queue.update_metadata(job.id, {"started_file": started_file})
            self._queue.mark_printing(job.id)
            self._watch[job.id] = JobWatch(printer_name, last_contact=self._clock())
            self._event_bus.publish(
                EventType.JOB_STARTED,
                {"job_id": job.id, "printer_name": printer_name, "file_name": started_file or job.file_name,
                 "copies": placed},
                source="scheduler",
            )
            wanted = int(((job.metadata or {}).get("fill") or {}).get("copies") or 0)
            if placed is not None and 0 < placed < wanted:
                self._queue_rest(job, wanted - placed)
            return
        with self._lock:
            self._active_jobs.pop(job.id, None)
        code = str(start_data.get("code") or "")
        if code in _TURN_CODES:
            # Turned away at the start for another printer's turn: nothing
            # printed, the job keeps its place and waits for that printer.
            self._wait_turn(job, printer_name, sorted(self._last_idle), code, {})
            return
        why = str(error) if error is not None else str(getattr(result, "message", "") or "the plate did not start")
        self._requeue_or_fail(job.id, f"The next plate of copies did not start on {printer_name}: {why}", [])

    def _queue_rest(self, job: Any, copies: int) -> None:
        """Queue the copies this plate could not take as the plate after it,
        on the same terms: the same part, printer and yes."""
        metadata = {k: v for k, v in (job.metadata or {}).items() if k in ("preview_signoff",)}
        fill = {**((job.metadata or {}).get("fill") or {}), "copies": int(copies)}
        rest_id = self._queue.submit(
            file_name=job.file_name, printer_name=job.printer_name, submitted_by="multi_copy_print",
            priority=getattr(job, "priority", 0), metadata={**metadata, "fill": fill},
        )
        self._event_bus.publish(
            EventType.JOB_QUEUED,
            {"job_id": rest_id, "file_name": job.file_name, "printer_name": job.printer_name, "copies": int(copies)},
            source="scheduler",
        )
        logger.info("Job %s: %d copies did not fit; queued as job %s for the next plate", job.id, copies, rest_id)

    # ------------------------------------------------------------------
    # After a restart (see the module comment above)
    # ------------------------------------------------------------------

    def _settle_unfinished(self, failed: list[dict[str, str]]) -> None:
        """Settle every job left starting, printing or paused with nothing
        watching it.  Cheap when there are none, which is every pass but the
        first after a restart."""
        with self._lock:
            watched = set(self._active_jobs)
        try:
            orphans = [j for j in self._queue.unfinished() if j.id not in watched]
        except Exception:  # noqa: BLE001
            return
        for job in orphans:
            try:
                self._settle_one(job, failed)
            except Exception:  # noqa: BLE001 -- one job's fault never stops the queue
                logger.warning("Could not settle job %s after a restart", job.id, exc_info=True)

    def _settle_one(self, job: Any, failed: list[dict[str, str]]) -> None:
        name = _file_label(job.file_name)
        was = getattr(job.status, "value", "")
        printer_name = job.printer_name or (job.metadata or {}).get("dispatched_to") or self._where_it_ran(job)
        if not printer_name:
            if was == "starting":
                # No printer named, none recorded, no part anywhere: nothing
                # took it.  Back in line; the plate gate still guards it.
                self._queue.requeue(job.id)
                logger.info("Job %s: no printer took it before the restart; back in line", job.id)
                return
            self._queue.note_waiting(job.id, None)
            self._queue.mark_failed(
                job.id,
                f"Kiln was restarted while {name} was printing, and it cannot tell on which printer or whether it "
                f"finished, so it will not print it again. If it did not print, queue it again.",
            )
            failed.append({"job_id": job.id, "error": "unsettled after restart"})
            self._event_bus.publish(
                EventType.JOB_FAILED,
                {"job_id": job.id, "reason": "unsettled_after_restart", "file_name": job.file_name},
                source="scheduler",
            )
            return
        try:
            adapter = self._registry.get(printer_name)
        except PrinterNotFoundError:
            self._queue.note_waiting(job.id, None)
            self._queue.mark_failed(
                job.id,
                f"Kiln was restarted while {name} was on {printer_name}, and {printer_name} is no longer set up in "
                f"Kiln, so how it ended cannot be read. It will not print again; queue it again if it did not print.",
            )
            failed.append({"job_id": job.id, "error": "printer no longer registered"})
            return
        from kiln.printers.engagement import internal_read

        try:
            with internal_read():
                state = adapter.get_state()
                progress = adapter.get_job()
        except Exception:  # noqa: BLE001 -- not answering: wait, and say so
            state, progress = None, None
        headline = getattr(state, "state", None)
        if (
            state is None or not getattr(state, "connected", False)
            or status_is_unreachable(headline) or headline is PrinterStatus.STALE
        ):
            self._queue.note_waiting(job.id, {
                "kind": WAIT_RECOVERING, "printers": [printer_name],
                "sentence": (
                    f"Kiln was restarted while {name} was {'starting' if was == 'starting' else 'printing'} on "
                    f"{printer_name}, and {printer_name} is not answering yet. Kiln picks it back up as soon as it "
                    "does; nothing is started again in the meantime."
                ),
            })
            return
        running = getattr(state, "effective_state", headline)
        current = getattr(progress, "file_name", None)
        expected = (job.metadata or {}).get("started_file") or job.file_name
        same_file = not current or _same_file(current, expected) or (
            # The rest of a fill names a part, not the file it was sliced to;
            # a print the plate record shows started since the job did is it.
            isinstance((job.metadata or {}).get("fill"), dict) and self._started_on(job, adapter)
        )
        if running in (PrinterStatus.PRINTING, PrinterStatus.PAUSED) and same_file:
            self._adopt(job, printer_name, doubt=None)
            # The queue's state follows the printer's: a pause resumed at the
            # machine while Kiln was away is printing again, and a start the
            # printer took is printing.
            if running is PrinterStatus.PRINTING and was in ("starting", "paused"):
                self._queue.mark_printing(job.id)
            elif running is PrinterStatus.PAUSED and was in ("starting", "printing"):
                if was == "starting":
                    self._queue.mark_printing(job.id)
                self._queue.mark_paused(job.id)
            logger.info("Job %s: still on %s after the restart; watching it again", job.id, printer_name)
            return
        if running in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
            # Busy with something else: this one ended while Kiln was away.
            self._queue.note_waiting(job.id, None)
            if was == "starting" and not self._started_on(job, adapter):
                self._queue.requeue(job.id)
                return
            self._close_unwatched(
                job, printer_name,
                f"Kiln was restarted while {name} was on {printer_name}, and {printer_name} has since started "
                f"{_file_label(current)}; how {name} ended was not seen.",
            )
            return
        if was == "starting" and not self._started_on(job, adapter):
            self._queue.note_waiting(job.id, None)
            self._queue.requeue(job.id)
            logger.info("Job %s: %s never took it before the restart; back in line", job.id, printer_name)
            return
        # It ran, and the printer is idle now: one watched reading closes it
        # with the ending the printer names, or ``unknown`` if it names none.
        # The queue's states only end from PRINTING, so it is put there first.
        if was in ("starting", "paused"):
            self._queue.mark_printing(job.id)
        self._adopt(job, printer_name, doubt="Kiln was restarted while it printed, so it did not see it end")

    def _adopt(self, job: Any, printer_name: str, *, doubt: str | None) -> None:
        """Watch *job* on *printer_name* as if this run had dispatched it."""
        self._queue.note_waiting(job.id, None)
        with self._lock:
            self._active_jobs[job.id] = printer_name
        self._seen_printing.add(job.id)
        self._watch[job.id] = JobWatch(printer_name, last_contact=self._clock(), restart_doubt=doubt)

    def _close_unwatched(self, job: Any, printer_name: str, why: str) -> None:
        """End a job whose ending nobody saw: completed in the queue, its
        outcome ``unknown`` (the person is asked how it went, the way every
        unwatched ending is)."""
        if getattr(job.status, "value", "") in ("starting", "paused"):
            self._queue.mark_printing(job.id)
        self._queue.mark_completed(job.id)
        self._event_bus.publish(
            EventType.JOB_COMPLETED, {"job_id": job.id, "printer_name": printer_name, "ending": "not_seen"},
            source="scheduler",
        )
        self._auto_record_outcome(job.id, printer_name, "unknown", error_msg=why, determined_by="inferred")

    def _started_on(self, job: Any, adapter: Any) -> bool:
        """Whether the plate record shows *job*'s part was put on this
        printer's plate by a start at or after the job's own."""
        from kiln import plate_state

        try:
            state = plate_state.read(adapter)
        except Exception:  # noqa: BLE001
            return False
        expected = (job.metadata or {}).get("started_file") or job.file_name
        is_fill = isinstance((job.metadata or {}).get("fill"), dict)
        if not is_fill and not any(_same_file(j.file, expected) for j in state.jobs):
            return False
        if is_fill and not state.occupied:
            return False
        started = getattr(job, "started_at", None)
        if not started or not state.since:
            return True
        try:
            written = datetime.fromisoformat(state.since)
            if written.tzinfo is None:
                written = written.replace(tzinfo=timezone.utc)
            return written.timestamp() >= float(started) - _START_RECORD_SLACK_S
        except (TypeError, ValueError):
            return True

    def _where_it_ran(self, job: Any) -> str | None:
        """The printer whose plate record holds *job*'s part from a start at
        or after the job's own, for a job that named no printer and predates
        Kiln recording where a start went.  ``None`` when no plate says."""
        try:
            names = list(self._registry.list_names())
        except Exception:  # noqa: BLE001
            return None
        for name in names:
            try:
                if self._started_on(job, self._registry.get(name)):
                    return name
            except Exception:  # noqa: BLE001
                continue
        return None

    # ------------------------------------------------------------------
    # Waiting: the plate, and the turn (see the module comment above)
    # ------------------------------------------------------------------

    def _startable_job(
        self, printer_name: str, waits: dict[str, dict[str, Any]], plate_blocks: dict[str, dict[str, Any]],
    ) -> Any | None:
        """The first queued job *printer_name* may start as its plate stands,
        or ``None``.  Jobs the plate holds back are written into *waits*.

        A clear or unrecorded plate starts the first job in line, as every
        door does.  An occupied one starts only a file sliced to start
        beside exactly what is there; every other job keeps its place.
        """
        candidates = self._queue.queued_for(printer_name)
        if not candidates:
            return None
        from kiln import plate_state

        try:
            adapter = self._registry.get(printer_name)
            state = plate_state.read(adapter)
        except Exception:  # noqa: BLE001 -- the dispatch below says what is wrong with the printer
            return candidates[0]
        if not state.occupied:
            return candidates[0]
        version = (str(state.since or ""), plate_state.fingerprint(state))
        startable = None
        held: list[str] = []
        for job in candidates:
            code = self._plate_answer(job, printer_name, adapter, version)
            if code is None:
                startable = job
                break
            held.append(job.id)
            wait = waits.setdefault(job.id, {"kind": WAIT_PLATE, "code": code, "printers": []})
            if wait.get("kind") == WAIT_PLATE and printer_name not in wait["printers"]:
                wait["printers"].append(printer_name)
                wait.setdefault("_state", state)
        if held:
            plate_blocks[printer_name] = {
                "state": state, "version": version, "jobs": held,
                "camera": bool(plate_state.knows_a_camera(adapter)),
            }
        return startable

    def _plate_answer(self, job: Any, printer_name: str, adapter: Any, version: tuple[str, str]) -> str | None:
        """``start_hold``'s code for *job* on this plate, or ``None`` when it may
        start.  Read once per plate: the answer turns only on the record and
        the file, and the file does not change while the job waits."""
        from kiln import plate_state

        key = (job.id, printer_name)
        cached = self._plate_answers.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        held = plate_state.start_hold(adapter, file_name=job.file_name)
        code = None if held is None else held[0]
        self._plate_answers[key] = (version, code)
        return code

    def _wait_turn(
        self, job: Any, printer_name: str, idle_printers: list[str], code: str, waits: dict[str, dict[str, Any]],
    ) -> None:
        """*job* was turned away because another printer holds the turn: put
        it back in line, untouched, and wait for that printer."""
        self._queue.requeue(job.id)
        idle = set(idle_printers)
        try:
            registered = list(self._registry.list_names())
        except Exception:  # noqa: BLE001
            registered = []
        with self._lock:
            running = set(self._active_jobs.values())
        busy = frozenset(n for n in registered if n not in idle and n != printer_name) | (running - {printer_name})
        sentence = turn_wait_sentence(job.file_name, printer_name, sorted(busy), label=_job_label(job))
        note = {
            "kind": WAIT_TURN, "code": code, "printers": [printer_name], "waiting_for": sorted(busy),
            "sentence": sentence, "upgrade": TURN_UPGRADE,
        }
        announce = self._turn_wait is None
        self._turn_wait = TurnWait(busy=busy, started=self._clock(), note=note)
        waits[job.id] = note
        if announce:
            self._event_bus.publish(
                EventType.JOB_WAITING,
                {
                    "job_id": job.id, "file_name": job.file_name, "printer_name": printer_name,
                    "kind": WAIT_TURN, "waiting_for": sorted(busy), "message": f"{sentence} {TURN_UPGRADE}",
                },
                source="scheduler",
            )
        logger.info("Job %s waits its turn for %s (%s)", job.id, ", ".join(sorted(busy)) or "another printer", code)

    def _turn_over(self, idle_printers: list[str]) -> bool:
        """Whether the turn wait has ended: a printer it waited for is idle,
        none of them is registered any more, or it is time to ask the gate
        again (:data:`TURN_RECHECK_S`)."""
        wait = self._turn_wait
        if wait is None:
            return True
        over = any(name in idle_printers for name in wait.busy)
        if not over and wait.busy:
            try:
                over = not (wait.busy & set(self._registry.list_names()))
            except Exception:  # noqa: BLE001
                over = False
        if not over:
            over = self._clock() - wait.started >= TURN_RECHECK_S
        if over:
            self._turn_wait = None
        return over

    def _settle_waits(
        self, waits: dict[str, dict[str, Any]], plate_blocks: dict[str, dict[str, Any]],
        turn_ready: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Write each queued job's wait on the job, announce each plate that
        is holding the line once, and forget what no queued job needs.

        A job some printer could start now but for the turn (*turn_ready*)
        waits for the turn, even where another printer's plate also holds a
        part: clearing that plate would not start it, so saying so would be
        a promise the queue does not keep."""
        queued = self._queue.queued_for(None)
        queued_ids = {job.id for job in queued}
        for printer_name, block in plate_blocks.items():
            still = [job for job in queued if job.id in block["jobs"]]
            if not still or self._plate_announced.get(printer_name) == block["version"]:
                continue
            self._plate_announced[printer_name] = block["version"]
            first = still[0]
            code = waits.get(first.id, {}).get("code", "")
            self._event_bus.publish(
                EventType.JOB_WAITING,
                {
                    "job_id": first.id, "file_name": first.file_name, "printer_name": printer_name,
                    "kind": WAIT_PLATE, "waiting_jobs": [job.id for job in still],
                    "message": plate_wait_sentence(
                        first.file_name, [printer_name], block["state"], code=code, camera=block["camera"],
                        label=_job_label(first),
                    ),
                },
                source="scheduler",
            )
        turn = self._turn_wait
        for job in queued:
            wait = waits.get(job.id)
            if turn is not None and (job.id in turn_ready or (
                wait is None and (job.printer_name is None or job.printer_name not in turn.busy)
            )):
                wait = {
                    **turn.note,
                    "sentence": turn_wait_sentence(
                        job.file_name, job.printer_name, sorted(turn.busy), label=_job_label(job),
                    ),
                }
            self._queue.note_waiting(job.id, self._written_wait(job, wait, plate_blocks))
        for key in [k for k in self._plate_answers if k[0] not in queued_ids]:
            del self._plate_answers[key]

    @staticmethod
    def _written_wait(job: Any, wait: dict[str, Any] | None, plate_blocks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """The note a job carries: its kind, the printers, the sentence, and
        when the wait began -- kept from the note already on the job while
        it is the same wait, so the clock does not restart every pass."""
        if wait is None:
            return None
        printers = list(wait.get("printers") or [])
        note = {k: v for k, v in wait.items() if not k.startswith("_")}
        if note.get("kind") == WAIT_PLATE:
            camera = any(plate_blocks.get(p, {}).get("camera") for p in printers)
            note["sentence"] = plate_wait_sentence(
                job.file_name, printers, wait.get("_state"), code=str(wait.get("code") or ""), camera=camera,
                label=_job_label(job),
            )
        before = (job.metadata or {}).get("waiting")
        same = isinstance(before, dict) and before.get("kind") == note.get("kind") and before.get("printers") == printers
        note["since"] = before.get("since") if same and before.get("since") else (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        return note

    def waiting_for(self, job_id: str) -> dict[str, Any] | None:
        """Why a queued job has not started, in a sentence, or ``None`` for a
        job that is not waiting in line.  See :func:`describe_wait`."""
        try:
            job = self._queue.get_job(job_id)
        except Exception:  # noqa: BLE001
            return None
        with self._lock:
            running = {printer: jid for jid, printer in self._active_jobs.items()}
        try:
            registered: frozenset[str] | None = frozenset(self._registry.list_names())
        except Exception:  # noqa: BLE001
            registered = None
        return describe_wait(
            job, self._queue, running=running, idle=self._last_idle,
            retry_at=self._retry_not_before.get(job_id), registered=registered,
        )

    def tick(self) -> dict[str, Any]:
        """Run one scheduling cycle.  Can be called manually for testing.

        Returns a dict summarising what happened:
            dispatched: list of {job_id, printer_name, file_name}
            completed: list of job_ids detected as complete
            failed: list of {job_id, error}
            checked: number of active jobs checked
        """
        dispatched: list[dict[str, Any]] = []
        completed: list[str] = []
        failed: list[dict[str, str]] = []
        checked = 0

        # Phase 0: jobs a previous run of Kiln was starting or printing when
        # it stopped.  Settled from the printer before anything else, so a
        # print that finished while Kiln was away is never started again.
        self._settle_unfinished(failed)

        # Phase 1: Check active jobs for completion / failure
        with self._lock:
            active_snapshot = dict(self._active_jobs)

        for job_id, printer_name in active_snapshot.items():
            if job_id in self._preparing:
                continue    # still being laid out and sliced; its printer has not been asked yet
            checked += 1
            try:
                estop_reason = self._emergency_block_reason(printer_name)
                if estop_reason:
                    error_msg = f"Job stopped due to safety latch on {printer_name}: {estop_reason}"
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    self._fail_permanently(job_id, error_msg, failed, printer_name=printer_name)
                    continue

                adapter = self._registry.get(printer_name)
                # The scheduler watching jobs IT dispatched, to record how
                # they ended.  Exempt for a sharper reason than the other
                # internal reads: a refusal here would not surface as an
                # error, it would quietly stop outcomes being recorded, and
                # a learning loop that goes dark reports nothing at all.
                from kiln.printers.engagement import internal_read

                with internal_read():
                    state = adapter.get_state()
                    job_progress = adapter.get_job()

                # `is_occupied` so a reading that goes STALE mid-print still
                # counts as "we saw this printing" — losing that would make a
                # later idle read look like a print that never started, and
                # the outcome would be banked as "unknown" instead of watched.
                if getattr(state, "is_occupied", False) is True:
                    self._seen_printing.add(job_id)

                now = self._clock()
                self._observe(job_id, printer_name, adapter, state, job_progress, now)

                # Printer returned to idle -- the job has ENDED.  How it
                # ended is only as certain as what this loop actually saw:
                #   - a job the queue itself cancelled ends as "cancelled";
                #   - a job this loop WATCHED printing that is now idle with
                #     no error ended cleanly -> "success" (observed);
                #   - a job never seen printing may have failed to start —
                #     claiming success would be a guess, and a guessed
                #     success poisons the learning data that proven-settings
                #     and printer rankings are built from.  It ends as
                #     "unknown" (inferred) and the user gets asked.
                # ``confirmed_state`` on the two branches that END a job,
                # which is exactly as strict about staleness as the bare
                # `state` it replaced: a reading that has gone STALE is not
                # evidence the print finished or failed, and acting on one
                # would close a job that is still running.  Those cases fall
                # through to the next poll, which is what a printer going
                # quiet for a moment should cost.
                #
                # What it does see through is a FAULT.  A latched code takes
                # the headline off a reading that is otherwise current, and
                # on a faulted-but-idle machine the bare `state` matched
                # neither this branch nor the error one below -- leaving the
                # job with no outcome recorded at all.
                if state.confirmed_state == PrinterStatus.IDLE:
                    pre_idle_job = self._queue.get_job(job_id)
                    queue_cancelled = bool(
                        pre_idle_job is not None
                        and getattr(pre_idle_job.status, "value", str(pre_idle_job.status)).lower()
                        in ("cancelled", "canceled")
                    )
                    # CANCELLED is terminal in the queue's state machine —
                    # completing it would raise, and the cancel path
                    # already published its own event when it happened.
                    if not queue_cancelled:
                        if pre_idle_job is not None and pre_idle_job.status == JobStatus.PAUSED:
                            # Paused in the queue, idle at the printer (cancelled at
                            # its screen, or ended while the queue still read
                            # paused): the queue's states end only from PRINTING.
                            self._queue.mark_printing(job_id)
                        self._queue.mark_completed(job_id)
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    if not queue_cancelled:
                        self._event_bus.publish(
                            EventType.JOB_COMPLETED,
                            {"job_id": job_id, "printer_name": printer_name},
                            source="scheduler",
                        )
                    if queue_cancelled:
                        self._auto_record_outcome(
                            job_id, printer_name, "cancelled",
                            determined_by="observed",
                        )
                    elif job_id in self._seen_printing:
                        # IDLE is NOT testimony.  Every adapter folds a clean
                        # finish, a cancel and an untouched printer into that
                        # one value, so "watched printing, now idle" cannot
                        # tell a completed print from one stopped at the
                        # machine's own touchscreen.  Reading it as success
                        # and federating it published a cancel to the
                        # community pool as proof the settings worked.
                        #
                        # PrinterState.last_job_result is the field that
                        # carries what IDLE threw away.  When the machine
                        # NAMES its ending, that is testimony and is taken at
                        # its word.  When it names nothing — OctoPrint's
                        # flags, RRF's object model — the print most likely
                        # did finish, so it is still recorded as success for
                        # the user's own history, but it is an INFERENCE and
                        # does not federate.  Contributing is a claim about
                        # the model; only the machine gets to make it.
                        ended = getattr(state, "last_job_result", None)
                        named = getattr(ended, "value", None)
                        if named == "cancelled":
                            self._auto_record_outcome(
                                job_id, printer_name, "cancelled",
                                determined_by="observed",
                            )
                        elif named == "failed":
                            self._auto_record_outcome(
                                job_id, printer_name, "failed",
                                determined_by="observed",
                                contribute=True,
                            )
                        elif named is None and (doubt := self._ending_doubt(job_id)):
                            # Idle with nothing named, and the reading
                            # before this one was a stall or a silence:
                            # the ending was not watched.  A stalled print
                            # that was power-cycled or stopped at the
                            # screen must not be banked as a success.
                            self._auto_record_outcome(
                                job_id, printer_name, "unknown",
                                error_msg=(
                                    f"The printer went idle but {doubt}, so "
                                    f"Kiln did not see how this print ended. "
                                    f"Outcome needs the user's answer."
                                ),
                                determined_by="inferred",
                            )
                        else:
                            self._auto_record_outcome(
                                job_id, printer_name, "success",
                                determined_by="observed",
                                contribute=(named == "completed"),
                            )
                    else:
                        self._auto_record_outcome(
                            job_id, printer_name, "unknown",
                            error_msg=(
                                "Printer went idle before the scheduler ever "
                                "saw this job printing — it may not have "
                                "started. Outcome needs the user's answer."
                            ),
                            determined_by="inferred",
                        )
                    self._forget_watch(job_id)
                    completed.append(job_id)

                # ``confirmed_state``: it looks through a FAULT headline, so a
                # fault raised while the machine kept working still matches here,
                # and it is as strict about staleness as the bare state word was:
                # an expired reading is not evidence that anything ended.
                elif state.confirmed_state == PrinterStatus.ERROR:
                    error_msg = f"Printer {printer_name} entered error state"
                    # A machine-reported error is a print verdict only for a
                    # job this loop actually SAW printing — an error on a
                    # never-seen job may predate the print (it may never have
                    # started), and blaming the model for it would be a
                    # guess.  Captured before _requeue_or_fail, which
                    # discards the seen-printing mark on both branches.
                    machine_reported = job_id in self._seen_printing
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    self._requeue_or_fail(
                        job_id, error_msg, failed, printer_name=printer_name,
                        machine_reported=machine_reported,
                    )

                # ...and `effective_state` on the branch that only WATCHES
                # one.  The last thing the printer said is still the best
                # answer to "is this printing", so a stale reading keeps the
                # STARTING promotion instead of silently skipping it (the
                # watch above already counted it as silence).
                elif state.effective_state == PrinterStatus.PRINTING:
                    # Promote STARTING -> PRINTING when the printer confirms
                    try:
                        job = self._queue.get_job(job_id)
                        if job.status == JobStatus.STARTING:
                            self._queue.mark_printing(job_id)
                    except Exception as exc:
                        logger.debug("Failed to promote job %s to PRINTING: %s", job_id, exc)

                    # Publish progress event
                    if job_progress.completion is not None:
                        self._event_bus.publish(
                            EventType.PRINT_PROGRESS,
                            {
                                "job_id": job_id,
                                "printer_name": printer_name,
                                "completion": job_progress.completion,
                                "file_name": job_progress.file_name,
                            },
                            source="scheduler",
                        )

            except PrinterNotFoundError:
                error_msg = f"Printer {printer_name} no longer registered"
                with self._lock:
                    self._active_jobs.pop(job_id, None)
                self._requeue_or_fail(job_id, error_msg, failed, printer_name=printer_name)
            except Exception as exc:
                # A read that raises is a printer Kiln cannot see.  Nothing
                # is decided on it; it counts as silence, which is announced
                # once it outlasts the threshold.
                logger.warning("Error checking job %s on %s: %s", job_id, printer_name, exc)
                self._observe_silence(job_id, printer_name, self._clock(), cause="read_failed")

        # Phase 2: Dispatch queued jobs to idle printers -- only when a job is
        # waiting.  Reading every printer is not free: a Bambu counts each read
        # as use, so a scheduler polling an empty queue every few seconds kept
        # every server's connection alive and the printer's rationed LAN slots
        # full (measured 2026-09-15: four idle servers, all four slots, 10-14
        # hours).  An empty queue has nothing a free printer could take.
        has_pending = self._queue.pending_count() != 0
        if not has_pending:
            idle_printers: list[str] = []
        else:
            idle_printers = self._registry.get_idle_printers()

        # Filter out printers that already have active jobs
        with self._lock:
            busy_printers = set(self._active_jobs.values())
        available = [p for p in idle_printers if p not in busy_printers]
        if has_pending:
            self._last_idle = frozenset(idle_printers)

        # One printer at a time: while another printer's print holds the
        # turn, nothing starts on a second machine -- every such start would
        # be turned away the same way -- so no printer is asked anything.
        # The plates are still read (the record is on this computer, not the
        # printer), so a job that also waits for its plate says so.
        turn_held = self._turn_wait is not None and not self._turn_over(idle_printers)

        # Why each job that could not start is waiting, gathered as the
        # printers are walked and settled once at the end, so a job that
        # waits on one printer and starts on another this same pass is never
        # announced as waiting.
        waits: dict[str, dict[str, Any]] = {}
        plate_blocks: dict[str, dict[str, Any]] = {}

        # Smart routing: rank printers by historical success rate for the
        # next queued unassigned job.  This ensures the best-performing
        # printer for the job's file/material gets first dispatch priority.
        if self._persistence and available:
            queued = [j for j in self._queue.list_jobs(status=JobStatus.QUEUED) if j.printer_name is None]
            if queued:
                available = self._rank_printers(available, queued[0])

        # Jobs a printer could start right now but for the turn: their wait
        # is the turn, whatever another printer's plate says.
        turn_ready: set[str] = set()
        for printer_name in available:
            next_job = self._startable_job(printer_name, waits, plate_blocks)
            if next_job is None:
                continue
            if turn_held:
                turn_ready.add(next_job.id)
                continue

            estop_reason = self._emergency_block_reason(printer_name)
            if estop_reason:
                logger.warning(
                    "Dispatch blocked for %s (job %s): %s",
                    printer_name,
                    next_job.id,
                    estop_reason,
                )
                self._event_bus.publish(
                    EventType.SAFETY_ESCALATED,
                    {
                        "printer_name": printer_name,
                        "job_id": next_job.id,
                        "reason": "emergency_latched",
                        "message": estop_reason,
                    },
                    source="scheduler",
                )
                continue

            # Respect exponential backoff for retried jobs
            not_before = self._retry_not_before.get(next_job.id)
            if not_before is not None and time.time() < not_before:
                continue

            # Clear the backoff gate once we're past it
            self._retry_not_before.pop(next_job.id, None)

            # Try to dispatch (acquire per-printer lock to prevent concurrent ops)
            printer_mutex = self._registry.printer_lock(printer_name)
            if not printer_mutex.acquire(blocking=False):
                logger.debug(
                    "Printer %s locked by another operation, skipping dispatch",
                    printer_name,
                )
                continue
            try:
                adapter = self._registry.get(printer_name)
                self._queue.mark_starting(next_job.id, printer_name=printer_name)

                # An unconfirmed start must not be read as a failure here:
                # requeuing a job the printer actually took dispatches the
                # same file at a machine that is already running it.
                # A queued job was cleared when it entered the queue (every
                # queue door gates on a preview token and stores the
                # record on the job); the scheduler re-grants that record
                # here, in its own thread, so the adapter template can see
                # it.  A job with no record still dispatches as "queued" —
                # the queue's doors are the gate, pinned by test — but says
                # so, rather than borrowing a door it never passed.
                # The yes the job carries was for a scope — one printer,
                # a list, the fleet — and may rest on a standing window.
                # A printer outside the scope, or a window since revoked
                # or run out, gets a reason and no print: this is not a
                # dispatch error to retry, it is a no.
                signoff_record = (next_job.metadata or {}).get("preview_signoff") or {}
                refusal = record_refusal(signoff_record, printer_name)
                if refusal:
                    # No printer name on purpose: nothing reached a machine,
                    # so there is no print outcome to record — only a job
                    # that was not allowed to start, and says why.
                    self._fail_permanently(next_job.id, refusal, failed, printer_name=None)
                    continue
                if isinstance((next_job.metadata or {}).get("fill"), dict):
                    # The rest of a fill: laid out, sliced and started for
                    # the plate as it is now, off this thread.  Asked first
                    # whether it is this printer's turn, so no slice is made
                    # only to be turned away.
                    turn = self._turn_refusal(adapter)
                    if turn is not None:
                        self._wait_turn(next_job, printer_name, idle_printers, turn, waits)
                        break
                    self._start_fill_job(next_job, printer_name)
                    continue
                grant_from_record(
                    signoff_record,
                    tool="scheduler", file_name=next_job.file_name, printer_name=printer_name,
                )
                sent_at = time.monotonic()
                # A queued job that came from a kept design carries its ids in
                # the job's metadata; the start stamps them on the print row.
                _ids = {
                    k: str(next_job.metadata[k]).strip()
                    for k in ("version_id", "design_id", "branch_id")
                    if next_job.metadata and isinstance(next_job.metadata.get(k), str)
                    and next_job.metadata[k].strip()
                }
                result = adapter.start_print(next_job.file_name, **_ids)
                if getattr(result, "code", "") in _TURN_CODES:
                    # Another printer's turn.  Nothing was sent; the job
                    # keeps its place, and no other printer is tried this
                    # pass -- each would be turned away the same way.
                    self._wait_turn(next_job, printer_name, idle_printers, str(result.code), waits)
                    break
                verdict = resolve_print_start(
                    adapter, result, sent_at=sent_at,
                    file_name=next_job.file_name,
                )
                if verdict.ok:
                    self._queue.note_waiting(next_job.id, None)
                    self._queue.mark_printing(next_job.id)
                    with self._lock:
                        self._active_jobs[next_job.id] = printer_name
                    self._watch[next_job.id] = JobWatch(printer_name, last_contact=self._clock())
                    self._event_bus.publish(
                        EventType.JOB_STARTED,
                        {
                            "job_id": next_job.id,
                            "printer_name": printer_name,
                            "file_name": next_job.file_name,
                        },
                        source="scheduler",
                    )
                    dispatched.append(
                        {
                            "job_id": next_job.id,
                            "printer_name": printer_name,
                            "file_name": next_job.file_name,
                        }
                    )
                else:
                    # The verdict always carries a sentence, including when
                    # the adapter returned none — no local fallback needed.
                    self._requeue_or_fail(next_job.id, verdict.message, failed)

            except PrinterEngagementError as exc:
                code = str((exc.verdict or {}).get("code") or "")
                if code in _TURN_CODES:
                    self._wait_turn(next_job, printer_name, idle_printers, code, waits)
                    break
                self._requeue_or_fail(next_job.id, f"Failed to start print on {printer_name}: {exc}", failed)
            except PrinterError as exc:
                error_msg = f"Failed to start print on {printer_name}: {exc}"
                self._requeue_or_fail(next_job.id, error_msg, failed)
            except Exception as exc:
                logger.exception("Unexpected error dispatching job %s", next_job.id)
                self._requeue_or_fail(next_job.id, str(exc), failed)
            finally:
                printer_mutex.release()

        if has_pending:
            self._settle_waits(waits, plate_blocks, turn_ready)

        return {
            "dispatched": dispatched,
            "completed": completed,
            "failed": failed,
            "checked": checked,
        }

    def _run_loop(self) -> None:
        """Background polling loop."""
        while self._running:
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            if self._stop_event.wait(self._poll_interval):
                break
