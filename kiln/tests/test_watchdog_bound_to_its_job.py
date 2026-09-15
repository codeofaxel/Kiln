"""A print watchdog belongs to the print it was armed for, and leaves when that print is gone.

Retirement used to wait for one thing: a status read seeing the print END.
Two cases never show one, and since the emergency stop behind the watchdog
is real, each left a watchdog in the wrong place, ready to stop a machine for
a print it was never armed for:

* the printer accepted the start and never took the job up -- no ending came,
  and the watchdog stayed to police whatever that machine ran next, a print
  started at its own screen included;
* Kiln lost sight of the printer, the print ended unseen and another began --
  contact came back on the new job with no ending in between.

The watchdog now binds to the job the printer reports under the name of the
file Kiln sent.  It retires itself, stopping nothing, when the printer takes
up no job within the never-active bound, or when -- after Kiln lost sight of
the bound job -- the printer is running one it can PROVE is different.
Anything short of proof keeps it watching, and still able to stop a fault.

The Bambu tests drive the real adapter's MQTT message handler, as
``test_watchdog_stop_and_retirement_together`` does, with the watchdog's own
clock stood in so that losing sight of a printer is one line, not two minutes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln import print_watchdog as pw
from kiln import server
from kiln.print_watchdog import PrintWatchdog
from kiln.printers import base
from kiln.printers import progress_motion as pm
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import JobProgress
from kiln.registry import PrinterRegistry

#: What a start sends a Bambu; the printer reports the job back as ``bracket``.
UPLOADED = "/sdcard/model/bracket.gcode.3mf"

#: Longer than the codebase's bound for "we were actually watching".
OUT_OF_SIGHT_S = pm.WATCHED_ENDING_MAX_GAP_S + 60.0

#: The wait for a print to begin.  The fallback lets the file collect against
#: the code before this change, where each test then fails on its own claim.
NEVER_ACTIVE_S = getattr(pw, "DEFAULT_NEVER_ACTIVE_TIMEOUT_S", pw.DEFAULT_WARMUP_TIMEOUT_S)


class _NullHeaterWatchdog:
    @staticmethod
    def notify_print_started() -> None:
        pass

    @staticmethod
    def notify_print_ended() -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """A process that has attached nothing yet, and polls nothing on its own."""
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
    monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", ())
    monkeypatch.setattr(server, "_print_lifecycle_hooks_installed", False)
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_adapter", None)
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
    # Attached, never polling: the tests drive step() themselves.
    monkeypatch.setattr(PrintWatchdog, "start", lambda self: None)
    pm.reset_progress_observations()
    yield
    pm.reset_progress_observations()


@pytest.fixture
def incidents(monkeypatch) -> list[dict[str, Any]]:
    from kiln import incident_recorder

    filed: list[dict[str, Any]] = []
    monkeypatch.setattr(incident_recorder, "record_incident", lambda **kw: filed.append(kw))
    return filed


class _Clock:
    """The watchdog's clock, moved by hand."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# A Bambu on the server
# ---------------------------------------------------------------------------


def _bambu(name: str, *, host: str = "192.0.2.71", serial: str = "01S00C000000071") -> BambuAdapter:
    printer = BambuAdapter(host=host, access_code="12345678", serial=serial, timeout=2)
    printer._mqtt_connected.set()
    printer._connected = True
    printer._mqtt_client = mock.MagicMock()
    delivered = mock.MagicMock()
    delivered.rc = mqtt.MQTT_ERR_SUCCESS
    printer._mqtt_client.publish.return_value = delivered
    printer._confirm_window_s = 0.0
    server._get_registry().register(name, printer)
    return printer


def _push(printer: BambuAdapter, **fields: Any) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    printer._on_message(printer._mqtt_client, None, msg)


def _reports(printer: BambuAdapter, state: str = "RUNNING", *, nozzle: float = 220.0, **job: Any) -> None:
    """One status frame: the run state, both heaters at a 220/60 target, and the job fields given.

    The printer's status is a MERGE, so a field left out keeps its last value.
    """
    _push(
        printer,
        gcode_state=state,
        print_error=0,
        nozzle_temper=nozzle,
        nozzle_target_temper=220,
        bed_temper=60,
        bed_target_temper=60,
        **job,
    )


def _idle(printer: BambuAdapter) -> None:
    _push(
        printer,
        gcode_state="IDLE",
        print_error=0,
        nozzle_temper=25,
        nozzle_target_temper=0,
        bed_temper=25,
        bed_target_temper=0,
    )


def _stops(printer: BambuAdapter) -> int:
    published = [json.loads(call.args[1]) for call in printer._mqtt_client.publish.call_args_list]
    return sum(1 for p in published if p.get("print", {}).get("command") == "stop")


def _armed(printer: BambuAdapter, clock: _Clock, file_name: str = UPLOADED) -> PrintWatchdog:
    """The watchdog the print-started hook files for *printer*, on *clock*."""
    server._spawn_print_watchdog(printer, file_name)
    watchdog = server._print_watchdogs[base.outcome_printer_name(printer)]
    watchdog._time = clock
    return watchdog


def _attached(printer: BambuAdapter, watchdog: PrintWatchdog) -> bool:
    filed = server._print_watchdogs.get(base.outcome_printer_name(printer))
    return filed is watchdog and not watchdog._stop_event.is_set()


# ---------------------------------------------------------------------------
# Bound to its print
# ---------------------------------------------------------------------------


def test_a_print_reported_under_its_files_name_is_watched_through_an_outage_and_bound_to_it(incidents):
    """Sent as ``/sdcard/model/bracket.gcode.3mf``, reported back as ``bracket``."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, subtask_name="bracket")
    assert watchdog.step() is None

    clock.advance(OUT_OF_SIGHT_S)  # Kiln loses sight of the printer...
    _reports(a1, subtask_name="bracket")  # ...and finds the same print running
    assert watchdog.step() is None
    assert _attached(a1, watchdog)

    clock.advance(OUT_OF_SIGHT_S)  # loses sight again...
    _reports(a1, subtask_name="gasket")  # ...and finds another print: it was bound to the bracket
    assert watchdog.step() is None

    assert not _attached(a1, watchdog)
    assert "a1" not in server._print_watchdogs
    assert _stops(a1) == 0
    assert incidents == []


def test_a_name_that_changes_while_kiln_watches_the_print_run_is_still_that_print(incidents):
    """A Bambu status is a merge of partial frames, so one print can arrive under two names."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, subtask_name="bracket")
    watchdog.step()

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    _reports(a1, gcode_file="Metadata/plate_1.gcode")  # the same print, now named for its plate
    watchdog.step()

    clock.advance(OUT_OF_SIGHT_S)
    _reports(a1)  # back in sight: still that print, still under its second name
    assert watchdog.step() is None
    assert _attached(a1, watchdog)

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    _reports(a1, nozzle=180.0)  # and it still guards that print
    flag = watchdog.step()

    assert flag is not None and flag.rule == "tool_drop"
    assert _stops(a1) == 1


@pytest.mark.parametrize(
    "other_print",
    [{"subtask_name": "gasket"}, {"subtask_name": "gasket", "task_id": "918273"}],
    ids=["started-at-the-printer", "started-from-the-cloud"],
)
def test_a_different_print_found_after_losing_sight_retires_the_watchdog_and_stops_nothing(
    other_print, incidents, caplog
):
    """The cloud case carries a real job id where Kiln's own LAN print had none: the
    two share only a name, so the name decides."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, subtask_name="bracket")  # at temperature: its heater watch has arrived
    watchdog.step()

    clock.advance(OUT_OF_SIGHT_S)
    # The bracket ended unseen, and someone else's print is still warming up.  To a
    # watchdog guarding the bracket, a hotend 40 degrees under target is a red flag.
    _reports(a1, nozzle=180.0, **other_print)
    with caplog.at_level(logging.WARNING, logger="kiln.print_watchdog"):
        assert watchdog.step() is None

    assert not _attached(a1, watchdog)
    assert _stops(a1) == 0
    assert incidents == []
    said = [r.getMessage() for r in caplog.records if "retired" in r.getMessage()]
    assert len(said) == 1, said
    assert "bracket" in said[0] and "gasket" in said[0]

    published = len(a1._mqtt_client.publish.call_args_list)
    assert watchdog.step() is None  # retired: it reads and commands nothing more
    assert len(a1._mqtt_client.publish.call_args_list) == published


@pytest.mark.parametrize(
    "unnamed",
    [{"subtask_name": "", "gcode_file": ""}, {"subtask_name": "", "gcode_file": "", "task_id": "918273"}],
    ids=["no-name", "an-id-and-no-name"],
)
def test_a_print_the_printer_does_not_name_keeps_the_watchdog_after_losing_sight(unnamed, incidents):
    """No name, or only an id against a bound name: not proof of anything."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, subtask_name="bracket")
    watchdog.step()

    clock.advance(OUT_OF_SIGHT_S)
    _reports(a1, **unnamed)
    assert watchdog.step() is None
    assert _attached(a1, watchdog)

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    _reports(a1, nozzle=180.0)
    flag = watchdog.step()

    assert flag is not None and flag.rule == "tool_drop"  # still on duty
    assert _stops(a1) == 1


def test_a_different_print_found_during_an_unconfirmed_stop_retires_the_watchdog_before_another_stop(
    incidents,
):
    """Commanding the stop again would stop somebody else's print."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, subtask_name="bracket")
    watchdog.step()
    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    _reports(a1, nozzle=180.0)
    flag = watchdog.step()  # the printer reports nothing after the stop
    assert flag is not None and flag.context["estop_confirmed"] is False
    assert _stops(a1) == 1 and len(incidents) == 1

    clock.advance(OUT_OF_SIGHT_S)
    _reports(a1, nozzle=180.0, subtask_name="gasket")  # the poll that would have stopped it again
    assert watchdog.step() is None

    assert _stops(a1) == 1
    assert len(incidents) == 1
    assert not _attached(a1, watchdog)


def test_each_printers_watchdog_is_bound_to_its_own_printers_print(incidents):
    a1 = _bambu("a1", host="192.0.2.71", serial="01S00C000000071")
    a2 = _bambu("a2", host="192.0.2.72", serial="01S00C000000072")
    clock1, clock2 = _Clock(), _Clock()
    watchdog1 = _armed(a1, clock1, "/sdcard/model/bracket.gcode.3mf")
    watchdog2 = _armed(a2, clock2, "/sdcard/model/gasket.gcode.3mf")
    _reports(a1, subtask_name="bracket")
    _reports(a2, subtask_name="gasket")
    watchdog1.step()
    watchdog2.step()

    clock1.advance(OUT_OF_SIGHT_S)
    clock2.advance(OUT_OF_SIGHT_S)
    # Both come back into sight running "gasket": a2's own print, and a different one on a1.
    _reports(a1, subtask_name="gasket")
    _reports(a2, subtask_name="gasket")
    watchdog1.step()
    watchdog2.step()

    assert not _attached(a1, watchdog1)
    assert _attached(a2, watchdog2)
    assert list(server._print_watchdogs) == ["a2"]

    clock2.advance(pw.DEFAULT_POLL_INTERVAL)
    _reports(a2, nozzle=180.0)
    flag = watchdog2.step()

    assert flag is not None and flag.rule == "tool_drop"
    assert (_stops(a1), _stops(a2)) == (0, 1)


# ---------------------------------------------------------------------------
# A print that never began
# ---------------------------------------------------------------------------


def test_a_print_the_printer_never_takes_up_retires_its_watchdog_after_the_bound(incidents):
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _idle(a1)
    watchdog.step()  # the wait for the print to begin starts here

    clock.advance(NEVER_ACTIVE_S - 1.0)
    _idle(a1)
    assert watchdog.step() is None
    assert _attached(a1, watchdog)  # not yet

    clock.advance(2.0)
    _idle(a1)
    assert watchdog.step() is None

    assert not _attached(a1, watchdog)
    assert _stops(a1) == 0
    assert incidents == []


def test_a_printer_still_preparing_the_print_keeps_its_watchdog_past_the_bound(incidents):
    """Preparing is taking the job up: the ending edge follows it, not this bound."""
    a1 = _bambu("a1")
    clock = _Clock()
    watchdog = _armed(a1, clock)
    _reports(a1, "PREPARE", subtask_name="bracket")
    watchdog.step()

    clock.advance(NEVER_ACTIVE_S + 60.0)
    _reports(a1, "PREPARE", subtask_name="bracket")
    assert watchdog.step() is None

    assert _attached(a1, watchdog)


# ---------------------------------------------------------------------------
# The watchdog on its own, with nothing between it and the printer
# ---------------------------------------------------------------------------


@dataclass
class _Reading:
    state: str = "printing"
    tool_temp_actual: float | None = 220.0
    tool_temp_target: float | None = 220.0
    bed_temp_actual: float | None = 60.0
    bed_temp_target: float | None = 60.0
    print_error: int | None = 0
    hms_code: str | None = None


class _Printer:
    """Answers whatever the test last set, and counts emergency stops."""

    def __init__(self) -> None:
        self.state = _Reading()
        self.job = JobProgress(file_name="bracket.gcode")
        self.stops = 0

    def get_state(self) -> _Reading:
        return self.state

    def get_job(self) -> JobProgress:
        return self.job

    def emergency_stop(self) -> bool:
        self.stops += 1
        return True


def _armed_directly(printer: _Printer, clock: _Clock, retired: list[PrintWatchdog]) -> PrintWatchdog:
    try:
        return PrintWatchdog(
            printer, time_fn=clock, started_file="bracket.gcode", on_retired=retired.append
        )
    except TypeError:
        # The code before this change arms nothing, so nothing ever retires.
        return PrintWatchdog(printer, time_fn=clock)


def test_seeing_the_printer_stop_is_losing_sight_of_the_print():
    """No ending edge reaches this watchdog; its own reading is the only witness."""
    printer = _Printer()
    clock = _Clock()
    retired: list[PrintWatchdog] = []
    watchdog = _armed_directly(printer, clock, retired)
    watchdog.step()  # bracket, printing: bound

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    printer.state = _Reading(state="idle", tool_temp_target=0.0, bed_temp_target=0.0)
    watchdog.step()

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    printer.state = _Reading(state="printing", tool_temp_actual=150.0)
    printer.job = JobProgress(file_name="gasket.gcode")
    assert watchdog.step() is None

    assert retired == [watchdog]
    assert watchdog._stop_event.is_set()
    assert printer.stops == 0


def test_a_printer_kiln_cannot_read_has_not_told_it_that_nothing_began():
    printer = _Printer()
    printer.state = _Reading(state="offline", tool_temp_actual=None, bed_temp_actual=None)
    clock = _Clock()
    retired: list[PrintWatchdog] = []
    watchdog = _armed_directly(printer, clock, retired)
    watchdog.step()

    clock.advance(NEVER_ACTIVE_S + 60.0)
    watchdog.step()  # still no word from the printer
    assert retired == []

    clock.advance(pw.DEFAULT_POLL_INTERVAL)
    printer.state = _Reading(state="idle", tool_temp_target=0.0, bed_temp_target=0.0)
    watchdog.step()  # the printer itself says nothing is running

    assert retired == [watchdog]
    assert printer.stops == 0


def test_a_watchdog_stopped_by_its_own_read_acts_on_nothing_that_read_returned():
    """The read that sees a print end is where the ending hook retires the watchdog."""

    class _EndsDuringTheRead(_Printer):
        def get_state(self) -> _Reading:
            watchdog.stop()  # what the print-ended hook does, inside this very read
            return _Reading(state="idle", tool_temp_target=0.0, bed_temp_target=0.0, hms_code="0300-8014")

    printer = _EndsDuringTheRead()
    watchdog = PrintWatchdog(printer, hms_blocklist=["0300-8014"], time_fn=_Clock())

    assert watchdog.step() is None
    assert printer.stops == 0


def test_a_watchdog_leaving_does_not_take_the_next_prints_watchdog_with_it():
    """A retirement names the watchdog that retired, not just the printer."""
    a1 = _bambu("a1")
    server._spawn_print_watchdog(a1, "/sdcard/model/bracket.gcode.3mf")
    earlier = server._print_watchdogs["a1"]
    server._spawn_print_watchdog(a1, "/sdcard/model/gasket.gcode.3mf")
    later = server._print_watchdogs["a1"]
    assert later is not earlier

    release = getattr(server, "_release_print_watchdog", None)
    assert release is not None, "no way for a watchdog that retires itself to leave"
    release("a1", earlier)
    assert server._print_watchdogs.get("a1") is later

    release("a1", later)
    assert "a1" not in server._print_watchdogs
