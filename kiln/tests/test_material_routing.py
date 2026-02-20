"""Tests for material_routing module.

Covers material recommendation by intent, intent parsing, printer
capability filtering, budget filtering, material listing, and edge cases.
"""

from __future__ import annotations

from kiln.material_routing import (
    IntentMapping,
    MaterialProperties,
    MaterialRecommendation,
    get_material,
    list_materials,
    parse_intent,
    recommend_material,
)

# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestMaterialProperties:
    def test_to_dict(self) -> None:
        mat = MaterialProperties(
            name="pla",
            display_name="PLA",
            strength=0.5,
            flexibility=0.2,
            heat_resistance=0.2,
            surface_quality=0.8,
            ease_of_print=0.95,
            cost_per_kg_usd=20,
            typical_hotend_temp=200,
            typical_bed_temp=60,
            requires_enclosure=False,
            requires_heated_bed=False,
            suitable_for=["decorative"],
        )
        d = mat.to_dict()
        assert d["name"] == "pla"
        assert d["strength"] == 0.5
        assert isinstance(d["suitable_for"], list)


class TestMaterialRecommendation:
    def test_to_dict_includes_material(self) -> None:
        mat = get_material("pla")
        assert mat is not None
        rec = MaterialRecommendation(
            material=mat,
            score=85.0,
            reasoning="test",
            settings={"hotend_temp": 200},
            estimated_cost_usd=20.0,
            success_rate=None,
            alternatives=[],
        )
        d = rec.to_dict()
        assert d["material"]["name"] == "pla"
        assert d["score"] == 85.0


class TestIntentMapping:
    def test_to_dict(self) -> None:
        mapping = IntentMapping(
            intent="strong",
            primary_attribute="strength",
            weights={"strength": 0.5},
        )
        d = mapping.to_dict()
        assert d["intent"] == "strong"
        assert d["weights"]["strength"] == 0.5


# ---------------------------------------------------------------------------
# parse_intent
# ---------------------------------------------------------------------------


class TestParseIntent:
    def test_exact_match(self) -> None:
        mapping = parse_intent("strong")
        assert mapping.intent == "strong"
        assert mapping.primary_attribute == "strength"

    def test_substring_match(self) -> None:
        mapping = parse_intent("make it strong and durable")
        # "strong" appears first in iteration, should match
        assert mapping.intent in ("strong", "durable")

    def test_pretty_intent(self) -> None:
        mapping = parse_intent("make it pretty")
        assert mapping.intent == "pretty"
        assert mapping.primary_attribute == "surface_quality"

    def test_cheap_intent(self) -> None:
        mapping = parse_intent("cheap option please")
        assert mapping.intent == "cheap"

    def test_flexible_intent(self) -> None:
        mapping = parse_intent("I need something flexible")
        assert mapping.intent == "flexible"

    def test_outdoor_intent(self) -> None:
        mapping = parse_intent("outdoor use")
        assert mapping.intent == "outdoor"

    def test_easy_intent(self) -> None:
        mapping = parse_intent("easy to print")
        assert mapping.intent == "easy"

    def test_food_safe_intent(self) -> None:
        mapping = parse_intent("food_safe material")
        assert mapping.intent == "food_safe"

    def test_durable_intent(self) -> None:
        mapping = parse_intent("needs to be durable")
        assert mapping.intent == "durable"

    def test_unknown_intent_returns_balanced(self) -> None:
        mapping = parse_intent("something magical")
        assert mapping.intent == "balanced"

    def test_case_insensitive(self) -> None:
        mapping = parse_intent("STRONG material")
        assert mapping.intent == "strong"

    def test_empty_string(self) -> None:
        mapping = parse_intent("")
        assert mapping.intent == "balanced"


# ---------------------------------------------------------------------------
# recommend_material
# ---------------------------------------------------------------------------


class TestRecommendMaterial:
    def test_strong_recommends_high_strength(self) -> None:
        rec = recommend_material("strong")
        assert rec.material.strength >= 0.7
        assert rec.score > 0

    def test_pretty_recommends_high_surface_quality(self) -> None:
        rec = recommend_material("pretty")
        assert rec.material.surface_quality >= 0.6

    def test_cheap_recommends_low_cost(self) -> None:
        rec = recommend_material("cheap")
        assert rec.material.cost_per_kg_usd <= 25

    def test_flexible_recommends_tpu(self) -> None:
        rec = recommend_material("flexible")
        assert rec.material.name == "tpu"

    def test_easy_recommends_easy_material(self) -> None:
        rec = recommend_material("easy")
        assert rec.material.ease_of_print >= 0.8

    def test_outdoor_recommends_heat_resistant(self) -> None:
        rec = recommend_material("outdoor")
        assert rec.material.heat_resistance >= 0.5

    def test_recommendation_has_settings(self) -> None:
        rec = recommend_material("strong")
        assert "hotend_temp" in rec.settings
        assert "bed_temp" in rec.settings
        assert "layer_height" in rec.settings

    def test_recommendation_has_alternatives(self) -> None:
        rec = recommend_material("strong")
        assert isinstance(rec.alternatives, list)

    def test_reasoning_present(self) -> None:
        rec = recommend_material("strong")
        assert len(rec.reasoning) > 0

    def test_estimated_cost(self) -> None:
        rec = recommend_material("cheap")
        assert rec.estimated_cost_usd is not None
        assert rec.estimated_cost_usd > 0


# ---------------------------------------------------------------------------
# Printer capability filtering
# ---------------------------------------------------------------------------


class TestPrinterCapabilityFiltering:
    def test_no_enclosure_filters_enclosure_materials(self) -> None:
        rec = recommend_material(
            "strong",
            printer_capabilities={"has_enclosure": False, "has_heated_bed": True},
        )
        assert rec.material.requires_enclosure is False

    def test_no_heated_bed_filters_heated_bed_materials(self) -> None:
        rec = recommend_material(
            "strong",
            printer_capabilities={"has_enclosure": False, "has_heated_bed": False},
        )
        assert rec.material.requires_heated_bed is False

    def test_full_capabilities_allows_all(self) -> None:
        rec = recommend_material(
            "strong",
            printer_capabilities={"has_enclosure": True, "has_heated_bed": True},
        )
        # Should be able to recommend PC or nylon (high strength, needs enclosure)
        assert rec.material.strength >= 0.7

    def test_no_capabilities_defaults_to_safe(self) -> None:
        rec = recommend_material(
            "strong",
            printer_capabilities={"has_enclosure": False, "has_heated_bed": False},
        )
        # Should still return something printable
        assert rec.material is not None
        assert rec.score > 0


# ---------------------------------------------------------------------------
# Budget filtering
# ---------------------------------------------------------------------------


class TestBudgetFiltering:
    def test_budget_filters_expensive_materials(self) -> None:
        rec = recommend_material("strong", budget_usd=21)
        assert rec.material.cost_per_kg_usd <= 21

    def test_very_low_budget(self) -> None:
        rec = recommend_material("strong", budget_usd=20)
        assert rec.material.cost_per_kg_usd <= 20

    def test_high_budget_allows_all(self) -> None:
        rec = recommend_material("strong", budget_usd=100)
        # With high budget, should get the strongest material
        assert rec.material.strength >= 0.7

    def test_impossible_budget_falls_back(self) -> None:
        # Budget so low nothing qualifies — should still return something
        rec = recommend_material("strong", budget_usd=1)
        # Falls back to unfiltered list
        assert rec.material is not None


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestListMaterials:
    def test_returns_all_materials(self) -> None:
        materials = list_materials()
        assert len(materials) == 8

    def test_sorted_by_name(self) -> None:
        materials = list_materials()
        names = [m.name for m in materials]
        assert names == sorted(names)

    def test_all_have_required_fields(self) -> None:
        for mat in list_materials():
            assert mat.name
            assert mat.display_name
            assert 0 <= mat.strength <= 1
            assert 0 <= mat.flexibility <= 1
            assert mat.typical_hotend_temp > 0
            assert mat.cost_per_kg_usd > 0


# ---------------------------------------------------------------------------
# get_material
# ---------------------------------------------------------------------------


class TestGetMaterial:
    def test_existing_material(self) -> None:
        mat = get_material("pla")
        assert mat is not None
        assert mat.display_name == "PLA"

    def test_case_insensitive(self) -> None:
        mat = get_material("PLA")
        assert mat is not None

    def test_nonexistent_material(self) -> None:
        mat = get_material("unobtanium")
        assert mat is None

    def test_all_materials_accessible(self) -> None:
        for name in ("pla", "petg", "abs", "tpu", "asa", "nylon", "pc", "pla_plus"):
            mat = get_material(name)
            assert mat is not None, f"Material {name} not found"
