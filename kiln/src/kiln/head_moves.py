"""What a printer does with its head when a print Kiln started pauses, is cancelled or ends.

Kiln judges where a second part may go on a plate from a record of how
each printer model moves its head on its own, and a block nobody has
described is judged at the worst case.  The guided session
(:mod:`kiln.bench`) fills those blanks on purpose.  This module fills them
from real use: when Kiln itself sends a pause, a resume or a cancel to a
printer that says where its head is (:func:`kiln.bench.logs_positions`: a
Klipper printer through Moonraker, a Marlin printer over USB), it logs the
head while the block runs and keeps the same observation the guided session
keeps, in the same place.  From there it rides every placement request for
that printer and the same telemetry-gated send.

Every door that pauses or cancels reaches it, because it sits on the
adapter: each adapter's own ``pause_print`` and ``cancel_print`` are
wrapped when the class is made (``PrinterAdapter.__init_subclass__``) and
the one resume template calls :func:`around` itself.  The end of a print
is logged from its last minutes: the print watchdog Kiln runs beside every
print it starts hands each reading it takes to :func:`on_progress`, which
starts the log once the printer says the print is nearly done.  A pause or
cancel Kiln did not send (a runout, the printer's own screen) is seen only
after the head has moved, so the path is gone and nothing is written.

What a log teaches is said once, on the person's next reply
(:func:`kiln.bench.note_learned`), and counted in the lesson funnel.

Never in the way of the print: the log runs beside it on its own thread,
the command goes out at once, its result comes back untouched, nothing is
ever sent to the printer from here, and nothing here raises.  Off on Kiln's servers, under a test run or a sandbox unless a
test asks for it by name, and switched off with ``KILN_HEAD_MOVE_WATCH=0``.

What is written is :func:`kiln.bench.logged_block`'s to decide.  A pause is
written only with its way back, so Kiln must also send the resume; a cancel
or an end only once the head has settled; none when the log cannot tell the
print from the block or the print was not one Kiln started.  The print's
footprint is read when the log begins, while the plate record still holds
the running print: once a print has ended the record says only that the
part is still there.  An observation from
a guided session is never replaced by one from real use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from kiln import bench

logger = logging.getLogger(__name__)

#: ``KILN_HEAD_MOVE_WATCH=0`` switches the watch off on this machine.
SWITCH_ENV = "KILN_HEAD_MOVE_WATCH"
#: A test that wants the watch to run sets this.
TEST_OPT_IN_ENV = "KILN_HEAD_MOVE_WATCH_IN_TESTS"
#: The longest a pause or cancel is logged: a printer may finish a long
#: line, or a slow layer, before it acts.
BLOCK_MAX_S = 300.0
#: The longest the way back is logged once the resume is sent.
BACK_MAX_S = 180.0
#: No movement this long after the command: the printer was not printing,
#: and there is nothing to learn.
QUIET_S = 20.0
#: How long the head must read still before a block counts as over, and
#: how often it is read: the guided session's own.
SETTLE_S = bench.LOG_SETTLE_S
HZ = bench.LOG_HZ

#: How near its end a print must be before its end is logged: the
#: printer's own time left, or, when it gives none, how complete it says it is.
END_WINDOW_S = 180
END_COMPLETION = 99.0
#: The longest an end is logged: the last minutes and the end sequence.
END_MAX_S = 1200.0
#: How often the end log asks the printer whether the print is over.
END_STATE_EVERY_S = 2.0
#: How often the decision after a print looks for the last log to be done.
AFTER_PRINT_WAIT_S = 1.0

#: The blocks this module watches, and what each is in the observation.
BLOCKS = ("pause", "cancel", "resume", "end")

_LOCK = threading.Lock()
#: One block logged at a time per machine (by unit): the log in progress.
_RUNNING: dict[str, Any] = {}
#: A pause whose way back has not been seen yet, by unit: its log, the
#: footprint of the print it paused and the layer it began at.
_PAUSED: dict[str, dict[str, Any]] = {}
#: The print whose end is being (or was) logged, by unit, so a print's end
#: is logged once.
_ENDS: dict[str, str] = {}
#: The machines whose first print's offer is being decided, by unit.
_DECIDING: set[str] = set()
#: Set while a watched command is being sent, so an adapter that hands the
#: command on to another adapter (Creality's backend) is not logged twice.
_HERE = threading.local()

__all__ = ["BLOCKS", "SWITCH_ENV", "TEST_OPT_IN_ENV", "around", "enabled", "on_progress", "print_ended"]


def enabled() -> bool:
    """Whether this process watches the blocks it sends."""
    if os.environ.get(SWITCH_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return False
    except Exception:  # noqa: BLE001
        return False
    if os.environ.get(TEST_OPT_IN_ENV, "").strip() == "1":
        return True
    try:
        from kiln.dev_home_guard import SANDBOX_ENV, _pytest_running

        if _pytest_running() or os.environ.get(SANDBOX_ENV, "").strip():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def around(adapter: Any, block: str, send: Callable[[], Any]) -> Any:
    """Send *block* (``pause``, ``cancel`` or ``resume``) through *send*,
    logging the head beside it where this printer says where its head is.
    Returns *send*'s result, or raises its exception, untouched."""
    if getattr(_HERE, "sending", False) or _urgent():
        return send()
    watch = _begin(adapter, block)
    if watch is None:
        return send()
    _HERE.sending = True
    try:
        result = send()
    except BaseException:
        _drop(watch)
        raise
    finally:
        _HERE.sending = False
    try:
        _after(watch, result)
    except Exception:  # noqa: BLE001 -- learning never touches a command
        logger.debug("head moves: not followed", exc_info=True)
        _drop(watch)
    return result


# ---------------------------------------------------------------------------
# Starting and ending a log
# ---------------------------------------------------------------------------


def _urgent() -> bool:
    """An emergency stop that is a cancel on this backend: never watched."""
    try:
        from kiln.spool_usage import stopping_urgently

        return stopping_urgently()
    except Exception:  # noqa: BLE001
        return True


def _begin(adapter: Any, block: str) -> dict[str, Any] | None:
    """Start the log for *block* on *adapter*'s machine, or ``None`` when
    there is nothing to learn: the watch is off, the printer does not say
    where its head is, the machine has no identity, a log is already
    running there, or (for a resume) no watched pause is waiting."""
    try:
        if block not in BLOCKS or not enabled():
            return None
        if not bench.logs_positions(adapter):
            return None
        unit = bench.unit_of(adapter)
        if not unit:
            return None
        stop_when = None
        until_idle = None
        max_s = BLOCK_MAX_S
        footprint = bench.print_footprint(adapter)
        if block == "end":
            max_s = END_MAX_S
            until_idle = _ended_reader(adapter)
        if block == "resume":
            with _LOCK:
                paused = _PAUSED.get(unit)
            if paused is None:
                return None
            layer = float(paused["layer"])
            max_s = BACK_MAX_S

            def stop_when(log: Any) -> bool:
                return bench.way_back(log.points, log.extruder, layer) is not None

        log = bench.PositionLog(adapter, settle_s=SETTLE_S, hz=HZ, max_s=max_s, quiet_s=QUIET_S, stop_when=stop_when,
                                until_idle=until_idle)
        with _LOCK:
            if unit in _RUNNING:
                return None
            _RUNNING[unit] = log
        log.start()
        return {"adapter": adapter, "block": block, "unit": unit, "log": log, "max_s": max_s, "footprint": footprint}
    except Exception:  # noqa: BLE001
        logger.debug("head moves: no log started", exc_info=True)
        return None


def _drop(watch: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        watch["log"].stop()
    with _LOCK:
        if _RUNNING.get(watch["unit"]) is watch["log"]:
            _RUNNING.pop(watch["unit"], None)


def _after(watch: dict[str, Any], result: Any) -> None:
    """The command went out: follow the log to its end on its own thread,
    or drop it when the printer refused the command."""
    if watch["block"] == "cancel":
        with _LOCK:
            # A cancelled print never comes back: a pause waiting for its
            # way back will not see one.
            _PAUSED.pop(watch["unit"], None)
    if not getattr(result, "success", False):
        _drop(watch)
        return
    threading.Thread(target=_finish, args=(watch,), name="kiln-head-moves", daemon=True).start()


def _finish(watch: dict[str, Any]) -> None:
    """Wait for the log, then keep what it shows -- or, for a pause, hold
    it until Kiln sends the resume.  Never raises."""
    log = watch["log"]
    with contextlib.suppress(Exception):
        log.join(timeout=float(watch["max_s"]) + 30.0)
    try:
        _keep(watch)
    except Exception:  # noqa: BLE001
        logger.debug("head moves: nothing kept", exc_info=True)
    finally:
        # Released only once what the log showed is kept, so whatever waits
        # for this machine's log reads what it taught.
        with _LOCK:
            if _RUNNING.get(watch["unit"]) is log:
                _RUNNING.pop(watch["unit"], None)


def _keep(watch: dict[str, Any]) -> None:
    adapter, block, unit, log = watch["adapter"], watch["block"], watch["unit"], watch["log"]
    footprint = watch.get("footprint")
    if block == "pause":
        found = bench.block_start(list(log.points), list(log.extruder), footprint) if (
            footprint is not None and getattr(log, "complete", False)) else None
        if found is None or isinstance(found, str):
            logger.debug("head moves: pause not held (%s)", found or "no footprint or cut short")
            return
        with _LOCK:
            _PAUSED[unit] = {"log": log, "footprint": footprint, "layer": found[1]}
        return
    if block == "resume":
        with _LOCK:
            paused = _PAUSED.pop(unit, None)
        if paused is None:
            return
        found = bench.logged_block([paused["log"], log], paused["footprint"], returns=True)
        name = "pause"
    elif block == "end":
        found = bench.ended_block(log, footprint)
        name = "end"
    else:
        found = bench.logged_block([log], footprint, returns=False)
        name = "cancel"
    if isinstance(found, str):
        logger.debug("head moves: %s not kept (%s)", name, found)
        return
    _write(adapter, name, *found)


def _owner_stated_bed(model: str | None) -> tuple[float, float] | None:
    """A model the catalogue does not know yet: the bed its owner stated on
    this machine, so what it does is still kept and can teach the record
    that model gets one day."""
    try:
        from kiln.printers.bed_fit import owner_stated_build_volume

        volume = owner_stated_build_volume(model)
    except Exception:  # noqa: BLE001
        return None
    return (float(volume[0]), float(volume[1])) if volume else None


def _write(adapter: Any, block: str, points: list[tuple[float, float, float]], layer_z: float) -> None:
    from kiln import plate_state

    model = plate_state.declared_model_of(adapter)
    bed = bench.bed_of(model) or _owner_stated_bed(model)
    if not model or bed is None:
        return
    doc = bench.observation(
        model, block, "position_log", bed_mm=bed, layer_z_mm=layer_z, points=points,
        returns=(block == "pause"), unit=bench.unit_of(adapter), firmware=bench.firmware_of(adapter),
    )
    if bench.keep_observation(adapter, doc, seen="real_use") is not None:
        bench.note_learned(adapter, model, block)


# ---------------------------------------------------------------------------
# The end of a print, from the watchdog's own readings
# ---------------------------------------------------------------------------


def _printing(state: Any) -> bool:
    value = getattr(getattr(state, "state", None), "value", getattr(state, "state", None))
    return str(value or "").lower() == "printing"


def near_end(job: Any) -> bool:
    """Whether *job* says it is in its last minutes: its time left, or,
    when the printer gives none, how complete it is."""
    if job is None:
        return False
    left = getattr(job, "print_time_left_seconds", None)
    if isinstance(left, (int, float)) and not isinstance(left, bool) and left > 0:
        return left <= END_WINDOW_S
    done = getattr(job, "completion", None)
    return isinstance(done, (int, float)) and not isinstance(done, bool) and END_COMPLETION <= done < 100.0


def on_progress(adapter: Any, state: Any, job: Any) -> None:
    """One reading the print watchdog took of a print Kiln started: in its
    last minutes, start logging its end, once per print.  Returns at once;
    the log runs on its own thread, reads the head and the printer's state
    only, and sends nothing.  Never raises."""
    try:
        if not enabled() or not _printing(state) or not near_end(job):
            return
        if not bench.logs_positions(adapter) or bench.print_footprint(adapter) is None:
            return                                  # nothing a log could be read against
        unit = bench.unit_of(adapter)
        if not unit:
            return
        which = f"{getattr(job, 'job_id', None) or ''}|{getattr(job, 'file_name', None) or ''}"
        with _LOCK:
            if _ENDS.get(unit) == which or unit in _RUNNING:
                return
            _ENDS[unit] = which
        threading.Thread(target=_follow_end, args=(adapter,), name="kiln-head-moves-end", daemon=True).start()
    except Exception:  # noqa: BLE001 -- a reading of a print never turns into a fault
        logger.debug("head moves: end not followed", exc_info=True)


def _follow_end(adapter: Any) -> None:
    watch = _begin(adapter, "end")
    if watch is not None:
        _finish(watch)


def _ended_reader(adapter: Any) -> Callable[[], bool]:
    """Whether the print is over, asked of the printer at most every
    :data:`END_STATE_EVERY_S`: the head settling between the last layer and
    the end sequence is not the end."""
    last = {"at": 0.0, "over": False}

    def over() -> bool:
        now = time.monotonic()
        if now - last["at"] >= END_STATE_EVERY_S:
            last["at"] = now
            try:
                from kiln.printers.engagement import internal_read

                with internal_read():
                    last["over"] = not _printing(adapter.get_state())
            except Exception:  # noqa: BLE001 -- an unread state is not an ended print
                last["over"] = False
        return bool(last["over"])

    return over


# ---------------------------------------------------------------------------
# After a printer's first print
# ---------------------------------------------------------------------------


def print_ended(printer_name: str) -> None:
    """The print-ended hook: once a printer's print is over and whatever it
    was teaching has been kept, decide whether its first lesson is offered
    for what is still missing (:func:`kiln.bench.offer_after_first_print`),
    on its own thread.  Returns at once; never raises."""
    try:
        if not enabled() or not printer_name:
            return
        from kiln.server import _get_registry

        adapter = _get_registry().get(printer_name)
        unit = bench.unit_of(adapter)
        if not unit or not bench.may_offer(unit, "first_print"):
            return
        with _LOCK:
            if unit in _DECIDING:
                return
            _DECIDING.add(unit)
        threading.Thread(target=_decide_after_print, args=(adapter, unit, printer_name),
                         name="kiln-first-print-offer", daemon=True).start()
    except Exception:  # noqa: BLE001 -- an ending never turns into a fault
        logger.debug("head moves: no decision after the print", exc_info=True)


def _decide_after_print(adapter: Any, unit: str, printer_name: str) -> None:
    try:
        deadline = time.monotonic() + END_MAX_S + 60.0
        while time.monotonic() < deadline:
            with _LOCK:
                busy = unit in _RUNNING
            if not busy:
                break
            time.sleep(AFTER_PRINT_WAIT_S)
        bench.offer_after_first_print(adapter, printer_name)
    finally:
        with _LOCK:
            _DECIDING.discard(unit)
