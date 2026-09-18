"""The polled slot observer every backend gets from the base class.

A backend that can only be polled reads its multi-material unit's feeding
slot through one optional door; the base class watches the value between
status polls and reports a change the way the MQTT push path does natively.

Pinned here:
  - the first reading is a baseline, never a change;
  - a change while Kiln is not driving is reported once, flagged unverified
    when the backend's field is;
  - a change during a print Kiln started is counted against the charge held
    from the start, not reported;
  - the door is asked at most once per interval, never on a disconnected
    state, and never for a backend with its own stream;
  - a reader that raises never breaks status.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiln.printers import base
from kiln.printers.base import ActiveSlotReading, PrinterState, PrinterStatus


class _Polled:
    """The slice of an adapter the observer touches."""

    name = "k2"
    _slot_observer_native = False

    def __init__(self, readings):
        self.readings = list(readings)
        self.asked = 0
        self._cutter_print = None

    def read_active_slot(self):
        self.asked += 1
        value = self.readings.pop(0) if self.readings else None
        if isinstance(value, Exception):
            raise value
        return value

    def declared_printer_model(self):
        return "k2"

    def _kiln_is_driving(self):
        return False


def _state(connected=True):
    return PrinterState(connected=connected, state=PrinterStatus.IDLE)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(base.time, "monotonic", lambda: now["t"])
    return now


@pytest.fixture
def reported(monkeypatch):
    seen = []
    monkeypatch.setattr("kiln._pro_cutter_bridge.record_observed_switch", lambda name, **kw: seen.append((name, kw)))
    return seen


def _reading(slot, verified=False):
    return ActiveSlotReading(slot=slot, source="moonraker:mmu", verified=verified)


class TestTheObserver:
    def test_the_first_reading_is_a_baseline_and_a_change_is_reported_once(self, clock, reported):
        adapter = _Polled([_reading("1"), _reading("2"), _reading("2")])
        base._feed_slot_observer(adapter, _state())
        assert reported == []
        clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        base._feed_slot_observer(adapter, _state())
        clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        base._feed_slot_observer(adapter, _state())
        assert reported == [("k2", {"printer_model": "k2", "from_tray": "1", "to_tray": "2", "verified": False})]

    def test_a_verified_field_reports_verified(self, clock, reported):
        adapter = _Polled([_reading("1", True), _reading(None, True)])
        base._feed_slot_observer(adapter, _state())
        clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        base._feed_slot_observer(adapter, _state())
        assert reported[0][1]["verified"] is True and reported[0][1]["to_tray"] is None

    def test_a_change_during_kilns_print_is_counted_against_the_charge(self, clock, reported):
        adapter = _Polled([_reading("1"), _reading("2")])
        adapter._kiln_is_driving = lambda: True
        adapter._cutter_print = {"file": "f.3mf", "planned": 3, "observed": 0}
        base._feed_slot_observer(adapter, _state())
        clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        base._feed_slot_observer(adapter, _state())
        assert reported == [] and adapter._cutter_print["observed"] == 1

    def test_the_door_is_asked_at_most_once_per_interval(self, clock):
        adapter = _Polled([_reading("1")] * 5)
        for _ in range(4):
            base._feed_slot_observer(adapter, _state())
        assert adapter.asked == 1
        clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        base._feed_slot_observer(adapter, _state())
        assert adapter.asked == 2

    def test_a_disconnected_state_and_a_native_stream_are_left_alone(self, clock):
        adapter = _Polled([_reading("1")])
        base._feed_slot_observer(adapter, _state(connected=False))
        assert adapter.asked == 0
        adapter._slot_observer_native = True
        base._feed_slot_observer(adapter, _state())
        assert adapter.asked == 0

    def test_a_backend_that_cannot_say_is_not_a_change(self, clock, reported):
        adapter = _Polled([_reading("1"), None, _reading("1")])
        for _ in range(3):
            base._feed_slot_observer(adapter, _state())
            clock["t"] += base.SLOT_OBSERVE_MIN_INTERVAL_S
        assert reported == []

    def test_a_raising_reader_never_breaks_status(self, clock):
        adapter = _Polled([RuntimeError("no unit")])
        with pytest.raises(RuntimeError):
            base._feed_slot_observer(adapter, _state())
        # The get_state wrap swallows it; prove the wrap does, on a concrete
        # subclass built the way any backend is (every abstract door stubbed).
        seen = {}
        stubs = {n: (lambda self, *a, **k: None) for n in base.PrinterAdapter.__abstractmethods__}
        stubs["get_state"] = lambda self: _state()
        stubs["name"] = property(lambda self: "x")
        _Adapter = type("_Adapter", (base.PrinterAdapter,), stubs)
        with mock.patch.object(base, "_feed_slot_observer", side_effect=RuntimeError("boom")), \
                mock.patch.object(base, "_feed_outcome_lifecycle", lambda a, s: seen.setdefault("fed", True)):
            adapter = _Adapter.__new__(_Adapter)
            assert _Adapter.get_state(adapter).connected is True
        assert seen == {"fed": True}
