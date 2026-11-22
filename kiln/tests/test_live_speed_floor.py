"""A live speed-up never asks the nozzle for more plastic than it can melt.

Pinned through the real adapter template on two files PrusaSlicer sliced
(``tests/data/live_speed``): ``near_limit.gcode`` prints every layer at the
15 mm³/s it was sliced to, ``headroom.gcode`` at about a third of that.  A
speed-up the file cannot bear is lowered at the adapter's own command
chokepoint, whichever door sent it; a slow-down is never touched; anything
the floor cannot read lets the command through and says it was not checked.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiln import live_speed_floor as floor
from kiln import server
from kiln import speed_schedule_runner as runner
from kiln.printers.base import CommandVerdict, JobProgress, PrinterStatus
from kiln.ran_as_sliced import NOT_AT_SLICED_SPEED
from tests.test_learned_print_time import _Adapter

DATA = Path(__file__).parent / "data" / "live_speed"
NEAR_LIMIT = DATA / "near_limit.gcode"
HEADROOM = DATA / "headroom.gcode"


class _Fdm(_Adapter):
    """A printer that takes the feedrate override, built on the real template."""

    def __init__(self, file_name: str = "part.gcode", layer: int | None = 1) -> None:
        super().__init__("voron")
        self.status = PrinterStatus.PRINTING
        self.job_label = file_name
        self.layer = layer
        self.sent: list[str] = []

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.job_label, completion=None, current_layer=self.layer)

    def send_gcode(self, commands):
        self.sent.extend(commands)
        return CommandVerdict.accepted_only("sent")


class BambuStyle(_Fdm):
    """A printer that takes only its four presets."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.presets: list[str] = []

    def set_speed_profile(self, profile):
        self.presets.append(profile)
        return CommandVerdict.accepted_only("set")


@pytest.fixture
def sent_by_kiln(tmp_path, monkeypatch):
    """A sandboxed home and database; ``send(path)`` makes *path* the file
    Kiln uploaded for the running print, ``part.gcode``."""
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    import kiln.learned_print_time as lpt
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    monkeypatch.setattr(lpt, "_hosted", lambda: False)
    monkeypatch.setattr(server, "_UPLOADED_FROM", {})

    def send(path: Path, name: str = "part.gcode") -> None:
        copy = tmp_path / name
        shutil.copy(path, copy)
        server._UPLOADED_FROM[name] = str(copy)

    return send


@pytest.fixture
def ceiling(monkeypatch):
    """``set(value)`` gives the printer a melt limit on its own safety record."""

    def set_ceiling(value: float) -> None:
        import kiln.safety_profiles as sp

        monkeypatch.setattr(
            sp, "get_profile",
            lambda printer_id, **_: SimpleNamespace(id="test_printer", max_volumetric_flow=value),
        )
        monkeypatch.setattr("kiln.printers.base.upload_scan_target", lambda adapter: "test_printer")

    return set_ceiling


def _doors(monkeypatch, printer) -> None:
    monkeypatch.setattr(server, "_resolve_control_target", lambda name: (printer, name or "default"))
    monkeypatch.setattr(server, "_resolve_adapter", lambda name: printer)
    monkeypatch.setattr(server, "_check_auth", lambda scope: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
    monkeypatch.setattr(server, "_speed_schedule_runs", {})


def _peak(path: Path, first: int = 1, last: int | None = None) -> float:
    layers = floor.flow_of_file(str(path)).layers
    return max(p for i, (_s, p) in enumerate(layers, start=1) if first <= i <= (last or len(layers)))


# ---------------------------------------------------------------------------
# What a file asks for
# ---------------------------------------------------------------------------


class TestWhatAFileAsksFor:
    def test_a_file_sliced_at_its_limit_reads_at_that_limit(self):
        flow = floor.flow_of_file(str(NEAR_LIMIT))
        assert flow.diameter == 1.75
        assert [round(p, 1) for _s, p in flow.layers] == [15.0] * 6
        assert min(v for v, _what in flow.stated_limits) == 15.0

    def test_a_slower_slice_reads_well_under_it(self):
        assert 3.0 < _peak(HEADROOM) < 6.0


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


class TestPercentDoor:
    def test_300_percent_on_a_file_at_its_limit_stays_at_the_sliced_speed(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(NEAR_LIMIT)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=300)
        assert printer.sent == ["M220 S100"]
        assert answer["percent"] == 300 and answer["percent_actual"] == 100
        assert "Kept at 100%, not 300%" in answer["message"]
        assert answer["speed_floor"][0]["checked"] is True and answer["speed_floor"][0]["lowered"] is True

    def test_a_speed_up_past_the_printers_own_limit_is_lowered_to_the_highest_that_fits(
        self, sent_by_kiln, ceiling, monkeypatch,
    ):
        sent_by_kiln(HEADROOM)
        ceiling(11.0)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=300)
        allowed = math.floor(100 * 11.0 / _peak(HEADROOM))
        assert 100 < allowed < 300
        assert printer.sent == [f"M220 S{allowed}"]
        assert f"Set to {allowed}%, not 300%" in answer["message"]
        assert "11 mm³/s" in answer["message"]

    def test_a_speed_up_the_file_can_bear_goes_out_as_asked(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(HEADROOM)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=300)
        assert printer.sent == ["M220 S300"]
        assert "percent_actual" not in answer and "not 300%" not in answer["message"]

    def test_a_slow_down_is_never_judged(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(NEAR_LIMIT)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=50)
        assert printer.sent == ["M220 S50"]
        assert "speed_floor" not in answer

    def test_every_change_says_the_time_no_longer_follows_the_slicer_once(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(HEADROOM)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        for percent in (50, 150):
            message = server.set_speed_profile(percent=percent)["message"]
            assert message.count(NOT_AT_SLICED_SPEED) == 1
            assert message.count(floor.SLICE_IT_NEXT_TIME) == 1

    def test_a_file_kiln_did_not_send_is_let_through_and_said_unchecked(self, sent_by_kiln, monkeypatch):
        printer = _Fdm(file_name="from_the_printers_screen.gcode")
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=300)
        assert printer.sent == ["M220 S300"]
        assert "was set without checking" in answer["message"]
        assert floor.NOT_SENT_BY_KILN in answer["message"]
        assert answer["speed_floor"][0]["checked"] is False

    def test_an_unknown_limit_is_let_through_and_said_unchecked(self, sent_by_kiln, tmp_path, monkeypatch):
        bare = tmp_path / "bare.gcode"
        bare.write_text("".join(
            line for line in NEAR_LIMIT.read_text().splitlines(keepends=True) if "max_volumetric_speed" not in line
        ))
        sent_by_kiln(bare)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=300)
        assert printer.sent == ["M220 S300"]
        assert floor.NO_LIMIT in answer["message"]


class TestPresetDoor:
    def test_ludicrous_on_a_file_at_its_limit_is_set_as_standard(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(NEAR_LIMIT)
        printer = BambuStyle()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(profile="ludicrous")
        assert printer.presets == ["standard"]
        assert answer["profile"] == "standard" and answer["profile_requested"] == "ludicrous"
        assert "Kept at standard (100%), not ludicrous (166%)" in answer["message"]

    def test_the_highest_preset_that_fits_is_set_and_its_acceleration_is_said(
        self, sent_by_kiln, ceiling, monkeypatch,
    ):
        sent_by_kiln(HEADROOM)
        ceiling(7.5)  # sport fits, ludicrous does not
        printer = BambuStyle()
        _doors(monkeypatch, printer)
        assert 124 * _peak(HEADROOM) / 100 <= 7.5 < 166 * _peak(HEADROOM) / 100
        answer = server.set_speed_profile(profile="ludicrous")
        assert printer.presets == ["sport"]
        assert "Set to sport (124%), not ludicrous (166%)" in answer["message"]
        assert floor.ACCELERATION_UNBOUNDED in answer["message"]

    def test_a_percentage_on_a_preset_printer_lands_on_a_preset_that_fits(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(NEAR_LIMIT)
        printer = BambuStyle()
        _doors(monkeypatch, printer)
        answer = server.set_speed_profile(percent=170)
        assert printer.presets == ["standard"] and answer["percent_actual"] == 100


class TestRawCommandDoors:
    def test_the_adapter_lowers_a_raw_override_whoever_sends_it(self, sent_by_kiln):
        sent_by_kiln(NEAR_LIMIT)
        printer = _Fdm()
        with floor.collect() as found:
            printer.send_gcode(["M220 S250 ; faster", "G1 X1"])
        assert printer.sent == ["M220 S100 ; faster", "G1 X1"]
        assert found and found[0].lowered

    def test_send_gcode_reports_what_it_really_sent(self, sent_by_kiln, monkeypatch):
        sent_by_kiln(NEAR_LIMIT)
        printer = _Fdm()
        monkeypatch.setattr(server, "_check_auth", lambda scope: None)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        monkeypatch.setattr(server, "_check_confirmation", lambda name, args: None)
        monkeypatch.setattr(server, "_emergency_latch_error", lambda name, printer: None)
        monkeypatch.setattr(server, "_get_adapter", lambda: printer)
        answer = server.send_gcode("M220 S300")
        assert printer.sent == ["M220 S100"] and answer["commands_sent"] == ["M220 S100"]
        assert "Kept at 100%, not 300%" in answer["message"]
        assert NOT_AT_SLICED_SPEED in answer["message"]

    def test_the_terminal_gcode_command_says_it_too(self, sent_by_kiln, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli import main as cli_main

        sent_by_kiln(NEAR_LIMIT)
        printer = _Fdm()
        monkeypatch.setattr(cli_main, "_get_adapter_from_ctx", lambda ctx: printer)
        monkeypatch.setattr(cli_main, "_saved_printer_model", lambda name: None)
        result = CliRunner().invoke(cli_main.cli, ["gcode", "M220 S300", "--json"])
        assert printer.sent == ["M220 S100"]
        assert "Kept at 100%, not 300%" in result.output


class TestTerminalSpeedCommand:
    def test_kiln_speed_sets_the_preset_that_fits_and_says_why(self, sent_by_kiln, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli import main as cli_main

        sent_by_kiln(NEAR_LIMIT)
        printer = BambuStyle()
        monkeypatch.setattr(cli_main, "_get_adapter_from_ctx", lambda ctx: printer)
        result = CliRunner().invoke(cli_main.cli, ["speed", "ludicrous"])
        assert printer.presets == ["standard"]
        assert "Speed profile 'standard'" in result.output
        assert "not ludicrous (166%)" in result.output


class TestSchedule:
    def test_each_segment_is_judged_on_its_own_layers(self, sent_by_kiln, ceiling, monkeypatch):
        sent_by_kiln(HEADROOM)
        ceiling(11.0)
        printer = _Fdm()
        _doors(monkeypatch, printer)
        first = _peak(HEADROOM, 1, 1)
        assert 300 * first / 100 <= 11.0  # the first layer bears 300%
        answer = server.run_speed_schedule(schedule=[
            {"from_layer": 1, "to_layer": 1, "speed_percent": 300},
            {"from_layer": 2, "to_layer": 6, "speed_percent": 300},
        ])
        try:
            speeds = [s["speed_percent"] for s in answer["segments"]]
            assert speeds == [300, math.floor(100 * 11.0 / _peak(HEADROOM, 2, 6))]
            assert "not 300%" in answer["message"] and NOT_AT_SLICED_SPEED in answer["message"]
        finally:
            server.run_speed_schedule(action="stop")

    def test_the_runner_sends_a_segments_speed_judged_on_that_segment_alone(self, sent_by_kiln, ceiling):
        sent_by_kiln(HEADROOM)
        ceiling(11.0)
        printer = _Fdm(layer=1)
        run = runner.RunState(
            segments=(runner.Segment(1, 1, 300),), printer_name="voron", watching=None,
        )
        assert runner._tick(run, printer) is True
        assert printer.sent == ["M220 S300"]  # layer 1 alone bears it; the rest of the print would not


class TestWiring:
    def test_every_printer_backend_meets_the_floor(self):
        import kiln.printers as printers

        backends = [
            getattr(printers, name) for name in printers.__all__
            if isinstance(getattr(printers, name, None), type) and name.endswith("Adapter") and name != "PrinterAdapter"
        ]
        assert backends
        for cls in backends:
            for action in ("send_gcode", "set_speed_profile"):
                method = getattr(cls, action, None)
                if method is None or getattr(method, "__isabstractmethod__", False):
                    continue
                assert getattr(method, "_kiln_speed_floor_wrapped", False), f"{cls.__name__}.{action}"
