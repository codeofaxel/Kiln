"""A Bambu tray id is computed in ONE place, the way the vendor's own slicer computes it.

The vendor numbers a tray by its UNIT TYPE, never by printer model:

* a chained unit -- AMS, AMS Lite, AMS 2 Pro; unit ids 0-15 -- names its
  trays ``unit * 4 + slot``;
* an AMS HT -- one slot, unit ids 128-135 -- is addressed by its unit id
  itself: tray 128 is HT-A;
* 254 is the external spool and 255 is "no tray" in ``tray_now`` and in
  the load command's ``target``; neither belongs in ``ams_mapping``, where
  an unmapped or external filament is ``-1``;
* newer firmware names the feeding tray per nozzle in the extruder block,
  packed ``(unit << 8) | slot``, and there ``tray_now`` may be a local slot.

The rule lives in :mod:`kiln.bambu_trays`.  Kiln applied the chained rule
to every id, so an AMS HT feeding the nozzle read as "unit 32", its tray
was "not present" to the load command, and a spool on a second chained
unit was auto-routed and colour-checked as unit 0's.  The owner's A1
carries one AMS Lite (unit 0, trays 0-3), so nothing below is
bench-verified beyond that unit; every other case is pinned from the
vendor's own source and from community status captures.
"""

from __future__ import annotations

import json
import time
import zipfile
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Readings, in the shape get_ams_status hands out
# ---------------------------------------------------------------------------


def _unit(unit_id: int, *trays: tuple[int, str, str]) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "trays": [
            {"slot": slot, "tray_type": material, "tray_color": colour,
             "remain": 42, "remaining_known": True}
            for slot, material, colour in trays
        ],
    }


def _ams(tray_now: str, *units: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"tray_now": tray_now, "ams_exist_bits": "1", "tray_exist_bits": "f",
            "units": list(units), **fields}


#: Unit 0 (A) with red PLA in A1 and white PLA in A2; unit 1 (B) with blue
#: PETG in B2; an AMS HT (HT-B, unit 129) holding near-black ABS.
_A = _unit(0, (0, "PLA", "FF0000FF"), (1, "PLA", "FFFFFFFF"))
_B = _unit(1, (1, "PETG", "0000FFFF"))
_HT_B = _unit(129, (0, "ABS", "111111FF"))  # 000000 would mean "colour not read"


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestTheOneHelper:
    def test_a_chained_unit_numbers_its_trays_four_apart(self):
        from kiln.bambu_trays import tray_id

        assert [tray_id(0, s) for s in range(4)] == [0, 1, 2, 3]
        assert [tray_id(1, s) for s in range(4)] == [4, 5, 6, 7]
        assert tray_id(3, 3) == 15
        assert tray_id(15, 3) == 63  # Studio's ``ams_id < 16`` ceiling

    def test_an_ams_ht_is_addressed_by_its_unit_id(self):
        from kiln.bambu_trays import tray_id

        assert tray_id(128, 0) == 128
        assert tray_id(135, 0) == 135

    def test_an_impossible_pair_is_refused_not_invented(self):
        from kiln.bambu_trays import tray_id

        for unit, slot in ((128, 1), (16, 0), (127, 0), (136, 0), (-1, 0), (0, -1)):
            with pytest.raises(ValueError):
                tray_id(unit, slot)

    def test_a_wire_id_reads_back_to_its_unit_and_slot(self):
        from kiln.bambu_trays import read_tray_id

        five = read_tray_id("5")
        assert five is not None and (five.unit, five.slot, five.tray_id) == (1, 1, 5)
        assert read_tray_id(0) is not None and read_tray_id(0).unit == 0
        ht = read_tray_id("129")
        assert ht is not None and (ht.unit, ht.slot) == (129, 0)

    def test_the_two_sentinels_are_neither_units_nor_slots(self):
        from kiln.bambu_trays import EXTERNAL_SPOOL_TRAY, NO_TRAY, read_tray_id

        ext = read_tray_id("254")
        assert ext is not None and ext.external and not ext.loaded_tray
        assert (ext.unit, ext.slot, ext.tray_id) == (None, None, EXTERNAL_SPOOL_TRAY)
        none = read_tray_id(255)
        assert none is not None and none.none and not none.loaded_tray
        assert (none.unit, none.slot, none.tray_id) == (None, None, NO_TRAY)

    def test_junk_is_none_never_a_tray(self):
        from kiln.bambu_trays import read_tray_id

        for junk in (None, "", "  ", "abc", "-1", -1, 64, 127, 136, 253, 256, 3.5):
            assert read_tray_id(junk) is None, junk

    def test_the_round_trip_holds_for_every_id_the_protocol_defines(self):
        from kiln.bambu_trays import read_tray_id, tray_id

        for tid in [*range(64), *range(128, 136)]:
            ref = read_tray_id(tid)
            assert ref is not None and ref.loaded_tray
            assert tray_id(ref.unit, ref.slot) == tid

    def test_names_are_studios_own(self):
        from kiln.bambu_trays import read_tray_id, tray_name, unit_name

        assert [tray_name(0, s) for s in range(4)] == ["A1", "A2", "A3", "A4"]
        assert tray_name(1, 1) == "B2"
        assert tray_name(3, 3) == "D4"
        assert tray_name(128, 0) == "HT-A"
        assert tray_name(135, 0) == "HT-H"
        assert (unit_name(0), unit_name(3), unit_name(128), unit_name(135)) == ("A", "D", "HT-A", "HT-H")
        assert read_tray_id("5").name == "B2"
        assert read_tray_id("254").name == "Ext"

    def test_a_sentence_carries_both_the_id_and_the_name(self):
        from kiln.bambu_trays import describe_tray_id

        assert describe_tray_id(5) == "tray 5 (slot B2)"
        assert describe_tray_id("129") == "tray 129 (slot HT-B)"
        assert describe_tray_id(254) == "the external spool"
        assert describe_tray_id(255) == "no tray"
        assert describe_tray_id("abc") == "tray 'abc' (not an id this printer uses)"


# ---------------------------------------------------------------------------
# Every reader, the same id
# ---------------------------------------------------------------------------


class TestTheSharedRecord:
    def test_an_ams_ht_tray_carries_its_unit_id_as_its_tray_id(self):
        from kiln.ams_routing import loaded_trays

        trays = loaded_trays(_ams("129", _A, _B, _HT_B))
        assert [t.tray_id for t in trays] == [0, 1, 5, 129]
        assert [t.label for t in trays] == [
            "red PLA in slot A1",
            "white PLA in slot A2",
            "blue PETG in slot B2",
            "black ABS in slot HT-B",
        ]

    def test_the_feeding_tray_is_read_the_same_way(self):
        from kiln.multi_material import from_bambu_ams

        assert from_bambu_ams(_ams("129", _A, _HT_B), printer_model="bambu_p2s").feeding == (129, 0)
        assert from_bambu_ams(_ams("5", _A, _B), printer_model="bambu_x1c").feeding == (1, 1)
        ext = from_bambu_ams(_ams("254", _A), printer_model="bambu_a1")
        assert ext.feeding is None and ext.external_spool
        none = from_bambu_ams(_ams("255", _A), printer_model="bambu_a1")
        assert none.feeding is None and not none.external_spool

    def test_loaded_slots_in_the_record_name_their_unit(self):
        from kiln.multi_material import from_bambu_ams

        slots = from_bambu_ams(_ams("255", _B, _HT_B), printer_model="bambu_p2s").to_dict()["loaded_slots"]
        assert [(s["tray_id"], s["name"]) for s in slots] == [(5, "B2"), (129, "HT-B")]


class TestTheRoutingPlan:
    def test_the_plan_hands_the_print_command_the_ht_id(self):
        from kiln.ams_routing import Filament, loaded_trays, plan_ams_mapping

        plan = plan_ams_mapping([Filament(hex6="111111")], loaded_trays(_ams("255", _A, _HT_B)))
        assert plan.ok and plan.mapping == [129]
        assert plan.matches[0]["slot"] == 129 and plan.matches[0]["name"] == "HT-B"

    def test_the_summary_names_the_unit_never_a_bare_slot_number(self):
        from kiln.ams_routing import Filament, loaded_trays, plan_ams_mapping

        plan = plan_ams_mapping(
            [Filament(hex6="0000FF"), Filament(hex6="FF0000")],
            loaded_trays(_ams("255", _A, _B)),
        )
        assert plan.mapping == [5, 0]
        assert plan.summary == "blue → slot B2, red → slot A1"

    def test_an_unread_colour_warning_names_the_unit_too(self):
        from kiln.ams_routing import Filament, loaded_trays, plan_ams_mapping

        unread = _unit(1, (0, "PLA", "000000FF"))  # all-zero = colour not read
        plan = plan_ams_mapping([Filament(hex6="FF0000", material="PLA")], loaded_trays(_ams("255", unread)))
        assert plan.ok and plan.mapping == [4]
        assert plan.warnings == ["slot B1's colour was not read; matched on material only"]

    def test_the_colour_advisor_keys_by_the_same_id(self):
        from kiln.ams_routing import advise_colours, loaded_trays

        out = advise_colours(["111111"], loaded_trays(_ams("255", _A, _HT_B)), printer="p2s")
        assert out is not None and out.matched[0]["slot"] == 129
        assert out.matched[0]["nearest"] == "black ABS in slot HT-B"


class _MockMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(scope="module")
def get_active_material():
    from kiln.plugins.material_tools import plugin

    mcp = _MockMCP()
    plugin.register(mcp)
    return mcp.tools["get_active_material"]


def _report(fn, ams):
    adapter = mock.MagicMock()
    adapter.get_ams_status.return_value = ams
    with mock.patch("kiln.server._get_adapter", return_value=adapter):
        return fn()


class TestTheReportingDoor:
    def test_an_ams_ht_feeding_the_nozzle_is_found(self, get_active_material):
        r = _report(get_active_material, _ams("129", _A, _HT_B))
        assert r["material"] == "ABS"
        assert (r["active_slot"], r["active_unit"], r["active_tray"]) == (129, 129, 0)
        assert r["active_slot_name"] == "HT-B"
        assert "tray 129 (slot HT-B)" in r["message"]

    def test_a_second_units_tray_is_said_with_its_unit(self, get_active_material):
        r = _report(get_active_material, _ams("5", _A, _B))
        assert (r["material"], r["active_slot"], r["active_slot_name"]) == ("PETG", 5, "B2")
        assert "tray 5 (slot B2)" in r["message"]
        assert "slot 1" not in r["message"] and "slot 2)" not in r["message"].replace("slot B2)", "")

    def test_loaded_slots_carry_names_beside_the_ids(self, get_active_material):
        r = _report(get_active_material, _ams("255", _B, _HT_B))
        assert r["source"] == "ams_loaded_unknown_slot"
        assert r["loaded_slots"] == [5, 129]
        assert r["loaded_slot_names"] == ["B2", "HT-B"]

    def test_the_learning_door_reads_the_same_ht_id(self):
        from kiln.plugins.learning_tools import _material_from_printer

        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = _ams("129", _A, _HT_B)
        with mock.patch("kiln.server._resolve_adapter", return_value=adapter):
            assert _material_from_printer("default") == "ABS"


class TestTheServerSelection:
    def test_the_selection_record_finds_an_ht_colour_and_names_it(self):
        from kiln.server import _ams_selection_record

        rec = _ams_selection_record(129, "ABS", _ams("255", _A, _HT_B))
        assert rec == {"slot": 129, "name": "HT-B", "type": "ABS", "color": "111111FF"}
        assert _ams_selection_record(5, "PETG", _ams("255", _A, _B))["name"] == "B2"

    def _resolve(self, ams: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        from kiln.server import _resolve_use_ams

        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = ams
        return _resolve_use_ams("auto", None, adapter, **kwargs)

    def test_a_material_hint_routes_to_the_printers_id_of_the_tray(self):
        out = self._resolve(_ams("255", _A, _B), material="PETG")
        assert out["use_ams"] is True and out["ams_mapping"] == [5]
        assert out["selection"]["slot"] == 5 and out["selection"]["name"] == "B2"

    def test_the_first_loaded_tray_on_an_ht_is_routed_by_its_own_id(self):
        out = self._resolve(_ams("255", _HT_B))
        assert out["ams_mapping"] == [129]
        assert out["selection"] == {"slot": 129, "name": "HT-B", "type": "ABS", "color": "111111FF"}

    def test_the_no_match_warning_names_the_unit(self):
        out = self._resolve(_ams("255", _B), material="PLA")
        assert out["ams_mapping"] == [5]
        assert out["warnings"] == ["No AMS tray matches material 'PLA'; using tray 5 (slot B2) (PETG) instead."]


# ---------------------------------------------------------------------------
# The adapter: what goes on the wire and what is checked against it
# ---------------------------------------------------------------------------


def _adapter(**kwargs: Any):
    from kiln.printers.bambu import BambuAdapter

    adapter = BambuAdapter(host="192.168.1.100", access_code="12345678", serial="01P00A000000001", timeout=2, **kwargs)
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    adapter._last_state_time = time.monotonic()
    return adapter


#: The raw MQTT ``ams`` array behind the readings above: each unit's own
#: ``id`` and each tray's own ``id`` (0-3), exactly as the firmware sends it.
_RAW_AMS = [
    {"id": "0", "tray": [
        {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"},
        {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
    ]},
    {"id": "1", "tray": [{"id": "1", "tray_type": "PETG", "tray_color": "0000FFFF"}]},
    {"id": "129", "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "111111FF"}]},
]


class TestTheAdapter:
    def test_ams_status_reports_the_printers_id_and_name_beside_each_slot(self):
        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "129"}}
        status = adapter.get_ams_status()
        # ``unit_id`` / ``slot`` stay as the firmware sent them (strings);
        # ``tray_id`` is Kiln's own number and is always an int.
        units = {int(u["unit_id"]): u for u in status["units"]}
        assert [units[0]["name"], units[1]["name"], units[129]["name"]] == ["A", "B", "HT-B"]
        assert [(int(t["slot"]), t["tray_id"], t["name"]) for t in units[1]["trays"]] == [(1, 5, "B2")]
        assert [(int(t["slot"]), t["tray_id"], t["name"]) for t in units[129]["trays"]] == [(0, 129, "HT-B")]

    def test_the_load_command_finds_an_ht_tray_by_its_own_id(self):
        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "255"}}
        assert adapter._ams_tray(129)["tray_type"] == "ABS"
        assert adapter._ams_tray(5)["tray_type"] == "PETG"
        assert adapter._ams_tray(128) is None  # no HT-A on this machine
        assert adapter._ams_tray(4) is None  # unit B's slot 1 is empty

    def test_a_load_on_an_ht_is_sent_with_its_unit_id_as_the_target(self):
        from kiln.printers.base import FilamentOpPlan

        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "129"},
                                "nozzle_target_temper": 0}
        sent: list[dict[str, Any]] = []
        adapter._publish_command = lambda payload: sent.append(payload)
        adapter._park_for_firmware_routine = lambda plan: {"status": "in_place", "reason": "test"}
        plan = FilamentOpPlan(action="load", temperature=230.0, temperature_source="test", slot=129, material="ABS",
                              options={"wait_seconds": 1})
        result = adapter._load_filament_impl(plan)
        loads = [p["print"] for p in sent if p.get("print", {}).get("command") == "ams_change_filament"]
        assert len(loads) == 1 and loads[0]["target"] == 129
        assert result.success and "tray 129 (slot HT-B) is feeding the nozzle" in result.message

    def test_a_load_that_names_a_missing_tray_says_which_slot_that_would_be(self):
        from kiln.printers.base import FilamentOpPlan, PrinterError

        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "255"}}
        plan = FilamentOpPlan(action="load", temperature=230.0, temperature_source="test", slot=4)
        with pytest.raises(PrinterError, match=r"tray 4 \(slot B1\) is not present"):
            adapter._load_filament_impl(plan)

    def test_the_sentinels_are_the_external_spool_and_a_refusal_never_a_missing_tray(self):
        from kiln.printers.base import FilamentOpPlan, PrinterError

        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "254"},
                                "nozzle_target_temper": 0}
        sent: list[dict[str, Any]] = []
        adapter._publish_command = lambda payload: sent.append(payload)
        adapter._park_for_firmware_routine = lambda plan: {"status": "in_place", "reason": "test"}
        plan = FilamentOpPlan(action="load", temperature=210.0, temperature_source="test", slot=254,
                              options={"wait_seconds": 1})
        result = adapter._load_filament_impl(plan)
        loads = [p["print"] for p in sent if p.get("print", {}).get("command") == "ams_change_filament"]
        assert loads[0]["target"] == 254
        assert result.success and "the external spool is feeding the nozzle" in result.message
        for bad in (255, 300, 64):
            plan = FilamentOpPlan(action="load", temperature=210.0, temperature_source="test", slot=bad)
            with pytest.raises(PrinterError, match="not a tray this printer can load"):
                adapter._load_filament_impl(plan)

    def test_the_colour_check_keys_an_ht_and_a_second_unit_by_the_printers_ids(self, tmp_path):
        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("Metadata/plate_1.json", json.dumps({"filament_colors": ["#FFFFFF", "#00FF00"]}))
        adapter = _adapter()
        adapter.get_ams_status = mock.MagicMock(return_value=_ams("255", _A, _B, _HT_B))
        warnings = adapter._check_ams_color_mismatch(str(threemf), 1, [129, 5])
        assert len(warnings) == 2
        assert "tray 129 (slot HT-B) has #111111" in warnings[0]
        assert "tray 5 (slot B2) has #0000FF" in warnings[1]
        # A2 is white, so only the second filament is off.
        assert adapter._check_ams_color_mismatch(str(threemf), 1, [1, 5]) == [warnings[1]]

    def test_single_spool_auto_routing_sends_the_printers_id_of_a_second_units_tray(self):
        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": [_RAW_AMS[1]], "tray_now": "255"}}
        loaded = adapter._peek_loaded_ams_trays()
        assert loaded == [{"slot": 1, "unit": 1, "tray_id": 5, "name": "B2", "tray_type": "PETG", "tray_color": "0000FFFF"}]
        adapter._last_status["ams"]["ams"] = [_RAW_AMS[2]]
        assert adapter._peek_loaded_ams_trays()[0]["tray_id"] == 129

    def test_the_active_colour_is_read_off_a_second_unit_and_an_ht(self):
        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "5"}}
        assert adapter.active_filament_color() == "#0000FF"
        adapter._last_status["ams"]["tray_now"] = "129"
        assert adapter.active_filament_color() == "#111111"
        adapter._last_status["ams"]["tray_now"] = "254"
        assert adapter.active_filament_color() is None


# ---------------------------------------------------------------------------
# The composite doors that pick a tray themselves
# ---------------------------------------------------------------------------


def _square_stl(path: str, size: float = 10.0) -> str:
    """A flat square, enough for the plate composers to place."""
    import struct

    h = size / 2
    tris = [
        ((-h, -h, 0), (h, -h, 0), (h, h, 0)), ((-h, -h, 0), (h, h, 0), (-h, h, 0)),
        ((-h, -h, 1), (h, h, 1), (h, -h, 1)), ((-h, -h, 1), (-h, h, 1), (h, h, 1)),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for v in (a, b, c):
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
    return path


def _ams_status_reply(*units: dict[str, Any]) -> dict[str, Any]:
    """What the ``ams_status`` tool returns: the adapter reading plus ``success``."""
    return {"success": True, **_ams("255", *units)}


class TestTheCompositeDoors:
    """smart_reprint, multi_material_print and multi_color_copies scan the
    AMS report themselves; each used to keep the unit's slot (0-3) and send
    THAT as the mapping, which on a second unit or an AMS HT named the wrong
    tray."""

    @pytest.fixture(autouse=True)
    def _gates(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)

    def test_smart_reprint_routes_to_the_printers_id_of_a_second_units_spool(self, tmp_path):
        import kiln.server as srv

        model = _square_stl(str(tmp_path / "part.stl"))
        calls: list[dict[str, Any]] = []
        with mock.patch.object(srv, "ams_status", return_value=_ams_status_reply(_A, _B)), \
                mock.patch.object(srv, "run_reslice_and_print",
                                  side_effect=lambda **kw: calls.append(kw) or {"success": True}):
            result = srv.smart_reprint(file_name=model, material_id="petg", auto_ams=True)
        assert calls and calls[0]["ams_mapping"] == "[5]"
        step = next(s for s in result["smart_reprint_steps"] if s["step"] == "ams_detection")
        assert (step["slot"], step["name"]) == (5, "B2")
        assert (result["ams_slot_selected"]["slot"], result["ams_slot_selected"]["name"]) == (5, "B2")

    def test_multi_material_print_maps_each_object_to_the_printers_id(self, tmp_path):
        import kiln.server as srv

        paths = [_square_stl(str(tmp_path / f"p{i}.stl")) for i in range(2)]
        ht_petg = _unit(129, (0, "PETG", "111111FF"))  # PLA + PETG share a nozzle window; PLA + ABS do not
        calls: list[dict[str, Any]] = []
        with mock.patch.object(srv, "ams_status", return_value=_ams_status_reply(_A, ht_petg)), \
                mock.patch.object(srv, "run_reslice_and_print",
                                  side_effect=lambda **kw: calls.append(kw) or {"success": True}), \
                mock.patch.object(srv, "_refuse_undriven_multi_material", return_value=None):
            result = srv.multi_material_print(
                objects_json=json.dumps([
                    {"file_path": paths[0], "material_id": "pla", "color": "#FF0000"},
                    {"file_path": paths[1], "material_id": "petg", "color": "#111111"},
                ]),
                auto_ams=True,
            )
        assert calls and calls[0]["ams_mapping"] == "[0, 129]"
        assert [(m["slot"], m["name"]) for m in result["ams_mapping"]] == [(0, "A1"), (129, "HT-B")]

    def test_multi_color_copies_auto_mode_uses_the_printers_ids(self, tmp_path):
        import kiln.server as srv

        model = _square_stl(str(tmp_path / "part.stl"))
        two_pla = _unit(1, (1, "PLA", "0000FFFF"))
        calls: list[dict[str, Any]] = []
        with mock.patch.object(srv, "ams_status", return_value=_ams_status_reply(_A, two_pla)), \
                mock.patch.object(srv, "run_reslice_and_print",
                                  side_effect=lambda **kw: calls.append(kw) or {"success": True}):
            result = srv.multi_color_copies(model_path=model, spacing_mm=10.0)
        assert calls and calls[0]["ams_mapping"] == "[0, 1, 5]"
        assert [(m["slot"], m["name"]) for m in result["ams_mapping"]] == [(0, "A1"), (1, "A2"), (5, "B2")]

    def test_multi_color_copies_manual_mode_checks_the_printers_ids(self, tmp_path):
        import kiln.server as srv

        model = _square_stl(str(tmp_path / "part.stl"))
        with mock.patch.object(srv, "ams_status", return_value=_ams_status_reply(_A, _B)), \
                mock.patch.object(srv, "run_reslice_and_print", return_value={"success": True}):
            ok = srv.multi_color_copies(model_path=model, ams_slots=[0, 5])
            refused = srv.multi_color_copies(model_path=model, ams_slots=[0, 4])
        assert ok.get("multi_color_copies") is True
        assert refused["success"] is False
        assert "tray 4 (slot B1)" in refused["error"]["message"]
        assert "A1, A2, B2" in refused["error"]["message"]


class TestTheDesignContextDoor:
    def test_the_generation_context_reads_the_feeding_spool_on_a_second_unit(self):
        from types import SimpleNamespace

        from kiln.generation_feedback import resolve_printer_generation_context

        adapter = mock.MagicMock()
        adapter.get_printer_info.return_value = SimpleNamespace(build_volume=None, nozzle_diameter=None, model=None)
        adapter.get_ams_status.return_value = _ams("5", _A, _B)
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            ctx = resolve_printer_generation_context()
        assert (ctx.material, ctx.material_source) == ("petg", "ams")
        adapter.get_ams_status.return_value = _ams("129", _A, _HT_B)
        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            assert resolve_printer_generation_context().material == "abs"


class TestTheCommandLine:
    def _live(self, ams: dict[str, Any], *args: str) -> tuple[int, str]:
        from click.testing import CliRunner

        from kiln.cli.main import cli

        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = ams
        tracker = mock.MagicMock()
        tracker.get_all_materials.return_value = []
        with mock.patch("kiln.materials.MaterialTracker", return_value=tracker), \
                mock.patch("kiln.persistence.get_db"), \
                mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = CliRunner().invoke(cli, list(args))
        return result.exit_code, result.output

    def test_material_show_live_marks_the_tray_that_is_feeding_by_the_printers_id(self):
        code, out = self._live(_ams("5", _A, _B), "material", "show", "--live", "--json")
        assert code == 0, out
        rows = json.loads(out)["data"]
        assert [(r["slot"], r["tray_id"], r["active"]) for r in rows] == [("A1", 0, False), ("A2", 1, False), ("B2", 5, True)]
        code, out = self._live(_ams("5", _A, _B), "material", "show", "--live")
        assert "Slot B2: PETG (#0000FF) — 42% left ◀ active" in out
        assert "Slot A1: PLA (#FF0000) — 42% left\n" in out

    def test_kiln_ams_names_units_and_the_feeding_tray(self):
        code, out = self._live(_ams("129", _A, _HT_B), "ams")
        assert code == 0, out
        assert "AMS A:" in out and "AMS HT-B:" in out
        assert "Slot HT-B (tray 129): ABS" in out
        assert "Active: tray 129 (slot HT-B)" in out


class TestTheSameWordsEverywhereElse:
    def test_a_fault_on_a_second_unit_names_the_slot_the_way_the_load_does(self):
        from kiln.printers.bambu import describe_bambu_filament_fault_public

        # HMS 0701_7100: unit B (0701), slot 2 (7100) — the wiki's one row
        # covers all sixteen, and the reading says which one.
        reading, _ = describe_bambu_filament_fault_public("0701_7100_0002_0001")
        assert "(slot B2)" in reading
        reading, _ = describe_bambu_filament_fault_public("0700_7000_0002_0001")
        assert "(slot A1)" in reading

    def test_the_capacity_check_names_the_trays_it_counted(self, tmp_path):
        threemf = tmp_path / "model.3mf"
        with zipfile.ZipFile(threemf, "w") as zf:
            zf.writestr("Metadata/plate_1.json", json.dumps({"filament_ids": [0, 1, 2, 3]}))
        adapter = _adapter()
        adapter.get_ams_status = mock.MagicMock(return_value=_ams("255", _A, _HT_B))
        issues = adapter._validate_3mf_filament_ids(str(threemf), 1)
        assert len(issues) == 1 and "3 tray(s) (A1, A2, HT-B)" in issues[0]

    def test_the_record_kind_follows_the_units_own_module_name(self):
        from kiln.multi_material import from_bambu_ams

        lite = {**_A, "module_name": "ams_f1/0"}
        boxed = {**_A, "module_name": "ams/0"}
        assert from_bambu_ams(_ams("255", lite), printer_model="bambu_a1").kind == "ams_lite"
        # An A1 on its AMS Hub carries chained units; the model alone would call them Lite.
        assert from_bambu_ams(_ams("255", boxed), printer_model="bambu_a1").kind == "ams"
        assert from_bambu_ams(_ams("255", {**_HT_B, "module_name": "n3s/129"}), printer_model="bambu_a1").kind == "ams"
        # No module names (an older reading): the model decides, as before.
        assert from_bambu_ams(_ams("255", _A), printer_model="bambu_a1").kind == "ams_lite"
        assert from_bambu_ams(_ams("255", _A), printer_model="bambu_x1c").kind == "ams"


# ---------------------------------------------------------------------------
# New-protocol firmware: the extruder block names the feeding tray
# ---------------------------------------------------------------------------


#: Shaped from a community capture of an H2D (firmware 01.01.02.07): the
#: legacy ``ams.tray_now`` reads the unit's LOCAL slot ("0") while
#: ``device.extruder.info[0].snow`` = 32768 = unit 128, slot 0 — HT-A is
#: feeding.  The left nozzle's 65279 (unit 254, slot 255) is "nothing".
_H2D_STATUS = {
    "gcode_state": "IDLE",
    "ams": {
        "ams": [
            {"id": "0", "info": "1101", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"},
                {"id": "3", "tray_type": "PETG", "tray_color": "0000FFFF"},
            ]},
            {"id": "128", "info": "2004", "tray": [{"id": "0", "tray_type": "PA-GF", "tray_color": "111111FF"}]},
        ],
        "ams_exist_bits": "11", "tray_exist_bits": "1000f",
        "tray_now": "0", "tray_tar": "0", "tray_pre": "0",
    },
    "device": {"extruder": {"state": 2, "info": [
        {"id": 0, "snow": 32768, "spre": 32768, "star": 32768},
        {"id": 1, "snow": 65279, "spre": 65279, "star": 65279},
    ]}},
    "vir_slot": [{"id": "254"}, {"id": "255"}],
}


class TestTheExtruderBlockWins:
    def test_the_packed_slot_reads_into_the_same_vocabulary(self):
        from kiln.bambu_trays import read_extruder_slot

        assert (read_extruder_slot(32768).unit, read_extruder_slot(32768).slot, read_extruder_slot(32768).tray_id) == (128, 0, 128)
        assert (read_extruder_slot(258).unit, read_extruder_slot(258).slot, read_extruder_slot(258).tray_id) == (1, 2, 6)
        assert read_extruder_slot(0).tray_id == 0
        assert read_extruder_slot(65535).none          # 0xFFFF: nothing (the X1C capture, idle)
        assert read_extruder_slot(255).none            # 0x00FF: slot 255 is nothing
        assert read_extruder_slot(65279).none          # 0xFEFF: nothing on the left nozzle (the H2D capture)
        assert read_extruder_slot(65280).external      # 0xFF00: the right external spool (an X2D capture)
        assert read_extruder_slot(65024).external      # 0xFE00: the left external spool
        for junk in (None, "", "x", -1, 70000, 16 << 8):
            assert read_extruder_slot(junk) is None, junk

    def test_the_ams_report_names_the_feeding_tray_from_the_extruder_block(self):
        adapter = _adapter()
        adapter._last_status = dict(_H2D_STATUS)
        status = adapter.get_ams_status()
        assert status["tray_now"] == "0"  # the wire value, untouched
        assert status["feeding"] == {"tray_id": 128, "unit": 128, "slot": 0, "name": "HT-A", "source": "extruder"}

    def test_a_legacy_report_reads_tray_now_by_the_rule(self):
        adapter = _adapter()
        adapter._last_status = {"gcode_state": "IDLE", "ams": {"ams": _RAW_AMS, "tray_now": "5"}}
        status = adapter.get_ams_status()
        assert status["feeding"] == {"tray_id": 5, "unit": 1, "slot": 1, "name": "B2", "source": "tray_now"}
        adapter._last_status["ams"]["tray_now"] = "254"
        assert adapter.get_ams_status()["feeding"] == {"tray_id": 254, "unit": None, "slot": None, "name": "Ext", "source": "tray_now"}
        adapter._last_status["ams"]["tray_now"] = "255"
        assert adapter.get_ams_status()["feeding"] is None

    def test_every_reader_follows_the_report(self):
        from kiln.multi_material import from_bambu_ams

        adapter = _adapter()
        adapter._last_status = dict(_H2D_STATUS)
        status = adapter.get_ams_status()
        assert from_bambu_ams(status, printer_model="bambu_h2d").feeding == (128, 0)
        assert adapter.active_filament_color() == "#111111"

    def test_the_reporting_door_reads_the_feeding_block(self, get_active_material):
        adapter = _adapter()
        adapter._last_status = dict(_H2D_STATUS)
        r = _report(get_active_material, adapter.get_ams_status())
        assert (r["material"], r["active_slot"], r["active_slot_name"], r["active_slot_source"]) == ("PA-GF", 128, "HT-A", "extruder")

    def test_the_load_watch_confirms_on_the_extruder_block(self):
        from kiln.printers.base import FilamentOpPlan

        adapter = _adapter()
        adapter._last_status = {**_H2D_STATUS, "nozzle_target_temper": 0}
        sent: list[dict[str, Any]] = []
        adapter._publish_command = lambda payload: sent.append(payload)
        adapter._park_for_firmware_routine = lambda plan: {"status": "in_place", "reason": "test"}
        plan = FilamentOpPlan(action="load", temperature=260.0, temperature_source="test", slot=128,
                              options={"wait_seconds": 1})
        result = adapter._load_filament_impl(plan)
        assert result.success and "tray 128 (slot HT-A) is feeding the nozzle" in result.message

    def test_an_idle_new_firmware_report_is_not_read_as_unit_a(self, get_active_material):
        # The block says nothing feeds (65535) while the legacy fields still
        # read "0" (a local slot): the door must not answer "PLA in A1", nor
        # fall back to tray_pre / tray_tar, which are local slots here too.
        idle = {**_H2D_STATUS, "ams": {**_H2D_STATUS["ams"], "tray_now": "0", "tray_pre": "0", "tray_tar": "0"},
                "device": {"extruder": {"state": 2, "info": [{"id": 0, "snow": 65535, "spre": 65535, "star": 65535}]}}}
        adapter = _adapter()
        adapter._last_status = idle
        status = adapter.get_ams_status()
        assert status["feeding"] is None and status["feeding_source"] == "extruder"
        r = _report(get_active_material, status)
        assert r["source"] == "ams_loaded_unknown_slot"
        assert sorted(r["loaded_slot_names"]) == ["A1", "A4", "HT-A"]
