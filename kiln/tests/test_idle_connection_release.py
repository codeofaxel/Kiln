"""Tests for the shared idle connection release on PrinterAdapter.

Bambu (MQTT) and Elegoo (SDCP websocket) both ration client slots, and Kiln
runs one server per MCP session, so an adapter that holds its connection for
the life of its process turns closed sessions into a printer nobody can
reach.  The release machinery lives on the base class so the two backends
cannot drift on the subtle part — when NOT to let go.

``test_bambu_idle_release`` covers the Bambu specifics.  This module covers
the shared engine and the Elegoo wiring.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest import mock

import pytest

from kiln.printers.base import PrinterAdapter, PrinterStatus
from kiln.printers.elegoo import ElegooAdapter

HOST = "192.168.1.50"
MAINBOARD = "ABCD1234ABCD1234"


# ---------------------------------------------------------------------------
# The shared engine, exercised through a minimal adapter
# ---------------------------------------------------------------------------


class _FakeAdapter(PrinterAdapter):
    """Smallest thing that can opt in to the release, for engine tests."""

    _IDLE_RELEASE_ENV = "KILN_FAKE_IDLE_S"
    _IDLE_RELEASE_DEFAULT_S = 60.0

    def __init__(self) -> None:
        self._init_idle_release()
        self.live = True
        self.printing = False
        self.released = threading.Event()

    # -- the two hooks a backend supplies -----------------------------------
    def _connection_is_live(self) -> bool:
        return self.live

    def _print_in_flight(self) -> bool:
        return self.printing

    def disconnect(self) -> None:
        self.live = False
        self.released.set()

    @property
    def name(self) -> str:
        return "fake"


# The release machinery touches none of PrinterAdapter's printing surface, so
# clearing the abstract set is cheaper and clearer than stubbing fourteen
# methods that would never be called.
_FakeAdapter.__abstractmethods__ = frozenset()


def _fake() -> _FakeAdapter:
    """An adapter whose ``__init__`` has deliberately NOT run."""
    return _FakeAdapter.__new__(_FakeAdapter)


def _ready_fake() -> _FakeAdapter:
    return _FakeAdapter()


class TestSharedEngine:
    """Behaviour every opted-in backend inherits."""

    def test_releases_when_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "0.4")
        adapter = _ready_fake()
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        assert adapter.released.wait(timeout=5), "idle connection was not released"

    def test_holds_while_printing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "0.4")
        adapter = _ready_fake()
        adapter.printing = True
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        assert not adapter.released.wait(timeout=1.5), "dropped the slot mid-print"
        adapter._idle_stop.set()

    def test_deferred_release_resumes_after_the_print(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "0.4")
        adapter = _ready_fake()
        adapter.printing = True
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        assert not adapter.released.wait(timeout=0.6)
        adapter.printing = False
        assert adapter.released.wait(timeout=5), "release never resumed"

    def test_reaper_exits_when_the_connection_is_already_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "0.4")
        adapter = _ready_fake()
        adapter.live = False
        adapter._start_idle_reaper()
        adapter._idle_reaper.join(timeout=5)
        assert not adapter._idle_reaper.is_alive()
        assert not adapter.released.is_set(), "disconnected twice"

    def test_note_activity_defers_the_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "30")
        adapter = _ready_fake()
        adapter._last_activity = time.monotonic() - 100.0
        adapter._note_activity()
        adapter._start_idle_reaper()
        assert not adapter.released.wait(timeout=1.5), "in-use connection dropped"
        adapter._idle_stop.set()

    def test_only_one_reaper_per_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reconnect churn must not accumulate threads."""
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "30")
        adapter = _ready_fake()
        adapter._start_idle_reaper()
        first = adapter._idle_reaper
        for _ in range(5):
            adapter._start_idle_reaper()
        assert adapter._idle_reaper is first
        adapter._idle_stop.set()

    def test_opt_out_starts_no_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_FAKE_IDLE_S", "0")
        adapter = _ready_fake()
        adapter._start_idle_reaper()
        assert adapter._idle_reaper is None

    def test_backends_that_never_opted_in_are_inert(self) -> None:
        """A default adapter has no env var, so the window is zero."""
        adapter = _ready_fake()
        adapter._IDLE_RELEASE_ENV = ""
        assert adapter._idle_window() == 0.0

    def test_missing_init_degrades_to_never_releasing(self) -> None:
        """A backend that opts in but forgets _init_idle_release must not
        raise AttributeError out of a printer operation."""
        adapter = _fake()  # __init__ deliberately not run
        adapter._note_activity()  # must not raise
        assert isinstance(adapter._idle_stop, threading.Event)


class TestBaseDefaults:
    """The safe answers for a backend that supplies no hooks."""

    def test_connection_defaults_to_not_live(self) -> None:
        """So the reaper stops instead of spinning on an unknown backend."""
        assert PrinterAdapter._connection_is_live(object()) is False  # type: ignore[arg-type]

    def test_print_in_flight_defaults_to_holding(self) -> None:
        """'Cannot tell' must mean keep the connection, never drop it."""
        assert PrinterAdapter._print_in_flight(object()) is True  # type: ignore[arg-type]

    def test_disconnect_is_a_no_op_by_default(self) -> None:
        PrinterAdapter.disconnect(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Elegoo wiring
# ---------------------------------------------------------------------------


def _elegoo(**status: Any) -> ElegooAdapter:
    adapter = ElegooAdapter(host=HOST, mainboard_id=MAINBOARD, timeout=2)
    adapter._ws = mock.MagicMock()
    adapter._connected = True
    adapter._last_status = dict(status)
    return adapter


class TestElegooIdleRelease:
    """Elegoo rations websockets the same way Bambu rations MQTT."""

    def test_opted_in_with_its_own_env_var(self) -> None:
        assert ElegooAdapter._IDLE_RELEASE_ENV == "KILN_ELEGOO_IDLE_DISCONNECT_S"
        assert _elegoo()._idle_window() == 120.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_ELEGOO_IDLE_DISCONNECT_S", "45")
        assert _elegoo()._idle_window() == 45.0

    def test_live_while_the_socket_is_open(self) -> None:
        adapter = _elegoo()
        assert adapter._connection_is_live() is True
        adapter.disconnect()
        assert adapter._connection_is_live() is False

    @pytest.mark.parametrize("code", [13, 10, 5, 8, 9, 20])
    def test_active_codes_hold_the_slot(self, code: int) -> None:
        assert _elegoo(CurrentStatus=code)._print_in_flight() is True

    def test_idle_code_releases(self) -> None:
        assert _elegoo(CurrentStatus=0)._print_in_flight() is False

    def test_v3_list_status_is_understood(self) -> None:
        """SDCP V3 reports CurrentStatus as a list."""
        assert _elegoo(CurrentStatus=[13])._print_in_flight() is True
        assert _elegoo(CurrentStatus=[0])._print_in_flight() is False

    def test_unmapped_code_is_releasable(self) -> None:
        """The unmapped codes are the terminal ones, so this is a finished
        print — exactly when the slot should go back."""
        assert _elegoo(CurrentStatus=99)._print_in_flight() is False

    def test_unparseable_status_holds(self) -> None:
        """Genuine ignorance is different from a known gap in the map."""
        assert _elegoo(CurrentStatus="not-a-number")._print_in_flight() is True

    def test_releases_when_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_ELEGOO_IDLE_DISCONNECT_S", "0.4")
        adapter = _elegoo(CurrentStatus=0)
        adapter._last_activity = time.monotonic() - 10.0
        adapter._start_idle_reaper()
        adapter._idle_reaper.join(timeout=5)
        assert adapter._ws is None, "idle websocket was not released"
        assert adapter._connected is False

    def test_disconnect_stops_the_reaper(self) -> None:
        adapter = _elegoo(CurrentStatus=0)
        adapter._start_idle_reaper()
        adapter.disconnect()
        assert adapter._idle_stop.is_set()

    def test_disconnect_is_idempotent(self) -> None:
        adapter = _elegoo(CurrentStatus=0)
        adapter.disconnect()
        adapter.disconnect()  # must not raise
        assert adapter._ws is None

    def test_status_map_covers_every_active_status(self) -> None:
        """Guards the frozenset against a future PrinterStatus rename."""
        from kiln.printers.elegoo import _ACTIVE_PRINT_STATUSES

        assert set(PrinterStatus) >= _ACTIVE_PRINT_STATUSES
        assert PrinterStatus.IDLE not in _ACTIVE_PRINT_STATUSES
