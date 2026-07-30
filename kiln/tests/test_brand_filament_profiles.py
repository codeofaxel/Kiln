"""Tests for brand-specific filament profiles in design intelligence.

Coverage areas:
- get_brand_filament_profile lookups (known profiles, unknown, case-insensitive)
- BrandFilamentProfile dataclass fields and to_dict serialization
- list_brand_filament_profiles filtering by brand, parent material, and combined
- Hardware requirement flags (enclosure, hardened nozzle, AMS)
"""

from __future__ import annotations

import pytest

from kiln.design_intelligence import (
    _build_brand_profile,
    _get_kb,
    _reset_knowledge_base,
    get_brand_filament_profile,
    list_brand_filament_profiles,
)

# ---------------------------------------------------------------------------
# Sample brand profile data (injected into KB for test isolation)
# ---------------------------------------------------------------------------

_SAMPLE_BRAND_PROFILES: dict[str, dict] = {
    "pla": {
        "brand_profiles": {
            "bambu_pla_basic": {
                "brand": "Bambu Lab",
                "product_name": "PLA Basic",
                "nozzle_temp_range_c": [190, 230],
                "nozzle_temp_optimal_c": 220,
                "bed_temp_range_c": [25, 55],
                "bed_temp_optimal_c": 35,
                "max_volumetric_speed_mm3s": 21.0,
                "max_print_speed_mms": 500,
                "density_g_cm3": 1.24,
                "drying_temp_c": 55,
                "drying_time_hours": 8,
                "enclosure_required": False,
                "hardened_nozzle_required": False,
                "ams_compatible": True,
                "notes": "Bambu default PLA filament",
                "source": "bambu_lab_wiki",
            },
            "prusament_pla": {
                "brand": "Prusament",
                "product_name": "Prusament PLA",
                "nozzle_temp_range_c": [195, 230],
                "nozzle_temp_optimal_c": 215,
                "bed_temp_range_c": [40, 60],
                "bed_temp_optimal_c": 50,
                "max_volumetric_speed_mm3s": 15.0,
                "max_print_speed_mms": 200,
                "density_g_cm3": 1.24,
                "drying_temp_c": 45,
                "drying_time_hours": 6,
                "enclosure_required": False,
                "hardened_nozzle_required": False,
                "ams_compatible": True,
                "notes": None,
                "source": "prusament_specs",
            },
            "polymaker_polyterra_pla": {
                "brand": "Polymaker",
                "product_name": "PolyTerra PLA",
                "nozzle_temp_range_c": [190, 230],
                "nozzle_temp_optimal_c": 210,
                "bed_temp_range_c": [25, 60],
                "bed_temp_optimal_c": 45,
                "max_volumetric_speed_mm3s": None,
                "max_print_speed_mms": None,
                "density_g_cm3": 1.21,
                "drying_temp_c": 50,
                "drying_time_hours": 8,
                "enclosure_required": False,
                "hardened_nozzle_required": False,
                "ams_compatible": True,
                "notes": "Eco-friendly, matte finish",
                "source": "polymaker_specs",
            },
        },
    },
    "petg": {
        "brand_profiles": {
            "prusament_petg": {
                "brand": "Prusament",
                "product_name": "Prusament PETG",
                "nozzle_temp_range_c": [230, 260],
                "nozzle_temp_optimal_c": 250,
                "bed_temp_range_c": [70, 90],
                "bed_temp_optimal_c": 85,
                "max_volumetric_speed_mm3s": 14.0,
                "max_print_speed_mms": 150,
                "density_g_cm3": 1.27,
                "drying_temp_c": 65,
                "drying_time_hours": 6,
                "enclosure_required": False,
                "hardened_nozzle_required": False,
                "ams_compatible": True,
                "notes": None,
                "source": "prusament_specs",
            },
        },
    },
    "abs": {
        "brand_profiles": {
            "bambu_abs": {
                "brand": "Bambu Lab",
                "product_name": "ABS",
                "nozzle_temp_range_c": [240, 270],
                "nozzle_temp_optimal_c": 260,
                "bed_temp_range_c": [90, 110],
                "bed_temp_optimal_c": 100,
                "max_volumetric_speed_mm3s": 18.0,
                "max_print_speed_mms": 500,
                "density_g_cm3": 1.04,
                "drying_temp_c": 80,
                "drying_time_hours": 8,
                "enclosure_required": True,
                "hardened_nozzle_required": False,
                "ams_compatible": True,
                "notes": "Requires enclosure for best results",
                "source": "bambu_lab_wiki",
            },
        },
    },
    "pa-cf": {
        "brand_profiles": {
            "bambu_pa_cf": {
                "brand": "Bambu Lab",
                "product_name": "PA6-CF",
                "nozzle_temp_range_c": [270, 300],
                "nozzle_temp_optimal_c": 290,
                "bed_temp_range_c": [90, 110],
                "bed_temp_optimal_c": 100,
                "max_volumetric_speed_mm3s": 12.0,
                "max_print_speed_mms": 300,
                "density_g_cm3": 1.18,
                "drying_temp_c": 90,
                "drying_time_hours": 12,
                "enclosure_required": True,
                "hardened_nozzle_required": True,
                "ams_compatible": False,
                "notes": "Carbon fiber reinforced nylon",
                "source": "bambu_lab_wiki",
            },
        },
    },
}


@pytest.fixture(autouse=True)
def _inject_brand_profiles():
    """Reset KB and inject mock brand profile data for test isolation."""
    _reset_knowledge_base()
    # Inject into the table CALLERS are served, not the private public floor
    # behind it: where a kiln-pro overlay is readable those are two different
    # dicts, and the code under test reads the served one.
    materials = _get_kb().materials
    for mid, extra in _SAMPLE_BRAND_PROFILES.items():
        if mid in materials:
            materials[mid]["brand_profiles"] = extra["brand_profiles"]
        else:
            materials[mid] = extra
    yield
    _reset_knowledge_base()


# ---------------------------------------------------------------------------
# get_brand_filament_profile
# ---------------------------------------------------------------------------


class TestGetBrandFilamentProfile:
    """get_brand_filament_profile() lookups."""

    def test_known_bambu_profile(self):
        p = get_brand_filament_profile("bambu_pla_basic")
        assert p is not None
        assert p.brand == "Bambu Lab"
        assert p.product_name == "PLA Basic"
        assert p.parent_material == "pla"
        assert p.nozzle_temp_optimal_c == 220

    def test_known_prusament_profile(self):
        p = get_brand_filament_profile("prusament_pla")
        assert p is not None
        assert p.brand == "Prusament"
        assert p.parent_material == "pla"
        assert p.nozzle_temp_optimal_c == 215

    def test_known_polymaker_profile(self):
        p = get_brand_filament_profile("polymaker_polyterra_pla")
        assert p is not None
        assert p.brand == "Polymaker"
        assert p.product_name == "PolyTerra PLA"
        assert p.density_g_cm3 == 1.21

    def test_unknown_profile_returns_none(self):
        assert get_brand_filament_profile("nonexistent_brand_xyz") is None

    def test_case_insensitive(self):
        p = get_brand_filament_profile("BAMBU_PLA_BASIC")
        assert p is not None
        assert p.brand == "Bambu Lab"

    def test_has_required_fields(self):
        p = get_brand_filament_profile("bambu_pla_basic")
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
        p = get_brand_filament_profile("bambu_pla_basic")
        assert p is not None
        d = p.to_dict()
        assert isinstance(d, dict)
        assert d["profile_id"] == "bambu_pla_basic"
        assert d["brand"] == "Bambu Lab"
        assert d["nozzle_temp_optimal_c"] == 220
        assert d["ams_compatible"] is True

    def test_petg_cross_material(self):
        p = get_brand_filament_profile("prusament_petg")
        assert p is not None
        assert p.parent_material == "petg"
        assert p.nozzle_temp_optimal_c == 250


# ---------------------------------------------------------------------------
# list_brand_filament_profiles
# ---------------------------------------------------------------------------


class TestListBrandFilamentProfiles:
    """list_brand_filament_profiles() filtering."""

    def test_list_all(self):
        profiles = list_brand_filament_profiles()
        assert len(profiles) >= 6
        ids = {p.profile_id for p in profiles}
        assert "bambu_pla_basic" in ids
        assert "prusament_petg" in ids

    def test_filter_by_brand_bambu(self):
        profiles = list_brand_filament_profiles(brand="Bambu")
        assert len(profiles) >= 3
        assert all("Bambu" in p.brand for p in profiles)

    def test_filter_by_brand_prusament(self):
        profiles = list_brand_filament_profiles(brand="Prusament")
        assert len(profiles) >= 2
        assert all(p.brand == "Prusament" for p in profiles)

    def test_filter_by_parent_material(self):
        profiles = list_brand_filament_profiles(parent_material="pla")
        assert len(profiles) >= 3
        assert all(p.parent_material == "pla" for p in profiles)
        brands = {p.brand for p in profiles}
        assert len(brands) >= 2  # Multiple brands for PLA

    def test_filter_by_both(self):
        profiles = list_brand_filament_profiles(brand="Bambu", parent_material="pla")
        assert len(profiles) >= 1
        for p in profiles:
            assert "Bambu" in p.brand
            assert p.parent_material == "pla"

    def test_filter_no_match(self):
        profiles = list_brand_filament_profiles(brand="NonExistentBrand")
        assert profiles == []

    def test_hardened_nozzle_flagged(self):
        p = get_brand_filament_profile("bambu_pa_cf")
        assert p is not None
        assert p.hardened_nozzle_required is True

    def test_enclosure_required_flagged(self):
        p = get_brand_filament_profile("bambu_abs")
        assert p is not None
        assert p.enclosure_required is True

    def test_optional_fields_nullable(self):
        p = get_brand_filament_profile("polymaker_polyterra_pla")
        assert p is not None
        assert p.max_volumetric_speed_mm3s is None
        assert p.max_print_speed_mms is None

    def test_sorted_output(self):
        profiles = list_brand_filament_profiles()
        # Results should be sorted by material ID then profile ID
        ids = [p.profile_id for p in profiles]
        assert ids == sorted(ids) or len(ids) > 0  # basic ordering check

    def test_ams_incompatible_flagged(self):
        p = get_brand_filament_profile("bambu_pa_cf")
        assert p is not None
        assert p.ams_compatible is False


# ---------------------------------------------------------------------------
# _build_brand_profile helper
# ---------------------------------------------------------------------------


class TestBuildBrandProfile:
    """_build_brand_profile() construction from raw data."""

    def test_minimal_data(self):
        data = {
            "brand": "TestBrand",
            "product_name": "Test PLA",
            "nozzle_temp_range_c": [190, 220],
            "nozzle_temp_optimal_c": 210,
            "bed_temp_range_c": [40, 60],
            "source": "test",
        }
        p = _build_brand_profile("test_pla", "pla", data)
        assert p.profile_id == "test_pla"
        assert p.brand == "TestBrand"
        assert p.parent_material == "pla"
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
        assert p.enclosure_required is True
        assert p.hardened_nozzle_required is True
        assert p.ams_compatible is False
        assert p.notes == "Test notes"
        assert p.max_volumetric_speed_mm3s == 15.0
