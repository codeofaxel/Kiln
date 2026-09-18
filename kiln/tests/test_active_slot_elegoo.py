"""The Elegoo adapter reads its multi-colour unit's feeding slot from the cache it already holds.

The SDCP V3.0.0 status report has no slot field.  The slot is client-observed
in the reply to the unit-status request (SDCP command 324): ``active_canvas_id``
and ``active_tray_id``, zero-based, ``-1`` when nothing is feeding.  The
adapter's message handler merges that reply flat into ``_last_status`` like any
other reply, and :meth:`ElegooAdapter.read_active_slot` only reads what is there.

These tests seed the cache directly, the way the other Elegoo tests do, and
never touch the network.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from kiln.printers.base import ActiveSlotReading, _feed_slot_observer
from kiln.printers.elegoo import ElegooAdapter, _slot_index

HOST = "192.168.1.50"
MAINBOARD_ID = "ABCD1234ABCD1234"

#: A unit-status reply as the adapter's message handler leaves it in the
#: cache: the reply's ``Data`` block merged flat beside the ordinary status
#: keys.  Shape from the clients that parse it; not from a printer in this
#: repo's hands.
_UNIT_REPLY: dict[str, Any] = {
    "CurrentStatus": 0,
    "TempOfNozzle": 25.0,
    "Ack": 0,
    "active_canvas_id": 0,
    "active_tray_id": 2,
    "auto_refill": 1,
    "canvas_list": [
        {
            "canvas_id": 0,
            "connected": 1,
            "tray_list": [
                {"tray_id": 0, "status": 1, "filament_type": "PLA"},
                {"tray_id": 2, "status": 1, "filament_type": "PETG"},
            ],
        }
    ],
}


def _adapter(status: Any) -> ElegooAdapter:
    """An adapter whose status cache holds *status*, with no connection."""
    adapter = ElegooAdapter(host=HOST, mainboard_id=MAINBOARD_ID, timeout=2)
    adapter._last_status = status
    return adapter


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------


def test_feeding_slot_reads_the_tray_id_unverified() -> None:
    reading = _adapter(dict(_UNIT_REPLY)).read_active_slot()

    assert reading == ActiveSlotReading(slot="2", source="sdcp:active_tray_id", verified=False)


def test_verified_stays_false_even_for_a_fully_populated_reply() -> None:
    # The field is client-observed, not proven on hardware: no payload,
    # however complete, earns a verified reading from this backend.
    reading = _adapter(dict(_UNIT_REPLY)).read_active_slot()

    assert reading is not None
    assert reading.verified is False


def test_minus_one_is_an_explicit_nothing_feeding() -> None:
    status = dict(_UNIT_REPLY, active_tray_id=-1)

    reading = _adapter(status).read_active_slot()

    # A reading whose slot is None, not "cannot say": the machine answered.
    assert reading is not None
    assert reading.slot is None
    assert reading.verified is False


def test_tray_zero_is_a_real_slot() -> None:
    reading = _adapter(dict(_UNIT_REPLY, active_tray_id=0)).read_active_slot()

    assert reading is not None
    assert reading.slot == "0"


def test_status_without_the_field_cannot_say() -> None:
    reading = _adapter({"CurrentStatus": 13, "TempOfNozzle": 200.0}).read_active_slot()

    assert reading is None


def test_empty_cache_cannot_say() -> None:
    assert _adapter({}).read_active_slot() is None


def test_canvas_info_wrapper_is_read_too() -> None:
    # Some clients see the block wrapped; the flat keys are absent then.
    status = {
        "CurrentStatus": 0,
        "canvas_info": {"active_canvas_id": 0, "active_tray_id": 1, "canvas_list": []},
    }

    reading = _adapter(status).read_active_slot()

    assert reading is not None
    assert reading.slot == "1"


def test_a_later_unit_is_named_with_its_tray() -> None:
    reading = _adapter(dict(_UNIT_REPLY, active_canvas_id=1, active_tray_id=3)).read_active_slot()

    assert reading is not None
    assert reading.slot == "1:3"


def test_digit_strings_are_read_as_indexes() -> None:
    reading = _adapter(dict(_UNIT_REPLY, active_canvas_id="0", active_tray_id="3")).read_active_slot()

    assert reading is not None
    assert reading.slot == "3"


@pytest.mark.parametrize("bad", ["abc", True, False, [], {}, None, 2.5, object()])
def test_a_tray_id_that_is_not_an_index_cannot_say(bad: Any) -> None:
    assert _adapter(dict(_UNIT_REPLY, active_tray_id=bad)).read_active_slot() is None


def test_a_wrapper_that_is_not_a_block_falls_back_to_the_flat_keys() -> None:
    assert _adapter({"canvas_info": "bogus"}).read_active_slot() is None
    reading = _adapter(dict(_UNIT_REPLY, canvas_info="bogus")).read_active_slot()
    assert reading is not None
    assert reading.slot == "2"


@pytest.mark.parametrize("broken", ["garbage", None, 42])
def test_never_raises_on_a_broken_cache(broken: Any) -> None:
    assert _adapter(broken).read_active_slot() is None


def test_reads_only_and_never_sends() -> None:
    adapter = _adapter(dict(_UNIT_REPLY))
    adapter._ws = mock.MagicMock()
    adapter._connected = True

    with mock.patch.object(ElegooAdapter, "_send_command") as send:
        adapter.read_active_slot()
        adapter.read_active_slot()

    send.assert_not_called()
    adapter._ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# The door the reply enters through, and the observer that watches it
# ---------------------------------------------------------------------------


def test_a_unit_status_reply_through_the_message_handler_is_readable() -> None:
    # The SDCP response frame for command 324, as the WebSocket delivers it.
    adapter = _adapter({"CurrentStatus": 0})
    frame = {
        "Id": "abc",
        "Data": {
            "Cmd": 324,
            "Data": {
                "Ack": 0,
                "active_canvas_id": 0,
                "active_tray_id": 2,
                "auto_refill": 1,
                "canvas_list": [],
            },
            "RequestID": "req-1",
            "MainboardID": MAINBOARD_ID,
            "TimeStamp": 1,
        },
        "Topic": f"sdcp/response/{MAINBOARD_ID}",
    }

    adapter._handle_message(frame)

    reading = adapter.read_active_slot()
    assert reading is not None
    assert reading.slot == "2"
    # The ordinary status keys survive the merge.
    assert adapter._last_status["CurrentStatus"] == 0


def test_the_polled_observer_reports_a_change_as_unverified() -> None:
    adapter = _adapter(dict(_UNIT_REPLY, active_tray_id=0))
    state = adapter._build_state_from_cache({"CurrentStatus": 0})
    assert state.connected

    with (
        mock.patch.object(ElegooAdapter, "_kiln_is_driving", return_value=False),
        mock.patch("kiln._pro_cutter_bridge.record_observed_switch") as record,
    ):
        _feed_slot_observer(adapter, state)  # baseline: never a change
        record.assert_not_called()

        adapter._slot_observed_at = 0.0  # past the rate limit
        adapter._last_status["active_tray_id"] = 2
        _feed_slot_observer(adapter, state)

    record.assert_called_once()
    kwargs = record.call_args.kwargs
    assert kwargs["from_tray"] == "0"
    assert kwargs["to_tray"] == "2"
    assert kwargs["verified"] is False


def test_the_polled_observer_stays_quiet_when_the_cache_cannot_say() -> None:
    adapter = _adapter({"CurrentStatus": 0})
    state = adapter._build_state_from_cache({"CurrentStatus": 0})

    with mock.patch("kiln._pro_cutter_bridge.record_observed_switch") as record:
        _feed_slot_observer(adapter, state)
        adapter._slot_observed_at = 0.0
        _feed_slot_observer(adapter, state)

    record.assert_not_called()
    assert getattr(adapter, "_slot_last_seen", None) is None


# ---------------------------------------------------------------------------
# The index helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (3, 3), (-1, -1), ("2", 2), (" 1 ", 1), ("-1", -1)],
)
def test_slot_index_reads_whole_numbers(value: Any, expected: int) -> None:
    assert _slot_index(value) == expected


@pytest.mark.parametrize("value", [True, False, 1.0, "x", "", None, [], {}])
def test_slot_index_refuses_what_is_not_an_index(value: Any) -> None:
    assert _slot_index(value) is None
