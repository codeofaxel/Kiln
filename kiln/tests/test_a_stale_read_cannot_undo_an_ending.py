"""A status read that was already in flight cannot undo the ending it missed.

Two doors write the outcome ledger's previous-state table from different
threads: a Bambu's push callback files each frame as it lands, and every
``get_state()`` -- the print watchdog, the monitors, the status tools --
files what it read.  A read that began just before the printer's idle frame
could be filed just after it, still saying "printing".  Taken as the latest
word, that stale reading looked like a new print starting on the machine:

* it cleared the cancel the ending was about to be classified by, so a print
  the user cancelled was recorded a success; and
* it re-armed the table, so the next ordinary read found the same ending
  again, announced it a second time and recorded a second success under the
  polled door's job name.

Measured with a real Bambu adapter before the fix: two rows, both "success",
for one cancelled print.  Each reading now carries the time it was taken, and
one older than what the table holds changes nothing.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from kiln import auto_record_hook as hook
from kiln.printers import base
from kiln.printers import progress_motion as pm
from kiln.registry import PrinterRegistry


@pytest.fixture()
def rows(monkeypatch):
    """The outcome rows the hook records, instead of writing them."""
    import kiln.plugins.learning_tools as lt

    recorded: list[dict] = []
    monkeypatch.setattr(
        lt, "record_print_outcome", lambda **kw: recorded.append(kw) or {"success": True}
    )
    return recorded


def _workshop(monkeypatch):
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    adapter = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000000",
    )
    PrinterRegistry().register("workshop", adapter)
    return adapter


def _push(adapter, gcode_state: str, *, job: str) -> None:
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


def test_a_read_filed_after_the_ending_leaves_the_cancel_and_the_ending_alone(
    rows, monkeypatch
):
    workshop = _workshop(monkeypatch)
    _push(workshop, "RUNNING", job="spool-holder")
    _push(workshop, "RUNNING", job="spool-holder")  # the table now says running
    hook.register_cancel_intent("workshop")

    main = threading.current_thread()
    read_taken, ending_filed, read_filed = (threading.Event() for _ in range(3))
    endings: list[str] = []

    # The read sees the print still running, then is held until the ending
    # has been filed -- one real ordering of two threads, made certain.
    real_note_status_read = pm.note_status_read

    def note_status_read(adapter):
        if threading.current_thread() is not main:
            read_taken.set()
            ending_filed.wait(5)
        return real_note_status_read(adapter)

    monkeypatch.setattr(pm, "note_status_read", note_status_read)

    # The ending is held between being filed and being classified, which is
    # where the stale read used to land.
    def hold_the_ending(name: str) -> None:
        endings.append(name)
        if threading.current_thread() is main:
            ending_filed.set()
            read_filed.wait(5)

    monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", base._PRINT_ENDED_HOOKS)
    base.register_print_ended_hook(hold_the_ending)

    def status_read() -> None:
        workshop.get_state()
        read_filed.set()

    reader = threading.Thread(target=status_read)
    reader.start()
    assert read_taken.wait(5)
    _push(workshop, "IDLE", job="spool-holder")
    reader.join(5)
    assert read_filed.is_set()

    workshop.get_state()  # the next ordinary read, printer idle

    assert [(r["job_id"], r["outcome"]) for r in rows] == [("spool-holder", "cancelled")]
    assert endings == ["workshop"]


def test_an_older_reading_changes_nothing_and_a_newer_one_still_does():
    assert hook.observe_state("p", "running", read_at=1.0) is None
    hook.register_cancel_intent("p")
    assert hook.observe_state("p", "idle", read_at=3.0) == "running"

    # Taken before the ending was filed: no previous state, no clear.
    assert hook.observe_state("p", "printing", read_at=2.0) is None
    assert hook._HOOK_STATE.previous_state("p") == "idle"
    assert hook.cancel_intent_pending("p") is True

    # Taken after it: a new print really did start, and the rule that
    # retires a stale cancel on a new print still holds.
    assert hook.observe_state("p", "printing", read_at=4.0) == "idle"
    assert hook.cancel_intent_pending("p") is False
