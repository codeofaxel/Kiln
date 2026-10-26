"""The pause hold keeps the bed warm, never leaves a nozzle sitting hot, and
heats nothing it has any reason to doubt.

While a print is paused Kiln holds its temperatures (``server.
_PauseKeepAlive``).  Until 2026-10-25 the hold re-set BOTH heaters every two
minutes, for as long as the printer said PAUSED, and answered to nothing
else.  Two families of fault followed.

Doubt heated.  No stop path ended the hold, and it sent its targets straight
to the adapter, past the latch ``set_temperature`` honours, so a printer that
still read PAUSED after an emergency stop had both heaters switched back on
within two minutes.  A state read that failed logged "continuing" and heated
anyway.  And a heater a person switched off during the pause came back at
the next round.

The nozzle sat hot.  A hot nozzle over a part nobody is watching is the risk
in a pause, and the hold fought the printers that lower it themselves (an A1
sets about 90 °C at once) and kept it at full heat on the ones that do not.
Now the bed alone is held; the nozzle is left where the printer put it, or
switched off when the printer would keep it hot; and a resume heats it back
and waits on the reading, so it never pushes plastic through a cold nozzle.

The A tests fail at 9a887870 (public main before this change); the B tests
fail at 1afcc9f4 (the doubt fixes alone).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln import emergency, server
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A fresh registry, hold, latch store and database per test."""
    import kiln.persistence as persistence
    import kiln.registry as registry_module

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None)
    registry = PrinterRegistry()
    monkeypatch.setattr(server, "_registry", registry)
    monkeypatch.setattr(registry_module, "get_printer_registry", lambda: registry)
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_pause_keepalive", server._PauseKeepAlive())
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(emergency, "_coordinator", None)
    # Long enough that only the first round runs on its own; every later
    # round in these tests is driven by hand, so nothing races the asserts.
    monkeypatch.setattr(server, "_PAUSE_KEEPALIVE_INTERVAL_S", 3600.0)
    monkeypatch.setattr(server, "_REHEAT_POLL_S", 0.0, raising=False)
    yield
    hold = server._pause_keepalive
    with hold._lock:
        entries = list(hold._entries.values())
        hold._entries.clear()
    for entry in entries:
        entry["stop_event"].set()
        entry["thread"].join(timeout=2.0)


class _Printer(PrinterAdapter):
    """A printer whose readings answer to what it was last told.

    A real adapter subclass, so a resume runs the template the reheat lives
    in.  Setting a target changes what the next reading reports.  While
    *heats*, each reading moves the nozzle a few degrees toward its target.
    *standby* is the nozzle target its own firmware sets on pause (an A1's
    is about 90 °C), or ``None`` for a printer that keeps it hot.  Its
    emergency stop leaves it reading PAUSED: the shape of a stop the printer
    did not confirm.
    """

    def __init__(self, host: str, *, standby: float | None = None, can_set_temp: bool = True) -> None:
        self.host = host
        self.serial = ""
        self.status = PrinterStatus.PRINTING
        self.tool_target = 220.0
        self.tool_actual = 220.0
        self.bed_target = 60.0
        self.standby = standby
        self.heats = True
        self.readable = True
        self.can_set_temp = can_set_temp
        self.sent: list[tuple[str, float]] = []
        self.events: list[str] = []
        self.hold_running_at_stop: bool | None = None

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(can_set_temp=self.can_set_temp)

    def get_state(self) -> PrinterState:
        if not self.readable:
            raise ConnectionError("printer did not answer")
        if self.heats and self.tool_actual < self.tool_target:
            self.tool_actual = min(self.tool_target, self.tool_actual + 5.0)
        return PrinterState(
            state=self.status,
            connected=True,
            tool_temp_actual=self.tool_actual,
            tool_temp_target=self.tool_target,
            bed_temp_actual=self.bed_target,
            bed_temp_target=self.bed_target,
        )

    def pause_print(self) -> PrintResult:
        self.status = PrinterStatus.PAUSED
        if self.standby is not None:
            self.tool_target = self.standby
        return PrintResult(success=True, message="paused")

    def _resume_print_impl(self) -> PrintResult:
        self.events.append(f"resume at {self.tool_actual:.0f}")
        self.status = PrinterStatus.PRINTING
        return PrintResult(success=True, message="resumed")

    def set_tool_temp(self, target: float) -> bool:
        if not self.can_set_temp:
            raise RuntimeError("this printer cannot set temperatures")
        self.sent.append(("tool", target))
        self.tool_target = target
        return True

    def set_bed_temp(self, target: float) -> bool:
        if not self.can_set_temp:
            raise RuntimeError("this printer cannot set temperatures")
        self.sent.append(("bed", target))
        self.bed_target = target
        return True

    def emergency_stop(self) -> PrintResult:
        self.hold_running_at_stop = server._pause_keepalive.is_running(self)
        return PrintResult(success=True, message="stopped")

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return []

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=file_path, message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        self.status = PrinterStatus.IDLE
        return PrintResult(success=True, message="cancelled")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


def _bench(**kwargs) -> tuple[_Printer, _Printer]:
    garage = _Printer("192.0.2.10", **kwargs)
    workshop = _Printer("192.0.2.11", **kwargs)
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    return garage, workshop


def _pause(printer: _Printer, name: str, **kwargs) -> dict:
    """Pause through the tool and wait for the hold's first round to land."""
    out = server.pause_print(printer_name=name, **kwargs)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not printer.sent:
        time.sleep(0.01)
    assert printer.sent == [("bed", 60.0)], "the first round holds the bed, and only the bed"
    printer.sent.clear()
    return out


def _round(printer: _Printer) -> bool:
    """Run one hold round on *printer* now, as the timer would."""
    hold = server._pause_keepalive
    key = hold._key(printer)
    with hold._lock:
        entry = hold._entries.get(key)
    if entry is None:
        return False
    return hold._reassert(key, entry["stop_event"])


def _age_the_pause(printer: _Printer) -> None:
    """Make the pause old enough for the nozzle to be decided."""
    hold = server._pause_keepalive
    with hold._lock:
        entry = hold._entries[hold._key(printer)]
        entry["paused_at"] = entry.get("paused_at", time.monotonic()) - getattr(server, "_NOZZLE_OFF_AFTER_S", 45.0)


# ---------------------------------------------------------------------------
# A. Emergency stop
# ---------------------------------------------------------------------------


def test_an_emergency_stop_ends_the_hold_before_the_stop_goes_out():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    out = server.emergency_stop(printer_name="workshop")

    assert out["success"] is True
    assert workshop.hold_running_at_stop is False
    assert not server._pause_keepalive.is_running(workshop)
    assert _round(workshop) is False
    assert workshop.sent == []


def test_an_emergency_stop_on_one_machine_leaves_the_other_held():
    """The guard against over-reach: passes before and after the change."""
    garage, workshop = _bench()
    _pause(garage, "garage")
    _pause(workshop, "workshop")

    server.emergency_stop(printer_name="workshop")

    assert server._pause_keepalive.is_running(garage)
    assert _round(garage) is True
    assert ("bed", 60.0) in garage.sent


def test_a_latch_the_hold_never_heard_about_still_ends_it():
    """A latch set elsewhere (another Kiln process, loaded from the database
    after a restart) is found by the hold itself, under either of the names
    one machine answers to."""
    _garage, workshop = _bench()
    server._get_registry().register("default", workshop)
    _pause(workshop, "workshop")

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

    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    watchdog = PrintWatchdog.__new__(PrintWatchdog)
    watchdog._adapter = workshop

    watchdog._command_stop()

    assert workshop.hold_running_at_stop is False
    assert _round(workshop) is False
    assert workshop.sent == []


# ---------------------------------------------------------------------------
# A. An unreadable printer
# ---------------------------------------------------------------------------


def test_a_printer_kiln_cannot_read_is_not_heated_that_round():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    workshop.readable = False

    assert _round(workshop) is True, "a dropped reading must not end the hold"
    assert workshop.sent == []

    workshop.readable = True
    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]


# ---------------------------------------------------------------------------
# A. A target a person set
# ---------------------------------------------------------------------------


def test_a_bed_switched_off_with_set_temperature_stays_off():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    out = server.set_temperature(bed_temp=0, printer_name="workshop")
    workshop.sent.clear()

    _round(workshop)
    assert workshop.sent == [], "the bed came back on"
    assert out["success"] is True
    assert out["pause_hold"]["bed_target"] == 0.0


def test_a_new_bed_target_set_during_a_pause_is_the_one_held():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    server.set_temperature(bed_temp=70, printer_name="workshop")
    workshop.sent.clear()

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 70.0)]


def test_a_bed_target_changed_outside_kiln_is_followed():
    """``kiln temp`` from another terminal, the printer's own screen, the
    maker's app: none of them can reach this process's hold, so the hold
    reads the printer's target back and follows a change it did not make."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    workshop.bed_target = 0.0  # switched off at the printer

    _round(workshop)
    assert workshop.sent == []


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


# ---------------------------------------------------------------------------
# B. The nozzle during a pause
# ---------------------------------------------------------------------------


def test_the_nozzle_is_not_touched_in_the_first_seconds_of_a_pause():
    """A quick look-and-resume must not cost a reheat."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]
    assert workshop.tool_target == 220.0


def test_a_printer_that_keeps_its_nozzle_hot_has_it_switched_off():
    """A printer whose pause leaves the nozzle at full heat for as long as it
    lasts has it switched off."""
    _garage, workshop = _bench()
    out = _pause(workshop, "workshop")
    assert out["keep_alive"]["nozzle"] == "watching"
    _age_the_pause(workshop)

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0), ("tool", 0.0)]
    assert server._pause_keepalive.describe(workshop)["nozzle"] == "off"

    workshop.sent.clear()
    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)], "switched off once, not every round"


def test_a_printer_that_lowers_its_own_nozzle_is_left_alone():
    """An A1 sets about 90 °C the moment it pauses.  That is the maker's
    call, and the hold used to fight it straight back to print heat."""
    _garage, workshop = _bench(standby=90.0)
    _pause(workshop, "workshop")
    _age_the_pause(workshop)

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]
    assert workshop.tool_target == 90.0
    assert server._pause_keepalive.describe(workshop)["nozzle"] == "printer"


def test_a_filament_pause_leaves_the_nozzle_to_the_person():
    """A runout or a clog: the person is about to load filament or do a cold
    pull, which needs the nozzle hot and under their hand."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop", leave_nozzle=True)
    _age_the_pause(workshop)

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]
    assert workshop.tool_target == 220.0


def test_a_nozzle_target_a_person_sets_during_the_pause_is_theirs():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    out = server.set_temperature(tool_temp=200, printer_name="workshop")
    assert out["success"] is True, out
    _age_the_pause(workshop)
    workshop.sent.clear()

    assert _round(workshop) is True
    assert workshop.sent == [("bed", 60.0)]
    assert workshop.tool_target == 200.0
    assert out["pause_hold"]["nozzle"] == "person"


def test_the_health_monitor_auto_pause_holds_the_bed_and_leaves_a_runout_nozzle():
    """The health monitor paused straight on the adapter, so its pauses got
    no hold at all.  A runout is filament work, so its nozzle is left."""
    from kiln.print_health_monitor import PrintHealthMonitor

    _garage, workshop = _bench()
    monitor = PrintHealthMonitor.__new__(PrintHealthMonitor)
    monitor._pause_kill_switch_enabled = lambda: False
    session = SimpleNamespace(session_id="s1", printer_name="workshop")

    monitor._honor_auto_pause(session, {}, "detect_failure_filament_runout")

    assert server._pause_keepalive.is_running(workshop)
    assert server._pause_keepalive.describe(workshop)["nozzle"] == "person"


# ---------------------------------------------------------------------------
# B. Resume heats the nozzle back and waits
# ---------------------------------------------------------------------------


def test_resume_heats_the_nozzle_back_and_waits_for_the_reading():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    _age_the_pause(workshop)
    _round(workshop)
    workshop.tool_actual = 40.0  # cooled while it waited

    out = server.resume_print(printer_name="workshop")

    assert out["success"] is True, out
    assert ("tool", 220.0) in workshop.sent
    assert workshop.events == ["resume at 220"], "resumed onto a nozzle that was not hot"


def test_resume_after_a_printer_standby_heats_to_the_print_not_the_standby():
    _garage, workshop = _bench(standby=90.0)
    _pause(workshop, "workshop")
    workshop.tool_actual = 95.0

    server.resume_print(printer_name="workshop")

    assert ("tool", 220.0) in workshop.sent
    assert workshop.events == ["resume at 220"]


def test_a_second_pause_does_not_make_the_standby_the_resume_target():
    """Pausing a machine that is already paused reads its standby, which is
    not the print's heat; the target resume heats to must not move."""
    _garage, workshop = _bench(standby=90.0)
    _pause(workshop, "workshop")
    server.pause_print(printer_name="workshop")
    workshop.tool_actual = 95.0

    server.resume_print(printer_name="workshop")

    assert workshop.events == ["resume at 220"]


def test_a_nozzle_still_hot_resumes_at_once():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")

    server.resume_print(printer_name="workshop")

    assert ("tool", 220.0) not in workshop.sent
    assert workshop.events == ["resume at 220"]


def test_a_nozzle_that_does_not_heat_is_switched_off_and_not_resumed(monkeypatch):
    monkeypatch.setattr(server, "_REHEAT_WATCH_PERIOD_S", 0.05, raising=False)
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    workshop.tool_actual = 40.0
    workshop.heats = False

    out = server.resume_print(printer_name="workshop")

    assert out["success"] is False
    assert out["code"] == "RESUME_REHEAT_FAILED"
    assert workshop.events == []
    assert workshop.sent[-1] == ("tool", 0.0)


def test_a_nozzle_kiln_cannot_read_is_not_resumed(monkeypatch):
    monkeypatch.setattr(server, "_REHEAT_WATCH_PERIOD_S", 0.05, raising=False)
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    workshop.readable = False

    out = server.resume_print(printer_name="workshop")

    assert out["success"] is False
    assert out["code"] == "RESUME_REHEAT_UNREAD"
    assert workshop.events == []


def test_an_emergency_stop_during_the_reheat_stops_the_resume():
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    workshop.tool_actual = 40.0
    coordinator = emergency.get_emergency_coordinator()

    def stop_while_heating(target: float) -> bool:
        with coordinator._lock:
            coordinator._stopped_printers.add("workshop")
        workshop.tool_target = target
        return True

    workshop.set_tool_temp = stop_while_heating
    out = workshop.resume_print()

    assert out.success is False
    assert out.code == "E_STOP_LATCHED"
    assert workshop.events == []
    assert workshop.tool_target == 0.0, "a stopped machine was left heating"


def test_a_stopped_machine_is_never_heated_by_a_resume_door_that_skips_the_latch():
    """``adapter.resume_print`` is reached by doors that ask no latch of their
    own (the CLI, a script); the reheat asks before it heats anything."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    workshop.tool_actual = 40.0
    coordinator = emergency.get_emergency_coordinator()
    with coordinator._lock:
        coordinator._stopped_printers.add("workshop")

    out = workshop.resume_print()

    assert out.success is False and out.code == "E_STOP_LATCHED"
    assert ("tool", 220.0) not in workshop.sent
    assert workshop.events == []


def test_a_fleet_resume_never_starts_heating_a_stopped_machine():
    garage, workshop = _bench()
    _pause(garage, "garage")
    _pause(workshop, "workshop")
    garage.tool_actual = workshop.tool_actual = 40.0
    coordinator = emergency.get_emergency_coordinator()
    with coordinator._lock:
        coordinator._stopped_printers.add("workshop")

    server._begin_resume_reheat(garage)
    server._begin_resume_reheat(workshop)

    assert ("tool", 220.0) in garage.sent
    assert ("tool", 220.0) not in workshop.sent


def test_a_nozzle_heated_ahead_for_a_resume_that_never_comes_is_switched_off_again():
    """The fleet's head start heats a nozzle before its resume is sent.  If
    that resume is refused or never sent, the hold switches the nozzle off
    on its own clock instead of leaving it hot over the part."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    _age_the_pause(workshop)
    _round(workshop)  # the printer kept it hot: switched off
    workshop.tool_actual = 40.0
    workshop.sent.clear()

    server._begin_resume_reheat(workshop)
    assert workshop.sent == [("tool", 220.0)]
    _age_the_pause(workshop)
    workshop.sent.clear()
    _round(workshop)

    assert ("tool", 0.0) in workshop.sent


def test_a_bed_held_past_the_limit_is_left_to_the_printer_and_resume_still_reheats():
    """A pause still going after an hour was forgotten: the hold stops
    sending the bed (it never switches it off), and the resume still heats
    the nozzle back first."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    hold = server._pause_keepalive
    with hold._lock:
        hold._entries[hold._key(workshop)]["held_since"] -= server._BED_HOLD_LIMIT_S
    _age_the_pause(workshop)

    assert _round(workshop) is True  # bed released; the kept-hot nozzle still switched off
    assert workshop.sent == [("tool", 0.0)]
    assert _round(workshop) is False
    assert not hold.is_running(workshop)

    workshop.tool_actual = 40.0
    server.resume_print(printer_name="workshop")
    assert ("tool", 220.0) in workshop.sent
    assert workshop.events == ["resume at 220"]


def test_no_nozzle_is_heated_ahead_once_its_hold_has_ended():
    """The fleet head start heats only a nozzle a running hold can switch
    back off; past the hold, the resume heats it under its own watch."""
    _garage, workshop = _bench()
    _pause(workshop, "workshop")
    hold = server._pause_keepalive
    with hold._lock:
        hold._entries[hold._key(workshop)]["held_since"] -= server._BED_HOLD_LIMIT_S
    _age_the_pause(workshop)
    _round(workshop)  # bed released, nozzle switched off
    _round(workshop)  # nothing left: the hold ends
    workshop.tool_actual = 40.0
    workshop.sent.clear()

    server._begin_resume_reheat(workshop)

    assert workshop.sent == []


def test_a_fault_auto_pause_holds_no_heat():
    """A health-monitor pause on a thermal fault (or any fault but a runout or
    a clog) must not keep a heater on.  Before the auto-pause went through the
    shared engine it held nothing; the engine's hold is not for faults."""
    from kiln.print_health_monitor import PrintHealthMonitor

    _garage, workshop = _bench()
    monitor = PrintHealthMonitor.__new__(PrintHealthMonitor)
    monitor._pause_kill_switch_enabled = lambda: False
    session = SimpleNamespace(session_id="s1", printer_name="workshop")

    monitor._honor_auto_pause(session, {}, "detect_failure_thermal_runaway")

    assert workshop.status is PrinterStatus.PAUSED
    assert not server._pause_keepalive.is_running(workshop)
    assert workshop.sent == []


def test_a_printer_kiln_cannot_set_temperatures_on_is_left_to_its_own_pause():
    """Prusa Link offers no way to set a temperature: no hold, no reheat, and
    no refused resume for want of one."""
    _garage, workshop = _bench(can_set_temp=False)

    out = server.pause_print(printer_name="workshop")
    workshop.tool_actual = 40.0
    workshop.heats = False
    resumed = server.resume_print(printer_name="workshop")

    assert "keep_alive" not in out
    assert not server._pause_keepalive.is_running(workshop)
    assert resumed["success"] is True
    assert workshop.events == ["resume at 40"]
