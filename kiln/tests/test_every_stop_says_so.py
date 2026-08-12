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

from kiln import auto_record_hook as hook
from kiln.printers import progress_motion as pm
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
        ("kiln.server", "cancel_print"),
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
