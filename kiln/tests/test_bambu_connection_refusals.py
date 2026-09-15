"""A full printer is not a wrong access code.

Measured 2026-09-15 on an A1 whose four LAN connection slots were held by
four idle Kiln servers: a fifth connection completed TCP, then its TLS
handshake timed out -- the printer never answered the login at all.  Kiln
reported that as UNAUTHORIZED, because the adapter's own "no response"
message advised checking the access code, and the read-failure diagnosis
matches credential words before it weighs the slot evidence.

The other half was invisible: a login the printer DID refuse never reached
the adapter's credentials branch.  paho 2 hands ``on_connect`` a ReasonCode
whose value is the MQTT 5 number (135 "Not authorized", 134 "Bad user name
or password"), not the MQTT 3.1.1 numbers 5 and 4 the branch tested for, so
the refusal waited out the whole timeout and was named by accident.

These tests drive the REAL adapter messages through the REAL classifiers, so
a later rewording that reintroduces the confusion fails here rather than in
front of a user.
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from kiln.printers.base import PrinterError, PrinterStatus, diagnose_read_failure
from kiln.printers.bambu import BambuAdapter

HOST = "192.0.2.10"


def _adapter(timeout: float = 0.3) -> BambuAdapter:
    return BambuAdapter(host=HOST, access_code="12345678", serial="03900A000000001", timeout=timeout)


def _no_answer_error() -> PrinterError:
    """The error the adapter really raises when the printer never answers."""
    adapter = _adapter(timeout=0.2)
    with mock.patch("paho.mqtt.client.Client"):
        with pytest.raises(PrinterError) as info:
            adapter._ensure_mqtt()
    return info.value


def _answered_with(adapter: BambuAdapter, code: Any):
    """A fake ``connect_async`` the printer answers with *code*."""

    def connect_async(*_args: Any, **_kwargs: Any) -> None:
        adapter._on_connect(mock.MagicMock(), None, {}, code)

    return connect_async


class TestAPrinterThatNeverAnswers:
    def test_it_is_not_read_as_a_credentials_problem(self) -> None:
        from kiln import server

        message = str(_no_answer_error())
        assert server._reads_as_auth_failure(message) is False
        diagnosis = diagnose_read_failure(message, kiln_slot_holders=4, reachable=True)
        assert diagnosis.state is PrinterStatus.CONNECTION_LIMIT

    def test_it_still_names_the_connection_limit(self) -> None:
        assert "connections at once" in str(_no_answer_error())

    def test_the_state_read_says_connection_limit_when_kiln_holds_the_slots(self) -> None:
        adapter = _adapter()
        error = _no_answer_error()
        with (
            mock.patch.object(adapter, "_ensure_mqtt", side_effect=error),
            mock.patch(
                "kiln.serve_siblings.printer_connection_holders",
                return_value={"supported": True, "kiln_count": 4},
            ),
            mock.patch("kiln.printers.base.probe_tcp", return_value=True),
        ):
            state = adapter.get_state()
        assert state.state is PrinterStatus.CONNECTION_LIMIT
        assert "trim_serve_processes" in (state.remedy or "")


class TestARefusedLogin:
    @pytest.mark.parametrize(
        "code",
        [
            ReasonCode(PacketTypes.CONNACK, "Not authorized"),
            ReasonCode(PacketTypes.CONNACK, "Bad user name or password"),
            5,
            4,
        ],
        ids=["paho2-not-authorized", "paho2-bad-credentials", "mqtt311-5", "mqtt311-4"],
    )
    def test_it_is_named_as_credentials_at_once(self, code: Any) -> None:
        from kiln import server

        adapter = _adapter(timeout=5.0)
        with mock.patch("paho.mqtt.client.Client") as client_cls:
            client_cls.return_value.connect_async.side_effect = _answered_with(adapter, code)
            started = time.monotonic()
            with pytest.raises(PrinterError) as info:
                adapter._ensure_mqtt()
            elapsed = time.monotonic() - started
        assert elapsed < 1.0, "the printer already answered; waiting out the timeout hides the answer"
        message = str(info.value)
        assert server._reads_as_auth_failure(message) is True
        assert diagnose_read_failure(message, kiln_slot_holders=4, reachable=True).state is PrinterStatus.UNAUTHORIZED

    def test_the_connect_callback_stops_on_paho2_codes(self) -> None:
        """The stop exists so a refused login does not retry against a printer
        that rations connections; under paho 2 it never fired."""
        adapter = _adapter()
        client = mock.MagicMock()
        with mock.patch.object(adapter, "_safe_stop_client") as stop:
            adapter._on_connect(client, None, {}, ReasonCode(PacketTypes.CONNACK, "Not authorized"))
        stop.assert_called_once_with(client)

    def test_a_refusal_that_is_not_about_credentials_is_not_called_one(self) -> None:
        from kiln import server

        adapter = _adapter(timeout=5.0)
        with mock.patch("paho.mqtt.client.Client") as client_cls:
            client_cls.return_value.connect_async.side_effect = _answered_with(
                adapter, ReasonCode(PacketTypes.CONNACK, "Server unavailable")
            )
            with pytest.raises(PrinterError) as info:
                adapter._ensure_mqtt()
        assert server._reads_as_auth_failure(str(info.value)) is False


class TestOneReadingOfCredentials:
    def test_there_is_one_list_of_credential_words(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Printer status used to keep its own copy of the adapter diagnosis's
        word list; two copies of one decision drift apart silently."""
        import kiln.printers.base as base
        from kiln import server

        monkeypatch.setattr(base, "_AUTH_NEEDLES", ("zebra-refusal",))
        assert server._reads_as_auth_failure("a zebra-refusal happened") is True
        assert server._reads_as_auth_failure("not authorized") is False
