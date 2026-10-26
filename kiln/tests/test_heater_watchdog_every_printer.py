"""Every machine Kiln heats has an idle-heat watchdog, and none cools a busy one.

Until 2026-10-26 one watchdog existed, bound to the default printer.  Heat
set on any other machine, or a cancel there that kept the temperatures, was
never timed, so a second printer could sit hot indefinitely.  And the
watchdog's tick trusted its print-started / print-ended notices instead of
the machine, so a missed notice could cool a running print.

Each test fails at the public main this branch was cut from.
"""

from __future__ import annotations

import time

import pytest

from kiln import server
from kiln.heater_watchdog import HeaterWatchdog
from kiln.printers.base import PrinterState, PrinterStatus
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import kiln.persistence as persistence

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None)
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_heater_watchdog", None)
    monkeypatch.setattr(server, "_other_heater_watchdogs", {}, raising=False)
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})


class _Printer:
    name = "fake"

    def __init__(self, host: str, status: PrinterStatus = PrinterStatus.IDLE) -> None:
        self.host, self.serial = host, ""
        self.status = status
        self.tool_target, self.bed_target = 0.0, 0.0
        self.sent: list[tuple[str, float]] = []

    def get_state(self) -> PrinterState:
        return PrinterState(
            state=self.status, connected=True,
            tool_temp_actual=self.tool_target, tool_temp_target=self.tool_target,
            bed_temp_actual=self.bed_target, bed_temp_target=self.bed_target,
        )

    def set_tool_temp(self, value: float) -> bool:
        self.sent.append(("tool", value))
        self.tool_target = value
        return True

    def set_bed_temp(self, value: float) -> bool:
        self.sent.append(("bed", value))
        self.bed_target = value
        return True

    def send_gcode(self, commands) -> bool:
        return True


def _bench(monkeypatch) -> tuple[_Printer, _Printer]:
    garage, workshop = _Printer("192.0.2.10"), _Printer("192.0.2.11")
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop


def _time_out(watchdog: HeaterWatchdog) -> None:
    with watchdog._lock:
        watchdog._last_heater_activity = time.monotonic() - watchdog._timeout_seconds - 1


def test_heat_set_on_a_second_printer_is_timed_and_cooled_there(monkeypatch):
    garage, workshop = _bench(monkeypatch)

    assert server.set_temperature(tool_temp=200, bed_temp=60, printer_name="workshop")["success"]
    watchdog = server._heater_watchdog_for(workshop)
    _time_out(watchdog)
    workshop.sent.clear()
    watchdog._tick()

    assert ("tool", 0) in workshop.sent and ("bed", 0) in workshop.sent
    assert garage.sent == []


def test_the_default_printer_keeps_its_own_watchdog(monkeypatch):
    garage, workshop = _bench(monkeypatch)

    assert server._heater_watchdog_for(garage) is server._get_heater_watchdog()
    assert server._heater_watchdog_for(workshop) is not server._get_heater_watchdog()


def test_a_busy_machine_is_never_cooled_whatever_the_notices_said():
    """No print-started notice reached it, and it is printing: hands off."""
    printer = _Printer("192.0.2.12", status=PrinterStatus.PRINTING)
    printer.tool_target, printer.bed_target = 210.0, 60.0
    watchdog = HeaterWatchdog(get_adapter=lambda: printer, timeout_minutes=0.0001)
    _time_out(watchdog)

    watchdog._tick()

    assert printer.sent == []


def test_a_paused_print_is_left_to_the_pause_hold():
    printer = _Printer("192.0.2.13", status=PrinterStatus.PAUSED)
    printer.bed_target = 60.0
    watchdog = HeaterWatchdog(get_adapter=lambda: printer, timeout_minutes=0.0001)
    _time_out(watchdog)

    watchdog._tick()

    assert printer.sent == []
