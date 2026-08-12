"""Tests for :class:`kiln.print_watchdog.PrintWatchdog`.

These tests drive the watchdog through its :meth:`step` entry point so
no real threads are needed for the red-flag logic.  The thread
lifecycle itself is exercised in a dedicated test at the bottom.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from kiln.print_watchdog import (
    DEFAULT_BED_DROP_C,
    DEFAULT_NO_RISE_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STALL_SECONDS,
    DEFAULT_TOOL_DROP_C,
    HEATING_RISE_C,
    MIN_ACTIVE_TARGET_C,
    REACHED_MARGIN_C,
    Flag,
    PrintWatchdog,
)


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


class FakeAdapter:
    """Records calls to ``emergency_stop`` and lets tests mutate state."""

    def __init__(self, state: FakeState | None = None, job: FakeJob | None = None):
        self.state = state if state is not None else FakeState()
        self.job = job if job is not None else FakeJob()
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

    def emergency_stop(self) -> bool:
        self.emergency_stops += 1
        return True


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
        wd, adapter, _, anomalies = _make_watchdog()
        adapter.state.print_error = 0x03008014  # Bambu HMS code

        flag = wd.step()

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

    def test_stall_timer_holds_while_warming_then_releases(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.tool_temp_target = 220.0
        adapter.state.tool_temp_actual = 25.0
        wd.step()

        clock.advance(DEFAULT_STALL_SECONDS + 30.0)
        adapter.state.tool_temp_actual = 100.0  # still climbing
        assert wd.step() is None  # no stall while warming

        adapter.state.tool_temp_actual = 220.0  # arrived; the hold ends
        wd.step()
        clock.advance(DEFAULT_STALL_SECONDS + 1.0)

        flag = wd.step()

        assert flag is not None
        assert flag.rule == "stalled_layer"

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


class TestStalledLayer:
    def test_stalls_trigger_after_threshold_expires(self):
        wd, adapter, clock, _ = _make_watchdog()
        # First tick establishes baseline progress.
        wd.step()
        assert wd.anomaly_triggered is False

        # Hold the printer at the same layer; advance past stall threshold.
        clock.advance(DEFAULT_STALL_SECONDS + 1.0)
        flag = wd.step()

        assert flag is not None
        assert flag.rule == "stalled_layer"
        assert adapter.emergency_stops == 1
        assert flag.context["stalled_seconds"] >= DEFAULT_STALL_SECONDS

    def test_layer_advance_resets_stall_timer(self):
        wd, adapter, clock, _ = _make_watchdog()
        wd.step()  # baseline

        # Advance almost to the stall threshold, but bump the layer.
        clock.advance(DEFAULT_STALL_SECONDS - 1.0)
        adapter.job.current_layer += 1
        adapter.job.completion = 20.0
        assert wd.step() is None

        # Now advance past the original threshold — should NOT trip
        # because the counter reset on the layer bump.
        clock.advance(DEFAULT_STALL_SECONDS - 1.0)
        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0

    def test_no_stall_check_when_not_printing(self):
        wd, adapter, clock, _ = _make_watchdog()
        adapter.state.state = "paused"
        wd.step()

        clock.advance(DEFAULT_STALL_SECONDS * 5)
        flag = wd.step()

        assert flag is None
        assert adapter.emergency_stops == 0


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
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.print_error = 0x03008014

        wd.step()
        wd.step()
        wd.step()

        assert adapter.emergency_stops == 1
        assert wd.anomaly_triggered is True

    def test_callback_invoked_once_per_trip(self):
        calls: list[Flag] = []

        def cb(flag: Flag) -> None:
            calls.append(flag)

        wd, adapter, _, _ = _make_watchdog(on_anomaly=cb)
        adapter.state.print_error = 42

        wd.step()
        wd.step()
        wd.step()

        # on_anomaly is wrapped by _make_watchdog; the `calls` list is the
        # user-supplied outer callback, so it should fire once.
        assert len(calls) == 1

    def test_status_reports_trip_after_anomaly(self):
        wd, adapter, _, _ = _make_watchdog()
        adapter.state.print_error = 1
        wd.step()

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

    def test_estop_failure_still_latches_and_invokes_callback(self):
        calls: list[Flag] = []

        def cb(flag: Flag) -> None:
            calls.append(flag)

        wd, adapter, _, _ = _make_watchdog(on_anomaly=cb)

        def explode() -> None:
            raise RuntimeError("mqtt disconnected")

        adapter.emergency_stop = explode  # type: ignore[assignment]
        adapter.state.print_error = 1

        wd.step()

        assert wd.anomaly_triggered is True
        assert len(calls) == 1
        assert calls[0].rule == "print_error"


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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-q"])
