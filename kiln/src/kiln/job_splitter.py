"""Multi-printer job splitting for parallel printing.

Splits large models or multi-part assemblies across multiple printers
for parallel printing. Handles: build volume overflow (model too big for
one printer), multi-copy jobs (print 10 copies across 5 printers), and
assembly splitting (multi-STL archive across fleet).

Uses the printer registry to discover available printers and their
build volumes.

Copies of one part across the fleet wherever there is safe room (Kiln
Business, https://kiln3d.com/pricing) are planned on Kiln's servers
(:mod:`kiln._pro_fleet_fill_bridge`) and kept here as a FILL plan: the plan
the person saw, saved under its id, and queued as one job per printer when
they say go (:func:`submit_fill_plan`).  Each printer's job is laid out for
its plate when its turn comes and queues the copies that plate cannot take
as the plate after it, on the same printer, carrying the plan's id, so the
whole run is one plan to follow (:func:`fill_progress`) and to stop
(:func:`cancel_split_plan`).  :func:`plan_multi_copy_split` below is the
blind spread: it reads no plate.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SplitJob:
    """A single part within a split plan."""

    part_id: str
    file_path: str
    printer_name: str
    printer_model: str
    estimated_time_seconds: int
    material: str
    settings: dict[str, Any]
    status: str  # "pending", "printing", "completed", "failed"
    job_id: str | None = None  # queue job ID once submitted

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SplitPlan:
    """Plan for distributing print jobs across multiple printers."""

    original_file: str
    split_type: str  # "multi_copy", "build_volume_overflow", "assembly"
    parts: list[SplitJob]
    total_printers: int
    estimated_total_time_seconds: int  # wall clock (parallel)
    estimated_sequential_time_seconds: int  # if printed on one printer
    time_savings_percentage: float
    assembly_instructions: list[str] | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["parts"] = [p.to_dict() for p in self.parts]
        return data


@dataclass
class SplitProgress:
    """Current progress of a split plan execution."""

    plan_id: str
    total_parts: int
    completed_parts: int
    failed_parts: int
    in_progress_parts: int
    pending_parts: int
    overall_progress: float  # 0.0 - 1.0
    estimated_remaining_seconds: int
    part_statuses: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_available_printers(available_printers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Resolve available printers from explicit list or registry."""
    if available_printers:
        return available_printers

    try:
        from kiln.registry import get_registry

        registry = get_registry()
        printers = []
        for name in registry.list():
            try:
                adapter = registry.get(name)
                state = adapter.get_state()
                printers.append(
                    {
                        "name": name,
                        "model": getattr(adapter, "printer_model", "unknown"),
                        "status": state.state.value if state else "unknown",
                        "build_volume": getattr(adapter, "build_volume", None),
                    }
                )
            except Exception:
                logger.debug("Failed to query printer %s", name)
        return printers
    except Exception:
        logger.debug("Failed to get printer registry")
        return []


def _estimate_print_time(file_path: str) -> int:
    """Rough time estimate for a single print in seconds.

    Uses file size as a proxy — 1MB of G-code ~ 30 minutes.
    """
    import os

    try:
        size_bytes = os.path.getsize(file_path)
        size_mb = size_bytes / (1024 * 1024)
        estimate = int(size_mb * 30 * 60)  # 30 min per MB
        return max(estimate, 60)  # minimum 1 minute
    except OSError:
        return 3600  # default 1 hour


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def plan_multi_copy_split(
    file_path: str,
    copies: int,
    *,
    material: str = "pla",
    available_printers: list[dict[str, Any]] | None = None,
) -> SplitPlan:
    """Plan parallel printing of multiple copies across printers.

    Distributes *copies* copies of *file_path* across available printers
    using round-robin assignment.

    :param file_path: Path to the G-code or model file.
    :param copies: Number of copies to print.
    :param material: Material type (default ``"pla"``).
    :param available_printers: Optional explicit list of printer dicts.
    :returns: A :class:`SplitPlan` with per-printer assignments.
    """
    printers = _get_available_printers(available_printers)
    if not printers:
        # Single-printer fallback
        printers = [{"name": "default", "model": "unknown"}]

    time_per_copy = _estimate_print_time(file_path)
    num_printers = min(len(printers), copies)
    parts: list[SplitJob] = []

    for i in range(copies):
        printer = printers[i % num_printers]
        parts.append(
            SplitJob(
                part_id=f"copy_{i + 1}",
                file_path=file_path,
                printer_name=printer.get("name", f"printer_{i % num_printers}"),
                printer_model=printer.get("model", "unknown"),
                estimated_time_seconds=time_per_copy,
                material=material,
                settings={},
                status="pending",
            )
        )

    # Calculate times
    sequential_time = time_per_copy * copies

    # Wall clock time: max copies assigned to any single printer
    copies_per_printer: dict[str, int] = {}
    for part in parts:
        copies_per_printer[part.printer_name] = copies_per_printer.get(part.printer_name, 0) + 1
    max_copies_on_one = max(copies_per_printer.values()) if copies_per_printer else copies
    parallel_time = time_per_copy * max_copies_on_one

    savings = ((sequential_time - parallel_time) / sequential_time * 100) if sequential_time > 0 else 0.0

    return SplitPlan(
        original_file=file_path,
        split_type="multi_copy",
        parts=parts,
        total_printers=num_printers,
        estimated_total_time_seconds=parallel_time,
        estimated_sequential_time_seconds=sequential_time,
        time_savings_percentage=round(savings, 1),
        assembly_instructions=None,
    )


def plan_assembly_split(
    file_paths: list[str],
    *,
    material: str = "pla",
    available_printers: list[dict[str, Any]] | None = None,
) -> SplitPlan:
    """Split a multi-file assembly across printers.

    Assigns each STL/G-code file in the assembly to a different printer,
    optimising for parallel execution.

    :param file_paths: List of file paths in the assembly.
    :param material: Material type (default ``"pla"``).
    :param available_printers: Optional explicit list of printer dicts.
    :returns: A :class:`SplitPlan` with per-printer assignments and
        assembly instructions.
    """
    printers = _get_available_printers(available_printers)
    if not printers:
        printers = [{"name": "default", "model": "unknown"}]

    num_printers = min(len(printers), len(file_paths))
    parts: list[SplitJob] = []
    total_sequential = 0

    for i, fp in enumerate(file_paths):
        time_est = _estimate_print_time(fp)
        total_sequential += time_est
        printer = printers[i % num_printers]
        import os

        part_name = os.path.splitext(os.path.basename(fp))[0]

        parts.append(
            SplitJob(
                part_id=f"part_{part_name}",
                file_path=fp,
                printer_name=printer.get("name", f"printer_{i % num_printers}"),
                printer_model=printer.get("model", "unknown"),
                estimated_time_seconds=time_est,
                material=material,
                settings={},
                status="pending",
            )
        )

    # Wall clock: max time among all printers
    printer_times: dict[str, int] = {}
    for part in parts:
        printer_times[part.printer_name] = printer_times.get(part.printer_name, 0) + part.estimated_time_seconds
    parallel_time = max(printer_times.values()) if printer_times else total_sequential

    savings = ((total_sequential - parallel_time) / total_sequential * 100) if total_sequential > 0 else 0.0

    assembly_instructions = [
        f"Step {i + 1}: Collect part '{p.part_id}' from printer '{p.printer_name}'"
        for i, p in enumerate(parts)
    ]
    assembly_instructions.append(f"Step {len(parts) + 1}: Assemble all {len(parts)} parts per the model design")

    return SplitPlan(
        original_file=file_paths[0] if file_paths else "",
        split_type="assembly",
        parts=parts,
        total_printers=num_printers,
        estimated_total_time_seconds=parallel_time,
        estimated_sequential_time_seconds=total_sequential,
        time_savings_percentage=round(savings, 1),
        assembly_instructions=assembly_instructions,
    )


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------


def submit_split_plan(
    plan: SplitPlan,
    idempotency_key: str | None = None,
    *,
    preview_token: str | None = None,
) -> str:
    """Submit all parts of a split plan to the job queue.

    Each part is submitted as a separate job with metadata linking it
    back to the plan.

    Every part is a print the scheduler will start later with nobody there
    to ask, so the preview gate is taken here, once, on the plan's original
    file: refused without ``preview_token`` (or a person's yes recorded by
    the host) — the same rule as ``submit_job``.

    :param plan: The :class:`SplitPlan` to submit.
    :param preview_token: Token from ``issue_preview_token`` for the plan's
        original file.
    :param idempotency_key: Optional opaque key naming this whole
        submission.  Each part's queue job is keyed
        ``{idempotency_key}:part:{part_id}``, so a retry of the same
        plan submission after a lost response re-adopts the parts it
        already queued instead of queuing every part twice.  Without a
        key, behaviour is unchanged (a fresh plan every call).
    :returns: A plan_id string for tracking.
    """
    from kiln.persistence import get_db

    plan_id = str(uuid.uuid4())
    db = get_db()

    # Save the plan to the database
    try:
        db._conn.execute(
            """INSERT INTO split_plans (id, original_file, split_type, parts, total_printers, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id,
                plan.original_file,
                plan.split_type,
                json.dumps([p.to_dict() for p in plan.parts]),
                plan.total_printers,
                time.time(),
                "pending",
            ),
        )
        db._conn.commit()
    except Exception:
        logger.exception("Failed to save split plan (non-fatal)")

    # Submit each part to the job queue.  The queue accessor lives on
    # the server module, not kiln.queue — the old
    # `from kiln.queue import get_queue` raised ImportError on every
    # part and the blanket except below silently marked them all failed.
    import kiln.server as _srv

    if block := _srv._preview_gate_error(
        "submit_split_plan", plan.original_file, preview_token,
    ):
        refusal = block.get("error") if isinstance(block.get("error"), dict) else {}
        raise PermissionError(str(refusal.get("message") or block))

    queue = _srv._get_queue()

    for part in plan.parts:
        try:
            job_id = queue.submit(
                file_name=part.file_path,
                printer_name=part.printer_name,
                submitted_by="split-plan",
                metadata={"split_plan_id": plan_id, "part_id": part.part_id},
                idempotency_key=(
                    f"{idempotency_key}:part:{part.part_id}"
                    if idempotency_key
                    else None
                ),
            )
            part.job_id = job_id
            part.status = "pending"
        except Exception:
            logger.warning("Failed to submit part %s to queue", part.part_id)
            part.status = "failed"

    # Update the plan with job IDs
    try:
        db._conn.execute(
            "UPDATE split_plans SET parts = ?, status = ? WHERE id = ?",
            (json.dumps([p.to_dict() for p in plan.parts]), "submitted", plan_id),
        )
        db._conn.commit()
    except Exception:
        logger.debug("Failed to update split plan with job IDs")

    return plan_id


def get_split_progress(plan_id: str) -> SplitProgress:
    """Check the progress of a split plan.

    :param plan_id: The plan ID returned by :func:`submit_split_plan`.
    :returns: A :class:`SplitProgress` with per-part statuses.
    """
    from kiln.persistence import get_db

    db = get_db()
    row = db._conn.execute("SELECT * FROM split_plans WHERE id = ?", (plan_id,)).fetchone()

    if not row:
        return SplitProgress(
            plan_id=plan_id,
            total_parts=0,
            completed_parts=0,
            failed_parts=0,
            in_progress_parts=0,
            pending_parts=0,
            overall_progress=0.0,
            estimated_remaining_seconds=0,
            part_statuses=[],
        )

    parts_data = json.loads(dict(row)["parts"])
    if not isinstance(parts_data, list):
        parts_data = []
    total = len(parts_data)
    completed = sum(1 for p in parts_data if p.get("status") == "completed")
    failed = sum(1 for p in parts_data if p.get("status") == "failed")
    in_progress = sum(1 for p in parts_data if p.get("status") == "printing")
    pending = sum(1 for p in parts_data if p.get("status") == "pending")

    progress = completed / total if total > 0 else 0.0

    # Estimate remaining time from pending + in-progress parts
    remaining = sum(
        p.get("estimated_time_seconds", 0) for p in parts_data if p.get("status") in ("pending", "printing")
    )

    return SplitProgress(
        plan_id=plan_id,
        total_parts=total,
        completed_parts=completed,
        failed_parts=failed,
        in_progress_parts=in_progress,
        pending_parts=pending,
        overall_progress=round(progress, 2),
        estimated_remaining_seconds=remaining,
        part_statuses=parts_data,
    )


def cancel_split_plan(plan_id: str) -> dict[str, Any]:
    """Cancel all pending/in-progress parts of a split plan.

    :param plan_id: The plan ID to cancel.
    :returns: Summary dict with cancellation results.
    """
    from kiln.persistence import get_db

    db = get_db()
    row = db._conn.execute("SELECT * FROM split_plans WHERE id = ?", (plan_id,)).fetchone()

    if not row:
        return {"success": False, "error": f"Split plan {plan_id} not found"}

    record = dict(row)
    if record.get("split_type") == FILL_SPLIT_TYPE:
        return _cancel_fill_plan(plan_id)
    parts_data = json.loads(record["parts"])
    cancelled = 0
    errors: list[str] = []

    # The queue accessor lives on the server module (see submit_split_plan);
    # ``kiln.queue`` has none, and asking it for one failed every cancel.
    import kiln.server as _srv

    for part in parts_data:
        if part.get("status") in ("pending", "printing") and part.get("job_id"):
            try:
                _srv._get_queue().cancel(part["job_id"])
                part["status"] = "cancelled"
                cancelled += 1
            except Exception as exc:
                errors.append(f"Failed to cancel {part['part_id']}: {exc}")

    # Update the plan
    try:
        db._conn.execute(
            "UPDATE split_plans SET parts = ?, status = ? WHERE id = ?",
            (json.dumps(parts_data), "cancelled", plan_id),
        )
        db._conn.commit()
    except Exception:
        logger.debug("Failed to update cancelled split plan")

    return {
        "success": True,
        "plan_id": plan_id,
        "cancelled_count": cancelled,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Fill plans: copies across the fleet, wherever there is safe room
# ---------------------------------------------------------------------------

FILL_SPLIT_TYPE = "fill"


def save_fill_plan(
    model_path: str, copies: int, material: str, fill: dict[str, Any], plates: dict[str, str] | None = None,
) -> str:
    """Keep the plan the person was shown, and the part, under a new plan id.

    The part is copied into Kiln's own folder first, as a queued copies job
    keeps it (``kiln.server._queue_fill_rest``), so tidying the folder it
    came from loses nothing.  *plates* is each occupied plate's record
    fingerprint as the plan saw it.  Returns the plan id.
    """
    import os

    from kiln.asset_store import kiln_root, persist_asset
    from kiln.persistence import get_db

    kept = persist_asset(model_path, os.path.join(kiln_root(), "queued_parts"), prefix="part") or model_path
    plan_id = str(uuid.uuid4())
    record = {"copies": int(copies), "material": material, "fill": fill, "plates": dict(plates or {}), "queued": []}
    db = get_db()
    db._conn.execute(
        """INSERT INTO split_plans (id, original_file, split_type, parts, total_printers, created_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (plan_id, kept, FILL_SPLIT_TYPE, json.dumps(record), len(fill.get("printers") or []), time.time(), "planned"),
    )
    db._conn.commit()
    return plan_id


def _fill_record(plan_id: str) -> tuple[str, dict[str, Any], str] | None:
    """``(model_path, record, status)`` for a fill plan, or ``None``."""
    from kiln.persistence import get_db

    row = get_db()._conn.execute("SELECT * FROM split_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row or dict(row).get("split_type") != FILL_SPLIT_TYPE:
        return None
    row = dict(row)
    try:
        record = json.loads(row["parts"])
    except (TypeError, ValueError):
        return None
    return str(row.get("original_file") or ""), record if isinstance(record, dict) else {}, str(row.get("status") or "")


def submit_fill_plan(plan_id: str, signoff: dict[str, Any] | None) -> dict[str, Any]:
    """Queue a kept fill plan: one copies job per printer, pinned to it.

    Each job asks for the copies that printer prints now and later; it is
    laid out for its plate when its turn comes, and what that plate cannot
    take is queued again on the same printer (the queue's own copies path),
    so later plates follow as plates are cleared.  *signoff* is the person's
    yes, carried on every job.  The caller takes the gate first.  Queues
    nothing a second time: a plan already queued answers what it queued.
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    found = _fill_record(plan_id)
    if found is None:
        return {"success": False, "error": {"code": "NOT_FOUND", "message": f"No copies plan {plan_id!r} on this computer."}}
    model_path, record, status = found
    if status in ("submitted", "cancelled"):
        return {"success": True, "plan_id": plan_id, "queued": list(record.get("queued") or []), "already": status}
    queued: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in (record.get("fill") or {}).get("printers") or []:
        copies = int(row.get("now") or 0) + int(row.get("later") or 0)
        if copies < 1:
            continue
        beside = (record.get("plates") or {}).get(str(row["printer"])) if row.get("status") == "occupied" and row.get("now") else None
        out = _srv._queue_fill_rest(
            model_path, copies, printer_name=str(row["printer"]), printer_id=None, overrides=None,
            slicer_path=None, spacing_mm=10.0, signoff=signoff, run={"split_plan_id": plan_id},
            beside_plate=beside or None,
        )
        if out.get("job_id"):
            queued.append({"printer": out.get("printer_name") or row["printer"], "job_id": out["job_id"], "copies": copies})
        else:
            errors.append(f"{row['printer']}: {out.get('why') or 'could not be queued'}")
    record["queued"] = queued
    db = get_db()
    db._conn.execute(
        "UPDATE split_plans SET parts = ?, status = ? WHERE id = ?",
        (json.dumps(record), "submitted" if queued else "planned", plan_id),
    )
    db._conn.commit()
    return {"success": bool(queued), "plan_id": plan_id, "queued": queued, "errors": errors}


def _jobs_of(plan_id: str) -> list[Any]:
    """Every queue job of the run: the first per printer and every plate queued after it."""
    import kiln.server as _srv

    return [j for j in _srv._get_queue().list_jobs() if (j.metadata or {}).get("split_plan_id") == plan_id]


def failed_plate_report(job: Any, *, registry: Any, queue: Any) -> dict[str, Any] | None:
    """What the queue sends Kiln's servers about a plate of a fleet run that
    failed, read on this computer: the part and the run's material, every
    registered printer as a routing candidate, the printers' slicer times
    for the part, and the ``failed`` block -- where, how many copies, how
    many failures in a row there, the printer's own fault reading now, the
    run's plates done and failed per printer, and the printers the person's
    yes covers.  ``None`` when the job is not a plate of a kept fleet run.
    Nothing here decides anything; the decision is the server's
    (``plan_fleet_fill``), and the queue acts on it.
    """
    from kiln import print_signoff
    from kiln.auto_record_hook import version_of_print_file
    from kiln.plugins.recovery_tools import _printer_times
    from kiln.printers.base import describe_screen_faults
    from kiln.printers.engagement import internal_read
    from kiln.queue import JobStatus
    from kiln.routing_candidates import collect_routing_candidates

    meta = job.metadata or {}
    plan_id = meta.get("split_plan_id")
    fill = meta.get("fill") or {}
    found = _fill_record(str(plan_id)) if plan_id else None
    if found is None or not fill.get("model_path"):
        return None
    _kept, record, _status = found
    material = str(record.get("material") or "").strip() or "pla"
    file_path = str(fill["model_path"])
    names = registry.list_names()
    adapters = {name: registry.get(name) for name in names}
    pending: dict[str, int] = {}
    for queued in queue.list_jobs(status=JobStatus.QUEUED):
        if queued.printer_name:
            pending[queued.printer_name] = pending.get(queued.printer_name, 0) + 1
    candidates = collect_routing_candidates(
        adapters=adapters, material=material, pending_counts=pending, version_id=version_of_print_file(file_path),
    )
    times, _per_model, _model_of = _printer_times(file_path, [str(c["printer_id"]) for c in candidates], adapters, material)
    # The failed printer's own reading, as it stands now: its state word,
    # its fault codes and the lines its screen shows.  Raw; the server reads it.
    fault: dict[str, Any] = {"state": "unknown", "codes": [], "lines": []}
    adapter = adapters.get(str(job.printer_name))
    if adapter is not None:
        try:
            with internal_read():
                state = adapter.get_state()
            word = getattr(getattr(state, "state", None), "value", None)
            fault["state"] = str(word or "unknown").lower()
            faults = getattr(state, "faults", None) or []
            fault["codes"] = [str(f.get("code")) for f in faults if isinstance(f, dict) and f.get("code")][:12]
            lines = describe_screen_faults(faults)
            note = getattr(state, "fault_note", None)
            if isinstance(note, str) and note.strip():
                lines.append(note.strip())
            fault["lines"] = [line[:300] for line in lines][:12]
        except Exception:  # noqa: BLE001 -- a printer that will not answer is a reading too
            fault["state"] = "offline"
    run: dict[str, dict[str, int]] = {}
    for other in _jobs_of(str(plan_id)):
        if other.id == job.id:
            continue
        row = run.setdefault(str(other.printer_name or ""), {"done": 0, "failed": 0})
        ended = (other.metadata or {}).get("ended")
        if other.status == JobStatus.COMPLETED and ended == "success":
            row["done"] += 1
        elif (other.status == JobStatus.COMPLETED and ended == "failed") or other.status == JobStatus.FAILED:
            row["failed"] += 1
    here = run.setdefault(str(job.printer_name or ""), {"done": 0, "failed": 0})
    here["failed"] += 1
    run = {name: counts for name, counts in run.items() if name in adapters}
    return {
        "file_path": file_path,
        "material": material,
        "candidates": candidates,
        "adapters": adapters,
        "times": times,
        "failed": {
            "printer": str(job.printer_name or ""),
            "copies": int(fill.get("copies") or 0),
            "tries": int(fill.get("failed_retries") or 0) + 1,
            "fault": fault,
            "run": run,
            "allowed": print_signoff.covered_printers(meta.get("preview_signoff")),
        },
    }


def fill_progress(plan_id: str) -> dict[str, Any] | None:
    """Where a queued fill plan stands, per printer: plates printed, printing
    and waiting, failed, and moved to another printer, and the copies still
    to come.  ``None`` for no such plan."""
    from kiln.queue import JobStatus

    found = _fill_record(plan_id)
    if found is None:
        return None
    _model, record, status = found
    per: dict[str, dict[str, int]] = {}
    for job in _jobs_of(plan_id):
        row = per.setdefault(str(job.printer_name or ""), {
            "done": 0, "printing": 0, "waiting": 0, "failed": 0, "moved": 0, "copies_done": 0, "copies_to_come": 0,
        })
        ended = (job.metadata or {}).get("ended")
        if ((job.metadata or {}).get("failed_plate") or {}).get("decision") == "move":
            row["moved"] += 1
        if job.status == JobStatus.COMPLETED and ended in ("failed", "cancelled", "unknown"):
            # The printer went idle, but the plate did not come out: a failed
            # plate's copies are queued again (kiln.scheduler), a cancel is
            # the person's, and an ending nobody saw is asked about.
            row["failed"] += 1
        elif job.status == JobStatus.COMPLETED:
            row["done"] += 1
            row["copies_done"] += int(((job.metadata or {}).get("fill") or {}).get("copies") or 0)
        elif job.status in (JobStatus.PRINTING, JobStatus.STARTING):
            row["printing"] += 1
        elif job.status == JobStatus.QUEUED:
            row["waiting"] += 1
            row["copies_to_come"] += int(((job.metadata or {}).get("fill") or {}).get("copies") or 0)
        elif job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
            row["failed"] += 1
    return {
        "plan_id": plan_id,
        "status": status,
        "wanted": int(record.get("copies") or 0),
        "printers": [{"printer": name, "plates": counts} for name, counts in per.items()],
        "copies_done": sum(r["copies_done"] for r in per.values()),
        "copies_to_come": sum(r["copies_to_come"] for r in per.values()),
    }


def _cancel_fill_plan(plan_id: str) -> dict[str, Any]:
    """Cancel every job of a fill plan that has not reached a printer."""
    import kiln.server as _srv
    from kiln.persistence import get_db
    from kiln.queue import JobStatus

    cancelled, printing, errors = 0, [], []
    for job in _jobs_of(plan_id):
        if job.status == JobStatus.QUEUED:
            try:
                _srv._get_queue().cancel(job.id)
                cancelled += 1
            except Exception as exc:  # noqa: BLE001 -- said, never lost
                errors.append(f"{job.printer_name}: {exc}")
        elif job.status in (JobStatus.PRINTING, JobStatus.STARTING):
            printing.append(str(job.printer_name))
    found = _fill_record(plan_id)
    if found is not None:
        db = get_db()
        db._conn.execute("UPDATE split_plans SET status = ? WHERE id = ?", ("cancelled", plan_id))
        db._conn.commit()
    still = f" Still printing, untouched: {', '.join(printing)} (cancel_print stops one)." if printing else ""
    return {
        "success": not errors,
        "plan_id": plan_id,
        "cancelled": cancelled,
        "printing": printing,
        "errors": errors,
        "message": f"Cancelled {cancelled} queued plate{'s' if cancelled != 1 else ''} of this run.{still}",
    }
