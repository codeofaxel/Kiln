"""Tests for releasing the Bambu MQTT connection when it falls idle.

A Bambu printer accepts only a few simultaneous LAN clients.  Every MCP
session spawns its own ``kiln serve``, each builds its own adapter, and
before the idle release each held a connection slot for the life of the
process — so leftover servers from closed sessions starved the printer and
the user met a timeout that blamed Bambu Studio (2026-08-14 field report:
five servers, five held slots, printer powered on and answering pings).

These tests pin the behaviour that fixes it and, just as importantly, the
two things it must not break: a connection is kept through a running print
so the push stream still turns the ending into a recorded outcome, and the
reconcile that settles outcomes re-arms on every reconnect rather than
firing once per process.
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

import pytest

from kiln.printers.bambu import BambuAdapter

HOST = "192.168.1.100"
ACCESS_CODE = "12345678"
SERIAL = "01P00A000000001"


def _adapter(**kwargs: Any) -> BambuAdapter:
    defaults: dict[str, Any] = {
        "host": HOST,
        "access_code": ACCESS_CODE,
        "serial": SERIAL,
        "timeout": 2,
    }
    defaults.update(kwargs)
    return BambuAdapter(**defaults)


@pytest.fixture(autouse=True)
def _pin_store_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "bambu_tls_pins.json"))


def _connected(**status: Any) -> BambuAdapter:
    """An adapter in the state a successful connect leaves behind."""
    adapter = _adapter()
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    adapter._last_status = dict(status)
    return adapter


class TestIdleWindow:
    """The configured window, and the escape hatch that disables it."""

    def test_defaults_to_two_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KILN_BAMBU_IDLE_DISCONNECT_S", raising=False)
        assert _adapter()._idle_window() == 120.0

    def test_env_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "30")
        assert _adapter()._idle_window() == 30.0

    def test_zero_disables_the_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 restores the hold-forever behaviour, and starts no thread."""
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "0")
        adapter = _connected()
        assert adapter._idle_window() == 0.0
        adapter._start_idle_reaper()
        assert adapter._idle_reaper is None

    def test_garbage_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed env var must not fail a printer operation."""
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "soon-ish")
        assert _adapter()._idle_window() == 120.0


class TestIdleRelease:
    """The slot is handed back once nobody is using it."""

    def test_releases_after_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "0.4")
        adapter = _connected(gcode_state="idle")
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        assert adapter._idle_reaper is not None
        adapter._idle_reaper.join(timeout=5)
        assert adapter._mqtt_client is None, "idle connection was not released"
        assert not adapter._mqtt_connected.is_set()

    def test_recent_activity_keeps_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A burst of work must not be cut off mid-burst."""
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "30")
        adapter = _connected(gcode_state="idle")
        adapter._last_activity = time.monotonic()
        adapter._start_idle_reaper()
        assert adapter._idle_reaper is not None
        adapter._idle_reaper.join(timeout=1.5)
        assert adapter._mqtt_client is not None, "in-use connection was dropped"
        adapter._idle_stop.set()

    def test_holds_the_slot_while_printing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never drop mid-print.

        The push stream is what turns a finished print into a recorded
        outcome.  Dropping mid-job would push that onto the reconnect
        reconcile, which by design refuses to guess a duration across an
        outage — the print would bank as "unknown" instead of measured.
        """
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "0.4")
        adapter = _connected(gcode_state="running")
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        assert adapter._idle_reaper is not None
        adapter._idle_reaper.join(timeout=1.5)
        assert adapter._mqtt_client is not None, "dropped the slot mid-print"
        assert adapter._idle_reaper.is_alive(), "release was cancelled, not deferred"
        adapter._idle_stop.set()

    def test_release_is_deferred_then_taken_after_the_print(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the job ends, the deferred release goes through."""
        monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "0.4")
        adapter = _connected(gcode_state="running")
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        time.sleep(0.3)
        assert adapter._mqtt_client is not None
        with adapter._state_lock:
            adapter._last_status["gcode_state"] = "finish"
        assert adapter._idle_reaper is not None
        adapter._idle_reaper.join(timeout=5)
        assert adapter._mqtt_client is None, "release never resumed after the print"

    def test_activity_stamp_moves_on_every_call(self) -> None:
        """_ensure_mqtt is the one funnel every read and write passes."""
        adapter = _connected(gcode_state="idle")
        adapter._last_activity = time.monotonic() - 500.0
        before = adapter._last_activity
        adapter._ensure_mqtt()
        assert adapter._last_activity > before


class TestDisconnect:
    """What a release must leave behind for the next connect."""

    def test_rearms_the_outcome_reconcile(self) -> None:
        """The one-shot guard is per-connection now, not per-process.

        A print that starts and ends inside a release window is exactly the
        case the reconcile exists for; leaving the flag set would skip it.
        """
        adapter = _connected(gcode_state="idle")
        adapter._pending_outcomes_reconciled = True
        adapter.disconnect()
        assert adapter._pending_outcomes_reconciled is False

    def test_is_idempotent(self) -> None:
        adapter = _connected(gcode_state="idle")
        adapter.disconnect()
        adapter.disconnect()  # must not raise
        assert adapter._mqtt_client is None

    def test_safe_when_never_connected(self) -> None:
        _adapter().disconnect()  # must not raise

    def test_update_credentials_releases_the_slot(self) -> None:
        adapter = _connected(gcode_state="idle")
        adapter.update_credentials("87654321")
        assert adapter._mqtt_client is None
        assert adapter._access_code == "87654321"


def _held(kiln_count: int, supported: bool = True) -> dict:
    return {
        "supported": supported,
        "holders": [{"pid": 100 + i, "is_kiln": True} for i in range(kiln_count)],
        "kiln_count": kiln_count,
    }


def _siblings(count: int | None) -> dict:
    return {"count": count, "pids": [], "oldest_age": None, "warning": None}


class TestOtherClientsHint:
    """The error text names Kiln's own servers, measured.

    Prefers the socket scan over the process count: "who holds a connection
    to this printer right now" is the question, and "how many servers exist"
    is only a stand-in for it.
    """

    def test_names_the_measured_holders(self) -> None:
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders", return_value=_held(3)
        ):
            hint = _adapter()._other_clients_hint()
        assert "3 other copies" in hint
        assert "are holding" in hint
        assert HOST in hint
        assert "kiln trim" in hint
        assert "power-cycling will not help" in hint.lower()

    def test_singular_reads_correctly(self) -> None:
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders", return_value=_held(1)
        ):
            hint = _adapter()._other_clients_hint()
        assert "1 other copy" in hint
        assert "is holding" in hint

    def test_silent_when_no_kiln_server_holds_this_printer(self) -> None:
        """Sockets listed fine and none are ours — the culprit really is
        other software, so the Bambu Studio advice should stand alone."""
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders", return_value=_held(0)
        ):
            assert _adapter()._other_clients_hint() == ""

    def test_falls_back_to_the_process_count_when_sockets_cannot_be_listed(
        self,
    ) -> None:
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders",
            return_value=_held(0, supported=False),
        ), mock.patch(
            "kiln.serve_siblings.check_serve_siblings", return_value=_siblings(5)
        ):
            hint = _adapter()._other_clients_hint()
        assert "5 servers" in hint
        assert "kiln trim" in hint

    def test_silent_when_only_this_server_is_running(self) -> None:
        """One server holds no evidence, so it makes no accusation."""
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders",
            return_value=_held(0, supported=False),
        ), mock.patch(
            "kiln.serve_siblings.check_serve_siblings", return_value=_siblings(1)
        ):
            assert _adapter()._other_clients_hint() == ""

    def test_silent_when_nothing_can_be_measured(self) -> None:
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders",
            return_value=_held(0, supported=False),
        ), mock.patch(
            "kiln.serve_siblings.check_serve_siblings", return_value=_siblings(None)
        ):
            assert _adapter()._other_clients_hint() == ""

    def test_never_raises_out_of_an_error_path(self) -> None:
        """This runs while building an error message; it must not add one."""
        with mock.patch(
            "kiln.serve_siblings.printer_connection_holders",
            side_effect=RuntimeError("lsof exploded"),
        ):
            assert _adapter()._other_clients_hint() == ""
