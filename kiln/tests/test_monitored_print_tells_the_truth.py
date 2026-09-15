"""A monitored print that the printer took is a success, and its monitor runs.

``start_monitored_print`` started the print, then built its first-layer
monitor with a policy and a constructor call that no longer matched the class,
and called ``start()`` on a class that had no such method.  Every successful
call therefore reported an internal error for a print that was running, and
the monitor never ran.  An agent told "failed" about a running print sends the
same file again onto an occupied bed.  The ``kiln watch`` command had the same
broken calls.

These tests drive the real tool functions with a real adapter.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import pytest
from click.testing import CliRunner

from kiln import server
from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy, MonitorResult
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


class _Bench(PrinterAdapter):
    """A real adapter that talks to nothing.

    Real, because the start template and the status wrap are inherited from
    ``PrinterAdapter``; a MagicMock skips both.
    """

    def __init__(self, host: str = "192.0.2.20", *, accepts: bool = True) -> None:
        self.host = host
        self.serial = ""
        self._accepts = accepts
        self._status = PrinterStatus.IDLE
        self.started: list[str] = []

    @property
    def name(self) -> str:
        return "bench"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(
            connected=True,
            state=self._status,
            tool_temp_actual=215.0,
            tool_temp_target=215.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )

    def get_job(self) -> JobProgress:
        return JobProgress(
            file_name=self.started[-1] if self.started else None,
            completion=3.0 if self.started else None,
        )

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1024)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="uploaded")

    def delete_file(self, file_name: str) -> bool:
        return True

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        if not self._accepts:
            return PrintResult(success=False, message="The printer refused the job.")
        self.started.append(file_name)
        self._status = PrinterStatus.PRINTING
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="cancelled")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="paused")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="resumed")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="stopped")

    def send_gcode(self, commands: Any) -> bool:
        return True

    def set_tool_temp(self, celsius: float, tool: int = 0) -> bool:
        return True

    def set_bed_temp(self, celsius: float) -> bool:
        return True

    def _load_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _unload_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _purge_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError


class _NullHeaterWatchdog:
    @staticmethod
    def notify_print_started() -> None:
        pass

    @staticmethod
    def notify_print_ended() -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_adapter", None)
    monkeypatch.setattr(server, "_first_layer_monitors", {})
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
    monkeypatch.setattr(
        server, "preflight_check", lambda **_: {"ready": True, "summary": "ready"}
    )
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")


def _tools() -> dict[str, Any]:
    from kiln.plugins import monitoring_tools

    tools: dict[str, Any] = {}

    class _Registrar:
        def tool(self, *args: Any, **kwargs: Any):
            def decorator(fn):
                tools[kwargs.get("name") or fn.__name__] = fn
                return fn

            return decorator

        def __getattr__(self, name: str):
            return lambda *a, **k: (lambda fn: fn)

    monitoring_tools.plugin.register(_Registrar())
    return tools


def _workshop(**kwargs: Any) -> _Bench:
    bench = _Bench(**kwargs)
    server._get_registry().register("workshop", bench)
    return bench


def _start(tools: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return tools["start_monitored_print"](
        file_name="part.gcode",
        printer_name="workshop",
        first_layer_delay=0,
        first_layer_checks=1,
        first_layer_interval=0,
        **kwargs,
    )


def test_a_print_the_printer_took_is_a_success_and_its_monitor_runs():
    bench = _workshop()
    tools = _tools()

    result = _start(tools)

    assert result["success"] is True
    assert result["print_start"] == "started"
    assert bench.started == ["part.gcode"]
    assert result["monitor_status"] == "started"
    assert "warning" not in result
    monitor = server._first_layer_monitors[result["monitor_id"]]
    monitor._thread.join(timeout=10)
    status = tools["first_layer_status"](result["monitor_id"])
    assert status["success"] is True
    assert status["finished"] is True
    assert status["outcome"] == "passed"  # a camera-less printer gets the telemetry checks


def test_first_layer_status_reads_a_running_monitor_then_its_result(monkeypatch):
    release = threading.Event()

    def _held_until_released(self, *, monitoring_mode=None):
        release.wait(10)
        return MonitorResult(success=True, outcome="passed", message="checked")

    monkeypatch.setattr(FirstLayerMonitor, "monitor", _held_until_released)
    _workshop()
    tools = _tools()
    result = _start(tools)
    monitor_id = result["monitor_id"]

    running = tools["first_layer_status"](monitor_id)
    assert running["success"] is True
    assert running["finished"] is False

    release.set()
    server._first_layer_monitors[monitor_id]._thread.join(timeout=10)
    done = tools["first_layer_status"](monitor_id)
    assert done["finished"] is True
    assert done["outcome"] == "passed"
    assert monitor_id not in server._first_layer_monitors  # read once, then retired


def test_a_monitor_that_cannot_start_leaves_a_success_with_a_warning(monkeypatch):
    def _cannot_start(self, *, monitoring_mode=None):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(FirstLayerMonitor, "start", _cannot_start, raising=False)
    bench = _workshop()

    result = _start(_tools())

    assert result["success"] is True
    assert bench.started == ["part.gcode"]
    assert result["monitor_status"] == "not_started"
    assert "could not start" in result["warning"]
    assert "do not start it again" in result["message"]
    assert server._first_layer_monitors == {}


def test_a_refused_start_is_a_failure_and_starts_no_monitor():
    bench = _workshop(accepts=False)

    result = _start(_tools())

    assert result["success"] is False
    assert result["print_start"] == "failed"
    assert "did not start part.gcode" in result["message"]
    assert bench.started == []
    assert server._first_layer_monitors == {}


def test_a_session_that_crashes_ends_as_an_error_result(monkeypatch):
    def _crash(self, *, monitoring_mode=None):
        raise RuntimeError("snapshot pipeline broke")

    monkeypatch.setattr(FirstLayerMonitor, "monitor", _crash)
    monitor = FirstLayerMonitor(_Bench(), "workshop", policy=MonitorPolicy())

    monitor.start()
    monitor._thread.join(timeout=10)

    outcome = monitor.result()
    assert outcome is not None
    assert outcome.outcome == "error"
    assert "snapshot pipeline broke" in outcome.message
    assert monitor.running is False


def test_starting_twice_runs_one_session(monkeypatch):
    release = threading.Event()
    calls: list[int] = []

    def _counted(self, *, monitoring_mode=None):
        calls.append(1)
        release.wait(10)
        return MonitorResult(success=True, outcome="passed")

    monkeypatch.setattr(FirstLayerMonitor, "monitor", _counted)
    monitor = FirstLayerMonitor(_Bench(), "workshop", policy=MonitorPolicy())

    monitor.start()
    first = monitor._thread
    monitor.start()
    assert monitor._thread is first
    release.set()
    first.join(timeout=10)
    assert calls == [1]


def test_finished_monitors_do_not_accumulate_without_bound(monkeypatch):
    from kiln.plugins import monitoring_tools

    class _Filed:
        def __init__(self, finished: bool) -> None:
            self._finished = finished

        def result(self):
            return MonitorResult(success=True, outcome="passed") if self._finished else None

    monkeypatch.setattr(monitoring_tools, "_MAX_FINISHED_MONITORS", 2)
    server._first_layer_monitors.update(
        {"old": _Filed(True), "older-running": _Filed(False), "mid": _Filed(True), "new": _Filed(True)}
    )

    monitoring_tools._file_monitor("fresh", _Filed(False))

    assert list(server._first_layer_monitors) == ["older-running", "new", "fresh"]


def _cli_watch(monkeypatch, *args: str) -> Any:
    from kiln.cli import main as cli_main

    bench = _Bench()
    bench.started.append("part.gcode")
    bench._status = PrinterStatus.PRINTING
    monkeypatch.setattr(
        cli_main,
        "load_printer_config",
        lambda printer=None: {"type": "octoprint", "host": "http://192.0.2.20", "name": "workshop"},
    )
    monkeypatch.setattr(cli_main, "validate_printer_config", lambda cfg: (True, None))
    monkeypatch.setattr(cli_main, "_make_adapter", lambda cfg: bench)
    return CliRunner().invoke(
        cli_main.cli,
        ["watch", "--delay", "0", "--checks", "1", "--interval", "0", *args],
    )


def test_kiln_watch_runs_the_monitor_and_reports_it_in_json(monkeypatch):
    run = _cli_watch(monkeypatch, "--json")

    assert run.exit_code == 0, run.output
    payload = json.loads(run.output)
    assert payload["status"] == "success"
    assert payload["data"]["outcome"] == "passed"


def test_kiln_watch_reports_the_outcome_in_words(monkeypatch):
    run = _cli_watch(monkeypatch)

    assert run.exit_code == 0, run.output
    assert "Outcome: passed" in run.output
    assert "Elapsed:" in run.output
