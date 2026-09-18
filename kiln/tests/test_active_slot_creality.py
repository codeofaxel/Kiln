"""The Creality adapter's feeding-slot reading through Moonraker's CFS objects.

``read_active_slot`` is the polled door the base observer asks between
status polls.  It reuses the CFS discovery ``get_cfs_status`` makes but must
stay cheap (one object list per adapter, one query per call, never the
G-code help list), must never raise, and must carry ``verified=False``
because the slot flags are a keyword guess no bench has confirmed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from kiln.printers.base import ActiveSlotReading, PrinterError
from kiln.printers.creality import CrealityAdapter

OBJECTS_WITH_CFS = {
    "result": {"objects": ["print_stats", "cfs", "filament_switch_sensor runout"]}
}
OBJECTS_WITHOUT_CFS = {"result": {"objects": ["print_stats", "toolhead"]}}


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = payload
    return response


def _adapter() -> CrealityAdapter:
    with patch("kiln.printers.creality.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"result": {"klippy_state": "ready"}})
        return CrealityAdapter("k1-max.local", timeout=5, retries=1)


def _box(box_id: Any, **extra: Any) -> dict[str, Any]:
    return {"boxId": box_id, "materialId": "PLA", "color": "#FFFFFF", "remain": 80, **extra}


def _cfs_query(*boxes: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"status": {"cfs": {"boxsInfo": list(boxes)}}}}


class _Backend:
    """A scripted ``_get_json``: one answer per path, every call recorded."""

    def __init__(self, list_payload: Any, query_payload: Any) -> None:
        self.list_payload = list_payload
        self.query_payload = query_payload
        self.calls: list[str] = []

    def __call__(self, path: str, **kwargs: Any) -> Any:
        self.calls.append(path)
        payload = {
            "/printer/objects/list": self.list_payload,
            "/printer/objects/query": self.query_payload,
        }.get(path)
        if payload is None:
            raise AssertionError(f"unexpected request: {path}")
        if isinstance(payload, Exception):
            raise payload
        return payload


def _drive(adapter: CrealityAdapter, backend: _Backend, calls: int = 1) -> list[Any]:
    with patch.object(adapter._backend, "_get_json", side_effect=backend):
        return [adapter.read_active_slot() for _ in range(calls)]


class TestReadActiveSlot:
    def test_one_loaded_slot_is_the_reading_and_is_unverified(self) -> None:
        backend = _Backend(
            OBJECTS_WITH_CFS,
            _cfs_query(_box(0, loaded=False), _box(1, loaded=False), _box(2, loaded=True), _box(3, loaded=False)),
        )
        (reading,) = _drive(_adapter(), backend)

        assert reading == ActiveSlotReading(slot="2", source="moonraker_cfs", verified=False)
        assert isinstance(reading.slot, str)
        assert reading.verified is False

    def test_nothing_loaded_is_a_reading_of_no_slot(self) -> None:
        backend = _Backend(
            OBJECTS_WITH_CFS,
            _cfs_query(_box(0, loaded=False), _box(1, loaded=0), _box(2, loaded="false")),
        )
        (reading,) = _drive(_adapter(), backend)

        assert reading == ActiveSlotReading(slot=None, source="moonraker_cfs", verified=False)

    def test_selected_and_active_spellings_name_the_slot(self) -> None:
        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box("A", selected=False), _box("B", selected=1)))
        (reading,) = _drive(_adapter(), backend)
        assert reading is not None and reading.slot == "B"

        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box(1, active="true"), _box(2, active="0")))
        (reading,) = _drive(_adapter(), backend)
        assert reading is not None and reading.slot == "1"

    def test_no_cfs_objects_cannot_say_and_never_queries(self) -> None:
        backend = _Backend(OBJECTS_WITHOUT_CFS, _cfs_query(_box(0, loaded=True)))
        readings = _drive(_adapter(), backend, calls=2)

        assert readings == [None, None]
        assert backend.calls == ["/printer/objects/list"]

    def test_payload_that_never_flags_a_slot_cannot_say(self) -> None:
        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box(0), _box(1), _box(2), _box(3)))
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

    def test_two_slots_claiming_loaded_cannot_say(self) -> None:
        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box(0, loaded=True), _box(1, loaded=True)))
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

    def test_loaded_slot_without_an_id_cannot_say(self) -> None:
        nameless = {"materialId": "PLA", "color": "#000000", "loaded": True}
        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(nameless))
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

    def test_failing_query_returns_none_and_never_raises(self) -> None:
        backend = _Backend(OBJECTS_WITH_CFS, PrinterError("moonraker unreachable"))
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

        backend = _Backend(OBJECTS_WITH_CFS, RuntimeError("socket blew up"))
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

        backend = _Backend(OBJECTS_WITH_CFS, {"result": {"status": "not a dict"}})
        (reading,) = _drive(_adapter(), backend)
        assert reading is None

    def test_object_list_is_fetched_once_across_calls(self) -> None:
        backend = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box(0, loaded=True), _box(1, loaded=False)))
        adapter = _adapter()
        readings = _drive(adapter, backend, calls=3)

        assert [r.slot for r in readings] == ["0", "0", "0"]
        assert backend.calls == [
            "/printer/objects/list",
            "/printer/objects/query",
            "/printer/objects/query",
            "/printer/objects/query",
        ]
        assert "/printer/gcode/help" not in backend.calls
        assert adapter._cfs_object_names == ["cfs"]

    def test_failed_object_list_is_not_cached(self) -> None:
        adapter = _adapter()
        failing = _Backend(PrinterError("moonraker unreachable"), _cfs_query(_box(0, loaded=True)))
        (first,) = _drive(adapter, failing)
        assert first is None
        assert adapter._cfs_object_names is None

        working = _Backend(OBJECTS_WITH_CFS, _cfs_query(_box(0, loaded=True)))
        (second,) = _drive(adapter, working)
        assert second is not None and second.slot == "0"
        assert working.calls[0] == "/printer/objects/list"

    def test_adapter_built_without_init_still_answers(self) -> None:
        adapter = CrealityAdapter.__new__(CrealityAdapter)
        adapter._backend = MagicMock()
        adapter._backend._get_json.side_effect = _Backend(
            OBJECTS_WITH_CFS, _cfs_query(_box(0, loaded=False), _box(1, loaded=True))
        )
        reading = adapter.read_active_slot()
        assert reading is not None and reading.slot == "1"
