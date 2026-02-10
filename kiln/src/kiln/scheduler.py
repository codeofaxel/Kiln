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
from typing import Any, Dict, Optional

from kiln.events import EventBus, EventType
from kiln.printers.base import PrinterError, PrinterStatus
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry

logger = logging.getLogger(__name__)


class JobScheduler:
    """Background scheduler that dispatches print jobs to printers.

    Lifecycle:
        scheduler = JobScheduler(queue, registry, event_bus)
        scheduler.start()   # launches background thread
        ...
        scheduler.stop()    # graceful shutdown

    The scheduler polls every ``poll_interval`` seconds (default 5).
    """

    def __init__(
        self,
        queue: PrintQueue,
        registry: PrinterRegistry,
        event_bus: EventBus,
        poll_interval: float = 5.0,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_jobs: Dict[str, str] = {}  # job_id -> printer_name
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
                    self._event_bus.publish(
                        EventType.JOB_COMPLETED,
                        {"job_id": job_id, "printer_name": printer_name},
                        source="scheduler",
                    )
                    completed.append(job_id)

                elif state.state == PrinterStatus.ERROR:
                    error_msg = f"Printer {printer_name} entered error state"
                    self._queue.mark_failed(job_id, error_msg)
                    with self._lock:
                        self._active_jobs.pop(job_id, None)
                    self._event_bus.publish(
                        EventType.JOB_FAILED,
                        {
                            "job_id": job_id,
                            "printer_name": printer_name,
                            "error": error_msg,
                        },
                        source="scheduler",
                    )
                    failed.append({"job_id": job_id, "error": error_msg})

                elif state.state == PrinterStatus.PRINTING:
                    # Promote STARTING -> PRINTING when the printer confirms
                    try:
                        job = self._queue.get_job(job_id)
                        if job.status == JobStatus.STARTING:
                            self._queue.mark_printing(job_id)
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
                self._queue.mark_failed(job_id, error_msg)
                with self._lock:
                    self._active_jobs.pop(job_id, None)
                failed.append({"job_id": job_id, "error": error_msg})
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

        for printer_name in available:
            next_job = self._queue.next_job(printer_name=printer_name)
            if next_job is None:
                continue

            # Try to dispatch
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
                    self._queue.mark_failed(next_job.id, error_msg)
                    self._event_bus.publish(
                        EventType.JOB_FAILED,
                        {"job_id": next_job.id, "error": error_msg},
                        source="scheduler",
                    )
                    failed.append({"job_id": next_job.id, "error": error_msg})

            except PrinterError as exc:
                error_msg = f"Failed to start print on {printer_name}: {exc}"
                self._queue.mark_failed(next_job.id, error_msg)
                self._event_bus.publish(
                    EventType.JOB_FAILED,
                    {"job_id": next_job.id, "error": error_msg},
                    source="scheduler",
                )
                failed.append({"job_id": next_job.id, "error": error_msg})
            except Exception as exc:
                logger.exception("Unexpected error dispatching job %s", next_job.id)
                self._queue.mark_failed(next_job.id, str(exc))
                failed.append({"job_id": next_job.id, "error": str(exc)})

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
