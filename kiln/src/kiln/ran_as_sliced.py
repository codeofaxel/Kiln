"""Whether a print ran the way its file was sliced.

A slicer's time is the time of its file printed as written.  A print whose
speed or flow was changed, that paused, whose objects were skipped, or that
carried on from a file other than the one it started with ran some other
way, so its real length says nothing about the printer's pace, and the
slicer's time for it no longer holds.

Each print Kiln starts keeps a list of plain reasons it did not run as
sliced, on its outcome row (``not_as_sliced`` in :mod:`kiln.persistence`),
each with who saw it: :data:`KILN` (Kiln did it) or :data:`PRINTER` (the
printer reported it, so it was done on its own screen or app).  Every
reason is written through :func:`note`, from the one place each happens:
the adapter template's command chokepoints and its status reads
(:mod:`kiln.printers.base`) and the start's own reading of its file.

Unknown stays unknown.  An empty list means nothing was seen, never that
the print ran as sliced: a change made while no Kiln process was watching
is not on it.  A row with no list at all is a print started before the
list existed.

The person's own prints on their own printer: written on their computer and
never sent anywhere.  Nothing here raises into a print.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: Who saw a reason: Kiln did the thing itself.
KILN = "kiln"
#: Who saw a reason: the printer reported it.
PRINTER = "printer"

#: The speed was changed from the sliced speed (a preset, a percentage, a
#: speed schedule).
SPEED_CHANGED = "speed_changed"
#: The flow was changed from the sliced flow.
FLOW_CHANGED = "flow_changed"
#: The print waited on a heater it was told to reach.
TEMPERATURE_WAIT = "temperature_wait"
#: The print stood paused (a person, a filament change, a planned stop, a hold).
PAUSED = "paused"
#: The print carried on from a file edited while it ran.
EDITED = "edited"
#: Objects on the plate were skipped.
OBJECTS_SKIPPED = "objects_skipped"
#: The print was picked up again after it stopped (a recovery, a firmware resume).
RESUMED = "resumed"
#: The file started carries an earlier print on from a layer.
START_FILE_EDITED = "start_file_edited"

KINDS = frozenset({
    SPEED_CHANGED, FLOW_CHANGED, TEMPERATURE_WAIT, PAUSED, EDITED,
    OBJECTS_SKIPPED, RESUMED, START_FILE_EDITED,
})

#: Said, in place of a time, for a print Kiln knows is not running as sliced.
NOT_AT_SLICED_SPEED = (
    "This print is not running the way its file was sliced, so the slicer's time "
    "at the sliced speed no longer holds."
)

#: Commands that change how a print runs, by the reason each is.
_COMMAND_REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^M220\b", re.I), SPEED_CHANGED),
    (re.compile(r"^SET_VELOCITY_LIMIT\b", re.I), SPEED_CHANGED),
    (re.compile(r"^M221\b", re.I), FLOW_CHANGED),
    (re.compile(r"^(?:M109|M190|M191|TEMPERATURE_WAIT)\b", re.I), TEMPERATURE_WAIT),
    (re.compile(r"^(?:M0|M1|M25|M226|M600|M601|PAUSE)\b", re.I), PAUSED),
    (re.compile(r"^(?:M486|EXCLUDE_OBJECT)\b", re.I), OBJECTS_SKIPPED),
)


def _hosted() -> bool:
    from kiln.learned_print_time import _hosted as hosted

    return hosted()


def _noted(adapter: Any) -> set[tuple[str, str]]:
    """What this adapter already wrote for the print it runs now."""
    seen = getattr(adapter, "_ran_as_sliced_noted", None)
    if not isinstance(seen, set):
        seen = set()
        with contextlib.suppress(Exception):  # an adapter that keeps nothing writes each time
            adapter._ran_as_sliced_noted = seen
    return seen


def started(adapter: Any) -> None:
    """Forget what was written for the print before: a new one started."""
    with contextlib.suppress(Exception):
        adapter._ran_as_sliced_noted = set()


def note(adapter: Any, kind: str, seen_by: str = KILN) -> bool:
    """Write that the print open on *adapter*'s machine did not run as
    sliced, for *kind*, seen by *seen_by*.  The one writer of a reason.

    Written onto the newest open print Kiln started on that machine; with
    none open, nothing is written.  Returns whether a row took it.  Never
    raises.
    """
    if kind not in KINDS or seen_by not in (KILN, PRINTER) or _hosted():
        return False
    try:
        seen = _noted(adapter)
        if (kind, seen_by) in seen:
            return False
        from kiln.persistence import get_db
        from kiln.printers.base import outcome_printer_names
        from kiln.registry import machine_fingerprint

        try:
            fingerprint = machine_fingerprint(adapter)
        except Exception:  # noqa: BLE001 -- the names find it then
            fingerprint = None
        took = get_db().note_not_as_sliced(
            kind, seen_by, printer_names=outcome_printer_names(adapter), machine_fingerprint=fingerprint,
        )
        seen.add((kind, seen_by))
        return took
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("reason %s not written", kind, exc_info=True)
        return False


def note_on_job(job_id: str | None, kind: str, seen_by: str = KILN) -> bool:
    """:func:`note` for the row a start just opened, by its job id."""
    if not job_id or kind not in KINDS or seen_by not in (KILN, PRINTER) or _hosted():
        return False
    try:
        from kiln.persistence import get_db

        return get_db().note_not_as_sliced(kind, seen_by, job_id=job_id)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("reason %s not written for %s", kind, job_id, exc_info=True)
        return False


def open_record(job_id: str | None) -> None:
    """Start the list for the row a start opened: watched from its start,
    nothing seen yet.  Never raises."""
    if not job_id or _hosted():
        return
    try:
        from kiln.persistence import get_db

        get_db().open_as_sliced_record(job_id)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("record not opened for %s", job_id, exc_info=True)


def note_commands(adapter: Any, commands: Any) -> None:
    """Write a reason for each raw command sent to a printer that changes
    how its print runs (speed, flow, a heater wait, a pause, a skipped
    object).  Called from the adapter template's ``send_gcode``.  Never
    raises."""
    try:
        if isinstance(commands, str):
            commands = commands.splitlines()
        if not isinstance(commands, Iterable):
            return
        for raw in commands:
            if not isinstance(raw, str):
                continue
            for line in raw.splitlines():
                word = line.split(";", 1)[0].strip()
                for pattern, kind in _COMMAND_REASONS:
                    if word and pattern.match(word):
                        note(adapter, kind, KILN)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("commands not read for reasons", exc_info=True)


def observe_state(adapter: Any, state: Any) -> None:
    """Write a reason the printer itself reports: it stands paused, or it
    runs at a speed other than the sliced one.  Called from the adapter
    template's status read.  Never raises."""
    try:
        from kiln.printers.base import PrinterStatus

        status = getattr(state, "state", None)
        if status is PrinterStatus.PAUSED:
            note(adapter, PAUSED, PRINTER)
            return
        if status is not PrinterStatus.PRINTING:
            return
        magnitude = getattr(state, "speed_magnitude", None)
        if isinstance(magnitude, int) and not isinstance(magnitude, bool) and magnitude > 0 and magnitude != 100:
            note(adapter, SPEED_CHANGED, PRINTER)
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks status
        logger.debug("printer state not read for reasons", exc_info=True)


def reasons_of(row: Any) -> list[dict[str, str]] | None:
    """The reasons on an outcome row: a list (maybe empty), or ``None`` for a
    row started before the list existed."""
    value = row.get("not_as_sliced") if isinstance(row, dict) else None
    if value is None:
        return None
    try:
        reasons = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if not isinstance(reasons, list):
        return None
    return [r for r in reasons if isinstance(r, dict) and r.get("kind") in KINDS]


def known_not_as_sliced(row: Any) -> bool:
    """Whether the row names a reason its print did not run as sliced."""
    return bool(reasons_of(row))


__all__ = [
    "EDITED", "FLOW_CHANGED", "KILN", "KINDS", "NOT_AT_SLICED_SPEED", "OBJECTS_SKIPPED", "PAUSED",
    "PRINTER", "RESUMED", "SPEED_CHANGED", "START_FILE_EDITED", "TEMPERATURE_WAIT",
    "known_not_as_sliced", "note", "note_commands", "note_on_job", "observe_state", "open_record",
    "reasons_of", "started",
]
