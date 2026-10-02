"""Counting a spool down as it prints.

Kiln keeps a spool list on the person's computer (``add_spool``) and tells
them "you have red PLA but it isn't loaded" from it
(:mod:`kiln.colour_availability`).  That sentence is only as true as each
spool's remaining figure, so this module is what moves the figure:

* a print Kiln STARTS takes what the sliced file says each filament uses
  from the spool that prints it.  Counted at the start, from the one door
  every start passes through (:meth:`PrinterAdapter.start_print`): it is the
  event Kiln causes and so cannot miss, where an ending is only seen when
  something happens to be watching.  A print that finishes on its own keeps
  its whole charge;
* a print Kiln itself CANCELS gives back the share it did not print, when
  the printer said how far it had got (:func:`before_cancel` /
  :func:`after_cancel`, around every backend's ``cancel_print``).  A cancel
  with the progress unknown, an emergency stop, and a print stopped at the
  machine keep the whole charge: nothing here guesses a share;
* a tray whose printer reports how much is left on its own spool sets the
  figure outright at that same start, before the count.  A reading is the
  machine's; a count is Kiln's arithmetic.

Which spool: each filament the file uses, the tray that prints it (the
start's own slot mapping when it carries one, else the matcher the print
gate uses, :func:`kiln.ams_routing.plan_ams_mapping`), then the recorded
spool in that tray: the one linked to it (``set_material(..., spool_id=...,
tool_index=<tray id>)``) unless the tray plainly holds something else, else
the one paired with it by colour and material, by the same pairing
:mod:`kiln.colour_availability` uses.  Two recorded spools that fit a tray
equally: the one with least left is taken, because a part-used spool is the
one a person loads before opening a new one.  A printer with no
multi-material unit has one feed per tool, and only a linked spool is
charged there: nothing says what colour is in it.

What is never done is a guess.  A file whose grams cannot be read, grams
that cannot be told apart per filament, a unit that could not be read, a
tray no recorded spool fits: nothing is charged.  Nothing here raises, and
nothing here waits on a printer in front of a start: the file and the
printer are read on a thread of their own, and only when there is a spool
on record to count against.

A count is not a measurement.  A print started outside Kiln is not in it,
and a print that fails on its own is charged whole.  The person's word
(``add_spool`` with a ``spool_id``) and a printer's reading both overwrite
it, and each spool carries which of the three last set its figure.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CancelReading",
    "after_cancel",
    "before_cancel",
    "charge_print",
    "resume_print",
    "settle",
    "urgent_stop",
]

#: The states of a remembered charge (``spool_charges.state``).
_OPEN = "open"  # charged; a cancel may still give some back
_GIVEN_BACK = "given_back"  # Kiln cancelled it and gave back what it had not printed
_CLOSED = "closed"  # nothing more to give back: progress unknown at the cancel, or resumed

#: How long a give-back waits for a count that is still being taken (a
#: cancel sent within a moment of the start).  It waits after the stop has
#: been sent, never in front of it.
_WORK_TIMEOUT_S: float = 5.0

#: The most a cancel waits for the printer to say how far the print has
#: got.  A stop matters more than a spool figure: past this the stop is
#: sent and the charge stays whole.
_PROGRESS_WAIT_S: float = 1.0

#: Set on a thread for the length of an emergency stop (:class:`urgent_stop`).
_urgent = threading.local()

#: One spool write sequence at a time in this process: a count, a
#: give-back and a resume read a figure, move it and remember what moved.
_work_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}
_workers_lock = threading.Lock()


# ---------------------------------------------------------------------------
# What the file uses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Usage:
    """What a sliced file says it uses.

    ``grams`` holds one figure per filament the file declares, in the
    file's own order, or ``None`` when the slicer's list cannot be laid
    against the declared filaments (it lists the used extruders only, and
    the file declares more).  ``total`` is the plate's grams either way.
    """

    filaments: tuple[Any, ...]
    grams: tuple[float, ...] | None
    total: float


def _file_usage(file_name: str | None) -> _Usage | None:
    """The grams *file_name* uses, or ``None`` when its own figures cannot be read."""
    from kiln._pro_cutter_bridge import local_sliced_path
    from kiln.ams_routing import read_file_filaments
    from kiln.file_metadata import sliced_gcode_lines
    from kiln.gcode import slicer_filament_totals

    path = local_sliced_path(file_name) or _uploaded_copy(file_name)
    if not path:
        return None
    lines = sliced_gcode_lines(path)
    if not lines:
        return None
    totals = slicer_filament_totals("\n".join(lines))
    total = totals.weight_g
    if not total or total <= 0:
        return None
    filaments = tuple(read_file_filaments(path).filaments)
    listed = tuple(float(g) for g in totals.grams) if totals.grams and sum(totals.grams) > 0 else (float(total),)
    aligned = not filaments or len(listed) == len(filaments)
    return _Usage(filaments=filaments, grams=listed if aligned else None, total=float(total))


def _uploaded_copy(file_name: str | None) -> str | None:
    """The local file this session uploaded under *file_name*, if the server knows one."""
    server = sys.modules.get("kiln.server")
    if server is None or not file_name:
        return None
    try:
        return server._local_copy_of(file_name)
    except Exception:  # noqa: BLE001 -- no copy is no figures
        return None


# ---------------------------------------------------------------------------
# The start
# ---------------------------------------------------------------------------


def charge_print(adapter: Any, file_name: str | None, kwargs: dict[str, Any] | None = None) -> None:
    """A print started: take what it will use from the spools that print it.

    Called from :meth:`PrinterAdapter.start_print`.  The caller's thread
    pays one look at the spool list (the local database); the file's own
    figures and the printer's loaded trays are read on a thread of its own,
    and only when there is a spool on record to count against.  Never
    raises.
    """
    try:
        if _hosted():
            return
        printer = _printer_name(adapter)
        if not file_name or not _db().list_spools():
            # No spool on record, so nothing to count.  The last print's
            # charge goes all the same: that print is over, and nothing can
            # be given back to it.
            _forget(printer)
            return
        # Not a daemon: `kiln print` sends the start and exits, and the
        # count has to land before it does.  The work is bounded by the
        # printer read's own timeout.
        worker = threading.Thread(
            target=_charge_worker,
            args=(adapter, printer, str(file_name), dict(kwargs or {})),
            name="kiln-spool-usage",
            daemon=False,
        )
        with _workers_lock:
            _workers[printer] = worker
        worker.start()
    except Exception:  # noqa: BLE001 -- a count never breaks or delays a start
        logger.debug("spool count not started", exc_info=True)


def _charge_worker(adapter: Any, printer: str, file_name: str, kwargs: dict[str, Any]) -> None:
    try:
        with _work_lock:
            _forget(printer)
            usage = _file_usage(file_name)
            charges = _take(adapter, printer, usage, kwargs) if usage is not None else []
            if charges:
                _db().save_spool_charge(printer, file_name, charges, state=_OPEN)
    except Exception:  # noqa: BLE001 -- a count that fails is a count that missed
        logger.debug("spool count not taken", exc_info=True)
    finally:
        with _workers_lock:
            if _workers.get(printer) is threading.current_thread():
                del _workers[printer]


def settle(timeout: float = _WORK_TIMEOUT_S) -> None:
    """Wait for every count still being taken.  For a caller that reads the
    spool list straight after a start, and for tests."""
    with _workers_lock:
        pending = list(_workers.values())
    for worker in pending:
        worker.join(timeout)


def _take(adapter: Any, printer: str, usage: _Usage, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Write the printer's own readings, then take this print's grams.

    Returns what was taken, one entry per spool charged; empty when nothing
    could be charged honestly.
    """
    from kiln.multi_material import KIND_UNKNOWN, multi_material_status

    tracker = _tracker()
    labels = _labels(adapter, printer)
    status = multi_material_status(adapter)
    if status.kind == KIND_UNKNOWN:
        # The unit could not be read, so which spool feeds is not known.
        return []
    if not status.detected:
        return _take_from_tools(tracker, labels, printer, usage)
    trays = list(status.slots)
    if not trays:
        return []
    held = _spools_in_trays(trays, labels, tracker)
    _write_readings(trays, held, tracker)
    if kwargs.get("use_ams") is False:
        # Fed from outside the unit, about which the unit says nothing.
        return []
    mapping = _tray_per_filament(usage, trays, kwargs)
    if mapping is None or usage.grams is None or len(mapping) != len(usage.grams):
        return []
    charges: list[dict[str, Any]] = []
    for grams, tray_id in zip(usage.grams, mapping, strict=True):
        hold = held.get(tray_id)
        if grams > 0 and hold is not None:
            charge = _charge(tracker, printer, hold, grams)
            if charge is not None:
                charges.append(charge)
    return charges


def _tray_per_filament(usage: _Usage, trays: list[Any], kwargs: dict[str, Any]) -> list[int] | None:
    """The tray id that prints each of the file's filaments, or ``None``.

    The start's own mapping when it carries one; otherwise the plan the
    print gate makes from the same file and the same trays.
    """
    given = kwargs.get("ams_mapping")
    if isinstance(given, (list, tuple)) and given:
        try:
            return [int(tray_id) for tray_id in given]
        except (TypeError, ValueError):
            return None
    if not usage.filaments:
        return None
    from kiln.ams_routing import plan_ams_mapping

    plan = plan_ams_mapping(list(usage.filaments), trays)
    return list(plan.mapping) if plan.ok and plan.mapping is not None else None


def _take_from_tools(tracker: Any, labels: list[str], printer: str, usage: _Usage) -> list[dict[str, Any]]:
    """A printer with no multi-material unit: each tool's linked spool.

    One feed per tool and nothing that reports what is in it, so only a
    spool the person linked to the tool is charged.
    """
    label, tools = None, {}
    for candidate in labels:
        materials = tracker.get_all_materials(candidate)
        if materials:
            label, tools = candidate, {int(m.tool_index): m for m in materials}
            break
    if label is None:
        return []
    if usage.grams is not None and len(usage.grams) > 1:
        wanted = dict(enumerate(usage.grams))
    elif set(tools) == {0}:
        # One figure and one feed: everything the plate uses comes from it.
        wanted = {0: usage.total}
    else:
        return []
    charges: list[dict[str, Any]] = []
    for tool_index, grams in wanted.items():
        loaded = tools.get(tool_index)
        spool = tracker.get_spool(loaded.spool_id) if loaded is not None and loaded.spool_id else None
        if grams > 0 and spool is not None:
            charge = _charge(tracker, printer, _Held(spool, label=label, tool_index=tool_index), grams)
            if charge is not None:
                charges.append(charge)
    return charges


# ---------------------------------------------------------------------------
# Which recorded spool is in which tray
# ---------------------------------------------------------------------------


@dataclass
class _Held:
    """The recorded spool a tray (or a tool) holds.

    ``label`` and ``tool_index`` name the loaded-material row that links
    it, when one does.  ``sure`` says no other reading of the records fits:
    only then may the printer's own figure for the tray be written to it.
    """

    spool: Any
    label: str | None = None
    tool_index: int | None = None
    sure: bool = True


def _spools_in_trays(trays: list[Any], labels: list[str], tracker: Any) -> dict[int, _Held]:
    """``{tray id: the recorded spool in it}`` for the trays one can be named for."""
    from kiln.ams_routing import MATCH_DELTA_E
    from kiln.colour_availability import _clearly_differs, _distance, _pair_best_first, _resolve_colour

    spools = tracker.list_spools()
    by_id = {spool.id: spool for spool in spools}
    colour_of = {spool.id: (_resolve_colour(spool.color) or (None,))[0] for spool in spools}

    def fits(tray: Any, spool: Any) -> bool:
        code = colour_of.get(spool.id)
        return (
            tray.hex6 is not None
            and code is not None
            and _distance(tray.hex6, code) <= MATCH_DELTA_E
            and not _clearly_differs(tray.material, spool.material_type)
        )

    held: dict[int, _Held] = {}
    # A link the person made stands unless the tray plainly holds something
    # else: the record is a claim, and it keeps saying the same thing after
    # a swap.
    for tray in trays:
        for label in labels:
            loaded = tracker.get_material(label, tray.tray_id)
            spool = by_id.get(loaded.spool_id) if loaded is not None and loaded.spool_id else None
            if spool is None or any(h.spool.id == spool.id for h in held.values()):
                continue
            code = colour_of.get(spool.id)
            contradicted = _clearly_differs(tray.material, spool.material_type) or (
                tray.hex6 is not None and code is not None and _distance(tray.hex6, code) > MATCH_DELTA_E
            )
            if not contradicted:
                held[tray.tray_id] = _Held(spool, label=label, tool_index=tray.tray_id)
                break

    taken_ids = {h.spool.id for h in held.values()} | _linked_elsewhere(labels)
    shelf = sorted(
        (s for s in spools if s.id not in taken_ids and colour_of.get(s.id) is not None),
        # Least left first, so two spools that fit a tray equally resolve to
        # the opened one; then the older, then the id, so it never varies.
        key=lambda s: (float(s.remaining_grams or 0.0), float(s.purchase_date or 0.0), s.id),
    )
    unlinked = [t for t in trays if t.tray_id not in held and t.hex6 is not None]
    live = [(s, colour_of[s.id]) for s in shelf if float(s.remaining_grams or 0.0) > 0]
    paired = _pair_best_first([t.hex6 for t in unlinked], live, set(), fits=lambda i, spool: fits(unlinked[i], spool))
    for i, tray in enumerate(unlinked):
        candidates = [s for s, _code in live if fits(tray, s)]
        if not candidates:
            # No spool with filament left fits.  One the count has emptied
            # may still be the spool in the tray: the printer's reading says.
            candidates = [s for s in shelf if float(s.remaining_grams or 0.0) <= 0 and fits(tray, s)]
            if len(candidates) != 1:
                continue
            spool = candidates[0]
        elif i in paired:
            spool = live[paired[i]][0]
        else:
            continue
        only = len(candidates) == 1 and not any(fits(other, spool) for other in unlinked if other is not tray)
        held[tray.tray_id] = _Held(spool, sure=only)
    return held


def _linked_elsewhere(labels: list[str]) -> set[str]:
    """Spools another printer's loaded-material record links: not on this one."""
    try:
        from kiln.material_inventory import _get_all_materials

        ours = set(labels)
        return {
            str(row["spool_id"])
            for row in _get_all_materials(_db())
            if row.get("spool_id") and row.get("printer_name") not in ours
        }
    except Exception:  # noqa: BLE001 -- unreadable links exclude nothing
        logger.debug("spool links not readable", exc_info=True)
        return set()


def _write_readings(trays: list[Any], held: dict[int, _Held], tracker: Any) -> None:
    """Set a spool's remaining from its tray's own reading, where there is one.

    A tray reports a percentage only for a spool the printer can measure
    (:func:`kiln.ams_routing.loaded_trays` leaves ``remain`` ``None``
    otherwise), and it is written only to a spool that tray holds beyond
    doubt.  The reading wins over whatever the count had.
    """
    from kiln.materials import MEASURED

    for tray in trays:
        hold = held.get(tray.tray_id)
        percent = tray.remain
        if hold is None or not hold.sure or not isinstance(percent, int) or not 0 <= percent <= 100:
            continue
        weight = float(hold.spool.weight_grams or 0.0)
        if weight <= 0:
            continue
        updated = tracker.set_spool_remaining(
            hold.spool.id, round(weight * percent / 100.0, 1), determined_by=MEASURED
        )
        if updated is not None:
            hold.spool = updated


def _charge(tracker: Any, printer: str, hold: _Held, grams: float) -> dict[str, Any] | None:
    """Take *grams* from the spool *hold* names; what was taken, to remember."""
    spool = tracker.get_spool(hold.spool.id)
    if spool is None:
        return None
    before = float(spool.remaining_grams or 0.0)
    if hold.label is not None and hold.tool_index is not None:
        tracker.deduct_usage(hold.label, grams, hold.tool_index)
    else:
        tracker.count_spool_usage(spool.id, grams, printer_name=printer)
    after = tracker.get_spool(spool.id)
    left = float(after.remaining_grams or 0.0) if after is not None else before
    return {
        "spool_id": spool.id,
        "grams": round(float(grams), 3),
        # Less than ``grams`` when the spool held less than the print needs.
        "taken": round(max(0.0, before - left), 3),
        "label": hold.label,
        "tool_index": hold.tool_index,
    }


# ---------------------------------------------------------------------------
# A cancel Kiln sends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelReading:
    """What was known just before Kiln sent a cancel to a charged print."""

    printer: str
    #: 0-100 as the printer reported it, or ``None`` when it did not.
    completion: float | None
    job_label: str | None = None


def before_cancel(adapter: Any) -> CancelReading | None:
    """Read how far the print has got, if Kiln charged spools for it.

    ``None`` -- and nothing asked of the printer -- when no charge is open
    for this printer, which is every cancel of a print Kiln did not start
    and every install with no spool list.  Otherwise one job read, taken
    before the stop because afterwards the printer no longer says where it
    was, and given at most :data:`_PROGRESS_WAIT_S`: a slow printer never
    holds the stop.  Inside :class:`urgent_stop` nothing is read at all.
    Never raises.
    """
    try:
        if _hosted():
            return None
        printer = _printer_name(adapter)
        with _workers_lock:
            counting = printer in _workers
        if not counting:
            row = _db().get_spool_charge(printer)
            if row is None or row.get("state") != _OPEN:
                return None
        if getattr(_urgent, "on", False):
            # An emergency stop that is this backend's cancel: nothing is
            # read in front of it.  The charge is closed whole afterwards.
            return CancelReading(printer, None, None)
        completion, label = _progress_within(adapter, _PROGRESS_WAIT_S)
        return CancelReading(printer, completion, label)
    except Exception:  # noqa: BLE001 -- bookkeeping never touches a stop
        logger.debug("spool charge not looked up before a cancel", exc_info=True)
        return None


def _progress_within(adapter: Any, seconds: float) -> tuple[float | None, str | None]:
    """How far the print has got and what it is, or ``(None, None)``.

    The job read runs on a thread of its own and is given *seconds*: a
    printer that is slow to answer never holds a stop for longer.  A read
    that comes back late is dropped and the charge stays whole.
    """
    box: dict[str, Any] = {}

    def read() -> None:
        try:
            job = adapter.get_job()
            label = getattr(job, "file_name", None)
            box["label"] = label if isinstance(label, str) else None
            done = getattr(job, "completion", None)
            if (
                getattr(job, "is_active", True) is True
                and isinstance(done, (int, float))
                and not isinstance(done, bool)
                and 0 <= done <= 100
            ):
                box["completion"] = float(done)
        except Exception:  # noqa: BLE001 -- progress not known; the charge stays
            logger.debug("progress not read before a cancel", exc_info=True)
        finally:
            box["done"] = True

    reader = threading.Thread(target=read, name="kiln-spool-progress", daemon=True)
    reader.start()
    reader.join(seconds)
    if not box.get("done"):
        return None, None
    return box.get("completion"), box.get("label")


class urgent_stop:
    """While open on this thread, a cancel reads nothing before it stops.

    For a backend whose emergency stop IS its cancel: the stop is sent with
    no job read in front of it, and the spools keep the whole charge.
    """

    def __enter__(self) -> None:
        self._was = getattr(_urgent, "on", False)
        _urgent.on = True

    def __exit__(self, *_exc: Any) -> None:
        _urgent.on = self._was


def after_cancel(reading: CancelReading | None) -> float:
    """The cancel landed: give back what the print had not used.

    Each spool gets back what was taken from it less the print's share so
    far (the file's grams for it, times the progress).  With the progress
    unknown, or the printer naming a different file than the one charged,
    the charge is left as it is and closed.  Returns the grams given back.
    Never raises.
    """
    if reading is None:
        return 0.0
    with _workers_lock:
        counting = _workers.get(reading.printer)
    if counting is not None and counting is not threading.current_thread():
        counting.join(_WORK_TIMEOUT_S)
    if not _work_lock.acquire(timeout=_WORK_TIMEOUT_S):
        return 0.0
    try:
        db = _db()
        row = db.get_spool_charge(reading.printer)
        if row is None or row.get("state") != _OPEN:
            return 0.0
        charges = [c for c in row["charges"] if isinstance(c, dict)]
        same_print = _same_job(row.get("file_name"), reading.job_label)
        total = 0.0
        if reading.completion is not None and same_print:
            tracker = _tracker()
            for charge in charges:
                used = float(charge.get("grams") or 0.0) * reading.completion / 100.0
                back = round(max(0.0, float(charge.get("taken") or 0.0) - used), 3)
                if back > 0 and _move(tracker, charge, -back):
                    charge["back"] = back
                    total += back
        db.save_spool_charge(
            reading.printer,
            str(row.get("file_name") or ""),
            charges,
            state=_GIVEN_BACK if total > 0 else _CLOSED,
            started_at=row.get("started_at"),
        )
        return total
    except Exception:  # noqa: BLE001 -- a give-back that fails leaves the charge
        logger.debug("spool give-back failed", exc_info=True)
        return 0.0
    finally:
        _work_lock.release()


def resume_print(adapter: Any) -> None:
    """A resume file started: the print Kiln cancelled is being finished.

    A mid-print change cancels the running print and starts a file that
    prints the rest of it, so what the cancel gave back is being used after
    all and is taken again.  A print that was never given anything back (it
    stopped on its own) is left alone.  Never raises.
    """
    try:
        if _hosted():
            return
        printer = _printer_name(adapter)
        db = _db()
        row = db.get_spool_charge(printer)
        if row is None or row.get("state") != _GIVEN_BACK:
            return
        # Local database writes only, on the caller's thread: a resume has
        # no printer to ask anything of.
        if not _work_lock.acquire(timeout=_WORK_TIMEOUT_S):
            return
        try:
            row = db.get_spool_charge(printer)
            if row is None or row.get("state") != _GIVEN_BACK:
                return
            tracker = _tracker()
            charges = [c for c in row["charges"] if isinstance(c, dict)]
            for charge in charges:
                back = float(charge.pop("back", 0.0) or 0.0)
                if back > 0:
                    _move(tracker, charge, back)
            # How far the whole print has got can no longer be read off the
            # file now printing, so a later cancel gives nothing back.
            db.save_spool_charge(
                printer,
                str(row.get("file_name") or ""),
                charges,
                state=_CLOSED,
                started_at=row.get("started_at"),
            )
        finally:
            _work_lock.release()
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a start
        logger.debug("spool charge not restored on resume", exc_info=True)


def _move(tracker: Any, charge: dict[str, Any], grams: float) -> bool:
    """Move the spool a remembered charge names by *grams* used (negative gives back)."""
    spool_id = charge.get("spool_id")
    if not spool_id:
        return False
    label, tool_index = charge.get("label"), charge.get("tool_index")
    if label and tool_index is not None:
        loaded = tracker.get_material(label, int(tool_index))
        if loaded is not None and loaded.spool_id == spool_id:
            # Still linked: the loaded-material row's own figure moves with it.
            return tracker.deduct_usage(label, grams, int(tool_index)) is not None
    return tracker.count_spool_usage(spool_id, grams) is not None


def _same_job(charged: Any, running: Any) -> bool:
    """Whether the job being cancelled can be the one that was charged.

    A name the printer does not report cannot contradict the record; a
    name that clearly belongs to another file does.
    """
    from kiln.printers.progress_motion import normalize_job_label

    ours, theirs = normalize_job_label(charged), normalize_job_label(running)
    return ours is None or theirs is None or ours == theirs


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _forget(printer: str) -> None:
    db = _db()
    if db.get_spool_charge(printer) is not None:
        db.clear_spool_charge(printer)


def _db() -> Any:
    from kiln.persistence import get_db

    return get_db()


def _tracker() -> Any:
    """The spool inventory on the database in force, publishing through the
    server's event bus when this process runs one."""
    from kiln.materials import MaterialTracker

    bus = None
    server = sys.modules.get("kiln.server")
    if server is not None:
        try:
            bus = server._get_event_bus()
        except Exception:  # noqa: BLE001 -- no bus is no events, never no count
            bus = None
    return MaterialTracker(db=_db(), event_bus=bus)


def _printer_name(adapter: Any) -> str:
    from kiln.printers.base import outcome_printer_name

    return str(outcome_printer_name(adapter))


def _labels(adapter: Any, printer: str) -> list[str]:
    """Every name this machine's material records may be filed under."""
    labels = [printer]
    try:
        from kiln.registry import get_printer_registry

        for name in get_printer_registry().names_for(adapter):
            if isinstance(name, str) and name and name not in labels:
                labels.append(name)
    except Exception:  # noqa: BLE001 -- an unregistered adapter has the one name
        logger.debug("printer labels not resolved", exc_info=True)
    return labels


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unsure is treated as hosted: count nothing
        return True
