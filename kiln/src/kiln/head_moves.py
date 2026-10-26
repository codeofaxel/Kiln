"""What a printer does with its head when Kiln pauses or cancels a print.

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
the one resume template calls :func:`around` itself.

Never in the way of the command: the log runs beside it on its own thread,
the command goes out at once, its result comes back untouched, and nothing
here raises.  Off on Kiln's servers, under a test run or a sandbox unless a
test asks for it by name, and switched off with ``KILN_HEAD_MOVE_WATCH=0``.

What is written is :func:`kiln.bench.logged_block`'s to decide.  A pause is
written only with its way back, so Kiln must also send the resume; a cancel
only once the head has settled; neither when the log cannot tell the print
from the block or the print was not one Kiln started.  An observation from
a guided session is never replaced by one from real use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
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

#: The blocks this module watches, and what each is in the observation.
BLOCKS = ("pause", "cancel", "resume")

_LOCK = threading.Lock()
#: One block logged at a time per machine (by unit): the log in progress.
_RUNNING: dict[str, Any] = {}
#: A pause whose way back has not been seen yet, by unit: its log, the
#: footprint of the print it paused and the layer it began at.
_PAUSED: dict[str, dict[str, Any]] = {}
#: Set while a watched command is being sent, so an adapter that hands the
#: command on to another adapter (Creality's backend) is not logged twice.
_HERE = threading.local()

__all__ = ["BLOCKS", "SWITCH_ENV", "TEST_OPT_IN_ENV", "around", "enabled"]


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
        max_s = BLOCK_MAX_S
        if block == "resume":
            with _LOCK:
                paused = _PAUSED.get(unit)
            if paused is None:
                return None
            layer = float(paused["layer"])
            max_s = BACK_MAX_S

            def stop_when(log: Any) -> bool:
                return bench.way_back(log.points, log.extruder, layer) is not None

        log = bench.PositionLog(adapter, settle_s=SETTLE_S, hz=HZ, max_s=max_s, quiet_s=QUIET_S, stop_when=stop_when)
        with _LOCK:
            if unit in _RUNNING:
                return None
            _RUNNING[unit] = log
        log.start()
        return {"adapter": adapter, "block": block, "unit": unit, "log": log, "max_s": max_s}
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
    with _LOCK:
        if _RUNNING.get(watch["unit"]) is log:
            _RUNNING.pop(watch["unit"], None)
    try:
        _keep(watch)
    except Exception:  # noqa: BLE001
        logger.debug("head moves: nothing kept", exc_info=True)


def _keep(watch: dict[str, Any]) -> None:
    adapter, block, unit, log = watch["adapter"], watch["block"], watch["unit"], watch["log"]
    if block == "pause":
        footprint = bench.print_footprint(adapter)
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
    else:
        found = bench.logged_block([log], bench.print_footprint(adapter), returns=False)
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
    bench.keep_observation(adapter, doc, seen="real_use")
