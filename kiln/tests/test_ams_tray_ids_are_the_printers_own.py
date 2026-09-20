"""A Bambu names a tray by its GLOBAL id, and every reader resolves it that way.

``tray_now`` (and ``tray_pre`` / ``tray_tar`` / ``active_tray``) carry
``unit * 4 + slot``: unit 1's first tray is 4, not 0; 254 is the external
spool holder, 255 is "no tray feeding".  ``get_ams_status`` hands each tray
its unit's OWN slot id (0-3).  Three readers compared the two directly, so on
a second AMS unit the active tray never matched and unit 0's tray answered,
and the external spool was reported as "slot 254, tray data unavailable":

* ``get_active_material`` (the reporting door),
* the learning door's live-material read (what ran the print), and
* the slice-time reader (what the print is weighed as), fixed earlier.

The owner's single-unit A1 cannot reach the two-unit cases; they are pinned
from the one rule in :mod:`kiln.bambu_trays` (Bambu Studio's own).
"""

from __future__ import annotations

from unittest import mock

import pytest


def _ams(tray_now: str, units: dict[int, list[tuple[int, str]]], **fields):
    return {
        "tray_now": tray_now,
        "units": [
            {"unit_id": unit_id, "trays": [
                {"slot": slot, "tray_type": material, "tray_color": "FF0000FF", "remain": 42, "remaining_known": True}
                for slot, material in trays
            ]}
            for unit_id, trays in units.items()
        ],
        **fields,
    }


_TWO_UNITS = {0: [(0, "PLA"), (1, "PETG")], 1: [(0, "ABS"), (1, "TPU")]}


class _MockMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs):
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


def _run(fn, ams):
    adapter = mock.MagicMock()
    adapter.get_ams_status.return_value = ams
    with mock.patch("kiln.server._get_adapter", return_value=adapter):
        return fn()


class TestTheReportingDoor:
    def test_a_second_units_tray_is_found_by_its_global_id(self, get_active_material):
        r = _run(get_active_material, _ams("5", _TWO_UNITS))
        assert r["material"] == "TPU"
        assert (r["active_slot"], r["active_unit"], r["active_tray"]) == (5, 1, 1)
        assert r["source"] == "ams_slot_5"
        r = _run(get_active_material, _ams("4", _TWO_UNITS))
        assert r["material"] == "ABS"

    def test_unit_zero_reads_exactly_as_before(self, get_active_material):
        r = _run(get_active_material, _ams("1", _TWO_UNITS))
        assert (r["material"], r["active_slot"], r["active_unit"], r["active_tray"]) == ("PETG", 1, 0, 1)
        assert r["color"] == "FF0000FF" and r["remaining_percent"] == 42

    def test_the_external_spool_is_named_as_such(self, get_active_material):
        r = _run(get_active_material, _ams("254", {0: [(0, "PETG")]}))
        assert (r["material"], r["source"]) == ("unknown", "external_spool")

    def test_the_previously_fed_tray_is_a_global_id_too(self, get_active_material):
        r = _run(get_active_material, _ams("255", _TWO_UNITS, tray_pre="4"))
        assert (r["material"], r["active_slot_source"], r["active_unit"]) == ("ABS", "tray_pre", 1)

    def test_loaded_slots_are_the_printers_ids(self, get_active_material):
        r = _run(get_active_material, _ams("255", _TWO_UNITS))
        assert r["source"] == "ams_loaded_unknown_slot"
        assert r["loaded_slots"] == [0, 1, 4, 5]
        assert r["candidate_materials"] == ["ABS", "PETG", "PLA", "TPU"]


class TestTheLearningDoor:
    def _material(self, ams):
        from kiln.plugins.learning_tools import _material_from_printer

        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = ams
        with mock.patch("kiln.server._resolve_adapter", return_value=adapter):
            return _material_from_printer("default")

    def test_the_tray_that_ran_the_print_on_a_second_unit(self):
        assert self._material(_ams("5", _TWO_UNITS)) == "TPU"
        assert self._material(_ams("255", _TWO_UNITS, tray_pre="4")) == "ABS"

    def test_the_external_spool_is_honestly_nothing(self):
        assert self._material(_ams("254", {0: [(0, "PETG")]})) is None

    def test_agreement_across_units_is_the_one_allowed_inference(self):
        assert self._material(_ams("255", {0: [(0, "PLA")], 1: [(2, "PLA")]})) == "PLA"
        assert self._material(_ams("255", _TWO_UNITS)) is None


class TestTheRoutingPath:
    """The plan hands ``start_print`` the printer's tray ids, and every
    reader beside it keys by the same id."""

    def _trays(self):
        from kiln.ams_routing import loaded_trays

        return loaded_trays({"tray_now": "255", "units": [
            {"unit_id": 0, "trays": [{"slot": 0, "tray_type": "PLA", "tray_color": "FF0000FF"}]},
            {"unit_id": 1, "trays": [{"slot": 1, "tray_type": "PLA", "tray_color": "0000FFFF"}]},
        ]})

    def test_a_colour_on_the_second_unit_maps_to_its_printer_id(self):
        from kiln.ams_routing import Filament, plan_ams_mapping

        trays = self._trays()
        assert [t.tray_id for t in trays] == [0, 5]
        plan = plan_ams_mapping([Filament(hex6="0000FF")], trays)
        assert plan.ok and plan.mapping == [5]
        assert plan.matches[0]["slot"] == 5
        assert plan.matches[0]["tray"] == "blue PLA in slot B2" and plan.matches[0]["name"] == "B2"

    def test_the_selection_record_finds_the_second_units_colour(self):
        from kiln.server import _ams_selection_record

        info = {"units": [
            {"unit_id": 0, "trays": [{"slot": 1, "tray_type": "PLA", "tray_color": "FF0000FF"}]},
            {"unit_id": 1, "trays": [{"slot": 1, "tray_type": "PETG", "tray_color": "0000FFFF"}]},
        ]}
        assert _ams_selection_record(5, "PETG", info)["color"] == "0000FFFF"
        assert _ams_selection_record(1, "PLA", info)["color"] == "FF0000FF"

    def test_the_colour_advisor_keys_by_the_same_id(self):
        from kiln.ams_routing import advise_colours

        out = advise_colours(["0000FF"], self._trays(), printer="x1")
        assert out is not None and out.matched[0]["slot"] == 5
