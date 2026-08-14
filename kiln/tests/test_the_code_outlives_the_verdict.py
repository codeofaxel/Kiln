"""The firmware's error code is EVIDENCE; the failure_mode is a VERDICT.

Only the verdict was ever wrong about a cancel.  ``test_every_stop_says_so``
established that a deliberate stop outranks the firmware's error word —
correct, because aborting a print can itself trip a fault and the print was
still cancelled, not failed.  But the fix as shipped threw the code away
along with the verdict, so the endings that trip faults most reliably left
no diagnostic trace at all.

What the code is FOR is the other half.  Characterising one A1 fault took
six deliberate hardware runs in a single session: cancelling while the
printer is still levelling aborts a homing move, and the firmware reports
``gcode_state=failed`` with ``print_error=50348044`` (0x0300400C, Z axis
homing failed).  Four of six cleared themselves inside about a minute; two
latched past thirteen minutes, survived a ``clean_print_error`` and a G28,
and released only on a power cycle.  Nothing predicted which — pausing
first and cold-starting were both tested and falsified.

Six runs on one machine buys "sometimes, about a third of the time".
Which models, which firmware, and what predicts a latch are questions only
many machines can answer, and they can only be asked of rows that KEPT the
code.  These tests pin the capture half: the code lands on the row whatever
the outcome word turned out to be, it never lands on a row nobody can tie
it to, and a database written before the column existed grows one.
"""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

# Imported at module scope on purpose — see the note in
# test_every_stop_says_so: loading the tool plugins for the first time
# while the fixture has KILN_DB_PATH pointed at a throwaway database
# silently drops tools from the schema for every later test in the run.
import kiln.server  # noqa: F401  (registers the tool plugins)
from kiln import auto_record_hook as hook
from kiln.registry import PrinterRegistry

_Z_HOMING_FAILED = 50348044  # 0x0300400C, as measured on the A1


@pytest.fixture(autouse=True)
def tmp_kiln_env(tmp_path, monkeypatch):
    """A throwaway database, and the hook state that reads it.

    Also suspends kiln-pro's learning-engine patch on
    ``save_print_outcome`` when kiln-pro is importable: what is pinned
    here is PUBLIC row behavior, which must hold identically on an
    install without it.
    """
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    hook._HOOK_STATE = hook._HookState()

    pro_hook_was_installed = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record

        pro_hook_was_installed = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass

    yield tmp_path

    if pro_hook_was_installed:
        _pro_auto_record.install_auto_record_hook()
    hook._HOOK_STATE = hook._HookState()
    monkeypatch.setattr(_p, "_db", None, raising=False)


def _bambu(monkeypatch, *, registered_as: str = "garage"):
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    adapter = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )
    PrinterRegistry().register(registered_as, adapter)
    return adapter


def _push(adapter, gcode_state: str, *, job: str = "bracket", error: int = 0) -> None:
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": gcode_state,
            "subtask_name": job,
            "gcode_file": f"/sdcard/{job}.3mf",
            "print_error": error,
        }
    }
    adapter._on_message(
        None, None, SimpleNamespace(payload=json.dumps(payload).encode())
    )


def _rows(printer_name: str = "garage") -> list[dict]:
    from kiln.persistence import get_db

    return get_db().list_print_outcomes(
        printer_name=printer_name, limit=10, include_all=True,
    )


# ---------------------------------------------------------------------------
# The cancel that tripped a fault — evidence kept, verdict withheld
# ---------------------------------------------------------------------------


def test_a_cancelled_row_carries_the_code_it_tripped(monkeypatch):
    """The row this whole change exists for.

    A cancel during levelling IS a cancel — nobody's machine failed — and
    the firmware DID complain while it happened.  The row has to be able to
    say both, which it can only do if the code and the verdict are separate
    columns.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    hook.note_cancel_requested(adapter)
    _push(adapter, "FAILED", error=_Z_HOMING_FAILED)

    (row,) = _rows()
    assert row["outcome"] == "cancelled"
    assert row["print_error"] == _Z_HOMING_FAILED
    # And no verdict rides along with it: the fabricated-failure bug that
    # test_every_stop_says_so fixed must stay fixed.
    assert row["failure_mode"] in (None, "")


def test_a_spontaneous_fault_keeps_both(monkeypatch):
    """No cancel behind it — the diagnosis survives untouched.

    Keeping the code was never licence to weaken the verdict on an ending
    that really was a machine failure.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    _push(adapter, "FAILED", error=_Z_HOMING_FAILED)

    (row,) = _rows()
    assert row["outcome"] == "failed"
    assert row["print_error"] == _Z_HOMING_FAILED
    assert row["failure_mode"]


def test_a_clean_finish_files_no_code(monkeypatch):
    """``print_error=0`` is the firmware saying it has nothing to name.

    Stored as a literal 0 it would mean every clean print filed a code, and
    every reader counting faults would have to remember to exclude it.
    """
    adapter = _bambu(monkeypatch)

    _push(adapter, "RUNNING")
    _push(adapter, "FINISH")

    (row,) = _rows()
    assert row["outcome"] == "success"
    assert row["print_error"] is None


# ---------------------------------------------------------------------------
# Backends that have no such code at all
# ---------------------------------------------------------------------------


def test_a_backend_with_no_code_stores_null():
    """Most printers report no numeric fault code, and never will.

    The column is NULL for them — not 0, which would read as a real
    printer reporting a real absence of fault.  The polled door that every
    non-Bambu adapter reaches the hook through passes no code at all.
    """
    from kiln.persistence import get_db

    hook.fire_terminal_state_hook(
        prev_state="printing",
        new_state="failed",
        print_error_code=0,          # what the polled door passes
        printer_name="shop",
        job_id="benchy",
        file_name="benchy.gcode",
    )

    (row,) = _rows("shop")
    assert row["outcome"] == "failed"
    assert row["print_error"] is None
    assert get_db().get_print_outcome("benchy")["print_error"] is None


def test_a_code_that_cannot_be_grouped_is_refused():
    """This column exists to be grouped and counted.

    A value nobody can group is worse than an honest blank, so the tool
    layer says so rather than filing it — and the storage layer blanks it
    anyway, because the auto-record hook must never raise into the path
    that is recording how somebody's print ended.
    """
    from kiln.persistence import normalize_print_error
    from kiln.plugins.learning_tools import record_print_outcome

    out = record_print_outcome(
        job_id="junk", outcome="failed", printer_name="garage",
        print_error="not-a-code",
    )
    assert out.get("success") is not True

    assert normalize_print_error("not-a-code") is None
    assert normalize_print_error(None) is None
    assert normalize_print_error(0) is None
    assert normalize_print_error(True) is None          # not a code
    assert normalize_print_error(_Z_HOMING_FAILED) == _Z_HOMING_FAILED


# ---------------------------------------------------------------------------
# The row the ending actually lands on
# ---------------------------------------------------------------------------


def test_the_adopted_pending_row_keeps_the_code(monkeypatch):
    """Almost every real ending resolves a row opened at print START.

    That row predates the fault by definition, so if the adoption path
    dropped the code, the capture would be dead on exactly the path that
    produces nearly all of it — while looking wired everywhere else.
    """
    hook.open_pending_outcome("garage", "/tmp/bracket.gcode.3mf")

    adapter = _bambu(monkeypatch)
    _push(adapter, "RUNNING")
    _push(adapter, "FAILED", error=_Z_HOMING_FAILED)

    rows = _rows()
    assert len(rows) == 1, "the ending resolved the pending row, not a second one"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["print_error"] == _Z_HOMING_FAILED


def test_the_reconciler_pins_a_code_only_to_the_job_it_names():
    """A printer's state on reconnect is testimony about ONE job.

    When it names a different job — or names none — the pending row
    resolves to ``unknown``, and a code filed there would attach a fault to
    a print nobody can say it belongs to.  Poisoning a fleet corpus needs
    only a few of those.
    """
    from kiln.persistence import get_db

    get_db().open_pending_outcome(
        job_id="start:garage:1691000000000",
        printer_name="garage",
        file_name="bracket.3mf",
    )

    hook.reconcile_pending_outcomes(
        printer_name="garage",
        gcode_state="failed",
        print_error_code=_Z_HOMING_FAILED,
        current_job_label="some-other-part.3mf",   # not our print
    )

    (row,) = _rows()
    assert row["outcome"] == "unknown"
    assert row["print_error"] is None


def test_the_reconciler_keeps_the_code_for_the_job_it_does_name():
    """The other half — testimony that DOES reach this row brings its code."""
    from kiln.persistence import get_db

    get_db().open_pending_outcome(
        job_id="start:garage:1691000000001",
        printer_name="garage",
        file_name="bracket.3mf",
    )

    hook.reconcile_pending_outcomes(
        printer_name="garage",
        gcode_state="failed",
        print_error_code=_Z_HOMING_FAILED,
        current_job_label="bracket.3mf",
    )

    (row,) = _rows()
    assert row["outcome"] == "failed"
    assert row["print_error"] == _Z_HOMING_FAILED


# ---------------------------------------------------------------------------
# Databases written before the column existed
# ---------------------------------------------------------------------------


_PRE_COLUMN_SCHEMA = """
    CREATE TABLE print_outcomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          TEXT NOT NULL,
        printer_name    TEXT NOT NULL,
        file_name       TEXT,
        file_hash       TEXT,
        material_type   TEXT,
        outcome         TEXT NOT NULL,
        quality_grade   TEXT,
        failure_mode    TEXT,
        settings        TEXT,
        environment     TEXT,
        notes           TEXT,
        agent_id        TEXT,
        determined_by   TEXT,
        created_at      REAL NOT NULL
    );
"""


def test_an_older_database_grows_the_column(tmp_path, monkeypatch):
    """``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched.

    So every install that has ever recorded a print keeps the table it
    already has, and the ALTER is the only thing that gives it the column —
    the same shape the determined_by migration already follows.  Its old
    rows read NULL, which is honest: nothing knows what those prints
    tripped, and nothing should invent it.
    """
    import kiln.persistence as _p

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_PRE_COLUMN_SCHEMA)
    conn.execute(
        "INSERT INTO print_outcomes (job_id, printer_name, outcome, created_at) "
        "VALUES ('ancient', 'garage', 'failed', 1691000000.0)",
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("KILN_DB_PATH", str(db_path))
    monkeypatch.setattr(_p, "_db", None, raising=False)

    db = _p.get_db()

    assert db.get_print_outcome("ancient")["print_error"] is None

    # And the migrated table takes new codes — an ALTER that lands but
    # leaves writes broken would pass a column-existence check alone.
    db.save_print_outcome(
        {
            "job_id": "after-migration",
            "printer_name": "garage",
            "outcome": "cancelled",
            "print_error": _Z_HOMING_FAILED,
        }
    )
    assert db.get_print_outcome("after-migration")["print_error"] == _Z_HOMING_FAILED


def test_the_migration_is_idempotent(tmp_path, monkeypatch):
    """It runs on every startup, against a table that already has it."""
    import kiln.persistence as _p

    db_path = tmp_path / "twice.db"
    for _ in range(2):
        monkeypatch.setenv("KILN_DB_PATH", str(db_path))
        monkeypatch.setattr(_p, "_db", None, raising=False)
        _p.get_db().save_print_outcome(
            {
                "job_id": f"job-{_}",
                "printer_name": "garage",
                "outcome": "failed",
                "print_error": _Z_HOMING_FAILED,
            }
        )

    assert _p.get_db().get_print_outcome("job-0")["print_error"] == _Z_HOMING_FAILED
