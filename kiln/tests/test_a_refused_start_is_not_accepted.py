"""A start Kiln refused itself is a refusal, never "accepted".

``PrinterAdapter.start_print`` is a template: before it sends anything it
runs the pre-print safety gate and the sign-off backstop, and either may
turn the start away.  Until this was fixed the template answered a refusal
with the same ``PrintResult(success=False)`` an adapter returns for a
command that WAS sent and then looked failed -- and ``resolve_print_start``
doubts that second kind on purpose (a printer answering from a push cache
may be running the job).  So when no reading postdated the "command", a
start Kiln had refused, with nothing sent, came back as::

    {"success": true, "print_start": "accepted"}

Seen live on 2026-10-01 against an A1: the safety gate refused a file that
was not ready for the printer, nothing was sent, and the tool said accepted.
Two things followed from the same confusion and are pinned here too: the
scheduler marked the job as printing, and six doors told the heater
watchdog a print had begun, which stops it cooling an idle printer.

The tests go through the real doors: the registered ``start_print`` tool
called by a connected client, and the scheduler's own tick.

A/B: each test names the code it was run against with the fix removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import pytest

from kiln import preview_evidence, print_consent, print_signoff, server
from kiln.print_start_verdict import ACCEPTED, FAILED, resolve_print_start
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

#: A reading this old was made before any command in these tests: the
#: printer's last word, about whatever it was doing before.
LONG_AGO_S = 3600.0


class _Printer(PrinterAdapter):
    """A real adapter subclass, so a start runs the whole template.  It
    answers from a cache, like a printer that pushes its state: every
    reading is older than the command being asked about."""

    def __init__(self, serial: str, status: PrinterStatus = PrinterStatus.IDLE) -> None:
        self.serial = serial
        self.status = status
        self.sent: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(state=self.status, connected=True, state_age_seconds=LONG_AGO_S)

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.sent.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    # The sign-off gate is not what is under test: the audited switch that
    # stands in for a person's yes leaves the safety gate exactly as it is.
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_read_config_printers", lambda: {})
    monkeypatch.setattr(server, "_tool_limiter", type(server._tool_limiter)())
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    server._ensure_internal_tool_plugins_registered()
    yield
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()


@pytest.fixture
def one_at_a_time(monkeypatch):
    """This install runs one printer at a time, and ``workshop`` is busy:
    the pre-print gate refuses a second machine's start.  A real refusal
    from the real gate, made before anything is sent."""
    lic = sys.modules.get("kiln.licensing")
    if lic is None:
        lic = types.ModuleType("kiln.licensing")
        monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
    monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
    monkeypatch.setattr(lic, "max_printers_for_tier", lambda _t: 1, raising=False)
    garage = _Printer("SERIAL-GARAGE")
    workshop = _Printer("SERIAL-WORKSHOP", status=PrinterStatus.PRINTING)
    server._get_registry().register("garage", garage)
    server._get_registry().register("workshop", workshop)
    return garage


@pytest.fixture
def watchdog(monkeypatch):
    """The heater watchdog, watching whichever printer is started."""
    told: list[str] = []
    monkeypatch.setattr(server, "_is_heater_watchdog_machine", lambda adapter: True)
    monkeypatch.setattr(
        server, "_get_heater_watchdog",
        lambda: types.SimpleNamespace(notify_print_started=lambda: told.append("print started")),
    )
    return told


def _client():
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server.mcp)
    return Client(server.mcp)


def _call(tool: str, **arguments) -> dict:
    """The registered tool, called the way a host calls it."""

    async def _one_call():
        async with _client() as client:
            return await client.call_tool(tool, arguments)

    result = asyncio.run(_one_call())
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return json.loads("".join(b.text for b in result.content if getattr(b, "type", None) == "text"))


class TestTheStartTool:
    def test_a_start_the_safety_gate_refused_comes_back_refused(self, one_at_a_time, watchdog):
        """A/B: against the verdict without the ``refused_before_send``
        branch this fails — the answer is ``success: true, accepted``."""
        garage = one_at_a_time
        out = _call("start_print", file_name="part.gcode", printer_name="garage")
        assert garage.sent == [], "nothing was sent to the printer"
        assert out["success"] is False and out["print_start"] == FAILED, out
        assert out["confirmed_running"] is False
        # The gate's own reason is the message: which printer is busy, and why that matters.
        assert "workshop" in out["message"]
        assert out["evidence"]["refused_before_send"] is True
        assert out["evidence"]["adapter_reported_success"] is False

    def test_the_heater_watchdog_is_not_told_a_print_began(self, one_at_a_time, watchdog):
        """Marked busy for a print that never started, the watchdog would
        stop cooling an idle printer.  A/B: with the result not passed to
        ``_note_print_started`` this fails — it is told one began."""
        _call("start_print", file_name="part.gcode", printer_name="garage")
        assert watchdog == []

    def test_a_start_that_was_sent_is_answered_and_notified_as_before(self, watchdog):
        """The control: one printer, nothing in the way.  The command goes
        out, the reading predates it, and ``accepted`` is the honest answer
        — unchanged."""
        garage = _Printer("SERIAL-GARAGE")
        server._get_registry().register("garage", garage)
        out = _call("start_print", file_name="part.gcode", printer_name="garage")
        assert garage.sent == ["part.gcode"]
        assert out["success"] is True and out["print_start"] == ACCEPTED, out
        assert "refused_before_send" not in out["evidence"]
        assert watchdog == ["print started"]


class TestTheScheduler:
    def test_a_refused_job_is_not_marked_as_printing(self, one_at_a_time, tmp_path):
        """A/B: against the verdict without the ``refused_before_send``
        branch this fails — the job is marked printing and announced as
        started, with nothing sent."""
        from kiln.events import EventBus, EventType
        from kiln.queue import PrintQueue
        from kiln.scheduler import JobScheduler

        garage = one_at_a_time
        bus = EventBus()
        started: list = []
        bus.subscribe(EventType.JOB_STARTED, started.append)
        queue = PrintQueue(db_path=str(tmp_path / "q.db"))
        scheduler = JobScheduler(queue, server._get_registry(), bus, poll_interval=0.01)
        job = queue.submit("part.gcode", "garage", "test")
        summary = scheduler.tick()
        assert garage.sent == []
        assert summary["dispatched"] == [], summary
        assert queue.get_job(job).status.value != "printing"
        assert started == []


class TestTheVerdict:
    """The rule itself, at the one function every door asks."""

    def test_refused_before_send_is_failed_and_the_printer_is_not_asked(self):
        printer = _Printer("SERIAL-A")
        asked: list[int] = []
        printer.get_state = lambda: asked.append(1) or PrinterState(state=PrinterStatus.IDLE, connected=True)
        refused = PrintResult(success=False, message="the gate said no", refused_before_send=True)
        verdict = resolve_print_start(printer, refused, sent_at=time.monotonic(), file_name="part.gcode")
        assert verdict.state == FAILED and verdict.ok is False
        assert verdict.message == "the gate said no"
        assert verdict.what_you_will_see is None
        assert asked == [], "there is no command for the printer to confirm or refute"

    def test_a_sent_command_that_looked_failed_is_still_doubted(self):
        """Unchanged on purpose: the command went out, the reading predates
        it, and a false "it failed" costs a duplicate print."""
        printer = _Printer("SERIAL-A")
        looked_failed = PrintResult(success=False, message="printer reported a failure")
        verdict = resolve_print_start(printer, looked_failed, sent_at=time.monotonic(), file_name="part.gcode")
        assert verdict.state == ACCEPTED and verdict.ok is True

    def test_the_template_marks_both_of_its_refusals(self, monkeypatch):
        """The safety gate's refusal and the sign-off backstop's: both are
        made before ``_start_print_impl``, and both say so on the result.
        A/B: with the flag left off either return, its half fails."""
        from kiln.print_signoff import require_signoff

        # The safety gate.
        printer = _Printer("SERIAL-A")
        monkeypatch.setattr(
            "kiln.printers.print_gate.run_adapter_gate",
            lambda adapter, file_name, kwargs: {"blocked": True, "reason": "too hot for this hotend"},
        )
        gated = printer.start_print("part.gcode")
        assert gated.success is False and gated.refused_before_send is True and printer.sent == []
        # The sign-off backstop: a registered printer, no clearance, the switch off.
        monkeypatch.setattr("kiln.printers.print_gate.run_adapter_gate", lambda *a, **k: None)
        monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE")
        require_signoff(printer)
        unsigned = printer.start_print("part.gcode")
        assert unsigned.success is False and unsigned.refused_before_send is True and printer.sent == []

    def test_only_a_refusal_carries_the_new_key(self):
        """Every other result keeps the three keys it has always had."""
        assert PrintResult(success=True, message="ok").to_dict() == {"success": True, "message": "ok", "job_id": None}
        assert PrintResult(success=False, message="no", refused_before_send=True).to_dict()["refused_before_send"] is True
