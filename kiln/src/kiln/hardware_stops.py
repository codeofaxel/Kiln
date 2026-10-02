"""Hardware stops: the pauses a plan writes into a print for nuts, magnets and bearings.

A part with a captive nut, a magnet or a bearing sealed inside it gets that
piece during a pause: the printer stops just before the layer that prints
over the cavity, a person drops the piece in, and the print carries on over
it.  Kiln's hardware planner (kiln-pro, https://kiln3d.com) picks that layer
from the sliced file, writes the pause into the job, and writes one more line
near the top of the file -- the plan, as JSON::

    ; kiln_hardware_plan = {"v":1,"stops":[...],"after_print":[...]}

This module is the half that runs while the print does.  When the print
starts, the start gate reads that line back (:func:`stage`,
:func:`note_print_started`).  From then on every door that reads the printer
-- status, watch, wait -- asks :func:`observe`, which answers with one of:

* ``planned``      -- a stop is ahead, not yet close;
* ``coming_up``    -- a stop is close: have the parts ready and be there;
* ``now``          -- the printer has stopped for them: what goes in, and how;
* ``paused_elsewhere`` -- paused, but not at a planned stop;
* ``missed`` / ``passed_unseen`` -- the print went past a stop without Kiln
  seeing it stop;
* ``after_print``  -- the print is done: what goes in afterwards (a heat-set
  insert is pressed in after the print, never during it).

And :func:`stop_awaiting_hands` keeps a resume at a stop from going ahead
until the person says every piece is in: Kiln never resumes a hardware stop on
its own.

Nothing here picks a layer or knows which pause command a printer obeys; both
are in the file.  A file without the line is an ordinary print and none of
this speaks.

Honest bounds.  Which stop a pause belongs to is read from the printer's
layer counter, which Bambu and Duet report and most others do not; without
it, every pause while a stop is still ahead is treated as that stop and asks
for the person's word before resuming.  The time to a stop is re-estimated
from the printer's own progress and remaining time when it reports both,
and is otherwise given in layers or not at all.  A stop is called missed
only when Kiln read the printer often enough that a real pause could not
have slipped between two readings (:data:`WATCHED_GAP_S`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How the plan line starts.  Writers build the line with :func:`plan_line`.
PLAN_LINE_PREFIX = "; kiln_hardware_plan = "

#: The plan format this module reads.  A line of another version is ignored:
#: the print runs, and nothing is said about it.
PLAN_VERSION = 1

#: How many lines from the top the plan line is looked for.  It sits just
#: below the slicer's own header (:func:`plan_line_index`), a few dozen lines
#: down at most.
_HEAD_LINES = 400

#: Where a slicer's header block may end and still be the file's opening.
_HEADER_BLOCK_SCAN = 100

#: How long before a stop Kiln says it is coming, in minutes.  A judgement,
#: not a measurement: long enough to walk to the printer and find the parts,
#: short enough that nobody stands waiting at it.
PREALERT_MINUTES = 10.0

#: With no time to go on, the same warning this many layers ahead.
PREALERT_LAYERS = 5

#: Readings no further apart than this cannot miss a stop: putting a part in
#: and resuming takes a person longer.  A print seen going past a stop with a
#: longer gap in its readings is reported as unseen, never as missed.
WATCHED_GAP_S = 20.0

#: How long a finished print keeps saying what goes in afterwards.
AFTER_PRINT_WINDOW_S = 24 * 3600.0

#: Why a resume at a stop was refused, for a caller that branches on it.
NOT_CONFIRMED = "HARDWARE_NOT_CONFIRMED"

_STORE_NAME = "hardware_stops.json"
_SCHEMA_VERSION = 1
_LOCK = threading.RLock()

#: Plans the start gate read from the file it judged, by machine, waiting for
#: the start to succeed.  In memory only: a start that fails leaves nothing.
_staged: dict[str, tuple[str | None, dict[str, Any]]] = {}

#: When this process last read each machine printing or paused, and which
#: stops it has watched without a break since the print was below them.
#: In memory on purpose: "Kiln saw it go past without stopping" is a claim
#: about readings this process made, and a restart honestly forgets them.
_last_seen: dict[str, float] = {}
_watched: dict[str, set[int]] = {}


# ---------------------------------------------------------------------------
# The line in the file
# ---------------------------------------------------------------------------


def plan_line(plan: dict[str, Any]) -> str:
    """The comment line that carries *plan* in a job file.

    :raises ValueError: when *plan* is not one this module can read back, so
        a writer cannot put a line into a file that the print would then
        ignore without a word.
    """
    if _validated(plan) is None:
        raise ValueError("not a hardware plan this version of Kiln reads back")
    return PLAN_LINE_PREFIX + json.dumps(plan, separators=(",", ":"), ensure_ascii=True)


def plan_line_index(lines: list[str]) -> int:
    """Where the plan line goes in *lines*: just below the slicer's opening.

    Bambu Studio and OrcaSlicer open with a header block their printers and
    apps read, PrusaSlicer with the line naming itself; those stay first.
    The plan goes right below them, above any thumbnail, where a reader of
    the file's head finds it.  A file that opens with a thumbnail gets the
    line in front of it, never inside it.
    """
    for n, line in enumerate(lines[:_HEADER_BLOCK_SCAN]):
        if line.strip() == "; HEADER_BLOCK_END":
            return n + 1
    first = lines[0].strip().lower() if lines else ""
    if first.startswith(";") and "thumbnail" not in first:
        return 1
    return 0


def read_plan(path: str) -> dict[str, Any] | None:
    """The hardware plan in the job file at *path*, or ``None``.

    Reads the head of a plain G-code file or of a sliced 3MF's plate, with
    the same reader the start gate uses.  Never raises.
    """
    try:
        from kiln.printers.print_gate import _gcode_head

        head = _gcode_head(str(path), _HEAD_LINES)
    except Exception:  # noqa: BLE001 -- an unreadable file carries no plan
        return None
    marker = PLAN_LINE_PREFIX.strip()
    for line in head or ():
        text = line.strip()
        if not text.startswith(marker):
            continue
        try:
            return _validated(json.loads(text[len(marker):].strip()))
        except ValueError:
            return None
    return None


def _validated(data: Any) -> dict[str, Any] | None:
    """The parts of *data* this module uses, checked, or ``None``.

    A stop needs its number, the layer it comes before and what goes in; the
    rest is optional.  Malformed entries are dropped rather than failing the
    plan, and a plan with nothing left in it is no plan.
    """
    if not isinstance(data, dict) or data.get("v") != PLAN_VERSION:
        return None
    stops: list[dict[str, Any]] = []
    for raw in data.get("stops") or ():
        if not isinstance(raw, dict):
            continue
        n, layer, insert = raw.get("n"), raw.get("before_layer"), raw.get("insert")
        if not (isinstance(n, int) and isinstance(layer, int) and layer >= 1 and isinstance(insert, str) and insert):
            continue
        stops.append({
            "n": n,
            "before_layer": layer,
            "z": _number(raw.get("z")),
            "insert": insert,
            "minutes_in": _number(raw.get("minutes_in")),
            "steps": [s for s in raw.get("steps") or () if isinstance(s, str) and s],
            "magnets": bool(raw.get("magnets")),
        })
    after: list[dict[str, Any]] = []
    for raw in data.get("after_print") or ():
        if not isinstance(raw, dict) or not isinstance(raw.get("item"), str) or not raw["item"]:
            continue
        after.append({
            "item": raw["item"],
            "spoken": raw.get("spoken") if isinstance(raw.get("spoken"), str) else None,
            "kind": raw.get("kind") if isinstance(raw.get("kind"), str) else None,
            "seats": _texts(raw.get("seats")),
            "safety": _texts(raw.get("safety")),
            "where": _texts(raw.get("where")),
            "when": raw.get("when") if isinstance(raw.get("when"), str) else None,
            "next_calls": [c for c in raw.get("next_calls") or () if isinstance(c, dict) and c.get("tool")],
        })
    if not stops and not after:
        return None
    return {
        "v": PLAN_VERSION,
        "firmware": data.get("firmware") if isinstance(data.get("firmware"), str) else None,
        "stop_word": data.get("stop_word") if isinstance(data.get("stop_word"), str) else None,
        "layers": data.get("layers") if isinstance(data.get("layers"), int) else None,
        "total_min": _number(data.get("total_min")),
        "stops": sorted(stops, key=lambda s: (s["before_layer"], s["n"])),
        "after_print": after,
        "safety": [s for s in data.get("safety") or () if isinstance(s, str) and s],
    }


def _texts(value: Any) -> list[str]:
    """A list of non-empty strings from a string or a list of them."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [item for item in items if isinstance(item, str) and item]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# The record: which machine is printing which plan, and what Kiln has seen
# ---------------------------------------------------------------------------


def _store_path() -> Path:
    from kiln.startup_failure import kiln_home

    return kiln_home() / _STORE_NAME


def _read_store() -> dict[str, Any]:
    try:
        data = json.loads(_store_path().read_text())
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    machines = data.get("machines")
    return machines if isinstance(machines, dict) else {}


def _write_store(machines: dict[str, Any]) -> None:
    """Replace the record atomically.  Never raises into a caller."""
    tmp: str | None = None
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"version": _SCHEMA_VERSION, "machines": machines}, indent=2, sort_keys=True))
        os.replace(tmp, path)
        tmp = None
    except (OSError, ValueError, TypeError):
        logger.debug("hardware-stop record could not be written", exc_info=True)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def machine_key(adapter: Any) -> str:
    """The machine a record is filed under: its serial or address, else its name.

    The durable identity is the engagement record's, so one machine answering
    to two names is one record.  A machine with no durable identity is filed
    by its configured name, which is stable for as long as the config is.
    """
    try:
        from kiln.printers.engagement import machine_id

        found = machine_id(adapter)
    except Exception:  # noqa: BLE001
        found = ""
    return found or f"name:{getattr(adapter, 'name', '') or 'default'}"


def has_plan(adapter: Any) -> bool:
    """Whether a hardware plan is on record for *adapter*'s machine."""
    try:
        if not _store_path().is_file():
            return False
        return machine_key(adapter) in _read_store()
    except Exception:  # noqa: BLE001
        return False


def stage(adapter: Any, file_name: str, path: str | None) -> None:
    """Remember the plan in the file the start gate is judging.

    The gate holds the one copy of the job that is certain to be the file
    about to start -- the local file, or the printer's own copy read back
    over the network -- so the plan is read there, and kept until the start
    succeeds.  Never raises.
    """
    if not path:
        return
    try:
        plan = read_plan(path)
        key = machine_key(adapter)
        with _LOCK:
            if plan is None:
                _staged.pop(key, None)
            else:
                _staged[key] = (_label(file_name), plan)
    except Exception:  # noqa: BLE001 -- bookkeeping never decides a start
        logger.debug("hardware plan could not be staged", exc_info=True)


def note_print_started(adapter: Any, file_name: str, kwargs: dict[str, Any] | None = None) -> None:
    """A print started on *adapter*: file its hardware plan, or clear the old one.

    Called from the one success block every start passes through.  The plan
    is the one the gate staged for this file, else the one in a local copy of
    it; a start with no plan clears the machine's record, so a plan from an
    earlier print never speaks over a new one.  Never raises.
    """
    try:
        key = machine_key(adapter)
        label = _label(file_name)
        with _LOCK:
            staged = _staged.pop(key, None)
        plan = staged[1] if staged is not None and staged[0] == label else None
        if plan is None:
            plan = _plan_from_local_copy(file_name, kwargs or {})
        with _LOCK:
            machines = _read_store() if _store_path().is_file() else {}
            if plan is None:
                if machines.pop(key, None) is not None:
                    _write_store(machines)
                return
            now = time.time()
            machines[key] = {
                "file": os.path.basename(str(file_name or "")) or str(file_name),
                "started_at": now,
                "plan": plan,
                "stops": {},
                "last_seen_at": None,
                "finished_at": None,
            }
            _write_store(machines)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("hardware plan could not be filed at print start", exc_info=True)


def _plan_from_local_copy(file_name: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """The plan in a local copy of *file_name*: a path the door named, the name
    itself when it is a path, the file Kiln sliced or wrapped for it, or the
    source it was uploaded from."""
    candidates: list[str] = []
    for k in ("local_file_path", "source_path", "local_path", "gcode_path", "file_path", "threemf_path"):
        value = kwargs.get(k)
        if isinstance(value, str) and value:
            candidates.append(value)
    if isinstance(file_name, str) and file_name:
        candidates.append(file_name)
        with contextlib.suppress(Exception):
            from kiln.monitor_twin import sliced_entry_for

            entry = sliced_entry_for(file_name) or {}
            candidates.extend(str(entry.get(k) or "") for k in ("wrapped", "output"))
        with contextlib.suppress(Exception):
            from kiln.upload_manifest import resolve_source_path

            candidates.append(resolve_source_path(os.path.basename(file_name)) or "")
    for path in candidates:
        if path and os.path.isfile(path):
            plan = read_plan(path)
            if plan is not None:
                return plan
    return None


def _label(file_name: Any) -> str | None:
    from kiln.printers.progress_motion import normalize_job_label

    return normalize_job_label(file_name)


# ---------------------------------------------------------------------------
# What the print is doing, and what to tell the person
# ---------------------------------------------------------------------------


def observe(
    adapter: Any, state: Any, job: Any, *, now: float | None = None, announce: bool = False,
) -> dict[str, Any] | None:
    """What the hardware plan says about the print right now, or ``None``.

    The one helper every door that reads the printer calls -- status, watch,
    wait, the resume gate -- so each says the same thing, and records what it
    sees (a stop reached, a stop passed) as it goes.  ``new`` on the answer is
    ``True`` until a door that hands moments to the person -- the watcher and
    the wait tool, which pass *announce* -- has handed this one over; a status
    read shows a moment without using it up.  ``None`` when no plan is on
    record for this machine, or the machine is running a different job.
    Never raises.
    """
    try:
        if not _store_path().is_file():
            return None
        key = machine_key(adapter)
        with _LOCK:
            machines = _read_store()
            record = machines.get(key)
            plan = _validated(record.get("plan")) if isinstance(record, dict) else None
            if plan is None:
                return None
            clock = time.time() if now is None else now
            note, changed = _observe(key, record, plan, state, job, clock, announce)
            if changed:
                machines[key] = record
                _write_store(machines)
            return note
    except Exception:  # noqa: BLE001 -- a status read never fails on this
        logger.debug("hardware plan could not be observed", exc_info=True)
        return None


def _observe(
    key: str, record: dict[str, Any], plan: dict[str, Any], state: Any, job: Any, now: float, announce: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """Record what this reading shows, then answer with the most pressing moment:
    the stop itself, a stop gone past, a stop close, a stop ahead, the finish."""
    from kiln.printers.base import JobResult, PrinterStatus, confirmed_state_of

    reported = getattr(job, "file_name", None)
    if reported and _label(reported) != _label(record.get("file")):
        return None, False  # the machine is running another job
    word = confirmed_state_of(state)
    layer = _whole(getattr(job, "current_layer", None))
    marks: dict[str, Any] = record.setdefault("stops", {})
    changed = False

    def mark_of(stop: dict[str, Any]) -> dict[str, Any]:
        return marks.setdefault(str(stop["n"]), {})

    def hand_over(mark: dict[str, Any], moment: str) -> bool:
        """``new`` for a moment, and spend it when this door hands it over."""
        nonlocal changed
        given = mark.setdefault("announced", [])
        if moment in given:
            return False
        if announce:
            given.append(moment)
            changed = True
        return True

    # -- what this reading shows ---------------------------------------------
    if word in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
        _note_reading(key, plan, layer, now)
    if word is PrinterStatus.PRINTING:
        changed = _passings(key, plan, marks, layer, now) or changed

    # -- the stop itself -------------------------------------------------------
    if word is PrinterStatus.PAUSED:
        ahead = _next_stop(plan, marks, layer)
        if ahead is None:
            return None, changed
        # The counter reads the stop's own layer on a printer that counts the
        # way the slicer numbers them, and one less on one that counts from 0.
        if layer is not None and not ahead["before_layer"] - 1 <= layer <= ahead["before_layer"]:
            return _paused_elsewhere(ahead, layer), changed
        mark = mark_of(ahead)
        if mark.get("paused_at") is None:
            mark["paused_at"] = now
            changed = True
        new = hand_over(mark, "now")
        return _now(plan, ahead, certain=layer is not None, new=new, since=mark["paused_at"], now=now), changed

    # -- a stop gone past, until a waiting door has said so -------------------
    for stop in plan["stops"]:
        mark = marks.get(str(stop["n"])) or {}
        if mark.get("passed_how") and "went_past" not in (mark.get("announced") or ()):
            hand_over(mark_of(stop), "went_past")
            return _went_past(plan, stop, mark["passed_how"]), changed

    if word is PrinterStatus.PRINTING:
        ahead = _next_stop(plan, marks, layer)
        if ahead is None:
            return None, changed
        minutes = _minutes_to(plan, ahead, job)
        layers = ahead["before_layer"] - layer if layer is not None else None
        close = (minutes is not None and minutes <= PREALERT_MINUTES) or (
            minutes is None and layers is not None and layers <= PREALERT_LAYERS
        )
        if not close:
            return _planned(plan, ahead, minutes, layers), changed
        new = hand_over(mark_of(ahead), "coming_up")
        return _coming_up(plan, ahead, minutes, layers, new), changed

    if word is PrinterStatus.IDLE:
        if getattr(job, "ended_as", None) is not JobResult.COMPLETED or not plan["after_print"]:
            return None, changed
        finished = record.get("finished_at")
        if not isinstance(finished, (int, float)):
            record["finished_at"] = finished = now
            changed = True
        if now - finished > AFTER_PRINT_WINDOW_S:
            return None, changed
        new = hand_over(record.setdefault("finish", {}), "after_print")
        return _after_print(plan, new=new), changed

    return None, changed


def _note_reading(key: str, plan: dict[str, Any], layer: int | None, now: float) -> None:
    """Keep the run of unbroken readings: a gap longer than
    :data:`WATCHED_GAP_S` breaks it, and a reading below a stop starts it."""
    last = _last_seen.get(key)
    _last_seen[key] = now
    watched = _watched.setdefault(key, set())
    if last is None or now - last > WATCHED_GAP_S:
        watched.clear()
    if layer is not None:
        watched.update(stop["n"] for stop in plan["stops"] if layer < stop["before_layer"])


def _passings(
    key: str, plan: dict[str, Any], marks: dict[str, Any], layer: int | None, now: float,
) -> bool:
    """Record the stops the print has gone past since the last reading, and how:
    resumed from, ``missed`` (Kiln read the printer all the way through and it
    never stopped) or ``passed_unseen``.  ``True`` when anything was recorded."""
    if layer is None:
        return False
    recorded = False
    for stop in plan["stops"]:
        mark = marks.setdefault(str(stop["n"]), {})
        if mark.get("passed") is not None or layer <= stop["before_layer"]:
            continue
        mark["passed"] = now
        recorded = True
        if mark.get("paused_at") is None:
            mark["passed_how"] = "missed" if stop["n"] in _watched.get(key, ()) else "passed_unseen"
    return recorded


def _next_stop(plan: dict[str, Any], marks: dict[str, Any], layer: int | None) -> dict[str, Any] | None:
    """The first stop the print has not gone past (by its layer) or been resumed from."""
    for stop in plan["stops"]:
        mark = marks.get(str(stop["n"])) or {}
        if mark.get("passed") is not None or mark.get("done") is not None:
            continue
        if layer is not None and layer > stop["before_layer"]:
            continue
        return stop
    return None


def _minutes_to(plan: dict[str, Any], stop: dict[str, Any], job: Any) -> float | None:
    """Minutes until *stop*, from the printer's own progress and time left.

    The slicer's estimate places the stop as a share of the print; the
    printer's percent and remaining time say how far along that share the
    print is, and how long the rest really takes.  ``None`` when either side
    is missing -- an estimate built on one of them would be the plan-time
    guess the live reading exists to correct.
    """
    share_in = stop.get("minutes_in")
    total = plan.get("total_min")
    done = _number(getattr(job, "completion", None))
    left = _number(getattr(job, "print_time_left_seconds", None))
    if share_in is None or not total or done is None or left is None or done >= 100:
        return None
    share = share_in / total
    whole_s = left / (1.0 - done / 100.0)
    return max((share - done / 100.0) * whole_s / 60.0, 0.0)


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------


def _stop_block(plan: dict[str, Any], stop: dict[str, Any]) -> dict[str, Any]:
    return {
        "stop": stop["n"],
        "of": len(plan["stops"]),
        "before_layer": stop["before_layer"],
        "z_mm": stop["z"],
        "insert": stop["insert"],
    }


def _planned(plan, stop, minutes, layers) -> dict[str, Any]:
    when = (f"in about {_minutes(minutes)}" if minutes is not None
            else f"in {layers} layers" if layers is not None else "later in this print")
    return {
        "stage": "planned", "new": False, **_stop_block(plan, stop),
        "minutes_to_stop": _rounded(minutes), "layers_to_stop": layers,
        "say": f"The printer stops before layer {stop['before_layer']} {when} so {stop['insert']} can go in.",
    }


def _coming_up(plan, stop, minutes, layers, new) -> dict[str, Any]:
    when = f"In about {_minutes(minutes)}" if minutes is not None else f"In {layers} layers"
    return {
        "stage": "coming_up", "new": new, **_stop_block(plan, stop),
        "minutes_to_stop": _rounded(minutes), "layers_to_stop": layers,
        "say": (f"{when} the printer stops before layer {stop['before_layer']} so you can put in "
                f"{stop['insert']}. Have the parts ready"
                + (" (and non-magnetic tweezers)" if stop["magnets"] else "")
                + " and be at the printer: it waits, hot, until you come."),
    }


def _now(plan, stop, *, certain: bool, new: bool, since: float, now: float) -> dict[str, Any]:
    where = f"before layer {stop['before_layer']}" + (f" ({stop['z']:g} mm)" if stop["z"] is not None else "")
    opening = (f"Now is the time to put in {stop['insert']}. The printer has stopped {where}, "
               "the layer that prints over it." if certain else
               f"The printer has paused. If this is the planned stop {where}, now is the time to put in "
               f"{stop['insert']}; if it paused for something else, say so.")
    return {
        "stage": "now", "new": new, "certain": certain, **_stop_block(plan, stop),
        "waiting_minutes": _rounded((now - since) / 60.0),
        "say": opening,
        "steps": stop["steps"] or [f"Put in: {stop['insert']}.",
                                    "Check each piece is level with or below the top of the print."],
        "safety": plan["safety"],
        "resume": ("Kiln does not resume this stop on its own. When the person says every piece is in and "
                   "sits level with or below the top of the print, resume with resume_print(hardware_confirmed=true)."),
    }


def _paused_elsewhere(stop, layer) -> dict[str, Any]:
    return {
        "stage": "paused_elsewhere", "new": False, "layer": layer,
        "next_stop_before_layer": stop["before_layer"],
        "say": (f"The printer is paused on layer {layer}, which is not a planned hardware stop "
                f"(the next one is before layer {stop['before_layer']}). Nothing goes in yet."),
    }


def _went_past(plan, stop, how: str) -> dict[str, Any]:
    if how == "missed":
        say = (f"The printer went past layer {stop['before_layer']} without stopping, so {stop['insert']} "
               "can no longer go in: the cavity is printed over. Let the print finish without it, or cancel "
               "and print again.")
        word = plan.get("stop_word")
        if word:
            say += f" The pause in the file ({word}) did not stop this printer; plan it again before the next print."
    else:
        say = (f"The print is past layer {stop['before_layer']}, where it was to stop for {stop['insert']}. "
               "Kiln was not reading the printer at that moment, so it cannot tell whether it stopped and the "
               "parts went in. If they did not, the cavity is printed over.")
    return {"stage": how, "new": True, **_stop_block(plan, stop), "say": say}


def _after_print(plan, *, new: bool) -> dict[str, Any]:
    lines, safety = [], []
    for step in plan["after_print"]:
        verb = "press in" if step["kind"] == "heat_set_insert" else "put in"
        seats = step["seats"]
        target = (f", one in each of {_listed(seats)}" if len(seats) > 1 else f" ({seats[0]})" if seats else "")
        lines.append(f"{verb} {step['spoken'] or step['item']}{target}." + (f" {step['when']}" if step["when"] else ""))
        safety.extend(line for line in step["safety"] if line not in safety)
    return {
        "stage": "after_print", "new": new,
        "say": "The print has finished. Now " + " ".join(lines),
        "after_print": plan["after_print"],
        "safety": safety,
    }


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _minutes(value: float) -> str:
    whole = max(round(value), 1)
    return f"{whole} minute" + ("" if whole == 1 else "s")


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------


def stop_awaiting_hands(adapter: Any) -> dict[str, Any] | None:
    """The ``now`` note when *adapter*'s printer is paused at a planned stop.

    Asked by the resume template before anything is sent: a printer waiting
    at a stop resumes only on the person's word that every piece is in.
    Every uncertainty -- no plan, an unreadable printer, a pause that is not
    a stop -- answers ``None`` and lets the resume go on, as every other
    resume check does.
    """
    if not has_plan(adapter):
        return None
    try:
        from kiln.printers.base import read_status

        state, job = read_status(adapter)
    except Exception:  # noqa: BLE001 -- never block a resume on a read error
        return None
    note = observe(adapter, state, job)
    return note if note and note.get("stage") == "now" else None


def note_resumed(adapter: Any, note: dict[str, Any]) -> None:
    """Record the person's word on the stop in *note*, once the resume took.

    Without a layer counter the stop is also marked done, since no later
    reading will ever show the print past it.  Never raises.
    """
    try:
        key = machine_key(adapter)
        with _LOCK:
            machines = _read_store()
            record = machines.get(key)
            if not isinstance(record, dict):
                return
            mark = record.setdefault("stops", {}).setdefault(str(note["stop"]), {})
            mark["confirmed_at"] = time.time()
            if not note.get("certain"):
                mark["done"] = mark["confirmed_at"]
            _write_store(machines)
    except Exception:  # noqa: BLE001
        logger.debug("hardware stop confirmation could not be recorded", exc_info=True)


def refusal_message(note: dict[str, Any]) -> str:
    """What a refused resume says: the stop, and the way through."""
    return (f"{note['say']} Kiln will not resume this stop until the person says every piece is in and sits "
            "level with or below the top of the print. Then resume with hardware_confirmed=true, or press "
            "resume on the printer itself.")


def forget_process_state() -> None:
    """Forget what this process staged and watched (a fresh process; tests)."""
    with _LOCK:
        _staged.clear()
        _last_seen.clear()
        _watched.clear()
