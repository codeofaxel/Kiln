"""Tests for kiln.consumer -- consumer workflow utilities.

Coverage areas:
- AddressValidation: dataclass, to_dict serialization
- validate_address: required fields, postal code patterns (US/CA/GB),
  normalization, warnings, zip alias, empty address, country case
- UseCase enum: count and values
- MaterialRecommendation / MaterialGuide: dataclass, to_dict
- recommend_material: all use cases, unknown use case, budget/weather/
  food_safe/high_detail/high_strength filters, combined filters,
  fallback when filters eliminate everything, best_pick, explanation
- TimelineStage / OrderTimeline: dataclass, to_dict
- estimate_timeline: FDM/SLA baselines, custom shipping_days,
  quantity 2-5 / >5 scaling, unknown technology fallback,
  shipping by country, confidence tiers, ISO delivery date
- PriceEstimate: dataclass, to_dict
- estimate_price: volume_cm3, dimensions_mm conversion, neither raises,
  unknown technology raises, quantity multiplied, confidence tiers,
  zero/negative dimensions, base fee per technology
- OnboardingStep / ConsumerOnboarding: dataclass, to_dict
- get_onboarding: 7 steps, summary non-empty
- list_supported_countries: 23 countries, includes US/GB/JP, returns copy
"""

from __future__ import annotations

import datetime

import pytest

from kiln.consumer import (
    AddressValidation,
    ConsumerOnboarding,
    MaterialGuide,
    MaterialRecommendation,
    OnboardingStep,
    OrderTimeline,
    PriceEstimate,
    TimelineStage,
    UseCase,
    estimate_price,
    estimate_timeline,
    get_onboarding,
    list_supported_countries,
    recommend_material,
    validate_address,
)


# ---------------------------------------------------------------------------
# AddressValidation dataclass
# ---------------------------------------------------------------------------


class TestAddressValidationDataclass:
    """AddressValidation to_dict and field defaults."""

    def test_to_dict_all_fields(self):
        av = AddressValidation(
            valid=True,
            address={"street": "123 Main St"},
            warnings=["warn"],
            errors=[],
            normalized={"street": "123 Main St"},
        )
        d = av.to_dict()
        assert d["valid"] is True
        assert d["address"] == {"street": "123 Main St"}
        assert d["warnings"] == ["warn"]
        assert d["errors"] == []
        assert d["normalized"] == {"street": "123 Main St"}

    def test_to_dict_defaults(self):
        av = AddressValidation(valid=False, address={})
        d = av.to_dict()
        assert d["warnings"] == []
        assert d["errors"] == []
        assert d["normalized"] == {}


# ---------------------------------------------------------------------------
# validate_address
# ---------------------------------------------------------------------------


class TestValidateAddress:
    """Required fields, postal code formats, warnings, normalization."""

    def test_valid_us_address_all_fields(self):
        result = validate_address({
            "street": "123 Main St",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        })
        assert result.valid is True
        assert result.errors == []
        assert result.normalized["street"] == "123 Main St"
        assert result.normalized["city"] == "Austin"
        assert result.normalized["state"] == "TX"
        assert result.normalized["postal_code"] == "78701"
        assert result.normalized["country"] == "US"

    def test_missing_street_error(self):
        result = validate_address({
            "city": "Austin",
            "country": "US",
            "postal_code": "78701",
        })
        assert result.valid is False
        assert any("Street" in e for e in result.errors)

    def test_missing_city_error(self):
        result = validate_address({
            "street": "123 Main St",
            "country": "US",
            "postal_code": "78701",
        })
        assert result.valid is False
        assert any("City" in e for e in result.errors)

    def test_missing_country_error(self):
        result = validate_address({
            "street": "123 Main St",
            "city": "Austin",
        })
        assert result.valid is False
        assert any("Country" in e for e in result.errors)

    def test_unsupported_country_error_with_supported_list(self):
        result = validate_address({
            "street": "123 Main St",
            "city": "Lagos",
            "country": "NG",
        })
        assert result.valid is False
        assert any("not supported" in e for e in result.errors)
        assert any("Supported:" in e for e in result.errors)

    def test_valid_us_zip_5_digit(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "90210",
            "country": "US",
        })
        assert result.valid is True

    def test_valid_us_zip_5_plus_4(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "90210-1234",
            "country": "US",
        })
        assert result.valid is True

    def test_invalid_us_zip_error(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "ABCDE",
            "country": "US",
        })
        assert result.valid is False
        assert any("Invalid US ZIP" in e for e in result.errors)

    def test_valid_canadian_postal_with_space(self):
        result = validate_address({
            "street": "24 Sussex Drive",
            "city": "Ottawa",
            "state": "ON",
            "postal_code": "K1A 0A6",
            "country": "CA",
        })
        assert result.valid is True

    def test_valid_canadian_postal_no_space(self):
        result = validate_address({
            "street": "24 Sussex Drive",
            "city": "Ottawa",
            "postal_code": "K1A0A6",
            "country": "CA",
        })
        assert result.valid is True

    def test_invalid_canadian_postal_error(self):
        result = validate_address({
            "street": "24 Sussex Drive",
            "city": "Ottawa",
            "postal_code": "12345",
            "country": "CA",
        })
        assert result.valid is False
        assert any("Invalid Canadian" in e for e in result.errors)

    def test_valid_uk_postcode(self):
        result = validate_address({
            "street": "10 Downing Street",
            "city": "London",
            "postal_code": "SW1A 2AA",
            "country": "GB",
        })
        assert result.valid is True

    def test_invalid_uk_postcode_error(self):
        result = validate_address({
            "street": "10 Downing Street",
            "city": "London",
            "postal_code": "INVALID",
            "country": "GB",
        })
        assert result.valid is False
        assert any("Invalid UK" in e for e in result.errors)

    def test_missing_postal_code_us_warning(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "country": "US",
        })
        assert result.valid is True
        assert any("Postal code" in w for w in result.warnings)

    def test_missing_postal_code_ca_warning(self):
        result = validate_address({
            "street": "1 St",
            "city": "Toronto",
            "country": "CA",
        })
        assert result.valid is True
        assert any("Postal code" in w for w in result.warnings)

    def test_missing_postal_code_gb_warning(self):
        result = validate_address({
            "street": "1 St",
            "city": "London",
            "country": "GB",
        })
        assert result.valid is True
        assert any("Postal code" in w for w in result.warnings)

    def test_missing_state_us_warning(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "postal_code": "78701",
            "country": "US",
        })
        assert result.valid is True
        assert any("State" in w for w in result.warnings)

    def test_zip_key_alias_works_for_postal_code(self):
        result = validate_address({
            "street": "123 Main St",
            "city": "Austin",
            "state": "TX",
            "zip": "78701",
            "country": "US",
        })
        assert result.valid is True
        assert result.normalized["postal_code"] == "78701"

    def test_empty_address_multiple_errors(self):
        result = validate_address({})
        assert result.valid is False
        assert len(result.errors) >= 3

    def test_normalized_output_only_when_valid(self):
        valid = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        })
        assert valid.normalized != {}

        invalid = validate_address({})
        assert invalid.normalized == {}

    def test_country_case_normalized(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "78701",
            "country": "us",
        })
        assert result.valid is True
        assert result.normalized["country"] == "US"

    def test_whitespace_stripped(self):
        result = validate_address({
            "street": "  123 Main  ",
            "city": "  Austin  ",
            "state": "  TX  ",
            "postal_code": "78701",
            "country": "  US  ",
        })
        assert result.valid is True
        assert result.normalized["street"] == "123 Main"
        assert result.normalized["city"] == "Austin"
        assert result.normalized["state"] == "TX"

    def test_to_dict_returns_all_fields(self):
        result = validate_address({
            "street": "1 St",
            "city": "C",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        })
        d = result.to_dict()
        assert "valid" in d
        assert "address" in d
        assert "warnings" in d
        assert "errors" in d
        assert "normalized" in d


# ---------------------------------------------------------------------------
# UseCase enum
# ---------------------------------------------------------------------------


class TestUseCase:
    """UseCase enum has exactly 10 values with string representations."""

    def test_enum_count(self):
        assert len(UseCase) == 10

    def test_all_values(self):
        expected = {
            "decorative", "functional", "mechanical", "prototype",
            "miniature", "jewelry", "enclosure", "wearable",
            "outdoor", "food_safe",
        }
        assert {uc.value for uc in UseCase} == expected


# ---------------------------------------------------------------------------
# MaterialRecommendation / MaterialGuide dataclasses
# ---------------------------------------------------------------------------


class TestMaterialDataclasses:
    """to_dict serialization for MaterialRecommendation and MaterialGuide."""

    def _sample_rec(self) -> MaterialRecommendation:
        return MaterialRecommendation(
            technology="FDM",
            material_name="PLA",
            reason="Cheap and easy",
            price_tier="budget",
            strength="low",
            detail_level="medium",
            weather_resistant=False,
            food_safe=False,
            typical_lead_days=5,
            recommended_provider="craftcloud",
        )

    def test_recommendation_to_dict(self):
        rec = self._sample_rec()
        d = rec.to_dict()
        assert d["technology"] == "FDM"
        assert d["material_name"] == "PLA"
        assert d["weather_resistant"] is False
        assert d["food_safe"] is False
        assert d["typical_lead_days"] == 5

    def test_guide_to_dict(self):
        rec = self._sample_rec()
        guide = MaterialGuide(
            use_case="functional",
            recommendations=[rec],
            best_pick=rec,
            explanation="Test explanation",
        )
        d = guide.to_dict()
        assert d["use_case"] == "functional"
        assert len(d["recommendations"]) == 1
        assert isinstance(d["recommendations"][0], dict)
        assert isinstance(d["best_pick"], dict)
        assert d["explanation"] == "Test explanation"


# ---------------------------------------------------------------------------
# recommend_material
# ---------------------------------------------------------------------------


class TestRecommendMaterial:
    """Use case matching, filters, fallback, explanation content."""

    @pytest.mark.parametrize("use_case", [uc.value for uc in UseCase])
    def test_each_use_case_returns_recommendations(self, use_case):
        guide = recommend_material(use_case)
        assert len(guide.recommendations) > 0
        assert guide.best_pick is not None
        assert guide.use_case == use_case

    def test_unknown_use_case_raises(self):
        with pytest.raises(ValueError, match="Unknown use case"):
            recommend_material("teleportation")

    def test_budget_filter_narrows_results(self):
        guide = recommend_material("decorative", budget="budget")
        for rec in guide.recommendations:
            assert rec.price_tier == "budget"

    def test_weather_resistant_filter(self):
        guide = recommend_material("functional", need_weather_resistant=True)
        for rec in guide.recommendations:
            assert rec.weather_resistant is True

    def test_food_safe_filter(self):
        guide = recommend_material("food_safe", need_food_safe=True)
        for rec in guide.recommendations:
            assert rec.food_safe is True

    def test_high_detail_filter(self):
        guide = recommend_material("decorative", need_high_detail=True)
        for rec in guide.recommendations:
            assert rec.detail_level == "high"

    def test_high_strength_filter(self):
        guide = recommend_material("mechanical", need_high_strength=True)
        for rec in guide.recommendations:
            assert rec.strength == "high"

    def test_multiple_filters_combined(self):
        guide = recommend_material(
            "functional",
            need_weather_resistant=True,
            need_high_strength=True,
        )
        assert len(guide.recommendations) >= 1
        for rec in guide.recommendations:
            assert rec.weather_resistant is True
            assert rec.strength == "high"

    def test_filters_eliminating_everything_fall_back(self):
        # Decorative has no food-safe materials, so food_safe filter
        # eliminates all, then subsequent filters also can't match.
        # Should fall back to original candidates.
        guide = recommend_material(
            "decorative",
            need_food_safe=True,
            need_high_strength=True,
            budget="premium",
            need_weather_resistant=True,
            need_high_detail=True,
        )
        assert len(guide.recommendations) > 0

    def test_best_pick_is_first_recommendation(self):
        guide = recommend_material("functional")
        assert guide.best_pick is guide.recommendations[0]

    def test_explanation_includes_constraints(self):
        guide = recommend_material(
            "decorative",
            budget="budget",
            need_weather_resistant=True,
        )
        assert "budget" in guide.explanation
        assert "weather-resistant" in guide.explanation

    def test_explanation_without_constraints(self):
        guide = recommend_material("decorative")
        assert "filters:" not in guide.explanation
        assert "decorative" in guide.explanation

    def test_case_insensitive_use_case(self):
        guide = recommend_material("DECORATIVE")
        assert guide.use_case == "decorative"
        assert len(guide.recommendations) > 0

    def test_to_dict(self):
        guide = recommend_material("decorative")
        d = guide.to_dict()
        assert "use_case" in d
        assert "recommendations" in d
        assert "best_pick" in d
        assert "explanation" in d
        assert isinstance(d["recommendations"], list)
        assert isinstance(d["best_pick"], dict)


# ---------------------------------------------------------------------------
# TimelineStage / OrderTimeline dataclasses
# ---------------------------------------------------------------------------


class TestTimelineDataclasses:
    """to_dict serialization for TimelineStage and OrderTimeline."""

    def test_stage_to_dict(self):
        stage = TimelineStage(
            stage="production",
            description="Manufacturing",
            estimated_days=3,
        )
        d = stage.to_dict()
        assert d["stage"] == "production"
        assert d["estimated_days"] == 3
        assert d["status"] == "pending"

    def test_stage_custom_status(self):
        stage = TimelineStage(
            stage="production",
            description="Manufacturing",
            estimated_days=3,
            status="in_progress",
        )
        assert stage.to_dict()["status"] == "in_progress"

    def test_timeline_to_dict(self):
        stage = TimelineStage(stage="s1", description="d1", estimated_days=1)
        timeline = OrderTimeline(
            stages=[stage],
            total_days=1,
            estimated_delivery_date="2025-01-01",
            confidence="high",
        )
        d = timeline.to_dict()
        assert len(d["stages"]) == 1
        assert isinstance(d["stages"][0], dict)
        assert d["total_days"] == 1
        assert d["confidence"] == "high"
        assert d["estimated_delivery_date"] == "2025-01-01"


# ---------------------------------------------------------------------------
# estimate_timeline
# ---------------------------------------------------------------------------


class TestEstimateTimeline:
    """Technology baselines, quantity scaling, shipping, confidence."""

    def test_fdm_timeline_stages_correct(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        stage_names = [s.stage for s in tl.stages]
        assert stage_names == [
            "quote_review", "production", "quality_check", "packaging", "shipping",
        ]

    def test_fdm_baseline_days(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        stages = {s.stage: s.estimated_days for s in tl.stages}
        assert stages["quote_review"] == 0
        assert stages["production"] == 3
        assert stages["quality_check"] == 1
        assert stages["packaging"] == 1
        assert stages["shipping"] == 5

    def test_sla_timeline_different_from_fdm(self):
        fdm = estimate_timeline("FDM", shipping_days=5)
        sla = estimate_timeline("SLA", shipping_days=5)
        fdm_prod = next(s for s in fdm.stages if s.stage == "production")
        sla_prod = next(s for s in sla.stages if s.stage == "production")
        assert sla_prod.estimated_days != fdm_prod.estimated_days
        # SLA production baseline is 4 days
        assert sla_prod.estimated_days == 4

    def test_custom_shipping_days_used(self):
        tl = estimate_timeline("FDM", shipping_days=20)
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 20

    def test_quantity_over_5_adds_production_time(self):
        tl_1 = estimate_timeline("FDM", shipping_days=5, quantity=1)
        tl_10 = estimate_timeline("FDM", shipping_days=5, quantity=10)
        prod_1 = next(s for s in tl_1.stages if s.stage == "production")
        prod_10 = next(s for s in tl_10.stages if s.stage == "production")
        # quantity=10: base 3 + (10-5)//5 + 1 = 3 + 2 = 5
        assert prod_10.estimated_days == 5
        assert prod_10.estimated_days > prod_1.estimated_days

    def test_quantity_20_production_time(self):
        tl = estimate_timeline("FDM", shipping_days=5, quantity=20)
        production = next(s for s in tl.stages if s.stage == "production")
        # Base 3, quantity>5: + (20-5)//5 + 1 = 3 + 4 = 7
        assert production.estimated_days == 7

    def test_quantity_2_to_5_adds_1_day(self):
        tl_1 = estimate_timeline("FDM", shipping_days=5, quantity=1)
        tl_3 = estimate_timeline("FDM", shipping_days=5, quantity=3)
        prod_1 = next(s for s in tl_1.stages if s.stage == "production")
        prod_3 = next(s for s in tl_3.stages if s.stage == "production")
        assert prod_3.estimated_days == prod_1.estimated_days + 1

    def test_quantity_5_adds_1_day(self):
        tl_1 = estimate_timeline("FDM", shipping_days=5, quantity=1)
        tl_5 = estimate_timeline("FDM", shipping_days=5, quantity=5)
        prod_1 = next(s for s in tl_1.stages if s.stage == "production")
        prod_5 = next(s for s in tl_5.stages if s.stage == "production")
        assert prod_5.estimated_days == prod_1.estimated_days + 1

    def test_unknown_technology_falls_back_to_fdm(self):
        tl_unknown = estimate_timeline("XYZTECH", shipping_days=5)
        tl_fdm = estimate_timeline("FDM", shipping_days=5)
        for su, sf in zip(tl_unknown.stages, tl_fdm.stages):
            assert su.estimated_days == sf.estimated_days

    def test_shipping_estimate_us(self):
        tl = estimate_timeline("FDM", country="US")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 5

    def test_shipping_estimate_ca(self):
        tl = estimate_timeline("FDM", country="CA")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 5

    def test_shipping_estimate_gb(self):
        tl = estimate_timeline("FDM", country="GB")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 7

    def test_shipping_estimate_de(self):
        tl = estimate_timeline("FDM", country="DE")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 7

    def test_shipping_estimate_jp(self):
        tl = estimate_timeline("FDM", country="JP")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 12

    def test_shipping_estimate_unknown_country(self):
        tl = estimate_timeline("FDM", country="ZZ")
        ship = next(s for s in tl.stages if s.stage == "shipping")
        assert ship.estimated_days == 14

    def test_confidence_high_for_fdm(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        assert tl.confidence == "high"

    def test_confidence_high_for_sla(self):
        tl = estimate_timeline("SLA", shipping_days=5)
        assert tl.confidence == "high"

    def test_confidence_medium_for_sls(self):
        tl = estimate_timeline("SLS", shipping_days=5)
        assert tl.confidence == "medium"

    def test_confidence_medium_for_mjf(self):
        tl = estimate_timeline("MJF", shipping_days=5)
        assert tl.confidence == "medium"

    def test_confidence_low_when_shipping_over_14(self):
        tl = estimate_timeline("FDM", shipping_days=15)
        assert tl.confidence == "low"

    def test_confidence_low_overrides_high_tech(self):
        # FDM would be high, but shipping > 14 should override to low
        tl = estimate_timeline("FDM", shipping_days=20)
        assert tl.confidence == "low"

    def test_estimated_delivery_date_is_iso_format(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        parsed = datetime.date.fromisoformat(tl.estimated_delivery_date)
        assert parsed >= datetime.date.today()

    def test_total_days_equals_stage_sum(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        assert tl.total_days == sum(s.estimated_days for s in tl.stages)

    def test_to_dict(self):
        tl = estimate_timeline("FDM", shipping_days=5)
        d = tl.to_dict()
        assert "stages" in d
        assert "total_days" in d
        assert "estimated_delivery_date" in d
        assert "confidence" in d
        assert isinstance(d["stages"], list)
        assert isinstance(d["stages"][0], dict)


# ---------------------------------------------------------------------------
# PriceEstimate dataclass
# ---------------------------------------------------------------------------


class TestPriceEstimateDataclass:
    """to_dict serialization for PriceEstimate."""

    def test_to_dict(self):
        pe = PriceEstimate(
            estimated_price_low=10.0,
            estimated_price_high=20.0,
            currency="USD",
            technology="FDM",
            material="PLA",
            volume_cm3=50.0,
            confidence="high",
            note="Test note",
        )
        d = pe.to_dict()
        assert d["estimated_price_low"] == 10.0
        assert d["estimated_price_high"] == 20.0
        assert d["currency"] == "USD"
        assert d["technology"] == "FDM"
        assert d["volume_cm3"] == 50.0
        assert d["confidence"] == "high"
        assert d["note"] == "Test note"


# ---------------------------------------------------------------------------
# estimate_price
# ---------------------------------------------------------------------------


class TestEstimatePrice:
    """Volume, dimensions, technology validation, quantity, confidence."""

    def test_volume_cm3_provided_directly(self):
        pe = estimate_price("FDM", volume_cm3=100.0)
        assert pe.volume_cm3 == 100.0
        assert pe.estimated_price_low > 0
        assert pe.estimated_price_high > pe.estimated_price_low

    def test_dimensions_mm_converted(self):
        # 100 * 50 * 20 = 100_000 mm3 -> 100 cm3 * 0.4 = 40.0 cm3
        pe = estimate_price("FDM", dimensions_mm={"x": 100, "y": 50, "z": 20})
        assert pe.volume_cm3 == 40.0

    def test_neither_provided_raises(self):
        with pytest.raises(ValueError, match="Provide either volume_cm3 or dimensions_mm"):
            estimate_price("FDM")

    def test_unknown_technology_raises(self):
        with pytest.raises(ValueError, match="Unknown technology"):
            estimate_price("QUANTUM", volume_cm3=10.0)

    def test_quantity_multiplied_correctly(self):
        pe_1 = estimate_price("FDM", volume_cm3=100.0, quantity=1)
        pe_3 = estimate_price("FDM", volume_cm3=100.0, quantity=3)
        assert pe_3.estimated_price_low == pytest.approx(
            pe_1.estimated_price_low * 3, rel=1e-6,
        )
        assert pe_3.estimated_price_high == pytest.approx(
            pe_1.estimated_price_high * 3, rel=1e-6,
        )

    def test_confidence_low_large_volume(self):
        pe = estimate_price("FDM", volume_cm3=600.0)
        assert pe.confidence == "low"

    def test_confidence_high_small_volume(self):
        pe = estimate_price("FDM", volume_cm3=30.0)
        assert pe.confidence == "high"

    def test_confidence_medium_mid_volume(self):
        pe = estimate_price("FDM", volume_cm3=200.0)
        assert pe.confidence == "medium"

    def test_zero_dimension_raises(self):
        with pytest.raises(ValueError, match="positive"):
            estimate_price("FDM", dimensions_mm={"x": 0, "y": 50, "z": 20})

    def test_negative_dimension_raises(self):
        with pytest.raises(ValueError, match="positive"):
            estimate_price("FDM", dimensions_mm={"x": -1, "y": 50, "z": 20})

    def test_base_fee_included_fdm(self):
        # FDM base fee is $5. Tiny volume so base fee dominates.
        pe = estimate_price("FDM", volume_cm3=0.01)
        # low = 0.01 * 0.10 + 5.0 = ~5.0
        assert pe.estimated_price_low >= 5.0

    def test_base_fee_included_sla(self):
        # SLA base fee is $10.
        pe = estimate_price("SLA", volume_cm3=0.01)
        assert pe.estimated_price_low >= 10.0

    def test_base_fee_included_dmls(self):
        # DMLS base fee is $50.
        pe = estimate_price("DMLS", volume_cm3=0.01)
        assert pe.estimated_price_low >= 50.0

    def test_technology_case_insensitive(self):
        pe = estimate_price("fdm", volume_cm3=100.0)
        assert pe.technology == "FDM"

    def test_sla_higher_than_fdm(self):
        fdm = estimate_price("FDM", volume_cm3=50.0)
        sla = estimate_price("SLA", volume_cm3=50.0)
        assert sla.estimated_price_low > fdm.estimated_price_low

    def test_to_dict(self):
        pe = estimate_price("FDM", volume_cm3=10.0)
        d = pe.to_dict()
        assert "estimated_price_low" in d
        assert "estimated_price_high" in d
        assert "currency" in d
        assert "technology" in d
        assert "confidence" in d
        assert "note" in d

    def test_note_mentions_technology(self):
        pe = estimate_price("SLS", volume_cm3=50.0)
        assert "SLS" in pe.note


# ---------------------------------------------------------------------------
# OnboardingStep / ConsumerOnboarding dataclasses
# ---------------------------------------------------------------------------


class TestOnboardingDataclasses:
    """to_dict serialization for OnboardingStep and ConsumerOnboarding."""

    def test_step_to_dict(self):
        step = OnboardingStep(
            step=1,
            title="Test",
            description="Desc",
            tool="tool_name",
            example="example()",
        )
        d = step.to_dict()
        assert d["step"] == 1
        assert d["title"] == "Test"
        assert d["tool"] == "tool_name"
        assert d["example"] == "example()"

    def test_onboarding_to_dict(self):
        step = OnboardingStep(
            step=1, title="T", description="D", tool="t", example="e",
        )
        ob = ConsumerOnboarding(steps=[step], summary="Summary")
        d = ob.to_dict()
        assert len(d["steps"]) == 1
        assert isinstance(d["steps"][0], dict)
        assert d["summary"] == "Summary"


# ---------------------------------------------------------------------------
# get_onboarding
# ---------------------------------------------------------------------------


class TestGetOnboarding:
    """Returns the guided onboarding workflow with 7 steps."""

    def test_returns_7_steps(self):
        ob = get_onboarding()
        assert len(ob.steps) == 7

    def test_steps_numbered_sequentially(self):
        ob = get_onboarding()
        for i, step in enumerate(ob.steps, 1):
            assert step.step == i

    def test_summary_is_non_empty(self):
        ob = get_onboarding()
        assert len(ob.summary) > 0

    def test_each_step_has_tool_and_example(self):
        ob = get_onboarding()
        for step in ob.steps:
            assert len(step.tool) > 0
            assert len(step.example) > 0
            assert len(step.title) > 0
            assert len(step.description) > 0

    def test_to_dict(self):
        ob = get_onboarding()
        d = ob.to_dict()
        assert len(d["steps"]) == 7
        assert isinstance(d["steps"][0], dict)
        assert "tool" in d["steps"][0]


# ---------------------------------------------------------------------------
# list_supported_countries
# ---------------------------------------------------------------------------


class TestListSupportedCountries:
    """Returns the dict of supported shipping countries."""

    def test_returns_24_countries(self):
        countries = list_supported_countries()
        assert len(countries) == 24

    def test_includes_us(self):
        countries = list_supported_countries()
        assert "US" in countries
        assert countries["US"] == "United States"

    def test_includes_gb(self):
        countries = list_supported_countries()
        assert "GB" in countries
        assert countries["GB"] == "United Kingdom"

    def test_includes_jp(self):
        countries = list_supported_countries()
        assert "JP" in countries
        assert countries["JP"] == "Japan"

    def test_all_codes_are_two_letter(self):
        countries = list_supported_countries()
        for code in countries:
            assert len(code) == 2
            assert code == code.upper()

    def test_returns_copy_not_reference(self):
        c1 = list_supported_countries()
        c1["XX"] = "Fake"
        c2 = list_supported_countries()
        assert "XX" not in c2
