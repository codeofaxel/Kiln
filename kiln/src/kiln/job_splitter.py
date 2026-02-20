"""Multi-printer job splitting for parallel printing.

Splits large models or multi-part assemblies across multiple printers
for parallel printing. Handles: build volume overflow (model too big for
one printer), multi-copy jobs (print 10 copies across 5 printers), and
assembly splitting (multi-STL archive across fleet).

Uses the printer registry to discover available printers and their
build volumes.
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


def submit_split_plan(plan: SplitPlan) -> str:
    """Submit all parts of a split plan to the job queue.

    Each part is submitted as a separate job with metadata linking it
    back to the plan.

    :param plan: The :class:`SplitPlan` to submit.
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

    # Submit each part to the job queue
    for part in plan.parts:
        try:
            from kiln.queue import get_queue

            queue = get_queue()
            job_id = queue.submit(
                file_name=part.file_path,
                printer_name=part.printer_name,
                submitted_by="split-plan",
                metadata={"split_plan_id": plan_id, "part_id": part.part_id},
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

    parts_data = json.loads(dict(row)["parts"])
    cancelled = 0
    errors: list[str] = []

    for part in parts_data:
        if part.get("status") in ("pending", "printing") and part.get("job_id"):
            try:
                from kiln.queue import get_queue

                queue = get_queue()
                queue.cancel(part["job_id"])
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
