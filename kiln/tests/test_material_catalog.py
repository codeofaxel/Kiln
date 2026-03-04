"""Tests for material catalog — brands, variants, sources, compatibility.

Covers:
    - _load() succeeds and _meta is skipped
    - get_material_entry() for known and unknown materials
    - list_catalog_ids() returns sorted non-empty list
    - search_catalog() by brand, material type, partial match, case insensitive
    - get_compatible_materials() for known and unknown families
    - get_purchase_urls() with and without color substitution
    - find_matching_entry() by vendor, material_type, color
    - MaterialCatalogEntry.to_dict() serialization
    - MaterialSource dataclass fields and immutability
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from kiln.material_catalog import (
    _DATA_FILE,
    MaterialSource,
    find_matching_entry,
    get_compatible_materials,
    get_material_entry,
    get_purchase_urls,
    list_catalog_ids,
    search_catalog,
)

# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_catalog():
    """Reset the singleton cache before each test for isolation."""
    import kiln.material_catalog as mod
    mod._catalog = None
    yield
    mod._catalog = None


# ===================================================================
# Loader
# ===================================================================

class TestMaterialCatalogLoader:
    """Tests for catalog loading and singleton caching."""

    def test_load_succeeds(self) -> None:
        ids = list_catalog_ids()
        assert len(ids) > 0

    def test_meta_skipped(self) -> None:
        ids = list_catalog_ids()
        assert "_meta" not in ids

    def test_known_entry_exists(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        assert entry.id == "hatchbox_pla"
        assert entry.vendor == "Hatchbox"

    def test_unknown_returns_none(self) -> None:
        entry = get_material_entry("nonexistent_material_xyz")
        assert entry is None

    def test_list_catalog_ids_returns_sorted(self) -> None:
        ids = list_catalog_ids()
        assert ids == sorted(ids)

    def test_list_catalog_ids_has_generics(self) -> None:
        ids = list_catalog_ids()
        assert "generic_pla" in ids
        assert "generic_petg" in ids

    def test_json_file_exists_and_parses(self) -> None:
        assert _DATA_FILE.exists()
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert "_meta" in raw

    def test_at_least_50_entries(self) -> None:
        ids = list_catalog_ids()
        assert len(ids) >= 50


# ===================================================================
# Search
# ===================================================================

class TestMaterialCatalogSearch:
    """Tests for search_catalog() text matching."""

    def test_search_by_brand_name(self) -> None:
        results = search_catalog("Hatchbox")
        assert len(results) >= 1
        assert all(r.vendor == "Hatchbox" for r in results)

    def test_search_by_material_type(self) -> None:
        results = search_catalog("PETG")
        assert len(results) >= 1
        assert all("petg" in r.material_type.lower() or "petg" in r.notes.lower() for r in results)

    def test_search_case_insensitive(self) -> None:
        upper = search_catalog("HATCHBOX PLA")
        lower = search_catalog("hatchbox pla")
        assert len(upper) == len(lower)
        assert len(upper) >= 1

    def test_search_no_results(self) -> None:
        results = search_catalog("nonexistentbrandxyz123")
        assert results == []

    def test_search_partial_match(self) -> None:
        results = search_catalog("eSun")
        assert len(results) >= 2

    def test_search_empty_query(self) -> None:
        results = search_catalog("")
        assert results == []

    def test_search_multi_token(self) -> None:
        results = search_catalog("Bambu TPU")
        assert len(results) >= 1
        assert results[0].vendor == "Bambu Lab"


# ===================================================================
# Compatible materials
# ===================================================================

class TestCompatibleMaterials:
    """Tests for get_compatible_materials() family grouping."""

    def test_pla_family_returns_multiple(self) -> None:
        results = get_compatible_materials("pla")
        assert len(results) >= 5

    def test_unknown_family_returns_empty(self) -> None:
        results = get_compatible_materials("unobtainium")
        assert results == []

    def test_entries_share_material_family(self) -> None:
        results = get_compatible_materials("petg")
        for entry in results:
            assert entry.material_family == "petg"

    def test_resin_family(self) -> None:
        results = get_compatible_materials("resin")
        assert len(results) >= 3
        for entry in results:
            assert entry.diameter_mm is None


# ===================================================================
# Purchase URLs
# ===================================================================

class TestPurchaseUrls:
    """Tests for get_purchase_urls() URL generation."""

    def test_known_material_returns_amazon_and_manufacturer(self) -> None:
        urls = get_purchase_urls("hatchbox_pla")
        assert "amazon" in urls
        assert "manufacturer" in urls
        assert "amazon.com" in urls["amazon"]
        assert "hatchbox3d.com" in urls["manufacturer"]

    def test_color_substitution_works(self) -> None:
        urls = get_purchase_urls("hatchbox_pla", color="blue")
        assert "blue" in urls["amazon"]
        assert "{color}" not in urls["amazon"]

    def test_unknown_material_returns_empty_dict(self) -> None:
        urls = get_purchase_urls("nonexistent_material_xyz")
        assert urls == {}

    def test_no_color_omits_placeholder(self) -> None:
        urls = get_purchase_urls("hatchbox_pla")
        assert "{color}" not in urls["amazon"]

    def test_amazon_url_format(self) -> None:
        urls = get_purchase_urls("esun_petg", color="black")
        assert urls["amazon"].startswith("https://www.amazon.com/s?k=")

    def test_generic_material_urls(self) -> None:
        urls = get_purchase_urls("generic_pla")
        assert "amazon" in urls
        assert "{color}" not in urls["amazon"]


# ===================================================================
# Find matching entry
# ===================================================================

class TestFindMatchingEntry:
    """Tests for find_matching_entry() fuzzy lookup."""

    def test_match_by_vendor_and_material(self) -> None:
        entry = find_matching_entry(vendor="Hatchbox", material_type="PLA")
        assert entry is not None
        assert entry.vendor == "Hatchbox"
        assert "PLA" in entry.material_type

    def test_match_by_vendor_only(self) -> None:
        entry = find_matching_entry(vendor="Prusament")
        assert entry is not None
        assert entry.vendor == "Prusament"

    def test_no_match_returns_none(self) -> None:
        entry = find_matching_entry(vendor="NonexistentBrandXYZ")
        assert entry is None

    def test_case_insensitive_match(self) -> None:
        entry = find_matching_entry(vendor="hatchbox", material_type="pla")
        assert entry is not None
        assert entry.vendor == "Hatchbox"

    def test_no_args_returns_none(self) -> None:
        entry = find_matching_entry()
        assert entry is None

    def test_color_preference(self) -> None:
        entry = find_matching_entry(vendor="eSun", material_type="PLA", color="fire_engine_red")
        assert entry is not None
        assert entry.vendor == "eSun"


# ===================================================================
# Serialization
# ===================================================================

class TestMaterialCatalogEntrySerialization:
    """Tests for MaterialCatalogEntry.to_dict() serialization."""

    def test_to_dict_all_fields_present(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        d = entry.to_dict()
        expected_keys = [
            "id", "vendor", "material_type", "material_family",
            "diameter_mm", "weight_options_kg", "price_range_usd",
            "variants", "sources", "compatible_with", "notes",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in to_dict()"

    def test_to_dict_json_serializable(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        serialized = json.dumps(entry.to_dict())
        assert isinstance(serialized, str)

    def test_to_dict_roundtrip_id(self) -> None:
        entry = get_material_entry("esun_petg")
        assert entry is not None
        d = entry.to_dict()
        assert d["id"] == entry.id
        assert d["vendor"] == entry.vendor
        assert d["material_type"] == entry.material_type

    def test_to_dict_sources_are_dict(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        d = entry.to_dict()
        assert isinstance(d["sources"], dict)
        assert "amazon" in d["sources"]
        assert "manufacturer" in d["sources"]

    def test_to_dict_tuples_become_lists(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        d = entry.to_dict()
        assert isinstance(d["variants"], list)
        assert isinstance(d["weight_options_kg"], list)
        assert isinstance(d["price_range_usd"], list)
        assert isinstance(d["compatible_with"], list)


# ===================================================================
# Dataclass immutability
# ===================================================================

class TestMaterialSourceDataclass:
    """Tests for MaterialSource frozen dataclass."""

    def test_fields(self) -> None:
        src = MaterialSource(amazon="test+query", manufacturer="https://example.com")
        assert src.amazon == "test+query"
        assert src.manufacturer == "https://example.com"

    def test_frozen(self) -> None:
        src = MaterialSource(amazon="test", manufacturer="https://example.com")
        with pytest.raises(FrozenInstanceError):
            src.amazon = "other"  # type: ignore[misc]


class TestMaterialCatalogEntryDataclass:
    """Tests for MaterialCatalogEntry frozen dataclass."""

    def test_frozen(self) -> None:
        entry = get_material_entry("hatchbox_pla")
        assert entry is not None
        with pytest.raises(FrozenInstanceError):
            entry.vendor = "Other"  # type: ignore[misc]

    def test_generic_entry_has_null_vendor(self) -> None:
        entry = get_material_entry("generic_pla")
        assert entry is not None
        assert entry.vendor is None

    def test_resin_entry_has_null_diameter(self) -> None:
        entry = get_material_entry("elegoo_standard_resin")
        assert entry is not None
        assert entry.diameter_mm is None
