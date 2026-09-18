"""What Kiln switched on, and how to switch it off, for as long as it is on.

A heater or fan routine Kiln starts has a terminal state -- heater off,
fan off -- that the machine must reach whether or not the caller is
still listening.  Measured 2026-09-18 on an A1: ``purge_filament`` heated,
extruded, switched the heater off and put the part fan on full for the
served cool-down; the client gave up on the request, the host restarted
the server (SIGTERM at 13:54:44) while the cool-down was waiting for the
hand-off temperature, and the fan-off never went out.  The tool's own
frame was gone, so nothing in the process remembered that the fan was
Kiln's to turn off.  This module is that memory, in three layers:

* **Holds** -- whoever turns something on registers how to turn it off,
  and releases the hold when it has.  :func:`drain` settles every open
  hold; the server's shutdown path calls it FIRST, before the printer
  connections are released, and ``atexit`` calls it again as a fallback.
  A hold is held across requests on purpose: a wipe walked one step at
  a time keeps the nozzle hot between calls.

* **Detached cool-downs** -- the served finish (fan on, wait for the
  hand-off temperature, fan off) runs in a thread that outlives the
  request, so the answer leaves as soon as the fan is on, the event loop
  is free to answer ``printer_status``, and the fan-off is sent by a
  thread nobody can time out.

* **The marker** -- a cool-down in flight is written to
  ``~/.kiln/pending_cooldown.json``; the thread clears it when the fan is
  off.  A server that died with the fan on (a kill no handler sees)
  leaves the marker, and the next ``printer_status`` on ANY server
  finishes the cool-down from it: once the nozzle reads at or below the
  hand-off, it sends the fan-off line the marker carries and says so.
  Every step is a plain G-code line safe to send twice, which is what
  lets a follow-up read complete a routine another process began.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_lock = threading.Lock()
_holds: dict[int, Hold] = {}
_ids = itertools.count(1)
_threads: list[threading.Thread] = []

#: A marker older than this is a different day's cool-down, not one to act on.
MARKER_MAX_AGE_S: float = 3600.0
_MARKER_NAME = "pending_cooldown.json"


def machine_is_printing(adapter: Any) -> bool:
    """Best effort: is a print in flight on *adapter* right now?

    A settle must never land on a running print -- a fan-off or heater-off
    in the middle of a job is the print's to send, not Kiln's.  A read
    that fails answers ``False``: the terminal state is still sent, which
    is the safe side for a machine Kiln cannot see.
    """
    try:
        state = adapter.get_state()
    except Exception:  # noqa: BLE001
        return False
    raw = getattr(state, "state", None)
    return getattr(raw, "value", raw) in ("printing", "paused")


class Hold:
    """One thing Kiln switched on, with the one call that switches it off."""

    def __init__(
        self,
        hold_id: int,
        label: str,
        key: str,
        settle: Callable[[], Any],
        *,
        adapter: Any = None,
        kind: str = "routine",
    ) -> None:
        self.id = hold_id
        self.label = label
        self.key = key
        self.kind = kind
        self.adapter = adapter
        self._settle = settle

    def is_open(self) -> bool:
        with _lock:
            return self.id in _holds

    def release(self) -> None:
        """The routine reached its terminal state on its own; forget it."""
        with _lock:
            _holds.pop(self.id, None)

    def settle(self) -> bool:
        """Bring the machine to the terminal state now.  Never raises.

        Nothing is sent to a machine that is printing or paused: the print
        owns its heater and fans from here, and the hold is simply dropped.
        """
        try:
            if self.adapter is not None and machine_is_printing(self.adapter):
                _logger.info("routine ledger: %s left to the running print", self.label)
                return False
            self._settle()
            return True
        except Exception as exc:  # noqa: BLE001 -- shutdown path; report, never abort
            _logger.warning("routine ledger: settling %r failed: %s", self.label, exc)
            return False
        finally:
            self.release()


def printer_key(adapter: Any) -> str:
    """A stable name for the machine an adapter drives, across processes."""
    host = (
        getattr(adapter, "_host", None)
        or getattr(adapter, "host", None)
        or getattr(adapter, "base_url", None)
        or ""
    )
    return f"{getattr(adapter, 'name', type(adapter).__name__)}@{host}"


def hold(
    label: str, key: str, settle: Callable[[], Any], *, adapter: Any = None, kind: str = "routine"
) -> Hold:
    """Register how to switch off what *label* says was just switched on.

    *adapter*, when given, is asked whether a print is running before the
    settle is sent.  *kind* names the class of hold (``"heater"``,
    ``"cooldown"``) for the marker's in-flight check.
    """
    h = Hold(next(_ids), label, key, settle, adapter=adapter, kind=kind)
    with _lock:
        _holds[h.id] = h
    return h


def open_holds() -> list[str]:
    """The labels of everything Kiln is currently holding on."""
    with _lock:
        return [h.label for h in _holds.values()]


def drain(reason: str = "shutdown", key: str | None = None) -> list[str]:
    """Settle every open hold (or those for one printer); the labels settled.

    Each hold settles inside its own try/except, so one refused command
    cannot skip the next.  Safe to call with nothing open, and safe to
    call twice.
    """
    with _lock:
        pending = [h for h in _holds.values() if key is None or h.key == key]
    settled: list[str] = []
    for h in pending:
        _logger.info("routine ledger (%s): settling %s", reason, h.label)
        if h.settle():
            settled.append(h.label)
    return settled


def drain_at_exit() -> None:
    drain("atexit")


# ---------------------------------------------------------------------------
# Detached cool-down
# ---------------------------------------------------------------------------


def _kiln_dir() -> Path:
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path() -> Path:
    return _kiln_dir() / _MARKER_NAME


def _read_marker() -> dict[str, Any]:
    try:
        raw = json.loads(_marker_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a missing or corrupt marker is an empty one
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_marker(data: dict[str, Any]) -> None:
    try:
        path = _marker_path()
        if not data:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 -- the marker is a backstop, never the routine
        _logger.debug("routine ledger: marker write failed: %s", exc)


def note_cooldown(key: str, *, fan_off: str, handoff_c: float) -> None:
    """Record that *key*'s fan is on for a cool-down Kiln owes a fan-off to."""
    data = _read_marker()
    data[key] = {"fan_off": fan_off, "handoff_c": float(handoff_c), "since": time.time()}
    _write_marker(data)


def clear_cooldown(key: str) -> None:
    data = _read_marker()
    if key in data:
        data.pop(key)
        _write_marker(data)


def stranded_cooldown(key: str, *, max_age_s: float = MARKER_MAX_AGE_S) -> dict[str, Any] | None:
    """The marker for *key* if one is on file and young enough, else ``None``."""
    entry = _read_marker().get(key)
    if not isinstance(entry, dict):
        return None
    since = float(entry.get("since") or 0)
    if time.time() - since > max_age_s:
        clear_cooldown(key)
        return None
    return entry


def start_cooldown(
    adapter: Any,
    *,
    fan_off: str,
    handoff_c: float,
    timeout_s: float,
) -> Hold:
    """The fan is on; watch the nozzle from a thread and send *fan_off* when
    it reads at or below *handoff_c*.

    The thread is a daemon: it must never keep a process alive, and the
    hold it registers is what a shutdown settles in its place.  A watch
    that runs out (the nozzle still above the hand-off after *timeout_s*)
    leaves the fan on -- cooling is the point -- and leaves the hold and
    the marker for the next ``printer_status`` to finish.
    """
    key = printer_key(adapter)
    note_cooldown(key, fan_off=fan_off, handoff_c=handoff_c)

    def _settle() -> None:
        try:
            adapter.send_gcode([fan_off])
        finally:
            clear_cooldown(key)

    h = hold(f"part fan on for the cool-down to {handoff_c:g} °C", key, _settle, adapter=adapter, kind="cooldown")

    def _watch() -> None:
        try:
            reached, reading = adapter._wait_for_hotend_below(handoff_c, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 -- a lost read is not a reason to leave the fan on
            _logger.warning("cool-down watch on %s failed: %s", key, exc)
            reached, reading = True, None
        if not h.is_open():
            return  # a shutdown or a status read settled it first; nothing to send twice
        if reached:
            _logger.info("cool-down on %s: nozzle read %s, fan off", key, reading)
            h.settle()
        else:
            _logger.warning(
                "cool-down on %s: nozzle still read %s after %gs; fan left on, printer_status finishes it",
                key, reading, timeout_s,
            )

    t = threading.Thread(target=_watch, name=f"kiln-cooldown-{h.id}", daemon=True)
    with _lock:
        _threads[:] = [x for x in _threads if x.is_alive()]  # finished watches do not pile up
        _threads.append(t)
    t.start()
    return h


def wait_settled(timeout: float = 5.0) -> bool:
    """Join every cool-down thread (tests); ``True`` when none is left running."""
    deadline = time.monotonic() + timeout
    with _lock:
        threads = list(_threads)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        return not _threads


def complete_stranded_cooldown(adapter: Any, state: Any) -> dict[str, Any] | None:
    """Finish a cool-down another process left behind, from a status read.

    ``None`` when there is nothing on file for this printer, or when the
    cool-down is still running in THIS process (its thread will finish
    it).  Otherwise a block for the answer: the fan-off sent once the
    nozzle reads at or below the hand-off; a note that it is still
    cooling while above it; and nothing sent to a machine that is
    printing or paused, whose fan is the print's -- the marker is cleared
    there, because the print's own end sequence takes the fan from here.
    """
    key = printer_key(adapter)
    entry = stranded_cooldown(key)
    if entry is None:
        return None
    with _lock:
        in_flight = any(h.key == key and h.kind == "cooldown" for h in _holds.values())
    if in_flight:
        return None
    raw = getattr(state, "state", None)
    status = getattr(raw, "value", raw)
    if status in ("printing", "paused"):
        clear_cooldown(key)
        return None
    handoff = float(entry.get("handoff_c") or 0)
    fan_off = str(entry.get("fan_off") or "")
    reading = getattr(state, "tool_temp_actual", None)
    if not fan_off:
        clear_cooldown(key)
        return None
    if reading is None or float(reading) > handoff:
        shown = "unknown" if reading is None else f"{float(reading):g} °C"
        return {
            "status": "cooling",
            "handoff_c": handoff,
            "note": (
                f"A cool-down Kiln started is still running: the part fan stays on until the nozzle "
                f"reads at or below {handoff:g} °C (it reads {shown}); the next printer_status turns it off."
            ),
        }
    try:
        adapter.send_gcode([fan_off])
        sent = True
    except Exception as exc:  # noqa: BLE001 -- say it, do not hide it
        _logger.warning("stranded cool-down on %s: fan-off refused: %s", key, exc)
        sent = False
    if sent:
        clear_cooldown(key)
    return {
        "status": "finished" if sent else "fan_off_refused",
        "handoff_c": handoff,
        "note": (
            f"Finished a cool-down an earlier Kiln server left running: the nozzle reads "
            f"{float(reading):g} °C, at or below the {handoff:g} °C hand-off, so the part fan was turned off."
            if sent else
            "A cool-down an earlier Kiln server left running could not be finished: the fan-off command "
            "was refused. Send set_fan(percent=0)."
        ),
    }
