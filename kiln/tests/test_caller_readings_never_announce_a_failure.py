"""Readings handed to ``detect_print_failure`` can never announce a failure.

A failure the recovery engine announces (PRINT_FAILED on the server's event
bus) ends the named printer's print watchdog and starts its heater cooldown.
``detect_print_failure`` takes its readings from whoever calls it, so made-up
readings used to do exactly that to a print that was running fine: the live
print lost its watchdog because someone typed a hotend temperature.

The rule: only readings Kiln takes off the printer itself, while it watches a
print, announce a failure.  Readings from outside are judged and kept (so
``plan_failure_recovery`` still finds them), marked as the caller's, and the
tool asks before it runs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from kiln import server
from kiln.events import EventType
from kiln.print_recovery import PrintRecovery

_PRINTER = "workshop"


def _thermal_runaway() -> dict[str, Any]:
    """Readings the engine classifies as critical thermal runaway."""
    return {
        "hotend_temp": 320.0,
        "hotend_target": 220.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "connected": True,
        "filament_detected": True,
    }


def _registered_tools() -> dict[str, Any]:
    from kiln.plugins.recovery_tools import plugin

    tools: dict[str, Any] = {}
    annotations: dict[str, Any] = {}

    class _MCP:
        def tool(self, **kwargs):
            def _decorator(fn):
                tools[fn.__name__] = fn
                annotations[fn.__name__] = kwargs.get("annotations")
                return fn

            return _decorator

    plugin.register(_MCP())
    tools["__annotations__"] = annotations
    return tools


@pytest.fixture
def wired_bus(monkeypatch):
    """The server's real event bus, with the print-ended handler subscribed
    the way the server wires it, and a record of every watchdog it stops."""
    stopped: list[str | None] = []
    published: list[Any] = []
    monkeypatch.setattr(
        server, "_stop_print_watchdog", lambda name=None: stopped.append(name)
    )

    def _record(event):
        published.append(event)

    bus = server._event_bus
    bus.subscribe(EventType.PRINT_FAILED, server._on_print_ended_event)
    bus.subscribe(None, _record)
    try:
        yield stopped, published
    finally:
        bus.unsubscribe(EventType.PRINT_FAILED, server._on_print_ended_event)
        bus.unsubscribe(None, _record)


def test_made_up_readings_leave_the_live_print_watched(wired_bus):
    """The incident: a caller's readings stopped a running print's watchdog."""
    stopped, published = wired_bus
    tools = _registered_tools()
    engine = PrintRecovery()

    with (
        patch("kiln.print_recovery.get_recovery_engine", return_value=engine),
        patch.object(server, "_check_auth", return_value=None),
    ):
        result = tools["detect_print_failure"](
            printer_name=_PRINTER, telemetry=_thermal_runaway(),
        )

    # The readings were judged (otherwise this test proves nothing) ...
    assert result["failure_detected"] is True
    # ... and nothing was announced, so the watchdog was never told to stop.
    assert [e for e in published if e.type == EventType.PRINT_FAILED] == []
    assert stopped == []
    assert result["failure"]["readings_from"] == "caller"


def test_a_caller_reported_failure_can_still_be_planned(wired_bus):
    """Keeping the report is what lets plan_failure_recovery find it."""
    tools = _registered_tools()
    engine = PrintRecovery()

    with (
        patch("kiln.print_recovery.get_recovery_engine", return_value=engine),
        patch.object(server, "_check_auth", return_value=None),
    ):
        found = tools["detect_print_failure"](
            printer_name=_PRINTER, telemetry=_thermal_runaway(),
        )
        plan = tools["plan_failure_recovery"](
            failure_id=found["failure"]["failure_id"],
        )

    assert plan["success"] is True


def test_detect_print_failure_asks_before_it_runs():
    annotation = _registered_tools()["__annotations__"]["detect_print_failure"]
    assert annotation.read_only_hint is False
    assert annotation.destructive_hint is True


def test_the_printers_own_readings_still_announce(wired_bus):
    """The other half: a failure the health monitor reads off the printer is
    still announced and still retires that printer's watchdog.  Without this,
    the fix could silence every real failure and the test above would pass."""
    from kiln.print_health_monitor import (
        MonitorPolicy,
        MonitorSession,
        PrintHealthMonitor,
        _StallTracker,
    )

    stopped, published = wired_bus
    monitor = PrintHealthMonitor()
    session = MonitorSession(
        session_id=str(uuid.uuid4()),
        printer_name=_PRINTER,
        job_id="part.gcode",
        policy=MonitorPolicy(),
    )
    monitor._sessions[session.session_id] = session
    monitor._stall_state[session.session_id] = _StallTracker()
    monitor._telemetry_from_health_report = lambda _report: _thermal_runaway()

    with patch("kiln.print_recovery.get_recovery_engine", return_value=PrintRecovery()):
        monitor._maybe_detect_failure(session, report=None)

    failed = [e for e in published if e.type == EventType.PRINT_FAILED]
    assert len(failed) == 1
    assert failed[0].data["printer_name"] == _PRINTER
    assert stopped == [_PRINTER]
