"""Queue/job management tool plugin.

Extracts the job queue MCP tools from server.py into a focused plugin
module.  Provides submit_job, job_status, queue_summary,
cancel_queued_job, cancel_queued_jobs, and job_history tools.

Discovered and registered automatically by
:func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.events import Event, EventType

_logger = logging.getLogger(__name__)

# A bulk queue clear must see every queued job, not the default 100-row
# page that list_jobs returns — ask for an effectively-unbounded page.
_ALL_QUEUED_JOBS = 1_000_000

# Job states that mean the file has already reached the printer.  The queue
# can still flip any of these to CANCELLED, but doing so only rewrites the
# row — the machine keeps running.  Stopping one needs cancel_print().
# Compared by JobStatus.value so this module keeps importing kiln.queue
# lazily, the way every function below does.
_ON_THE_MACHINE = frozenset({"starting", "printing", "paused"})

#: Pending-job cap for a free install, used when kiln-pro is not present
#: to supply one.
_DEFAULT_FREE_TIER_MAX_QUEUED_JOBS = 10


def _free_tier_queue_cap() -> int:
    """How many pending jobs a free install may hold.

    Resolved through a module-level function, not inline in the caller,
    so the cap has ONE name that exists whether or not kiln-pro is
    installed.  Read inline, the free-tier branch was a local variable
    inside ``submit_job`` — reachable by no test, because the only way
    to reach it was to patch a ``kiln.licensing`` that, on a free
    install, is not importable to patch.  The cap that nearly every
    real user runs under was therefore the one the suite could not
    describe.
    """
    try:
        from kiln.licensing import FREE_TIER_MAX_QUEUED_JOBS
    except ImportError:
        return _DEFAULT_FREE_TIER_MAX_QUEUED_JOBS
    return FREE_TIER_MAX_QUEUED_JOBS


def _is_free_tier() -> bool:
    """Whether this install is below Pro, and so subject to the cap.

    A free install has no licensing module to ask, and nothing below
    free to be: absence of kiln-pro IS the free tier.
    """
    try:
        from kiln.licensing import LicenseTier, get_tier
    except ImportError:
        return True
    current = get_tier()
    return current is not None and current < LicenseTier.PRO


# ---------------------------------------------------------------------------
# Standalone functions — importable for direct calls and testing.
#
# Each function resolves the queue via kiln.server._get_queue() (and reads
# _event_bus, etc. as module attributes) at call time, so monkeypatching works
# in tests AND the queue is lazily created if a server context never
# initialised it (e.g. the REST/local-admin server, which used to leave the raw
# _queue global None and crash every queue tool with AttributeError).
# ---------------------------------------------------------------------------


def submit_job(
    file_name: str,
    printer_name: str | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> dict:
    """Submit a print job to the queue.

    Free tier allows up to 10 queued jobs for single-printer use.
    Pro tier unlocks unlimited queue depth with multi-printer scheduling.

    Args:
        file_name: G-code file name (must already exist on the printer).
        printer_name: Target printer name, or omit to let the scheduler
            pick any idle printer.
        priority: Higher values are scheduled first (default 0).
        idempotency_key: Optional opaque key (e.g. a UUID you generate)
            naming this one submission.  If your call fails in a way
            where you cannot tell whether the job was queued — a timeout,
            a dropped connection — retry with the SAME key: you will get
            the original job back (``submission: "replayed"``) instead of
            queuing a duplicate print.  Use a NEW key for each job you
            genuinely want printed; reusing a key with different
            parameters is refused.

    Jobs are executed in priority order, with FIFO tie-breaking.
    Use ``job_status`` to check progress and ``queue_summary`` for an overview.
    """
    import kiln.server as _srv
    from kiln.queue import IdempotencyConflict

    if err := _srv._check_auth("queue"):
        return err
    # A replay of an already-queued job must not be judged by the cap:
    # the original job is already counted against it, and refusing the
    # retry would tell the caller "queue full" about a job that is in
    # the queue.  Racing submissions are still serialised inside
    # submit_result — this pre-check only decides whether the cap runs.
    _is_replay_candidate = (
        idempotency_key is not None
        and _srv._get_queue().find_by_idempotency_key(idempotency_key) is not None
    )
    # Free-tier queue cap: limit pending jobs.
    if _is_free_tier() and not _is_replay_candidate:
        pending = _srv._get_queue().pending_count()
        free_tier_max_queued_jobs = _free_tier_queue_cap()
        if pending >= free_tier_max_queued_jobs:
            from kiln.tiers_and_terms import (
                ALREADY_SUBSCRIBED_LINE,
                signin_hint_fields,
            )

            return {
                "success": False,
                "error": (
                    f"Job queue is limited to {free_tier_max_queued_jobs} pending jobs on the Free tier "
                    f"(you have {pending}). Wait for jobs to complete, "
                    "or upgrade to Kiln Pro for unlimited queue depth with multi-printer scheduling. "
                    f"{ALREADY_SUBSCRIBED_LINE} "
                    "Otherwise, see what Pro includes at kiln3d.com/pricing"
                ),
                "code": "FREE_TIER_LIMIT",
                "pending_count": pending,
                "max_allowed": free_tier_max_queued_jobs,
                "upgrade_url": "https://kiln3d.com/pricing",
                **signin_hint_fields(),
            }
    try:
        outcome = _srv._get_queue().submit_result(
            file_name=file_name,
            printer_name=printer_name,
            submitted_by="mcp-agent",
            priority=priority,
            idempotency_key=idempotency_key,
        )
        job = outcome.job
        if outcome.replayed:
            # Nothing new was queued, so no JOB_QUEUED event — the
            # original submission already published one.  Answer with
            # the original job's identity and current state so a caller
            # retrying after a lost response knows exactly where it is.
            return {
                "success": True,
                "job_id": job.id,
                "submission": "replayed",
                "job_state": job.status.value,
                "message": (
                    f"Job {job.id} was already submitted with this "
                    f"idempotency key (current state: {job.status.value}). "
                    "No duplicate was queued."
                ),
            }
        _srv._event_bus.publish(
            Event(
                type=EventType.JOB_QUEUED,
                data={"job_id": job.id, "file_name": file_name, "printer_name": printer_name},
                source="mcp",
            )
        )
        return {
            "success": True,
            "job_id": job.id,
            "submission": "queued",
            "message": f"Job {job.id} submitted to queue.",
        }
    except IdempotencyConflict as exc:
        return _srv._error_dict(
            f"Idempotency key already used by job {exc.existing_job_id!r} "
            "with different parameters (file, printer, or priority). "
            "Retries must repeat the original submission exactly; a new "
            "job needs a new key.",
            code="IDEMPOTENCY_CONFLICT",
        )
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
        job = _srv._get_queue().get_job(job_id)
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
        summary = _srv._get_queue().summary()
        next_job = _srv._get_queue().next_job()
        recent = _srv._get_queue().list_jobs(limit=10)
        pending = _srv._get_queue().pending_count()
        active = _srv._get_queue().active_count()
        total = _srv._get_queue().total_count
        registry = _srv._get_registry()
        registered_printers = registry.count
        emergency_latched_printers: list[str] = []
        if registered_printers > 0:
            try:
                from kiln.emergency import get_emergency_coordinator

                coord = get_emergency_coordinator()
                for name in registry.list_names():
                    status = coord.get_latch_status(name)
                    if bool(status.get("latched")):
                        emergency_latched_printers.append(name)
            except Exception:
                emergency_latched_printers = []

        no_printer_block = pending > 0 and active == 0 and registered_printers == 0
        all_latched_block = (
            pending > 0
            and active == 0
            and registered_printers > 0
            and len(emergency_latched_printers) >= registered_printers
        )
        dispatch_blocked = no_printer_block or all_latched_block
        if no_printer_block:
            dispatch_block_reason = (
                "Jobs are queued but no printers are registered. Register at least one printer with register_printer()."
            )
        elif all_latched_block:
            dispatch_block_reason = (
                "Jobs are queued but all registered printers are emergency-latched. "
                "Acknowledge and clear latch state before dispatch."
            )
        else:
            dispatch_block_reason = None
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
            "emergency_latched_printers": emergency_latched_printers,
        }
    except Exception as exc:
        _logger.exception("Unexpected error in queue_summary")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def cancel_queued_job(job_id: str) -> dict:
    """Remove one job from the print queue while it is still WAITING.

    Queue bookkeeping only: this marks the row cancelled and never sends
    anything to a printer.  To STOP a job the machine has already
    started, use ``cancel_print`` — that is the tool that talks to the
    hardware.

    Args:
        job_id: The job ID to cancel.

    Only a job still in the QUEUED state can be cancelled here.  A job
    that has reached the machine (starting, printing, or paused) is
    refused with ``code="PRINT_IN_PROGRESS"`` rather than cancelled,
    because marking the row cancelled would leave the queue claiming a
    print had stopped while the printer carried on running it.
    """
    import kiln.server as _srv
    from kiln.queue import JobNotFoundError

    if err := _srv._check_auth("queue"):
        return err
    try:
        # Read the state BEFORE cancelling.  The state machine happily
        # allows QUEUED/STARTING/PRINTING/PAUSED -> CANCELLED, so without
        # this the row and the machine disagree about reality.
        current = _srv._get_queue().get_job(job_id)
        if current.status.value in _ON_THE_MACHINE:
            return _srv._error_dict(
                f"Job {job_id!r} is already at the printer (status: "
                f"{current.status.value}), so removing it from the queue "
                f"would not stop it. Use cancel_print() to stop the "
                f"running print.",
                code="PRINT_IN_PROGRESS",
            )
        job = _srv._get_queue().cancel(job_id)
        _srv._event_bus.publish(
            Event(
                type=EventType.JOB_CANCELLED,
                data={"job_id": job_id},
                source="mcp",
            )
        )
        return {
            "success": True,
            "job": job.to_dict(),
            "message": f"Job {job_id} cancelled.",
        }
    except JobNotFoundError:
        return _srv._error_dict(f"Job not found: {job_id!r}", code="NOT_FOUND")
    except ValueError as exc:
        return _srv._error_dict(
            f"Cannot cancel job {job_id!r}: {exc}. Only a job still in the QUEUED state can be cancelled here.",
            code="INVALID_STATE",
        )
    except Exception as exc:
        _logger.exception("Unexpected error in cancel_queued_job")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def cancel_queued_jobs(
    printer_name: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Cancel ALL queued print jobs at once.

    The bulk companion to ``cancel_queued_job`` (which cancels one job by id).
    Cancels every job currently in the QUEUED state — clear a backed-up
    queue in one call instead of cancelling one job at a time.

    - ``printer_name``: limit the sweep to one printer's queued jobs; omit
      to clear every queued job.
    - ``dry_run=True``: preview exactly which jobs WOULD be cancelled and
      change nothing.  Run this first when clearing a large queue.

    Safety: this never cancels a running print.  Only jobs still in the
    QUEUED state are cancelled; each job's status is re-checked immediately
    before cancelling, so a job that has already started printing (or
    finished, or was cancelled elsewhere) is skipped rather than
    interrupted.  Use ``cancel_print`` to stop the job that is actually
    running.  Each cancel emits the same ``JOB_CANCELLED`` event as
    ``cancel_queued_job``.

    Returns ``{success, dry_run, count, cancelled, skipped, message}`` —
    ``count`` always equals ``len(cancelled)``; ``skipped`` is a list of
    ``{job_id, reason}`` for jobs that were not cancelled.
    """
    import kiln.server as _srv
    from kiln.queue import JobStatus

    if err := _srv._check_auth("queue"):
        return err

    # Snapshot the queued jobs.  The scheduler's own filter scopes to one
    # printer when asked; the high limit defeats the default 100-row page so
    # a long backlog is seen in full.
    try:
        targets = _srv._get_queue().list_jobs(
            status=JobStatus.QUEUED,
            printer_name=printer_name,
            limit=_ALL_QUEUED_JOBS,
        )
    except Exception as exc:
        _logger.exception("Unexpected error in cancel_queued_jobs (queue read)")
        return _srv._error_dict(
            f"Could not read the print queue: {exc}", code="INTERNAL_ERROR"
        )

    scope = f" on {printer_name}" if printer_name else ""

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "count": len(targets),
            "cancelled": [j.id for j in targets],
            "skipped": [],
            "message": (
                f"{len(targets)} queued job(s) would be cancelled{scope} "
                "— dry run, nothing changed."
                if targets
                else f"No queued jobs to cancel{scope} — dry run."
            ),
        }

    cancelled: list[str] = []
    skipped: list[dict[str, str]] = []
    for job in targets:
        # Re-check live status right before cancelling: a job that raced
        # QUEUED -> STARTING/PRINTING since the snapshot must NOT be
        # cancelled (cancel() accepts a running job and would kill the live
        # print).  Skip anything no longer QUEUED.
        try:
            live = _srv._get_queue().get_job(job.id)
        except Exception:
            skipped.append({"job_id": job.id, "reason": "no longer in the queue"})
            continue
        if live.status != JobStatus.QUEUED:
            skipped.append(
                {
                    "job_id": job.id,
                    "reason": f"no longer queued (status: {live.status.name})",
                }
            )
            continue
        try:
            _srv._get_queue().cancel(job.id)
            cancelled.append(job.id)
            _srv._event_bus.publish(
                Event(
                    type=EventType.JOB_CANCELLED,
                    data={"job_id": job.id, "bulk": True},
                    source="mcp",
                )
            )
        except Exception as exc:
            skipped.append(
                {"job_id": job.id, "reason": f"could not be cancelled: {exc}"}
            )

    if not targets:
        message = f"No queued jobs to cancel{scope}."
    else:
        message = f"Cancelled {len(cancelled)} queued job(s){scope}."
        if skipped:
            message += (
                f" {len(skipped)} skipped (already started or no longer queued)."
            )

    return {
        "success": True,
        "dry_run": False,
        "count": len(cancelled),
        "cancelled": cancelled,
        "skipped": skipped,
        "message": message,
    }


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
        all_jobs = _srv._get_queue().list_jobs(limit=capped)

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
        return "Print job queue management tools (submit, status, cancel, bulk-cancel, history)"

    def register(self, mcp: Any) -> None:
        """Register queue/job tools with the MCP server."""

        mcp.tool()(submit_job)
        mcp.tool()(job_status)
        mcp.tool()(queue_summary)
        mcp.tool()(cancel_queued_job)
        mcp.tool()(cancel_queued_jobs)

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
