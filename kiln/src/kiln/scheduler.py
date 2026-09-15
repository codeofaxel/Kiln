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
from typing import Any

from kiln.events import EventBus, EventType
from kiln.print_start_verdict import resolve_print_start
from kiln.printers.base import PrinterError, PrinterStatus, status_is_unreachable
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
        return watch.ending_doubt if watch else None

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

        # Phase 1: Check active jobs for completion / failure
        with self._lock:
            active_snapshot = dict(self._active_jobs)

        for job_id, printer_name in active_snapshot.items():
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
        if self._queue.pending_count() == 0:
            idle_printers: list[str] = []
        else:
            idle_printers = self._registry.get_idle_printers()

        # Filter out printers that already have active jobs
        with self._lock:
            busy_printers = set(self._active_jobs.values())
        available = [p for p in idle_printers if p not in busy_printers]

        # Smart routing: rank printers by historical success rate for the
        # next queued unassigned job.  This ensures the best-performing
        # printer for the job's file/material gets first dispatch priority.
        if self._persistence and available:
            queued = [j for j in self._queue.list_jobs(status=JobStatus.QUEUED) if j.printer_name is None]
            if queued:
                available = self._rank_printers(available, queued[0])

        for printer_name in available:
            next_job = self._queue.next_job(printer_name=printer_name)
            if next_job is None:
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
                self._queue.mark_starting(next_job.id)

                # An unconfirmed start must not be read as a failure here:
                # requeuing a job the printer actually took dispatches the
                # same file at a machine that is already running it.
                sent_at = time.monotonic()
                result = adapter.start_print(next_job.file_name)
                verdict = resolve_print_start(
                    adapter, result, sent_at=sent_at,
                    file_name=next_job.file_name,
                )
                if verdict.ok:
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

            except PrinterError as exc:
                error_msg = f"Failed to start print on {printer_name}: {exc}"
                self._requeue_or_fail(next_job.id, error_msg, failed)
            except Exception as exc:
                logger.exception("Unexpected error dispatching job %s", next_job.id)
                self._requeue_or_fail(next_job.id, str(exc), failed)
            finally:
                printer_mutex.release()

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
