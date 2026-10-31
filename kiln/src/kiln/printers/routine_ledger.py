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
  ``~/.kiln/pending_cooldown.json`` with the pid that watches it; the
  thread clears it when the fan is off.  A process that died with the fan
  on (a kill no handler sees) leaves the marker, and a running server's
  background check (:func:`start_stranded_watch`) takes it over: the same
  watch, the same fan-off once the nozzle reads at or below the hand-off.
  A status read only REPORTS a marker (:func:`describe_stranded_cooldown`);
  it never sends the fan-off, so reading a printer changes nothing.  A fan
  command from the person ends Kiln's claim on the fan
  (:func:`fan_taken_over`).  Every step is a plain G-code line safe to send
  twice, which is what lets one process finish a routine another began.
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
        self.gone = threading.Event()  # set on release, so a waiting watch wakes at once

    def is_open(self) -> bool:
        with _lock:
            return self.id in _holds

    def release(self) -> None:
        """The routine reached its terminal state on its own; forget it."""
        with _lock:
            _holds.pop(self.id, None)
        self.gone.set()

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


def note_cooldown(key: str, *, fan_off: str, handoff_c: float, timeout_s: float = 150.0) -> None:
    """Record that *key*'s fan is on for a cool-down this process owes a fan-off to."""
    data = _read_marker()
    data[key] = {
        "fan_off": fan_off,
        "handoff_c": float(handoff_c),
        "timeout_s": float(timeout_s),
        "since": time.time(),
        "pid": os.getpid(),
    }
    _write_marker(data)


def clear_cooldown(key: str) -> None:
    data = _read_marker()
    if key in data:
        data.pop(key)
        _write_marker(data)


def _young_entry(key: str, *, max_age_s: float) -> dict[str, Any] | None:
    """The marker for *key* if it is on file and young enough.  Writes nothing."""
    entry = _read_marker().get(key)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("since") or 0) > max_age_s:
        return None
    return entry


def stranded_cooldown(key: str, *, max_age_s: float = MARKER_MAX_AGE_S) -> dict[str, Any] | None:
    """The marker for *key* if one is on file and young enough, else ``None``.

    An expired marker is cleared here; :func:`describe_stranded_cooldown`
    reads through :func:`_young_entry` instead, because a status read
    writes nothing.
    """
    entry = _young_entry(key, max_age_s=max_age_s)
    if entry is None and isinstance(_read_marker().get(key), dict):
        clear_cooldown(key)
    return entry


def _cooling_here(key: str) -> bool:
    """A cool-down hold for *key* is open in THIS process."""
    with _lock:
        return any(h.key == key and h.kind == "cooldown" for h in _holds.values())


def _owner_alive(entry: dict[str, Any]) -> bool:
    """The process that wrote *entry* is still running (and so still watching).

    A marker from before owners were recorded carries no pid, and counts as
    left behind.  Our own pid is answered by :func:`_cooling_here` instead.
    """
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        return _pid_running_windows(pid) if os.name == "nt" else _pid_running_posix(pid)
    except Exception:  # noqa: BLE001 -- a check that cannot answer leaves the cool-down alone
        return True


def _pid_running_posix(pid: int) -> bool:
    """Signal 0 delivers nothing on POSIX; it only asks whether *pid* exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # a process we may not signal is still a process
    return True


def _pid_running_windows(pid: int) -> bool:
    """Ask Windows whether *pid* is still running.

    Never ``os.kill`` here: on Windows signal 0 is CTRL_C_EVENT, so the
    "liveness check" would interrupt the process it asks about.  Honest
    bound, both platforms: a pid reused by another program reads as alive,
    which leaves the cool-down to its marker's age limit and sends nothing.
    """
    import ctypes
    from ctypes import wintypes

    query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied  # exists, but not ours to open
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


#: How often a cool-down watch that outlived its first wait looks again.
LATE_POLL_S: float = 30.0


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
    hold it registers is what a shutdown settles in its place.  A nozzle
    still above the hand-off after *timeout_s* (or a printer that stopped
    answering) keeps the fan on -- cooling is the point -- and the watch
    looks again every :data:`LATE_POLL_S` until the marker's age limit, so
    the fan-off still goes out without anyone asking for it.
    """
    key = printer_key(adapter)
    note_cooldown(key, fan_off=fan_off, handoff_c=handoff_c, timeout_s=timeout_s)

    def _settle() -> None:
        try:
            adapter.send_gcode([fan_off])
        finally:
            clear_cooldown(key)

    h = hold(f"part fan on for the cool-down to {handoff_c:g} °C", key, _settle, adapter=adapter, kind="cooldown")

    def _wait(timeout: float) -> tuple[bool, Any]:
        try:
            return adapter._wait_for_hotend_below(handoff_c, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- a lost read is not a reason to leave the fan on
            _logger.warning("cool-down watch on %s failed: %s", key, exc)
            return True, None

    def _watch() -> None:
        give_up_at = time.monotonic() + MARKER_MAX_AGE_S
        reached, reading = _wait(timeout_s)
        while not reached and time.monotonic() < give_up_at:
            if h.gone.wait(LATE_POLL_S):
                break
            reached, reading = _wait(0.0)
        if not h.is_open():
            return  # a shutdown or a fan command settled it first; nothing to send twice
        if reached:
            _logger.info("cool-down on %s: nozzle read %s, fan off", key, reading)
            h.settle()
        else:
            _logger.warning(
                "cool-down on %s: nozzle still read %s after %gs; fan left on, the shutdown settles it",
                key, reading, MARKER_MAX_AGE_S,
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


def fan_taken_over(adapter: Any) -> None:
    """The person set the part fan themselves: Kiln stops owning it.

    An open cool-down on this printer is released without its fan-off (its
    thread sees the hold gone and sends nothing), and a marker left by an
    earlier process is cleared, so no Kiln process later turns off a fan
    the person chose to run.
    """
    key = printer_key(adapter)
    with _lock:
        mine = [h for h in _holds.values() if h.key == key and h.kind == "cooldown"]
    for h in mine:
        h.release()
    clear_cooldown(key)


def describe_stranded_cooldown(adapter: Any, state: Any) -> dict[str, Any] | None:
    """What a status read says about a cool-down another process left behind.

    Reads only: nothing is sent to the printer and the marker is not
    touched.  The fan-off belongs to :func:`adopt_stranded_cooldown`, run by
    the server's own background check, never to a read.

    ``None`` when there is nothing on file for this printer, when the
    cool-down is still being watched (here, or by the live process that
    started it), or when the machine is printing or paused, whose fan is
    the print's.
    """
    key = printer_key(adapter)
    entry = _young_entry(key, max_age_s=MARKER_MAX_AGE_S)
    if entry is None or _cooling_here(key) or _owner_alive(entry):
        return None
    raw = getattr(state, "state", None)
    if getattr(raw, "value", raw) in ("printing", "paused"):
        return None
    handoff = float(entry.get("handoff_c") or 0)
    reading = getattr(state, "tool_temp_actual", None)
    shown = "unknown" if reading is None else f"{float(reading):g} °C"
    yourself = "To turn it off now: set_fan with percent 0 (or `kiln fan --percent 0`)."
    if reading is None or float(reading) > handoff:
        return {
            "status": "cooling",
            "handoff_c": handoff,
            "note": (
                f"A Kiln server stopped during a cool-down, so the part fan may still be on. It should "
                f"stay on until the nozzle reads at or below {handoff:g} °C (it reads {shown}). A running "
                f"Kiln server finishes it on its own within a minute. {yourself}"
            ),
        }
    return {
        "status": "fan_left_on",
        "handoff_c": handoff,
        "note": (
            f"A Kiln server stopped during a cool-down, so the part fan may still be on. The nozzle reads "
            f"{shown}, at or below the {handoff:g} °C hand-off, so the fan can go off. A running Kiln "
            f"server turns it off on its own within a minute. {yourself}"
        ),
    }


def adopt_stranded_cooldown(adapter: Any) -> str | None:
    """Take over a cool-down a stopped Kiln process left on *adapter*'s machine.

    ``"adopted"`` when this process now watches it (the fan-off goes out
    from the same thread a live cool-down uses, at once if the nozzle is
    already cool); ``"left_to_print"`` when a print has the fan now and the
    marker is cleared; ``None`` when there is nothing to take over.
    """
    key = printer_key(adapter)
    entry = stranded_cooldown(key)
    if entry is None or _cooling_here(key) or _owner_alive(entry):
        return None
    fan_off = str(entry.get("fan_off") or "")
    if not fan_off:
        clear_cooldown(key)
        return None
    if machine_is_printing(adapter):
        clear_cooldown(key)
        return "left_to_print"
    handoff = float(entry.get("handoff_c") or 0)
    _logger.info("cool-down on %s: taken over from a stopped Kiln process", key)
    start_cooldown(adapter, fan_off=fan_off, handoff_c=handoff, timeout_s=float(entry.get("timeout_s") or 150.0))
    return "adopted"


def stranded_keys() -> list[str]:
    """Printers with a young marker on file (cheap: one small file read)."""
    return [k for k in _read_marker() if _young_entry(k, max_age_s=MARKER_MAX_AGE_S) is not None]


#: How often a server checks for a cool-down a stopped process left behind.
STRANDED_CHECK_S: float = 30.0


def start_stranded_watch(
    get_adapters: Callable[[], list[Any]], *, interval_s: float | None = None
) -> threading.Event:
    """A server's background check for cool-downs a stopped Kiln left running.

    Every *interval_s* it reads the marker; only when a printer has one does
    it ask *get_adapters* for the machines it can reach (so a quiet install
    never connects to a printer for this) and adopts what it finds.  The
    returned event stops the loop.
    """
    stop = threading.Event()
    every = STRANDED_CHECK_S if interval_s is None else interval_s

    def _loop() -> None:
        while not stop.is_set():
            try:
                keys = set(stranded_keys())
                if keys:
                    for adapter in get_adapters():
                        if printer_key(adapter) in keys:
                            adopt_stranded_cooldown(adapter)
            except Exception as exc:  # noqa: BLE001 -- a backstop never takes the server down
                _logger.debug("stranded cool-down check failed: %s", exc)
            if stop.wait(every):
                break

    threading.Thread(target=_loop, name="kiln-stranded-cooldown", daemon=True).start()
    return stop
