"""A print that was STOPPED is not a print that succeeded.

Bambu firmware has no "cancelled" gcode_state: a cancelled print reports
the same ``idle`` a finished one does.  ``register_cancel_intent`` exists to
tell them apart — the next ending after a cancel was asked for is a cancel.

It had exactly ONE caller, ``cancel_print``, and that call filed the flag
under a name the reader never looked up (see
:mod:`test_outcomes_name_the_machine`).  Five other paths end a print and
registered nothing at all:

* ``kiln cancel`` — the CLI;
* the health monitor's auto-cancel;
* ``watch_print``'s cancel-at-percent;
* the print watchdog's emergency stop;
* the emergency coordinator's.

Every one of them recorded a SUCCESS.  The auto-cancel pair are the ones
that matter most: Kiln stops those prints precisely because it has judged
them to be failing, so the loop was being taught that the runs Kiln flagged
were its best ones — the exact inversion of the signal.

Two things had to be true for one helper to serve all six.  It keys off the
ADAPTER, so the writer and the reader cannot disagree about the printer's
name.  And the intent is DURABLE, because ``kiln cancel`` sends the stop and
exits — the ending is seen by whatever is left watching, in another process
entirely, where an in-memory flag stamped with a monotonic clock means
nothing.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

# Imported at module scope on purpose.  Importing it inside a test would load
# the tool plugins for the first time while the fixture below has KILN_DB_PATH
# pointed at a throwaway database, and a plugin that registers under that never
# registers again — which silently drops tools from the schema for every later
# test in the run.
import kiln.server  # noqa: F401  (registers the tool plugins)
from kiln import auto_record_hook as hook
from kiln.printers import progress_motion as pm
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _reset_lifecycle_state(tmp_path, monkeypatch):
    """Fresh in-memory state AND a throwaway database for the durable half.

    The database matters here in a way it does not elsewhere in this suite:
    a durable intent that leaked from the previous test would make these
    pass for the wrong reason.  ``persistence._db`` is a module singleton
    built on first use, so the env var alone would be ignored by every test
    after the first — it has to be torn down on both sides.
    """
    import kiln.persistence as persistence

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None)
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()
    yield
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()
    persistence._db = None


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch):
    calls: list[dict] = []

    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: calls.append(kw) or {"success": True},
    )
    return calls


def _bambu(monkeypatch, *, registered_as: str | None = "garage"):
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    adapter = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )
    if registered_as:
        PrinterRegistry().register(registered_as, adapter)
    return adapter


def _push(adapter, gcode_state: str, *, job: str = "bracket") -> None:
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": gcode_state,
            "subtask_name": job,
            "gcode_file": f"/sdcard/{job}.3mf",
            "print_error": 0,
        }
    }
    adapter._on_message(
        None, None, SimpleNamespace(payload=json.dumps(payload).encode())
    )


def _outcomes(calls) -> list[str]:
    return [c["outcome"] for c in calls]


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


def test_a_stop_from_any_door_is_recorded_as_a_cancel(_no_db_writes, monkeypatch):
    """The helper is what every stop path calls; this is that contract.

    Driven at the helper rather than through five tools, because what each
    door owes is exactly this one call — the tools are pinned separately
    below by reading their source.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)
    _push(adapter, "IDLE")

    assert _outcomes(_no_db_writes) == ["cancelled"]


@pytest.mark.parametrize(
    ("module", "symbol"),
    [
        # cancel_print delegates its whole body to _cancel_print_on — the
        # engine kiln-pro's fleet fan-out drives too — so the call lives
        # there, and the tool's delegation is pinned separately below.
        ("kiln.server", "_cancel_print_on"),
        ("kiln.cli.main", "cancel"),
        ("kiln.print_health_monitor", None),
        ("kiln.plugins.monitoring_tools", None),
        ("kiln.print_watchdog", None),
        ("kiln.emergency", None),
    ],
)
def test_every_stop_path_notes_the_cancel(module, symbol):
    """Pinned by source, because what regressed is a call site going missing.

    A door that stops a print without this call is the bug, and it is
    invisible from the outcome alone — an unnoticed cancel looks exactly
    like a print that finished.
    """
    import importlib
    import inspect

    mod = importlib.import_module(module)
    if symbol is None:
        source = inspect.getsource(mod)
    else:
        fn = getattr(mod, symbol)
        source = inspect.getsource(getattr(fn, "fn", getattr(fn, "callback", fn)))

    # The CALL, with its bracket — not the bare name, which the import line
    # also carries.  Checking for the name alone passed on a module whose
    # call had been deleted and whose import was left behind, which is the
    # same "looks wired" shape this whole area keeps producing.
    assert "note_cancel_requested(" in source, (
        f"{module}{'.' + symbol if symbol else ''} ends a print without "
        "noting the cancel — its stops will record as successes"
    )


def test_the_cancel_tool_still_routes_through_the_engine():
    """The delegation the row above depends on.

    Pinned because the source scan can only follow it one way: if
    cancel_print ever stopped calling the engine and grew its own body
    again, the scan would keep passing against an engine nobody calls.
    """
    import inspect

    from kiln import server as ksrv

    body = inspect.getsource(getattr(ksrv.cancel_print, "fn", ksrv.cancel_print))
    assert "_cancel_print_on(" in body


def test_the_auto_cancel_that_saw_a_failure_is_not_filed_as_a_win(
    _no_db_writes, monkeypatch
):
    """The worst case, and the reason this is not merely tidiness.

    The health monitor cancels because it JUDGED the print to be failing.
    Recorded as a success, that print becomes training data arguing the
    settings which produced it were good.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    # What print_health_monitor._auto_cancel does, in order.
    hook.note_cancel_requested(adapter)
    _push(adapter, "IDLE")

    assert _outcomes(_no_db_writes) == ["cancelled"]


# ---------------------------------------------------------------------------
# The durable half — the CLI's ending is watched by another process
# ---------------------------------------------------------------------------


def test_an_intent_survives_the_process_that_asked_for_it(_no_db_writes, monkeypatch):
    """``kiln cancel`` sends the stop and exits before the printer stops.

    Simulated exactly: the intent is registered, then every scrap of
    in-memory state is thrown away — a new process — and only then does the
    ending arrive.  Nothing in memory can survive that, so if this passes,
    the durable record is what carried it.
    """
    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")

    hook.note_cancel_requested(adapter)     # the CLI process
    hook._HOOK_STATE = hook._HookState()    # ...which then exits

    _push(adapter, "IDLE")                  # the server process sees the end

    assert _outcomes(_no_db_writes) == ["cancelled"]


def test_the_durable_intent_is_single_use(_no_db_writes, monkeypatch):
    """Consumed once, or it would label the NEXT print a cancel too."""
    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)
    hook._HOOK_STATE = hook._HookState()
    _push(adapter, "IDLE")

    # A second print on the same machine, finishing cleanly.
    hook._HOOK_STATE = hook._HookState()
    _push(adapter, "RUNNING", job="spool-holder")
    _push(adapter, "IDLE", job="spool-holder")

    assert _outcomes(_no_db_writes) == ["cancelled", "success"]


def test_a_new_print_clears_a_stale_intent(monkeypatch):
    """What replaces guessing at how long a stop takes.

    The in-memory TTL is five seconds — a guess at how long a printer needs
    to stop moving, retract, park and report idle, and short enough that a
    real cancel can outlive it.  Rather than guess a bigger number, the
    intent now lives until the event the window was standing in for: the
    next print starting on that machine.
    """
    adapter = _bambu(monkeypatch)
    hook.note_cancel_requested(adapter)

    from kiln.printers.base import outcome_printer_name

    name = outcome_printer_name(adapter)
    hook.clear_cancel_intent(name)

    assert hook._HOOK_STATE.consume_cancel_intent(name) is False


def test_start_print_clears_the_intent(monkeypatch):
    """And the clear is wired into the one path every print start passes."""
    import inspect

    from kiln.printers.base import PrinterAdapter

    source = inspect.getsource(PrinterAdapter.start_print)
    assert "clear_cancel_intent" in source


def test_a_stop_on_an_idle_printer_cannot_label_the_next_print(
    _no_db_writes, monkeypatch
):
    """``emergency_stop`` with no printer named sweeps EVERY machine.

    Idle ones included — and an idle printer has no print to cancel, so the
    intent left on it belongs to nothing.  ``start_print`` cannot clear it
    either, because the next print may be started from the printer's own
    touchscreen, which never passes through Kiln at all.  Watching the
    machine ENTER an active state is what retires it, and that is observed
    whoever started the print.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "IDLE")                       # nothing running
    hook.note_cancel_requested(adapter)          # the sweep hits it anyway
    _push(adapter, "RUNNING", job="next-job")    # a touchscreen print begins
    _push(adapter, "IDLE", job="next-job")       # and finishes, ambiguously

    assert _outcomes(_no_db_writes) == ["success"]


def test_resuming_a_paused_print_does_not_retire_the_intent(monkeypatch):
    """The clear fires on entering an active state, and pause IS one.

    So a resume is not mistaken for a new print — otherwise cancelling a
    paused print would lose its label the moment the printer moved again.
    """
    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")
    _push(adapter, "PAUSE")
    hook.note_cancel_requested(adapter)
    _push(adapter, "RUNNING")

    from kiln.printers.base import outcome_printer_name

    assert hook._HOOK_STATE.consume_cancel_intent(
        outcome_printer_name(adapter)
    ) is True


def test_a_slow_stop_still_counts(_no_db_writes, monkeypatch):
    """The five-second window is no longer what decides this.

    A printer that takes a full minute to finish its stop sequence used to
    have its cancel recorded as a success, because the intent had expired
    before the ending arrived.
    """
    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)

    # Age the durable stamp well past the in-memory TTL, and drop memory so
    # only the durable record can answer.
    from kiln.persistence import get_db
    from kiln.printers.base import outcome_printer_name

    key = hook._CANCEL_INTENT_KEY.format(outcome_printer_name(adapter))
    get_db().set_setting(key, repr(time.time() - 60.0))
    hook._HOOK_STATE = hook._HookState()

    _push(adapter, "IDLE")

    assert _outcomes(_no_db_writes) == ["cancelled"]


def test_an_ancient_intent_is_refused(_no_db_writes, monkeypatch):
    """The backstop: nothing was watching, and no next print ever came."""
    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)

    from kiln.persistence import get_db
    from kiln.printers.base import outcome_printer_name

    key = hook._CANCEL_INTENT_KEY.format(outcome_printer_name(adapter))
    get_db().set_setting(
        key, repr(time.time() - (hook._CANCEL_INTENT_MAX_AGE_S + 60))
    )
    hook._HOOK_STATE = hook._HookState()

    _push(adapter, "IDLE")

    assert _outcomes(_no_db_writes) == ["success"]


def test_a_stamp_from_the_future_is_not_an_eternal_intent(monkeypatch):
    """A wall clock can move backwards; a monotonic one cannot.

    That is the price of making this readable across processes, so a stamp
    that cannot be true is refused rather than trusted forever.
    """
    adapter = _bambu(monkeypatch)
    from kiln.persistence import get_db
    from kiln.printers.base import outcome_printer_name

    name = outcome_printer_name(adapter)
    get_db().set_setting(
        hook._CANCEL_INTENT_KEY.format(name), repr(time.time() + 3600)
    )

    assert hook._HOOK_STATE.consume_cancel_intent(name) is False


def test_a_broken_database_never_blocks_a_cancel(monkeypatch):
    """The ledger is not allowed to stand between a user and a stopped print."""
    import kiln.persistence as persistence

    def _explode(*a, **k):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(persistence, "get_db", _explode)

    adapter = _bambu(monkeypatch, registered_as=None)
    hook.note_cancel_requested(adapter)      # must not raise
    assert hook._HOOK_STATE.consume_cancel_intent("bambu") is True  # memory still works


# ---------------------------------------------------------------------------
# The ending we know most about — the one that was asked for
# ---------------------------------------------------------------------------


def test_a_cancel_that_trips_a_fault_is_still_a_cancel(_no_db_writes, monkeypatch):
    """Aborting a print can ITSELF trip a firmware fault.

    Measured on an A1 (2026-08-13): cancelling during bed levelling aborts
    the homing move, and the firmware reports ``gcode_state=failed`` with a
    real Z-homing error code — ``print_error=50348044``.  The failed branch
    returned before ever consulting the cancel intent, so the one ending we
    know the MOST about (the user asked for it) was recorded as a machine
    failure, with a failure_mode fabricated from the code the abort itself
    produced.  Every such cancel taught the failure statistics about a fault
    that never happened on its own.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": "FAILED",
            "subtask_name": "bracket",
            "gcode_file": "/sdcard/bracket.3mf",
            "print_error": 50348044,
        }
    }
    adapter._on_message(
        None, None, SimpleNamespace(payload=json.dumps(payload).encode())
    )

    assert _outcomes(_no_db_writes) == ["cancelled"]
    # And no fabricated failure mode rides along with it.
    assert _no_db_writes[-1].get("failure_mode") in (None, "")


def test_a_spontaneous_fault_still_records_failed(_no_db_writes, monkeypatch):
    """No intent, same fault — the diagnosis must survive untouched.

    The change above is only allowed to reclassify endings somebody asked
    for.  A fault with no cancel behind it keeps its code-derived
    failure_mode, or the fix would cost exactly the signal it protects.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": "FAILED",
            "subtask_name": "bracket",
            "gcode_file": "/sdcard/bracket.3mf",
            "print_error": 50348044,
        }
    }
    adapter._on_message(
        None, None, SimpleNamespace(payload=json.dumps(payload).encode())
    )

    assert _outcomes(_no_db_writes) == ["failed"]
    assert _no_db_writes[-1].get("failure_mode")


# ---------------------------------------------------------------------------
# Pending rows: opened under the name their resolvers will look up
# ---------------------------------------------------------------------------


def test_pending_rows_open_under_the_registered_name():
    """The identity fix moved every RESOLVER to the registered name.

    Both reconcile doors and save_print_outcome's pending-row adoption key
    on outcome_printer_name — but the row was still OPENED under self.name,
    the backend family.  Opened under a name no resolver looks up, every
    print left one more forever-pending row, and its real ending was
    inserted as a second row beside it.  Pinned by source because what
    regressed is which name a call site passes.
    """
    import inspect

    from kiln.printers.base import PrinterAdapter

    source = inspect.getsource(PrinterAdapter.start_print)
    assert "open_pending_outcome(\n                    outcome_printer_name(self)" in source, (
        "start_print opens pending rows under a name the resolvers will "
        "never look up"
    )


def test_reconcile_sweeps_rows_stranded_under_the_family_name(
    _no_db_writes, monkeypatch
):
    """Rows opened before the identity fix are keyed by the family name.

    A resolver that only queries the registered name leaves them pending
    FOREVER — 693 of them on the install this was found on.  On the first
    status after connect, the sweep settles them with exactly the honesty
    the reconciler already promises: a row today's testimony cannot reach
    resolves to unknown, never success.
    """
    from kiln.persistence import get_db

    db = get_db()
    db.open_pending_outcome(
        job_id="start:bambu:1691000000000",
        printer_name="bambu",              # the family name, as before the fix
        file_name="old-part.gcode",
    )

    adapter = _bambu(monkeypatch)           # registered as "garage"
    _push(adapter, "IDLE", job="bracket")   # first status after connect

    assert db.list_print_outcomes(printer_name="bambu", outcome="pending") == []
    # Settled to unknown — testimony from today cannot reach a days-old job —
    # and never laundered into a success.  include_all, because the default
    # listing hides unresolved rows.
    rows = db.list_print_outcomes(printer_name="bambu", limit=5, include_all=True)
    assert [r.get("outcome") for r in rows] == ["unknown"]


def test_the_polled_door_sweeps_the_family_name_too(_no_db_writes, monkeypatch):
    """The other door to the same reconciler, with its own cheap gate.

    The gate only calls reconcile when a pending row exists — and it asked
    under the registered name alone, so a backlog stranded under the family
    name never even triggered the sweep on polled backends.
    """
    from kiln.persistence import get_db
    from kiln.printers.base import (
        JobProgress,
        PrinterAdapter,
        PrinterCapabilities,
        PrinterState,
        PrintResult,
        UploadResult,
    )

    class _Idle(PrinterAdapter):
        # Filament handling is part of the adapter contract; these stubs
        # never move filament, so the hooks refuse.
        def _load_filament_impl(self, plan):
            raise NotImplementedError
        def _unload_filament_impl(self, plan):
            raise NotImplementedError
        def _purge_filament_impl(self, plan):
            raise NotImplementedError

        @property
        def name(self) -> str:
            return "moonraker"

        @property
        def capabilities(self) -> PrinterCapabilities:
            return PrinterCapabilities()

        def get_state(self) -> PrinterState:
            return PrinterState(connected=True, state=PrinterStatus.IDLE)

        def get_job(self) -> JobProgress:
            return JobProgress(file_name=None)

        def _start_print_impl(self, file_name, **kw):
            return PrintResult(success=True, message="ok")

        def list_files(self):
            return []

        def upload_file(self, file_path):
            return UploadResult(success=True, message="ok")

        def delete_file(self, file_name):
            return True

        def cancel_print(self):
            return PrintResult(success=True, message="ok")

        def pause_print(self):
            return PrintResult(success=True, message="ok")

        def _resume_print_impl(self):
            return PrintResult(success=True, message="ok")

        def emergency_stop(self):
            return PrintResult(success=True, message="ok")

        def send_gcode(self, command):
            return "ok"

        def set_tool_temp(self, celsius, tool=0):
            return True

        def set_bed_temp(self, celsius):
            return True

    db = get_db()
    db.open_pending_outcome(
        job_id="start:moonraker:1691000000001",
        printer_name="moonraker",
        file_name="old-benchy.gcode",
    )

    adapter = _Idle()
    PrinterRegistry().register("shop", adapter)
    adapter.get_state()                      # first poll after connect

    assert db.list_print_outcomes(printer_name="moonraker", outcome="pending") == []


# ---------------------------------------------------------------------------
# The levelling window — where a cancel can brick the printer
# ---------------------------------------------------------------------------
#
# Four cancels on an A1 (2026-08-13), one variable at a time:
#
#   during levelling, no pause  -> Z-homing fault, LATCHED past 13 minutes,
#                                  survived clean_print_error and a G28;
#                                  only a power cycle cleared it       (x2)
#   after levelling, no pause   -> clean: idle, last_job_result=cancelled
#   during levelling, PAUSED    -> same fault code, but TRANSIENT: gone in
#                                  ~15s, landing on cancelled, no power cycle
#
# So the pause does not prevent the fault, it makes it survivable — which is
# the whole difference between "carry on" and "walk to the machine".


def _job(layer, completion):
    from kiln.printers.base import JobProgress

    return JobProgress(
        file_name="bracket.3mf", current_layer=layer, completion=completion,
    )


def test_the_window_is_read_from_the_job_not_the_state():
    """The state word cannot tell levelling from printing.

    An A1 reports "printing" throughout bed levelling exactly as it does
    mid-part.  What separates them is that nothing has been laid down yet:
    across six cancels, every one that tripped the fault read layer 0 at 0%,
    and the run that cancelled cleanly had reported 1%.
    """
    from kiln.printers.base import in_calibration_window

    assert in_calibration_window(None, _job(0, 0.0)) is True
    assert in_calibration_window(None, _job(0, 1.0)) is False
    assert in_calibration_window(None, _job(3, 42.0)) is False
    # Unknown counts as inside: what this gates is a sentence, so an
    # unnecessary one costs nothing.
    assert in_calibration_window(None, None) is True


def test_nothing_is_sent_to_the_printer_for_the_window():
    """Kiln knows this window is hazardous and does NOT act on it.

    Pausing first was tried against real hardware and, across six cancels,
    changed nothing about whether the fault stuck — one paused run cleared,
    two unpaused runs latched, three unpaused runs cleared.  An intervention
    with no measured effect, sent to a printer mid-homing, is the shape this
    codebase keeps having to remove; it is not shipped here.
    """
    import inspect

    from kiln.printers.base import PrinterAdapter

    source = inspect.getsource(PrinterAdapter.__init_subclass__)
    assert "cancel_print" not in source or "_kiln_calibration_guarded" not in source, (
        "cancel_print is wrapped again — the pause was measured to do nothing"
    )


def test_cancelling_in_the_window_says_what_to_expect(monkeypatch):
    """The guidance that IS supported: wait before power-cycling.

    Four of six cancels in this window cleared themselves inside about a
    minute, so telling the user to wait is right two times in three — and it
    is the difference between a minute of patience and a walk to the machine.
    """
    from unittest.mock import MagicMock

    from kiln import server
    from kiln.printers.base import PrinterCapabilities

    adapter = MagicMock()
    adapter.name = "bambu"
    adapter.capabilities = PrinterCapabilities(
        cancel_during_calibration_faults=True,
    )
    adapter.get_state.return_value = PrinterState(
        connected=True, state=PrinterStatus.PRINTING,
    )
    adapter.get_job.return_value = _job(0, 0.0)
    adapter.cancel_print.return_value = PrintResult(
        success=True, message="Print cancelled.",
    )

    def _fixed():
        return adapter

    lim = server._tool_limiter
    for d in (lim._call_history, lim._last_call, lim._block_history, lim._cooldown_until):
        d.clear()
    monkeypatch.setattr(server, "_get_adapter", _fixed)

    fn = server.cancel_print
    out = getattr(fn, "fn", getattr(fn, "callback", fn))()

    assert "minute" in out["note"].lower()
    assert "power-cycl" in out["note"].lower()
    # And the cancel itself is untouched — no extra command to the printer.
    assert not adapter.pause_print.called


def test_a_normal_cancel_says_nothing_extra(monkeypatch):
    """Past the routine there is nothing to warn about, so it stays quiet."""
    from unittest.mock import MagicMock

    from kiln import server
    from kiln.printers.base import PrinterCapabilities

    adapter = MagicMock()
    adapter.name = "bambu"
    adapter.capabilities = PrinterCapabilities(
        cancel_during_calibration_faults=True,
    )
    adapter.get_state.return_value = PrinterState(
        connected=True, state=PrinterStatus.PRINTING,
    )
    adapter.get_job.return_value = _job(3, 42.0)
    adapter.cancel_print.return_value = PrintResult(
        success=True, message="Print cancelled.",
    )

    def _fixed():
        return adapter

    lim = server._tool_limiter
    for d in (lim._call_history, lim._last_call, lim._block_history, lim._cooldown_until):
        d.clear()
    monkeypatch.setattr(server, "_get_adapter", _fixed)

    fn = server.cancel_print
    out = getattr(fn, "fn", getattr(fn, "callback", fn))()

    assert "note" not in out


