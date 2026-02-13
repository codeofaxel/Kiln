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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# State transition validation
# ------------------------------------------------------------------

_VALID_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {}  # populated after JobStatus is defined


class InvalidTransitionError(ValueError):
    """Raised when attempting an invalid job status transition."""

    def __init__(self, job_id: str, current_status: JobStatus, target_status: JobStatus) -> None:
        super().__init__(
            f"Invalid transition for job {job_id!r}: "
            f"{current_status.value} → {target_status.value}"
        )
        self.job_id = job_id
        self.current_status = current_status
        self.target_status = target_status


class JobStatus(enum.Enum):
    """Lifecycle states for a print job."""

    QUEUED = "queued"
    STARTING = "starting"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Populate transition table after JobStatus is defined
_VALID_TRANSITIONS = {
    JobStatus.QUEUED: frozenset({JobStatus.STARTING, JobStatus.PRINTING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.STARTING: frozenset({JobStatus.PRINTING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}),  # QUEUED for re-queue
    JobStatus.PRINTING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),  # terminal
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),  # allow retry
    JobStatus.CANCELLED: frozenset(),  # terminal
}


def get_valid_transitions(status: JobStatus) -> frozenset[JobStatus]:
    """Return the set of valid target states from the given status.

    Args:
        status: The current job status.

    Returns:
        A frozenset of valid target statuses.
    """
    return _VALID_TRANSITIONS[status]


@dataclass
class PrintJob:
    """A single print job in the queue."""

    id: str
    file_name: str
    printer_name: Optional[str]  # None = any available printer
    status: JobStatus
    submitted_by: str  # agent identifier
    priority: int = 0  # higher = more urgent
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @property
    def elapsed_seconds(self) -> Optional[float]:
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


class PrintQueue:
    """Thread-safe print job queue with optional SQLite persistence.

    Jobs are stored in insertion order.  :meth:`next_job` returns the
    highest-priority queued job, breaking ties by submission time (FIFO).

    If *db_path* is provided, jobs are persisted to SQLite so they survive
    server crashes.  On init, in-flight jobs (STARTING/PRINTING) are reset
    to QUEUED since the printer state is unknown after a restart.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._jobs: Dict[str, PrintJob] = {}
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None

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
        printer_name: Optional[str] = None,
        submitted_by: str = "unknown",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
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

        Only jobs in QUEUED or PRINTING state can be cancelled.

        Args:
            job_id: The job to cancel.

        Returns:
            The updated job.

        Raises:
            JobNotFoundError: If the job doesn't exist.
            InvalidTransitionError: If the transition is not allowed.
        """
        with self._lock:
            job = self._get(job_id)
            self._validate_transition(job, JobStatus.CANCELLED)
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
            InvalidTransitionError: If the transition is not allowed.
        """
        with self._lock:
            job = self._get(job_id)
            self._validate_transition(job, JobStatus.STARTING)
            job.status = JobStatus.STARTING
            job.started_at = time.time()
        self._update_job_db(job)
        return job

    def mark_printing(self, job_id: str) -> PrintJob:
        """Mark a job as actively printing.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        with self._lock:
            job = self._get(job_id)
            self._validate_transition(job, JobStatus.PRINTING)
            job.status = JobStatus.PRINTING
            if job.started_at is None:
                job.started_at = time.time()
        self._update_job_db(job)
        return job

    def mark_completed(self, job_id: str) -> PrintJob:
        """Mark a job as successfully completed.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        with self._lock:
            job = self._get(job_id)
            self._validate_transition(job, JobStatus.COMPLETED)
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
        self._update_job_db(job)
        return job

    def mark_failed(self, job_id: str, error: str) -> PrintJob:
        """Mark a job as failed with an error message.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        with self._lock:
            job = self._get(job_id)
            self._validate_transition(job, JobStatus.FAILED)
            job.status = JobStatus.FAILED
            job.completed_at = time.time()
            job.error = error
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
        status: Optional[JobStatus] = None,
        printer_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[PrintJob]:
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

    def next_job(self, printer_name: Optional[str] = None) -> Optional[PrintJob]:
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
            candidates = [
                j for j in candidates
                if j.printer_name is None or j.printer_name == printer_name
            ]

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
            return sum(
                1 for j in self._jobs.values()
                if j.status in (JobStatus.STARTING, JobStatus.PRINTING)
            )

    @property
    def total_count(self) -> int:
        """Total number of jobs (all statuses)."""
        with self._lock:
            return len(self._jobs)

    def summary(self) -> Dict[str, int]:
        """Return a count of jobs per status."""
        with self._lock:
            counts: Dict[str, int] = {}
            for job in self._jobs.values():
                key = job.status.value
                counts[key] = counts.get(key, 0) + 1
            return counts

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, job_id: str) -> PrintJob:
        """Return job or raise (caller must hold lock)."""
        if job_id not in self._jobs:
            raise JobNotFoundError(job_id)
        return self._jobs[job_id]

    def _validate_transition(self, job: PrintJob, target_status: JobStatus) -> None:
        """Validate that a state transition is allowed.

        Args:
            job: The job being transitioned.
            target_status: The desired target status.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        valid_targets = _VALID_TRANSITIONS[job.status]
        if target_status not in valid_targets:
            raise InvalidTransitionError(job.id, job.status, target_status)

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
                    job.id, job.file_name, job.printer_name,
                    job.status.value, job.submitted_by, job.priority,
                    job.created_at, job.started_at, job.completed_at,
                    job.error, json.dumps(job.metadata),
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
                    job.status.value, job.started_at, job.completed_at,
                    job.error, job.id,
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
               WHERE status IN ('queued', 'starting', 'printing')"""
        )
        recovered = 0
        for row in cursor.fetchall():
            status_str = row[3]
            # Reset in-flight jobs to QUEUED — printer state unknown after crash
            if status_str in ("starting", "printing"):
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
