"""Tests for :class:`kiln.print_watchdog.PrintWatchdog`.

These tests drive the watchdog through its :meth:`step` entry point so
no real threads are needed for the red-flag logic.  The thread
lifecycle itself is exercised in a dedicated test at the bottom.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from kiln.print_watchdog import (
    DEFAULT_BED_DROP_C,
    DEFAULT_NO_RISE_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TOOL_DROP_C,
    DEFAULT_WARMUP_TIMEOUT_S,
    HEATING_RISE_C,
    MAX_ESTOP_ATTEMPTS,
    MIN_ACTIVE_TARGET_C,
    PRINT_ERROR_PERSIST_S,
    REACHED_MARGIN_C,
    WARMUP_WARN_FRACTION,
    Flag,
    PrintWatchdog,
)
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


@dataclass
class FakeState:
    """Duck-typed stand-in for :class:`kiln.printers.base.PrinterState`."""

    state: str = "printing"
    tool_temp_actual: float | None = 215.0
    tool_temp_target: float | None = 220.0
    bed_temp_actual: float | None = 60.0
    bed_temp_target: float | None = 60.0
    wifi_signal: str | None = "-60dBm"
    chamber_fan_speed: int | None = None
    print_error: int | None = 0
    hms_code: str | None = None


@dataclass
class FakeJob:
    current_layer: int | None = 10
    completion: float | None = 12.5
    print_time_left_seconds: int | None = None


#: The shared detector's threshold, plus a minute: long enough that frozen
#: counters read as a stall.  The watchdog owns no stall number of its own.
STALL_S: float = 15 * 60 + 60

#: A real Bambu fault code: 1200-8007, "failed to extrude the filament",
#: measured on an A1 on 2026-09-07.  Which fault it names does not matter to
#: the rules under test -- only that the printer reports one.
A_FAULT: int = 302022663

#: The code a cancel walks an A1's firmware through (measured 2026-08-14), and
#: the one the 2026-08-13 emergency stop was raised on.
CANCEL_CODE: int = 50348044


@pytest.fixture(autouse=True)
def _fresh_motion_store():
    from kiln.printers import progress_motion as pm

    pm.reset_progress_observations()
    yield
    pm.reset_progress_observations()


class FakeAdapter:
    """Records calls to ``emergency_stop`` and lets tests mutate state.

    ``stop_results`` is what each ``emergency_stop()`` call answers, in order,
    the last one repeating; an exception instance is raised instead.  The
    default answers a bare ``True``, as older doubles do, which the watchdog
    counts as a confirmed stop.
    """

    def __init__(
        self,
        state: FakeState | None = None,
        job: FakeJob | None = None,
        stop_results: list[object] | None = None,
    ):
        self.state = state if state is not None else FakeState()
        self.job = job if job is not None else FakeJob()
        self.stop_results: list[object] = list(stop_results) if stop_results is not None else [True]
        self.emergency_stops = 0
        self.get_state_calls = 0
        self.get_job_calls = 0
        self.raise_on_get_state = False

    def get_state(self) -> FakeState:
        self.get_state_calls += 1
        if self.raise_on_get_state:
            raise RuntimeError("printer offline")
        return self.state

    def get_job(self) -> FakeJob:
        self.get_job_calls += 1
        return self.job

    def emergency_stop(self) -> object:
        answer = self.stop_results[min(self.emergency_stops, len(self.stop_results) - 1)]
        self.emergency_stops += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeClock:
    """Monotonic-like clock driven explicitly by tests."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_watchdog(
    adapter: FakeAdapter | None = None,
    clock: FakeClock | None = None,
    on_anomaly=None,
    hms_blocklist=None,
    **kwargs,
) -> tuple[PrintWatchdog, FakeAdapter, FakeClock, list[Flag]]:
    adapter = adapter or FakeAdapter()
    clock = clock or FakeClock()
    anomalies: list[Flag] = []

    def capture(flag: Flag) -> None:
        anomalies.append(flag)
        if on_anomaly is not None:
            on_anomaly(flag)

    wd = PrintWatchdog(
        adapter=adapter,
        poll_interval_sec=0.01,
        on_anomaly=capture,
        hms_blocklist=hms_blocklist,
        time_fn=clock,
        **kwargs,
    )
    return wd, adapter, clock, anomalies


def _raise_print_error(wd: PrintWatchdog, adapter: FakeAdapter, clock: FakeClock, code: int) -> Flag | None:
    """Hold *code* on a printing reading across the persistence window.

    Returns what the second poll raised.  The first sighting must raise
    nothing: a code that has not stood yet is still news in transit.
    """
    adapter.state.print_error = code
    assert wd.step() is None
    clock.advance(PRINT_ERROR_PERSIST_S)
    return wd.step()


def _reading(state: PrinterStatus, print_error: int = 0) -> PrinterState:
    """A real reading, so an uncleared code takes the headline as it does live."""
    return PrinterState(
        connected=True,
        state=state,
        tool_temp_actual=220.0,
        tool_temp_target=220.0,
        bed_temp_actual=60.0,
        bed_temp_target=60.0,
        print_error=print_error,
    )


# --------------------------------------------------------------------------
# Red flag: tool temperature drop
# --------------------------------------------------------------------------


class TestToolTempDrop:
    def test_triggers_when_tool_drops_below_threshold(self):
        wd, adapter, _, anomalies = _make_watchdog()
        # Reach the setpoint first so the drop check is armed — a gap
        # present before first reach is a warmup ramp, not a drop.
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None
        # Drop hotend 35°C below the target — exceeds default 30°C threshold.
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"
        assert flag.kind == "red"
        assert adapter.emergency_stops == 1
        assert wd.anomaly_triggered is True
        assert len(anomalies) == 1
        assert anomalies[0].rule == "tool_drop"

    def test_does_not_trigger_inside_pid_wobble(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 215.0  # 5°C drop, well under threshold

        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0
        assert wd.anomaly_triggered is False

    def test_ignores_drop_when_heater_is_off(self):
        wd, adapter, _, _ = _make_watchdog()
        # Target below MIN_ACTIVE_TARGET_C — cooling is expected.
        adapter.state.tool_temp_target = 0.0
        adapter.state.tool_temp_actual = -1000  # nonsensically low
        adapter.state.bed_temp_target = 0.0
        adapter.state.bed_temp_actual = -1000

        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0


# --------------------------------------------------------------------------
# Red flag: bed temperature drop
# --------------------------------------------------------------------------


class TestBedTempDrop:
    def test_triggers_when_bed_drops_below_threshold(self):
        wd, adapter, _, _ = _make_watchdog()
        # Reach the setpoint first so the drop check is armed.
        adapter.state.bed_temp_target = 60.0
        adapter.state.bed_temp_actual = 60.0
        assert wd.step() is None
        adapter.state.bed_temp_actual = 60.0 - (DEFAULT_BED_DROP_C + 2.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "bed_drop"
        assert adapter.emergency_stops == 1


# --------------------------------------------------------------------------
# Red flag: print_error / HMS blocklist
# --------------------------------------------------------------------------


class TestPrintError:
    def test_nonzero_print_error_triggers_estop(self):
        """A code the printer keeps printing through, once it has stood."""
        wd, adapter, clock, anomalies = _make_watchdog()

        flag = _raise_print_error(wd, adapter, clock, 0x03008014)  # Bambu HMS code

        assert flag is not None
        assert flag.rule == "print_error"
        assert adapter.emergency_stops == 1
        assert anomalies[0].context["print_error"] == 0x03008014

    def test_zero_print_error_does_not_trigger(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.print_error = 0

        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0

    def test_hms_blocklist_match_triggers_estop(self):
        wd, adapter, _, _ = _make_watchdog(
            hms_blocklist=["0300-8014", "0500-C010"],
        )
        adapter.state.hms_code = "0300-8014"

        flag = wd.step()

        assert flag is not None
        assert flag.rule in ("hms_blocklist", "print_error")
        assert adapter.emergency_stops == 1

    def test_hms_blocklist_case_insensitive(self):
        wd, adapter, _, _ = _make_watchdog(
            hms_blocklist=["0300-abcd"],
        )
        adapter.state.hms_code = "0300-ABCD"

        flag = wd.step()

        assert flag is not None
        assert adapter.emergency_stops == 1


class TestAFaultTheMachineHasActedOn:
    """Bambu firmware reports ``print_error`` once it has ALREADY acted.

    It pauses the job or ends it.  An emergency stop on top of that cancels a
    recoverable pause or lands on a print that is already over -- harmless
    only while the stop itself did nothing, and a ten-hour print killed for a
    filament runout once it does.
    """

    def test_replay_2026_08_13_a_print_the_printer_already_failed_is_not_stopped(self):
        """The server log on the A1, 21:02: the outcome hook recorded
        ``prev='running'→new='failed', hms=50348044``, then the watchdog raised
        ``RED FLAG [print_error]`` and dispatched an emergency stop onto a print
        the printer had already failed.  Replayed at the watchdog's cadence."""
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state = _reading(PrinterStatus.PRINTING)
        assert wd.step() is None

        adapter.state = _reading(PrinterStatus.ERROR, CANCEL_CODE)  # failed, code attached
        for _ in range(8):  # twenty seconds of the code standing
            clock.advance(DEFAULT_POLL_INTERVAL)
            assert wd.step() is None

        adapter.state = _reading(PrinterStatus.IDLE)
        clock.advance(DEFAULT_POLL_INTERVAL)
        assert wd.step() is None

        assert adapter.emergency_stops == 0
        assert wd.status()["red_flags"] == []

    def test_a_paused_print_reporting_a_code_is_never_cancelled(self):
        """Ten minutes of polls on a print the firmware paused for a fault."""
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state = _reading(PrinterStatus.PAUSED, A_FAULT)
        # Live, the fault takes the headline and the pause sits underneath it.
        assert adapter.state.state is PrinterStatus.ERROR

        for _ in range(int(600 / DEFAULT_POLL_INTERVAL)):
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        assert adapter.emergency_stops == 0
        assert [f for f in anomalies if f.kind == "red"] == []


class TestAFaultThePrinterIsPrintingThrough:
    """A code that stands while the machine keeps printing is the one to stop for."""

    def test_the_same_code_standing_while_printing_stops_the_machine(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state = _reading(PrinterStatus.PRINTING, A_FAULT)
        # A live reading puts the fault on top and the run state underneath;
        # the rule has to read through the one to the other.
        assert adapter.state.state is PrinterStatus.ERROR

        assert wd.step() is None  # first sighting
        clock.advance(PRINT_ERROR_PERSIST_S / 2)
        assert wd.step() is None  # has not stood long enough yet
        clock.advance(PRINT_ERROR_PERSIST_S / 2)
        flag = wd.step()

        assert flag is not None and flag.kind == "red" and flag.rule == "print_error"
        assert flag.context["print_error"] == A_FAULT
        assert adapter.emergency_stops == 1
        assert [f.rule for f in anomalies] == ["print_error"]

    def test_a_code_gone_on_the_next_poll_stops_nothing(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state = _reading(PrinterStatus.PRINTING, A_FAULT)
        assert wd.step() is None

        clock.advance(PRINT_ERROR_PERSIST_S)
        adapter.state = _reading(PrinterStatus.PRINTING)
        assert wd.step() is None

        # Back again: a new sighting, not the old one continued.
        clock.advance(PRINT_ERROR_PERSIST_S)
        adapter.state = _reading(PrinterStatus.PRINTING, A_FAULT)
        assert wd.step() is None
        clock.advance(DEFAULT_POLL_INTERVAL)
        assert wd.step() is None

        assert adapter.emergency_stops == 0

    def test_a_code_during_a_stop_kiln_asked_for_stops_nothing(self):
        """Stopping a Bambu walks its firmware through a real code; that code
        is the stop's own noise, not a fault to stop the machine for."""
        from kiln.auto_record_hook import register_cancel_intent
        from kiln.printers.base import outcome_printer_name

        wd, adapter, clock, _ = _make_watchdog()
        register_cancel_intent(outcome_printer_name(adapter))
        adapter.state = _reading(PrinterStatus.PRINTING, CANCEL_CODE)

        assert wd.step() is None
        clock.advance(PRINT_ERROR_PERSIST_S)
        assert wd.step() is None
        clock.advance(DEFAULT_POLL_INTERVAL)
        assert wd.step() is None

        assert adapter.emergency_stops == 0


# --------------------------------------------------------------------------
# A stop the printer did not confirm
# --------------------------------------------------------------------------


class TestAnUnconfirmedStop:
    """A stop nobody saw land must not put the watchdog to sleep."""

    NOT_CONFIRMED = PrintResult(
        success=False,
        message=(
            "Emergency stop SENT but NOT confirmed: the printer still reports "
            "printing 5s later. Stop it at the machine now, with its own screen "
            "or its power switch."
        ),
    )

    def test_it_is_retried_while_the_fault_stands_then_latched(self, monkeypatch):
        noted: list[object] = []
        monkeypatch.setattr("kiln.print_watchdog.note_cancel_requested", noted.append)
        wd, adapter, clock, anomalies = _make_watchdog(
            adapter=FakeAdapter(stop_results=[self.NOT_CONFIRMED])
        )

        flag = _raise_print_error(wd, adapter, clock, A_FAULT)

        assert flag is not None and flag.rule == "print_error"
        assert adapter.emergency_stops == 1
        assert wd.anomaly_triggered is False, "a stop nobody saw land must not latch"

        # The stop files its own intent; that must not read as somebody else's
        # stop and silence the fault it is retrying against.
        for attempt in range(2, MAX_ESTOP_ATTEMPTS + 1):
            clock.advance(DEFAULT_POLL_INTERVAL)
            assert wd.step() is not None  # the fault still stands
            assert adapter.emergency_stops == attempt
        assert wd.anomaly_triggered is True  # the ceiling

        clock.advance(DEFAULT_POLL_INTERVAL)
        assert wd.step() is None
        assert adapter.emergency_stops == MAX_ESTOP_ATTEMPTS
        # Filed once: a retried stop is the same ending, not another one.
        assert len(noted) == 1
        # Recorded and handed on once, with the stop's own answer attached.
        assert [f.rule for f in anomalies] == ["print_error"]
        assert anomalies[0].context["estop_confirmed"] is False
        assert "stop it at the machine" in anomalies[0].context["estop_result"].lower()
        assert len(wd.status()["red_flags"]) == 1

    def test_a_confirmed_stop_latches_after_one_call(self):
        confirmed = PrintResult(
            success=True,
            message="Emergency stop confirmed: the printer left printing 1.2s after the stop command.",
        )
        wd, adapter, clock, anomalies = _make_watchdog(adapter=FakeAdapter(stop_results=[confirmed]))
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None  # arrived
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        assert wd.step() is not None
        assert wd.anomaly_triggered is True

        for _ in range(5):
            clock.advance(DEFAULT_POLL_INTERVAL)
            assert wd.step() is None
        assert adapter.emergency_stops == 1
        assert [f.rule for f in anomalies] == ["tool_drop"]
        assert anomalies[0].context["estop_confirmed"] is True

    def test_a_poll_with_nothing_left_to_stop_for_latches_without_another_stop(self):
        wd, adapter, clock, _ = _make_watchdog(adapter=FakeAdapter(stop_results=[self.NOT_CONFIRMED]))
        assert _raise_print_error(wd, adapter, clock, A_FAULT) is not None
        assert wd.anomaly_triggered is False

        # The printer ended the job after all, a little late.
        adapter.state.state = "error"
        clock.advance(DEFAULT_POLL_INTERVAL)
        assert wd.step() is None

        assert wd.anomaly_triggered is True
        assert adapter.emergency_stops == 1


# --------------------------------------------------------------------------
# Warmup grace
# --------------------------------------------------------------------------


class TestWarmupGrace:
    """A heater on its way to its target must not be read as a failed one."""

    def test_cold_start_ramp_does_not_trip(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.bed_temp_target = 60.0

        for i in range(72):  # a three minute ramp from ambient
            frac = i / 71
            adapter.state.tool_temp_actual = 25.0 + (220.0 - 25.0) * frac
            adapter.state.bed_temp_actual = 25.0 + (60.0 - 25.0) * frac
            adapter.job.current_layer += 1
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        assert adapter.emergency_stops == 0

    def test_check_starts_once_the_heater_arrives(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        assert wd.step() is None  # far below target, but on its way up

        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None  # arrived
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_heater_that_never_heats_is_reported(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        assert wd.step() is None

        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S - DEFAULT_POLL_INTERVAL)
        assert wd.step() is None  # not yet: the deadline has not passed
        clock.advance(DEFAULT_POLL_INTERVAL * 2)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_not_heating"
        assert flag.kind == "red"
        assert flag.context["no_rise_seconds"] >= DEFAULT_NO_RISE_TIMEOUT_S
        assert adapter.emergency_stops == 1
        assert anomalies[0].rule == "tool_not_heating"

    def test_bed_that_never_heats_is_reported(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.bed_temp_target = 60.0
        adapter.state.bed_temp_actual = 20.0
        assert wd.step() is None
        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S + 1.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "bed_not_heating"
        assert flag.kind == "red"
        assert adapter.emergency_stops == 1

    def test_a_slow_climb_is_accepted_at_any_speed(self):
        """The rule is whether a heater is still climbing, not how fast."""
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        rise_per_poll = HEATING_RISE_C / 4  # far slower than any real heater

        for _ in range(int(DEFAULT_NO_RISE_TIMEOUT_S / DEFAULT_POLL_INTERVAL) * 3):
            adapter.state.tool_temp_actual += rise_per_poll
            adapter.job.current_layer += 1
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        assert adapter.emergency_stops == 0

    def test_a_heater_that_stops_climbing_is_reported(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 100.0
        wd.step()
        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S + 1.0)
        adapter.job.current_layer += 1

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_not_heating"

    def test_a_warming_heater_never_becomes_a_stall_verdict(self):
        """Heating is not a fault and a stall is not a red flag: a print
        held at the same layer through a slow warmup and beyond raises
        nothing red, and emergency_stop is never called."""
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()

        clock.advance(STALL_S)
        adapter.state.tool_temp_actual = 100.0  # still climbing
        assert wd.step() is None

        adapter.state.tool_temp_actual = 220.0  # arrived
        wd.step()
        clock.advance(STALL_S)

        assert wd.step() is None
        assert adapter.emergency_stops == 0
        assert [f.rule for f in anomalies if f.kind == "red"] == []

    def test_missing_reading_does_not_stop_the_check(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None  # arrived
        adapter.state.tool_temp_actual = None  # one reading goes missing
        assert wd.step() is None
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_missing_reading_does_not_restart_the_timer(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        assert wd.step() is None
        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S - 10.0)

        adapter.state.tool_temp_actual = None  # missing near the deadline
        adapter.job.current_layer += 1  # keep the stall rule quiet
        assert wd.step() is None

        clock.advance(20.0)
        adapter.state.tool_temp_actual = 25.0
        adapter.job.current_layer += 1

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_not_heating"

    def test_a_setpoint_change_does_not_restart_the_timer(self):
        """The timer measures time since the last rise, whatever the target."""
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_actual = 200.0  # stuck here for the whole test
        adapter.state.tool_temp_target = 240.0
        assert wd.step() is None

        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S / 2)
        adapter.state.tool_temp_target = 220.0  # target changes, heater does not move
        adapter.job.current_layer += 1
        assert wd.step() is None

        clock.advance(DEFAULT_NO_RISE_TIMEOUT_S / 2 + 1.0)
        adapter.state.tool_temp_target = 240.0
        adapter.job.current_layer += 1

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_not_heating"

    def test_a_small_steady_shortfall_is_never_reported(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0 - (REACHED_MARGIN_C - 2.0)

        for _ in range(int(DEFAULT_NO_RISE_TIMEOUT_S / DEFAULT_POLL_INTERVAL) + 20):
            adapter.job.current_layer += 1
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_a_second_print_checks_from_scratch(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        wd.step()

        adapter.state.state = "idle"
        adapter.state.tool_temp_actual = 25.0
        wd.step()
        clock.advance(300.0)

        adapter.state.state = "printing"  # second print, same target, cold again
        for i in range(72):
            adapter.state.tool_temp_actual = 25.0 + (220.0 - 25.0) * (i / 71)
            adapter.job.current_layer += 1
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_raising_the_target_is_treated_as_warming(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.tool_temp_target = 200.0
        adapter.state.tool_temp_actual = 200.0
        assert wd.step() is None  # arrived at 200

        adapter.state.tool_temp_target = 240.0  # now 40 below the new target
        assert wd.step() is None

        adapter.state.tool_temp_actual = 240.0
        assert wd.step() is None  # arrived again
        adapter.state.tool_temp_actual = 240.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_switching_a_heater_off_and_on_is_treated_as_warming(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None

        adapter.state.tool_temp_target = MIN_ACTIVE_TARGET_C - 1.0  # M104 S0
        adapter.state.tool_temp_actual = 150.0
        assert wd.step() is None

        adapter.state.tool_temp_target = 220.0  # back on, still cool
        adapter.state.tool_temp_actual = 120.0
        assert wd.step() is None

        assert adapter.emergency_stops == 0

    def test_switching_the_bed_off_and_on_is_treated_as_warming(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.bed_temp_target = 60.0
        adapter.state.bed_temp_actual = 60.0
        assert wd.step() is None

        adapter.state.bed_temp_target = MIN_ACTIVE_TARGET_C - 1.0
        adapter.state.bed_temp_actual = 40.0
        assert wd.step() is None

        adapter.state.bed_temp_target = 60.0
        adapter.state.bed_temp_actual = 30.0
        assert wd.step() is None

        assert adapter.emergency_stops == 0


# --------------------------------------------------------------------------
# Red flag: stuck layer
# --------------------------------------------------------------------------


class TestStalledPrintIsReportedNeverStopped:
    """A print that stops moving is a yellow flag from the shared detector.

    It used to be a red flag at 90 seconds: nothing measured supported the
    number, a Bambu's whole-percent counter alone freezes longer than that
    on any print over 2.5 hours, a clog does not freeze the counters at all,
    and the idempotent trip switched off every real rule for the rest of
    the print.  These tests fail on that code.
    """

    def test_frozen_counters_are_a_yellow_flag_once_and_never_an_estop(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        wd.step()  # baseline
        clock.advance(STALL_S)
        assert wd.step() is None  # no RED flag
        clock.advance(60.0)
        assert wd.step() is None

        assert adapter.emergency_stops == 0
        assert wd.anomaly_triggered is False
        stalls = [f for f in anomalies if f.rule == "stalled"]
        assert len(stalls) == 1
        assert stalls[0].kind == "yellow"
        assert "has not actually moved" in stalls[0].message
        assert stalls[0].context["frozen_for_seconds"] >= 15 * 60

    def test_ninety_seconds_is_not_a_stall(self):
        """The retired trip point, on the print that would have died there."""
        wd, adapter, clock, anomalies = _make_watchdog()
        wd.step()
        clock.advance(91.0)
        assert wd.step() is None
        assert adapter.emergency_stops == 0
        assert [f for f in anomalies if f.rule == "stalled"] == []

    def test_a_moving_countdown_holds_the_flag_quiet(self):
        """A huge first layer: counters frozen, but the printer's own ETA is
        ticking down.  The detector's guard keeps the watchdog silent."""
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.job.print_time_left_seconds = 3600
        wd.step()
        clock.advance(STALL_S)
        adapter.job.print_time_left_seconds = 3000
        assert wd.step() is None
        assert [f for f in anomalies if f.rule == "stalled"] == []

    def test_layer_advance_keeps_it_quiet(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        wd.step()
        clock.advance(STALL_S - 120.0)  # inside the threshold
        adapter.job.current_layer += 1
        assert wd.step() is None
        clock.advance(STALL_S - 120.0)  # inside it again, measured from the move
        assert wd.step() is None
        assert [f for f in anomalies if f.rule == "stalled"] == []
        assert adapter.emergency_stops == 0

    def test_no_stall_check_when_not_printing(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state.state = "paused"
        wd.step()
        clock.advance(STALL_S * 5)
        assert wd.step() is None
        assert [f for f in anomalies if f.rule == "stalled"] == []

    def test_a_stall_no_longer_disarms_the_real_rules(self):
        """The failure that made the old rule dangerous twice over: after a
        (false) stall trip the watchdog slept, so a real fault later in the
        print was never acted on.  Now a stall is reported and the watchdog
        keeps guarding."""
        wd, adapter, clock, anomalies = _make_watchdog()
        wd.step()
        clock.advance(STALL_S)
        wd.step()
        assert [f for f in anomalies if f.rule == "stalled"]

        flag = _raise_print_error(wd, adapter, clock, 50348044)  # the machine's own fault

        assert flag is not None and flag.rule == "print_error"
        assert adapter.emergency_stops == 1

    def test_a_second_stall_on_the_same_print_is_reported_again(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        wd.step()
        clock.advance(STALL_S)
        wd.step()
        adapter.job.current_layer += 5  # moving again
        wd.step()
        clock.advance(STALL_S)
        wd.step()
        assert len([f for f in anomalies if f.rule == "stalled"]) == 2

# --------------------------------------------------------------------------
# Yellow flags — do NOT trigger e-stop
# --------------------------------------------------------------------------


class TestYellowFlags:
    # "Yellow only" means the print is not stopped.  It does not mean the
    # condition goes unreported: these two asserted ``anomalies == []``, which
    # pinned the missing notification path as though it were the contract.
    def test_weak_wifi_is_yellow_only(self):
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"

        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0
        assert [f.kind for f in anomalies] == ["yellow"]  # reported, not stopped

        status = wd.status()
        assert len(status["yellow_flags"]) == 1
        assert status["yellow_flags"][0]["rule"] == "wifi_weak"

    def test_chamber_fan_stalled_is_yellow_only(self):
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.chamber_fan_speed = 0

        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0
        assert [f.kind for f in anomalies] == ["yellow"]

        status = wd.status()
        rules = [f["rule"] for f in status["yellow_flags"]]
        assert "chamber_fan_stalled" in rules

    def test_strong_wifi_produces_no_yellow_flag(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.wifi_signal = "-55dBm"

        wd.step()

        assert wd.status()["yellow_flags"] == []


# --------------------------------------------------------------------------
# Idempotence / latching
# --------------------------------------------------------------------------


class TestLatching:
    def test_subsequent_steps_after_trip_do_not_spam_estop(self):
        wd, adapter, clock, _ = _make_watchdog()
        _raise_print_error(wd, adapter, clock, 0x03008014)

        wd.step()
        wd.step()
        wd.step()

        assert adapter.emergency_stops == 1
        assert wd.anomaly_triggered is True

    def test_callback_invoked_once_per_trip(self):
        calls: list[Flag] = []

        def cb(flag: Flag) -> None:
            calls.append(flag)

        wd, adapter, clock, _ = _make_watchdog(on_anomaly=cb)
        _raise_print_error(wd, adapter, clock, 42)

        wd.step()
        wd.step()

        # on_anomaly is wrapped by _make_watchdog; the `calls` list is the
        # user-supplied outer callback, so it should fire once.
        assert len(calls) == 1

    def test_status_reports_trip_after_anomaly(self):
        wd, adapter, clock, _ = _make_watchdog()
        _raise_print_error(wd, adapter, clock, 1)

        status = wd.status()
        assert status["anomaly_triggered"] is True
        assert len(status["red_flags"]) == 1
        assert status["red_flags"][0]["rule"] == "print_error"


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


class TestErrorHandling:
    def test_get_state_failure_does_not_crash_watchdog(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.raise_on_get_state = True

        # Should swallow the exception and simply skip this tick.
        assert wd.step() is None
        assert wd.anomaly_triggered is False

    def test_an_estop_that_raises_is_unconfirmed_and_still_invokes_callback(self):
        """A stop that raised is a stop nobody saw land.

        This used to assert the watchdog latched anyway, which put it to sleep
        on a print that, for all anyone knew, was still running.
        """
        calls: list[Flag] = []
        wd, adapter, clock, _ = _make_watchdog(
            adapter=FakeAdapter(stop_results=[RuntimeError("mqtt disconnected")]),
            on_anomaly=calls.append,
        )

        _raise_print_error(wd, adapter, clock, 1)

        assert wd.anomaly_triggered is False
        assert len(calls) == 1
        assert calls[0].rule == "print_error"
        assert calls[0].context["estop_confirmed"] is False

        clock.advance(DEFAULT_POLL_INTERVAL)
        wd.step()
        assert adapter.emergency_stops == 2  # commanded again while the fault stands


# --------------------------------------------------------------------------
# Thread lifecycle
# --------------------------------------------------------------------------


class TestThreadLifecycle:
    def test_start_then_stop_cleanly_joins_thread(self):
        wd, _adapter, _, _ = _make_watchdog()

        wd.start()
        # Give the thread a moment to enter its wait loop.
        time.sleep(0.05)
        assert wd._thread is not None
        assert wd._thread.is_alive()

        wd.stop(timeout=1.0)

        # _thread is cleared to None by stop()
        assert wd._thread is None

    def test_stop_is_safe_without_start(self):
        wd, _adapter, _, _ = _make_watchdog()
        wd.stop()  # should not raise

    def test_start_is_idempotent(self):
        wd, _adapter, _, _ = _make_watchdog()
        wd.start()
        first = wd._thread
        wd.start()
        assert wd._thread is first  # no second thread spawned
        wd.stop(timeout=1.0)

    def test_thread_actually_polls_adapter(self):
        adapter = FakeAdapter()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0  # healthy

        wd = PrintWatchdog(
            adapter=adapter,
            poll_interval_sec=0.02,
            # Use wall-clock here so the background thread can drive itself.
        )

        wd.start()
        try:
            # Wait for the thread to poll at least a few times.
            deadline = time.time() + 2.0
            while adapter.get_state_calls < 3 and time.time() < deadline:
                time.sleep(0.02)
            assert adapter.get_state_calls >= 3
        finally:
            wd.stop(timeout=1.0)


# --------------------------------------------------------------------------
# Yellow flags reach the caller
# --------------------------------------------------------------------------


class TestYellowFlagsAreDelivered:
    """A warning nobody receives is not a warning.

    Before the notify/stop split, ``on_anomaly`` was reachable only from
    ``_trip``, which also fires the e-stop.  Yellow flags were therefore
    appended to an in-memory list and logged, and nothing in ``kiln/src`` ever
    read that list — so ``wifi_weak`` and ``chamber_fan_stalled`` had never
    reached a caller.
    """

    def test_yellow_flag_reaches_the_callback(self):
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"

        assert wd.step() is None  # a warning is not a red flag

        assert [f.rule for f in anomalies] == ["wifi_weak"]
        assert anomalies[0].kind == "yellow"

    def test_yellow_flag_does_not_stop_the_print(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"
        adapter.state.chamber_fan_speed = 0

        wd.step()

        assert adapter.emergency_stops == 0
        assert wd.status()["anomaly_triggered"] is False

    def test_a_standing_condition_is_reported_once(self):
        """Weak WiFi holds for hours; the caller is told once, not 5000 times."""
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"

        for _ in range(50):
            adapter.job.current_layer += 1
            wd.step()
            clock.advance(2.5)

        assert len(anomalies) == 1
        assert len(wd.status()["yellow_flags"]) == 1

    def test_each_condition_is_reported_on_its_own(self):
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"
        wd.step()
        adapter.state.chamber_fan_speed = 0
        wd.step()

        assert sorted(f.rule for f in anomalies) == ["chamber_fan_stalled", "wifi_weak"]

    def test_a_new_print_hears_about_it_again(self):
        """'Your WiFi was weak on the last print' is not worth withholding."""
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.wifi_signal = "-90dBm"
        wd.step()
        assert len(anomalies) == 1

        adapter.state.state = "idle"
        wd.step()
        adapter.state.state = "printing"
        wd.step()

        assert len(anomalies) == 2

    def test_a_raising_callback_does_not_escape(self):
        def boom(flag):
            raise RuntimeError("consumer exploded")

        wd, adapter, _, _ = _make_watchdog(on_anomaly=boom)
        adapter.state.wifi_signal = "-90dBm"

        assert wd.step() is None  # swallowed, exactly as on the red path
        assert adapter.emergency_stops == 0


class TestRedFlagBehaviourUnchanged:
    """The split must be invisible to red flags."""

    # Both of these reach the setpoint before dropping away from it. A gap
    # present before the heater ever arrived is a warmup ramp, and the drop
    # rules are right not to fire on it — the same correction the warmup
    # grace required of the two temperature-drop tests above.

    def test_red_still_records_notifies_and_stops(self):
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None  # arrived
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        flag = wd.step()

        assert flag is not None and flag.rule == "tool_drop"
        assert adapter.emergency_stops == 1
        assert [f.rule for f in anomalies] == ["tool_drop"]
        assert wd.status()["red_flags"][0]["rule"] == "tool_drop"
        assert wd.status()["anomaly_triggered"] is True

    def test_a_red_flag_short_circuits_the_yellow_pass(self):
        """Unchanged: step() returns on the red flag before yellows run."""
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 220.0
        assert wd.step() is None  # arrive first, with nothing yellow to say
        # Now both conditions are live on the same poll.
        adapter.state.wifi_signal = "-90dBm"
        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)

        wd.step()

        assert [f.rule for f in anomalies] == ["tool_drop"]


# --------------------------------------------------------------------------
# Warmup ceiling
# --------------------------------------------------------------------------

# A climb slow enough to stay far from target for a long time, but fast
# enough to satisfy the still-climbing rule on every poll.  Both halves
# matter: this is exactly the signature that had no detector at all.
_CREEP_STEP_S = 100.0  # < DEFAULT_NO_RISE_TIMEOUT_S, so no-rise never fires
_CREEP_RISE_C = 1.5  # > HEATING_RISE_C, so the rise reference keeps advancing


def _creep(wd, adapter, clock, seconds, heater="tool"):
    """Run the heater up at the pathological rate, returning every flag."""
    flags = []
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(_CREEP_STEP_S)
        elapsed += _CREEP_STEP_S
        if heater == "tool":
            adapter.state.tool_temp_actual += _CREEP_RISE_C
        else:
            adapter.state.bed_temp_actual += _CREEP_RISE_C
        adapter.job.current_layer += 1  # keep the stall rule quiet
        flags.append(wd.step())
    return flags


def _reds(flags):
    return [f for f in flags if f is not None and f.kind == "red"]


class TestWarmupCeiling:
    """A heater may climb as slowly as it likes — but not forever.

    The warmup grace disarms the drop rule until a heater arrives, and
    pauses the stall timer while it climbs.  That leaves one state with no
    detector: still climbing, never arriving.  A rise of 1°C per 119s
    satisfies the still-climbing rule indefinitely.
    """

    def test_slow_climb_far_below_is_reported_at_the_ceiling(self):
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        assert wd.step() is None

        reds = _reds(_creep(wd, adapter, clock, 600.0))

        assert reds, "a heater 180°C below target after the ceiling must be reported"
        assert reds[0].rule == "tool_warmup_timeout"
        assert adapter.emergency_stops == 1
        # It was climbing the whole way, so the no-rise rule must not be what
        # fired — otherwise this passes for the wrong reason.
        assert not any(f.rule == "tool_not_heating" for f in reds)

    def test_slow_climb_near_target_is_not_reported(self):
        """The asymptote is protected: heaters slow down as they arrive."""
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        # Inside the drop threshold but outside the reached margin.
        adapter.state.tool_temp_actual = 200.0
        assert wd.step() is None

        assert not _reds(_creep(wd, adapter, clock, 1200.0))
        assert adapter.emergency_stops == 0

    def test_the_ceiling_hands_back_to_the_ordinary_drop_rule(self):
        """Past the ceiling the verdict belongs to the calibrated threshold."""
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 200.0
        wd.step()
        assert not _reds(_creep(wd, adapter, clock, 600.0))

        adapter.state.tool_temp_actual = 220.0 - (DEFAULT_TOOL_DROP_C + 5.0)
        flag = wd.step()

        assert flag is not None
        assert flag.rule == "tool_drop"

    def test_the_warning_reaches_the_caller_before_anything_stops(self):
        """Say something long before doing anything — and say it to someone.

        This is the half that only works because reporting and stopping are
        separate: before that split a yellow flag went to a list nothing
        reads, so a fifteen-minute warmup would have warned nobody.
        """
        wd, adapter, clock, anomalies = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()

        first_half = _creep(wd, adapter, clock, 600.0 * 0.5 + _CREEP_STEP_S)

        assert [f.rule for f in anomalies] == ["tool_warmup_slow"]
        assert anomalies[0].kind == "yellow"
        assert not _reds(first_half), "a warning must not stop the print"
        assert adapter.emergency_stops == 0

        # ...and it is said once, not every poll for fifteen minutes.  Stay
        # short of the ceiling: crossing it is supposed to add a red flag.
        _creep(wd, adapter, clock, _CREEP_STEP_S)
        assert [f.rule for f in anomalies].count("tool_warmup_slow") == 1

    def test_a_normal_warmup_never_reaches_the_ceiling(self):
        wd, adapter, clock, anomalies = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.bed_temp_target = 60.0

        for i in range(72):  # a three minute ramp
            frac = i / 71
            adapter.state.tool_temp_actual = 25.0 + (220.0 - 25.0) * frac
            adapter.state.bed_temp_actual = 25.0 + (60.0 - 25.0) * frac
            adapter.job.current_layer += 1
            assert wd.step() is None
            clock.advance(DEFAULT_POLL_INTERVAL)

        assert anomalies == []
        assert adapter.emergency_stops == 0

    def test_reaching_the_target_clears_the_ceiling(self):
        """Arriving late is not a fault; the clock is about never arriving."""
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()
        _creep(wd, adapter, clock, 300.0)

        adapter.state.tool_temp_actual = 220.0  # arrives, late but arrives
        assert wd.step() is None
        clock.advance(1800.0)
        adapter.job.current_layer += 1

        assert wd.step() is None
        assert adapter.emergency_stops == 0

    # -- anti-evasion: the clock must not be restartable ------------------

    def test_toggling_the_heater_off_does_not_restart_the_ceiling(self):
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()
        _creep(wd, adapter, clock, 500.0)

        adapter.state.tool_temp_target = MIN_ACTIVE_TARGET_C - 1.0  # M104 S0
        adapter.job.current_layer += 1
        assert wd.step() is None
        adapter.state.tool_temp_target = 220.0
        adapter.job.current_layer += 1

        reds = _reds(_creep(wd, adapter, clock, 200.0))

        assert reds, "toggling the heater must not buy another full grace"
        assert reds[0].rule == "tool_warmup_timeout"

    def test_changing_the_target_does_not_restart_the_ceiling(self):
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()
        _creep(wd, adapter, clock, 500.0)

        adapter.state.tool_temp_target = 240.0  # never reached either
        adapter.job.current_layer += 1

        assert _reds(_creep(wd, adapter, clock, 200.0))

    def test_a_second_print_starts_its_own_clock(self):
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()
        _creep(wd, adapter, clock, 500.0)

        adapter.state.state = "idle"
        assert wd.step() is None
        clock.advance(3600.0)
        adapter.state.state = "printing"
        adapter.state.tool_temp_actual = 25.0

        assert not _reds(_creep(wd, adapter, clock, 500.0))

    def test_the_bed_has_its_own_ceiling(self):
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.bed_temp_target = 110.0
        adapter.state.bed_temp_actual = 20.0
        assert wd.step() is None

        reds = _reds(_creep(wd, adapter, clock, 600.0, heater="bed"))

        assert reds
        assert reds[0].rule == "bed_warmup_timeout"

    # -- contract details ------------------------------------------------

    def test_the_ceiling_names_itself_distinctly(self):
        """'Dropped below setpoint' would describe the wrong fault.

        A heater that never started is a different problem from one that
        died mid-print, and the user's next move differs.  Pinned so it
        cannot quietly regress into reusing the drop rule's voice.
        """
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=600.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()

        flag = _reds(_creep(wd, adapter, clock, 600.0))[0]

        assert flag.rule != "tool_drop"
        assert "never reached" in flag.message.lower()
        assert "dropped" not in flag.message.lower()
        assert flag.context["tool_temp_target"] == 220.0
        assert flag.context["warming_seconds"] >= 600.0

    def test_the_default_ceiling_is_generous(self):
        """A policy choice about tolerating ambiguity, not a physical constant.

        It must comfortably clear the slowest legitimate warmup we know of —
        a large enclosed bed reaching an ABS temperature in a cold room,
        which runs 15-25 minutes.
        """
        assert DEFAULT_WARMUP_TIMEOUT_S >= 1500.0
        assert 0.0 < WARMUP_WARN_FRACTION < 1.0

    def test_the_ceiling_is_configurable(self):
        """Big enclosed machines legitimately warm slower than desktop ones."""
        wd, adapter, clock, _ = _make_watchdog(warmup_timeout_s=300.0)
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()

        assert _reds(_creep(wd, adapter, clock, 300.0))


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-q"])
