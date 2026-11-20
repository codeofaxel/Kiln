"""A printer's own pause and cancel moves, learned from the ones Kiln sends.

Pinned here: how a log begun while the printer was still printing is read
(:func:`kiln.bench.block_start`, :func:`kiln.bench.way_back`,
:func:`kiln.bench.logged_block`) -- the print's last lines left out, the
climb seen from where the head stopped, and nothing said by a log that
began late, that shows the head off the print first, or whose way back
was not seen; that an observation from real use never replaces one from a
guided session; and the watch itself, through a real adapter's own
``pause_print`` / ``resume_print`` / ``cancel_print``: what it keeps, that
a printer that cannot say where its head is is never polled, and that the
command's result is never touched.
"""

from __future__ import annotations

import json
import time

import pytest

from kiln import bench, head_moves, plate_state
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
)

FOOTPRINT = [100.0, 100.0, 140.0, 140.0]
LAYER = 1.0

#: The last lines of the print (filament going forward), the stop, the
#: climb in place, the travel to the park and the head at rest there.
PRINT = [(110.0, 110.0, LAYER, 10.0), (125.0, 110.0, LAYER, 10.5), (140.0, 110.0, LAYER, 11.0),
         (140.0, 125.0, LAYER, 11.5)]
STOP = [(140.0, 128.0, LAYER, 10.7)]
CLIMB = [(140.0, 128.0, 1.6, 10.7), (140.0, 128.0, 3.0, 10.7), (140.0, 128.0, 11.0, 10.7)]
PARK = [(60.0, 200.0, 11.0, 10.7), (0.0, 215.0, 11.0, 10.7)]
#: The way back after a resume: over the stop at the park height, down,
#: then the print again.
BACK = [(0.0, 215.0, 11.0, 10.7), (140.0, 128.0, 11.0, 10.7), (140.0, 128.0, LAYER, 10.7),
        (140.0, 128.0, LAYER, 11.5), (135.0, 128.0, LAYER, 11.8)]


def _split(readings):
    return [r[:3] for r in readings], [r[3] for r in readings]


class _Log:
    def __init__(self, readings, complete=True):
        self.points, self.extruder = _split(readings)
        self.complete = complete


# ---------------------------------------------------------------------------
# Reading a log that began mid-print
# ---------------------------------------------------------------------------


class TestWhereTheBlockBegins:
    def test_the_print_is_left_out_and_the_climb_is_seen_from_where_the_head_stopped(self):
        points, extruder = _split(PRINT + STOP + CLIMB + PARK)
        start, layer = bench.block_start(points, extruder, FOOTPRINT)
        assert points[start] == STOP[0][:3] and layer == LAYER

    def test_a_log_that_began_after_the_block_had_says_nothing(self):
        # The head already up and travelling: no filament went forward first,
        # so the log cannot show where the block began.
        points, extruder = _split(CLIMB[1:] + PARK)
        assert isinstance(bench.block_start(points, extruder, FOOTPRINT), str)

    def test_a_purge_at_the_park_is_never_taken_for_the_start(self):
        purge = [(0.0, 215.0, 11.0, 14.0)]
        points, extruder = _split(PRINT + STOP + CLIMB + PARK + purge)
        start, _layer = bench.block_start(points, extruder, FOOTPRINT)
        assert points[start] == STOP[0][:3]

    def test_a_purge_off_the_print_before_any_climb_says_nothing(self):
        # A lift too small to count, a travel off the print, a purge there:
        # the last push is not over the print, so the log cannot tell the
        # print from the block.
        low = [(140.0, 128.0, 1.5, 10.7), (-40.0, 128.0, 1.5, 10.7), (-40.0, 128.0, 1.5, 14.0)]
        points, extruder = _split(PRINT + STOP + low + CLIMB)
        assert isinstance(bench.block_start(points, extruder, FOOTPRINT), str)

    def test_a_head_already_climbing_when_the_log_began_never_raises_the_layer_claimed(self):
        # The stop's sample caught mid-lift: the layer claimed is that height,
        # so the climb counted from it can only be smaller than the real one.
        points, extruder = _split(PRINT + [(140.0, 128.0, 1.8, 10.7)] + CLIMB[1:] + PARK)
        start, layer = bench.block_start(points, extruder, FOOTPRINT)
        assert points[start][2] == 1.8 and layer == 1.8

    def test_a_log_with_no_extruder_readings_says_nothing(self):
        points = [r[:3] for r in PRINT + STOP + CLIMB]
        assert isinstance(bench.block_start(points, [None] * len(points), FOOTPRINT), str)


class TestTheWayBack:
    def test_the_way_back_ends_where_filament_goes_forward_again_at_the_layer(self):
        points, extruder = _split(BACK)
        assert bench.way_back(points, extruder, LAYER) == 3

    def test_a_prime_at_the_park_height_is_not_the_print_resuming(self):
        points, extruder = _split([(0.0, 215.0, 11.0, 10.7), (0.0, 215.0, 11.0, 11.5)] + BACK[1:])
        assert bench.way_back(points, extruder, LAYER) == 4

    def test_a_log_that_never_saw_the_print_resume_has_no_way_back(self):
        points, extruder = _split(BACK[:3])
        assert bench.way_back(points, extruder, LAYER) is None


class TestOneBlockFromItsLogs:
    def test_a_cancel_is_its_own_log_from_the_stop(self):
        points, layer = bench.logged_block([_Log(PRINT + STOP + CLIMB + PARK)], FOOTPRINT, returns=False)
        assert points[0] == STOP[0][:3] and points[-1] == PARK[-1][:3] and layer == LAYER

    def test_a_pause_is_written_only_with_its_way_back(self):
        assert bench.logged_block([_Log(PRINT + STOP + CLIMB + PARK)], FOOTPRINT, returns=True) == "the way back was not seen"
        points, _layer = bench.logged_block([_Log(PRINT + STOP + CLIMB + PARK), _Log(BACK)], FOOTPRINT, returns=True)
        assert points[0] == STOP[0][:3] and points[-1] == (140.0, 128.0, LAYER)

    def test_a_log_cut_short_or_with_no_footprint_writes_nothing(self):
        cut = bench.logged_block([_Log(PRINT + STOP + CLIMB, complete=False)], FOOTPRINT, returns=False)
        assert "cut short" in cut
        assert "footprint" in bench.logged_block([_Log(PRINT + STOP + CLIMB + PARK)], None, returns=False)


# ---------------------------------------------------------------------------
# Where it is kept
# ---------------------------------------------------------------------------


class _Machine:
    serial = "HEADMOVES0001"
    host = ""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    return tmp_path / "kiln-home"


class TestWhatIsKept:
    def _doc(self, how="position_log", layer=1.0):
        if how == "position_log":
            return bench.observation("creality_ender3", "pause", how, bed_mm=(220.0, 220.0), layer_z_mm=layer,
                                     points=[(140.0, 128.0, 1.0), (0.0, 215.0, 11.0)], returns=True)
        return bench.observation("creality_ender3", "pause", how, bed_mm=(220.0, 220.0), layer_z_mm=layer,
                                 zones=[24], lift_mm=10.0, returns=True, return_how="level")

    def test_real_use_never_replaces_what_the_owner_taught_in_a_session(self, home):
        m = _Machine()
        bench.keep_observation(m, self._doc("owner_zone"))
        assert bench.keep_observation(m, self._doc(), seen="real_use") is None
        assert bench.observations_of(m)[0]["how"] == "owner_zone"

    def test_real_use_replaces_real_use_and_a_session_replaces_both(self, home):
        m = _Machine()
        bench.keep_observation(m, self._doc(layer=1.0), seen="real_use")
        bench.keep_observation(m, self._doc(layer=2.0), seen="real_use")
        assert bench.observations_of(m)[0]["layer_z_mm"] == 2.0
        bench.keep_observation(m, self._doc("owner_zone"))
        assert bench.observations_of(m)[0]["how"] == "owner_zone"


# ---------------------------------------------------------------------------
# The watch, through a real adapter's own methods
# ---------------------------------------------------------------------------


class _Klipperish(PrinterAdapter):
    """A printer that says where its head is and the extruder, reading a
    script one reading per ask and then holding the last."""

    head_position = "head_and_extruder"
    _printer_model = "creality_ender3"

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def __init__(self, serial="HEADMOVES0001"):
        self.serial = serial
        self.host = ""
        self.state = PrinterStatus.PRINTING
        self.script: list[tuple[float, float, float, float]] = []
        self.asked = 0
        self.sent: list[str] = []

    @property
    def name(self):
        return "probe"

    @property
    def capabilities(self):
        return PrinterCapabilities()

    def declared_printer_model(self):
        return self._printer_model

    def get_state(self):
        return PrinterState(connected=True, state=self.state)

    def get_job(self):
        return JobProgress(file_name="bracket.gcode", print_time_seconds=60)

    def get_tool_position(self):
        self.asked += 1
        if not self.script:
            return None
        reading = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return {"x": reading[0], "y": reading[1], "z": reading[2], "e": reading[3]}

    def list_files(self):
        return []

    def upload_file(self, path):
        return None

    def delete_file(self, path):
        return True

    def _start_print_impl(self, file_name, **kwargs):
        return PrintResult(success=True, message="")

    def _resume_print_impl(self):
        self.sent.append("resume")
        self.state = PrinterStatus.PRINTING
        return PrintResult(success=True, message="resumed")

    def cancel_print(self):
        self.sent.append("cancel")
        self.state = PrinterStatus.IDLE
        return PrintResult(success=True, message="cancelled")

    def pause_print(self):
        self.sent.append("pause")
        self.state = PrinterStatus.PAUSED
        return PrintResult(success=True, message="paused")

    def emergency_stop(self):
        return PrintResult(success=True, message="")

    def send_gcode(self, commands):
        return True

    def set_bed_temp(self, target):
        return True

    def set_tool_temp(self, target):
        return True


class _Closed(_Klipperish):
    """A printer that cannot say where its head is (a Bambu)."""

    head_position = "this printer reports no head position"


@pytest.fixture
def watching(home, monkeypatch):
    from .conftest import serve_synthetic_roster_records

    # The Ender 3's record served with the bed these readings are laid out
    # on, whatever is installed here: its real record is kiln-pro's, so a
    # session without kiln-pro served a made-up 250 mm cube and one with it
    # the real bed, and the kept bed differed by machine.
    serve_synthetic_roster_records(monkeypatch, volumes={"ender3": (220.0, 220.0, 250.0)})
    monkeypatch.setenv(head_moves.TEST_OPT_IN_ENV, "1")
    monkeypatch.setattr(head_moves, "SETTLE_S", 0.15)
    monkeypatch.setattr(head_moves, "HZ", 200.0)
    head_moves._PAUSED.clear()
    head_moves._RUNNING.clear()
    yield
    head_moves._PAUSED.clear()
    head_moves._RUNNING.clear()


def _on_the_plate(adapter):
    plate_state.mark_occupied(adapter, plate_state.PlateJob(file="bracket.gcode", footprint_mm=list(FOOTPRINT),
                                                            max_z_mm=12.0, printer_id="creality_ender3"))


def _until(check, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    return None


def _kept(adapter, block):
    return next((d for d in bench.observations_of(adapter) if d["block"] == block), None)


class TestTheWatch:
    def test_a_cancel_kiln_sends_is_kept_from_where_the_head_stopped(self, watching):
        m = _Klipperish()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        result = m.cancel_print()
        assert result.success and result.message == "cancelled" and m.sent == ["cancel"]
        doc = _until(lambda: _kept(m, "cancel"))
        assert doc is not None and doc["how"] == "position_log" and doc["returns"] is False
        assert doc["points"][0] == list(STOP[0][:3]) and doc["points"][-1] == list(PARK[-1][:3])
        assert doc["layer_z_mm"] == LAYER and doc["model"] == "creality_ender3" and doc["bed_mm"] == [220.0, 220.0]
        store = json.loads((bench.bench_dir() / f"{bench.unit_of(m)}.json").read_text())
        assert store["seen"] == {"cancel": "real_use"}

    def test_a_pause_is_kept_with_its_way_back_once_kiln_sends_the_resume(self, watching):
        m = _Klipperish()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        assert m.pause_print().success
        assert _until(lambda: head_moves._PAUSED.get(bench.unit_of(m))) is not None
        assert _kept(m, "pause") is None              # not without the way back
        m.script = list(BACK)
        assert m.resume_print().success and m.sent == ["pause", "resume"]
        doc = _until(lambda: _kept(m, "pause"))
        assert doc is not None and doc["returns"] is True
        assert doc["points"][0] == list(STOP[0][:3]) and doc["points"][-1] == [140.0, 128.0, LAYER]

    def test_a_pause_resumed_elsewhere_then_cancelled_writes_no_pause(self, watching):
        m = _Klipperish()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        m.pause_print()
        assert _until(lambda: head_moves._PAUSED.get(bench.unit_of(m))) is not None
        m.script = list(PARK[-1:])                  # still at the park: nothing moves
        m.cancel_print()
        assert bench.unit_of(m) not in head_moves._PAUSED
        time.sleep(0.3)
        assert _kept(m, "pause") is None

    def test_a_print_kiln_did_not_start_teaches_nothing(self, watching):
        m = _Klipperish()
        m.script = list(PRINT + STOP + CLIMB + PARK)
        m.cancel_print()
        time.sleep(0.5)
        assert bench.observations_of(m) == []

    def test_a_printer_that_cannot_say_where_its_head_is_is_never_asked(self, watching):
        m = _Closed()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        assert m.pause_print().success and m.cancel_print().success
        time.sleep(0.2)
        assert m.asked == 0 and bench.observations_of(m) == []

    def test_a_refused_command_is_not_followed(self, watching):
        class _Refuses(_Klipperish):
            def cancel_print(self):
                return PrintResult(success=False, message="no")

        m = _Refuses()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        assert m.cancel_print().success is False
        time.sleep(0.3)
        assert bench.observations_of(m) == []

    def test_an_emergency_stop_that_is_a_cancel_is_never_watched(self, watching):
        """On a backend whose emergency stop is its cancel, nothing may sit in
        front of the stop: not even a position read."""

        class _StopsByCancelling(_Klipperish):
            def emergency_stop(self):
                return self.cancel_print()

        m = _StopsByCancelling()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        assert m.emergency_stop().success and m.sent == ["cancel"]
        time.sleep(0.2)
        assert m.asked == 0 and head_moves._RUNNING == {}

    def test_off_under_a_test_run_unless_asked_and_off_by_its_switch(self, home, monkeypatch):
        monkeypatch.delenv(head_moves.TEST_OPT_IN_ENV, raising=False)
        assert head_moves.enabled() is False
        monkeypatch.setenv(head_moves.TEST_OPT_IN_ENV, "1")
        assert head_moves.enabled() is True
        monkeypatch.setenv(head_moves.SWITCH_ENV, "0")
        assert head_moves.enabled() is False

    def test_a_command_that_raises_raises_untouched_and_drops_the_log(self, watching):
        class _Breaks(_Klipperish):
            def pause_print(self):
                raise RuntimeError("link lost")

        m = _Breaks()
        _on_the_plate(m)
        m.script = list(PRINT)
        with pytest.raises(RuntimeError, match="link lost"):
            m.pause_print()
        assert head_moves._RUNNING == {}


# ---------------------------------------------------------------------------
# The end of a print, and the footprint read while the print runs
# ---------------------------------------------------------------------------

#: A print's last layers climbing 0.2 mm at a time -- far more than a block's
#: own rise over a few minutes -- then the last line stops, the head climbs
#: and travels to its park.
LAST_LAYERS = [
    (x, 110.0 + 5.0 * k, z, 20.0 + 0.5 * n)
    for n, (k, z, x) in enumerate(
        (k, round(1.0 + 0.2 * layer, 1), x)
        for layer in range(7) for k, x in enumerate((110.0, 125.0, 140.0))
    )
]
END_STOP = [(140.0, 128.0, 2.2, LAST_LAYERS[-1][3])]
END_CLIMB = [(140.0, 128.0, 4.0, LAST_LAYERS[-1][3] - 0.8), (140.0, 128.0, 12.0, LAST_LAYERS[-1][3] - 0.8)]
END_PARK = [(60.0, 200.0, 12.0, LAST_LAYERS[-1][3] - 0.8), (0.0, 215.0, 12.0, LAST_LAYERS[-1][3] - 0.8)]


class _Ending(_Klipperish):
    """A print in its last minutes; the printer says the print is over once
    the head reaches its park."""

    def __init__(self, serial="HEADMOVES0002"):
        super().__init__(serial)
        self.gcode_sent: list[str] = []

    def get_job(self):
        return JobProgress(file_name="bracket.gcode", print_time_seconds=3600, print_time_left_seconds=60,
                           completion=98.0, job_id="job-7")

    def get_tool_position(self):
        reading = super().get_tool_position()
        if reading is not None and reading["z"] >= 12.0 and reading["x"] <= 0.0:
            self.state = PrinterStatus.IDLE
        return reading

    def send_gcode(self, commands):
        self.gcode_sent.append(str(commands))
        return True


@pytest.fixture
def ending(watching, monkeypatch, tmp_path):
    from kiln import daily_stats

    monkeypatch.setattr(head_moves, "END_STATE_EVERY_S", 0.0)
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    head_moves._ENDS.clear()
    yield
    head_moves._ENDS.clear()


def _printing():
    return PrinterState(connected=True, state=PrinterStatus.PRINTING)


class TestTheEnd:
    def test_the_end_is_read_from_after_the_last_line_not_from_the_first_layer_change(self, ending):
        m = _Ending()
        _on_the_plate(m)
        m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
        head_moves.on_progress(m, _printing(), m.get_job())
        doc = _until(lambda: _kept(m, "end"))
        assert doc is not None and doc["how"] == "position_log" and doc["returns"] is False
        assert doc["layer_z_mm"] == 2.2, "the top layer, not the layer the log began at"
        assert doc["points"][0] == list(END_STOP[0][:3]) and doc["points"][-1] == list(END_PARK[-1][:3])
        # Read and never commanded: nothing was sent to the printer.
        assert m.sent == [] and m.gcode_sent == []

    def test_what_the_end_taught_is_said_once_and_counted(self, ending):
        from kiln import daily_stats

        m = _Ending()
        _on_the_plate(m)
        m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
        head_moves.on_progress(m, _printing(), m.get_job())
        assert _until(lambda: _kept(m, "end")) is not None
        said = _until(lambda: bench._learned_path().exists() and bench.take_news()["said"])
        assert said == [f"Kiln learned how your {bench._display_name('creality_ender3')} ends a print."]
        assert bench.take_news()["said"] == [], "said once"
        assert daily_stats.get_daily_stats()["bench_funnel"] == {"learned|motion|real_print": 1}

    def test_an_end_is_logged_once_per_print_and_only_in_its_last_minutes(self, ending):
        m = _Ending()
        _on_the_plate(m)
        early = JobProgress(file_name="bracket.gcode", print_time_left_seconds=head_moves.END_WINDOW_S + 1,
                            job_id="job-7")
        head_moves.on_progress(m, _printing(), early)
        assert head_moves._ENDS == {} and m.asked == 0
        m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
        head_moves.on_progress(m, _printing(), m.get_job())
        assert _until(lambda: _kept(m, "end")) is not None
        assert _until(lambda: not head_moves._RUNNING) is not None or not head_moves._RUNNING
        asked = m.asked
        head_moves.on_progress(m, _printing(), m.get_job())
        time.sleep(0.2)
        assert m.asked == asked, "the same print's end is not logged twice"
        assert head_moves.near_end(JobProgress(completion=99.5)) and not head_moves.near_end(JobProgress(completion=90.0))

    def test_a_print_kiln_did_not_start_or_a_printer_that_cannot_say_teaches_no_end(self, ending):
        m = _Ending()
        m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
        head_moves.on_progress(m, _printing(), m.get_job())
        time.sleep(0.3)
        assert m.asked == 0 and head_moves._ENDS == {}, "a print Kiln did not start is not even read"
        assert bench.observations_of(m) == [] and not bench._learned_path().exists()

        class _ClosedEnding(_Ending):
            head_position = "this printer reports no head position"

        c = _ClosedEnding(serial="HEADMOVES0003")
        _on_the_plate(c)
        head_moves.on_progress(c, _printing(), c.get_job())
        assert c.asked == 0 and head_moves._ENDS == {}

    def test_a_cancel_is_kept_even_when_the_plate_record_moves_on_before_the_head_settles(self, ending):
        # The print-ended hook re-marks the plate the moment a status read sees
        # the print over, and from then on the record no longer says which
        # print ran: the footprint is the one read when the log began.
        m = _Klipperish()
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        assert m.cancel_print().success
        plate_state.mark_occupied(m, None, source="print_ended")
        assert bench.print_footprint(m) is None
        doc = _until(lambda: _kept(m, "cancel"))
        assert doc is not None and doc["points"][0] == list(STOP[0][:3])


class TestNeverInThePrintsWay:
    """Observation on or off, the doors answer the same, send the same and
    return at once, and the watchdog's poll reads the same and commands nothing."""

    #: A log that cannot finish sooner than this, so a door that waited for it
    #: could not return sooner either.
    LONG_SETTLE_S = 2.0

    def _run(self, monkeypatch, on: bool, serial: str):
        monkeypatch.setenv(head_moves.SWITCH_ENV, "1" if on else "0")
        monkeypatch.setattr(head_moves, "SETTLE_S", self.LONG_SETTLE_S)
        m = _Ending(serial=serial)
        _on_the_plate(m)
        m.script = list(PRINT + STOP + CLIMB + PARK)
        answers, slowest, logging_after = [], 0.0, []
        for door in (m.pause_print, m.resume_print, m.cancel_print):
            began = time.monotonic()
            result = door()
            slowest = max(slowest, time.monotonic() - began)
            logging_after.append(bench.unit_of(m) in head_moves._RUNNING)
            answers.append(result.to_dict())
        assert _until(lambda: not head_moves._RUNNING) is not None or not head_moves._RUNNING
        return m, answers, slowest, logging_after

    def test_the_pause_resume_and_cancel_doors_are_the_same_with_the_watch_on_and_off(self, ending, monkeypatch):
        on, on_answers, on_slowest, on_logging = self._run(monkeypatch, True, "IDENTITY0001")
        off, off_answers, _, off_logging = self._run(monkeypatch, False, "IDENTITY0002")
        assert on_answers == off_answers and all(a["success"] for a in on_answers)
        assert on.sent == off.sent == ["pause", "resume", "cancel"]
        assert on.gcode_sent == off.gcode_sent == []
        # The pause's log was still running when the pause door answered: the
        # door did not wait for it.
        assert on_logging[0] is True and on_slowest < self.LONG_SETTLE_S
        assert off.asked == 0 and off_logging == [False, False, False], "off reads nothing"

    def test_the_watchdogs_poll_is_the_same_and_commands_nothing(self, ending, monkeypatch):
        from kiln.print_watchdog import PrintWatchdog

        seen = []
        monkeypatch.setattr(head_moves, "SETTLE_S", self.LONG_SETTLE_S)
        for on in (True, False):
            monkeypatch.setenv(head_moves.SWITCH_ENV, "1" if on else "0")
            m = _Ending(serial=f"IDENTITY001{int(on)}")
            _on_the_plate(m)
            m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
            dog = PrintWatchdog(adapter=m, poll_interval_sec=60.0)
            began = time.monotonic()
            flag = dog.step()
            seen.append((flag, time.monotonic() - began, m))
        (on_flag, on_took, on_m), (off_flag, _t, off_m) = seen
        assert on_flag is None and off_flag is None and on_took < self.LONG_SETTLE_S
        assert _until(lambda: _kept(on_m, "end")) is not None and bench.observations_of(off_m) == []
        assert on_m.sent == off_m.sent == [] and on_m.gcode_sent == off_m.gcode_sent == []


class _Result:
    def __init__(self, structured=None, is_error=False):
        self.structuredContent = structured
        self.isError = is_error
        self.content = []


class TestWhatWasLearnedIsSaid:
    def test_the_next_good_result_carries_it_once_and_a_failure_never(self, home, monkeypatch):
        from kiln import learned_notice

        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        bench.note_learned(_Machine(), "creality_ender3", "end")
        failed = _Result({"success": False, "error": "x"})
        learned_notice._attach(failed, None, "printer_status")
        assert learned_notice.RESULT_KEY not in failed.structuredContent
        ok = _Result({"success": True})
        learned_notice._attach(ok, None, "printer_status")
        block = ok.structuredContent[learned_notice.RESULT_KEY]
        assert block["said"] == [f"Kiln learned how your {bench._display_name('creality_ender3')} ends a print."]
        assert ok.structuredContent["success"] is True, "the result it rides on is kept"
        again = _Result({"success": True})
        learned_notice._attach(again, None, "printer_status")
        assert learned_notice.RESULT_KEY not in again.structuredContent, "said once"

    def test_it_names_what_was_learned_never_what_is_missing(self, home):
        for block in ("pause", "cancel", "end"):
            bench.note_learned(_Machine(), "creality_ender3", block)
        said = " ".join(bench.take_news()["said"]).lower()
        assert said.count("kiln learned how your") == 3
        assert not any(w in said for w in ("doesn't know", "no record", "worst", "still", "missing", "unknown"))

    def test_not_on_the_hosted_server(self, home, monkeypatch):
        from kiln import learned_notice

        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        bench.note_learned(_Machine(), "creality_ender3", "end")
        r = _Result({"success": True})
        learned_notice._attach(r, None, "printer_status")
        assert learned_notice.RESULT_KEY not in r.structuredContent


# ---------------------------------------------------------------------------
# The first print first: the lesson is offered after it, for what it left
# ---------------------------------------------------------------------------


def _blanks_left_by(adapter, *, verdict=True):
    """What the record lacks for *adapter* once its own observations ride the
    request: every block a session teaches, less what is kept on this machine."""
    from tests.test_bench import _verdict

    kept = {d["block"] for d in bench.observations_of(adapter)}
    left = [b for b in ("pause", "cancel", "end", "head") if b not in kept]
    return left, (_verdict(ok=True, blanks=left) if verdict else None)


@pytest.fixture
def first_print(ending, monkeypatch):
    import kiln.licensing as lic
    import kiln.server as srv

    machines: dict[str, object] = {}
    monkeypatch.setattr(srv, "_get_registry", lambda: type("R", (), {"get": staticmethod(lambda n: machines[n])})())
    monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: _blanks_left_by(adapter))
    monkeypatch.setattr(head_moves, "AFTER_PRINT_WAIT_S", 0.02)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    plan = {"pro": False}
    monkeypatch.setattr(lic, "check_tier", lambda tier: (plan["pro"], "pro" if plan["pro"] else "free"))
    head_moves._DECIDING.clear()
    yield machines, plan
    head_moves._DECIDING.clear()


def _next_reply(structured=None):
    from kiln import learned_notice

    r = _Result(structured or {"success": True})
    learned_notice._attach(r, None, "printer_status")
    return r.structuredContent.get(learned_notice.RESULT_KEY)


class TestTheFirstPrintFirst:
    def test_after_the_first_print_the_lesson_is_offered_for_only_what_it_left(self, first_print):
        from kiln import daily_stats

        machines, _plan = first_print
        m = _Ending()
        machines["ender"] = m
        _on_the_plate(m)
        m.script = list(LAST_LAYERS + END_STOP + END_CLIMB + END_PARK)
        head_moves.on_progress(m, _printing(), m.get_job())
        # The edge, while the end is still being logged: the log began in the
        # print's last minutes, before the printer said it was over.
        assert _until(lambda: bench.unit_of(m) in head_moves._RUNNING) is not None
        head_moves.print_ended("ender")
        assert _until(lambda: _kept(m, "end")) is not None
        # What was learned may reach a reply before the offer is decided; each
        # is said once, on whichever good reply comes next.
        said, block = [], {}
        for _ in range(3):
            block = _until(lambda: _next_reply()) or {}
            said += block.get("said", [])
            if "offers" in block:
                break
        name = bench._display_name("creality_ender3")
        assert said == [f"Kiln learned how your {name} ends a print."]
        assert [o["sentence"] for o in block["offers"]] == [
            f"A few minutes with a coin-sized test print teaches Kiln how your {name} pauses and cancels, and how big "
            "its head is, so it can tell you whether a second part fits beside one already on the plate."]
        assert block["offers"][0]["tool"] == "printer_bench" and block["offers"][0]["printer_name"] == "ender"
        assert "finishes" not in block["offers"][0]["sentence"], "what the print taught is not offered"
        assert _next_reply() is None, "said once"
        assert daily_stats.get_daily_stats()["bench_funnel"] == {
            "learned|motion|real_print": 1, "offered|motion|first_print": 1}
        # Once, ever: the next print asks nothing.
        head_moves.print_ended("ender")
        time.sleep(0.2)
        assert _next_reply() is None

    def test_the_offer_is_worded_for_the_callers_plan_when_it_is_said(self, first_print):
        machines, plan = first_print
        m = _Closed(serial="FIRSTPRINT0002")      # a printer that cannot say where its head is
        machines["p1s"] = m
        bench.offer_after_first_print(m, "p1s")
        plan["pro"] = True
        block = _next_reply()
        name = bench._display_name("creality_ender3")
        assert "said" not in block
        assert block["offers"][0]["sentence"] == (
            f"A few minutes with a coin-sized test print teaches Kiln how your {name} pauses, cancels and finishes, "
            "and how big its head is, so it can find where a second part fits beside one already on the plate.")
        assert "start it" not in json.dumps(block)

    def test_nothing_missing_is_never_offered_and_offline_spends_nothing(self, first_print, monkeypatch):
        m = _Closed(serial="FIRSTPRINT0003")
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: _blanks_left_by(adapter, verdict=False))
        bench.offer_after_first_print(m, "x")
        assert _next_reply() is None and bench.may_offer(bench.unit_of(m), "first_print") is True
        from tests.test_bench import _verdict

        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["filament_change"], _verdict(ok=True)))
        bench.offer_after_first_print(m, "x")
        assert _next_reply() is None
        assert not any(bench.may_offer(bench.unit_of(m), door) for door in bench.OFFER_DOORS if door != "lesson")

    def test_an_answer_given_before_the_reply_ends_the_offer(self, first_print):
        m = _Closed(serial="FIRSTPRINT0004")
        bench.offer_after_first_print(m, "x")
        bench.note_offer(bench.unit_of(m), answer="later")
        assert _next_reply() is None
