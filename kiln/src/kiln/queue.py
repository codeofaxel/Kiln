"""Print job queue — ordered, persistent job management.

Agents submit print jobs to the queue rather than directly calling
``start_print()``.  The queue ensures:

- Jobs are executed in priority/FIFO order.
- Only one job runs per printer at a time.
- Failed jobs are tracked and can be retried.
- The full history of submitted, running, and completed jobs is queryable.
- Jobs are persisted to SQLite for crash recovery.  On startup, jobs that
  were in QUEUED or STARTING/PRINTING state are reloaded (active jobs are
  reset to QUEUED since the printer state is unknown after a crash).

Example::

    queue = PrintQueue()          # in-memory only
    queue = PrintQueue(db_path="~/.kiln/queue.db")  # persistent
    job_id = queue.submit(
        file_name="benchy.gcode",
        printer_name="voron-350",
        submitted_by="agent-claude",
    )
    queue.get_job(job_id)
"""

from __future__ import annotations

import enum
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(enum.Enum):
    """Lifecycle states for a print job."""

    QUEUED = "queued"
    STARTING = "starting"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PrintJob:
    """A single print job in the queue."""

    id: str
    file_name: str
    printer_name: str | None  # None = any available printer
    status: JobStatus
    submitted_by: str  # agent identifier
    priority: int = 0  # higher = more urgent
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @property
    def elapsed_seconds(self) -> float | None:
        """Seconds since the job started printing, or None if not started."""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return round(end - self.started_at, 1)

    @property
    def wait_seconds(self) -> float:
        """Seconds the job has been waiting in the queue."""
        start = self.started_at or time.time()
        return round(start - self.created_at, 1)


class JobNotFoundError(KeyError):
    """Raised when a job ID is not in the queue."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job not found: {job_id!r}")
        self.job_id = job_id


class InvalidStateTransition(ValueError):
    """Raised when a job transition violates the state machine."""

    def __init__(self, job_id: str, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(f"Invalid state transition for job {job_id!r}: {from_status.value} -> {to_status.value}")
        self.job_id = job_id
        self.from_status = from_status
        self.to_status = to_status


class JobStateMachine:
    """Defines valid state transitions for the print job lifecycle.

    Transitions are explicit — any transition not listed here is illegal
    and will raise :class:`InvalidStateTransition`.
    """

    _TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
        JobStatus.QUEUED: frozenset(
            {
                JobStatus.STARTING,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }
        ),
        JobStatus.STARTING: frozenset(
            {
                JobStatus.PRINTING,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }
        ),
        JobStatus.PRINTING: frozenset(
            {
                JobStatus.PAUSED,
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }
        ),
        JobStatus.PAUSED: frozenset(
            {
                JobStatus.PRINTING,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }
        ),
        # Terminal states — no outgoing transitions
        JobStatus.COMPLETED: frozenset(),
        JobStatus.FAILED: frozenset(),
        JobStatus.CANCELLED: frozenset(),
    }

    @classmethod
    def validate(cls, job_id: str, from_status: JobStatus, to_status: JobStatus) -> None:
        """Raise :class:`InvalidStateTransition` if the transition is illegal.

        :param job_id: Used only for error messages.
        :param from_status: Current job status.
        :param to_status: Desired next status.
        """
        allowed = cls._TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise InvalidStateTransition(job_id, from_status, to_status)

    @classmethod
    def allowed_transitions(cls, status: JobStatus) -> frozenset[JobStatus]:
        """Return the set of states reachable from *status*."""
        return cls._TRANSITIONS.get(status, frozenset())


# Stuck job timeout — configurable via environment variable.
_STUCK_JOB_TIMEOUT_MINUTES: int = int(os.environ.get("KILN_STUCK_JOB_TIMEOUT_MINUTES", "30"))


class PrintQueue:
    """Thread-safe print job queue with optional SQLite persistence.

    Jobs are stored in insertion order.  :meth:`next_job` returns the
    highest-priority queued job, breaking ties by submission time (FIFO).

    If *db_path* is provided, jobs are persisted to SQLite so they survive
    server crashes.  On init, in-flight jobs (STARTING/PRINTING) are reset
    to QUEUED since the printer state is unknown after a restart.
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        event_bus: Any | None = None,
    ) -> None:
        self._jobs: dict[str, PrintJob] = {}
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._event_bus: Any | None = event_bus  # kiln.events.EventBus

        if db_path:
            resolved = os.path.expanduser(db_path)
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            self._db = sqlite3.connect(resolved, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    printer_name TEXT,
                    status TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    error TEXT,
                    metadata TEXT DEFAULT '{}'
                )"""
            )
            self._db.commit()
            self._reload_from_db()

    # ------------------------------------------------------------------
    # Submit / cancel
    # ------------------------------------------------------------------

    def submit(
        self,
        file_name: str,
        printer_name: str | None = None,
        submitted_by: str = "unknown",
        priority: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Add a new job to the queue.

        Args:
            file_name: G-code file name (must already exist on the printer
                or be uploaded before the job starts).
            printer_name: Target printer name, or ``None`` to let the
                scheduler pick any idle printer.
            submitted_by: Identifier of the agent or user submitting.
            priority: Higher values are scheduled first.
            metadata: Arbitrary key-value data to attach to the job.

        Returns:
            The unique job ID.
        """
        job_id = uuid.uuid4().hex[:12]
        job = PrintJob(
            id=job_id,
            file_name=file_name,
            printer_name=printer_name,
            status=JobStatus.QUEUED,
            submitted_by=submitted_by,
            priority=priority,
            metadata=metadata or {},
        )
        with self._lock:
            self._jobs[job_id] = job
        self._persist_job(job)
        return job_id

    def cancel(self, job_id: str) -> PrintJob:
        """Cancel a queued or running job.

        Args:
            job_id: The job to cancel.

        Returns:
            The updated job.

        Raises:
            JobNotFoundError: If the job doesn't exist.
            InvalidStateTransition: If the job cannot be cancelled from
                its current state (e.g. already completed).
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.CANCELLED)
            job.status = JobStatus.CANCELLED
            job.completed_at = time.time()
        self._update_job_db(job)
        return job

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def mark_starting(self, job_id: str) -> PrintJob:
        """Mark a job as starting (being sent to the printer).

        Raises:
            InvalidStateTransition: If the job is not in QUEUED state.
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.STARTING)
            job.status = JobStatus.STARTING
            job.started_at = time.time()
        self._update_job_db(job)
        return job

    def mark_printing(self, job_id: str) -> PrintJob:
        """Mark a job as actively printing.

        Raises:
            InvalidStateTransition: If the job is not in STARTING or
                PAUSED state.
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.PRINTING)
            job.status = JobStatus.PRINTING
            if job.started_at is None:
                job.started_at = time.time()
        self._update_job_db(job)
        return job

    def mark_completed(self, job_id: str) -> PrintJob:
        """Mark a job as successfully completed.

        Raises:
            InvalidStateTransition: If the job is not in PRINTING state.
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.COMPLETED)
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
        self._update_job_db(job)
        return job

    def mark_failed(self, job_id: str, error: str) -> PrintJob:
        """Mark a job as failed with an error message.

        Raises:
            InvalidStateTransition: If the job is already in a terminal
                state.
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.FAILED)
            job.status = JobStatus.FAILED
            job.completed_at = time.time()
            job.error = error
        self._update_job_db(job)
        return job

    def mark_paused(self, job_id: str) -> PrintJob:
        """Mark a job as paused.

        Raises:
            InvalidStateTransition: If the job is not in PRINTING state.
        """
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.PAUSED)
            job.status = JobStatus.PAUSED
        self._update_job_db(job)
        return job

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> PrintJob:
        """Return a job by ID.

        Raises:
            JobNotFoundError: If the job doesn't exist.
        """
        with self._lock:
            return self._get(job_id)

    def list_jobs(
        self,
        status: JobStatus | None = None,
        printer_name: str | None = None,
        limit: int = 100,
    ) -> list[PrintJob]:
        """Return jobs matching the given filters.

        Results are ordered by priority (descending) then creation time
        (ascending).

        Args:
            status: Filter by job status, or ``None`` for all.
            printer_name: Filter by target printer, or ``None`` for all.
            limit: Maximum number of results.
        """
        with self._lock:
            jobs = list(self._jobs.values())

        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        if printer_name is not None:
            jobs = [j for j in jobs if j.printer_name == printer_name]

        # Sort: highest priority first, then oldest first (FIFO within priority)
        jobs.sort(key=lambda j: (-j.priority, j.created_at))
        return jobs[:limit]

    def next_job(self, printer_name: str | None = None) -> PrintJob | None:
        """Return the next queued job to execute.

        Picks the highest-priority QUEUED job, optionally filtered to
        jobs targeting a specific printer (or jobs with no printer
        preference).

        Args:
            printer_name: If given, only return jobs targeting this printer
                or jobs with ``printer_name=None`` (any printer).

        Returns:
            The next job to execute, or ``None`` if the queue is empty.
        """
        with self._lock:
            candidates = [j for j in self._jobs.values() if j.status == JobStatus.QUEUED]

        if printer_name is not None:
            candidates = [j for j in candidates if j.printer_name is None or j.printer_name == printer_name]

        if not candidates:
            return None

        candidates.sort(key=lambda j: (-j.priority, j.created_at))
        return candidates[0]

    def pending_count(self) -> int:
        """Number of jobs in QUEUED state."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)

    def active_count(self) -> int:
        """Number of jobs in STARTING or PRINTING state."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in (JobStatus.STARTING, JobStatus.PRINTING))

    @property
    def total_count(self) -> int:
        """Total number of jobs (all statuses)."""
        with self._lock:
            return len(self._jobs)

    def summary(self) -> dict[str, int]:
        """Return a count of jobs per status."""
        with self._lock:
            counts: dict[str, int] = {}
            for job in self._jobs.values():
                key = job.status.value
                counts[key] = counts.get(key, 0) + 1
            return counts

    # ------------------------------------------------------------------
    # Stuck job detection
    # ------------------------------------------------------------------

    def check_stuck_jobs(self, *, timeout_minutes: int | None = None) -> list[PrintJob]:
        """Scan for jobs stuck in STARTING or PRINTING and auto-fail them.

        A job is considered stuck if it has been in STARTING or PRINTING
        state for longer than *timeout_minutes* (default read from
        ``KILN_STUCK_JOB_TIMEOUT_MINUTES``, falling back to 30).

        Each auto-failed job is transitioned to FAILED with
        ``error="stuck_timeout"`` and a :data:`JOB_STUCK_TIMEOUT` event
        is published if an event bus is available.

        Args:
            timeout_minutes: Override the default timeout.

        Returns:
            List of jobs that were auto-failed.
        """
        timeout = timeout_minutes if timeout_minutes is not None else _STUCK_JOB_TIMEOUT_MINUTES
        cutoff = time.time() - (timeout * 60)
        failed_jobs: list[PrintJob] = []

        with self._lock:
            candidates = [
                j
                for j in self._jobs.values()
                if j.status in (JobStatus.STARTING, JobStatus.PRINTING)
                and j.started_at is not None
                and j.started_at < cutoff
            ]

        for job in candidates:
            previous_status = job.status.value
            previous_started_at = job.started_at
            try:
                self.mark_failed(job.id, "stuck_timeout")
                failed_jobs.append(job)
                logger.warning(
                    "Auto-failed stuck job %s (was %s for %.0f min)",
                    job.id,
                    previous_status,
                    (time.time() - (previous_started_at or 0)) / 60,
                )
                self._publish_stuck_timeout(job)
            except (InvalidStateTransition, JobNotFoundError):
                # Job was already transitioned by another thread — skip.
                pass

        return failed_jobs

    def _publish_stuck_timeout(self, job: PrintJob) -> None:
        """Publish a JOB_STUCK_TIMEOUT event if an event bus is wired."""
        if self._event_bus is None:
            return
        try:
            from kiln.events import Event, EventType

            self._event_bus.publish(
                Event(
                    type=EventType.JOB_STUCK_TIMEOUT,
                    data=job.to_dict(),
                    source="queue",
                )
            )
        except Exception:
            logger.exception("Failed to publish stuck timeout event for job %s", job.id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, job_id: str) -> PrintJob:
        """Return job or raise (caller must hold lock)."""
        if job_id not in self._jobs:
            raise JobNotFoundError(job_id)
        return self._jobs[job_id]

    # ------------------------------------------------------------------
    # SQLite persistence helpers
    # ------------------------------------------------------------------

    def _persist_job(self, job: PrintJob) -> None:
        """Write a job to SQLite (INSERT)."""
        if self._db is None:
            return
        try:
            self._db.execute(
                """INSERT OR REPLACE INTO jobs
                   (id, file_name, printer_name, status, submitted_by,
                    priority, created_at, started_at, completed_at, error, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id,
                    job.file_name,
                    job.printer_name,
                    job.status.value,
                    job.submitted_by,
                    job.priority,
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                    job.error,
                    json.dumps(job.metadata),
                ),
            )
            self._db.commit()
        except Exception:
            logger.exception("Failed to persist job %s to SQLite", job.id)

    def _update_job_db(self, job: PrintJob) -> None:
        """Update an existing job row in SQLite."""
        if self._db is None:
            return
        try:
            self._db.execute(
                """UPDATE jobs SET status=?, started_at=?, completed_at=?,
                   error=? WHERE id=?""",
                (
                    job.status.value,
                    job.started_at,
                    job.completed_at,
                    job.error,
                    job.id,
                ),
            )
            self._db.commit()
        except Exception:
            logger.exception("Failed to update job %s in SQLite", job.id)

    def _reload_from_db(self) -> None:
        """Reload non-terminal jobs from SQLite on startup."""
        if self._db is None:
            return
        cursor = self._db.execute(
            """SELECT id, file_name, printer_name, status, submitted_by,
                      priority, created_at, started_at, completed_at, error, metadata
               FROM jobs
               WHERE status IN ('queued', 'starting', 'printing', 'paused')"""
        )
        recovered = 0
        for row in cursor.fetchall():
            status_str = row[3]
            # Reset in-flight jobs to QUEUED — printer state unknown after crash
            if status_str in ("starting", "printing", "paused"):
                status_str = "queued"
            job = PrintJob(
                id=row[0],
                file_name=row[1],
                printer_name=row[2],
                status=JobStatus(status_str),
                submitted_by=row[4],
                priority=row[5],
                created_at=row[6],
                started_at=None,  # reset since we're requeuing
                completed_at=row[8],
                error=row[9],
                metadata=json.loads(row[10]) if row[10] else {},
            )
            self._jobs[job.id] = job
            # Update DB to reflect the reset
            self._update_job_db(job)
            recovered += 1
        if recovered:
            logger.info("Recovered %d job(s) from SQLite after restart", recovered)
