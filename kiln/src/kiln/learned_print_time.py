"""How long prints really take on a printer, learned from its own prints.

A slicer's time is a model of a machine, not the machine.  Every print Kiln
starts is written down with the slicer's time for its file (at the start,
:func:`slicer_seconds_for`) and, when Kiln can vouch for the ending, how long
it really printed (``kiln.printers.base._record_print_duration``).  This
module turns those pairs into one figure per printer: how much longer, or
shorter, its prints run than the slicer says.  Every door that states a print
time says it through :func:`learned_time` (a time for a file not printing
yet) or :func:`learned_time_left` (the print running now), so the answer and
its reason read the same everywhere.

The person's own prints on their own printer: free at every tier, computed on
their computer, and never sent anywhere.

What counts.  Only prints that finished (a cancelled or failed one stopped
short of the time it was estimated for), whose start Kiln sent with a file it
could read the slicer's time from, and whose length Kiln watched or the
printer's own clock reported.  A print that ended unseen is not a sample,
never a guess.  The last :data:`WINDOW` count; each is real time over slicer
time, a reading outside :data:`RATIO_BOUNDS` is left out (a print paused
overnight, a file whose estimate was for another machine), and the middle
reading is the printer's pace, so one odd print moves nothing.  Below
:data:`MIN_PRINTS` the slicer's time is said as the slicer's, with how many
prints Kiln has timed so far.

Honest bounds.  A pause the printer's clock counts is in the time.  A print
started outside Kiln, or from a file Kiln never read, teaches nothing.  One
figure per printer: a printer that runs slow on small parts and on time on
large ones is told the middle of the two.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Prints a printer needs before Kiln corrects the slicer's time for it.
MIN_PRINTS = 3

#: The newest prints a pace is read from, so a printer that changed (a new
#: nozzle, a faster profile) is followed within a few weeks of printing.
WINDOW = 20

#: A print shorter than this by the slicer's own time teaches little: its
#: heating and levelling are most of it, whatever the printer's pace.
MIN_ESTIMATE_SECONDS = 600.0

#: Real time over slicer time outside this range is not a pace: a print left
#: paused, or a file sliced for another machine.
RATIO_BOUNDS = (0.5, 2.5)


@dataclass(frozen=True)
class Pace:
    """A printer's pace: real print time over the slicer's, from its own prints."""

    printer: str
    factor: float
    prints: int

    @property
    def usable(self) -> bool:
        return self.prints >= MIN_PRINTS


# ---------------------------------------------------------------------------
# The start: what the file Kiln sent says (its time, and its slicer)
# ---------------------------------------------------------------------------


def _sent_file_lines(file_name: str | None) -> list[str] | None:
    """The slicer's own lines of the file a start sent, or ``None``.

    Read from the G-code Kiln sliced or uploaded under *file_name*: the slice
    ledger's file behind the printer-side name, else the copy this session
    uploaded.  Never raises.
    """
    if not file_name:
        return None
    try:
        from kiln._pro_cutter_bridge import local_sliced_path
        from kiln.file_metadata import sliced_gcode_lines
        from kiln.spool_usage import _uploaded_copy

        path = local_sliced_path(file_name) or _uploaded_copy(file_name)
        if not path:
            return None
        return sliced_gcode_lines(path) or None
    except Exception:  # noqa: BLE001 -- a file Kiln cannot read says nothing
        logger.debug("the sent file was not read for %s", file_name, exc_info=True)
        return None


def _seconds_in(lines: list[str] | None) -> float | None:
    if not lines:
        return None
    from kiln.gcode import slicer_print_time

    printed = slicer_print_time("\n".join(lines))
    return float(printed.seconds) if printed is not None and printed.seconds > 0 else None


def slicer_seconds_for(file_name: str | None) -> float | None:
    """The slicer's own time for the file a start sent, or ``None``.  Never raises."""
    try:
        return _seconds_in(_sent_file_lines(file_name))
    except Exception:  # noqa: BLE001 -- a file Kiln cannot read has no time
        logger.debug("no slicer time read for %s", file_name, exc_info=True)
        return None


def note_start_estimate(job_id: str | None, file_name: str | None) -> None:
    """Write what the sent file says onto the outcome row a start just opened:
    the slicer's time for it, and which slicer made it.

    Called from ``PrinterAdapter.start_print``.  The file is read once; a
    figure the file does not state is left empty, never guessed.  Never raises.
    """
    if not job_id or _hosted():
        return
    try:
        from kiln.gcode import slicer_made_by
        from kiln.persistence import get_db

        lines = _sent_file_lines(file_name)
        if not lines:
            return
        seconds = _seconds_in(lines)
        if seconds is not None:
            get_db().set_outcome_estimate(job_id, seconds)
        made_by = slicer_made_by("\n".join(lines))
        if made_by is not None:
            get_db().set_outcome_slicer(job_id, made_by.name, made_by.version)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("the sent file's figures were not written for %s", job_id, exc_info=True)


def note_print_seconds(job_id: str | None, seconds: float, printer_name: str | None = None) -> None:
    """Write how long a print really took, from an ending Kiln can vouch for.

    Called from ``kiln.printers.base._record_print_duration`` after its
    watched-or-frozen rule passed, with the printer the ending was filed
    under, so a reprint's length lands on the reprint's row.  Never raises.
    """
    if not job_id or _hosted():
        return
    try:
        from kiln.persistence import get_db

        names = _machine(printer_name)[0] if printer_name else None
        get_db().record_print_seconds(job_id, seconds, names)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("print length not written for %s", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# The pace
# ---------------------------------------------------------------------------


def pace_for(printer_name: str | None) -> Pace | None:
    """*printer_name*'s pace from its own prints, or ``None`` when it has none.

    The machine is found by its fingerprint and every name it is registered
    under, so a print filed under "default" counts for "my-voron".  ``None``
    on Kiln's servers, where no printer and no print is the caller's.
    """
    if not printer_name or _hosted():
        return None
    names, fingerprint = _machine(printer_name)
    try:
        from kiln.persistence import get_db

        rows = get_db().print_time_rows(names, fingerprint, limit=WINDOW)
    except Exception:  # noqa: BLE001 -- no records is no pace
        logger.debug("print times unread for %s", printer_name, exc_info=True)
        return None
    ratios = []
    for row in rows:
        estimate, actual = row.get("estimated_seconds"), row.get("print_seconds")
        if not estimate or not actual or estimate < MIN_ESTIMATE_SECONDS:
            continue
        ratio = float(actual) / float(estimate)
        if RATIO_BOUNDS[0] <= ratio <= RATIO_BOUNDS[1]:
            ratios.append(ratio)
    if not ratios:
        return None
    return Pace(printer=printer_name, factor=statistics.median(ratios), prints=len(ratios))


def resolve_printer(printer_name: str | None = None, printer_model: str | None = None) -> tuple[str | None, bool]:
    """The printer a time is for: the one named, else the only printer Kiln
    knows of when it is the model asked about (or no model was asked about).

    Returns ``(name, assumed)``; *assumed* is true when the name was not given.
    """
    if printer_name:
        return printer_name, False
    try:
        from kiln.assumed_nozzle import _registered_machines

        machines = _registered_machines()
    except Exception:  # noqa: BLE001 -- no registry is no printer
        return None, False
    if len(machines) != 1:
        return None, False
    only = machines[0]
    if printer_model:
        try:
            from kiln.registry import get_printer_registry

            declared = get_printer_registry().get(only).declared_printer_model()
        except Exception:  # noqa: BLE001 -- a model nobody declared matches nothing
            declared = None
        if not declared or str(declared).lower() != str(printer_model).lower():
            return None, False
    return only, True


def learned_time(
    slicer_seconds: float | None,
    printer_name: str | None = None,
    *,
    printer_model: str | None = None,
) -> dict[str, Any] | None:
    """The time a print of *slicer_seconds* will really take on the printer.

    ``None`` when there is no slicer time or no printer to learn from (none
    named, and not exactly one known of that model), and on Kiln's servers,
    where the caller's prints are not.  Otherwise::

        {"seconds", "slicer_seconds", "printer", "prints", "learned", "said"}

    ``seconds`` is the slicer's time corrected by the printer's pace once it
    rests on :data:`MIN_PRINTS` prints, else the slicer's; ``said`` says which,
    and from how many prints.
    """
    if not slicer_seconds or slicer_seconds <= 0 or _hosted():
        return None
    name, assumed = resolve_printer(printer_name, printer_model)
    if not name:
        return None
    pace = pace_for(name)
    prints = pace.prints if pace else 0
    on = f"on {name}" + (", the only printer Kiln knows of" if assumed else "")
    if pace is None or not pace.usable:
        so_far = f"{prints} so far" if prints else "none yet"
        return {
            "seconds": round(float(slicer_seconds)),
            "slicer_seconds": round(float(slicer_seconds)),
            "printer": name,
            "prints": prints,
            "learned": False,
            "said": (
                f"About {_duration(slicer_seconds)} by the slicer's estimate. Kiln corrects it from your own "
                f"prints {on} once it has timed {MIN_PRINTS} ({so_far})."
            ),
        }
    seconds = float(slicer_seconds) * pace.factor
    return {
        "seconds": round(seconds),
        "slicer_seconds": round(float(slicer_seconds)),
        "printer": name,
        "prints": pace.prints,
        "learned": True,
        "said": (
            f"About {_duration(seconds)}, from your last {pace.prints} prints {on} "
            f"(the slicer says {_duration(slicer_seconds)})."
        ),
    }


def attach_learned_time(
    block: dict[str, Any] | None,
    printer_name: str | None = None,
    *,
    printer_model: str | None = None,
) -> dict[str, Any] | None:
    """Put :func:`learned_time` on an estimate's dict, as ``learned_time``,
    from its ``estimated_time_seconds`` (the slicer's time).

    The one call every estimate door makes.  Returns what it attached.
    Never raises.
    """
    if not isinstance(block, dict):
        return None
    try:
        learned = learned_time(block.get("estimated_time_seconds"), printer_name or None, printer_model=printer_model)
    except Exception:  # noqa: BLE001 -- an estimate never fails on its forecast
        logger.debug("learned time not attached", exc_info=True)
        return None
    if learned:
        block["learned_time"] = learned
    return learned


def learned_time_left(printer_name: str | None, elapsed_seconds: float | None = None) -> dict[str, Any] | None:
    """When the print running now on *printer_name* should end, from its own pace.

    Read from the print Kiln started: the slicer's time for its file, scaled
    by the printer's pace, less how long it has been going (the printer's
    own elapsed clock when given, else since Kiln sent the start).  ``None``
    when Kiln did not start it with a file it could time, or the pace rests
    on fewer than :data:`MIN_PRINTS` prints, or it is already past that
    time (the printer's own countdown is then the better word).  Otherwise::

        {"seconds_left", "printer", "prints", "said"}
    """
    if not printer_name or _hosted():
        return None
    pace = pace_for(printer_name)
    if pace is None or not pace.usable:
        return None
    names, fingerprint = _machine(printer_name)
    try:
        import time

        from kiln.persistence import get_db

        running = get_db().running_print_estimate(names, fingerprint)
        if not running:
            return None
        elapsed = (
            float(elapsed_seconds)
            if isinstance(elapsed_seconds, (int, float)) and not isinstance(elapsed_seconds, bool) and elapsed_seconds >= 0
            else time.time() - float(running["created_at"])
        )
    except Exception:  # noqa: BLE001 -- no record is no time
        logger.debug("running print unread for %s", printer_name, exc_info=True)
        return None
    left = float(running["estimated_seconds"]) * pace.factor - elapsed
    if left <= 0:
        return None
    return {
        "seconds_left": round(left),
        "printer": printer_name,
        "prints": pace.prints,
        "said": f"About {_duration(left)} left, from your last {pace.prints} prints on {printer_name}.",
    }


def attach_time_left(job_block: dict[str, Any] | None, adapter: Any) -> dict[str, Any] | None:
    """Put :func:`learned_time_left` on a status door's job block, as
    ``learned_time_left``, when the job is running and the printer has a pace.

    The one call every live door makes, beside the printer's own countdown
    (which stays as the printer said it).  Returns what it attached.  Never
    raises.
    """
    try:
        if not isinstance(job_block, dict) or not job_block.get("file_name") or job_block.get("active") is False:
            return None
        from kiln.printers.base import outcome_printer_name

        left = learned_time_left(outcome_printer_name(adapter), job_block.get("print_time_seconds"))
    except Exception:  # noqa: BLE001 -- a status read never fails on a forecast
        logger.debug("learned time left not attached", exc_info=True)
        return None
    if left:
        job_block["learned_time_left"] = left
    return left


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _machine(printer_name: str) -> tuple[list[str], str | None]:
    """Every name *printer_name*'s machine is registered under, and its fingerprint."""
    names = [printer_name]
    fingerprint = None
    try:
        from kiln.registry import get_printer_registry, stable_machine_fingerprint

        registry = get_printer_registry()
        adapter = registry.get(printer_name)
        names = sorted(set(names) | set(registry.names_for(adapter)))
        fingerprint = stable_machine_fingerprint(adapter)
    except Exception:  # noqa: BLE001 -- an unregistered name is searched by itself
        pass
    return names, fingerprint


def _duration(seconds: float) -> str:
    from kiln.generation.validation import _human_duration

    return _human_duration(float(seconds))


def _hosted() -> bool:
    """On Kiln's servers no printer and no print is the caller's: learn nothing."""
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unsure is treated as hosted
        return True
