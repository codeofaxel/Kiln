"""A heater or fan routine Kiln starts reaches its terminal state whether
or not the caller is still listening.

Measured 2026-09-18 on an A1: ``purge_filament`` had switched the heater
off and the part fan on full for the served cool-down, the client gave up
on the request, the host restarted the server mid-wait, and the fan-off
never went out.  Three layers now carry that fan-off: the cool-down runs
in a thread the request cannot take with it; the shutdown path settles
whatever is still held; and a marker on disk lets the next status read
on any server finish what a killed one began.
"""

from __future__ import annotations

import threading
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

    def test_a_watch_that_runs_out_leaves_the_fan_on_and_the_hold_open(self):
        adapter = _Adapter([200, 190])  # never reaches 140
        run_finish(adapter, _result(), FINISH)
        assert ledger.wait_settled(5.0)
        assert adapter.gcode == [["M106 S255"]]
        assert ledger.open_holds() == ["part fan on for the cool-down to 140 °C"]
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is not None


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

    def test_a_watch_that_runs_out_says_what_finishes_it(self, capsys):
        from kiln.cli.main import _emit_filament_result

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


class TestTheNextReadFinishesAStrandedCoolDown:
    """The marker outlives a killed server; the read every filament answer
    names as its follow-up completes the cool-down from it."""

    def _state(self, temp, status="idle"):
        return SimpleNamespace(tool_temp_actual=temp, state=SimpleNamespace(value=status))

    def test_below_the_handoff_the_fan_goes_off_and_the_marker_clears(self):
        adapter = _Adapter([])
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        block = ledger.complete_stranded_cooldown(adapter, self._state(70.8))
        assert block["status"] == "finished" and "turned off" in block["note"]
        assert adapter.gcode == [["M106 S0"]]
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None

    def test_above_the_handoff_it_says_so_and_sends_nothing(self):
        adapter = _Adapter([])
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        block = ledger.complete_stranded_cooldown(adapter, self._state(180.0))
        assert block["status"] == "cooling" and adapter.gcode == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is not None

    def test_a_printing_machine_keeps_its_own_fan(self):
        adapter = _Adapter([])
        ledger.note_cooldown(ledger.printer_key(adapter), fan_off="M106 S0", handoff_c=140)
        assert ledger.complete_stranded_cooldown(adapter, self._state(70.0, "printing")) is None
        assert adapter.gcode == []
        assert ledger.stranded_cooldown(ledger.printer_key(adapter)) is None

    def test_a_cool_down_still_running_here_is_left_to_its_thread(self):
        gate = threading.Event()
        adapter = _Adapter([200, 200], on_wait=gate.wait)
        run_finish(adapter, _result(), FINISH)
        assert ledger.complete_stranded_cooldown(adapter, self._state(70.0)) is None
        assert adapter.gcode == [["M106 S255"]]
        gate.set()

    def test_a_heater_hold_on_the_same_printer_does_not_hide_a_stranded_marker(self):
        adapter = _Adapter([])
        key = ledger.printer_key(adapter)
        ledger.hold("hotend at 210 °C", key, lambda: None, kind="heater")
        ledger.note_cooldown(key, fan_off="M106 S0", handoff_c=140)
        block = ledger.complete_stranded_cooldown(adapter, self._state(70.0))
        assert block["status"] == "finished" and adapter.gcode == [["M106 S0"]]

    def test_nothing_on_file_means_no_block(self):
        assert ledger.complete_stranded_cooldown(_Adapter([]), self._state(70.0)) is None

    def test_kiln_status_finishes_it_too(self, monkeypatch):
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
        assert block["status"] == "finished", out.output
        assert adapter.gcode == [["M106 S0"]]

    def test_printer_status_carries_the_block(self, monkeypatch):
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
        assert out["cooldown"]["status"] == "finished"
        assert adapter.gcode == [["M106 S0"]]
