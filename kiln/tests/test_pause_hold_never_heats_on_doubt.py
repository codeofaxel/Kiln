"""The pause hold heats nothing it has any reason to doubt.

While a print is paused, Kiln re-sets the heater targets every two minutes
so the firmware's own pause cooldown cannot strand the part (``server.
_PauseKeepAlive``).  Until 2026-10-25 that hold answered to one question
only, "does the printer still say PAUSED?", and so it heated in three
situations where it had every reason not to:

* after an EMERGENCY STOP.  No stop path ended the hold, and the hold sends
  its targets straight to the adapter, past the latch ``set_temperature``
  honours.  A printer that still read PAUSED after its stop (a Bambu stop
  can come back unconfirmed) had both heaters switched back on within two
  minutes.
* when the printer could not be READ.  The state read failing logged
  "continuing" and heated anyway, so a machine Kiln could not see was heated
  on the strength of the last thing it said.
* after a PERSON changed a target.  ``set_temperature`` and ``kiln temp``
  never told the hold, so a nozzle switched off during a pause came back to
  print temperature at the next round, with the person told it was off.

Each test below fails at 9a887870 (public main before this change).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln import emergency, server
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A fresh registry, hold, latch store and database per test."""
    import kiln.persistence as persistence

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None)
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_pause_keepalive", server._PauseKeepAlive())
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(emergency, "_coordinator", None)
    # Long enough that only the first round runs on its own; every later
    # round in these tests is driven by hand, so nothing races the asserts.
    monkeypatch.setattr(server, "_PAUSE_KEEPALIVE_INTERVAL_S", 3600.0)
    yield
    hold = server._pause_keepalive
    with hold._lock:
        entries = list(hold._entries.values())
        hold._entries.clear()
    for entry in entries:
        entry["stop_event"].set()
        entry["thread"].join(timeout=2.0)


class _PausedPrinter:
    """A paused printer whose reported targets are the ones it was last given.

    Unlike a bare recorder, setting a target here CHANGES what the next
    reading reports, which is what a real machine does and what the hold
    reads back.  Its emergency stop leaves it reading PAUSED: the shape of a
    stop the printer did not confirm.
    """

    name = "fake"

    def __init__(self, host: str) -> None:
        self.host = host
        self.serial = ""
        self.tool_target = 220.0
        self.bed_target = 60.0
        self.sent: list[tuple[str, float]] = []
        self.readable = True
        self.hold_running_at_stop: bool | None = None

    def get_state(self) -> PrinterState:
        if not self.readable:
            raise ConnectionError("printer did not answer")
        return PrinterState(
            state=PrinterStatus.PAUSED,
            connected=True,
            tool_temp_actual=self.tool_target,
            tool_temp_target=self.tool_target,
            bed_temp_actual=self.bed_target,
            bed_temp_target=self.bed_target,
        )

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="paused")

    def set_tool_temp(self, value: float) -> bool:
        self.sent.append(("tool", value))
        self.tool_target = value
        return True

    def set_bed_temp(self, value: float) -> bool:
        self.sent.append(("bed", value))
        self.bed_target = value
        return True

    def emergency_stop(self) -> PrintResult:
        self.hold_running_at_stop = server._pause_keepalive.is_running(self)
        return PrintResult(success=True, message="stopped")

    def send_gcode(self, commands) -> None:
        pass


def _paused_bench() -> tuple[_PausedPrinter, _PausedPrinter]:
    garage = _PausedPrinter("192.0.2.10")
    workshop = _PausedPrinter("192.0.2.11")
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    return garage, workshop


def _pause_and_settle(printer: _PausedPrinter, name: str) -> None:
    """Pause through the tool and wait for the hold's first round to land."""
    server.pause_print(printer_name=name)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(printer.sent) < 2:
        time.sleep(0.01)
    assert sorted(printer.sent) == [("bed", 60.0), ("tool", 220.0)]
    printer.sent.clear()


def _round(printer: _PausedPrinter) -> bool:
    """Run one hold round on *printer* now, as the two-minute timer would."""
    hold = server._pause_keepalive
    key = hold._key(printer)
    with hold._lock:
        entry = hold._entries.get(key)
    if entry is None:
        return False
    return hold._reassert(key, entry["stop_event"])


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------


def test_an_emergency_stop_ends_the_hold_before_the_stop_goes_out():
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")

    out = server.emergency_stop(printer_name="workshop")

    assert out["success"] is True
    assert workshop.hold_running_at_stop is False
    assert not server._pause_keepalive.is_running(workshop)
    assert _round(workshop) is False
    assert workshop.sent == []


def test_an_emergency_stop_on_one_machine_leaves_the_other_held():
    """The guard against over-reach: passes before and after the change."""
    garage, workshop = _paused_bench()
    _pause_and_settle(garage, "garage")
    _pause_and_settle(workshop, "workshop")

    server.emergency_stop(printer_name="workshop")

    assert server._pause_keepalive.is_running(garage)
    assert _round(garage) is True
    assert sorted(garage.sent) == [("bed", 60.0), ("tool", 220.0)]


def test_a_latch_the_hold_never_heard_about_still_ends_it():
    """A latch set elsewhere (another Kiln process, loaded from the database
    after a restart) is found by the hold itself, under either of the names
    one machine answers to."""
    _garage, workshop = _paused_bench()
    server._get_registry().register("default", workshop)
    _pause_and_settle(workshop, "workshop")

    coordinator = emergency.get_emergency_coordinator()
    with coordinator._lock:
        coordinator._stopped_printers.add("default")

    assert _round(workshop) is False
    assert workshop.sent == []
    assert not server._pause_keepalive.is_running(workshop)


def test_the_print_watchdog_stop_ends_the_hold_too():
    """The watchdog sends its stop straight to the adapter, past the
    coordinator, so it is a second door and has to end the hold itself."""
    from kiln.print_watchdog import PrintWatchdog

    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")
    watchdog = PrintWatchdog.__new__(PrintWatchdog)
    watchdog._adapter = workshop

    watchdog._command_stop()

    assert workshop.hold_running_at_stop is False
    assert _round(workshop) is False
    assert workshop.sent == []


# ---------------------------------------------------------------------------
# An unreadable printer
# ---------------------------------------------------------------------------


def test_a_printer_kiln_cannot_read_is_not_heated_that_round():
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")
    workshop.readable = False

    assert _round(workshop) is True, "a dropped reading must not end the hold"
    assert workshop.sent == []

    workshop.readable = True
    assert _round(workshop) is True
    assert sorted(workshop.sent) == [("bed", 60.0), ("tool", 220.0)]


# ---------------------------------------------------------------------------
# A target a person set
# ---------------------------------------------------------------------------


def test_a_heater_switched_off_with_set_temperature_stays_off():
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")

    out = server.set_temperature(tool_temp=0, printer_name="workshop")
    workshop.sent.clear()

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)], "the nozzle came back on"
    assert out["success"] is True
    assert out["pause_hold"]["targets"] == {"tool": 0.0, "bed": 60.0}


def test_switching_both_heaters_off_ends_the_hold():
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")

    out = server.set_temperature(tool_temp=0, bed_temp=0, printer_name="workshop")
    workshop.sent.clear()

    assert _round(workshop) is False
    assert workshop.sent == []
    assert out["pause_hold"]["active"] is False


def test_a_new_target_set_during_a_pause_is_the_one_held():
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")

    server.set_temperature(bed_temp=70, printer_name="workshop")
    workshop.sent.clear()

    assert _round(workshop) is True
    assert sorted(workshop.sent) == [("bed", 70.0), ("tool", 220.0)]


def test_a_target_changed_outside_kiln_is_followed():
    """``kiln temp`` from another terminal, the printer's own screen, the
    maker's app: none of them can reach this process's hold, so the hold
    reads the printer's target back and follows a change it did not make."""
    _garage, workshop = _paused_bench()
    _pause_and_settle(workshop, "workshop")

    workshop.tool_target = 0.0  # switched off at the printer

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]
    workshop.sent.clear()
    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]


def test_kiln_temp_refuses_to_heat_an_emergency_stopped_machine():
    """The CLI set heaters straight on the adapter and never asked the latch
    ``set_temperature`` asks, so ``kiln temp --tool 200`` heated a stopped
    machine.  Turning a heater off stays allowed."""
    from kiln.cli.main import cli

    adapter = MagicMock()
    adapter.name = "mock"
    latched = MagicMock()
    latched.get_latch_status.return_value = {"latched": True}
    patches = (
        patch("kiln.cli.main._make_adapter", return_value=adapter),
        patch(
            "kiln.cli.main.load_printer_config",
            return_value={"type": "moonraker", "host": "http://test.local:7125"},
        ),
        patch("kiln.cli.main.validate_printer_config", return_value=(True, None)),
        patch("kiln.emergency.get_emergency_coordinator", return_value=latched),
    )
    runner = CliRunner()
    with patches[0], patches[1], patches[2], patches[3]:
        heat = runner.invoke(cli, ["temp", "--tool", "200", "--json"])
        cool = runner.invoke(cli, ["temp", "--tool", "0", "--json"])

    assert heat.exit_code == 1
    assert "Emergency latch is active" in heat.output
    adapter.set_tool_temp.assert_called_once_with(0.0)
    assert cool.exit_code == 0
