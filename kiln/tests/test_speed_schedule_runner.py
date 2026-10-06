"""The schedule runner: pace the print the schedule was made for, and only it.

The runner decides no speed -- a schedule arrives checked -- and does the
one thing only the computer attached to the printer can: watch the layer and
set each segment's speed as the print reaches it.  These tests hold what it
must refuse (a malformed schedule, a printer with nothing printing), when it
must stop (the print ended, a different print appeared, it was asked to),
and that a backend naming no job is not read as a different one.
"""

from __future__ import annotations

import time

import pytest

from kiln import speed_schedule_runner as runner
from kiln.printers.base import CommandVerdict, JobProgress, PrinterState, PrinterStatus


class _Printer:
    def __init__(self, layers, *, state=PrinterStatus.PRINTING, job_id="job-1", file_name="part.gcode"):
        self.layers = list(layers)
        self.state = state
        self.job_id = job_id
        self.file_name = file_name
        self.sent: list[str] = []
        self.presets: list[str] = []
        self.refuse = False

    def get_state(self):
        return PrinterState(state=self.state, connected=True, tool_temp_actual=200.0,
                            tool_temp_target=200.0, bed_temp_actual=60.0, bed_temp_target=60.0)

    def get_job(self):
        layer = self.layers.pop(0) if len(self.layers) > 1 else self.layers[0]
        return JobProgress(file_name=self.file_name, completion=10.0, current_layer=layer,
                           total_layers=100, job_id=self.job_id, print_time_seconds=120)

    def send_gcode(self, commands):
        self.sent.extend(commands)
        return CommandVerdict.accepted_only("sent") if not self.refuse else CommandVerdict.refused("no")


class BambuPrinter(_Printer):
    def set_speed_profile(self, profile):
        self.presets.append(profile)
        return CommandVerdict.accepted_only("set")


SCHEDULE = [
    {"from_layer": 1, "to_layer": 5, "speed_percent": 100},
    {"from_layer": 6, "to_layer": 10, "speed_percent": 140},
    {"from_layer": 11, "to_layer": 20, "speed_percent": 80},
]


def _run_until_done(printer, schedule=SCHEDULE, **kw):
    run = runner.start(schedule, lambda: printer, poll_s=0.005, **kw)
    deadline = time.time() + 5
    while run.thread.is_alive() and time.time() < deadline:
        time.sleep(0.01)
    runner.stop(run)
    return run


class TestTheSchedule:
    @pytest.mark.parametrize(
        "schedule",
        [
            [], "fast", None,
            [{"from_layer": 1, "to_layer": 5}],
            [{"from_layer": 5, "to_layer": 1, "speed_percent": 100}],
            [{"from_layer": -1, "to_layer": 5, "speed_percent": 100}],
            [{"from_layer": 1, "to_layer": 5, "speed_percent": 5}],
            [{"from_layer": 1, "to_layer": 5, "speed_percent": 400}],
            [{"from_layer": 1, "to_layer": 5, "speed_percent": True}],
            [{"from_layer": 1, "to_layer": 5, "speed_percent": 100}, {"from_layer": 3, "to_layer": 8, "speed_percent": 100}],
            [{"from_layer": i, "to_layer": i, "speed_percent": 100} for i in range(65)],
        ],
    )
    def test_a_malformed_schedule_is_refused_before_anything_runs(self, schedule):
        with pytest.raises(runner.ScheduleRefused):
            runner.parse_segments(schedule)

    def test_segments_are_sorted(self):
        segs = runner.parse_segments([SCHEDULE[2], SCHEDULE[0], SCHEDULE[1]])
        assert [s.from_layer for s in segs] == [1, 6, 11]
        assert runner.segment_for(segs, 7) == 1 and runner.segment_for(segs, 25) is None


class TestTheRun:
    def test_each_segment_is_set_once_as_the_print_reaches_it(self):
        printer = _Printer([1, 1, 2, 6, 7, 11, 12, 12])  # the first read is the start's own
        run = runner.start(SCHEDULE, lambda: printer, poll_s=0.005)
        time.sleep(0.2)
        runner.stop(run)
        assert printer.sent == ["M220 S100", "M220 S140", "M220 S80"]
        assert [pct for _l, pct in run.applied] == [100, 140, 80]

    def test_a_bambu_takes_the_nearest_preset(self):
        printer = BambuPrinter([1, 1, 6, 11, 11])
        run = runner.start(SCHEDULE, lambda: printer, poll_s=0.005)
        time.sleep(0.15)
        runner.stop(run)
        assert printer.presets == ["standard", "sport", "standard"] and printer.sent == []  # 80% is nearer standard than silent

    def test_it_stops_when_the_print_ends(self):
        printer = _Printer([3, 4, 4])

        def resolve():
            if len(printer.layers) == 1:
                printer.state = PrinterStatus.IDLE
            return printer

        run = runner.start(SCHEDULE, resolve, poll_s=0.005)
        time.sleep(0.2)
        assert not run.thread.is_alive() and run.ended == "the print ended"

    def test_it_stops_the_moment_a_different_print_appears(self):
        printer = _Printer([3, 4, 5, 6, 7, 7])

        def resolve():
            if len(printer.layers) <= 3:
                printer.job_id = "job-2"
                printer.file_name = "other.gcode"
            return printer

        run = runner.start(SCHEDULE, resolve, poll_s=0.005)
        time.sleep(0.2)
        assert not run.thread.is_alive()
        assert "different print" in run.ended
        assert "M220 S140" not in printer.sent  # the other print was never paced

    def test_a_backend_naming_no_job_is_not_read_as_a_different_one(self):
        printer = _Printer([1, 1, 6, 6], job_id=None, file_name=None)
        run = runner.start(SCHEDULE, lambda: printer, poll_s=0.005)
        time.sleep(0.1)
        alive = run.thread.is_alive()
        runner.stop(run)
        assert alive and printer.sent == ["M220 S100", "M220 S140"]

    def test_it_refuses_a_printer_with_nothing_printing(self):
        with pytest.raises(runner.ScheduleRefused, match="not printing"):
            runner.start(SCHEDULE, lambda: _Printer([1, 1], state=PrinterStatus.IDLE))

    def test_a_refused_speed_is_said_and_the_run_goes_on(self):
        printer = _Printer([1, 6, 6])
        printer.refuse = True
        run = runner.start(SCHEDULE, lambda: printer, poll_s=0.005)
        time.sleep(0.1)
        runner.stop(run)
        assert run.error and "refused" in run.error and run.applied == []

    def test_stop_ends_the_thread_and_sends_nothing(self):
        printer = _Printer([3, 3, 3])
        run = runner.start(SCHEDULE, lambda: printer, poll_s=0.005)
        time.sleep(0.05)
        runner.stop(run)
        assert not run.thread.is_alive() and run.ended == "stopped"
        assert printer.sent == ["M220 S100"]  # the segment the print was already in
        assert run.to_dict()["running"] is False
