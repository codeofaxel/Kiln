"""Brand filament profiles: the roster is public, the figures are served.

Since 2026-10-16 ``materials.json`` lists each family's brand profiles by
name only (``brand`` and ``product_name`` per profile id); a brand's figures
-- its nozzle and bed ranges and optimum, enclosure, hardened nozzle, AMS --
are the family's catalogue record in kiln-pro, served one material at a
time through ``kiln._pro_material_bridge`` and merged onto the roster row.
Public CI holds no real brand figure (kiln-pro's own tests pin those), so
every test here is a test of CODE against the synthetic record the session
serves for every roster material: a brand's range is its family's own print
range with the optimum at the midpoint, and the same for the bed.

Coverage areas:
- get_brand_filament_profile: the roster names, the served figures, the
  names-only answer when the figures could not be had (and why)
- list_brand_filament_profiles: a family list carries figures, the roster
  list is names only, filtering by brand / family / both
- _build_brand_profile: construction from a roster row with and without
  figures, the hardware flags
"""

from __future__ import annotations

import json

import pytest

from kiln import _pro_material_bridge as bridge
from kiln.design_intelligence import (
    _DATA_DIR,
    _build_brand_profile,
    _get_kb,
    _reset_knowledge_base,
    brand_figures_unanswered,
    get_brand_filament_profile,
    list_brand_filament_profiles,
)

_FIGURE_FIELDS = ("nozzle_temp_range_c", "nozzle_temp_optimal_c", "bed_temp_range_c", "bed_temp_optimal_c")


def _roster() -> dict[str, dict[str, dict]]:
    """family id -> profile id -> {brand, product_name}, as the file ships."""
    materials = json.loads(_DATA_DIR.joinpath("materials.json").read_text(encoding="utf-8"))
    return {
        mid: dict(row.get("brand_profiles") or {})
        for mid, row in materials.items()
        if not mid.startswith("_") and isinstance(row, dict) and row.get("brand_profiles")
    }


def _family_range(mid: str, key: str) -> list[int]:
    thermal = _get_kb().materials[mid].get("thermal") or {}
    low, high = thermal[key]
    return [int(low), int(high)]


def _first_profile(mid: str) -> str:
    return sorted(_roster()[mid])[0]


@pytest.fixture(autouse=True)
def _served_figures(served_material_records):
    """The synthetic record for every roster family, whatever is installed."""
    _reset_knowledge_base()
    yield
    _reset_knowledge_base()


@pytest.fixture
def asks(monkeypatch):
    """Counts which families the served door is asked for."""
    calls: list[str] = []
    answer = bridge._served

    def counting(key: str):
        calls.append(key)
        return answer(key)

    monkeypatch.setattr(bridge, "_served", counting)
    return calls


# ---------------------------------------------------------------------------
# get_brand_filament_profile
# ---------------------------------------------------------------------------


class TestGetBrandFilamentProfile:
    """get_brand_filament_profile() lookups."""

    def test_the_names_come_from_the_roster(self):
        pid = _first_profile("pla")
        p = get_brand_filament_profile(pid)
        assert p is not None
        assert p.profile_id == pid and p.parent_material == "pla"
        assert p.brand == _roster()["pla"][pid]["brand"]
        assert p.product_name == _roster()["pla"][pid]["product_name"]

    def test_the_figures_are_the_familys_served_record(self):
        """A/B of the merge: the synthetic record gives a brand its family's
        own range with the optimum at the midpoint, so a roster row that
        carries no figure of its own still answers with figures."""
        pid = _first_profile("pla")
        p = get_brand_filament_profile(pid)
        assert p is not None and p.has_figures()
        nozzle = _family_range("pla", "print_temp_range_c")
        bed = _family_range("pla", "bed_temp_range_c")
        assert p.nozzle_temp_range_c == nozzle
        assert p.nozzle_temp_optimal_c == (nozzle[0] + nozzle[1]) // 2
        assert p.bed_temp_range_c == bed
        assert p.bed_temp_optimal_c == (bed[0] + bed[1]) // 2
        assert nozzle[0] <= p.nozzle_temp_optimal_c <= nozzle[1]

    def test_a_profile_in_another_family_reads_that_familys_record(self):
        pid = _first_profile("petg")
        p = get_brand_filament_profile(pid)
        assert p is not None and p.parent_material == "petg" and p.has_figures()
        assert p.nozzle_temp_range_c == _family_range("petg", "print_temp_range_c")
        assert p.nozzle_temp_range_c != _family_range("pla", "print_temp_range_c")

    def test_unknown_profile_returns_none(self):
        assert get_brand_filament_profile("nonexistent_brand_xyz") is None

    def test_case_insensitive(self):
        pid = _first_profile("pla")
        p = get_brand_filament_profile(pid.upper())
        assert p is not None and p.profile_id == pid

    def test_has_required_fields(self):
        p = get_brand_filament_profile(_first_profile("pla"))
        assert p is not None
        assert isinstance(p.profile_id, str)
        assert isinstance(p.brand, str)
        assert isinstance(p.product_name, str)
        assert isinstance(p.parent_material, str)
        assert isinstance(p.nozzle_temp_range_c, list)
        assert len(p.nozzle_temp_range_c) == 2
        assert isinstance(p.nozzle_temp_optimal_c, int)
        assert isinstance(p.bed_temp_range_c, list)
        assert isinstance(p.enclosure_required, bool)
        assert isinstance(p.hardened_nozzle_required, bool)
        assert isinstance(p.source, str)

    def test_to_dict(self):
        pid = _first_profile("pla")
        p = get_brand_filament_profile(pid)
        assert p is not None
        d = p.to_dict()
        assert isinstance(d, dict)
        assert d["profile_id"] == pid
        assert d["brand"] == p.brand
        assert d["nozzle_temp_optimal_c"] == p.nozzle_temp_optimal_c
        assert d["ams_compatible"] is None  # the synthetic record states none

    def test_the_hardware_flags_come_from_the_record(self, monkeypatch):
        """Enclosure, hardened nozzle and AMS ride on the served record, not
        the roster: a record that states them reaches the profile."""
        pid = _first_profile("pla")
        base = bridge._served

        def flagged(key: str):
            record, miss = base(key)
            if record and key == "pla":
                record["brands"][pid].update(
                    {"enclosure_required": True, "hardened_nozzle_required": True, "ams_compatible": False}
                )
            return record, miss

        monkeypatch.setattr(bridge, "_served", flagged)
        p = get_brand_filament_profile(pid)
        assert p is not None
        assert p.enclosure_required is True
        assert p.hardened_nozzle_required is True
        assert p.ams_compatible is False

    def test_a_family_whose_figures_were_had_has_nothing_to_say(self):
        p = get_brand_filament_profile(_first_profile("pla"))
        assert p is not None and p.has_figures()
        assert brand_figures_unanswered("pla") is None

    def test_a_family_the_record_does_not_cover_is_names_only(self, monkeypatch):
        """A roster name the family's record says nothing about keeps its
        names and carries no figure -- never another brand's."""
        pid = _first_profile("pla")
        base = bridge._served

        def without(key: str):
            record, miss = base(key)
            if record and key == "pla":
                record["brands"].pop(pid, None)
            return record, miss

        monkeypatch.setattr(bridge, "_served", without)
        p = get_brand_filament_profile(pid)
        assert p is not None and not p.has_figures()
        assert p.brand == _roster()["pla"][pid]["brand"]
        assert p.nozzle_temp_optimal_c is None and p.nozzle_temp_range_c is None


class TestSignedOut:
    """The install that cannot get a record: names only, and it says why."""

    @pytest.fixture(autouse=True)
    def _signed_out(self, signed_out_material_install):
        _reset_knowledge_base()
        yield
        _reset_knowledge_base()

    def test_a_profile_is_names_only_and_says_why(self):
        pid = _first_profile("pla")
        p = get_brand_filament_profile(pid)
        assert p is not None
        assert p.brand == _roster()["pla"][pid]["brand"]
        assert p.product_name == _roster()["pla"][pid]["product_name"]
        assert not p.has_figures()
        assert all(getattr(p, field) is None for field in _FIGURE_FIELDS)
        why = brand_figures_unanswered("pla")
        assert why and "signed out" in why

    def test_a_family_list_is_names_only_too(self):
        profiles = list_brand_filament_profiles(parent_material="petg")
        assert profiles and all(not p.has_figures() for p in profiles)
        assert "signed out" in (brand_figures_unanswered("petg") or "")



# ---------------------------------------------------------------------------
# list_brand_filament_profiles
# ---------------------------------------------------------------------------


class TestListBrandFilamentProfiles:
    """list_brand_filament_profiles() filtering."""

    def test_list_all_is_the_whole_roster_names_only(self, asks):
        """An unfiltered list on an install without kiln-pro is the roster:
        every name, no figures, and the servers are never asked (filling
        it would ask for every family's record in one call)."""
        profiles = list_brand_filament_profiles()
        roster = _roster()
        assert {(p.parent_material, p.profile_id) for p in profiles} == {
            (mid, pid) for mid, rows in roster.items() for pid in rows
        }
        assert all(not p.has_figures() for p in profiles)
        assert asks == []

    def test_filter_by_parent_material_carries_the_familys_figures(self, asks):
        profiles = list_brand_filament_profiles(parent_material="pla")
        assert {p.profile_id for p in profiles} == set(_roster()["pla"])
        assert all(p.parent_material == "pla" for p in profiles)
        assert all(p.has_figures() for p in profiles)
        nozzle = _family_range("pla", "print_temp_range_c")
        assert all(p.nozzle_temp_range_c == nozzle for p in profiles)
        assert asks == ["pla"]

    def test_filter_by_parent_material_is_case_insensitive(self):
        assert [p.profile_id for p in list_brand_filament_profiles(parent_material="PLA")] == sorted(_roster()["pla"])

    def test_filter_by_brand(self):
        pid = _first_profile("pla")
        brand = _roster()["pla"][pid]["brand"]
        profiles = list_brand_filament_profiles(brand=brand.lower())
        assert profiles and all(brand.lower() in p.brand.lower() for p in profiles)
        assert pid in {p.profile_id for p in profiles}
        # Partial match: the first word of the brand finds the same rows.
        assert {p.profile_id for p in list_brand_filament_profiles(brand=brand.split()[0])} >= {
            p.profile_id for p in profiles
        }

    def test_filter_by_both(self):
        pid = _first_profile("pla")
        brand = _roster()["pla"][pid]["brand"]
        profiles = list_brand_filament_profiles(brand=brand, parent_material="pla")
        assert profiles
        for p in profiles:
            assert p.brand == brand and p.parent_material == "pla" and p.has_figures()

    def test_filter_no_match(self):
        assert list_brand_filament_profiles(brand="NonExistentBrand") == []
        assert list_brand_filament_profiles(parent_material="unobtanium") == []

    def test_sorted_output(self):
        profiles = list_brand_filament_profiles()
        keys = [(p.parent_material, p.profile_id) for p in profiles]
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# _build_brand_profile helper
# ---------------------------------------------------------------------------


class TestBuildBrandProfile:
    """_build_brand_profile() construction from a roster row (+ figures)."""

    def test_a_roster_row_alone_is_names_only(self):
        p = _build_brand_profile("test_pla", "pla", {"brand": "TestBrand", "product_name": "Test PLA"})
        assert p.profile_id == "test_pla" and p.brand == "TestBrand" and p.parent_material == "pla"
        assert not p.has_figures()
        assert all(getattr(p, field) is None for field in _FIGURE_FIELDS)
        assert p.enclosure_required is False and p.hardened_nozzle_required is False
        assert p.ams_compatible is None and p.notes is None and p.source == ""

    def test_minimal_figures(self):
        data = {
            "brand": "TestBrand",
            "product_name": "Test PLA",
            "nozzle_temp_range_c": [190, 220],
            "nozzle_temp_optimal_c": 210,
            "bed_temp_range_c": [40, 60],
            "source": "test",
        }
        p = _build_brand_profile("test_pla", "pla", data)
        assert p.has_figures()
        assert p.bed_temp_optimal_c is None
        assert p.max_volumetric_speed_mm3s is None
        assert p.max_print_speed_mms is None
        assert p.density_g_cm3 is None
        assert p.drying_temp_c is None
        assert p.drying_time_hours is None
        assert p.enclosure_required is False
        assert p.hardened_nozzle_required is False
        assert p.ams_compatible is None
        assert p.notes is None

    def test_all_fields_populated(self):
        data = {
            "brand": "FullBrand",
            "product_name": "Full PLA",
            "nozzle_temp_range_c": [190, 230],
            "nozzle_temp_optimal_c": 215,
            "bed_temp_range_c": [40, 60],
            "bed_temp_optimal_c": 50,
            "max_volumetric_speed_mm3s": 15.0,
            "max_print_speed_mms": 200,
            "density_g_cm3": 1.24,
            "drying_temp_c": 45,
            "drying_time_hours": 6,
            "enclosure_required": True,
            "hardened_nozzle_required": True,
            "ams_compatible": False,
            "notes": "Test notes",
            "source": "test",
        }
        p = _build_brand_profile("full_pla", "pla", data)
        assert p.has_figures()
        assert p.enclosure_required is True
        assert p.hardened_nozzle_required is True
        assert p.ams_compatible is False
        assert p.notes == "Test notes"
        assert p.max_volumetric_speed_mm3s == 15.0


class TestTheRosterIsOnlyARoster:
    """The guard against the figures coming back: every brand_profiles row
    in the public file carries the names and the two printer-damage flags
    and nothing else.  A row that grows a temperature is the leak this
    move closed, restored by hand."""

    ROSTER_KEYS = {"brand", "product_name", "hardened_nozzle_required", "enclosure_required"}

    def test_every_roster_row_carries_names_and_damage_flags_only(self):
        import json

        from kiln import design_intelligence as di

        raw = json.loads(di._DATA_DIR.joinpath("materials.json").read_text(encoding="utf-8"))
        rows = 0
        for mid, material in raw.items():
            if mid.startswith("_"):
                continue
            for pid, row in (material.get("brand_profiles") or {}).items():
                assert set(row) == self.ROSTER_KEYS, f"{mid}.brand_profiles.{pid}: {sorted(row)}"
                assert isinstance(row["hardened_nozzle_required"], bool) and isinstance(row["enclosure_required"], bool), pid
                rows += 1
        assert rows >= 60

    def test_the_damage_flags_reach_the_profile_without_any_figures(self, signed_out_material_install):
        from kiln import design_intelligence as di

        flagged = [p for p in di.list_brand_filament_profiles() if p.hardened_nozzle_required]
        assert flagged, "no roster row needs a hardened nozzle: the flags did not reach the roster"
        assert all(not p.has_figures() for p in flagged)
        warnings = di._check_filament_printer_compat(flagged[0], "bambu_a1")
        assert any("hardened" in w.lower() for w in warnings)

    def test_a_high_temperature_brand_without_its_tune_prints_at_its_family_range(self, signed_out_material_install):
        """A/B: with the cost-table default as the fallback, a high-temperature
        family's brand came out at a PLA temperature under the brand's name."""
        import json

        from kiln import design_intelligence as di

        raw = json.loads(di._DATA_DIR.joinpath("materials.json").read_text(encoding="utf-8"))
        hot = [
            (mid, pid)
            for mid, m in raw.items()
            if not mid.startswith("_") and (m.get("thermal") or {}).get("print_temp_range_c", [0, 0])[0] >= 300
            for pid in (m.get("brand_profiles") or {})
        ]
        assert hot, "no high-temperature family carries a brand profile"
        for mid, pid in hot:
            lo, hi = raw[mid]["thermal"]["print_temp_range_c"]
            resolved = di.resolve_filament(pid)
            assert resolved.is_brand_specific is False, pid
            assert lo <= resolved.nozzle_temp_optimal_c <= hi, (pid, resolved.nozzle_temp_optimal_c, lo, hi)
            assert resolved.nozzle_temp_range_c == [lo, hi], pid
            assert any("not in hand" in w for w in resolved.warnings), pid
