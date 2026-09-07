"""A Bambu write answers with what the printer showed, not with ``True``.

Measured on an A1 on 2026-09-06: a hotend commanded to 250°C four times over
twenty minutes, every call answering ``accepted: True``, the nozzle never
heating.  Two holes, pinned here:

1. ``_publish_command`` discarded paho's publish result.  paho does not raise
   on a dead connection — it returns ``rc=MQTT_ERR_NO_CONN`` and drops the
   message — so a client that knew it was disconnected still "sent".
2. Every write ended in a hardcoded ``return True``.  Nothing asked the
   printer whether the command took.

The verdict is one field, three values, the ``print_start`` shape:
``confirmed`` (a report AFTER the command shows the effect), ``accepted``
(sent, not shown), ``failed`` (refused — raised as PrinterError).
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import PrinterError
from kiln.printers.command_verdict import ACCEPTED, CONFIRMED, CommandVerdict

pytestmark = pytest.mark.usefixtures("_pin_store_env")


@pytest.fixture(autouse=True)
def _pin_store_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "bambu_tls_pins.json"))


@pytest.fixture(autouse=True)
def _disable_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server doors rate-limit per tool; tests here call one door repeatedly."""
    import kiln.server as srv

    monkeypatch.setattr(srv._tool_limiter, "check", lambda *a, **kw: None)


def _connected(*, rc: int = mqtt.MQTT_ERR_SUCCESS, window: float | None = 0.0) -> BambuAdapter:
    """An adapter whose mocked client answers publish() like paho does."""
    adapter = BambuAdapter(host="192.168.1.100", access_code="12345678", serial="01P00A000000001", timeout=2)
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    adapter._mqtt_client.is_connected.return_value = True
    adapter._mqtt_client.publish.return_value = mock.MagicMock(rc=rc)
    adapter._confirm_window_s = window
    return adapter


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    """Deliver one push_status frame the way the MQTT thread would."""
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _reply_on_publish(adapter: BambuAdapter, **fields: Any) -> None:
    """Make the printer 'answer' the next publish with a status frame."""

    def _publish(*_a: Any, **_k: Any) -> mock.MagicMock:
        _push(adapter, **fields)
        return mock.MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    adapter._mqtt_client.publish.side_effect = _publish


# ---------------------------------------------------------------------------
# Hole 1: the publish result
# ---------------------------------------------------------------------------


class TestPublishResultIsRead:
    def test_disconnected_client_is_not_reported_as_sent(self) -> None:
        """paho returns MQTT_ERR_NO_CONN instead of raising; that is a failure."""
        adapter = _connected(rc=mqtt.MQTT_ERR_NO_CONN)

        with pytest.raises(PrinterError, match="NOT delivered"):
            adapter.set_tool_temp(250)

        # And the next call reconnects instead of reusing the dead client.
        assert not adapter._mqtt_connected.is_set()
        assert adapter._connected is False

    def test_any_non_success_rc_is_a_failure(self) -> None:
        adapter = _connected(rc=mqtt.MQTT_ERR_QUEUE_SIZE)
        with pytest.raises(PrinterError, match="NOT delivered"):
            adapter.send_gcode(["G28"])
        # Only NO_CONN means the session is gone.
        assert adapter._mqtt_connected.is_set()

    def test_client_that_knows_it_is_disconnected_never_publishes(self) -> None:
        adapter = _connected()
        adapter._mqtt_client.is_connected.return_value = False

        with pytest.raises(PrinterError, match="not connected"):
            adapter.set_bed_temp(60)

        adapter._mqtt_client.publish.assert_not_called()
        assert not adapter._mqtt_connected.is_set()

    def test_successful_publish_still_sends_once(self) -> None:
        adapter = _connected()
        adapter.send_gcode(["G28"])
        adapter._mqtt_client.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Hole 2: the verdict
# ---------------------------------------------------------------------------


class TestTemperatureVerdict:
    def test_no_report_means_accepted_not_confirmed(self) -> None:
        """The exact session shape: sent, nothing heard back, nozzle cold."""
        adapter = _connected()
        verdict = adapter.set_tool_temp(250)

        assert isinstance(verdict, CommandVerdict)
        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False
        assert verdict.ok is True
        assert bool(verdict) is True  # legacy `if adapter.set_tool_temp(t)` keeps meaning
        assert verdict.evidence["corroboration"] == "no_report_since_command"
        assert verdict.evidence["sent"] == "M104 S250"
        assert "NOT confirmed" in verdict.message
        assert "nozzle_target_temper" in verdict.message

    def test_report_after_command_confirms(self) -> None:
        adapter = _connected(window=1.0)
        _reply_on_publish(adapter, nozzle_target_temper=250)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == CONFIRMED
        assert verdict.confirmed is True
        assert verdict.evidence["corroboration"] == "read_back"
        assert verdict.evidence["observed"] == 250

    def test_report_before_command_does_not_confirm(self) -> None:
        """A reading that predates the command is not a verdict on it."""
        adapter = _connected()
        _push(adapter, nozzle_target_temper=250)  # the PREVIOUS command's effect
        time.sleep(0.005)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False
        assert adapter._last_status["nozzle_target_temper"] == 250  # cache says 250 and it is not enough

    def test_report_after_command_with_wrong_value_stays_accepted(self) -> None:
        """A contradicting frame is reported, not promoted to 'failed'."""
        adapter = _connected()
        _reply_on_publish(adapter, nozzle_target_temper=0)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "read_back_mismatch"
        assert verdict.evidence["observed"] == 0
        assert "nozzle_target_temper=0" in verdict.message

    def test_report_without_the_field_stays_accepted(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, nozzle_temper=27.5)  # spoke, but not about the target

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "report_without_field"

    def test_confirmed_returns_as_soon_as_the_frame_lands(self) -> None:
        adapter = _connected(window=2.0)
        _reply_on_publish(adapter, bed_target_temper=60)
        started = time.monotonic()
        verdict = adapter.set_bed_temp(60)
        assert verdict.confirmed
        assert time.monotonic() - started < 0.5

    def test_active_fault_is_named_when_unconfirmed(self) -> None:
        """A fault on the screen is a reason a command may not take; say so."""
        adapter = _connected()
        _push(adapter, hms=[{"attr": 0x12008000, "code": 0x00010001}])

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.evidence["printer_faults"]
        assert "fault code" in verdict.message


class TestOtherWritesReadBack:
    def test_bed_confirmed_by_bed_target(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, bed_target_temper=60)
        assert adapter.set_bed_temp(60).confirmed

    def test_speed_profile_confirmed_by_spd_lvl(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, spd_lvl=3)
        assert adapter.set_speed_profile("sport").confirmed
        adapter2 = _connected()
        _reply_on_publish(adapter2, spd_lvl=2)
        assert not adapter2.set_speed_profile("sport").confirmed

    def test_light_confirmed_by_lights_report(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, lights_report=[{"node": "chamber_light", "mode": "on"}])
        assert adapter.set_light("chamber_light", "on").confirmed
        adapter2 = _connected()
        _reply_on_publish(adapter2, lights_report=[{"node": "chamber_light", "mode": "off"}])
        assert not adapter2.set_light("chamber_light", "on").confirmed

    def test_fan_confirmed_by_reported_level(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, cooling_fan_speed="15")
        assert adapter.set_fan("part", 100).confirmed
        adapter2 = _connected()
        _reply_on_publish(adapter2, cooling_fan_speed="15")
        assert not adapter2.set_fan("part", 0).confirmed

    def test_raw_gcode_says_it_cannot_confirm(self) -> None:
        adapter = _connected()
        verdict = adapter.send_gcode(["G28", "G1 Z120"])
        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "none"
        assert "NOT confirmed" in verdict.message

    def test_publish_print_command_says_it_cannot_confirm(self) -> None:
        adapter = _connected()
        verdict = adapter.publish_print_command("print_option", {"auto_recovery": True})
        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "none"

    def test_no_write_returns_a_bare_bool(self) -> None:
        adapter = _connected()
        for call in (
            lambda: adapter.set_tool_temp(200),
            lambda: adapter.set_bed_temp(50),
            lambda: adapter.set_speed_profile("standard"),
            lambda: adapter.set_light("work_light", "off"),
            lambda: adapter.set_fan("aux", 50),
            lambda: adapter.send_gcode(["M400"]),
            lambda: adapter.publish_print_command("print_option"),
        ):
            assert isinstance(call(), CommandVerdict)


# ---------------------------------------------------------------------------
# The doors: server tools publish the verdict, and never promote a bool
# ---------------------------------------------------------------------------


class TestServerDoors:
    @mock.patch("kiln.server._get_adapter")
    def test_set_temperature_reports_outcome(self, get_adapter: mock.MagicMock) -> None:
        from kiln.server import set_temperature

        adapter = _connected()
        get_adapter.return_value = adapter

        result = set_temperature(tool_temp=250)

        assert result["success"] is True
        assert result["tool"]["outcome"] == "accepted"
        assert result["tool"]["confirmed"] is False
        assert result["tool"]["accepted"] is True
        assert any("NOT confirmed" in w for w in result["warnings"])

    @mock.patch("kiln.server._get_adapter")
    def test_set_temperature_confirmed(self, get_adapter: mock.MagicMock) -> None:
        from kiln.server import set_temperature

        adapter = _connected()
        _reply_on_publish(adapter, nozzle_target_temper=250)
        get_adapter.return_value = adapter

        result = set_temperature(tool_temp=250)

        assert result["tool"]["outcome"] == "confirmed"
        assert result["tool"]["confirmed"] is True
        assert "warnings" not in result

    @mock.patch("kiln.server._get_adapter")
    def test_legacy_bool_is_accepted_never_confirmed(self, get_adapter: mock.MagicMock) -> None:
        from kiln.printers.octoprint import OctoPrintAdapter
        from kiln.server import set_temperature

        adapter = mock.MagicMock(spec=OctoPrintAdapter)
        adapter.set_bed_temp.return_value = True
        get_adapter.return_value = adapter

        result = set_temperature(bed_temp=60)

        assert result["bed"]["accepted"] is True
        assert result["bed"]["outcome"] == "accepted"
        assert result["bed"]["confirmed"] is False

    @mock.patch("kiln.server._get_adapter")
    def test_send_gcode_tool_reports_outcome(self, get_adapter: mock.MagicMock) -> None:
        from kiln.server import send_gcode

        adapter = _connected()
        get_adapter.return_value = adapter

        result = send_gcode("G28")

        assert result["success"] is True
        assert result["outcome"] == "accepted"
        assert result["confirmed"] is False
        assert "NOT confirmed" in result["message"]

    @mock.patch("kiln.server._get_adapter")
    def test_disconnected_client_surfaces_as_an_error(self, get_adapter: mock.MagicMock) -> None:
        from kiln.server import set_temperature

        get_adapter.return_value = _connected(rc=mqtt.MQTT_ERR_NO_CONN)

        result = set_temperature(tool_temp=250)

        # The refused publish marks the session dropped, so the write's own
        # _ensure_mqtt tries to reconnect and, with no printer, fails: either
        # message is the truth, and neither is "accepted".
        assert result["success"] is False
        msg = result["error"]["message"]
        assert "NOT delivered" in msg or "Couldn't reach the printer" in msg
        assert "tool" not in result


class TestTheStampIsTakenWithTheLinkUp:
    """A frame that lands while the SESSION is being built predates the command.

    ``_ensure_mqtt`` can block for seconds rebuilding a dropped session.  A
    stamp taken before that call would sit behind any frame arriving during
    it, so the printer's last word about the PREVIOUS command would postdate
    this one and read as confirmation of it.
    """

    def test_a_frame_during_connect_does_not_confirm(self) -> None:
        adapter = _connected()

        connects: list[int] = []

        def _slow_connect() -> Any:
            # The printer's answer to an EARLIER command, landing while the
            # session is being re-established.  Only the first call rebuilds
            # the session; the publish path's own call finds it up.
            if not connects:
                connects.append(1)
                _push(adapter, nozzle_target_temper=250)
                time.sleep(0.01)
            return adapter._mqtt_client

        adapter._ensure_mqtt = _slow_connect  # type: ignore[method-assign]

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False


class TestResponsesAreJsonSafe:
    """A verdict object left in a response would break the MCP transport."""

    @mock.patch("kiln.server._get_adapter")
    def test_every_write_door_returns_json(self, get_adapter: mock.MagicMock) -> None:
        import json as _json

        from kiln.server import (
            send_gcode,
            set_fan,
            set_printer_light,
            set_speed_profile,
            set_temperature,
        )

        get_adapter.return_value = _connected()
        for call in (
            lambda: set_temperature(tool_temp=200, bed_temp=60),
            lambda: set_speed_profile("silent"),
            lambda: set_printer_light("chamber_light", "on"),
            lambda: set_fan("part", 100),
            lambda: send_gcode("G28"),
        ):
            _json.dumps(call())


class TestSkipObjectsIsReadBack:
    """Skipping is irreversible for the objects named, so "sent" is not enough.

    The printer reports what it has skipped in ``s_obj``; a skip is confirmed
    only when every requested id appears there in a frame after the command.
    """

    def test_confirmed_when_the_printer_lists_them(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, s_obj=[3, 7])
        assert adapter.skip_objects([3, 7]).confirmed

    def test_a_partial_skip_list_is_not_confirmation(self) -> None:
        adapter = _connected()
        _reply_on_publish(adapter, s_obj=[3])
        verdict = adapter.skip_objects([3, 7])
        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False

    def test_silence_is_not_confirmation(self) -> None:
        adapter = _connected()
        verdict = adapter.skip_objects([3])
        assert verdict.state == ACCEPTED
        assert "NOT confirmed" in verdict.message

    @mock.patch("kiln.server._get_adapter")
    def test_the_door_says_it_is_unconfirmed(self, get_adapter: mock.MagicMock) -> None:
        from kiln.server import skip_print_objects

        get_adapter.return_value = _connected()
        result = skip_print_objects(object_ids=[3])

        assert result["success"] is True
        assert result["outcome"] == "accepted"
        assert result["confirmed"] is False
        assert "irreversible" in result["message"]


class TestAlreadyAtTheRequestedValue:
    """A printer that reports on CHANGE says nothing when nothing changes.

    Measured on Adam's A1 (2026-09-06): "chamber light off" with the light
    already off produced no report at all, while "on" — a real change — was
    confirmed in 2.05s.  Without this, a request the printer already
    satisfies is indistinguishable from one that never arrived.
    """

    def test_a_no_change_request_says_so(self) -> None:
        adapter = _connected()
        _push(adapter, nozzle_target_temper=250)  # the printer is ALREADY at 250
        time.sleep(0.005)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False  # still not evidence about THIS command
        assert verdict.evidence["already_at_requested_value"] is True
        assert verdict.evidence["reported_before_command"] == 250
        assert "already reported" in verdict.message
        assert "in the requested state either way" in verdict.message

    def test_a_different_prior_value_is_not_called_already_there(self) -> None:
        adapter = _connected()
        _push(adapter, nozzle_target_temper=200)
        time.sleep(0.005)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == ACCEPTED
        assert "already_at_requested_value" not in verdict.evidence
        assert "NOT confirmed" in verdict.message

    def test_a_never_reported_field_is_not_called_already_there(self) -> None:
        adapter = _connected()
        verdict = adapter.set_tool_temp(250)
        assert "already_at_requested_value" not in verdict.evidence

    def test_a_real_confirmation_still_wins(self) -> None:
        adapter = _connected()
        _push(adapter, nozzle_target_temper=250)
        time.sleep(0.005)
        _reply_on_publish(adapter, nozzle_target_temper=250)

        verdict = adapter.set_tool_temp(250)

        assert verdict.state == CONFIRMED
        assert "already_at_requested_value" not in verdict.evidence


class TestFanConfirmationMatchesTheHardware:
    """The fan is the one watched field that is MEASURED, not set.

    Measured on an A1 (2026-09-06) — reported level after an M106:
        40%:  0 -> 5 @2.0s -> 6 @6.1s
        100%: 5 -> 13 @4.1s -> 14 @6.1s
        0%:  14 -> 10 @2.0s -> 1 @4.1s -> 0 @8.1s
    Two things follow: full speed reports 14, not 15, and settling takes about
    eight seconds.  A three-second window with an exact match at the ends
    called every working fan command unconfirmed.
    """

    def test_full_speed_reported_as_fourteen_confirms(self) -> None:
        adapter = _connected(window=1.0)
        _reply_on_publish(adapter, cooling_fan_speed="14")
        verdict = adapter.set_fan("part", 100)
        assert verdict.state == CONFIRMED, verdict.evidence

    def test_a_mid_ramp_reading_does_not_confirm(self) -> None:
        """5 on the way to 6 is not yet 6; the window is what waits it out."""
        adapter = _connected(window=0.4)
        _reply_on_publish(adapter, cooling_fan_speed="2")
        verdict = adapter.set_fan("part", 40)
        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "read_back_mismatch"

    def test_the_fan_gets_the_longer_window(self) -> None:
        """A fan waits longer than a setting, because a fan physically ramps."""
        from kiln.printers.bambu import _COMMAND_CONFIRM_WINDOW_S, _FAN_CONFIRM_WINDOW_S

        assert _FAN_CONFIRM_WINDOW_S > _COMMAND_CONFIRM_WINDOW_S
        adapter = _connected(window=None)  # no override: the command decides
        adapter._timeout = 999
        verdict = adapter.set_fan("part", 40)
        assert verdict.evidence["window_seconds"] == _FAN_CONFIRM_WINDOW_S

    def test_a_setting_keeps_the_short_window(self) -> None:
        from kiln.printers.bambu import _COMMAND_CONFIRM_WINDOW_S

        adapter = _connected(window=None)  # no override: the command decides
        adapter._timeout = 999
        verdict = adapter.set_light("chamber_light", "on")
        assert verdict.evidence["window_seconds"] == _COMMAND_CONFIRM_WINDOW_S
