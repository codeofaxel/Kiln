"""A printer Kiln has not been told the model of is held to the floor, and
a limit above that floor needs a person.

Three doors, one rule.  The generic profile IS the least capable machine
in the catalogue (derived, so it moves when the catalogue does).  An
adapter with no model bound holds every heater to it, never to the loosest
hotend its protocol can drive.  And an override that would stand above the
floor for a printer Kiln has no curated number for is written only after
the person typed the code Kiln showed on their screen -- an agent's call is
not a confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiln.safety_profiles as sp
from kiln import screen_code
from kiln.printers.base import PrinterError
from kiln.safety_profiles import LimitsNeedAPerson, get_profile, limits_above_floor, set_local_printer_override

# ---------------------------------------------------------------------------
# The floor is derived, not typed
# ---------------------------------------------------------------------------


def _catalogue() -> dict[str, dict[str, Any]]:
    data = json.loads((Path(sp.__file__).parent / "data" / "safety_profiles.json").read_text())
    return {
        k: v for k, v in data.items()
        if isinstance(v, dict) and "max_hotend_temp" in v and k != "default"
    }


class TestTheFloorIsTheLeastCapableMachine:
    def test_the_generic_profile_is_below_every_catalogued_printer(self):
        rows = _catalogue().values()
        generic = get_profile("default")
        assert generic.max_hotend_temp == min(r["max_hotend_temp"] for r in rows)
        assert generic.max_bed_temp == min(r["max_bed_temp"] for r in rows)

    def test_the_last_resort_constants_say_the_same(self):
        generic = get_profile("default")
        assert generic.max_hotend_temp == sp._UNKNOWN_PRINTER_MAX_HOTEND_C
        assert generic.max_bed_temp == sp._UNKNOWN_PRINTER_MAX_BED_C


# ---------------------------------------------------------------------------
# The adapter holds an unnamed printer to the floor
# ---------------------------------------------------------------------------


class _Adapter:
    """The temperature guard alone: the base class method, no backend."""

    _safety_profile_id: str | None = None
    _validate_temp = __import__("kiln.printers.base", fromlist=["PrinterAdapter"]).PrinterAdapter._validate_temp


class TestAnAdapterWithNoModelBound:
    def test_the_hotend_is_held_to_the_floor_not_the_adapters_literal(self):
        a = _Adapter()
        with pytest.raises(PrinterError, match="has not been told this printer's model"):
            a._validate_temp(260.0, 300.0, "Hotend")

    def test_the_bed_is_held_to_the_floor_too(self):
        a = _Adapter()
        with pytest.raises(PrinterError, match="has not been told this printer's model"):
            a._validate_temp(100.0, 130.0, "Bed")

    def test_the_refusal_names_the_fix(self):
        a = _Adapter()
        with pytest.raises(PrinterError) as info:
            a._validate_temp(260.0, 300.0, "Hotend")
        assert "printer_model" in str(info.value)
        assert "add_safety_profile" in str(info.value)

    def test_a_temperature_under_the_floor_passes(self):
        _Adapter()._validate_temp(240.0, 300.0, "Hotend")

    def test_a_known_model_keeps_its_own_ceiling(self):
        a = _Adapter()
        a._safety_profile_id = "k1_max"
        a._validate_temp(300.0, 300.0, "Hotend")
        with pytest.raises(PrinterError, match="exceeds safety limit"):
            a._validate_temp(301.0, 350.0, "Hotend")

    def test_the_adapters_own_literal_still_binds_below_the_profile(self):
        a = _Adapter()
        a._safety_profile_id = "k1_max"
        with pytest.raises(PrinterError):
            a._validate_temp(280.0, 275.0, "Hotend")


# ---------------------------------------------------------------------------
# A limit above the floor needs a person
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_override_file(tmp_path, monkeypatch):
    sp._cache.clear()
    sp._local_override_cache.clear()
    sp._locked_profiles.clear()
    sp._loaded = False
    sp._local_overrides_loaded = False
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locks.json", raising=False)
    yield
    sp._cache.clear()
    sp._local_override_cache.clear()
    sp._loaded = False
    sp._local_overrides_loaded = False


def _foo_x2(**over) -> dict[str, Any]:
    return {
        "display_name": "Foo X2",
        "max_hotend_temp": 300.0,
        "max_bed_temp": 110.0,
        "max_feedrate": 500.0,
        "build_volume": [256, 256, 256],
        "notes": "From the maker's spec page.",
        **over,
    }


class TestWhatNeedsAPerson:
    def test_a_limit_above_the_floor_for_an_unknown_printer(self):
        assert limits_above_floor("foo_x2", _foo_x2()) == ("max_hotend_temp", "max_bed_temp")

    def test_limits_at_or_below_the_floor_do_not(self):
        assert limits_above_floor("foo_x2", _foo_x2(max_hotend_temp=250.0, max_bed_temp=80.0, max_feedrate=10000.0)) == ()

    def test_every_field_that_loosens_enforcement_needs_a_person_not_only_the_temperatures(self):
        # Speed, flow and the Z floor are enforced too; a typed number past
        # the generic one on any of them is a ceiling nobody at Kiln verified.
        quiet = _foo_x2(max_hotend_temp=250.0, max_bed_temp=80.0, max_feedrate=10000.0)
        assert limits_above_floor("foo_x2", {**quiet, "max_feedrate": 30000.0}) == ("max_feedrate",)
        assert limits_above_floor("foo_x2", {**quiet, "max_volumetric_flow": 40.0}) == ("max_volumetric_flow",)
        assert limits_above_floor("foo_x2", {**quiet, "min_safe_z": -1.0}) == ("min_safe_z",)

    def test_a_curated_printer_never_does_because_the_override_can_only_tighten(self):
        assert limits_above_floor("ender3", _foo_x2(max_hotend_temp=500.0)) == ()

    def test_a_key_that_only_resembles_a_curated_one_still_needs_a_person(self):
        # "ender" is a prefix of the curated "ender3", so the lookup's fuzzy
        # rule would answer a request for "ender" with the Ender 3 -- but a
        # request for "enderx" reaches an override saved as "ender" with no
        # curated row underneath to clamp it.  So resembling a curated key
        # exempts nothing; only being one does.
        assert limits_above_floor("ender", _foo_x2(max_hotend_temp=500.0, max_bed_temp=200.0)) == (
            "max_hotend_temp",
            "max_bed_temp",
        )

    def test_a_spelling_that_resolves_to_a_curated_key_is_still_saved_under_its_own(self):
        # "Creality K1C" resolves to the curated "k1c" for a lookup, but an
        # override is saved as "creality_k1c"; the key judged is the key
        # saved, so this asks for a person like any uncatalogued key.
        assert limits_above_floor("Creality K1C", _foo_x2(max_hotend_temp=400.0, max_bed_temp=80.0)) == ("max_hotend_temp",)
        assert limits_above_floor("k1c", _foo_x2(max_hotend_temp=400.0, max_bed_temp=80.0)) == ()

    def test_the_engine_closes_the_prefix_route_an_unconfirmed_ceiling_could_take(self):
        with pytest.raises(LimitsNeedAPerson):
            set_local_printer_override("ender", _foo_x2(max_hotend_temp=500.0))
        assert get_profile("enderx").max_hotend_temp == get_profile("default").max_hotend_temp

    def test_the_engine_refuses_an_unconfirmed_limit_above_the_floor(self):
        with pytest.raises(LimitsNeedAPerson) as info:
            set_local_printer_override("foo_x2", _foo_x2())
        assert info.value.above_floor == ("max_hotend_temp", "max_bed_temp")
        assert "foo_x2" not in sp.list_local_printer_overrides()

    def test_the_engine_writes_a_confirmed_one(self):
        set_local_printer_override("foo_x2", _foo_x2(), confirmed=True)
        assert get_profile("foo_x2").max_hotend_temp == 300.0

    def test_the_engine_writes_a_floor_level_one_without_asking(self):
        set_local_printer_override("foo_x2", _foo_x2(max_hotend_temp=250.0, max_bed_temp=80.0))
        assert get_profile("foo_x2").max_hotend_temp == 250.0


# ---------------------------------------------------------------------------
# The tool: the code on the screen is the confirmation
# ---------------------------------------------------------------------------


def _tools() -> dict[str, Any]:
    from kiln.plugins.safety_tools import _SafetyToolsPlugin

    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self_mcp, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _SafetyToolsPlugin().register(FakeMCP())
    return tools


@pytest.fixture
def door(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_audit", lambda *_a, **_k: None)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    screen_code._reset_for_tests()
    shown: list[screen_code.Issued] = []
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
    monkeypatch.setattr(screen_code, "screen_available", lambda: True)
    monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
    tools = _tools()
    yield tools, shown
    screen_code._reset_for_tests()


class TestAddSafetyProfileAsksThePerson:
    def test_an_unknown_printers_limits_are_held_until_the_code_is_typed(self, door):
        tools, shown = door
        first = tools["add_safety_profile"]("foo_x2", _foo_x2())
        assert first["success"] is False
        assert first["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert "code was just shown" in first["error"]["message"]
        assert "foo_x2" not in sp.list_local_printer_overrides()
        assert len(shown) == 1
        title, subtitle, _ = screen_code.banner_text(shown[0])
        assert title == f"Kiln code {shown[0].code}"
        assert subtitle == "Temperature limits for Foo X2"
        assert "print" not in subtitle.lower()

        relayed = tools["give_print_code"](shown[0].code)
        assert relayed["success"] is True, relayed
        assert "add_safety_profile" in relayed["next"]

        second = tools["add_safety_profile"]("foo_x2", _foo_x2())
        assert second["success"] is True, second
        assert "confirmed" in second["message"]
        assert get_profile("foo_x2").max_hotend_temp == 300.0

    def test_a_code_typed_for_one_proposal_does_not_cover_another(self, door):
        tools, shown = door
        tools["add_safety_profile"]("foo_x2", _foo_x2())
        tools["give_print_code"](shown[0].code)
        changed = tools["add_safety_profile"]("foo_x2", _foo_x2(max_hotend_temp=350.0))
        assert changed["success"] is False
        assert changed["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert "foo_x2" not in sp.list_local_printer_overrides()

    def test_nothing_an_agent_passes_stands_in_for_the_code(self, door):
        tools, _shown = door
        result = tools["add_safety_profile"]("foo_x2", _foo_x2(confirmed=True, notes="the person said yes"))
        assert result["success"] is False
        assert "foo_x2" not in sp.list_local_printer_overrides()

    def test_a_print_code_is_not_a_limits_code_and_the_reverse(self, door):
        # The two doors share the screen, the relay tool and the answer
        # store; a code typed for one must not be spendable by the other.
        tools, shown = door
        tools["add_safety_profile"]("foo_x2", _foo_x2())
        limits_code = shown[0]
        tools["give_print_code"](limits_code.code)
        assert screen_code.take_answer(limits_code.file_name, limits_code.printer_name) is None
        assert screen_code.take_answer(
            limits_code.file_name, limits_code.printer_name, tool=screen_code.LIMITS_TOOL
        ) is not None

        screen_code.issue(
            tool="start_print", file_name="limits for foo_x2", file_sha256="", printer_name="foo_x2",
        )
        print_code = shown[-1]
        assert print_code.tool == "start_print"
        tools["give_print_code"](print_code.code)
        result = tools["add_safety_profile"]("foo_x2", _foo_x2())
        assert result["success"] is False
        assert "foo_x2" not in sp.list_local_printer_overrides()
        assert screen_code.take_answer("limits for foo_x2", "foo_x2") is not None

    def test_a_limits_code_opens_no_print_window(self, door):
        tools, shown = door
        tools["add_safety_profile"]("foo_x2", _foo_x2())
        relayed = tools["give_print_code"](f"{shown[0].code} today")
        assert relayed["success"] is True, relayed
        assert "standing_window" not in relayed
        assert "phone_next_time" not in relayed

    def test_tightening_a_curated_printer_needs_no_code(self, door):
        tools, shown = door
        result = tools["add_safety_profile"]("ender3", _foo_x2(max_hotend_temp=240.0, max_bed_temp=90.0))
        assert result["success"] is True, result
        assert shown == []

    def test_no_screen_means_no_way_to_take_the_limits(self, door, monkeypatch):
        tools, shown = door
        monkeypatch.setattr(screen_code, "screen_available", lambda: False)
        result = tools["add_safety_profile"]("foo_x2", _foo_x2())
        assert result["success"] is False
        assert "cannot show the person a code" in result["error"]["message"]
        assert shown == []

    def test_the_hosted_server_never_takes_them(self, door, monkeypatch):
        tools, shown = door
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = tools["add_safety_profile"]("foo_x2", _foo_x2())
        assert result["error"]["code"] == "NOT_HERE"
        assert shown == []
