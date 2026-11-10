"""Print job queue — ordered, persistent job management."""

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
    idempotency_key: str | None = None  # caller-supplied replay guard

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


class IdempotencyConflict(ValueError):
    """Raised when an idempotency key is reused with a different submission."""

    def __init__(self, idempotency_key: str, existing_job_id: str) -> None:
        super().__init__(
            f"Idempotency key {idempotency_key!r} was already used by job "
            f"{existing_job_id!r} with different submission parameters. "
            "Use a new key for a new job."
        )
        self.idempotency_key = idempotency_key
        self.existing_job_id = existing_job_id


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of a queue submission."""

    job: PrintJob
    replayed: bool


class InvalidStateTransition(ValueError):
    """Raised when a job transition violates the state machine."""

    def __init__(self, job_id: str, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(f"Invalid state transition for job {job_id!r}: {from_status.value} -> {to_status.value}")
        self.job_id = job_id
        self.from_status = from_status
        self.to_status = to_status


class JobStateMachine:
    """Defines valid state transitions for the print job lifecycle."""

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
        """Raise :class:`InvalidStateTransition` if the transition is illegal."""
        allowed = cls._TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise InvalidStateTransition(job_id, from_status, to_status)

    @classmethod
    def allowed_transitions(cls, status: JobStatus) -> frozenset[JobStatus]:
        """Return the set of states reachable from *status*."""
        return cls._TRANSITIONS.get(status, frozenset())


class PrintQueue:
    """Thread-safe print job queue with optional SQLite persistence."""

    def __init__(self, db_path: str | None = None) -> None:
        self._jobs: dict[str, PrintJob] = {}
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._idem_index: dict[str, str] = {}

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
                    metadata TEXT DEFAULT '{}',
                    idempotency_key TEXT
                )"""
            )
            cols = {
                row[1]
                for row in self._db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "idempotency_key" not in cols:
                self._db.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
            self._db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key
                   ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"""
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
        idempotency_key: str | None = None,
    ) -> str:
        """Add a new job to the queue."""
        return self.submit_result(
            file_name,
            printer_name=printer_name,
            submitted_by=submitted_by,
            priority=priority,
            metadata=metadata,
            idempotency_key=idempotency_key,
        ).job.id

    def submit_result(
        self,
        file_name: str,
        printer_name: str | None = None,
        submitted_by: str = "unknown",
        priority: int = 0,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> SubmitResult:
        """:meth:`submit`, returning the job and whether it was a replay."""
        with self._lock:
            if idempotency_key is not None:
                existing = self._find_by_key_locked(idempotency_key)
                if existing is not None:
                    self._check_replay_matches(
                        existing, file_name, printer_name, priority
                    )
                    return SubmitResult(job=existing, replayed=True)

            job_id = uuid.uuid4().hex[:12]
            job = PrintJob(
                id=job_id,
                file_name=file_name,
                printer_name=printer_name,
                status=JobStatus.QUEUED,
                submitted_by=submitted_by,
                priority=priority,
                metadata=metadata or {},
                idempotency_key=idempotency_key,
            )
            self._jobs[job_id] = job
            if idempotency_key is not None:
                self._idem_index[idempotency_key] = job_id

        # Persist OUTSIDE the lock
        try:
            self._persist_job(job)
        except sqlite3.IntegrityError:
            with self._lock:
                self._jobs.pop(job_id, None)
                if (
                    idempotency_key is not None
                    and self._idem_index.get(idempotency_key) == job_id
                ):
                    del self._idem_index[idempotency_key]
                winner = (
                    self._db_find_by_key(idempotency_key)
                    if idempotency_key is not None
                    else None
                )
            if winner is None:
                raise
            self._check_replay_matches(winner, file_name, printer_name, priority)
            return SubmitResult(job=winner, replayed=True)
        return SubmitResult(job=job, replayed=False)

    def find_by_idempotency_key(self, idempotency_key: str) -> PrintJob | None:
        """Return the job an idempotency key names, or ``None``."""
        with self._lock:
            return self._find_by_key_locked(idempotency_key)

    def _find_by_key_locked(self, idempotency_key: str) -> PrintJob | None:
        """Resolve a key to its job (caller must hold the lock)."""
        job_id = self._idem_index.get(idempotency_key)
        if job_id is not None and job_id in self._jobs:
            return self._jobs[job_id]
        return self._db_find_by_key(idempotency_key)

    def _db_find_by_key(self, idempotency_key: str) -> PrintJob | None:
        """Read a keyed job row straight from SQLite (any status)."""
        if self._db is None:
            return None
        row = self._db.execute(
            """SELECT id, file_name, printer_name, status, submitted_by,
                      priority, created_at, started_at, completed_at, error,
                      metadata, idempotency_key
               FROM jobs WHERE idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return PrintJob(
            id=row[0],
            file_name=row[1],
            printer_name=row[2],
            status=JobStatus(row[3]),
            submitted_by=row[4],
            priority=row[5],
            created_at=row[6],
            started_at=row[7],
            completed_at=row[8],
            error=row[9],
            metadata=json.loads(row[10]) if row[10] else {},
            idempotency_key=row[11],
        )

    @staticmethod
    def _check_replay_matches(
        existing: PrintJob,
        file_name: str,
        printer_name: str | None,
        priority: int,
    ) -> None:
        """Refuse a key reuse whose canonical parameters differ."""
        if (
            existing.file_name != file_name
            or existing.printer_name != printer_name
            or existing.priority != priority
        ):
            raise IdempotencyConflict(
                existing.idempotency_key or "", existing.id
            )

    def cancel(self, job_id: str) -> PrintJob:
        """Cancel a queued or running job."""
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

    def mark_starting(self, job_id: str, printer_name: str | None = None) -> PrintJob:
        """Mark a job as starting (being sent to the printer)."""
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.STARTING)
            job.status = JobStatus.STARTING
            job.started_at = time.time()
            if printer_name:
                job.metadata = {**job.metadata, "dispatched_to": printer_name}
        self._update_job_db(job)
        if printer_name:
            self._update_metadata_db(job)
        return job

    def mark_printing(self, job_id: str) -> PrintJob:
        """Mark a job as actively printing."""
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.PRINTING)
            job.status = JobStatus.PRINTING
            if job.started_at is None:
                job.started_at = time.time()
        self._update_job_db(job)
        return job

    def mark_completed(self, job_id: str) -> PrintJob:
        """Mark a job as successfully completed."""
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.COMPLETED)
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
        self._update_job_db(job)
        return job

    def mark_failed(self, job_id: str, error: str) -> PrintJob:
        """Mark a job as failed with an error message."""
        with self._lock:
            job = self._get(job_id)
            JobStateMachine.validate(job.id, job.status, JobStatus.FAILED)
            job.status = JobStatus.FAILED
            job.completed_at = time.time()
            job.error = error
        self._update_job_db(job)
        return job

    def mark_paused(self, job_id: str) -> PrintJob:
        """Mark a job as paused."""
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
        """Return a job by ID."""
        with self._lock:
            return self._get(job_id)

    def list_jobs(
        self,
        status: JobStatus | None = None,
        printer_name: str | None = None,
        limit: int = 100,
    ) -> list[PrintJob]:
        """Return jobs matching the given filters."""
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
        """Return the next queued job to execute."""
        candidates = self.queued_for(printer_name)
        return candidates[0] if candidates else None

    def queued_for(self, printer_name: str | None = None) -> list[PrintJob]:
        """Every QUEUED job *printer_name* could take, in the order the queue runs them: highest priority first, then oldest."""
        with self._lock:
            candidates = [j for j in self._jobs.values() if j.status == JobStatus.QUEUED]

        if printer_name is not None:
            candidates = [j for j in candidates if j.printer_name is None or j.printer_name == printer_name]

        candidates.sort(key=lambda j: (-j.priority, j.created_at))
        return candidates

    def unfinished(self) -> list[PrintJob]:
        """Every job a printer may be working on: STARTING, PRINTING or PAUSED."""
        live = (JobStatus.STARTING, JobStatus.PRINTING, JobStatus.PAUSED)
        with self._lock:
            return [j for j in self._jobs.values() if j.status in live]

    def requeue(self, job_id: str) -> PrintJob:
        """Put a job Kiln tried to start back in line, untouched."""
        with self._lock:
            job = self._get(job_id)
            if job.status != JobStatus.STARTING:
                return job
            job.status = JobStatus.QUEUED
            job.started_at = None
            job.error = None
        self._update_job_db(job)
        return job

    def names_printer(self, printer_name: str) -> bool:
        """Whether any job, in memory or on disk, is aimed at *printer_name*."""
        wanted = str(printer_name or "").lower()
        if any(str(j.printer_name or "").lower() == wanted for j in self._jobs.values()):
            return True
        if self._db is None:
            return False
        row = self._db.execute("SELECT 1 FROM jobs WHERE lower(printer_name) = ? LIMIT 1", (wanted,)).fetchone()
        return row is not None

    def follow_rename(self, old_name: str, new_name: str) -> int:
        """Re-aim, in memory only, every job that names *old_name* at *new_name*."""
        if not old_name or not new_name or old_name == new_name:
            raise ValueError("a rename needs two different names")
        with self._lock:
            held = [j for j in self._jobs.values() if j.printer_name == old_name]
            for job in held:
                job.printer_name = new_name
            return len(held)

    def rename_printer(self, old_name: str, new_name: str) -> int:
        """Re-aim every job that names *old_name* at *new_name*, in memory and on disk together."""
        if not old_name or not new_name or old_name == new_name:
            raise ValueError("a rename needs two different names")
        with self._lock:
            held = [j for j in self._jobs.values() if j.printer_name == old_name]
            if new_name.lower() != old_name.lower() and self.names_printer(new_name):
                raise ValueError(f"{new_name!r} already has jobs of its own; renaming onto it would merge two printers' queues")
            if self._db is not None:
                cur = self._db.execute(
                    "UPDATE jobs SET printer_name = ? WHERE printer_name = ?", (new_name, old_name)
                )
                self._db.commit()
                moved = int(cur.rowcount or 0)
            else:
                moved = len(held)
            for job in held:
                job.printer_name = new_name
        return moved

    def update_metadata(self, job_id: str, updates: dict[str, Any]) -> None:
        """Merge *updates* into a job's metadata and write it to the store."""
        try:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.metadata = {**job.metadata, **updates}
            self._update_metadata_db(job)
        except Exception:  # noqa: BLE001 -- bookkeeping never breaks the queue
            logger.debug("metadata for %s not recorded", job_id, exc_info=True)

    def note_waiting(self, job_id: str, waiting: dict[str, Any] | None) -> None:
        """Record why a queued job has not started, or clear it (``None``)."""
        try:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                current = job.metadata.get("waiting")
                if current == waiting:
                    return
                metadata = dict(job.metadata)
                if waiting is None:
                    metadata.pop("waiting", None)
                else:
                    metadata["waiting"] = dict(waiting)
                job.metadata = metadata
            self._update_metadata_db(job)
        except Exception:  # noqa: BLE001 -- a note never breaks the queue
            logger.debug("waiting note for %s not recorded", job_id, exc_info=True)

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

    @property
    def has_durable_store(self) -> bool:
        """Whether a SQLite database backs this queue."""
        return self._db is not None

    def list_finished_jobs(
        self,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[PrintJob]:
        """Return finished jobs from the DURABLE store, newest first."""
        terminal = (
            {status}
            if status is not None
            else {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        )
        with self._lock:
            merged: dict[str, PrintJob] = {
                j.id: j for j in self._jobs.values() if j.status in terminal
            }

        if self._db is not None:
            values = tuple(t.value for t in terminal)
            placeholders = ",".join("?" for _ in values)
            try:
                rows = self._db.execute(
                    f"""SELECT id, file_name, printer_name, status,
                               submitted_by, priority, created_at,
                               started_at, completed_at, error, metadata,
                               idempotency_key
                        FROM jobs WHERE status IN ({placeholders})""",
                    values,
                ).fetchall()
            except sqlite3.Error:
                logger.warning(
                    "could not read finished jobs from the queue database; "
                    "listing the in-memory view only",
                    exc_info=True,
                )
                rows = []
            for row in rows:
                if row[0] in merged:
                    continue  # memory wins: it may be newer than the row
                merged[row[0]] = PrintJob(
                    id=row[0],
                    file_name=row[1],
                    printer_name=row[2],
                    status=JobStatus(row[3]),
                    submitted_by=row[4],
                    priority=row[5],
                    created_at=row[6],
                    started_at=row[7],
                    completed_at=row[8],
                    error=row[9],
                    metadata=json.loads(row[10]) if row[10] else {},
                    idempotency_key=row[11],
                )

        finished = sorted(
            merged.values(),
            key=lambda j: j.completed_at if j.completed_at is not None else j.created_at,
            reverse=True,
        )
        return finished[:limit]

    def count_finished_on_disk(self, status: JobStatus | None = None) -> int | None:
        """How many FINISHED jobs the queue database holds."""
        if self._db is None:
            return None
        terminal = (
            (status.value,)
            if status is not None
            else (
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            )
        )
        placeholders = ",".join("?" for _ in terminal)
        try:
            row = self._db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({placeholders})",
                terminal,
            ).fetchone()
        except sqlite3.Error:
            logger.warning("could not count finished jobs on disk", exc_info=True)
            return None
        return int(row[0]) if row else 0

    def summary(self) -> dict[str, int]:
        """Return a count of jobs per status."""
        with self._lock:
            counts: dict[str, int] = {}
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

    # ------------------------------------------------------------------
    # SQLite persistence helpers
    # ------------------------------------------------------------------

    def _persist_job(self, job: PrintJob) -> None:
        """Write a new job to SQLite."""
        if self._db is None:
            return
        try:
            self._db.execute(
                """INSERT INTO jobs
                   (id, file_name, printer_name, status, submitted_by,
                    priority, created_at, started_at, completed_at, error,
                    metadata, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    job.idempotency_key,
                ),
            )
            self._db.commit()
        except sqlite3.IntegrityError:
            raise
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

    def _update_metadata_db(self, job: PrintJob) -> None:
        """Write a job's metadata to SQLite."""
        if self._db is None:
            return
        try:
            self._db.execute("UPDATE jobs SET metadata=? WHERE id=?", (json.dumps(job.metadata), job.id))
            self._db.commit()
        except Exception:
            logger.exception("Failed to update metadata for job %s in SQLite", job.id)

    def _reload_from_db(self) -> None:
        """Reload non-terminal jobs from SQLite on startup."""
        if self._db is None:
            return
        cursor = self._db.execute(
            """SELECT id, file_name, printer_name, status, submitted_by,
                      priority, created_at, started_at, completed_at, error,
                      metadata, idempotency_key
               FROM jobs
               WHERE status IN ('queued', 'starting', 'printing', 'paused')"""
        )
        recovered = 0
        for row in cursor.fetchall():
            job = PrintJob(
                id=row[0],
                file_name=row[1],
                printer_name=row[2],
                status=JobStatus(row[3]),
                submitted_by=row[4],
                priority=row[5],
                created_at=row[6],
                started_at=row[7],
                completed_at=row[8],
                error=row[9],
                metadata=json.loads(row[10]) if row[10] else {},
                idempotency_key=row[11],
            )
            self._jobs[job.id] = job
            if job.idempotency_key:
                self._idem_index[job.idempotency_key] = job.id
            recovered += 1
        if recovered:
            logger.info("Recovered %d job(s) from SQLite after restart", recovered)
