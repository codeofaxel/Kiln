"""Import a Moonraker server's own job history into Kiln's print outcomes.

Kiln's outcome capture only ever saw prints Kiln itself started or
watched (:mod:`kiln.auto_record_hook`).  A Klipper user who has been
printing for two years through Fluidd or Mainsail therefore meets Kiln
with an EMPTY history — no success rates, no proven settings, no
regression baseline — even though the machine in front of them has kept
a complete record the whole time.  Moonraker's ``[history]`` component
stores every job it ran (``GET /server/history/list``), independent of
Kiln, so that record can simply be adopted.

What this module does NOT do is as load-bearing as what it does:

* It writes through :meth:`kiln.persistence.KilnDB.save_print_outcome`
  directly, never through the ``record_print_outcome`` TOOL.  The tool
  is the door for a print Kiln participated in; it also drives the
  learning-loop side effects that a bulk historical import must not
  fire.  One writer per physical event — the import claims the events
  no other writer ever saw.
* It never touches :mod:`kiln.daily_stats`.  Those counters describe
  TODAY; a two-year backfill landing in today's ``prints`` counter
  would report a phantom fleet to the heartbeat.
* It never contributes to the community outbox
  (:mod:`kiln.community_autofire`).  Bulk historical rows entering the
  shared corpus is precisely the failure this week's outbox guards
  exist to prevent — thousands of unverified rows outvoting the real
  ones for every other user.  Contribution stays with the live paths,
  where a human saw the print.

Honesty rules (the house stance, unchanged here): the server's status
string decides the outcome, absence becomes ``unknown``, and an
unrecognized status is never guessed into a verdict.  Every imported
row is ``determined_by='inferred'`` — the machine's testimony, not an
observation and not a user's report.

Dedupe is the ``job_id`` unique index: a row is keyed
``moonraker:<server job id>``, so re-running the import re-offers rows
that already exist and each one is refused and counted as skipped.
Nothing accumulates.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Moonraker's history endpoint, provided by the optional ``[history]``
# component.  A server without it answers 404 — a clean no-op here.
_HISTORY_PATH = "/server/history/list"

# Moonraker job status -> Kiln's outcome vocabulary (the ONE vocabulary
# persistence enforces).  Anything absent from this map is a status this
# version of Kiln does not understand, and an unknown status yields
# 'unknown', never a guessed verdict — including Klipper's
# 'klippy_disconnect', which reports how the RECORDING ended and not how
# the PRINT did.
_STATUS_MAP: dict[str, str] = {
    "completed": "success",
    "cancelled": "cancelled",
    "error": "failed",
    "klippy_shutdown": "failed",
    "server_exit": "failed",
    "in_progress": "unknown",
    "interrupted": "unknown",
}

# Namespace for imported rows: the SERVER's job id, kept distinct from the
# ids Kiln mints for prints it ran itself.
_JOB_ID_PREFIX = "moonraker:"

# How far Kiln's clock and the server's clock are allowed to disagree when
# deciding whether an imported job is a print Kiln already recorded.
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
    """When the print actually happened — its ending, else its start.

    Never ``now()``: a row stamped with the import time would tell every
    downstream reader that two years of prints all happened this
    afternoon.
    """
    return _epoch(job.get("end_time")) or _epoch(job.get("start_time"))


def _material_from_metadata(job: dict[str, Any]) -> str | None:
    """The filament type the SERVER recorded, or None.

    Slicer metadata is present only when the gcode carried it; absence
    stays absence rather than becoming a default guess of "PLA".
    """
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
    }


def _kiln_recorded_timestamps(db: Any, printer_name: str) -> list[float]:
    """When Kiln's OWN outcome rows for this printer say prints happened.

    Rows this importer wrote are excluded — they are deduped by job id.
    What is left is every print Kiln itself started or watched.
    """
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
    """True when Kiln already holds a row for this same physical print.

    The ``job_id`` index cannot see this one: a print Kiln started names
    its row after the FILE, while the server names the same print after
    its own job number — two ids, one physical print, and importing it
    again would double-count it in every success rate.

    A printer runs one job at a time, so any Kiln row stamped inside a
    job's run window IS that job.  The window is padded because the two
    clocks (Kiln's and the server's) are not the same clock.
    """
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
    """True for the job the printer is running RIGHT NOW.

    A job with no ending has not ended, so there is no outcome to
    import — the live lifecycle in :mod:`kiln.auto_record_hook` owns it
    and will settle its pending row when it finishes.  Importing it here
    would stamp a verdict on a print still in progress and leave the
    live ending nowhere to land.
    """
    status = job.get("status")
    ended = job.get("end_time")
    return (
        isinstance(status, str)
        and status.strip().lower() == "in_progress"
        and not isinstance(ended, (int, float))
    )


def backfill_history(adapter: Any, *, limit: int = 200) -> dict[str, Any]:
    """Import the Moonraker server's job history as Kiln print outcomes.

    Args:
        adapter: A connected :class:`~kiln.printers.moonraker.MoonrakerAdapter`.
            Its own HTTP helper (session, API key, timeout, retries) makes
            the request — this module never opens its own connection.
        limit: How many of the most recent jobs to ask for.  Bounded by
            default: a backfill is a courtesy, not a migration tool.

    Returns:
        A counts dict — ``available`` (did the server have a history to
        offer), ``fetched``, ``imported``, ``skipped`` (already present),
        ``ignored`` (unusable or still running), ``error``.

    Never raises: a server error, an absent ``[history]`` component, or a
    malformed row degrades to a no-op with a debug log.  This runs off a
    connection, and nothing about a courtesy import may break connecting
    to a printer.
    """
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
            # Moonraker without the [history] component.  Expected on
            # older/minimal installs; nothing to import, nothing wrong.
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
            # Kiln was there for this one under its own job id.  Its
            # record is the better one — it may have been observed
            # rather than inferred.
            result["skipped"] += 1
            continue
        try:
            if db.get_print_outcome(row["job_id"]) is not None:
                # Already imported.  Asking first (rather than letting the
                # write bounce) matters for a row still sitting at
                # 'unknown': the index would let that one be RE-resolved,
                # and an import must never rewrite a print it already
                # claimed — the user may be part-way through settling it.
                result["skipped"] += 1
                continue
            db.save_print_outcome(row)
            result["imported"] += 1
        except ValueError as exc:
            # The unique job_id index refusing a row we already hold — the
            # backstop under the check above, and the guard that keeps an
            # auto-record from overwriting a decided verdict.
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
