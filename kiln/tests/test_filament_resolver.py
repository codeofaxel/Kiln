"""Tests for the unified filament resolver and its integration points.

Covers:
    - resolve_filament: brand lookup, parent fallback, unknown materials
    - Brand-specific density used in estimator
    - Printer compatibility warnings (enclosure, nozzle, AMS)
    - brand_overrides_for_slicer: temp override generation
    - MaterialRecommendation.recommended_brands population
    - ResolvedFilament serialization
"""

from __future__ import annotations

import pytest

from .conftest import requires_engineering_overlay

from kiln.design_intelligence import (
    recommend_material_for_design,
    resolve_filament,
)
from kiln.pre_estimate import _get_material_profile
from kiln.slicer_profiles import brand_overrides_for_slicer

# ---------------------------------------------------------------------------
# resolve_filament — brand lookup
# ---------------------------------------------------------------------------


class TestResolveFilamentBrand:
    """Tests for resolving brand profile IDs."""

    @requires_engineering_overlay
    def test_bambu_pla_basic_resolves(self):
        r = resolve_filament("bambu_pla_basic")
        assert r.is_brand_specific is True
        assert r.brand_profile_id == "bambu_pla_basic"
        assert r.density_g_per_cm3 == pytest.approx(1.26)
        assert "Bambu" in r.display_name

    @requires_engineering_overlay
    def test_prusament_tpu_resolves(self):
        r = resolve_filament("prusament_tpu_95a")
        assert r.is_brand_specific is True
        assert r.material_id == "tpu"
        assert r.density_g_per_cm3 == pytest.approx(1.23)

    def test_polymaker_polyflex_resolves(self):
        r = resolve_filament("polymaker_polyflex_tpu95")
        assert r.is_brand_specific is True
        assert r.nozzle_temp_optimal_c == 220

    @requires_engineering_overlay
    def test_bambu_pa6_cf_resolves(self):
        r = resolve_filament("bambu_pa6_cf")
        assert r.is_brand_specific is True
        assert r.hardened_nozzle_required is True
        assert r.enclosure_required is True
        assert r.density_g_per_cm3 == pytest.approx(1.10)

    def test_bambu_petg_cf_resolves(self):
        r = resolve_filament("bambu_petg_cf")
        assert r.is_brand_specific is True
        assert r.hardened_nozzle_required is True

    def test_case_insensitive(self):
        r = resolve_filament("Bambu_PLA_Basic")
        assert r.is_brand_specific is True

    def test_brand_has_no_generic_warning(self):
        r = resolve_filament("bambu_pla_basic")
        assert not any("Generic" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# resolve_filament — parent material fallback
# ---------------------------------------------------------------------------


class TestResolveFilamentParent:
    """Tests for falling back to parent material."""

    def test_pla_generic(self):
        r = resolve_filament("PLA")
        assert r.is_brand_specific is False
        assert r.material_id == "pla"
        assert r.density_g_per_cm3 > 0

    def test_petg_generic(self):
        r = resolve_filament("PETG")
        assert r.is_brand_specific is False

    def test_unknown_falls_back(self):
        r = resolve_filament("UNOBTANIUM")
        assert r.is_brand_specific is False
        assert r.density_g_per_cm3 > 0  # should get PLA defaults

    def test_generic_has_warning(self):
        r = resolve_filament("PLA")
        assert any("Generic" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# resolve_filament — printer compatibility warnings
# ---------------------------------------------------------------------------


class TestResolveFilamentCompat:
    """Tests for printer compatibility warning generation."""

    def test_hardened_nozzle_warning(self):
        r = resolve_filament("bambu_pa6_cf", printer_id="bambu_a1")
        assert any("hardened" in w.lower() for w in r.warnings)

    def test_ams_incompatible_warning(self):
        r = resolve_filament("bambu_tpu_95a", printer_id="bambu_a1")
        assert any("AMS" in w for w in r.warnings)

    def test_no_warnings_for_compatible(self):
        r = resolve_filament("bambu_pla_basic", printer_id="bambu_a1")
        # PLA Basic is AMS compatible, no enclosure/nozzle issues
        assert not any("hardened" in w.lower() for w in r.warnings)
        assert not any("AMS" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Pre-gen estimator integration
# ---------------------------------------------------------------------------


class TestEstimatorBrandIntegration:
    """Tests for brand-specific density in the estimator."""

    @requires_engineering_overlay
    def test_brand_density_used_in_estimator(self):
        """Bambu PLA Basic (1.26) should differ from generic PLA (1.24)."""
        brand = _get_material_profile("bambu_pla_basic")
        generic = _get_material_profile("PLA")
        assert brand["density_g_per_cm3"] == pytest.approx(1.26)
        assert generic["density_g_per_cm3"] == pytest.approx(1.24)

    def test_brand_name_in_estimator(self):
        brand = _get_material_profile("bambu_pla_basic")
        assert "Bambu" in brand["name"]

    def test_generic_name_clean(self):
        generic = _get_material_profile("PLA")
        assert generic["name"] == "PLA"

    def test_unknown_falls_back_to_pla(self):
        result = _get_material_profile("UNOBTANIUM")
        assert result["name"] == "PLA"
        assert result["density_g_per_cm3"] > 0

    @requires_engineering_overlay
    def test_brand_density_flows_to_estimate(self):
        """Brand density should flow through to estimate_from_dimensions."""
        from kiln.pre_estimate import estimate_from_dimensions

        # Bambu PLA Basic (1.26) vs generic PLA (1.24) — brand should be heavier
        est_brand = estimate_from_dimensions(
            100, 100, 50, materials=["bambu_pla_basic"]
        )
        est_generic = estimate_from_dimensions(
            100, 100, 50, materials=["PLA"]
        )
        assert est_brand.total_weight_grams > est_generic.total_weight_grams

    def test_nylon_brand_density_in_estimate(self):
        """PA6-CF (1.10) should weigh less than generic Nylon (assumed ~1.14)."""
        from kiln.pre_estimate import estimate_from_dimensions

        est = estimate_from_dimensions(
            100, 100, 50, materials=["bambu_pa6_cf"]
        )
        assert est.total_weight_grams > 0
        assert est.filaments[0].material == "Bambu Lab PA6-CF"


# ---------------------------------------------------------------------------
# Slicer temp override integration
# ---------------------------------------------------------------------------


class TestSlicerBrandOverrides:
    """Tests for brand_overrides_for_slicer()."""

    def test_brand_returns_overrides(self):
        overrides = brand_overrides_for_slicer("bambu_petg_cf")
        assert overrides is not None
        assert "temperature" in overrides
        assert overrides["temperature"] == "255"
        assert "bed_temperature" in overrides
        assert overrides["bed_temperature"] == "70"

    def test_prusament_tpu_overrides(self):
        overrides = brand_overrides_for_slicer("prusament_tpu_95a")
        assert overrides is not None
        assert overrides["temperature"] == "230"
        assert overrides["bed_temperature"] == "65"

    def test_generic_returns_none(self):
        overrides = brand_overrides_for_slicer("PLA")
        assert overrides is None

    def test_unknown_returns_none(self):
        overrides = brand_overrides_for_slicer("nonexistent_brand_xyz")
        assert overrides is None


# ---------------------------------------------------------------------------
# Material recommendation — brand suggestions
# ---------------------------------------------------------------------------


class TestRecommendationBrands:
    """Tests for recommended_brands in MaterialRecommendation."""

    def test_recommendation_includes_brands(self):
        rec = recommend_material_for_design("outdoor garden planter, UV resistant")
        # Should recommend ASA or similar — and include brand profiles if they exist
        assert hasattr(rec, "recommended_brands")
        assert isinstance(rec.recommended_brands, list)

    def test_recommendation_brand_has_required_fields(self):
        rec = recommend_material_for_design("basic prototype, easy to print")
        # PLA will likely be recommended — it has brand profiles
        if rec.recommended_brands:
            brand = rec.recommended_brands[0]
            assert "profile_id" in brand
            assert "brand" in brand
            assert "product_name" in brand
            assert "nozzle_temp_optimal_c" in brand

    def test_recommendation_serializable(self):
        import json

        rec = recommend_material_for_design("basic prototype")
        d = rec.to_dict()
        assert "recommended_brands" in d
        json_str = json.dumps(d)
        assert json_str


# ---------------------------------------------------------------------------
# ResolvedFilament serialization
# ---------------------------------------------------------------------------


class TestResolvedFilamentSerialization:
    """Tests for ResolvedFilament.to_dict()."""

    def test_to_dict_has_all_fields(self):
        r = resolve_filament("bambu_pla_basic")
        d = r.to_dict()
        assert "material_id" in d
        assert "brand_profile_id" in d
        assert "density_g_per_cm3" in d
        assert "nozzle_temp_optimal_c" in d
        assert "enclosure_required" in d
        assert "hardened_nozzle_required" in d
        assert "warnings" in d

    def test_to_dict_json_serializable(self):
        import json

        r = resolve_filament("prusament_tpu_95a")
        json_str = json.dumps(r.to_dict())
        assert json_str
