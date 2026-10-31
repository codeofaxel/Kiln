"""A heater or fan routine Kiln starts reaches its terminal state whether
or not the caller is still listening.

Measured 2026-09-18 on an A1: ``purge_filament`` had switched the heater
off and the part fan on full for the served cool-down, the client gave up
on the request, the host restarted the server mid-wait, and the fan-off
never went out.  Three layers now carry that fan-off: the cool-down runs
in a thread the request cannot take with it; the shutdown path settles
whatever is still held; and a marker on disk lets a running server's
background check take over what a killed process began.  A status read
only reports the marker: reading a printer sends it nothing.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from kiln.printers import routine_ledger as ledger
from kiln.printers.motion_plan import run_finish

FINISH = {"fan_on": "M106 S255", "handoff_c": 140, "timeout_s": 150, "fan_off": "M106 S0", "over_chute": True}


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path))
    ledger._holds.clear()
    ledger._threads.clear()
    yield
    ledger.wait_settled(5.0)
    ledger._holds.clear()
    ledger._threads.clear()


class _Adapter:
    """Records G-code; answers the cooling waiter from a scripted series."""

    name = "stub"
    _host = "10.0.0.9"

    def __init__(self, readings, *, on_wait=None):
        self.readings = list(readings)
        self.gcode: list[list[str]] = []
        self.temps: list[float] = []
        self.on_wait = on_wait

    def send_gcode(self, lines):
        self.gcode.append(list(lines))
        return True

    def set_tool_temp(self, target):
        self.temps.append(target)
        return True

    def _wait_for_hotend_below(self, threshold, *, timeout, poll=0.0):
        reads = 1 if timeout == 0 else max(1, len(self.readings))
        if timeout and self.on_wait is not None:
            self.on_wait()
        last = None
        for _ in range(reads):
            if self.readings:
                last = self.readings.pop(0)
            if last is not None and last <= threshold:
                return True, last
        return False, last


def _result():
    return SimpleNamespace(details={"purge_station": {"status": "parked"}})


class TestTheCoolDownOutlivesTheRequest:
    def test_the_answer_leaves_with_the_fan_on_and_the_thread_sends_the_fan_off(self):
        adapter = _Adapter([200, 180, 150, 138])
        result = _result()
        sentence = run_finish(adapter, result, FINISH)
        assert "turns the fan off on its own" in sentence and "read 200 °C when this answer left" in sentence
        assert result.details["fan"].startswith("on full (cooling")
        assert result.details["cooldown"]["status"] == "running" and result.details["cooled_below_c"] is None
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]]
        assert ledger.open_holds() == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None

    def test_a_nozzle_already_at_the_handoff_gets_its_fan_off_in_the_answer(self):
        adapter = _Adapter([120])
        result = _result()
        sentence = run_finish(adapter, result, FINISH)
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]]
        assert result.details["fan"] == "off" and result.details["cooled_below_c"] == 140
        assert "already read 120 °C" in sentence and "over the chute" in sentence
        assert ledger.open_holds() == []

    def test_a_watch_that_never_sees_the_handoff_leaves_the_fan_on_and_the_hold_open(self, monkeypatch):
        monkeypatch.setattr(ledger, "LATE_POLL_S", 0.01)
        monkeypatch.setattr(ledger, "MARKER_MAX_AGE_S", 0.2)
        adapter = _Adapter([200, 190])  # never reaches 140
        run_finish(adapter, _result(), FINISH)
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"]]
        assert ledger.open_holds() == ["part fan on for the cool-down to 140 °C"]
        assert ledger.stranded_cooldown(ledger.printer_key(adapter), max_age_s=60) is not None

    def test_a_watch_past_its_first_wait_keeps_looking_and_turns_the_fan_off(self, monkeypatch):
        """The first wait runs out above the hand-off; the nozzle gets there
        later.  The fan-off still goes out, with nobody reading the printer."""
        monkeypatch.setattr(ledger, "LATE_POLL_S", 0.01)
        adapter = _Adapter([200, 190])
        run_finish(adapter, _result(), FINISH)
        adapter.readings.extend([170, 150, 139])
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]]
        assert ledger.open_holds() == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None


class TestTheShutdownSettlesWhatIsHeld:
    def test_drain_sends_every_fan_off_and_clears_the_marker(self):
        gate = threading.Event()
        adapter = _Adapter([200, 200], on_wait=gate.wait)
        run_finish(adapter, _result(), FINISH)
        assert ledger.open_holds() == ["part fan on for the cool-down to 140 °C"]
        settled = ledger.drain("shutdown")
        assert settled == ["part fan on for the cool-down to 140 °C"]
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]]
        assert ledger.open_holds() == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None
        gate.set()
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]], "the thread must not send the fan-off twice"

    def test_a_running_print_is_never_touched_by_a_settle(self):
        """A print started during the cool-down owns the fan from there."""
        gate = threading.Event()
        adapter = _Adapter([200, 200], on_wait=gate.wait)
        adapter.get_state = lambda: SimpleNamespace(state=SimpleNamespace(value="printing"))
        run_finish(adapter, _result(), FINISH)
        assert ledger.drain("shutdown") == []
        assert adapter.gcode == [["M106 S255"]] and ledger.open_holds() == []
        gate.set()
        assert ledger.wait_settled(5.0) and adapter.gcode == [["M106 S255"]]

    def test_the_thread_leaves_a_print_that_started_meanwhile_alone(self):
        adapter = _Adapter([200, 130])
        adapter.get_state = lambda: SimpleNamespace(state=SimpleNamespace(value="printing"))
        run_finish(adapter, _result(), FINISH)
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"]] and ledger.open_holds() == []

    def test_one_refused_settle_skips_nothing(self):
        def _refuse():
            raise RuntimeError("no connection")

        sent: list[str] = []
        ledger.hold("heater", "a@1", _refuse)
        ledger.hold("fan", "a@1", lambda: sent.append("M106 S0"))
        assert ledger.drain("shutdown") == ["fan"]
        assert sent == ["M106 S0"] and ledger.open_holds() == []


class TestTheCommandLineDoorStaysForTheCoolDown:
    """``kiln filament …`` is a one-shot process: it has no client timeout
    and would take the cool-down thread with it, so it waits."""

    def test_the_answer_is_printed_then_the_process_waits_for_the_fan_off(self, capsys):
        from kiln.cli.main import _emit_filament_result

        adapter = _Adapter([200, 180, 138])
        result = _result()
        run_finish(adapter, result, FINISH)
        payload = {"success": True, "message": "purged", "details": result.details}
        _emit_filament_result(payload, json_mode=True)
        captured = capsys.readouterr()
        assert '"success": true' in captured.out.lower()
        assert "Cooling: the part fan is on until the nozzle reads 140 °C" in captured.err
        assert "Fan off." in captured.err
        assert adapter.gcode == [["M106 S255"], ["M106 S0"]] and ledger.open_holds() == []

    def test_a_watch_that_runs_out_says_what_finishes_it(self, capsys, monkeypatch):
        from kiln.cli.main import _emit_filament_result

        monkeypatch.setattr(ledger, "LATE_POLL_S", 0.01)
        monkeypatch.setattr(ledger, "MARKER_MAX_AGE_S", 0.2)
        adapter = _Adapter([200, 190])
        result = _result()
        run_finish(adapter, result, FINISH)
        _emit_filament_result({"success": True, "details": result.details}, json_mode=True)
        assert "the fan is left on" in capsys.readouterr().err
        assert adapter.gcode == [["M106 S255"]]

    def test_nothing_to_wait_for_returns_at_once(self, capsys):
        from kiln.cli.main import _emit_filament_result

        _emit_filament_result({"success": True, "details": {"fan": "not driven"}}, json_mode=True)
        assert capsys.readouterr().err == ""


class TestAReadReportsAStrandedCoolDownAndSendsNothing:
    """A status read only says what a stopped process left; it never sends
    the fan-off and never touches the marker."""

    def _state(self, temp, status="idle"):
        return SimpleNamespace(tool_temp_actual=temp, state=SimpleNamespace(value=status))

    def test_below_the_handoff_it_says_the_fan_can_go_off_and_sends_nothing(self):
        adapter = _Adapter([])
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        before = ledger._marker_path().read_bytes()
        block = ledger.describe_stranded_cooldown(adapter, self._state(70.8))
        assert block["status"] == "fan_left_on" and "set_fan with percent 0" in block["note"]
        assert adapter.gcode == []
        assert ledger._marker_path().read_bytes() == before

    def test_above_the_handoff_it_says_cooling_and_sends_nothing(self):
        adapter = _Adapter([])
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        block = ledger.describe_stranded_cooldown(adapter, self._state(180.0))
        assert block["status"] == "cooling" and adapter.gcode == []

    def test_a_printing_machine_gets_no_block_and_the_marker_is_left_alone(self):
        adapter = _Adapter([])
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        assert ledger.describe_stranded_cooldown(adapter, self._state(70.0, "printing")) is None
        assert adapter.gcode == [] and ledger.stranded_cooldown(key) is not None

    def test_an_expired_marker_is_not_reported_and_not_cleared_by_the_read(self, monkeypatch):
        adapter = _Adapter([])
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        monkeypatch.setattr(ledger.time, "time", lambda: 10**12)
        assert ledger.describe_stranded_cooldown(adapter, self._state(70.0)) is None
        assert key in ledger._read_marker()

    def test_a_cool_down_still_running_here_is_left_to_its_thread(self):
        gate = threading.Event()
        adapter = _Adapter([200, 200], on_wait=gate.wait)
        run_finish(adapter, _result(), FINISH)
        assert ledger.describe_stranded_cooldown(adapter, self._state(70.0)) is None
        assert adapter.gcode == [["M106 S255"]]
        gate.set()

    def test_a_cool_down_another_live_process_is_watching_is_not_reported(self):
        adapter = _Adapter([])
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        data = ledger._read_marker()
        data[key]["pid"] = os.getppid()  # alive: the process that started this test run
        ledger._write_marker(data)
        assert ledger.describe_stranded_cooldown(adapter, self._state(70.0)) is None
        assert ledger.adopt_stranded_cooldown(adapter) is None and adapter.gcode == []

    def test_nothing_on_file_means_no_block(self):
        assert ledger.describe_stranded_cooldown(_Adapter([]), self._state(70.0)) is None

    def test_on_windows_the_owner_check_never_signals_a_process(self, monkeypatch):
        """Signal 0 is CTRL_C_EVENT on Windows: ``os.kill(pid, 0)`` there would
        interrupt the process it only meant to ask about."""
        asked: list[int] = []

        def _no_kill(*_args):
            raise AssertionError("os.kill must not be called on Windows")

        monkeypatch.setattr(ledger.os, "name", "nt")
        monkeypatch.setattr(ledger.os, "kill", _no_kill)
        monkeypatch.setattr(ledger, "_pid_running_windows", lambda pid: asked.append(pid) or True)
        assert ledger._owner_alive({"pid": 424242}) is True
        assert asked == [424242]

    def test_kiln_status_reports_it_and_sends_nothing(self, monkeypatch):
        """The command-line read is its own door onto the adapter."""
        import json

        from click.testing import CliRunner

        import kiln.cli.main as cli_main
        from kiln.printers.base import JobProgress, PrinterState, PrinterStatus

        adapter = _Adapter([])
        adapter.get_state = lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=70.8)
        adapter.get_job = lambda: JobProgress()
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        monkeypatch.setattr(cli_main, "_get_adapter_from_ctx", lambda ctx: adapter)
        out = CliRunner().invoke(cli_main.cli, ["status", "--json"])
        assert out.exit_code == 0, out.output
        payload = json.loads(out.output)
        block = payload.get("cooldown") or (payload.get("data") or {}).get("cooldown")
        assert block["status"] == "fan_left_on", out.output
        assert adapter.gcode == []

    def test_printer_status_carries_the_block_and_sends_nothing(self, monkeypatch):
        from kiln import server
        from kiln.printers.base import JobProgress, PrinterState, PrinterStatus

        adapter = _Adapter([])
        adapter.capabilities = SimpleNamespace(to_dict=lambda: {})
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        state = PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=70.8)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(server, "read_status", lambda a: (state, JobProgress()))
        monkeypatch.setattr("kiln.nozzle_clumping_detection.read_switch", lambda a: None)
        out = server.printer_status(detail="lite")
        assert out["success"] is True
        assert out["cooldown"]["status"] == "fan_left_on"
        assert adapter.gcode == []

    def test_printer_status_is_labelled_read_only(self):
        import asyncio

        from kiln import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        hints = tools["printer_status"].annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False


class TestARunningServerTakesOverAStrandedCoolDown:
    """The fan-off a killed process owed goes out from a server's background
    check, through the same watch a live cool-down uses."""

    def _dead_pid(self):
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def _strand(self, adapter, *, pid=None):
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        data = ledger._read_marker()
        data[key]["pid"] = self._dead_pid() if pid is None else pid
        ledger._write_marker(data)
        return key

    def test_a_cool_nozzle_gets_its_fan_off_and_the_marker_clears(self):
        adapter = _Adapter([70.8])
        key = self._strand(adapter)
        assert ledger.adopt_stranded_cooldown(adapter) == "adopted"
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S0"]]
        assert ledger.stranded_cooldown(key) is None and ledger.open_holds() == []

    def test_a_hot_nozzle_is_watched_until_the_handoff(self):
        adapter = _Adapter([190, 160, 138])
        self._strand(adapter)
        assert ledger.adopt_stranded_cooldown(adapter) == "adopted"
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S0"]]

    def test_a_printing_machine_keeps_its_fan_and_the_marker_clears(self):
        adapter = _Adapter([70.0])
        adapter.get_state = lambda: SimpleNamespace(state=SimpleNamespace(value="printing"))
        key = self._strand(adapter)
        assert ledger.adopt_stranded_cooldown(adapter) == "left_to_print"
        assert adapter.gcode == [] and ledger.stranded_cooldown(key) is None

    def test_a_marker_from_before_owners_were_recorded_counts_as_stranded(self):
        adapter = _Adapter([70.0])
        key = self._strand(adapter)
        data = ledger._read_marker()
        data[key].pop("pid")
        ledger._write_marker(data)
        assert ledger.adopt_stranded_cooldown(adapter) == "adopted"
        assert ledger.wait_settled(5.0) and adapter.gcode == [["M106 S0"]]

    def test_the_background_check_builds_no_adapter_while_nothing_is_on_file(self):
        asked = threading.Event()

        def _adapters():
            asked.set()
            return []

        stop = ledger.start_stranded_watch(_adapters, interval_s=0.01)
        try:
            assert not asked.wait(0.2)
        finally:
            stop.set()

    def test_the_background_check_finishes_a_stranded_cool_down(self):
        adapter = _Adapter([70.0])
        key = self._strand(adapter)
        stop = ledger.start_stranded_watch(lambda: [adapter], interval_s=0.01)
        try:
            deadline = time.monotonic() + 5.0
            while adapter.gcode != [["M106 S0"]] and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S0"]] and ledger.stranded_cooldown(key) is None

    def test_the_server_starts_the_background_check(self):
        import inspect

        from kiln import bridge_client, server

        assert "start_stranded_watch(_stranded_cooldown_adapters)" in inspect.getsource(server._start)
        assert "_start_stranded_cooldown_watch()" in inspect.getsource(bridge_client.run_bridge)


class TestAFanCommandEndsKilnsClaimOnTheFan:
    def test_a_live_cool_down_is_released_without_its_fan_off(self):
        gate = threading.Event()
        adapter = _Adapter([200, 130], on_wait=gate.wait)
        run_finish(adapter, _result(), FINISH)
        ledger.fan_taken_over(adapter)
        gate.set()
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"]] and ledger.open_holds() == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None

    def test_set_fan_on_the_part_fan_clears_a_stranded_marker(self, monkeypatch):
        from kiln import server

        adapter = _Adapter([])
        adapter.set_fan = lambda node, percent: True
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(server, "_check_auth", lambda scope: None)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        out = server.set_fan(percent=0)
        assert out["success"] is True, out
        assert key not in ledger._read_marker()

    def test_set_fan_on_another_fan_leaves_the_cool_down_alone(self, monkeypatch):
        from kiln import server

        adapter = _Adapter([])
        adapter.set_fan = lambda node, percent: True
        key = ledger.printer_key(adapter)
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
        monkeypatch.setattr(server, "_check_auth", lambda scope: None)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        server.set_fan(percent=100, node="chamber")
        assert key in ledger._read_marker()
