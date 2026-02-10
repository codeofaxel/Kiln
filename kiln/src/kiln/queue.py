"""Print job queue — ordered, persistent-ready job management.

Agents submit print jobs to the queue rather than directly calling
``start_print()``.  The queue ensures:

- Jobs are executed in priority/FIFO order.
- Only one job runs per printer at a time.
- Failed jobs are tracked and can be retried.
- The full history of submitted, running, and completed jobs is queryable.

This is an in-memory implementation.  A future version will persist to
SQLite or Supabase for crash recovery.

Example::

    queue = PrintQueue()
    job_id = queue.submit(
        file_name="benchy.gcode",
        printer_name="voron-350",
        submitted_by="agent-claude",
    )
    queue.get_job(job_id)
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class JobStatus(enum.Enum):
    """Lifecycle states for a print job."""

    QUEUED = "queued"
    STARTING = "starting"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    """Thread-safe in-memory print job queue.

    Jobs are stored in insertion order.  :meth:`next_job` returns the
    highest-priority queued job, breaking ties by submission time (FIFO).
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, PrintJob] = {}
        self._lock = threading.Lock()

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
            ValueError: If the job is already in a terminal state.
        """
        with self._lock:
            job = self._get(job_id)
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                raise ValueError(
                    f"Cannot cancel job {job_id!r}: already {job.status.value}"
                )
            job.status = JobStatus.CANCELLED
            job.completed_at = time.time()
            return job

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def mark_starting(self, job_id: str) -> PrintJob:
        """Mark a job as starting (being sent to the printer)."""
        with self._lock:
            job = self._get(job_id)
            job.status = JobStatus.STARTING
            job.started_at = time.time()
            return job

    def mark_printing(self, job_id: str) -> PrintJob:
        """Mark a job as actively printing."""
        with self._lock:
            job = self._get(job_id)
            job.status = JobStatus.PRINTING
            if job.started_at is None:
                job.started_at = time.time()
            return job

    def mark_completed(self, job_id: str) -> PrintJob:
        """Mark a job as successfully completed."""
        with self._lock:
            job = self._get(job_id)
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
            return job

    def mark_failed(self, job_id: str, error: str) -> PrintJob:
        """Mark a job as failed with an error message."""
        with self._lock:
            job = self._get(job_id)
            job.status = JobStatus.FAILED
            job.completed_at = time.time()
            job.error = error
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
