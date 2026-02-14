"""Queue/job management tool plugin.

Extracts the job queue MCP tools from server.py into a focused plugin
module.  Provides submit_job, job_status, queue_summary, cancel_job,
and job_history tools.

Discovered and registered automatically by
:func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone functions — importable for direct calls and testing.
#
# Each function reads _queue, _event_bus, etc. from kiln.server at call time
# (via module attribute access) so that monkeypatching works in tests.
# ---------------------------------------------------------------------------


def submit_job(
    file_name: str,
    printer_name: str | None = None,
    priority: int = 0,
) -> dict:
    """Submit a print job to the queue.

    Free tier allows up to 10 queued jobs for single-printer use.
    Pro tier unlocks unlimited queue depth with multi-printer scheduling.

    Args:
        file_name: G-code file name (must already exist on the printer).
        printer_name: Target printer name, or omit to let the scheduler
            pick any idle printer.
        priority: Higher values are scheduled first (default 0).

    Jobs are executed in priority order, with FIFO tie-breaking.
    Use ``job_status`` to check progress and ``queue_summary`` for an overview.
    """
    import kiln.server as _srv
    from kiln.events import Event, EventType
    from kiln.licensing import FREE_TIER_MAX_QUEUED_JOBS, LicenseTier, get_tier

    if err := _srv._check_auth("queue"):
        return err
    # Free-tier queue cap: limit pending jobs.
    current_tier = get_tier()
    if current_tier < LicenseTier.PRO:
        pending = _srv._queue.pending_count()
        if pending >= FREE_TIER_MAX_QUEUED_JOBS:
            return {
                "success": False,
                "error": (
                    f"Job queue is limited to {FREE_TIER_MAX_QUEUED_JOBS} pending jobs on the Free tier "
                    f"(you have {pending}). Wait for jobs to complete, "
                    "or upgrade to Kiln Pro for unlimited queue depth with multi-printer scheduling. "
                    "Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
                ),
                "code": "FREE_TIER_LIMIT",
                "pending_count": pending,
                "max_allowed": FREE_TIER_MAX_QUEUED_JOBS,
                "upgrade_url": "https://kiln3d.com/pro",
            }
    try:
        job_id = _srv._queue.submit(
            file_name=file_name,
            printer_name=printer_name,
            submitted_by="mcp-agent",
            priority=priority,
        )
        _srv._event_bus.publish(Event(
            type=EventType.JOB_QUEUED,
            data={"job_id": job_id, "file_name": file_name, "printer_name": printer_name},
            source="mcp",
        ))
        return {
            "success": True,
            "job_id": job_id,
            "message": f"Job {job_id} submitted to queue.",
        }
    except Exception as exc:
        _logger.exception("Unexpected error in submit_job")
        return _srv._error_dict(
            f"Failed to submit job for '{file_name}': {exc}. "
            "Verify the file exists on the printer with 'printer_files()'.",
            code="INTERNAL_ERROR",
        )


def job_status(job_id: str) -> dict:
    """Get the status of a queued or completed print job.

    Args:
        job_id: The job ID returned by ``submit_job``.

    Returns the full job record including status, timing, and metadata.
    """
    import kiln.server as _srv
    from kiln.queue import JobNotFoundError

    try:
        job = _srv._queue.get_job(job_id)
        return {
            "success": True,
            "job": job.to_dict(),
        }
    except JobNotFoundError:
        return _srv._error_dict(f"Job not found: {job_id!r}", code="NOT_FOUND")
    except Exception as exc:
        _logger.exception("Unexpected error in job_status")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def queue_summary() -> dict:
    """Get an overview of the print job queue.

    Returns counts by status, next job to execute, and recent jobs.
    """
    import kiln.server as _srv

    try:
        summary = _srv._queue.summary()
        next_job = _srv._queue.next_job()
        recent = _srv._queue.list_jobs(limit=10)
        pending = _srv._queue.pending_count()
        active = _srv._queue.active_count()
        total = _srv._queue.total_count
        registered_printers = _srv._registry.count
        dispatch_blocked = pending > 0 and active == 0 and registered_printers == 0
        dispatch_block_reason = (
            "Jobs are queued but no printers are registered. "
            "Register at least one printer with register_printer()."
            if dispatch_blocked
            else None
        )
        return {
            "success": True,
            "counts": summary,
            "pending": pending,
            "active": active,
            "total": total,
            "next_job": next_job.to_dict() if next_job else None,
            "recent_jobs": [j.to_dict() for j in recent],
            "registered_printers": registered_printers,
            "dispatch_blocked": dispatch_blocked,
            "dispatch_block_reason": dispatch_block_reason,
        }
    except Exception as exc:
        _logger.exception("Unexpected error in queue_summary")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running print job.

    Args:
        job_id: The job ID to cancel.

    Only jobs in QUEUED or PRINTING state can be cancelled.
    """
    import kiln.server as _srv
    from kiln.events import Event, EventType
    from kiln.queue import JobNotFoundError

    if err := _srv._check_auth("queue"):
        return err
    try:
        job = _srv._queue.cancel(job_id)
        _srv._event_bus.publish(Event(
            type=EventType.JOB_CANCELLED,
            data={"job_id": job_id},
            source="mcp",
        ))
        return {
            "success": True,
            "job": job.to_dict(),
            "message": f"Job {job_id} cancelled.",
        }
    except JobNotFoundError:
        return _srv._error_dict(f"Job not found: {job_id!r}", code="NOT_FOUND")
    except ValueError as exc:
        return _srv._error_dict(
            f"Cannot cancel job {job_id!r}: {exc}. Only jobs in QUEUED or PRINTING state can be cancelled.",
            code="INVALID_STATE",
        )
    except Exception as exc:
        _logger.exception("Unexpected error in cancel_job")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def _job_history(limit: int = 20, status: str | None = None) -> dict:
    """Get history of completed, failed, and cancelled print jobs.

    Args:
        limit: Maximum number of jobs to return (default 20, max 100).
        status: Optional filter by status -- "completed", "failed", or
            "cancelled".  Omit to show all finished jobs.

    Returns recent job records from newest to oldest.
    """
    import kiln.server as _srv
    from kiln.queue import JobStatus

    try:
        capped = min(max(limit, 1), 100)
        all_jobs = _srv._queue.list_jobs(limit=capped)

        finished_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        if status:
            status_map = {
                "completed": JobStatus.COMPLETED,
                "failed": JobStatus.FAILED,
                "cancelled": JobStatus.CANCELLED,
            }
            target = status_map.get(status.lower())
            if target is None:
                return _srv._error_dict(
                    f"Invalid status filter: {status!r}. Use 'completed', 'failed', or 'cancelled'.",
                    code="INVALID_ARGS",
                )
            jobs = [j for j in all_jobs if j.status == target]
        else:
            jobs = [j for j in all_jobs if j.status in finished_statuses]

        return {
            "success": True,
            "jobs": [j.to_dict() for j in jobs],
            "count": len(jobs),
        }
    except Exception as exc:
        _logger.exception("Unexpected error in job_history")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Plugin class — registers standalone functions as MCP tools.
# ---------------------------------------------------------------------------


class _QueueToolsPlugin:
    """MCP tools for print job queue management.

    Covers job submission, status queries, queue overview, cancellation,
    and history retrieval.
    """

    @property
    def name(self) -> str:
        return "queue_tools"

    @property
    def description(self) -> str:
        return "Print job queue management tools (submit, status, cancel, history)"

    def register(self, mcp: Any) -> None:
        """Register queue/job tools with the MCP server."""

        mcp.tool()(submit_job)
        mcp.tool()(job_status)
        mcp.tool()(queue_summary)
        mcp.tool()(cancel_job)

        @mcp.tool()
        def job_history(limit: int = 20, status: str | None = None) -> dict:
            """Get history of completed, failed, and cancelled print jobs.

            Args:
                limit: Maximum number of jobs to return (default 20, max 100).
                status: Optional filter by status -- "completed", "failed", or
                    "cancelled".  Omit to show all finished jobs.

            Returns recent job records from newest to oldest.
            """
            return _job_history(limit=limit, status=status)

        _logger.debug("Registered queue/job management tools")


# Public alias for CLI imports
job_history = _job_history

plugin = _QueueToolsPlugin()
