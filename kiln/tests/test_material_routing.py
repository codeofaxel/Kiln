"""Tests for material_routing module.

Covers material recommendation by intent, intent parsing, printer
capability filtering, budget filtering, material listing, and edge cases.
"""

from __future__ import annotations

from kiln.material_routing import (
    IntentMapping,
    MaterialProperties,
    MaterialRecommendation,
    _catalog_match,
    get_material,
    list_materials,
    parse_intent,
    recommend_material,
)


def _on_hand_entry(
    material_type: str,
    *,
    loaded_on: list[dict] | None = None,
    shelf_spools: list[dict] | None = None,
) -> dict:
    """Build an on-hand entry in the OnHandMaterial.to_dict() shape."""
    return {
        "material_type": material_type,
        "loaded_on": loaded_on or [],
        "shelf_spools": shelf_spools or [],
        "total_grams": sum(
            (r.get("remaining_grams") or 0.0)
            for r in (loaded_on or []) + (shelf_spools or [])
        ),
    }


def _loaded_row(printer_name: str, grams: float = 500.0, color: str = "black") -> dict:
    return {
        "printer_name": printer_name,
        "tool_index": 0,
        "remaining_grams": grams,
        "color": color,
        "spool_id": None,
    }


def _shelf_row(grams: float = 500.0, brand: str = "Generic") -> dict:
    return {
        "spool_id": "sp-test",
        "brand": brand,
        "color": "black",
        "remaining_grams": grams,
    }

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

    def test_wearable_prepends_skin_contact_advisory(self) -> None:
        rec = recommend_material("a bracelet worn on the wrist")
        assert "SKIN CONTACT" in rec.reasoning
        assert "skin-safe" in rec.reasoning.lower()
        assert "medical" in rec.reasoning.lower()

    def test_non_wearable_has_no_skin_advisory(self) -> None:
        rec = recommend_material("strong")
        assert "SKIN CONTACT" not in rec.reasoning

    def test_skin_advisory_warns_never_filters(self) -> None:
        # warn-don't-block: a worn intent must still return a real material,
        # never drop candidates the way the food-safe filter does.
        rec = recommend_material("a ring worn daily")
        assert rec.material is not None
        assert rec.score > 0

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


# ---------------------------------------------------------------------------
# Catalog matching (inventory string -> catalog name)
# ---------------------------------------------------------------------------


class TestCatalogMatch:
    def test_exact_names(self) -> None:
        assert _catalog_match("PETG") == "petg"
        assert _catalog_match("pla") == "pla"

    def test_filled_variant_matches_base_family(self) -> None:
        assert _catalog_match("PETG-CF") == "petg"
        assert _catalog_match("ABS-GF") == "abs"

    def test_aliases(self) -> None:
        assert _catalog_match("PLA+") == "pla_plus"
        assert _catalog_match("PA") == "nylon"
        assert _catalog_match("PA-CF") == "nylon"
        assert _catalog_match("Polycarbonate") == "pc"

    def test_unknown_returns_none(self) -> None:
        assert _catalog_match("PVA") is None
        assert _catalog_match("") is None


# ---------------------------------------------------------------------------
# On-hand narrowing
# ---------------------------------------------------------------------------


class TestOnHandRecommendation:
    """On-hand narrowing: answer from what the user physically has, name
    the machine holding it, and label honest fallbacks."""

    def test_narrows_to_loaded_materials_and_names_machine(self) -> None:
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("a1-right", 900.0)]),
            _on_hand_entry("PETG", loaded_on=[_loaded_row("a1-left", 800.0)]),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        assert rec.material.name == "petg"
        assert rec.availability is not None
        assert rec.availability["status"] == "loaded"
        printers = [r["printer_name"] for r in rec.availability["loaded_on"]]
        assert printers == ["a1-left"]
        assert "ON HAND" in rec.reasoning
        assert "a1-left" in rec.reasoning

    def test_variant_string_survives_to_attribution(self) -> None:
        on_hand = [
            _on_hand_entry("PETG-CF", loaded_on=[_loaded_row("x1c", 750.0)]),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        assert rec.material.name == "petg"
        assert rec.availability["as_recorded"] == ["PETG-CF"]
        assert "PETG-CF" in rec.reasoning

    def test_shelf_only_flags_swap(self) -> None:
        on_hand = [
            _on_hand_entry("PETG", shelf_spools=[_shelf_row(600.0, "Polymaker")]),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        assert rec.availability["status"] == "on_shelf"
        assert rec.availability["swap_needed"] is True
        assert "swap" in rec.reasoning.lower()

    def test_no_catalog_match_labels_needs_purchase(self) -> None:
        on_hand = [
            _on_hand_entry("PVA", loaded_on=[_loaded_row("ender3", 400.0)]),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        # Honest fallback: full-catalog answer, clearly labeled.
        assert rec.availability["status"] == "needs_purchase"
        assert rec.availability["on_hand_recorded"] == ["PVA"]
        assert rec.reasoning.startswith("NOT ON HAND")
        assert rec.material.name  # a real catalog pick is still returned

    def test_empty_inventory_is_honest(self) -> None:
        rec = recommend_material("strong", on_hand=[])
        assert rec.availability["status"] == "no_inventory_recorded"
        assert "add_spool" in rec.reasoning
        assert rec.reasoning.startswith("NOT ON HAND")

    def test_none_keeps_catalog_behavior(self) -> None:
        rec = recommend_material("strong")
        assert rec.availability is None
        assert "ON HAND" not in rec.reasoning

    def test_entry_without_physical_rows_confers_no_candidacy(self) -> None:
        # An entry with neither a loaded row nor a shelf spool carries no
        # physical material — it must not narrow candidacy.
        on_hand = [_on_hand_entry("PETG")]
        rec = recommend_material("strong", on_hand=on_hand)
        assert rec.availability["status"] == "needs_purchase"

    def test_alternatives_carry_availability(self) -> None:
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("a1-right", 900.0)]),
            _on_hand_entry("PETG", loaded_on=[_loaded_row("a1-left", 800.0)]),
            _on_hand_entry("TPU", shelf_spools=[_shelf_row(300.0)]),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        assert rec.alternatives, "expected at least one alternative"
        for alt in rec.alternatives:
            assert alt["availability"]["status"] in ("loaded", "on_shelf")

    def test_intent_still_ranks_within_on_hand(self) -> None:
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("m1", 900.0)]),
            _on_hand_entry("TPU", loaded_on=[_loaded_row("m2", 400.0)]),
        ]
        rec = recommend_material("flexible", on_hand=on_hand)
        assert rec.material.name == "tpu"

    def test_multiple_machines_ordered_by_stock(self) -> None:
        """When two machines hold the material, the fullest is named
        first — matching find_printers_with_material ordering."""
        on_hand = [
            _on_hand_entry(
                "PETG",
                loaded_on=[
                    _loaded_row("low-machine", 100.0),
                    _loaded_row("full-machine", 900.0),
                ],
            ),
        ]
        rec = recommend_material("strong", on_hand=on_hand)
        printers = [r["printer_name"] for r in rec.availability["loaded_on"]]
        assert printers == ["full-machine", "low-machine"]

    def test_abrasive_consult_uses_recorded_variant(self, monkeypatch) -> None:
        """The nozzle bridge must be asked about the material the user
        will ACTUALLY print (PETG-CF), not the catalog base (petg)."""
        from kiln import _pro_nozzle_bridge

        consulted: dict = {}

        def _fake_consult(*, filament_material: str, printer_id: str):
            consulted["filament"] = filament_material
            return None

        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation", _fake_consult
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary", lambda _pid: None
        )
        on_hand = [
            _on_hand_entry("PETG-CF", loaded_on=[_loaded_row("x1c", 750.0)]),
        ]
        recommend_material("strong", printer_id="x1c", on_hand=on_hand)
        assert consulted["filament"] == "PETG-CF"

    def test_poor_fit_names_the_material_you_lack(self) -> None:
        """Best-of-a-poor-shelf must not pass as right-for-the-job: when
        an unowned material fits the intent materially better, say so."""
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("x1c", 940.0)]),
        ]
        rec = recommend_material("flexible phone case", on_hand=on_hand)
        # Still recommends what they actually have...
        assert rec.material.name == "pla"
        assert rec.availability["status"] == "loaded"
        # ...but names TPU as the materially better fit they lack.
        better = rec.availability["better_catalog_option"]
        assert better["name"] == "tpu"
        assert better["score_gap"] >= 10.0
        assert "POOR FIT ON HAND" in rec.reasoning
        assert "TPU" in rec.reasoning

    def test_good_fit_stays_quiet(self) -> None:
        """No poor-fit noise when the on-hand pick IS the best pick."""
        on_hand = [
            _on_hand_entry("TPU", loaded_on=[_loaded_row("x1c", 500.0)]),
            _on_hand_entry("PLA", loaded_on=[_loaded_row("x1c", 940.0)]),
        ]
        rec = recommend_material("flexible", on_hand=on_hand)
        assert rec.material.name == "tpu"
        assert "better_catalog_option" not in rec.availability
        assert "POOR FIT" not in rec.reasoning

    def test_better_option_respects_printer_capability(self) -> None:
        """Never name a material the printer cannot run: PC/nylon need an
        enclosure, so an open-frame printer must not be told to buy them."""
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("a1", 940.0)]),
        ]
        rec = recommend_material(
            "strong",
            printer_capabilities={"has_enclosure": False, "has_heated_bed": True},
            on_hand=on_hand,
        )
        better = rec.availability.get("better_catalog_option")
        if better is not None:
            suggested = get_material(better["name"])
            assert suggested is not None
            assert suggested.requires_enclosure is False

    def test_better_option_respects_budget(self) -> None:
        """A material priced out by the budget is not a 'better option'."""
        on_hand = [
            _on_hand_entry("PLA", loaded_on=[_loaded_row("x1c", 940.0)]),
        ]
        rec = recommend_material(
            "flexible", budget_usd=25.0, on_hand=on_hand
        )
        better = rec.availability.get("better_catalog_option")
        if better is not None:
            suggested = get_material(better["name"])
            assert suggested.cost_per_kg_usd <= 25.0
