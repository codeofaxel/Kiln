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
from typing import Any, Dict, List, Optional

from kiln.events import EventBus, EventType
from kiln.printers.base import PrinterError, PrinterStatus
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry

logger = logging.getLogger(__name__)


# Jobs in PRINTING state longer than this are considered stuck.
_STUCK_JOB_TIMEOUT_SECONDS: float = 7200.0  # 2 hours


class JobScheduler:
    """Background scheduler that dispatches print jobs to printers.

    Lifecycle:
        scheduler = JobScheduler(queue, registry, event_bus)
        scheduler.start()   # launches background thread
        ...
        scheduler.stop()    # graceful shutdown

    The scheduler polls every ``poll_interval`` seconds (default 5).
    Jobs stuck in PRINTING state for over 2 hours are auto-failed.
    """

    def __init__(
        self,
        queue: PrintQueue,
        registry: PrinterRegistry,
        event_bus: EventBus,
        poll_interval: float = 5.0,
        max_retries: int = 2,
        retry_backoff_base: float = 30.0,
        persistence: Optional[object] = None,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._persistence = persistence
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_jobs: Dict[str, str] = {}  # job_id -> printer_name
        self._retry_counts: Dict[str, int] = {}  # job_id -> attempts so far
        self._retry_not_before: Dict[str, float] = {}  # job_id -> earliest retry timestamp
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Whether the scheduler background thread is running."""
        return self._running

    @property
    def active_jobs(self) -> Dict[str, str]:
        """Return a copy of the active job->printer mapping."""
        with self._lock:
            return dict(self._active_jobs)

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._running:
            return
        self._running = True
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
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            self._thread = None
        logger.info("Job scheduler stopped")

    def _requeue_or_fail(
        self,
        job_id: str,
        error_msg: str,
        failed_list: list[Dict[str, str]],
        printer_name: str | None = None,
    ) -> bool:
        """Try to re-queue a failed job if retries remain.

        Returns ``True`` if the job was re-queued, ``False`` if it was
        permanently marked as failed (appended to *failed_list*).
        """
        count = self._retry_counts.get(job_id, 0)
        if count < self._max_retries:
            self._retry_counts[job_id] = count + 1
            # Exponential backoff: 30s, 60s, 120s, ...
            delay = self._retry_backoff_base * (2 ** count)
            self._retry_not_before[job_id] = time.time() + delay
            # Reset the job back to QUEUED so a future tick can redispatch it
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
        self._retry_counts.pop(job_id, None)
        self._retry_not_before.pop(job_id, None)
        self._queue.mark_failed(job_id, error_msg)
        self._event_bus.publish(
            EventType.JOB_FAILED,
            {"job_id": job_id, "error": error_msg},
            source="scheduler",
        )
        if printer_name:
            self._auto_record_outcome(job_id, printer_name, "failed", error_msg=error_msg)
        failed_list.append({"job_id": job_id, "error": error_msg})
        return False

    def _rank_printers(self, available: List[str], job) -> List[str]:
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
            file_hash=file_hash, material_type=material_type,
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
    ) -> None:
        """Best-effort auto-record a print outcome to the learning database."""
        if not self._persistence:
            return
        try:
            # Check if outcome already recorded (agent may have beaten us)
            existing = self._persistence.get_print_outcome(job_id)
            if existing is not None:
                return  # Agent already recorded — don't overwrite

            # Try to get job metadata for richer outcome data
            job = self._queue.get_job(job_id)

            self._persistence.save_print_outcome({
                "job_id": job_id,
                "printer_name": printer_name,
                "file_name": job.file_name if job else None,
                "file_hash": job.metadata.get("file_hash") if job and job.metadata else None,
                "material_type": job.metadata.get("material_type") if job and job.metadata else None,
                "outcome": outcome,
                "quality_grade": None,  # Only agents can assess quality
                "failure_mode": None,   # Only agents can classify failure mode
                "settings": None,
                "environment": None,
                "notes": f"Auto-recorded by scheduler. {error_msg}" if error_msg else "Auto-recorded by scheduler.",
                "agent_id": "scheduler",
                "created_at": time.time(),
            })
            logger.debug("Auto-recorded %s outcome for job %s", outcome, job_id)
        except Exception:
            logger.debug("Failed to auto-record outcome for job %s (non-fatal)", job_id, exc_info=True)

    def tick(self) -> Dict[str, Any]:
        """Run one scheduling cycle.  Can be called manually for testing.

        Returns a dict summarising what happened:
            dispatched: list of {job_id, printer_name, file_name}
            completed: list of job_ids detected as complete
            failed: list of {job_id, error}
            checked: number of active jobs checked
        """
        dispatched: list[Dict[str, Any]] = []
        completed: list[str] = []
        failed: list[Dict[str, str]] = []
        checked = 0

        # Phase 1: Check active jobs for completion / failure
        with self._lock:
            active_snapshot = dict(self._active_jobs)

        for job_id, printer_name in active_snapshot.items():
            checked += 1
            try:
                adapter = self._registry.get(printer_name)
                state = adapter.get_state()
                job_progress = adapter.get_job()

                # Printer returned to idle -- job is done
                if state.state == PrinterStatus.IDLE:
                    self._queue.mark_completed(job_id)
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                        self._retry_counts.pop(job_id, None)
                        self._retry_not_before.pop(job_id, None)
                    self._event_bus.publish(
                        EventType.JOB_COMPLETED,
                        {"job_id": job_id, "printer_name": printer_name},
                        source="scheduler",
                    )
                    self._auto_record_outcome(job_id, printer_name, "success")
                    completed.append(job_id)

                elif state.state == PrinterStatus.ERROR:
                    error_msg = f"Printer {printer_name} entered error state"
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    self._requeue_or_fail(job_id, error_msg, failed, printer_name=printer_name)

                elif state.state == PrinterStatus.PRINTING:
                    # Promote STARTING -> PRINTING when the printer confirms
                    try:
                        job = self._queue.get_job(job_id)
                        if job.status == JobStatus.STARTING:
                            self._queue.mark_printing(job_id)
                    except Exception:
                        pass

                    # Stuck job detection: fail jobs in PRINTING too long
                    try:
                        job = self._queue.get_job(job_id)
                        if (
                            job.started_at is not None
                            and (time.time() - job.started_at) > _STUCK_JOB_TIMEOUT_SECONDS
                        ):
                            error_msg = (
                                f"Job timed out after "
                                f"{_STUCK_JOB_TIMEOUT_SECONDS / 3600:.0f}h "
                                f"— printer may be disconnected or hung"
                            )
                            logger.warning(
                                "Stuck job detected: %s on %s (%.0f min)",
                                job_id, printer_name,
                                (time.time() - job.started_at) / 60,
                            )
                            with self._lock:
                                self._active_jobs.pop(job_id, None)
                            self._requeue_or_fail(job_id, error_msg, failed, printer_name=printer_name)
                            continue
                    except Exception:
                        pass

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
                logger.warning(
                    "Error checking job %s on %s: %s", job_id, printer_name, exc
                )

        # Phase 2: Dispatch queued jobs to idle printers
        idle_printers = self._registry.get_idle_printers()

        # Filter out printers that already have active jobs
        with self._lock:
            busy_printers = set(self._active_jobs.values())
        available = [p for p in idle_printers if p not in busy_printers]

        # Smart routing: rank printers by historical success rate for the
        # next queued unassigned job.  This ensures the best-performing
        # printer for the job's file/material gets first dispatch priority.
        if self._persistence and available:
            queued = [
                j for j in self._queue.list_jobs(status=JobStatus.QUEUED)
                if j.printer_name is None
            ]
            if queued:
                available = self._rank_printers(available, queued[0])

        for printer_name in available:
            next_job = self._queue.next_job(printer_name=printer_name)
            if next_job is None:
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

                result = adapter.start_print(next_job.file_name)
                if result.success:
                    self._queue.mark_printing(next_job.id)
                    with self._lock:
                        self._active_jobs[next_job.id] = printer_name
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
                    error_msg = result.message or "start_print returned failure"
                    self._requeue_or_fail(next_job.id, error_msg, failed)

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
            time.sleep(self._poll_interval)
