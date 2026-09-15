"""The honest emergency stop and the watchdog's retirement, on one path.

Two changes meet here.  The print watchdog now stops a Bambu with a command
the firmware obeys and waits for the printer to report that the job ended.
That same report reaches the print lifecycle, which retires the watchdog.  So
the report that CONFIRMS the stop arrives, on the printer's own channel, while
the watchdog is still inside its trip.

These tests pin that the two halves agree: one stop, one latch, one incident,
the watchdog retired, and nothing commanded again afterwards.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln import server
from kiln.print_watchdog import PrintWatchdog
from kiln.printers import base
from kiln.printers import progress_motion as pm
from kiln.printers.bambu import BambuAdapter
from kiln.registry import PrinterRegistry

NAME = "bench-a1"
JOB = {"subtask_name": "part", "gcode_file": "part.3mf"}


class _NullHeaterWatchdog:
    @staticmethod
    def notify_print_started() -> None:
        pass

    @staticmethod
    def notify_print_ended() -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """A process that has attached nothing yet, and polls nothing."""
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
    monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", ())
    monkeypatch.setattr(server, "_print_lifecycle_hooks_installed", False)
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_adapter", None)
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
    # Attached, never polling: the tests drive step() themselves.
    monkeypatch.setattr(PrintWatchdog, "start", lambda self: None)
    pm.reset_progress_observations()
    yield
    pm.reset_progress_observations()


@pytest.fixture
def incidents(monkeypatch) -> list[dict[str, Any]]:
    from kiln import incident_recorder

    filed: list[dict[str, Any]] = []
    monkeypatch.setattr(incident_recorder, "record_incident", lambda **kw: filed.append(kw))
    return filed


def _bambu_on_the_server() -> BambuAdapter:
    a1 = BambuAdapter(
        host="192.0.2.61", access_code="12345678", serial="01S00C000000061", timeout=2
    )
    a1._mqtt_connected.set()
    a1._connected = True
    a1._mqtt_client = mock.MagicMock()
    delivered = mock.MagicMock()
    delivered.rc = mqtt.MQTT_ERR_SUCCESS
    a1._mqtt_client.publish.return_value = delivered
    a1._confirm_window_s = 0.0
    server._get_registry().register(NAME, a1)
    return a1


def _push(a1: BambuAdapter, **fields: Any) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **JOB, **fields}}).encode()
    a1._on_message(a1._mqtt_client, None, msg)


def _published(a1: BambuAdapter) -> list[dict[str, Any]]:
    return [json.loads(call.args[1]) for call in a1._mqtt_client.publish.call_args_list]


def _stops(a1: BambuAdapter) -> int:
    return sum(1 for p in _published(a1) if p.get("print", {}).get("command") == "stop")


def _printing_at(a1: BambuAdapter, nozzle: float) -> None:
    _push(
        a1,
        gcode_state="RUNNING",
        print_error=0,
        nozzle_temper=nozzle,
        nozzle_target_temper=220,
        bed_temper=60,
        bed_target_temper=60,
    )


def _armed(a1: BambuAdapter) -> PrintWatchdog:
    server._spawn_print_watchdog(a1, "part.3mf")
    return server._print_watchdogs[base.outcome_printer_name(a1)]


def test_a_stop_confirmed_by_the_ending_retires_the_watchdog_after_one_stop(incidents):
    a1 = _bambu_on_the_server()
    assert base.outcome_printer_name(a1) == NAME
    watchdog = _armed(a1)

    _printing_at(a1, 220)
    assert watchdog.step() is None  # at temperature

    delivered = a1._mqtt_client.publish.return_value

    def _printer_obeys_the_stop(topic: str, payload: str, qos: int = 0) -> Any:
        if json.loads(payload).get("print", {}).get("command") == "stop":
            _push(a1, gcode_state="FAILED", nozzle_target_temper=0, bed_target_temper=0)
        return delivered

    a1._mqtt_client.publish.side_effect = _printer_obeys_the_stop
    _printing_at(a1, 180)  # 40 degrees below its target, after reaching it

    flag = watchdog.step()

    assert flag is not None and flag.rule == "tool_drop"
    assert flag.context["estop_confirmed"] is True
    assert watchdog.anomaly_triggered is True
    assert _stops(a1) == 1
    assert NAME not in server._print_watchdogs  # the ending retired it
    assert watchdog._stop_event.is_set()
    assert [i["incident_type"] for i in incidents] == ["watchdog_red_flag"]

    sent = len(_published(a1))
    assert watchdog.step() is None  # latched: nothing more to say or send
    assert len(_published(a1)) == sent


def test_an_ending_during_an_unconfirmed_stop_ends_the_retries(incidents):
    a1 = _bambu_on_the_server()
    watchdog = _armed(a1)
    _printing_at(a1, 220)
    watchdog.step()
    _printing_at(a1, 180)

    flag = watchdog.step()  # the printer reports nothing after the stop

    assert flag is not None and flag.context["estop_confirmed"] is False
    assert watchdog.anomaly_triggered is False  # awake, ready to command it again
    assert _stops(a1) == 1

    _push(a1, gcode_state="FINISH", nozzle_target_temper=0, bed_target_temper=0)

    assert NAME not in server._print_watchdogs
    assert watchdog._stop_event.is_set()
    watchdog._run_loop()  # a retired watchdog's loop exits without polling again
    assert _stops(a1) == 1
