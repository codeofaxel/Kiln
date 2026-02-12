"""Fleet orchestrator — manages print job assignment across multiple FDM printers.

The fleet orchestrator sits above the job queue and printer registry.  It
accepts print jobs, selects the best available printer for each job, monitors
progress across the fleet, and handles failures by re-assigning jobs to
alternative printers when possible.

Unlike the lower-level :mod:`kiln.queue` (which tracks individual jobs) and
:mod:`kiln.registry` (which tracks individual printers), the orchestrator
provides a unified view of fleet-wide operations and makes scheduling
decisions.

The orchestrator does NOT communicate with printers directly — it delegates
to the registry and queue, keeping a clean separation of concerns.

Example::

    orch = get_fleet_orchestrator()
    job_id = orch.submit_job("/path/to/benchy.gcode", submitted_by="agent-claude")
    status = orch.get_job_status(job_id)
    orch.cancel_job(job_id, reason="wrong filament loaded")
    utilization = orch.get_fleet_utilization()
"""

from __future__ import annotations

import enum
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Base exception for fleet orchestration failures.

    :param message: Human-readable error description.
    :param cause: Original exception that triggered this error, if any.
    """

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


class JobNotFoundError(OrchestratorError):
    """Raised when referencing a job ID that does not exist."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Orchestrated job not found: {job_id!r}")
        self.job_id = job_id


class NoPrinterAvailableError(OrchestratorError):
    """Raised when no suitable printer is available for a job."""

    def __init__(self, message: str = "No idle printer available for this job") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OrchestratedJobStatus(enum.Enum):
    """Lifecycle states for an orchestrated print job.

    These mirror the queue's :class:`~kiln.queue.JobStatus` but add
    ``ASSIGNED`` to represent the window between printer selection and
    print start.
    """

    QUEUED = "queued"
    ASSIGNED = "assigned"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class OrchestratedJob:
    """Tracks a single print job through the fleet orchestration lifecycle.

    :param job_id: Unique identifier for this orchestrated job.
    :param file_path: Absolute path to the G-code file to print.
    :param printer_name: Name of the assigned printer (None while queued).
    :param status: Current lifecycle state.
    :param submitted_at: Unix timestamp when the job was submitted.
    :param started_at: Unix timestamp when printing began.
    :param completed_at: Unix timestamp when the job finished or failed.
    :param submitted_by: Identifier of the agent or user that submitted the job.
    :param priority: Scheduling priority (higher = more urgent).
    :param error: Human-readable error message if the job failed.
    :param attempt: Current attempt number (increments on reassignment).
    :param max_attempts: Maximum number of printer assignments before giving up.
    :param preferred_printer: Optional printer name to prefer for assignment.
    :param failed_printers: Set of printer names that already failed this job.
    :param metadata: Arbitrary key-value pairs for caller use.
    """

    job_id: str
    file_path: str
    printer_name: Optional[str] = None
    status: OrchestratedJobStatus = OrchestratedJobStatus.QUEUED
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    submitted_by: str = "unknown"
    priority: int = 0
    error: Optional[str] = None
    attempt: int = 0
    max_attempts: int = 3
    preferred_printer: Optional[str] = None
    failed_printers: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        Enum values are converted to strings and sets to sorted lists.
        """
        return {
            "job_id": self.job_id,
            "file_path": self.file_path,
            "printer_name": self.printer_name,
            "status": self.status.value,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "submitted_by": self.submitted_by,
            "priority": self.priority,
            "error": self.error,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "preferred_printer": self.preferred_printer,
            "failed_printers": sorted(self.failed_printers),
            "metadata": self.metadata,
        }

    @property
    def elapsed_seconds(self) -> Optional[float]:
        """Seconds since the job started printing, or None if not started."""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return round(end - self.started_at, 1)

    @property
    def wait_seconds(self) -> float:
        """Seconds the job has been waiting since submission."""
        start = self.started_at or time.time()
        return round(start - self.submitted_at, 1)

    @property
    def is_terminal(self) -> bool:
        """Whether the job has reached a final state."""
        return self.status in (
            OrchestratedJobStatus.COMPLETED,
            OrchestratedJobStatus.FAILED,
            OrchestratedJobStatus.CANCELLED,
        )


@dataclass
class AssignmentResult:
    """Result of attempting to assign a job to a printer.

    :param success: Whether assignment succeeded.
    :param printer_name: Name of the assigned printer (None on failure).
    :param message: Human-readable explanation.
    """

    success: bool
    printer_name: Optional[str] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "success": self.success,
            "printer_name": self.printer_name,
            "message": self.message,
        }


@dataclass
class FleetUtilization:
    """Snapshot of fleet-wide utilization metrics.

    :param total_printers: Number of registered printers.
    :param idle_printers: Number of printers not currently printing.
    :param busy_printers: Number of printers actively printing.
    :param offline_printers: Number of printers that are unreachable.
    :param error_printers: Number of printers in an error state.
    :param queued_jobs: Number of jobs waiting for a printer.
    :param active_jobs: Number of jobs currently printing.
    :param completed_jobs: Number of jobs that finished successfully.
    :param failed_jobs: Number of jobs that failed.
    :param cancelled_jobs: Number of jobs that were cancelled.
    :param utilization_pct: Percentage of printers currently busy.
    """

    total_printers: int = 0
    idle_printers: int = 0
    busy_printers: int = 0
    offline_printers: int = 0
    error_printers: int = 0
    queued_jobs: int = 0
    active_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    utilization_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Printer selection strategy
# ---------------------------------------------------------------------------


class PrinterSelector:
    """Selects the best available printer for a job.

    The default strategy is simple: prefer the job's ``preferred_printer``
    if it is idle, otherwise pick the first idle printer that has not
    already failed this job.  Subclass or replace with a custom callable
    for more sophisticated strategies (e.g. load balancing, material
    matching, build-volume filtering).
    """

    def select(
        self,
        job: OrchestratedJob,
        idle_printers: List[str],
    ) -> Optional[str]:
        """Choose a printer for *job* from the list of idle printers.

        :param job: The job that needs a printer.
        :param idle_printers: Names of printers currently idle.
        :returns: Printer name, or ``None`` if no suitable printer is available.
        """
        # Filter out printers that already failed this job.
        candidates = [
            p for p in idle_printers
            if p not in job.failed_printers
        ]
        if not candidates:
            return None

        # Prefer the explicitly requested printer if available.
        if job.preferred_printer and job.preferred_printer in candidates:
            return job.preferred_printer

        # Default: first available (stable ordering from registry).
        return candidates[0]


# ---------------------------------------------------------------------------
# Fleet orchestrator
# ---------------------------------------------------------------------------


class FleetOrchestrator:
    """Orchestrates print job assignment and monitoring across an FDM printer fleet.

    The orchestrator is the top-level coordinator for multi-printer
    operations.  It maintains its own job registry (separate from the
    lower-level :class:`~kiln.queue.PrintQueue`) and delegates printer
    interaction to the :class:`~kiln.registry.PrinterRegistry`.

    Thread safety: all public methods acquire ``_lock`` before mutating
    internal state, so the orchestrator is safe to call from MCP tool
    handlers running on concurrent threads.

    :param registry: Optional :class:`PrinterRegistry` instance.  If not
        provided, the orchestrator will lazily import and use the global
        registry from :mod:`kiln.registry`.
    :param event_bus: Optional :class:`EventBus` instance for publishing
        orchestration events.
    :param selector: Optional :class:`PrinterSelector` for custom printer
        selection logic.  Defaults to :class:`PrinterSelector`.
    """

    def __init__(
        self,
        *,
        registry: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        selector: Optional[PrinterSelector] = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._selector = selector or PrinterSelector()
        self._jobs: Dict[str, OrchestratedJob] = {}
        self._lock = threading.Lock()
        self._printer_jobs: Dict[str, str] = {}  # printer_name -> job_id

    # ------------------------------------------------------------------
    # Lazy accessors for kiln subsystems
    # ------------------------------------------------------------------

    def _get_registry(self) -> Any:
        """Lazily resolve the printer registry."""
        if self._registry is None:
            from kiln.registry import PrinterRegistry

            self._registry = PrinterRegistry()
            logger.debug("Fleet orchestrator created default PrinterRegistry")
        return self._registry

    def _get_event_bus(self) -> Optional[Any]:
        """Lazily resolve the event bus (returns None if unavailable)."""
        if self._event_bus is None:
            try:
                from kiln.events import EventBus

                self._event_bus = EventBus()
                logger.debug("Fleet orchestrator created default EventBus")
            except ImportError:
                logger.debug("EventBus not available; events will not be published")
                return None
        return self._event_bus

    def _publish_event(self, event_type_name: str, data: Dict[str, Any]) -> None:
        """Publish an event to the bus if available.

        :param event_type_name: String name of the :class:`EventType` member
            (e.g. ``"JOB_SUBMITTED"``).
        :param data: Event payload dictionary.
        """
        bus = self._get_event_bus()
        if bus is None:
            return
        try:
            from kiln.events import Event, EventType

            event_type = EventType[event_type_name]
            bus.publish(Event(
                type=event_type,
                data=data,
                source="fleet_orchestrator",
            ))
        except (KeyError, Exception) as exc:
            logger.debug(
                "Failed to publish event %s: %s", event_type_name, exc
            )

    # ------------------------------------------------------------------
    # Job submission
    # ------------------------------------------------------------------

    def submit_job(
        self,
        file_path: str,
        *,
        submitted_by: str = "unknown",
        priority: int = 0,
        preferred_printer: Optional[str] = None,
        max_attempts: int = 3,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a print job to the fleet orchestrator.

        The job enters ``QUEUED`` state.  Call :meth:`assign_jobs` to
        trigger printer selection, or use :meth:`submit_and_assign` to
        submit and immediately attempt assignment.

        :param file_path: Absolute path to the G-code file.
        :param submitted_by: Identifier of the submitting agent or user.
        :param priority: Scheduling priority (higher = more urgent).
        :param preferred_printer: Optional printer to prefer for this job.
        :param max_attempts: Maximum assignment attempts before permanent failure.
        :param metadata: Arbitrary key-value pairs attached to the job.
        :returns: The unique job ID.
        :raises OrchestratorError: If *file_path* is empty.
        """
        if not file_path or not file_path.strip():
            raise OrchestratorError("file_path must not be empty")

        job_id = str(uuid.uuid4())
        job = OrchestratedJob(
            job_id=job_id,
            file_path=file_path.strip(),
            submitted_by=submitted_by,
            priority=priority,
            preferred_printer=preferred_printer,
            max_attempts=max_attempts,
            metadata=metadata or {},
        )

        with self._lock:
            self._jobs[job_id] = job

        logger.info(
            "Submitted job %s: file=%s, submitted_by=%s, priority=%d",
            job_id, file_path, submitted_by, priority,
        )
        self._publish_event("JOB_SUBMITTED", {
            "job_id": job_id,
            "file_path": file_path,
            "submitted_by": submitted_by,
        })
        return job_id

    def submit_and_assign(
        self,
        file_path: str,
        *,
        submitted_by: str = "unknown",
        priority: int = 0,
        preferred_printer: Optional[str] = None,
        max_attempts: int = 3,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AssignmentResult:
        """Submit a job and immediately attempt to assign it to a printer.

        Convenience method combining :meth:`submit_job` and
        :meth:`assign_job`.  If no printer is available, the job remains
        in ``QUEUED`` state for later assignment.

        :param file_path: Absolute path to the G-code file.
        :param submitted_by: Identifier of the submitting agent or user.
        :param priority: Scheduling priority (higher = more urgent).
        :param preferred_printer: Optional printer to prefer for this job.
        :param max_attempts: Maximum assignment attempts before permanent failure.
        :param metadata: Arbitrary key-value pairs attached to the job.
        :returns: :class:`AssignmentResult` with the outcome.
        """
        job_id = self.submit_job(
            file_path,
            submitted_by=submitted_by,
            priority=priority,
            preferred_printer=preferred_printer,
            max_attempts=max_attempts,
            metadata=metadata,
        )
        return self.assign_job(job_id)

    # ------------------------------------------------------------------
    # Job assignment
    # ------------------------------------------------------------------

    def assign_job(self, job_id: str) -> AssignmentResult:
        """Attempt to assign a specific queued job to an available printer.

        The printer selector chooses the best candidate from the idle
        printers in the registry.  If assignment succeeds, the job
        transitions to ``ASSIGNED`` state.

        :param job_id: ID of the job to assign.
        :returns: :class:`AssignmentResult` indicating success or failure.
        :raises JobNotFoundError: If *job_id* does not exist.
        """
        with self._lock:
            job = self._get_job(job_id)

            if job.status != OrchestratedJobStatus.QUEUED:
                return AssignmentResult(
                    success=False,
                    message=(
                        f"Job {job_id} is {job.status.value}, "
                        f"only QUEUED jobs can be assigned"
                    ),
                )

            registry = self._get_registry()
            try:
                idle_names = registry.get_idle_printers()
            except Exception as exc:
                logger.warning(
                    "Failed to query idle printers: %s", exc
                )
                return AssignmentResult(
                    success=False,
                    message=f"Failed to query printer fleet: {exc}",
                )

            printer_name = self._selector.select(job, idle_names)
            if printer_name is None:
                return AssignmentResult(
                    success=False,
                    message="No suitable idle printer available",
                )

            # Assign the job.
            job.printer_name = printer_name
            job.status = OrchestratedJobStatus.ASSIGNED
            job.attempt += 1
            self._printer_jobs[printer_name] = job_id

        logger.info(
            "Assigned job %s to printer %r (attempt %d/%d)",
            job_id, printer_name, job.attempt, job.max_attempts,
        )
        self._publish_event("JOB_STARTED", {
            "job_id": job_id,
            "printer_name": printer_name,
            "file_path": job.file_path,
        })
        return AssignmentResult(
            success=True,
            printer_name=printer_name,
            message=f"Assigned to {printer_name}",
        )

    def assign_jobs(self) -> List[AssignmentResult]:
        """Attempt to assign all queued jobs to available printers.

        Jobs are processed in priority order (highest first), then by
        submission time (oldest first).

        :returns: List of :class:`AssignmentResult` for each queued job.
        """
        with self._lock:
            queued = [
                j for j in self._jobs.values()
                if j.status == OrchestratedJobStatus.QUEUED
            ]

        # Sort: highest priority first, then oldest first.
        queued.sort(key=lambda j: (-j.priority, j.submitted_at))

        results: List[AssignmentResult] = []
        for job in queued:
            result = self.assign_job(job.job_id)
            results.append(result)
            # Stop if we ran out of printers.
            if not result.success and "No suitable" in result.message:
                # Remaining jobs would also fail; skip them.
                for remaining in queued[len(results):]:
                    results.append(AssignmentResult(
                        success=False,
                        message="No idle printers remaining",
                    ))
                break

        return results

    # ------------------------------------------------------------------
    # Job lifecycle transitions
    # ------------------------------------------------------------------

    def mark_printing(self, job_id: str) -> None:
        """Transition a job from ``ASSIGNED`` to ``PRINTING``.

        Call this once the printer has acknowledged the print start
        command and is actively printing.

        :param job_id: ID of the job.
        :raises JobNotFoundError: If *job_id* does not exist.
        :raises OrchestratorError: If the job is not in ``ASSIGNED`` state.
        """
        with self._lock:
            job = self._get_job(job_id)
            if job.status != OrchestratedJobStatus.ASSIGNED:
                raise OrchestratorError(
                    f"Job {job_id} is {job.status.value}, expected ASSIGNED. "
                    f"Only assigned jobs can transition to PRINTING."
                )
            job.status = OrchestratedJobStatus.PRINTING
            job.started_at = time.time()

        logger.info("Job %s is now PRINTING on %s", job_id, job.printer_name)
        self._publish_event("PRINT_STARTED", {
            "job_id": job_id,
            "printer_name": job.printer_name,
            "file_path": job.file_path,
        })

    def mark_completed(self, job_id: str) -> None:
        """Transition a job to ``COMPLETED``.

        Releases the printer for new work.

        :param job_id: ID of the job.
        :raises JobNotFoundError: If *job_id* does not exist.
        :raises OrchestratorError: If the job is already in a terminal state.
        """
        with self._lock:
            job = self._get_job(job_id)
            if job.is_terminal:
                raise OrchestratorError(
                    f"Job {job_id} is already {job.status.value} and cannot "
                    f"be marked completed."
                )
            job.status = OrchestratedJobStatus.COMPLETED
            job.completed_at = time.time()
            self._release_printer(job)

        logger.info(
            "Job %s COMPLETED on %s (%.1fs elapsed)",
            job_id, job.printer_name,
            job.elapsed_seconds or 0,
        )
        self._publish_event("PRINT_COMPLETED", {
            "job_id": job_id,
            "printer_name": job.printer_name,
            "file_path": job.file_path,
            "elapsed_seconds": job.elapsed_seconds,
        })

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed and attempt reassignment if retries remain.

        If the job has not exhausted its ``max_attempts``, the printer is
        recorded in ``failed_printers`` and the job returns to ``QUEUED``
        state for reassignment to a different printer.  Otherwise, the
        job transitions to ``FAILED``.

        :param job_id: ID of the job.
        :param error: Human-readable error description.
        :raises JobNotFoundError: If *job_id* does not exist.
        :raises OrchestratorError: If the job is already in a terminal state.
        """
        with self._lock:
            job = self._get_job(job_id)
            if job.is_terminal:
                raise OrchestratorError(
                    f"Job {job_id} is already {job.status.value} and cannot "
                    f"be marked failed."
                )

            failed_printer = job.printer_name

            # Record the failed printer.
            if failed_printer:
                job.failed_printers.add(failed_printer)
            self._release_printer(job)

            if job.attempt < job.max_attempts:
                # Return to queue for reassignment.
                job.status = OrchestratedJobStatus.QUEUED
                job.printer_name = None
                job.error = None
                logger.warning(
                    "Job %s failed on %s (attempt %d/%d): %s — re-queuing",
                    job_id, failed_printer, job.attempt, job.max_attempts, error,
                )
                self._publish_event("JOB_FAILED", {
                    "job_id": job_id,
                    "printer_name": failed_printer,
                    "error": error,
                    "will_retry": True,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                })
                return

            # All attempts exhausted.
            job.status = OrchestratedJobStatus.FAILED
            job.error = error
            job.completed_at = time.time()

        logger.error(
            "Job %s permanently FAILED after %d attempts: %s",
            job_id, job.attempt, error,
        )
        self._publish_event("JOB_FAILED", {
            "job_id": job_id,
            "printer_name": failed_printer,
            "error": error,
            "will_retry": False,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
        })

    # ------------------------------------------------------------------
    # Job status queries
    # ------------------------------------------------------------------

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Return full status for a tracked job.

        :param job_id: Job tracking ID.
        :returns: Dict with all job fields plus computed properties.
        :raises JobNotFoundError: If the job ID is unknown.
        """
        with self._lock:
            job = self._get_job(job_id)
            result = job.to_dict()
            result["elapsed_seconds"] = job.elapsed_seconds
            result["wait_seconds"] = job.wait_seconds
            result["is_terminal"] = job.is_terminal
            return result

    def list_jobs(
        self,
        *,
        status: Optional[OrchestratedJobStatus] = None,
        printer_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return summaries of tracked jobs, optionally filtered.

        :param status: Filter to jobs in this state (None = all states).
        :param printer_name: Filter to jobs assigned to this printer.
        :param limit: Maximum number of jobs to return.
        :returns: List of job summary dicts, newest first.
        """
        with self._lock:
            jobs = list(self._jobs.values())

        # Apply filters.
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        if printer_name is not None:
            jobs = [j for j in jobs if j.printer_name == printer_name]

        # Sort by submission time, newest first.
        jobs.sort(key=lambda j: j.submitted_at, reverse=True)
        jobs = jobs[:limit]

        return [
            {
                "job_id": j.job_id,
                "file_path": j.file_path,
                "printer_name": j.printer_name,
                "status": j.status.value,
                "submitted_at": j.submitted_at,
                "submitted_by": j.submitted_by,
                "priority": j.priority,
                "attempt": j.attempt,
            }
            for j in jobs
        ]

    def get_active_jobs(self) -> List[Dict[str, Any]]:
        """Return all jobs currently assigned or printing.

        :returns: List of job detail dicts for non-terminal, non-queued jobs.
        """
        with self._lock:
            active = [
                j for j in self._jobs.values()
                if j.status in (
                    OrchestratedJobStatus.ASSIGNED,
                    OrchestratedJobStatus.PRINTING,
                )
            ]
        return [
            {
                "job_id": j.job_id,
                "file_path": j.file_path,
                "printer_name": j.printer_name,
                "status": j.status.value,
                "started_at": j.started_at,
                "elapsed_seconds": j.elapsed_seconds,
                "attempt": j.attempt,
            }
            for j in active
        ]

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_job(
        self,
        job_id: str,
        *,
        reason: str = "user requested",
    ) -> bool:
        """Cancel a job that is queued or assigned.

        Jobs that are already ``PRINTING`` should be cancelled through
        the printer adapter, then reported via :meth:`mark_failed` or
        a separate cancel acknowledgement.  Jobs in a terminal state
        cannot be cancelled.

        :param job_id: ID of the job to cancel.
        :param reason: Cancellation reason for logging and status.
        :returns: ``True`` if the job was cancelled, ``False`` if it was
            already in a terminal state.
        :raises JobNotFoundError: If the job ID is unknown.
        """
        with self._lock:
            job = self._get_job(job_id)

            if job.is_terminal:
                logger.debug(
                    "Cannot cancel job %s — already %s",
                    job_id, job.status.value,
                )
                return False

            if job.status == OrchestratedJobStatus.PRINTING:
                # Cannot cancel a printing job from the orchestrator alone;
                # the caller must also stop the printer.
                logger.warning(
                    "Cancelling PRINTING job %s on %s — caller must also "
                    "stop the printer",
                    job_id, job.printer_name,
                )

            job.status = OrchestratedJobStatus.CANCELLED
            job.error = f"Cancelled: {reason}"
            job.completed_at = time.time()
            self._release_printer(job)

        logger.info("Job %s CANCELLED: %s", job_id, reason)
        self._publish_event("JOB_CANCELLED", {
            "job_id": job_id,
            "printer_name": job.printer_name,
            "reason": reason,
        })
        return True

    # ------------------------------------------------------------------
    # Fleet utilization
    # ------------------------------------------------------------------

    def get_fleet_utilization(self) -> FleetUtilization:
        """Compute a snapshot of fleet-wide utilization metrics.

        Queries the printer registry for printer states and aggregates
        job counts from the internal job store.

        :returns: :class:`FleetUtilization` dataclass with all metrics.
        """
        registry = self._get_registry()
        util = FleetUtilization()

        # Printer metrics.
        try:
            from kiln.printers.base import PrinterStatus

            fleet_status = registry.get_fleet_status()
            util.total_printers = len(fleet_status)

            for entry in fleet_status:
                status_val = entry.get("status", "unknown")
                if status_val == PrinterStatus.IDLE.value:
                    util.idle_printers += 1
                elif status_val in (
                    PrinterStatus.PRINTING.value,
                    PrinterStatus.BUSY.value,
                ):
                    util.busy_printers += 1
                elif status_val == PrinterStatus.OFFLINE.value:
                    util.offline_printers += 1
                elif status_val == PrinterStatus.ERROR.value:
                    util.error_printers += 1
                else:
                    # PAUSED, CANCELLING, UNKNOWN — count as busy for
                    # utilization purposes.
                    util.busy_printers += 1

        except Exception as exc:
            logger.warning("Failed to query fleet status: %s", exc)

        # Job metrics.
        with self._lock:
            for job in self._jobs.values():
                if job.status == OrchestratedJobStatus.QUEUED:
                    util.queued_jobs += 1
                elif job.status in (
                    OrchestratedJobStatus.ASSIGNED,
                    OrchestratedJobStatus.PRINTING,
                ):
                    util.active_jobs += 1
                elif job.status == OrchestratedJobStatus.COMPLETED:
                    util.completed_jobs += 1
                elif job.status == OrchestratedJobStatus.FAILED:
                    util.failed_jobs += 1
                elif job.status == OrchestratedJobStatus.CANCELLED:
                    util.cancelled_jobs += 1

        # Utilization percentage.
        operable = util.total_printers - util.offline_printers
        if operable > 0:
            util.utilization_pct = round(
                (util.busy_printers / operable) * 100, 1
            )

        return util

    def get_printer_job(self, printer_name: str) -> Optional[Dict[str, Any]]:
        """Return the currently active job for a specific printer, if any.

        :param printer_name: Name of the printer to query.
        :returns: Job detail dict, or ``None`` if the printer has no active job.
        """
        with self._lock:
            job_id = self._printer_jobs.get(printer_name)
            if job_id is None:
                return None
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return None
            result = job.to_dict()
            result["elapsed_seconds"] = job.elapsed_seconds
            return result

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def cancel_all_queued(self, *, reason: str = "bulk cancellation") -> int:
        """Cancel all jobs in ``QUEUED`` state.

        :param reason: Cancellation reason.
        :returns: Number of jobs cancelled.
        """
        with self._lock:
            queued_ids = [
                j.job_id for j in self._jobs.values()
                if j.status == OrchestratedJobStatus.QUEUED
            ]

        count = 0
        for job_id in queued_ids:
            if self.cancel_job(job_id, reason=reason):
                count += 1
        return count

    def purge_completed(self, *, older_than_seconds: float = 0) -> int:
        """Remove completed/failed/cancelled jobs from the in-memory store.

        :param older_than_seconds: Only purge terminal jobs older than this
            many seconds.  ``0`` purges all terminal jobs.
        :returns: Number of jobs purged.
        """
        cutoff = time.time() - older_than_seconds
        with self._lock:
            to_remove = [
                job_id
                for job_id, job in self._jobs.items()
                if job.is_terminal
                and (job.completed_at or 0) <= cutoff
            ]
            for job_id in to_remove:
                del self._jobs[job_id]
            return len(to_remove)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_job(self, job_id: str) -> OrchestratedJob:
        """Look up a job by ID or raise :class:`JobNotFoundError`.

        Must be called with ``_lock`` held.
        """
        if job_id not in self._jobs:
            raise JobNotFoundError(job_id)
        return self._jobs[job_id]

    def _release_printer(self, job: OrchestratedJob) -> None:
        """Remove the printer-to-job mapping for a completed/failed/cancelled job.

        Must be called with ``_lock`` held.
        """
        if job.printer_name and self._printer_jobs.get(job.printer_name) == job.job_id:
            del self._printer_jobs[job.printer_name]

    @property
    def job_count(self) -> int:
        """Total number of tracked jobs (all states)."""
        with self._lock:
            return len(self._jobs)

    @property
    def queued_count(self) -> int:
        """Number of jobs in ``QUEUED`` state."""
        with self._lock:
            return sum(
                1 for j in self._jobs.values()
                if j.status == OrchestratedJobStatus.QUEUED
            )

    @property
    def active_count(self) -> int:
        """Number of jobs in ``ASSIGNED`` or ``PRINTING`` state."""
        with self._lock:
            return sum(
                1 for j in self._jobs.values()
                if j.status in (
                    OrchestratedJobStatus.ASSIGNED,
                    OrchestratedJobStatus.PRINTING,
                )
            )


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_orchestrator_instance: Optional[FleetOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_fleet_orchestrator(
    *,
    registry: Optional[Any] = None,
    event_bus: Optional[Any] = None,
    selector: Optional[PrinterSelector] = None,
) -> FleetOrchestrator:
    """Return the global :class:`FleetOrchestrator` singleton.

    On first call, creates the instance.  Subsequent calls return the
    same instance (additional keyword arguments are ignored after the
    first call).

    :param registry: Optional :class:`PrinterRegistry` to inject.
    :param event_bus: Optional :class:`EventBus` to inject.
    :param selector: Optional :class:`PrinterSelector` to inject.
    :returns: The singleton :class:`FleetOrchestrator`.
    """
    global _orchestrator_instance
    if _orchestrator_instance is not None:
        return _orchestrator_instance

    with _orchestrator_lock:
        # Double-checked locking.
        if _orchestrator_instance is not None:
            return _orchestrator_instance
        _orchestrator_instance = FleetOrchestrator(
            registry=registry,
            event_bus=event_bus,
            selector=selector,
        )
        logger.info("Fleet orchestrator singleton initialised")
        return _orchestrator_instance


def reset_fleet_orchestrator() -> None:
    """Reset the global singleton (for testing)."""
    global _orchestrator_instance
    with _orchestrator_lock:
        _orchestrator_instance = None
