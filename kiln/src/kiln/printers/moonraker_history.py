"""Import a Moonraker server's own job history into Kiln's print outcomes."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_HISTORY_PATH = "/server/history/list"

_STATUS_MAP: dict[str, str] = {
    "completed": "success",
    "cancelled": "cancelled",
    "error": "failed",
    "klippy_shutdown": "failed",
    "server_exit": "failed",
    "in_progress": "unknown",
    "interrupted": "unknown",
}

_JOB_ID_PREFIX = "moonraker:"

_CLOCK_SKEW_S = 120.0


def map_status(status: Any) -> str:
    """Translate a Moonraker history status into Kiln's outcome vocabulary."""
    if not isinstance(status, str):
        return "unknown"
    return _STATUS_MAP.get(status.strip().lower(), "unknown")


def _epoch(value: Any) -> float | None:
    """A usable unix timestamp, or None for anything that isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _job_timestamp(job: dict[str, Any]) -> float | None:
    """When the print actually happened — its ending, else its start."""
    return _epoch(job.get("end_time")) or _epoch(job.get("start_time"))


def _material_from_metadata(job: dict[str, Any]) -> str | None:
    """The filament type the SERVER recorded, or None."""
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("filament_type")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _row_from_job(job: dict[str, Any], printer_name: str) -> dict[str, Any] | None:
    """Build one outcome row from a history entry, or None if unusable."""
    server_job_id = job.get("job_id")
    if server_job_id is None or str(server_job_id).strip() == "":
        return None

    created_at = _job_timestamp(job)
    if created_at is None:
        # No ending and no start: nothing to place this print in time.
        return None

    raw_status = job.get("status")
    status_text = raw_status if isinstance(raw_status, str) else str(raw_status)

    file_name = job.get("filename")
    if not isinstance(file_name, str) or not file_name.strip():
        file_name = None

    return {
        "job_id": f"{_JOB_ID_PREFIX}{server_job_id}",
        "printer_name": printer_name,
        "file_name": file_name,
        "material_type": _material_from_metadata(job),
        "outcome": map_status(raw_status),
        "determined_by": "inferred",
        "agent_id": "auto",
        "notes": (
            "Imported from Moonraker job history "
            f"(server status: {status_text})"
        ),
        "created_at": created_at,
        "ended_at": _epoch(job.get("end_time")),
    }


def _kiln_recorded_timestamps(db: Any, printer_name: str) -> list[float]:
    """When Kiln's OWN outcome rows for this printer say prints happened."""
    try:
        rows = db.list_print_outcomes(
            printer_name=printer_name, limit=1000, include_all=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read existing outcomes for %s: %s", printer_name, exc)
        return []
    stamps: list[float] = []
    for row in rows:
        if str(row.get("job_id") or "").startswith(_JOB_ID_PREFIX):
            continue
        created = _epoch(row.get("created_at"))
        if created is not None:
            stamps.append(created)
    return stamps


def _already_recorded_by_kiln(job: dict[str, Any], stamps: list[float]) -> bool:
    """True when Kiln already holds a row for this same physical print."""
    if not stamps:
        return False
    start = _epoch(job.get("start_time"))
    end = _epoch(job.get("end_time"))
    if start is None and end is None:
        return False
    low = (start if start is not None else end) - _CLOCK_SKEW_S  # type: ignore[operator]
    high = (end if end is not None else start) + _CLOCK_SKEW_S  # type: ignore[operator]
    return any(low <= stamp <= high for stamp in stamps)


def _still_running(job: dict[str, Any]) -> bool:
    """True for the job the printer is running RIGHT NOW."""
    status = job.get("status")
    ended = job.get("end_time")
    return (
        isinstance(status, str)
        and status.strip().lower() == "in_progress"
        and not isinstance(ended, (int, float))
    )


def backfill_history(adapter: Any, *, limit: int = 200) -> dict[str, Any]:
    """Import the Moonraker server's job history as Kiln print outcomes."""
    printer_name = getattr(adapter, "name", "moonraker")
    result: dict[str, Any] = {
        "available": False,
        "fetched": 0,
        "imported": 0,
        "skipped": 0,
        "ignored": 0,
        "error": None,
    }

    try:
        payload = adapter._get_json(
            _HISTORY_PATH, params={"limit": int(limit), "order": "desc"}
        )
    except Exception as exc:  # noqa: BLE001 — a courtesy import never raises
        message = str(exc)
        if "404" in message:
            logger.debug(
                "Moonraker history component not available on %s: %s",
                printer_name,
                message,
            )
        else:
            logger.debug("Moonraker history fetch failed: %s", message)
        result["error"] = message
        return result

    jobs = payload.get("result", {}) if isinstance(payload, dict) else {}
    jobs = jobs.get("jobs") if isinstance(jobs, dict) else None
    if not isinstance(jobs, list):
        logger.debug("Moonraker history returned no job list for %s", printer_name)
        return result

    result["available"] = True
    result["fetched"] = len(jobs)

    try:
        from kiln.persistence import get_db

        db = get_db()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Moonraker history import: DB unavailable: %s", exc)
        result["error"] = str(exc)
        return result

    kiln_stamps = _kiln_recorded_timestamps(db, printer_name)

    for job in jobs:
        if not isinstance(job, dict) or _still_running(job):
            result["ignored"] += 1
            continue
        row = _row_from_job(job, printer_name)
        if row is None:
            result["ignored"] += 1
            continue
        if _already_recorded_by_kiln(job, kiln_stamps):
            # Kiln was there for this one under its own job id
            result["skipped"] += 1
            continue
        try:
            if db.get_print_outcome(row["job_id"]) is not None:
                # Already imported
                result["skipped"] += 1
                continue
            db.save_print_outcome(row)
            result["imported"] += 1
        except ValueError as exc:
            if "already recorded" in str(exc):
                result["skipped"] += 1
            else:
                logger.debug("Moonraker history row rejected: %s", exc)
                result["ignored"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("Moonraker history row failed to save: %s", exc)
            result["ignored"] += 1

    if result["imported"]:
        logger.info(
            "Imported %d print(s) from %s's own job history (%d already known)",
            result["imported"],
            printer_name,
            result["skipped"],
        )
    return result


__all__ = ["backfill_history", "map_status"]
