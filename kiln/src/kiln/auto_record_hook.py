"""Print-outcome lifecycle: open at start, record at end, reconcile on reconnect.

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

Watching the ending is not enough, though — it only records prints a
LIVE process saw finish.  Start a print, close the session, walk away:
nothing was watching when it ended, so nothing was recorded, and long
prints (where the data matters most) were systematically invisible.
Three additions close that hole:

1. :func:`open_pending_outcome` — called from
   ``PrinterAdapter.start_print`` (the chokepoint every entry point and
   adapter passes through), it writes the outcome row at print START
   with ``outcome='pending'``.  Kiln always sees the start, because
   Kiln initiates it; the record no longer depends on anyone watching
   the end.
2. :func:`fire_terminal_state_hook` (existing) resolves the pending
   row when a live process DOES watch the ending
   (``determined_by='observed'``).
3. :func:`reconcile_pending_outcomes` — called once per adapter
   connection, it settles pending rows with what the machine can
   honestly report: a matching terminal state resolves to
   success/failed (``determined_by='inferred'``); a printer that is
   merely idle with the job gone resolves to ``unknown`` — NEVER to
   success.  An unresolved print is a known unknown and known unknowns
   are safe; a guessed success is a silent lie that trains the model.
   The unknown rows are surfaced to the user next session, who is
   holding the part and can settle what the machine could not
   (``determined_by='user_reported'``).

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
previous state              new         outcome
==========================  ==========  =====================
printing / paused / busy    finish      success
printing / paused / busy    completed   success
printing / paused / busy    failed      failed
printing / paused / busy    cancelled   cancelled
printing / paused / busy    idle(*)     success, or cancelled
                                         with a live intent (*)
anything                    anything    (no record, not a
                                         terminal-after-active
                                         transition)
==========================  ==========  =====================

Two vocabularies arrive here.  Bambu's MQTT push path feeds its raw
firmware ``gcode_state``; every adapter's polled path feeds
``JobResult`` (``completed`` / ``cancelled`` / ``failed``) when the
printer named an ending, and the ``PrinterStatus`` value otherwise.

(*) The bare ``idle`` row is the residual guess, and it is only
reached now by firmware that names no ending at all — Bambu versions
that jump straight to ``idle`` after a print, rather than through
``finish``.  There, a cancel is distinguishable from a finish only if
our own side registered the intent within the last few seconds
(:func:`register_cancel_intent`, called from the ``cancel_print``
tool).  A cancel from the touchscreen or a rival client on such
firmware still lands as ``success``.  Adapters that CAN name the
ending no longer take this path: they report ``cancelled`` outright,
which needs no intent and beats one.

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

# Backstop for the DURABLE half of the same intent (see _HookState).
#
# The in-memory TTL above is a proxy for one thing: don't let an intent
# label a LATER print's ending.  A window is a poor proxy, because the
# quantity it is guessing at — how long a printer takes to stop moving,
# retract, park and report idle — is not knowable here and is certainly
# not the ~1-2s the note above assumes for every backend.  Guess short
# and the intent expires before the cancel lands, which records the
# cancelled print as a success; that is the failure this exists to stop.
#
# So the real guard is not time: ``clear_cancel_intent`` is called when a
# NEW print starts on the printer, which is the event the window was
# standing in for, and it is exact.  This bound only limits how long a
# never-observed intent can sit when no next print ever comes.
#
# Two minutes buys generous headroom over any real stop sequence — abort,
# retract, park, report idle is seconds to tens of seconds — while keeping
# the one case this costs narrow: a cancel that silently failed, on a print
# that then finished naturally inside the window, is recorded as cancelled
# when it did finish.  That needs the user to have cancelled a print which
# was already about to complete, and it errs toward "did not complete"
# rather than toward claiming a success nobody witnessed.
_CANCEL_INTENT_MAX_AGE_S: float = 120.0

#: ``settings`` key holding one printer's durable cancel intent.
_CANCEL_INTENT_KEY = "cancel_intent:{}"

# Which state values count as "actively printing" (so a transition OUT
# of one into a terminal state is the trigger).  Case-insensitive —
# upper A1 sometimes emits "RUNNING", lowercase X1 sends "running".
# Callers should ``.lower()`` before passing in.  Two vocabularies feed
# this set: Bambu's firmware gcode_state values AND the normalized
# ``PrinterStatus`` values every adapter's ``get_state()`` reports
# ("printing" / "cancelling") — both must read as active or the
# adapter-generic wiring would resolve rows for a print still running.
_ACTIVE_STATES: frozenset[str] = frozenset({
    "running",
    "pause",
    "paused",  # Bambu A1 occasionally uses this form
    "prepare",
    "slicing",
    "init",
    "busy",
    "printing",    # normalized PrinterStatus vocabulary
    "cancelling",  # a cancel in flight is still an active job
})

# State that means "the print finished cleanly": Bambu's firmware
# "finish" plus ``JobResult.COMPLETED``, the adapter-generic report.
_FINISH_STATES: frozenset[str] = frozenset({"finish", "completed"})

# State that means "the print failed": Bambu's firmware "failed", the
# normalized PrinterStatus "error", and ``JobResult.FAILED`` (which
# spells itself "failed" and so is already covered).
_FAILED_STATES: frozenset[str] = frozenset({"failed", "error"})

# ``JobResult.CANCELLED`` — a print that ENDED without completing, said
# by the machine rather than inferred from our own cancel intent.  This
# is the token Moonraker's "cancelled", Prusa Link's "STOPPED" and
# Bambu's post-cancel cache now arrive as.  Distinct from the ACTIVE
# "cancelling", which is a cancel still in flight.
_CANCELLED_STATES: frozenset[str] = frozenset({"cancelled"})

# Neutral idle — could mean finished naturally, cancelled, or startup.
# The cancel-intent table disambiguates.  Reaching this set is now the
# fallback for firmware that names no ending, not the common path: an
# adapter that can tell a finish from a cancel reports it above.
_IDLE_STATES: frozenset[str] = frozenset({"idle"})


def _write_durable_cancel_intent(printer: str, at: float) -> None:
    """Record the intent where another PROCESS can find it.  Never raises."""
    try:
        from kiln.persistence import get_db

        get_db().set_setting(_CANCEL_INTENT_KEY.format(printer), repr(float(at)))
    except Exception as exc:  # noqa: BLE001 — telemetry never blocks a cancel
        _logger.debug("durable cancel intent write failed: %s", exc)


def _consume_durable_cancel_intent(printer: str) -> bool:
    """Read-and-clear the durable intent.  ``True`` only if one was live.

    Stamped with the wall clock, because a monotonic one means nothing to
    the process that reads it.  That makes the value vulnerable to a clock
    change in a way the in-memory half is not, so a stamp in the FUTURE is
    treated as no intent rather than as an eternally valid one.

    Never raises: a database that will not answer must cost a print its
    label, never its recording.
    """
    key = _CANCEL_INTENT_KEY.format(printer)
    try:
        from kiln.persistence import get_db

        db = get_db()
        raw = db.get_setting(key)
        if not raw:
            return False
        # Cleared by overwrite — one row per printer, reused, never grows.
        db.set_setting(key, "")
        age = time.time() - float(raw)
        return 0 <= age <= _CANCEL_INTENT_MAX_AGE_S
    except Exception as exc:  # noqa: BLE001
        _logger.debug("durable cancel intent read failed: %s", exc)
        return False


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
        _write_durable_cancel_intent(printer, time.time())

    def consume_cancel_intent(self, printer: str) -> bool:
        """Return True and clear the intent if a cancel was registered
        recently; else False.  Single-consumer — the first terminal
        transition after a cancel claims the intent.

        Checks memory first and the durable record second, and clears BOTH
        either way.  Two backings, one intent: the process that asks for a
        cancel is very often not the process that sees the ending — ``kiln
        cancel`` sends the command and exits, leaving the running server to
        watch the printer stop — and this state is per-process and stamped
        with a monotonic clock, which is not even comparable across
        processes.  Without the durable half the intent from that door
        could never be found, whoever called it.
        """
        with self._lock:
            ts = self._cancel_intents.pop(printer, None)
        in_memory = ts is not None and time.monotonic() - ts <= _CANCEL_INTENT_TTL_S
        durable = _consume_durable_cancel_intent(printer)
        return in_memory or durable

    def clear_cancel_intent(self, printer: str) -> None:
        """Drop any intent without consuming it as an answer."""
        with self._lock:
            self._cancel_intents.pop(printer, None)
        _consume_durable_cancel_intent(printer)

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
    """Record that the next ending on ``printer_name`` was ASKED for.

    Prefer :func:`note_cancel_requested`, which takes the adapter and so
    cannot file the intent under a name the reader will not look up.  This
    remains for callers holding only a name.

    The next idle transition for that printer is then classified as a
    cancelled outcome rather than a success.  Without it Kiln cannot tell a
    clean finish from a cancel, because Bambu firmware exposes no
    "cancelled" gcode_state — an idle printer looks the same either way.
    """
    _HOOK_STATE.register_cancel_intent(printer_name)


def note_cancel_requested(adapter: Any) -> None:
    """THE call for any path that deliberately ends a running print.

    One helper, every door, because the two halves of this mechanism have
    to agree on a name and they are written in different places.  The
    intent is filed under :func:`~kiln.printers.base.outcome_printer_name`
    — the same string the lifecycle will look it up under when the ending
    arrives.  Passing a name instead is how the original call site went
    wrong: it guessed one from the registry and the reader used the backend
    family, so the flag was written where nothing would ever read it and a
    cancelled print was recorded as a success on every install.

    Every deliberate stop belongs here, not just the one tool: a cancel
    from the CLI, a stop the health monitor orders because it SAW the print
    fail, a watchdog's emergency stop.  Those recorded successes too, and
    the auto-cancel ones are the worst of them — the loop was being taught
    that the prints Kiln itself judged to be failing were the good ones.

    Never raises.  A print must always be stoppable, whatever the ledger
    thinks; an unlabelled cancel is a small loss, a cancel that did not
    happen is a ruined plate or worse.
    """
    try:
        from kiln.printers.base import outcome_printer_name

        _HOOK_STATE.register_cancel_intent(outcome_printer_name(adapter))
    except Exception as exc:  # noqa: BLE001
        _logger.debug("note_cancel_requested failed (non-fatal): %s", exc)


def clear_cancel_intent(printer_name: str) -> None:
    """Forget any pending intent — a NEW print is starting on this printer.

    This is what lets the intent outlive a slow stop sequence safely.  The
    thing a time window was guarding against is precisely this event, and
    here it is exactly rather than estimated: whatever the last cancel did
    or failed to do, it has nothing to say about the print starting now.

    Never raises.
    """
    try:
        _HOOK_STATE.clear_cancel_intent(printer_name)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("clear_cancel_intent failed (non-fatal): %s", exc)


def _failure_mode_from_code(print_error_code: int) -> str:
    """Map a firmware HMS error code to a canonical failure_mode.

    Unrecognised codes fall back to the generic "other" — the operator
    can refine via a manual record_print_outcome call with a specific
    failure_mode.  Common Bambu HMS codes:
      0C00_0100_0001_* = adhesion / first-layer
      0500_C010_*      = SD card R/W
      0700_0200_*      = filament runout
      0500_0400_*      = extrusion / clog
    """
    code = int(print_error_code or 0)
    if code == 0:
        return "other"
    # Cheap fingerprint — higher bits identify the HMS family.
    family = (code >> 24) & 0xFF
    if family == 0x07:
        return "filament_runout"
    if family == 0x0C:
        return "adhesion"
    if family == 0x05:
        return "mechanical"
    # Specific extruder / servo-overload code (HMS 0300-0900-...): a
    # P2S-class closed-loop servo extruder faults here where a stepper
    # would silently skip.  Narrow attr-prefix match (not the whole 0300
    # family) keeps the blast radius minimal and mirrors the adapter's
    # _classify_flow_anomaly attr-prefix convention.
    if f"{code:08X}".startswith("03000900"):
        return "mechanical"
    return "other"


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
    # An intent refers to the job that is ending NOW, so every terminal
    # transition SPENDS it — including the ones that do not need it to
    # classify.  Left unspent, an intent could linger and flip the next
    # job's honest ending into a "cancelled".
    if state in _FAILED_STATES:
        # A deliberate stop outranks the firmware's error word.  Aborting a
        # print can ITSELF trip a fault — measured on an A1 (2026-08-13):
        # a cancel during bed levelling aborts the homing move and the
        # firmware reports ``failed`` with a real Z-homing code.  Without
        # this check the ending we know the MOST about (it was asked for)
        # was recorded as a machine failure, with a failure_mode fabricated
        # from the code the abort produced — faults that never happened on
        # their own, filed into the failure statistics.  "Cancelled" also
        # keeps the row out of success-rate math entirely, so it errs
        # conservative in both directions; a monitor that diagnosed a REAL
        # failure before stopping the print still refines the row through
        # record_print_outcome, the documented path for outranking the hook.
        if _HOOK_STATE.consume_cancel_intent(printer_name):
            return ("cancelled", None)
        return ("failed", _failure_mode_from_code(print_error_code))
    if state in _FINISH_STATES:
        _HOOK_STATE.consume_cancel_intent(printer_name)
        return ("success", None)
    if state in _CANCELLED_STATES:
        # The machine said the job ended without completing.  This needs
        # no cancel intent and beats one: it is true for a cancel from
        # the touchscreen, from a rival client, or from any Kiln path
        # that isn't the one tool wired to register an intent.
        _HOOK_STATE.consume_cancel_intent(printer_name)
        return ("cancelled", None)
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


def is_terminal_transition(prev_state: str | None, new_state: str | None) -> bool:
    """Cheap predicate: could this edge produce an outcome record?

    Callers that must pay for job identity (a ``get_job()`` network round
    trip) before firing use this to skip the cost on benign transitions.
    """
    if not prev_state or not new_state:
        return False
    prev = prev_state.lower().strip()
    new = new_state.lower().strip()
    return prev in _ACTIVE_STATES and (
        new in _FINISH_STATES
        or new in _FAILED_STATES
        or new in _CANCELLED_STATES
        or new in _IDLE_STATES
    )


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

    # A job that was CANCELLING and then settled did not finish — it was
    # cancelled, whether or not our side registered the intent (the
    # touchscreen or another client may have issued it).
    if outcome == "success" and prev_lc == "cancelling":
        outcome = "cancelled"

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
        # A live process watched this terminal transition happen — the
        # strongest of the three outcome sources.
        "determined_by": "observed",
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

    Watching a printer ENTER an active state also retires any pending cancel
    intent, because a new print has begun and the last cancel cannot be about
    it.  ``start_print`` says the same thing earlier for prints Kiln causes;
    this is the half that covers the prints it does not — one started from
    the printer's own touchscreen never passes through ``start_print`` at
    all.  Both doors already call this, so the rule lives in one place
    rather than being re-stated per door.

    Without it, ``emergency_stop`` with no printer named — which sweeps every
    registered machine, idle ones included — left an intent on a printer that
    had no print to cancel, and the next touchscreen print to end on an
    ambiguous idle inherited it and was recorded cancelled after finishing.
    """
    prev = _HOOK_STATE.previous_state(printer_name)
    _HOOK_STATE.set_previous_state(printer_name, current_state)
    # Cheap string checks first: the clear touches durable storage, and this
    # runs on every status frame, but the edge itself happens once per print.
    if (
        current_state
        and current_state.lower().strip() in _ACTIVE_STATES
        and (prev or "").lower().strip() not in _ACTIVE_STATES
    ):
        clear_cancel_intent(printer_name)
    return prev


def open_pending_outcome(
    printer_name: str,
    file_name: str | None,
    material_type: str | None = None,
) -> str | None:
    """Open a ``pending`` outcome row the moment a print starts.

    Called from ``PrinterAdapter.start_print`` — the template method
    every adapter and entry point passes through — so every print Kiln
    initiates leaves a row whether or not anything is still alive when
    it ends.  The job id is synthetic (the printer's own job label
    isn't known until its firmware reports it); the resolution paths
    match by printer + file and adopt the real id.  Never raises.

    :returns: The synthetic job id, or ``None`` when nothing was opened.
    """
    try:
        from kiln.persistence import get_db

        job_id = f"start:{(printer_name or '').strip()[:64]}:{int(time.time() * 1000)}"
        row_id = get_db().open_pending_outcome(
            job_id=job_id,
            printer_name=printer_name,
            file_name=file_name,
            material_type=material_type,
        )
        return job_id if row_id is not None else None
    except Exception as exc:
        _logger.debug("open_pending_outcome failed (non-fatal): %s", exc)
        return None


def reconcile_pending_outcomes(
    *,
    printer_name: str,
    gcode_state: str,
    print_error_code: int = 0,
    current_job_label: str | None = None,
    legacy_printer_name: str | None = None,
) -> list[dict[str, Any]]:
    """Settle pending outcome rows with what a reconnected printer can say.

    Runs once per adapter connection.  A printer's status on reconnect
    is CURRENT state, not history — an idle machine says nothing about
    whether the last job finished, failed at layer 300, or was
    cancelled at the touchscreen.  So each pending row resolves only as
    far as the machine's testimony honestly reaches:

    - the printer still shows a TERMINAL state for the matching job →
      ``success`` / ``failed`` (``determined_by='inferred'``);
    - the printer is actively printing → every pending row is left
      alone (the live terminal-state hook owns the ending);
    - anything else — idle, job gone, a different job's remains →
      ``unknown``, never success.  The unknown row is excluded from all
      success-rate and proven-settings math and surfaced next session
      so the user, who is holding the part, can settle it.

    *legacy_printer_name* sweeps rows stranded under an older name for the
    same machine.  Pending rows used to be opened under the backend FAMILY
    (``adapter.name`` — "bambu" for every Bambu) while every resolver now
    keys on the registered name, so upgraded installs carry rows nothing
    would ever look up again — 693 of them on the install this was found
    on.  Legacy rows resolve by the same stem-match testimony as current
    ones, except that a lone unlabelled row never adopts a terminal state:
    these rows are old by definition, and a terminal state on reconnect is
    not testimony about a days-old job unless it still NAMES it.

    :returns: The rows that were resolved (possibly empty).
    """
    state = (gcode_state or "").lower().strip()
    if not printer_name or state in _ACTIVE_STATES:
        return []

    try:
        from kiln.persistence import _file_stem_token, get_db

        db = get_db()
        pending = db.list_print_outcomes(
            printer_name=printer_name, outcome="pending", limit=50,
        )
        legacy: list[dict[str, Any]] = []
        if legacy_printer_name and legacy_printer_name != printer_name:
            # Bounded and drained over successive connects, not all at once.
            legacy = db.list_print_outcomes(
                printer_name=legacy_printer_name, outcome="pending", limit=200,
            )
    except Exception as exc:
        _logger.debug("reconcile_pending_outcomes: DB unavailable: %s", exc)
        return []

    if not pending and not legacy:
        return []

    label_token = _file_stem_token(current_job_label)
    resolved: list[dict[str, Any]] = []
    rows = [(r, False) for r in pending] + [(r, True) for r in legacy]
    for row, is_legacy in rows:
        row_token = _file_stem_token(row.get("file_name"))
        # The machine's terminal report is only testimony about the job
        # it names.  A stem match ties them; a lone pending row may
        # claim an unlabelled report (single-printer reality), but a
        # MISmatched label means some other job ran after ours — which
        # tells us nothing about how ours ended.
        matches = bool(label_token) and label_token == row_token
        lone_unlabelled = (
            not is_legacy and not label_token and len(pending) == 1
        )

        outcome: str
        failure_mode: str | None = None
        if state in _FINISH_STATES and (matches or lone_unlabelled):
            outcome = "success"
            note = (
                "resolved on reconnect: printer still reported this job "
                "finished cleanly"
            )
        elif state in _FAILED_STATES and (matches or lone_unlabelled):
            outcome = "failed"
            failure_mode = _failure_mode_from_code(print_error_code)
            note = (
                "resolved on reconnect: printer still reported this job "
                f"in a failed state (print_error={print_error_code})"
            )
        elif state in _CANCELLED_STATES and (matches or lone_unlabelled):
            outcome = "cancelled"
            note = (
                "resolved on reconnect: printer still reported this job "
                "as ended without completing"
            )
        else:
            outcome = "unknown"
            note = (
                "print ended while Kiln was not watching; the printer's "
                "state on reconnect could not say how it went — ask the "
                "user, who has the part"
            )

        try:
            update: dict[str, Any] = {
                "job_id": row["job_id"],
                "printer_name": printer_name,
                "outcome": outcome,
                "agent_id": "auto",
                "determined_by": "inferred",
                "notes": note,
            }
            if failure_mode:
                update["failure_mode"] = failure_mode
            db.save_print_outcome(update)
            resolved.append({**row, "outcome": outcome, "notes": note})
            # Federate the resolution.  Watched endings and user reports
            # already reach the community pool through their own doors;
            # until 2026-08-05 a reconciled ending reached only the local
            # DB, so every print that outlived its session was missing
            # from the shared corpus.  ``unknown`` contributes nothing
            # (translate_outcome refuses non-verdicts), and the shared
            # dedupe key collapses any later refinement of this job.
            if outcome in ("success", "failed"):
                try:
                    from kiln import community_autofire

                    community_autofire.contribute_resolved_outcome(
                        outcome=outcome,
                        printer_file_name=row.get("file_name"),
                        job_id=row.get("job_id"),
                        printer_name=printer_name,
                        material=row.get("material_type"),
                        failure_mode=failure_mode,
                    )
                except Exception:
                    _logger.debug(
                        "reconcile federation skipped (best-effort)",
                        exc_info=True,
                    )
            _logger.info(
                "reconcile_pending_outcomes: %r (started %s) → %r",
                row.get("file_name") or row["job_id"],
                row.get("created_at"),
                outcome,
            )
        except Exception as exc:
            _logger.debug(
                "reconcile_pending_outcomes: could not resolve %r: %s",
                row.get("job_id"), exc,
            )
    return resolved


__all__ = [
    "fire_terminal_state_hook",
    "clear_cancel_intent",
    "note_cancel_requested",
    "observe_state",
    "open_pending_outcome",
    "reconcile_pending_outcomes",
    "register_cancel_intent",
]
