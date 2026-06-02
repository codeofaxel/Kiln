"""Auto-record print outcomes on terminal state transitions (Bug #10).

Kiln's learning loop (``record_print_outcome``, ``proven_settings``
updates, regression alerts) historically fired only when an agent
remembered to call ``record_print_outcome`` manually after a print
ended.  In practice, agents forget — which left the learning DB with
sparse, biased data (success recorded more than failure because agents
notice wins and move on quickly, while failures often left a half-
processed pipeline that never called the tool).

This module closes the loop by hooking the printer state-update path
so transitions to a terminal state auto-fire ``record_print_outcome``
with inferred outcome semantics.  The hook is debounced against
firmware state flickers and idempotent per job: repeat transitions to
the same terminal state for the same job_id no-op after the first.

The hook is printer-adapter agnostic.  Bambu's MQTT push_status path
calls :func:`fire_terminal_state_hook` from its state-update routine;
other adapters can do the same from their respective state-change
callbacks.  :func:`cancel_print` in the public server also fires the
hook synchronously so a user cancel turns into a recorded outcome
immediately.

Outcome inference
-----------------
The ``gcode_state`` / ``print_error`` pair maps to outcomes as:

==========================  ==========  =====================
gcode_state (previous)      new         outcome
==========================  ==========  =====================
printing / paused / busy    finish      success
printing / paused / busy    failed      failed
printing / paused / busy    idle(*)     cancelled (*see note)
anything                    anything    (no record, not a
                                         terminal-after-active
                                         transition)
==========================  ==========  =====================

(*) Bambu firmware has no dedicated "cancelled" state — a successful
cancel lands in ``idle``.  We can only tell it was a cancel vs a
natural finish if the cancel command was issued by our side within
the last few seconds.  :func:`register_cancel_intent` records the
intent so the next idle transition for that printer is treated as a
cancel rather than a success.

Upsert semantics
----------------
``record_print_outcome`` is extended with ``auto_recorded: bool =
False``.  When True, the row is inserted with the ``auto_recorded``
flag set, but a second call with the same ``job_id`` and
``auto_recorded=False`` (agent-curated) UPDATES the existing row
rather than inserting a duplicate.  Agents can always refine an
auto-recorded outcome with quality_grade, notes, or a corrected
outcome — the auto-record is a best-effort scaffold, not a lock.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_logger = logging.getLogger(__name__)

# Debounce window for firmware state flicker.  Bambu can briefly
# bounce through idle during pause/resume transitions — we don't
# want to auto-record a "finish" on a transient idle.  Observed
# flicker windows are <1 second; 2.5s gives us a 2x safety margin.
_TERMINAL_STATE_DEBOUNCE_S: float = 2.5

# Cancel intent TTL — after register_cancel_intent() is called, the
# next idle transition within this window is classified as a cancel
# rather than a success.  5 seconds is longer than the typical
# print→idle-after-cancel latency (~1-2s) but short enough that a
# cancel intent can't bleed into an unrelated subsequent print.
_CANCEL_INTENT_TTL_S: float = 5.0

# Which gcode_state values count as "actively printing" (so a
# transition OUT of one into a terminal state is the trigger).  Case-
# insensitive — upper A1 sometimes emits "RUNNING", lowercase X1 sends
# "running".  Callers should ``.lower()`` before passing in.
_ACTIVE_STATES: frozenset[str] = frozenset({
    "running",
    "pause",
    "paused",  # Bambu A1 occasionally uses this form
    "prepare",
    "slicing",
    "init",
    "busy",
})

# gcode_state that means "the print finished cleanly".
_FINISH_STATES: frozenset[str] = frozenset({"finish"})

# gcode_state that means "the print failed with an error".
_FAILED_STATES: frozenset[str] = frozenset({"failed"})

# Neutral idle — could mean finished naturally, cancelled, or startup.
# The cancel-intent table disambiguates.
_IDLE_STATES: frozenset[str] = frozenset({"idle"})


class _HookState:
    """Thread-safe state the hook needs: debouncing, cancel intents,
    and the "did we already record this job_id" idempotency ledger.
    Single process-wide instance; callers don't touch this directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # printer_name -> {"prev_state": str, "last_transition_ts": float}
        self._prev_by_printer: dict[str, dict[str, Any]] = {}
        # printer_name -> timestamp of most recent register_cancel_intent
        self._cancel_intents: dict[str, float] = {}
        # (printer_name, job_id) -> True once we've auto-recorded this job
        # Prevents double-recording if the hook fires multiple times
        # for the same terminal transition (e.g. MQTT retry).
        self._recorded: set[tuple[str, str]] = set()

    def previous_state(self, printer: str) -> str | None:
        with self._lock:
            entry = self._prev_by_printer.get(printer)
            if entry is None:
                return None
            return entry.get("prev_state")

    def set_previous_state(self, printer: str, state: str) -> None:
        with self._lock:
            self._prev_by_printer[printer] = {
                "prev_state": state,
                "last_transition_ts": time.monotonic(),
            }

    def register_cancel_intent(self, printer: str) -> None:
        with self._lock:
            self._cancel_intents[printer] = time.monotonic()

    def consume_cancel_intent(self, printer: str) -> bool:
        """Return True and clear the intent if a cancel was registered
        within the TTL; else False.  Single-consumer — first terminal
        transition after a cancel claims the intent."""
        with self._lock:
            ts = self._cancel_intents.pop(printer, None)
            if ts is None:
                return False
            return time.monotonic() - ts <= _CANCEL_INTENT_TTL_S

    def mark_recorded(self, printer: str, job_id: str) -> bool:
        """Return True if this is the first recording for (printer,
        job_id); False if we've already recorded this terminal event.
        Call SITE must skip the record when False is returned."""
        with self._lock:
            key = (printer, job_id)
            if key in self._recorded:
                return False
            self._recorded.add(key)
            return True


_HOOK_STATE = _HookState()


def register_cancel_intent(printer_name: str) -> None:
    """Call from :func:`cancel_print` before issuing the cancel command.

    The next idle transition for ``printer_name`` within
    ``_CANCEL_INTENT_TTL_S`` is classified as a cancelled outcome
    rather than a success.  Without this, kiln can't distinguish a
    clean finish from a cancel because Bambu firmware doesn't expose
    a "cancelled" gcode_state.
    """
    _HOOK_STATE.register_cancel_intent(printer_name)


def _infer_outcome(
    new_state: str,
    print_error_code: int,
    printer_name: str,
) -> tuple[str, str | None] | None:
    """Map a Bambu gcode_state + print_error_code to (outcome,
    failure_mode).  Returns ``None`` when the new state isn't a
    terminal transition we should record.

    :param new_state: Lower-cased ``gcode_state`` after transition.
    :param print_error_code: Firmware-reported HMS code (0 = none).
    :param printer_name: For cancel-intent lookup.
    :returns: ``("success", None)``, ``("failed", <mode>)``,
        ``("cancelled", None)``, or ``None`` if not a terminal
        transition.
    """
    state = new_state.lower().strip()
    if state in _FAILED_STATES:
        # Map print_error_code to a canonical failure_mode when we
        # recognise the HMS family.  Unrecognised codes fall back to
        # the generic "other" — the operator can refine via a manual
        # record_print_outcome call with a specific failure_mode.
        # Common Bambu HMS codes:
        #   0C00_0100_0001_* = adhesion / first-layer
        #   0500_C010_*      = SD card R/W
        #   0700_0200_*      = filament runout
        #   0500_0400_*      = extrusion / clog
        code = int(print_error_code or 0)
        if code == 0:
            return ("failed", "other")
        # Cheap fingerprint — higher bits identify the HMS family.
        family = (code >> 24) & 0xFF
        if family == 0x07:
            return ("failed", "filament_runout")
        if family == 0x0C:
            return ("failed", "adhesion")
        if family == 0x05:
            return ("failed", "mechanical")
        # Specific extruder / servo-overload code (HMS 0300-0900-...): a
        # P2S-class closed-loop servo extruder faults here where a stepper
        # would silently skip.  Narrow attr-prefix match (not the whole 0300
        # family) keeps the blast radius minimal and mirrors the adapter's
        # _classify_flow_anomaly attr-prefix convention.
        if f"{code:08X}".startswith("03000900"):
            return ("failed", "mechanical")
        return ("failed", "other")
    if state in _FINISH_STATES:
        return ("success", None)
    if state in _IDLE_STATES:
        # Ambiguous — check cancel intent.  If someone called
        # register_cancel_intent recently, treat as cancelled.  Else
        # assume natural completion (Bambu reports "idle" after finish
        # too, though most firmware versions route through "finish"
        # first; older firmware jumps straight to idle).
        if _HOOK_STATE.consume_cancel_intent(printer_name):
            return ("cancelled", None)
        return ("success", None)
    return None


def fire_terminal_state_hook(
    *,
    prev_state: str | None,
    new_state: str,
    print_error_code: int,
    printer_name: str,
    job_id: str,
    file_name: str | None = None,
    material_type: str | None = None,
) -> dict[str, Any] | None:
    """Fire the auto-record hook if ``(prev_state, new_state)`` is a
    terminal transition from an actively-printing state.

    Idempotent per (printer_name, job_id): the first call for a given
    pair records; subsequent calls return ``None`` (already recorded).

    :returns: The structured record payload on first call, or None
        when the transition isn't a terminal-after-active edge or when
        this (printer, job_id) was already auto-recorded.
    """
    if not prev_state or not job_id:
        return None

    prev_lc = prev_state.lower().strip()
    new_lc = new_state.lower().strip()

    # Only record transitions FROM active states TO terminal states.
    # Idle→idle, idle→busy, busy→idle (on boot) all no-op.
    if prev_lc not in _ACTIVE_STATES:
        return None

    outcome_info = _infer_outcome(new_lc, print_error_code, printer_name)
    if outcome_info is None:
        return None

    # Idempotency gate — prevents double-recording when MQTT replays a
    # terminal transition message.
    if not _HOOK_STATE.mark_recorded(printer_name, job_id):
        return None

    outcome, failure_mode = outcome_info

    # Lazy import to avoid circulars at module load.  learning_tools
    # depends on server which depends on many adapters, one of which
    # (bambu) imports THIS module at its top.
    try:
        from kiln.plugins.learning_tools import record_print_outcome
    except Exception as exc:  # pragma: no cover
        _logger.warning(
            "auto_record_hook: record_print_outcome unavailable: %s", exc,
        )
        return None

    kwargs: dict[str, Any] = {
        "job_id": job_id,
        "outcome": outcome,
        "printer_name": printer_name,
        "auto_recorded": True,
        "notes": (
            f"auto-recorded on terminal state transition "
            f"({prev_lc!r} → {new_lc!r}, print_error={print_error_code})"
        ),
    }
    if file_name is not None:
        kwargs["file_name"] = file_name
    if material_type is not None:
        kwargs["material_type"] = material_type
    if failure_mode is not None:
        kwargs["failure_mode"] = failure_mode

    try:
        result = record_print_outcome(**kwargs)
    except Exception as exc:  # pragma: no cover
        _logger.warning(
            "auto_record_hook: record_print_outcome raised: %s", exc,
        )
        return None

    _logger.info(
        "auto_record_hook: recorded outcome=%r for job_id=%r "
        "printer=%r (prev=%r→new=%r, hms=%s, failure_mode=%r)",
        outcome, job_id, printer_name, prev_lc, new_lc,
        print_error_code, failure_mode,
    )
    return result if isinstance(result, dict) else {"raw": result}


def observe_state(printer_name: str, current_state: str) -> str | None:
    """Track the previous state for ``printer_name`` and return it.

    Call from the adapter's state-update path BEFORE mutating its
    cached state.  The returned value is the caller's previous-state
    argument to :func:`fire_terminal_state_hook`.
    """
    prev = _HOOK_STATE.previous_state(printer_name)
    _HOOK_STATE.set_previous_state(printer_name, current_state)
    return prev


__all__ = [
    "fire_terminal_state_hook",
    "observe_state",
    "register_cancel_intent",
]
