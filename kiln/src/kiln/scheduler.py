"""Job scheduler — dispatches queued jobs to available printers."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiln.cli_command import kiln_command
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
# ---------------------------------------------------------------------------


@dataclass
class JobWatch:
    """What the scheduler currently knows about one job it is watching."""

    printer_name: str
    last_contact: float
    last_verdict: MotionVerdict | None = None
    ending_doubt: str | None = None
    no_contact_announced: bool = False
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
# ---------------------------------------------------------------------------

# AFTER A RESTART

TURN_RECHECK_S = 300.0

#: The start-gate codes that mean "not yet, it is another printer's turn".
_TURN_CODES = frozenset({"TIER_CONCURRENT_PRINT_LIMIT", "TIER_SINGLE_PRINTER_LIMIT"})

WAIT_PLATE = "plate"
WAIT_TURN = "turn"

LOOK_FIRST_CODE = "MOVED_COPIES_LOOK_FIRST"

FAILED_PLATE_RETRIES = 2
WAIT_SWITCH = "switch"
WAIT_RECOVERING = "recovering"

_START_RECORD_SLACK_S = 120.0


@dataclass
class TurnWait:
    """The queue is waiting for another printer's print, one at a time."""

    busy: frozenset[str]
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
    """Kiln's own words for a job."""

    many: bool = False


def _job_label(job: Any) -> str:
    """What a person calls a queued job: its file, or for the rest of a fill ``the other 14 copies of bracket``."""
    fill = (getattr(job, "metadata", None) or {}).get("fill")
    name = _file_label(getattr(job, "file_name", "") or "")
    if isinstance(fill, dict) and fill.get("copies"):
        n = int(fill["copies"])
        label = _Label(f"the other {n} cop{'y' if n == 1 else 'ies'} of {name}")
        label.many = n != 1
        return label
    return name


def _same_file(a: str | None, b: str | None) -> bool:
    """Whether two names a printer and the queue use are one file."""
    import os

    from kiln.plate_state import pretty_job_name

    if not a or not b:
        return False
    if os.path.basename(str(a)) == os.path.basename(str(b)):
        return True
    return bool(pretty_job_name(str(a))) and pretty_job_name(str(a)) == pretty_job_name(str(b))


def _cap(text: str) -> str:
    """Kiln's own words for a job (``the other 14 copies of ...``) opening a sentence, capitalised."""
    return text[:1].upper() + text[1:] if isinstance(text, _Label) else text


def _many(label: str) -> bool:
    """Whether a job's label names several things (the rest of a fill)."""
    return isinstance(label, _Label) and label.many


def _verb(label: str, one: str, several: str) -> str:
    return several if _many(label) else one


def plate_wait_sentence(
    file_name: str, printers: list[str], state: Any, *, code: str, camera: bool, label: str | None = None,
    beside: bool = False, moved: dict[str, Any] | None = None,
) -> str:
    """What a job waiting for a plate says, in one breath: what is there, that this file is next, and the one thing that starts it."""
    name = label or _file_label(file_name)
    from kiln.plate_state import PLATE_CHANGED_CODE

    if code == LOOK_FIRST_CODE:
        return _look_first_sentence(name, printers[0] if printers else "the printer", camera, moved or {})

    if code == PLATE_CHANGED_CODE and beside:
        where = printers[0] if printers else "the printer"
        return (
            f"{_cap(name)} {_verb(name, 'was', 'were')} planned beside what was on {where}'s plate when you saw the "
            f"plan, and the plate has changed since, so {_verb(name, 'it waits', 'they wait')} rather than start "
            f"there. Clear the plate and say so (tell your assistant the plate is clear, or run "
            f"`{kiln_command('plate clear')}`), "
            f"and Kiln prints {_verb(name, 'it', 'them')} on the empty plate."
        )
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
            f"`{kiln_command('plate clear')}`), and Kiln "
            f"{_verb(name, 'starts ' + name, 'prints the next plate of them')} there.{look}"
        )
    where = printers[0] if printers else "the printer"
    holds = state.holds_sentence() if state is not None else "The last print is still on the plate."
    return (
        f"{_cap(name)} {_verb(name, 'is', 'are')} next on {where}. {holds} Take it off and say so (tell your assistant "
        f"the plate is clear, or run "
        f"`{kiln_command('plate clear')}`), and Kiln "
        f"{_verb(name, 'starts ' + name, 'prints the next plate of them')} on its own.{look}"
    )


def _look_first_sentence(name: str, where: str, camera: bool, moved: dict[str, Any]) -> str:
    """What moved copies wait for: the failed printer's picture looked at first, then this plate seen clear (or the person's word)."""
    origin = str(moved.get("from") or "another printer")
    picture = moved.get("picture")
    first = (
        f" First look at the picture Kiln took of {origin}'s plate ({picture}): a part torn off the plate or "
        f"spaghetti means the part is the problem, so cancel these copies; a print laid into the air means "
        f"{origin} failed, not the part."
        if picture else
        f" Kiln has no picture of {origin}'s plate, so look at it yourself first: a part torn off the plate or "
        "spaghetti means the part is the problem, so cancel these copies."
    )
    then = (
        f" Then look at {where}'s plate through its camera (look_at_plate) and say what is there, or say yourself "
        f"that it is clear; Kiln starts {_verb(name, 'it', 'them')} once it has been seen clear."
        if camera else
        f" {where} has no camera Kiln can read, so Kiln starts {_verb(name, 'it', 'them')} once you say its plate "
        f"is clear (tell your assistant, or run `{kiln_command('plate clear')}`)."
    )
    return (
        f"{_cap(name)} {_verb(name, 'was', 'were')} moved to {where} after a plate failed on {origin}, and "
        f"{_verb(name, 'waits', 'wait')} for {where}'s plate to be seen before starting, because nobody was "
        f"there when Kiln moved {_verb(name, 'it', 'them')}.{first}{then}"
    )


def _moved(job: Any) -> dict[str, Any] | None:
    """A failed plate's copies moved to this job's printer: where from, and the picture of the plate that failed; ``None`` for any other job."""
    fill = ((getattr(job, "metadata", None) or {}).get("fill") or {})
    if not fill.get("look_first"):
        return None
    return {"from": fill.get("moved_from"), "picture": fill.get("failed_picture")}


def _picture_words(printer_name: Any, picture: str | None) -> str:
    """`` Kiln took a picture of X's plate: <path>.`` or nothing."""
    return f" Kiln took a picture of {printer_name}'s plate: {picture}." if picture else ""


def _planned_beside(job: Any) -> bool:
    """Whether *job* is a run's copies planned beside what was on its plate."""
    return bool(((getattr(job, "metadata", None) or {}).get("fill") or {}).get("beside_plate"))


def _miss_words(answer: Any) -> str:
    """What a served answer that is not a decision says, in one sentence."""
    if not isinstance(answer, dict):
        return "Kiln could not ask its servers where these copies should go."
    error = answer.get("error")
    text = error.get("message") if isinstance(error, dict) else error
    if not text and answer.get("required_tier"):
        text = f"Moving a failed plate's copies to another printer is part of Kiln {str(answer['required_tier']).title()}."
    return str(text or answer.get("message") or "Kiln's servers gave no answer about where these copies should go.").strip()


def turn_wait_sentence(file_name: str, printer_name: str | None, busy: list[str], *, label: str | None = None) -> str:
    """What a job waiting its turn says: whose print it waits for, and that it keeps its place."""
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

QUEUE_ON_ONE_PRINTER_CODE = "QUEUE_ON_ONE_PRINTER"


def switch_wait_sentence(file_name: str, printer_name: str, on: str, *, label: str | None = None) -> str:
    """What a job for another printer says while the queue stays on *on*."""
    name = label or _file_label(file_name)
    return (
        f"Kiln's queue works on one printer at a time on this plan, and it is on {on}. {_cap(name)} "
        f"{_verb(name, 'is', 'are')} for {printer_name}, so {_verb(name, 'it waits', 'they wait')} until you move "
        f"Kiln there: tell your assistant you are done with {on} (hand it back), or start a print on "
        f"{printer_name} yourself."
    )


@dataclass(frozen=True)
class QueueOn:
    """The printer the queue is on below the fleet tier: every registered name for that machine, and the name a person knows it by."""

    names: frozenset[str]
    label: str


def describe_wait(
    job: Any, queue: Any, *, running: dict[str, str] | None = None, idle: frozenset[str] | None = None,
    retry_at: float | None = None, registered: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Why a QUEUED job has not started yet."""
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
    """Background scheduler that dispatches print jobs to printers."""

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
        self._active_jobs: dict[str, str] = {}
        self._retry_counts: dict[str, int] = {}
        # Jobs this scheduler has actually SEEN printing
        self._seen_printing: set[str] = set()
        self._retry_not_before: dict[str, float] = {}
        self._watch: dict[str, JobWatch] = {}
        self._contact_cause: dict[str, str] = {}
        # Monotonic clock, overridable so tests can drive hours in a second.
        self._clock = time.monotonic
        self._lock = threading.Lock()
        self._plate_announced: dict[str, tuple[str, str]] = {}
        self._plate_answers: dict[tuple[str, str], tuple[tuple[str, str], Any]] = {}
        # One printer at a time: the wait for another printer's print.
        self._turn_wait: TurnWait | None = None
        self._last_ending: dict[str, str] = {}
        # Jobs already announced as waiting for the queue to move to them.
        self._switch_announced: set[str] = set()
        # The printers last read as idle, for a reader asking why a job waits.
        self._last_idle: frozenset[str] = frozenset()
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
        """Stop the scheduler gracefully."""
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
        """Try to re-queue a failed job if retries remain."""
        count = self._retry_counts.get(job_id, 0)
        if count < self._max_retries:
            self._retry_counts[job_id] = count + 1
            delay = self._retry_backoff_base * (2**count)
            self._retry_not_before[job_id] = time.time() + delay
            # Reset the job back to QUEUED so a future tick can redispatch it
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
        """Mark *job_id* FAILED for good and tell everyone who listens."""
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
        """What Kiln currently knows about a job it is watching, for the queue tools -- or ``None`` for a job it is not watching."""
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
        """Every watched job that currently needs a person: stalled or out of contact."""
        alerts = []
        for job_id in list(self._watch):
            note = self.watch_note(job_id)
            if note and note["state"] in ("stalled", "no_contact"):
                alerts.append({"job_id": job_id, **note})
        return alerts

    def _observe(
        self, job_id: str, printer_name: str, adapter: Any, state: Any, job: Any, now: float,
    ) -> None:
        """Record one trustworthy-or-not reading of a watched job."""
        watch = self._watch.get(job_id)
        if watch is None:
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

        # A trustworthy reading
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
        """A reading Kiln cannot vouch for."""
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
        """Reorder available printers by historical success rate for this job."""
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
        score = {r["printer_name"]: r["success_rate"] for r in rankings}
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
        """Best-effort auto-record a print outcome to the learning database."""
        self._last_ending[job_id] = outcome
        if not self._persistence:
            return
        try:
            # Try to get job metadata for richer outcome data
            job = self._queue.get_job(job_id)

            existing = self._persistence.get_print_outcome(job_id)
            if existing is not None and existing.get("outcome") not in ("pending", "unknown"):
                return  # decided already — don't overwrite
            if existing is None:
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
            # Federate the resolution
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
        """The one-printer-at-a-time code a start on *adapter* would meet now, or ``None``."""
        try:
            from kiln.printers.print_gate import _concurrent_fleet_verdict

            verdict = _concurrent_fleet_verdict(adapter)
        except Exception:  # noqa: BLE001 -- the start itself is the gate; this only saves a slice
            return None
        code = str((verdict or {}).get("code") or "")
        return code if code in _TURN_CODES else None

    def _start_fill_job(self, job: Any, printer_name: str) -> None:
        """Take *printer_name* for *job* and lay out, slice and start its plate off this thread (:meth:`_run_fill_job`)."""
        with self._lock:
            self._active_jobs[job.id] = printer_name
            self._preparing.add(job.id)
        self._queue.note_waiting(job.id, None)
        self._hold_queue(printer_name)
        logger.info("Job %s: laying out the next plate of copies on %s", job.id, printer_name)
        self._spawn(lambda: self._run_fill_job(job, printer_name))

    def _run_fill_job(self, job: Any, printer_name: str) -> None:
        """The print pipeline every fill runs."""
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
                still_wanted=lambda: self._cancelled_sentence(job.id),
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

    def _cancelled_sentence(self, job_id: str) -> str | None:
        """Why a plate being prepared must not reach the printer: its job was cancelled in the queue meanwhile."""
        try:
            if self._queue.get_job(job_id).status == JobStatus.CANCELLED:
                return "the job was cancelled while its plate was being prepared"
        except Exception:  # noqa: BLE001 -- a job the queue cannot find is not wanted
            return "the job is no longer in the queue"
        return None

    def _finish_fill_job(self, job: Any, printer_name: str, result: Any, error: BaseException | None) -> None:
        steps = {getattr(st, "name", ""): st for st in (getattr(result, "steps", None) or [])}
        start = steps.get("start_print")
        sliced = steps.get("slice")
        start_data = (getattr(start, "data", None) or {}) if start is not None else {}
        turn_code = next(
            (str((getattr(st, "data", None) or {}).get("code") or "") for st in steps.values()
             if not getattr(st, "success", True) and (getattr(st, "data", None) or {}).get("code") in _TURN_CODES),
            "",
        )
        block = ((getattr(sliced, "data", None) or {}).get("copies") or {}) if sliced is not None else {}
        placed = block.get("placed") if isinstance(block.get("placed"), int) else None
        with self._lock:
            self._preparing.discard(job.id)
        if result is not None and getattr(result, "success", False):
            started_file = start_data.get("file_name")
            if started_file:
                self._queue.update_metadata(job.id, {"started_file": started_file})
            try:
                cancelled = self._queue.get_job(job.id).status == JobStatus.CANCELLED
            except Exception:  # noqa: BLE001
                cancelled = False
            if cancelled:
                logger.warning(
                    "Job %s was cancelled while its plate was being prepared, and %s has started it; "
                    "watching it, queueing no more", job.id, printer_name,
                )
                self._watch[job.id] = JobWatch(printer_name, last_contact=self._clock())
                return
            self._queue.mark_printing(job.id)
            self._watch[job.id] = JobWatch(printer_name, last_contact=self._clock())
            self._event_bus.publish(
                EventType.JOB_STARTED,
                {"job_id": job.id, "printer_name": printer_name, "file_name": started_file or job.file_name,
                 "copies": placed},
                source="scheduler",
            )
            fill = dict((job.metadata or {}).get("fill") or {})
            wanted = int(fill.get("copies") or 0)
            if placed is not None and 0 < placed < wanted:
                self._queue_rest(job, wanted - placed)
            if placed is not None and placed > 0:
                self._queue.update_metadata(job.id, {"fill": {**fill, "copies": placed}})
            return
        with self._lock:
            self._active_jobs.pop(job.id, None)
        code = turn_code or str(start_data.get("code") or "")
        if code in _TURN_CODES:
            self._wait_turn(job, printer_name, sorted(self._last_idle), code, {})
            return
        try:
            if self._queue.get_job(job.id).status == JobStatus.CANCELLED:
                return
        except Exception:  # noqa: BLE001
            pass
        why = str(error) if error is not None else str(getattr(result, "message", "") or "the plate did not start")
        self._requeue_or_fail(job.id, f"The next plate of copies did not start on {printer_name}: {why}", [])

    def _settle_fill_plate(self, job_id: str) -> None:
        """A plate of copies the printer reported failed goes back in the run."""
        try:
            ending = self._last_ending.pop(job_id, None)
            job = self._queue.get_job(job_id)
            fill = (job.metadata or {}).get("fill") if job is not None else None
            if not isinstance(fill, dict) or ending is None:
                return
            self._queue.update_metadata(job_id, {"ended": ending})
            if ending != "failed":
                return
            copies = int(fill.get("copies") or 0)
            tries = int(fill.get("failed_retries") or 0)
            if copies < 1:
                return
            if self._kept_fleet_run(job):
                self._spawn(lambda: self._reroute_failed_plate(job, copies, tries))
                return
            self._retry_here(job, copies, tries)
        except Exception:  # noqa: BLE001 -- bookkeeping never stops the queue
            logger.debug("could not settle the plate of job %s", job_id, exc_info=True)

    def _kept_fleet_run(self, job: Any) -> bool:
        """Whether *job* is a plate of a fleet run this computer kept."""
        plan_id = (job.metadata or {}).get("split_plan_id")
        if not plan_id:
            return False
        try:
            from kiln.job_splitter import _fill_record

            return _fill_record(str(plan_id)) is not None
        except Exception:  # noqa: BLE001 -- a record Kiln cannot read is no record
            return False

    def _retry_here(
        self, job: Any, copies: int, tries: int, *, because: str | None = None, picture: str | None = None,
    ) -> None:
        """Today's floor for a failed plate: its copies queued again on the same printer, up to :data:`FAILED_PLATE_RETRIES` times in a row, then the job says so."""
        lead = f"{because} " if because else ""
        if tries >= FAILED_PLATE_RETRIES:
            self._queue.update_metadata(job.id, {"not_requeued": (
                f"{lead}This plate of {copies} failed on {job.printer_name} again, so Kiln did not queue its copies a "
                f"{tries + 2}{'rd' if tries + 2 == 3 else 'th'} time. Look at the printer and the part, then queue "
                f"them yourself.{_picture_words(job.printer_name, picture)}"
            )})
            return
        if because:
            self._queue.update_metadata(job.id, {"requeued_here": f"{because} The copies print again on {job.printer_name}."})
        self._queue_rest(job, copies, failed_retries=tries + 1)

    def _reroute_failed_plate(self, job: Any, copies: int, tries: int) -> None:
        """Where a fleet run's failed plate goes: the server's answer, acted on."""
        picture: str | None = None
        try:
            from kiln import _pro_fleet_fill_bridge as fleet_fill
            from kiln.job_splitter import failed_plate_report

            picture = self._picture_of(str(job.printer_name or ""))
            if picture:
                self._queue.update_metadata(job.id, {"failed_picture": picture})
            if tries >= FAILED_PLATE_RETRIES:
                self._retry_here(job, copies, tries, picture=picture)
                return
            report = failed_plate_report(job, registry=self._registry, queue=self._queue)
            if report is None:
                self._retry_here(job, copies, tries)
                return
            report["failed"].setdefault("cameras", {})[str(job.printer_name or "")] = bool(picture)
            answer = fleet_fill.ask(
                report["file_path"], copies, material=report["material"], candidates=report["candidates"],
                adapters=report["adapters"], times=report["times"], failed=report["failed"], asked_by_user=False,
            )
            verdict = answer.get("failed_plate") if isinstance(answer, dict) and answer.get("success") is True else None
            if not isinstance(verdict, dict) or verdict.get("decision") not in ("retry", "move", "stop"):
                self._retry_here(job, copies, tries, because=_miss_words(answer))
                return
            decision, target = str(verdict.get("decision")), verdict.get("printer")
            self._queue.update_metadata(job.id, {"failed_plate": {
                "decision": decision, "to": target if decision == "move" else None,
                "why": str(verdict.get("summary") or ""), "reasons": [str(w) for w in (verdict.get("why") or [])],
            }})
            if decision == "move" and isinstance(target, str) and target and target != job.printer_name:
                refusal = self._move_refusal(job, target)
                if refusal:
                    self._retry_here(job, copies, tries, because=f"Kiln would have moved these copies to {target}, but {refusal}")
                    return
                self._picture_of(target, hand_over=True)
                self._queue_rest(
                    job, copies, failed_retries=tries + 1, printer_name=target,
                    moved_from=job.printer_name, failed_picture=picture,
                )
                logger.info("Job %s: its %d copies moved from %s to %s", job.id, copies, job.printer_name, target)
            elif decision == "retry" or decision == "move":
                self._retry_here(job, copies, tries)
            else:
                said = str(verdict.get("summary") or "Kiln did not queue these copies again.")
                self._queue.update_metadata(job.id, {"not_requeued": said + _picture_words(job.printer_name, picture)})
        except Exception:  # noqa: BLE001 -- an answer that could not be had is the floor, never a lost plate
            logger.warning("Job %s: could not ask where its failed plate's copies go", job.id, exc_info=True)
            try:
                self._retry_here(job, copies, tries, because="Kiln could not ask its servers where these copies should go.")
            except Exception:  # noqa: BLE001
                logger.debug("could not queue the failed plate of job %s again", job.id, exc_info=True)

    def _move_refusal(self, job: Any, target: str) -> str | None:
        """Why a server answer naming *target* may NOT move this plate's copies there."""
        from kiln import print_signoff
        from kiln.job_splitter import run_printers

        plan_id = str((job.metadata or {}).get("split_plan_id") or "")
        if target not in run_printers(plan_id):
            return f"{target} is not one of this run's printers, so nothing the person said covers it."
        record = (job.metadata or {}).get("preview_signoff")
        if not print_signoff.names_printer(record, target):
            return f"the yes this job carries does not name {target}."
        return print_signoff.record_refusal(record, target)

    def _picture_of(self, printer_name: str, *, hand_over: bool = False) -> str | None:
        """A picture of *printer_name*'s plate through its camera, saved on this computer: its path, or ``None`` with no camera Kiln can read or no usable frame."""
        from kiln import plate_state

        try:
            adapter = self._registry.get(printer_name)
            found = plate_state.look(adapter)
            return plate_state.hand_over_frame(adapter, found) if hand_over else plate_state.save_frame(found)
        except Exception:  # noqa: BLE001 -- no picture is said as no picture, never a stopped queue
            logger.debug("no picture of %s's plate", printer_name, exc_info=True)
            return None

    def _queue_rest(
        self, job: Any, copies: int, *, failed_retries: int = 0, printer_name: str | None = None,
        beside_plate: str | None = None, moved_from: str | None = None, failed_picture: str | None = None,
    ) -> None:
        """Queue the copies this plate could not take as the plate after it, on the same terms: the same part, printer and yes, and the fleet plan it belongs to."""
        metadata = {k: v for k, v in (job.metadata or {}).items() if k in ("preview_signoff", "split_plan_id")}
        fill = {k: v for k, v in ((job.metadata or {}).get("fill") or {}).items()
                if k not in ("beside_plate", "failed_retries", "moved_from", "look_first", "failed_picture")}
        fill["copies"] = int(copies)
        if failed_retries:
            fill["failed_retries"] = int(failed_retries)
        if beside_plate:
            fill["beside_plate"] = beside_plate
        if moved_from:
            fill["moved_from"] = moved_from
            fill["look_first"] = True
            if failed_picture:
                fill["failed_picture"] = failed_picture
        where = printer_name or job.printer_name
        rest_id = self._queue.submit(
            file_name=job.file_name, printer_name=where, submitted_by="multi_copy_print",
            priority=getattr(job, "priority", 0), metadata={**metadata, "fill": fill},
        )
        self._event_bus.publish(
            EventType.JOB_QUEUED,
            {"job_id": rest_id, "file_name": job.file_name, "printer_name": where, "copies": int(copies)},
            source="scheduler",
        )
        logger.info("Job %s: %d copies did not fit; queued as job %s for the next plate", job.id, copies, rest_id)

    # ------------------------------------------------------------------
    # After a restart (see the module comment above)
    # ------------------------------------------------------------------

    def _settle_unfinished(self, failed: list[dict[str, str]]) -> None:
        """Settle every job left starting, printing or paused with nothing watching it."""
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
                # No printer named, none recorded, no part anywhere: nothing took it
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
            isinstance((job.metadata or {}).get("fill"), dict) and self._started_on(job, adapter)
        )
        if running in (PrinterStatus.PRINTING, PrinterStatus.PAUSED) and same_file:
            self._adopt(job, printer_name, doubt=None)
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
        """End a job whose ending nobody saw: completed in the queue, its outcome ``unknown`` (the person is asked how it went, the way every unwatched ending is)."""
        if getattr(job.status, "value", "") in ("starting", "paused"):
            self._queue.mark_printing(job.id)
        self._queue.mark_completed(job.id)
        self._event_bus.publish(
            EventType.JOB_COMPLETED, {"job_id": job.id, "printer_name": printer_name, "ending": "not_seen"},
            source="scheduler",
        )
        self._auto_record_outcome(job.id, printer_name, "unknown", error_msg=why, determined_by="inferred")

    def _started_on(self, job: Any, adapter: Any) -> bool:
        """Whether the plate record shows *job*'s part was put on this printer's plate by a start at or after the job's own."""
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
        """The printer whose plate record holds *job*'s part from a start at or after the job's own."""
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
        """The first queued job *printer_name* may start as its plate stands, or ``None``."""
        candidates = self._queue.queued_for(printer_name)
        if not candidates:
            return None
        from kiln import plate_state

        try:
            adapter = self._registry.get(printer_name)
            state = plate_state.read(adapter)
        except Exception:  # noqa: BLE001 -- the dispatch below says what is wrong with the printer
            if any(_moved(job) for job in candidates):
                rest = [job for job in candidates if not _moved(job)]
                return rest[0] if rest else None
            return candidates[0]
        if not state.occupied and not any(_moved(job) for job in candidates):
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
        """``start_hold``'s code for *job* on this plate, or ``None`` when it may start."""
        from kiln import plate_state

        if _moved(job) is not None:
            state = plate_state.read(adapter)
            if not (state.fresh_look() or state.fresh_say_so()):
                return LOOK_FIRST_CODE
        key = (job.id, printer_name)
        cached = self._plate_answers.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        beside = ((job.metadata or {}).get("fill") or {}).get("beside_plate")
        if isinstance(beside, str) and beside:
            code = None if version[1] == beside else plate_state.PLATE_CHANGED_CODE
            self._plate_answers[key] = (version, code)
            return code
        held = plate_state.start_hold(adapter, file_name=job.file_name)
        code = None if held is None else held[0]
        self._plate_answers[key] = (version, code)
        return code

    def _queue_on(self, has_pending: bool) -> QueueOn | None:
        """The printer the queue is on, below the fleet tier."""
        from kiln.printers import engagement

        try:
            entry = engagement.queue_printer()
            if entry is None:
                return None
            with self._lock:
                busy = bool(self._active_jobs)
            if not has_pending and not busy:
                engagement.release_queue()
                return None
            if engagement.multi_machine_tier():
                return None
            names = frozenset(
                name for name in self._registry.list_names()
                if engagement.machine_id(self._registry.get(name)) == entry["machine"]
            )
        except Exception:  # noqa: BLE001 -- unproven: the queue may start anywhere
            logger.debug("could not read the queue's printer", exc_info=True)
            return None
        if not names:
            engagement.release_queue()
            return None
        return QueueOn(names=names, label=str(entry.get("label") or sorted(names)[0]))

    def _hold_queue(self, printer_name: str) -> None:
        """Below the fleet tier, put the queue on *printer_name* after it takes a job there, if it is on no printer yet."""
        try:
            from kiln.printers import engagement

            if not engagement.multi_machine_tier():
                engagement.hold_queue(self._registry.get(printer_name))
        except Exception:  # noqa: BLE001 -- bookkeeping never stops a print
            logger.debug("could not put the queue on %s", printer_name, exc_info=True)

    def _wait_turn(
        self, job: Any, printer_name: str, idle_printers: list[str], code: str, waits: dict[str, dict[str, Any]],
    ) -> None:
        """*job* was turned away because another printer holds the turn: put it back in line, untouched, and wait for that printer."""
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
        """Whether the turn wait has ended."""
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
        turn_ready: set[str] | frozenset[str] = frozenset(), queue_on: QueueOn | None = None,
    ) -> None:
        """Write each queued job's wait on the job, announce each plate that is holding the line once, and forget what no queued job needs."""
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
                        label=_job_label(first), beside=_planned_beside(first), moved=_moved(first),
                    ),
                },
                source="scheduler",
            )
        turn = self._turn_wait
        switching: list[Any] = []
        for job in queued:
            wait = waits.get(job.id)
            if wait is None and queue_on is not None and job.printer_name and job.printer_name not in queue_on.names:
                wait = {
                    "kind": WAIT_SWITCH, "code": QUEUE_ON_ONE_PRINTER_CODE, "printers": [job.printer_name],
                    "on": queue_on.label, "upgrade": TURN_UPGRADE,
                    "sentence": switch_wait_sentence(
                        job.file_name, job.printer_name, queue_on.label, label=_job_label(job),
                    ),
                }
                switching.append(job)
            elif turn is not None and (job.id in turn_ready or (
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
        fresh = [job for job in switching if job.id not in self._switch_announced]
        if fresh and queue_on is not None:
            first = fresh[0]
            sentence = switch_wait_sentence(first.file_name, first.printer_name, queue_on.label, label=_job_label(first))
            self._event_bus.publish(
                EventType.JOB_WAITING,
                {
                    "job_id": first.id, "file_name": first.file_name, "printer_name": first.printer_name,
                    "kind": WAIT_SWITCH, "waiting_jobs": [job.id for job in fresh],
                    "message": f"{sentence} {TURN_UPGRADE}",
                },
                source="scheduler",
            )
        self._switch_announced = {job.id for job in switching}

    @staticmethod
    def _written_wait(job: Any, wait: dict[str, Any] | None, plate_blocks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """The note a job carries."""
        if wait is None:
            return None
        printers = list(wait.get("printers") or [])
        note = {k: v for k, v in wait.items() if not k.startswith("_")}
        if note.get("kind") == WAIT_PLATE:
            camera = any(plate_blocks.get(p, {}).get("camera") for p in printers)
            note["sentence"] = plate_wait_sentence(
                job.file_name, printers, wait.get("_state"), code=str(wait.get("code") or ""), camera=camera,
                label=_job_label(job), beside=_planned_beside(job), moved=_moved(job),
            )
        before = (job.metadata or {}).get("waiting")
        same = isinstance(before, dict) and before.get("kind") == note.get("kind") and before.get("printers") == printers
        note["since"] = before.get("since") if same and before.get("since") else (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        return note

    def waiting_for(self, job_id: str) -> dict[str, Any] | None:
        """Why a queued job has not started, in a sentence, or ``None`` for a job that is not waiting in line."""
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
        """Run one scheduling cycle."""
        dispatched: list[dict[str, Any]] = []
        completed: list[str] = []
        failed: list[dict[str, str]] = []
        checked = 0

        self._settle_unfinished(failed)

        # Phase 1: Check active jobs for completion / failure
        with self._lock:
            active_snapshot = dict(self._active_jobs)

        for job_id, printer_name in active_snapshot.items():
            if job_id in self._preparing:
                continue
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
                # The scheduler watching jobs IT dispatched, to record how they ended
                from kiln.printers.engagement import internal_read

                with internal_read():
                    state = adapter.get_state()
                    job_progress = adapter.get_job()

                if getattr(state, "is_occupied", False) is True:
                    self._seen_printing.add(job_id)

                now = self._clock()
                self._observe(job_id, printer_name, adapter, state, job_progress, now)

                # Printer returned to idle -- the job has ENDED
                if state.confirmed_state == PrinterStatus.IDLE:
                    pre_idle_job = self._queue.get_job(job_id)
                    queue_cancelled = bool(
                        pre_idle_job is not None
                        and getattr(pre_idle_job.status, "value", str(pre_idle_job.status)).lower()
                        in ("cancelled", "canceled")
                    )
                    if not queue_cancelled:
                        if pre_idle_job is not None and pre_idle_job.status == JobStatus.PAUSED:
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
                        # IDLE is NOT testimony
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
                    self._settle_fill_plate(job_id)
                    self._forget_watch(job_id)
                    completed.append(job_id)

                elif state.confirmed_state == PrinterStatus.ERROR:
                    error_msg = f"Printer {printer_name} entered error state"
                    machine_reported = job_id in self._seen_printing
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    self._requeue_or_fail(
                        job_id, error_msg, failed, printer_name=printer_name,
                        machine_reported=machine_reported,
                    )

                # ...and `effective_state` on the branch that only WATCHES one
                elif state.effective_state == PrinterStatus.PRINTING:
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
                # A read that raises is a printer Kiln cannot see
                logger.warning("Error checking job %s on %s: %s", job_id, printer_name, exc)
                self._observe_silence(job_id, printer_name, self._clock(), cause="read_failed")

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

        queue_on = self._queue_on(has_pending)
        if queue_on is not None:
            available = [p for p in available if p in queue_on.names]

        turn_held = self._turn_wait is not None and not self._turn_over(idle_printers)

        waits: dict[str, dict[str, Any]] = {}
        plate_blocks: dict[str, dict[str, Any]] = {}

        if self._persistence and available:
            queued = [j for j in self._queue.list_jobs(status=JobStatus.QUEUED) if j.printer_name is None]
            if queued:
                available = self._rank_printers(available, queued[0])

        turn_ready: set[str] = set()
        for printer_name in available:
            if queue_on is not None and printer_name not in queue_on.names:
                continue
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

                signoff_record = (next_job.metadata or {}).get("preview_signoff") or {}
                refusal = record_refusal(signoff_record, printer_name)
                if refusal:
                    self._fail_permanently(next_job.id, refusal, failed, printer_name=None)
                    continue
                if isinstance((next_job.metadata or {}).get("fill"), dict):
                    turn = self._turn_refusal(adapter)
                    if turn is not None:
                        self._wait_turn(next_job, printer_name, idle_printers, turn, waits)
                        break
                    self._start_fill_job(next_job, printer_name)
                    queue_on = queue_on or self._queue_on(True)
                    continue
                grant_from_record(
                    signoff_record,
                    tool="scheduler", file_name=next_job.file_name, printer_name=printer_name,
                )
                sent_at = time.monotonic()
                _ids = {
                    k: str(next_job.metadata[k]).strip()
                    for k in ("version_id", "design_id", "branch_id")
                    if next_job.metadata and isinstance(next_job.metadata.get(k), str)
                    and next_job.metadata[k].strip()
                }
                result = adapter.start_print(next_job.file_name, **_ids)
                if getattr(result, "code", "") in _TURN_CODES:
                    # Another printer's turn
                    self._wait_turn(next_job, printer_name, idle_printers, str(result.code), waits)
                    break
                verdict = resolve_print_start(
                    adapter, result, sent_at=sent_at,
                    file_name=next_job.file_name,
                )
                if verdict.ok:
                    self._queue.note_waiting(next_job.id, None)
                    self._queue.mark_printing(next_job.id)
                    self._hold_queue(printer_name)
                    queue_on = queue_on or self._queue_on(True)
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
            self._settle_waits(waits, plate_blocks, turn_ready, queue_on)

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
