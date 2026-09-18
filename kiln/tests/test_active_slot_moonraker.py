"""The Moonraker adapter's feeding-slot reading through Klipper's unit objects.

``read_active_slot`` is the polled door the base observer asks between
status polls.  It must find the unit-shaped objects once per adapter (one
``/printer/objects/list``), spend one ``/printer/objects/query`` per call,
never raise, never send a command, and carry ``verified=False`` on every
reading -- a documented field is still not one a bench has confirmed.

Shapes covered: Happy Hare's ``mmu`` object (``gate`` / ``tool``, ``-1``
unknown, ``-2`` bypass), the AFC add-on's ``AFC`` object (``current_load``
is the loaded lane name or ``None``), a generic unit answering under one
of the guessed field names, and QIDI's Box modules whose status carries
no slot field at all and so must read as ``None``.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from kiln.printers.base import ActiveSlotReading, PrinterError
from kiln.printers.moonraker import MoonrakerAdapter, _active_slot_unit_objects, _slot_id

HOST = "http://klipper.local:7125"

HH_OBJECTS = {
    "result": {
        "objects": ["gcode", "toolhead", "extruder", "mmu", "mmu_machine",
                    "mmu_stepper unit0_gear", "gcode_macro MMU__LOAD"],
    }
}
AFC_OBJECTS = {"result": {"objects": ["gcode", "toolhead", "AFC", "AFC_stepper lane1", "AFC_hub Turtle_1"]}}
PLAIN_OBJECTS = {"result": {"objects": ["gcode", "toolhead", "extruder", "print_stats"]}}
QIDI_BOX_OBJECTS = {
    "result": {
        "objects": ["gcode", "toolhead", "box_extras", "box_stepper slot0", "box_stepper slot1",
                    "box_rfid card_reader_1", "box_heater_fan heater_fan_a_box1",
                    "heater_generic heater_box1", "temperature_sensor heater_temp_a_box1"],
    }
}


def _query(**objects: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"eventtime": 1.0, "status": dict(objects)}}


def _mmu(**fields: Any) -> dict[str, Any]:
    return _query(mmu={"enabled": True, "filament": "Loaded", "num_gates": 4, **fields})


class _Backend:
    """A scripted ``_get_json``: one answer per path, every call recorded."""

    def __init__(self, list_payload: Any, query_payload: Any) -> None:
        self.list_payload = list_payload
        self.query_payload = query_payload
        self.calls: list[str] = []
        self.params: list[dict[str, Any] | None] = []

    def __call__(self, path: str, **kwargs: Any) -> Any:
        self.calls.append(path)
        self.params.append(kwargs.get("params"))
        payload = {
            "/printer/objects/list": self.list_payload,
            "/printer/objects/query": self.query_payload,
        }.get(path)
        if payload is None:
            raise AssertionError(f"unexpected request: {path}")
        if isinstance(payload, Exception):
            raise payload
        return payload


def _adapter() -> MoonrakerAdapter:
    return MoonrakerAdapter(host=HOST, timeout=5, retries=1)


def _drive(adapter: MoonrakerAdapter, backend: _Backend, calls: int = 1) -> list[Any]:
    with mock.patch.object(adapter, "_get_json", side_effect=backend):
        return [adapter.read_active_slot() for _ in range(calls)]


class TestHappyHare:
    def test_gate_is_the_reading_and_is_unverified(self) -> None:
        backend = _Backend(HH_OBJECTS, _mmu(gate=2, tool=2))
        (reading,) = _drive(_adapter(), backend)

        assert reading == ActiveSlotReading(slot="2", source="moonraker:mmu", verified=False)
        assert isinstance(reading.slot, str)
        assert reading.verified is False

    def test_unknown_gate_reads_as_no_slot(self) -> None:
        backend = _Backend(HH_OBJECTS, _mmu(gate=-1, tool=-1, filament="Unloaded"))
        (reading,) = _drive(_adapter(), backend)
        assert reading == ActiveSlotReading(slot=None, source="moonraker:mmu", verified=False)

    def test_tool_stands_in_when_gate_is_absent_and_bypass_is_no_slot(self) -> None:
        (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, _mmu(tool=1)))
        assert reading is not None and reading.slot == "1"

        (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, _mmu(tool=-2)))
        assert reading is not None and reading.slot is None

    def test_gate_wins_over_tool(self) -> None:
        (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, _mmu(gate=3, tool=0)))
        assert reading is not None and reading.slot == "3"

    def test_without_gate_or_tool_cannot_say(self) -> None:
        (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, _mmu()))
        assert reading is None

        (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, _mmu(gate=True)))
        assert reading is None

    def test_mmu_is_queried_alone(self) -> None:
        backend = _Backend(HH_OBJECTS, _mmu(gate=0))
        adapter = _adapter()
        _drive(adapter, backend)

        assert adapter._active_slot_objects == ["mmu"]
        assert backend.params[1] == {"mmu": ""}


class TestAfc:
    def test_current_load_is_the_lane_name(self) -> None:
        payload = _query(AFC={"current_load": "lane2", "current_lane": None, "next_lane": None})
        (reading,) = _drive(_adapter(), _Backend(AFC_OBJECTS, payload))
        assert reading == ActiveSlotReading(slot="lane2", source="moonraker:AFC", verified=False)

    def test_nothing_loaded_is_a_reading_of_no_slot(self) -> None:
        payload = _query(AFC={"current_load": None, "current_lane": None})
        (reading,) = _drive(_adapter(), _Backend(AFC_OBJECTS, payload))
        assert reading == ActiveSlotReading(slot=None, source="moonraker:AFC", verified=False)

    def test_a_lane_mid_load_is_not_the_loaded_lane(self) -> None:
        payload = _query(AFC={"current_lane": "lane3", "next_lane": "lane3"})
        (reading,) = _drive(_adapter(), _Backend(AFC_OBJECTS, payload))
        assert reading is None

    def test_afc_is_queried_alone(self) -> None:
        backend = _Backend(AFC_OBJECTS, _query(AFC={"current_load": "lane1"}))
        adapter = _adapter()
        _drive(adapter, backend)
        assert adapter._active_slot_objects == ["AFC"]
        assert backend.params[1] == {"AFC": ""}


class TestGenericUnit:
    def test_first_known_field_names_the_slot_and_the_source_names_the_object(self) -> None:
        objects = {"result": {"objects": ["gcode", "ace", "toolhead"]}}
        payload = _query(ace={"temperature": 40, "current_slot": 3, "tool": 0})
        (reading,) = _drive(_adapter(), _Backend(objects, payload))
        assert reading == ActiveSlotReading(slot="3", source="moonraker:ace", verified=False)

    def test_flags_are_skipped_in_favour_of_an_id(self) -> None:
        objects = {"result": {"objects": ["ace"]}}
        (reading,) = _drive(_adapter(), _Backend(objects, _query(ace={"gate": True, "slot": 4})))
        assert reading is not None and reading.slot == "4"

    def test_object_with_none_of_the_fields_cannot_say(self) -> None:
        objects = {"result": {"objects": ["ace"]}}
        (reading,) = _drive(_adapter(), _Backend(objects, _query(ace={"status": "idle", "slot": None})))
        assert reading is None

    def test_bare_objects_are_read_before_instanced_ones(self) -> None:
        objects = {"result": {"objects": ["box_stepper slot0", "box_extras"]}}
        payload = _query(**{"box_stepper slot0": {"slot": "slot0"}, "box_extras": {"slot": "slot2"}})
        adapter = _adapter()
        (reading,) = _drive(adapter, _Backend(objects, payload))

        assert adapter._active_slot_objects == ["box_extras", "box_stepper slot0"]
        assert reading == ActiveSlotReading(slot="slot2", source="moonraker:box_extras", verified=False)

    def test_qidi_box_status_carries_no_slot_and_reads_as_none(self) -> None:
        # The keys the shipped box_extras / box_stepper modules emit from
        # get_status -- endstop, button and RFID states, nothing about a slot.
        payload = _query(**{
            "box_extras": {"b_endstop_state": False, "box_button_state": 0, "e_endstop_state": True},
            "box_stepper slot0": {"r_endstop_state": True, "rfid_state": 0},
            "box_stepper slot1": {"r_endstop_state": False, "rfid_state": 0},
        })
        adapter = _adapter()
        backend = _Backend(QIDI_BOX_OBJECTS, payload)
        (reading,) = _drive(adapter, backend)

        assert reading is None
        assert backend.calls == ["/printer/objects/list", "/printer/objects/query"]
        assert "box_extras" in (adapter._active_slot_objects or [])
        assert "heater_generic heater_box1" not in (adapter._active_slot_objects or [])


class TestDiscovery:
    def test_no_unit_objects_cannot_say_and_never_queries(self) -> None:
        backend = _Backend(PLAIN_OBJECTS, _mmu(gate=1))
        readings = _drive(_adapter(), backend, calls=2)

        assert readings == [None, None]
        assert backend.calls == ["/printer/objects/list"]

    def test_sensors_and_things_named_after_a_unit_are_not_units(self) -> None:
        objects = {"result": {"objects": [
            "filament_switch_sensor ams_runout", "filament_motion_sensor box_encoder",
            "temperature_sensor heater_temp_a_box1", "heater_generic heater_box1",
            "gcode_macro AFC_PARK", "print_stats",
        ]}}
        backend = _Backend(objects, _query())
        (reading,) = _drive(_adapter(), backend)

        assert reading is None
        assert backend.calls == ["/printer/objects/list"]

    def test_unit_object_selection_from_the_object_list(self) -> None:
        assert _active_slot_unit_objects(["mmu", "mmu_machine", "AFC"]) == ["mmu"]
        assert _active_slot_unit_objects(["AFC", "AFC_stepper lane1"]) == ["AFC"]
        assert _active_slot_unit_objects(["ercf", "cfs", "filament_box", "qidi_box", "ACE"]) == [
            "ercf", "cfs", "filament_box", "qidi_box", "ACE",
        ]
        assert _active_slot_unit_objects(["box_stepper slot0", "box_extras"]) == ["box_extras", "box_stepper slot0"]
        assert _active_slot_unit_objects(["filament_switch_sensor mmu_gate", "space heater"]) == []

    def test_object_list_is_fetched_once_across_calls(self) -> None:
        backend = _Backend(HH_OBJECTS, _mmu(gate=2))
        adapter = _adapter()
        readings = _drive(adapter, backend, calls=2)

        assert [r.slot for r in readings] == ["2", "2"]
        assert backend.calls == ["/printer/objects/list", "/printer/objects/query", "/printer/objects/query"]

    def test_failed_object_list_is_not_cached(self) -> None:
        adapter = _adapter()
        failing = _Backend(PrinterError("moonraker unreachable"), _mmu(gate=2))
        (first,) = _drive(adapter, failing)
        assert first is None
        assert adapter._active_slot_objects is None

        misshapen = _Backend({"result": {"objects": "not a list"}}, _mmu(gate=2))
        (second,) = _drive(adapter, misshapen)
        assert second is None
        assert adapter._active_slot_objects is None

        working = _Backend(HH_OBJECTS, _mmu(gate=2))
        (third,) = _drive(adapter, working)
        assert third is not None and third.slot == "2"
        assert working.calls[0] == "/printer/objects/list"


class TestSafety:
    def test_failing_query_returns_none_and_never_raises(self) -> None:
        for failure in (
            PrinterError("moonraker unreachable"),
            RuntimeError("socket blew up"),
            {"result": {"status": "not a dict"}},
            {"result": {"status": {"mmu": "not a dict"}}},
        ):
            (reading,) = _drive(_adapter(), _Backend(HH_OBJECTS, failure))
            assert reading is None, failure

    def test_reading_sends_no_command(self) -> None:
        adapter = _adapter()
        backend = _Backend(HH_OBJECTS, _mmu(gate=1))
        with mock.patch.object(adapter, "_post") as post, mock.patch.object(adapter, "_send_gcode") as gcode:
            (reading,) = _drive(adapter, backend)

        assert reading is not None and reading.slot == "1"
        post.assert_not_called()
        gcode.assert_not_called()
        assert set(backend.calls) <= {"/printer/objects/list", "/printer/objects/query"}

    def test_adapter_built_without_init_still_answers(self) -> None:
        adapter = MoonrakerAdapter.__new__(MoonrakerAdapter)
        adapter._get_json = _Backend(HH_OBJECTS, _mmu(gate=0))  # type: ignore[method-assign]
        reading = adapter.read_active_slot()
        assert reading is not None and reading.slot == "0"


class TestSlotId:
    def test_numbers_names_and_nothing(self) -> None:
        assert _slot_id(2) == (True, "2")
        assert _slot_id("02") == (True, "2")
        assert _slot_id(2.0) == (True, "2")
        assert _slot_id("lane2") == (True, "lane2")
        assert _slot_id(" A1 ") == (True, "A1")
        assert _slot_id(-1) == (True, None)
        assert _slot_id("-2") == (True, None)
        assert _slot_id("") == (True, None)

    def test_things_that_are_not_an_id(self) -> None:
        assert _slot_id(True) == (False, None)
        assert _slot_id(1.5) == (False, None)
        assert _slot_id([1]) == (False, None)
        assert _slot_id({"gate": 1}) == (False, None)
