"""The two speed doors a served tool may ask this computer to run.

``set_speed_profile(percent=)`` is the feedrate override every FDM firmware
has, with a Bambu's presets standing in where that is all the firmware
takes; ``set_speed_profile(profile=)`` names a preset outright.
``run_speed_schedule`` starts, reports and stops the layer-speed runner for
one printer.  Neither judges a speed: that is the served tools' part.
"""

from __future__ import annotations

import time

import pytest

from kiln import server
from kiln.printers.base import CommandVerdict, JobProgress, PrinterState, PrinterStatus


class _Printer:
    name = "fake"

    def __init__(self, state=PrinterStatus.PRINTING):
        self.state = state
        self.sent: list[str] = []
        self.presets: list[str] = []

    def get_state(self):
        return PrinterState(state=self.state, connected=True, tool_temp_actual=200.0,
                            tool_temp_target=200.0, bed_temp_actual=60.0, bed_temp_target=60.0)

    def get_job(self):
        return JobProgress(file_name="part.gcode", completion=10.0, current_layer=3, total_layers=50, job_id="j1")

    def send_gcode(self, commands):
        self.sent.extend(commands)
        return CommandVerdict.accepted_only("sent")


class BambuPrinter(_Printer):
    def set_speed_profile(self, profile):
        self.presets.append(profile)
        return CommandVerdict.accepted_only("set")


@pytest.fixture
def printer(monkeypatch):
    p = _Printer()
    monkeypatch.setattr(server, "_resolve_control_target", lambda name: (p, name or "default"))
    monkeypatch.setattr(server, "_check_auth", lambda scope: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
    monkeypatch.setattr(server, "_speed_schedule_runs", {})
    return p


class TestSetSpeedProfile:
    def test_a_percentage_goes_out_as_the_feedrate_command(self, printer):
        answer = server.set_speed_profile(percent=115, printer_name="voron")
        assert answer["success"] is True and answer["percent"] == 115
        assert printer.sent == ["M220 S115"] and "preset" not in answer

    def test_a_bambu_takes_the_nearest_preset_and_says_so(self, monkeypatch):
        p = BambuPrinter()
        monkeypatch.setattr(server, "_resolve_control_target", lambda name: (p, "default"))
        monkeypatch.setattr(server, "_check_auth", lambda scope: None)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        answer = server.set_speed_profile(percent=115)
        assert p.presets == ["sport"] and p.sent == []
        assert answer["preset"] == "sport" and answer["percent_actual"] == 124

    @pytest.mark.parametrize("percent", [5, 400, "fast", True])
    def test_out_of_bounds_is_refused_before_the_printer_is_reached(self, printer, percent):
        answer = server.set_speed_profile(percent=percent)
        assert answer["success"] is False and answer["error"]["code"] == "VALIDATION_ERROR"
        assert printer.sent == []

    def test_a_preset_by_name_still_works(self, monkeypatch):
        p = BambuPrinter()
        monkeypatch.setattr(server, "_resolve_adapter", lambda name: p)
        monkeypatch.setattr(server, "_check_auth", lambda scope: None)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        answer = server.set_speed_profile(profile="sport")
        assert p.presets == ["sport"] and answer["profile"] == "sport" and "percent" not in answer

    def test_a_preset_on_a_printer_without_presets_points_at_percent(self, printer, monkeypatch):
        monkeypatch.setattr(server, "_resolve_adapter", lambda name: printer)
        answer = server.set_speed_profile(profile="sport")
        assert answer["error"]["code"] == "UNSUPPORTED" and "percent" in answer["error"]["message"]

    @pytest.mark.parametrize("kwargs", [{}, {"profile": "sport", "percent": 115}, {"profile": ""}])
    def test_exactly_one_of_profile_or_percent(self, printer, kwargs):
        answer = server.set_speed_profile(**kwargs)
        assert answer["error"]["code"] == "VALIDATION_ERROR" and printer.sent == []


class TestRunSpeedSchedule:
    SCHEDULE = [{"from_layer": 1, "to_layer": 5, "speed_percent": 100}, {"from_layer": 6, "to_layer": 9, "speed_percent": 140}]

    def test_run_status_stop(self, printer):
        started = server.run_speed_schedule(self.SCHEDULE, printer_name="voron")
        assert started["success"] is True and started["active"] is True
        time.sleep(0.05)
        status = server.run_speed_schedule(printer_name="voron", action="status")
        assert status["active"] is True and status["applied"] == [{"layer": 3, "speed_percent": 100}]
        stopped = server.run_speed_schedule(printer_name="voron", action="stop")
        assert stopped["active"] is False and "last speed" in stopped["message"]
        assert server.run_speed_schedule(printer_name="voron", action="status")["active"] is False
        assert printer.sent == ["M220 S100"]

    def test_a_new_schedule_replaces_the_running_one(self, printer):
        server.run_speed_schedule(self.SCHEDULE, printer_name="voron")
        first = server._speed_schedule_runs["voron"]
        server.run_speed_schedule([{"from_layer": 1, "to_layer": 9, "speed_percent": 80}], printer_name="voron")
        assert not first.thread.is_alive() and server._speed_schedule_runs["voron"] is not first
        server.run_speed_schedule(printer_name="voron", action="stop")

    def test_a_malformed_schedule_or_an_idle_printer_is_refused(self, printer):
        bad = server.run_speed_schedule([{"from_layer": 5, "to_layer": 1, "speed_percent": 100}])
        assert bad["error"]["code"] == "VALIDATION_ERROR"
        printer.state = PrinterStatus.IDLE
        answer = server.run_speed_schedule(self.SCHEDULE)
        assert answer["error"]["code"] == "VALIDATION_ERROR" and "not printing" in answer["error"]["message"]
        assert server.run_speed_schedule(action="dance")["error"]["code"] == "VALIDATION_ERROR"
