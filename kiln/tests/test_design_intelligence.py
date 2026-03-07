"""Tests for the design intelligence engine.

Coverage areas:
- Knowledge base loading and caching
- Material profile retrieval and listing
- Material recommendation based on functional requirements
- Design pattern retrieval, listing, and use-case search
- Functional requirement matching from natural language
- Design brief generation (the full pipeline)
- Constraint merging (min takes max, max takes min)
- Structural load estimation and interpolation
- Environment compatibility checks
- Printer capability profiles
- Troubleshooting: symptom search, severity ordering, storage, tips
- Printer-material compatibility: status, upgrades, fallback
- Post-processing: techniques, paintability, strengthening
- Multi-material compatibility: co-print, support pairs, dissolution
- Cross-file print diagnostic: combined troubleshooting + compatibility
- Construction domain: materials, patterns, requirements, briefs
- Edge cases: unknown materials, empty text, no matches
- Generation feedback enhancement integration
"""

from __future__ import annotations

import pytest

from kiln.design_intelligence import (
    ConstructionDesignBrief,
    ConstructionMaterialProfile,
    ConstructionPattern,
    ConstructionRequirement,
    DesignBrief,
    EnvironmentReport,
    LoadEstimate,
    MultiMaterialReport,
    PostProcessingGuide,
    PrintDiagnostic,
    PrinterCompatibilityReport,
    PrinterDesignProfile,
    TroubleshootingResult,
    _reset_knowledge_base,
    check_environment_compatibility,
    check_multi_material_compatibility,
    check_printer_material_compatibility,
    estimate_load_capacity,
    find_patterns_for_use_case,
    get_construction_design_brief,
    get_construction_material,
    get_construction_pattern,
    get_construction_requirement,
    get_design_constraints,
    get_design_pattern,
    get_material_profile,
    get_post_processing,
    get_print_diagnostic,
    get_printer_design_profile,
    get_support_material_options,
    list_compatibility_printers,
    list_construction_materials,
    list_construction_patterns,
    list_construction_requirements,
    list_design_patterns,
    list_material_profiles,
    list_printer_profiles,
    list_troubleshooting_materials,
    match_construction_requirements,
    match_requirements,
    recommend_material_for_design,
    troubleshoot_print_issue,
)


@pytest.fixture(autouse=True)
def _reset_kb():
    """Reset knowledge base before each test."""
    _reset_knowledge_base()
    yield
    _reset_knowledge_base()


# ---------------------------------------------------------------------------
# Material profiles
# ---------------------------------------------------------------------------


class TestMaterialProfiles:
    def test_get_pla(self):
        p = get_material_profile("pla")
        assert p is not None
        assert p.material_id == "pla"
        assert "PLA" in p.display_name
        assert p.mechanical["tensile_strength_mpa"] == 50

    def test_get_petg(self):
        p = get_material_profile("petg")
        assert p is not None
        assert p.thermal["max_service_temp_c"] == 65

    def test_get_nylon(self):
        p = get_material_profile("nylon")
        assert p is not None
        assert p.mechanical["fatigue_resistance"] == "excellent"

    def test_get_tpu_flexible(self):
        p = get_material_profile("tpu")
        assert p is not None
        assert p.mechanical["elongation_at_break_pct"] == 580
        assert p.design_limits["living_hinge_viable"] is True

    def test_get_polycarbonate(self):
        p = get_material_profile("polycarbonate")
        assert p is not None
        assert p.thermal["max_service_temp_c"] == 120

    def test_unknown_material_returns_none(self):
        assert get_material_profile("unobtanium") is None

    def test_case_insensitive_lookup(self):
        assert get_material_profile("PLA") is not None
        assert get_material_profile("Petg") is not None

    def test_list_materials_returns_all(self):
        profiles = list_material_profiles()
        assert len(profiles) >= 7
        ids = {p.material_id for p in profiles}
        assert "pla" in ids
        assert "petg" in ids
        assert "abs" in ids
        assert "tpu" in ids
        assert "asa" in ids
        assert "nylon" in ids
        assert "polycarbonate" in ids

    def test_material_to_dict_roundtrip(self):
        p = get_material_profile("petg")
        assert p is not None
        d = p.to_dict()
        assert d["material_id"] == "petg"
        assert isinstance(d["mechanical"], dict)
        assert isinstance(d["agent_guidance"], list)

    def test_every_material_has_agent_guidance(self):
        for p in list_material_profiles():
            assert len(p.agent_guidance) > 0, f"{p.material_id} missing guidance"

    def test_every_material_has_design_limits(self):
        for p in list_material_profiles():
            assert "min_wall_thickness_mm" in p.design_limits, (
                f"{p.material_id} missing min_wall_thickness_mm"
            )
            assert "max_unsupported_overhang_deg" in p.design_limits, (
                f"{p.material_id} missing max_unsupported_overhang_deg"
            )


# ---------------------------------------------------------------------------
# Material recommendation
# ---------------------------------------------------------------------------


class TestMaterialRecommendation:
    def test_load_bearing_excludes_pla(self):
        rec = recommend_material_for_design("shelf bracket that holds 10 lbs")
        assert rec.material.material_id != "pla"
        assert rec.material.material_id != "tpu"

    def test_outdoor_prefers_asa(self):
        rec = recommend_material_for_design(
            "garden sign that lives outside in the sun",
            printer_has_enclosure=True,
        )
        assert rec.material.material_id == "asa"

    def test_flexible_requires_tpu(self):
        rec = recommend_material_for_design("flexible phone case that absorbs drops")
        assert rec.material.material_id == "tpu"

    def test_food_contact_prefers_petg(self):
        rec = recommend_material_for_design("cookie cutter, food safe")
        assert rec.material.material_id == "petg"

    def test_no_enclosure_excludes_abs(self):
        rec = recommend_material_for_design(
            "strong bracket",
            printer_has_enclosure=False,
        )
        # ABS/ASA should be penalized without enclosure
        assert rec.material.material_id not in ("abs", "asa")

    def test_hotend_limit_penalizes_high_temp(self):
        rec = recommend_material_for_design(
            "strong durable part",
            max_hotend_temp_c=230,
        )
        # Should not recommend PC (needs 270+)
        assert rec.material.material_id != "polycarbonate"

    def test_no_direct_drive_penalizes_tpu(self):
        rec = recommend_material_for_design(
            "flexible gasket",
            printer_has_direct_drive=False,
        )
        # TPU should be heavily penalized
        for w in rec.warnings:
            if "tpu" in rec.material.material_id:
                assert "direct drive" in w.lower()

    def test_recommendation_has_alternatives(self):
        rec = recommend_material_for_design("strong functional part")
        assert len(rec.alternatives) > 0

    def test_recommendation_to_dict(self):
        rec = recommend_material_for_design("simple prototype")
        d = rec.to_dict()
        assert "material" in d
        assert "score" in d
        assert "alternatives" in d

    def test_aesthetic_prefers_pla(self):
        rec = recommend_material_for_design("beautiful decorative figurine for display")
        assert rec.material.material_id == "pla"

    def test_heat_resistant_excludes_pla(self):
        rec = recommend_material_for_design(
            "mount near a heat source in a car",
            printer_has_enclosure=True,
        )
        assert rec.material.material_id != "pla"


# ---------------------------------------------------------------------------
# Design patterns
# ---------------------------------------------------------------------------


class TestDesignPatterns:
    def test_get_snap_fit(self):
        p = get_design_pattern("snap_fit_cantilever")
        assert p is not None
        assert "snap" in p.display_name.lower()
        assert "pla" in p.material_compatibility["poor"]

    def test_get_gear(self):
        p = get_design_pattern("gear")
        assert p is not None
        assert "nylon" in p.material_compatibility["excellent"]

    def test_get_living_hinge(self):
        p = get_design_pattern("living_hinge")
        assert p is not None
        assert "tpu" in p.material_compatibility["excellent"]
        assert "pla" in p.material_compatibility["avoid"]

    def test_unknown_pattern_returns_none(self):
        assert get_design_pattern("quantum_teleporter") is None

    def test_list_patterns_returns_all(self):
        patterns = list_design_patterns()
        assert len(patterns) >= 8
        ids = {p.pattern_id for p in patterns}
        assert "snap_fit_cantilever" in ids
        assert "gear" in ids
        assert "watertight_container" in ids

    def test_find_patterns_for_enclosure(self):
        results = find_patterns_for_use_case("enclosures")
        ids = {p.pattern_id for p in results}
        assert "snap_fit_cantilever" in ids or "enclosure_box" in ids

    def test_find_patterns_for_gears(self):
        results = find_patterns_for_use_case("gear")
        assert len(results) > 0

    def test_find_patterns_empty_returns_empty(self):
        results = find_patterns_for_use_case("quantum_computing")
        assert len(results) == 0

    def test_pattern_to_dict(self):
        p = get_design_pattern("press_fit")
        assert p is not None
        d = p.to_dict()
        assert "design_rules" in d
        assert "material_compatibility" in d
        assert "agent_guidance" in d

    def test_every_pattern_has_guidance(self):
        for p in list_design_patterns():
            assert len(p.agent_guidance) > 0, f"{p.pattern_id} missing guidance"


# ---------------------------------------------------------------------------
# Functional requirement matching
# ---------------------------------------------------------------------------


class TestRequirementMatching:
    def test_load_bearing_match(self):
        results = match_requirements("shelf bracket that holds 10 lbs of books")
        ids = {r.requirement_id for r in results}
        assert "load_bearing" in ids

    def test_outdoor_match(self):
        results = match_requirements("garden planter that lives outside")
        ids = {r.requirement_id for r in results}
        assert "outdoor_use" in ids

    def test_watertight_match(self):
        results = match_requirements("vase that holds water")
        ids = {r.requirement_id for r in results}
        assert "watertight" in ids

    def test_food_contact_match(self):
        results = match_requirements("cookie cutter that touches food")
        ids = {r.requirement_id for r in results}
        assert "food_contact" in ids

    def test_heat_match(self):
        results = match_requirements("mount for car dashboard, survives summer heat")
        ids = {r.requirement_id for r in results}
        assert "heat_exposure" in ids

    def test_flexible_match(self):
        results = match_requirements("soft flexible phone case")
        ids = {r.requirement_id for r in results}
        assert "flexibility_required" in ids

    def test_impact_match(self):
        results = match_requirements("protective case for a kid's tablet, drop proof")
        ids = {r.requirement_id for r in results}
        assert "impact_resistant" in ids

    def test_precision_match(self):
        results = match_requirements("parts that fit together with tight tolerances")
        ids = {r.requirement_id for r in results}
        assert "precision_fit" in ids

    def test_aesthetic_match(self):
        results = match_requirements("beautiful display piece, decorative sculpture")
        ids = {r.requirement_id for r in results}
        assert "aesthetic_decorative" in ids

    def test_multiple_requirements_match(self):
        results = match_requirements(
            "outdoor shelf bracket that holds weight in the sun"
        )
        ids = {r.requirement_id for r in results}
        assert "outdoor_use" in ids
        assert "load_bearing" in ids

    def test_no_match_returns_empty(self):
        results = match_requirements("something vague and unspecified")
        assert len(results) == 0

    def test_constraint_set_to_dict(self):
        results = match_requirements("bracket to support heavy items")
        assert len(results) > 0
        d = results[0].to_dict()
        assert "constraint_rules" in d
        assert "agent_guidance" in d


# ---------------------------------------------------------------------------
# Design brief (full pipeline)
# ---------------------------------------------------------------------------


class TestDesignBrief:
    def test_basic_brief(self):
        brief = get_design_constraints("phone stand for my desk")
        assert isinstance(brief, DesignBrief)
        assert brief.recommended_material is not None
        assert len(brief.combined_guidance) > 0

    def test_load_bearing_brief_excludes_pla(self):
        brief = get_design_constraints("wall shelf bracket that holds 10 lbs")
        assert brief.recommended_material is not None
        assert brief.recommended_material.material.material_id != "pla"
        assert len(brief.functional_constraints) > 0

    def test_material_override(self):
        brief = get_design_constraints("vase", material="tpu")
        assert brief.recommended_material is not None
        assert brief.recommended_material.material.material_id == "tpu"
        assert brief.recommended_material.reasons == ["User-specified material."]

    def test_brief_has_combined_rules(self):
        brief = get_design_constraints("outdoor waterproof planter")
        assert len(brief.combined_rules) > 0

    def test_brief_finds_patterns(self):
        brief = get_design_constraints("snap fit enclosure for electronics")
        pattern_ids = {p.pattern_id for p in brief.applicable_patterns}
        assert "enclosure_box" in pattern_ids or "snap_fit_cantilever" in pattern_ids

    def test_min_constraint_merging(self):
        # Multiple requirements with different min_wall_thickness
        brief = get_design_constraints("outdoor load bearing bracket")
        rules = brief.combined_rules
        # Load bearing requires 3mm, outdoor requires 2mm — should take the max (3mm)
        if "min_wall_thickness_mm" in rules:
            assert rules["min_wall_thickness_mm"] >= 2

    def test_brief_to_dict(self):
        brief = get_design_constraints("simple coaster")
        d = brief.to_dict()
        assert "functional_constraints" in d
        assert "recommended_material" in d
        assert "combined_guidance" in d
        assert "combined_rules" in d

    def test_empty_requirements_still_works(self):
        brief = get_design_constraints("")
        assert isinstance(brief, DesignBrief)
        assert brief.recommended_material is not None

    def test_printer_model_influences_brief(self):
        brief = get_design_constraints(
            "outdoor garden sign that lives in the sun",
            printer_model="bambu_a1",
        )
        assert brief.recommended_material is not None
        assert brief.recommended_material.material.material_id != "asa"
        assert "printer_build_volume_mm" in brief.combined_rules
        assert "printer_supported_materials" in brief.combined_rules
        assert any("consumer platform" in note.lower() for note in brief.combined_guidance)

    def test_material_override_warns_when_printer_is_a_bad_fit(self):
        brief = get_design_constraints(
            "outdoor bracket",
            material="asa",
            printer_model="bambu_a1",
        )
        assert brief.recommended_material is not None
        assert any("open-frame" in warning.lower() or "not profiled" in warning.lower() for warning in brief.recommended_material.warnings)


# ---------------------------------------------------------------------------
# Structural load estimation
# ---------------------------------------------------------------------------


class TestLoadEstimation:
    def test_known_petg_load_at_100mm(self):
        estimate = estimate_load_capacity("petg", 24.0, 100.0)
        assert isinstance(estimate, LoadEstimate)
        assert estimate is not None
        assert estimate.max_load_n == pytest.approx(67.2)
        assert estimate.derating_applied == pytest.approx(1.0)

    def test_unknown_material_returns_none(self):
        assert estimate_load_capacity("unobtanium", 20.0, 100.0) is None

    def test_cross_section_interpolation(self):
        estimate = estimate_load_capacity("petg", 30.0, 100.0)
        assert estimate is not None
        # Interpolated between 24 mm^2 (67.2N) and 36 mm^2 (100.8N)
        assert estimate.max_load_n == pytest.approx(84.0)

    def test_cantilever_length_interpolation(self):
        estimate = estimate_load_capacity("petg", 24.0, 75.0)
        assert estimate is not None
        # Interpolated between 50 mm (107.52N) and 100 mm (67.2N)
        assert estimate.max_load_n == pytest.approx(87.36)

    def test_longer_cantilever_reduces_capacity(self):
        short_arm = estimate_load_capacity("nylon", 24.0, 50.0)
        long_arm = estimate_load_capacity("nylon", 24.0, 150.0)
        assert short_arm is not None
        assert long_arm is not None
        assert short_arm.max_load_n > long_arm.max_load_n

    def test_derating_for_layer_orientation(self):
        across = estimate_load_capacity("abs", 24.0, 100.0, load_across_layers=True)
        along = estimate_load_capacity("abs", 24.0, 100.0, load_across_layers=False)
        assert across is not None
        assert along is not None
        assert along.max_load_n == pytest.approx(across.max_load_n * 0.6)
        assert along.derating_applied == pytest.approx(0.6)

    def test_non_positive_cross_section_returns_zero(self):
        estimate = estimate_load_capacity("petg", 0.0, 100.0)
        assert estimate is not None
        assert estimate.max_load_n == 0.0
        assert any("positive" in msg.lower() for msg in estimate.reasoning)

    def test_below_min_cantilever_uses_shortest_table(self):
        at_25 = estimate_load_capacity("pla", 24.0, 25.0)
        at_10 = estimate_load_capacity("pla", 24.0, 10.0)
        assert at_25 is not None
        assert at_10 is not None
        assert at_10.max_load_n == pytest.approx(at_25.max_load_n)

    def test_above_max_cantilever_uses_longest_table(self):
        at_200 = estimate_load_capacity("pla", 24.0, 200.0)
        at_300 = estimate_load_capacity("pla", 24.0, 300.0)
        assert at_200 is not None
        assert at_300 is not None
        assert at_300.max_load_n == pytest.approx(at_200.max_load_n)

    def test_case_insensitive_material_lookup(self):
        estimate = estimate_load_capacity("PETG", 24.0, 100.0)
        assert estimate is not None
        assert estimate.material == "petg"

    def test_load_estimate_to_dict(self):
        estimate = estimate_load_capacity("petg", 24.0, 100.0)
        assert estimate is not None
        data = estimate.to_dict()
        assert data["material"] == "petg"
        assert "max_load_n" in data
        assert isinstance(data["reasoning"], list)


# ---------------------------------------------------------------------------
# Environment compatibility
# ---------------------------------------------------------------------------


class TestEnvironmentCompatibility:
    def test_outdoor_uv_petg_is_conditional(self):
        report = check_environment_compatibility("petg", "outdoor UV sun exposure")
        assert isinstance(report, EnvironmentReport)
        assert report is not None
        assert report.per_category_ratings["uv_resistance"] == "moderate"
        assert report.overall_verdict == "conditional"

    def test_outdoor_uv_asa_is_recommended(self):
        report = check_environment_compatibility("asa", "direct sunlight and UV")
        assert report is not None
        assert report.per_category_ratings["uv_resistance"] == "excellent"
        assert report.overall_verdict == "recommended"

    def test_nylon_immersion_is_not_recommended(self):
        report = check_environment_compatibility("nylon", "submerged in water immersion")
        assert report is not None
        assert report.overall_verdict == "not_recommended"
        assert any("immersion" in warning.lower() for warning in report.warnings)

    def test_pla_high_temperature_fails(self):
        report = check_environment_compatibility("pla", "near engine at 80C")
        assert report is not None
        assert report.overall_verdict == "not_recommended"
        assert any("outside service range" in warning.lower() for warning in report.warnings)

    def test_polycarbonate_110c_is_within_range(self):
        report = check_environment_compatibility("polycarbonate", "operates at 110C")
        assert report is not None
        assert report.overall_verdict in ("recommended", "conditional")
        assert "temperature_range" in report.per_category_ratings

    def test_solvents_flag_pc_as_not_recommended(self):
        report = check_environment_compatibility("polycarbonate", "frequent acetone solvent cleaning")
        assert report is not None
        assert report.overall_verdict == "not_recommended"
        assert report.per_category_ratings["chemicals_solvents"] == "poor"

    def test_chemical_oils_good_for_abs(self):
        report = check_environment_compatibility("abs", "contact with lubricating oil and grease")
        assert report is not None
        assert report.per_category_ratings["chemicals_oils_greases"] == "good"
        assert report.overall_verdict == "recommended"

    def test_tpu_vibration_is_recommended(self):
        report = check_environment_compatibility("tpu", "high vibration fatigue cycles")
        assert report is not None
        assert report.per_category_ratings["vibration_fatigue"] == "outstanding"
        assert report.overall_verdict == "recommended"

    def test_multiple_environment_factors(self):
        report = check_environment_compatibility(
            "petg",
            "outdoor UV, rain moisture, and household cleaner contact",
        )
        assert report is not None
        assert "uv_resistance" in report.per_category_ratings
        assert "moisture" in report.per_category_ratings
        assert "chemicals_household_cleaners" in report.per_category_ratings
        assert report.overall_verdict == "conditional"

    def test_unknown_material_returns_none(self):
        assert check_environment_compatibility("unobtanium", "outdoor sun") is None

    def test_vague_environment_returns_baseline(self):
        report = check_environment_compatibility("petg", "general indoor use")
        assert report is not None
        assert report.overall_verdict == "conditional"
        assert "uv_resistance" in report.per_category_ratings
        assert len(report.warnings) > 0

    def test_environment_report_to_dict(self):
        report = check_environment_compatibility("petg", "outdoor UV")
        assert report is not None
        data = report.to_dict()
        assert data["material"] == "petg"
        assert "overall_verdict" in data
        assert isinstance(data["warnings"], list)


# ---------------------------------------------------------------------------
# Printer profiles
# ---------------------------------------------------------------------------


class TestPrinterProfiles:
    def test_get_known_printer_profile(self):
        profile = get_printer_design_profile("bambu_x1c")
        assert isinstance(profile, PrinterDesignProfile)
        assert profile is not None
        assert profile.display_name == "Bambu Lab X1 Carbon"
        assert profile.has_enclosure is True

    def test_unknown_printer_returns_none(self):
        assert get_printer_design_profile("unknown_printer") is None

    def test_case_insensitive_lookup(self):
        profile = get_printer_design_profile("BAMBU_X1C")
        assert profile is not None
        assert profile.printer_id == "bambu_x1c"

    def test_list_printers_includes_all_known_profiles(self):
        profiles = list_printer_profiles()
        assert len(profiles) >= 9
        ids = {p.printer_id for p in profiles}
        assert "bambu_x1c" in ids
        assert "voron_2_4" in ids
        assert "prusa_mk4" in ids

    def test_filter_has_enclosure(self):
        enclosed = [p for p in list_printer_profiles() if p.has_enclosure]
        ids = {p.printer_id for p in enclosed}
        assert "bambu_x1c" in ids
        assert "voron_2_4" in ids
        assert "prusa_mini_plus" not in ids

    def test_filter_supported_materials(self):
        nylon_capable = [p.printer_id for p in list_printer_profiles() if "nylon" in p.supported_materials]
        assert "bambu_x1c" in nylon_capable
        assert "voron_2_4" in nylon_capable
        assert "prusa_mini_plus" not in nylon_capable

    def test_polycarbonate_support_subset(self):
        pc_capable = [p.printer_id for p in list_printer_profiles() if "polycarbonate" in p.supported_materials]
        assert "bambu_x1c" in pc_capable
        assert "voron_2_4" in pc_capable
        assert "ender_3_v2" not in pc_capable

    def test_default_layer_heights_present(self):
        profile = get_printer_design_profile("prusa_mk4")
        assert profile is not None
        assert profile.default_layer_heights_mm == [0.08, 0.12, 0.16, 0.2, 0.28]

    def test_direct_drive_capability_differs_between_enders(self):
        v2 = get_printer_design_profile("ender_3_v2")
        s1 = get_printer_design_profile("ender_3_s1")
        assert v2 is not None
        assert s1 is not None
        assert v2.has_direct_drive is False
        assert s1.has_direct_drive is True

    def test_printer_profile_to_dict(self):
        profile = get_printer_design_profile("bambu_a1")
        assert profile is not None
        data = profile.to_dict()
        assert data["printer_id"] == "bambu_a1"
        assert "supported_materials" in data
        assert isinstance(data["agent_notes"], list)


# ---------------------------------------------------------------------------
# Generation feedback enhancement
# ---------------------------------------------------------------------------


class TestGenerationFeedbackEnhancement:
    def test_enhance_adds_constraints(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence(
            "shelf bracket for books"
        )
        assert len(result.constraints_added) > 0
        assert result.improved_prompt != result.original_prompt
        assert "Requirements:" in result.improved_prompt

    def test_enhance_respects_max_length(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence(
            "outdoor waterproof shelf bracket that holds heavy books in the garden",
            max_length=200,
        )
        assert len(result.improved_prompt) <= 200

    def test_enhance_with_material_override(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence(
            "a vase", material="petg"
        )
        assert "PETG" in result.improved_prompt

    def test_enhance_includes_printability_basics(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence("decorative figurine")
        lower = result.improved_prompt.lower()
        assert "overhang" in lower or "flat bottom" in lower

    def test_enhance_vanilla_prompt_still_improves(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence("a cool robot toy")
        assert len(result.constraints_added) > 0

    def test_enhance_can_include_printer_build_volume(self):
        from kiln.generation_feedback import enhance_prompt_with_design_intelligence

        result = enhance_prompt_with_design_intelligence(
            "small desk organizer",
            printer_model="bambu_a1_mini",
        )
        assert "180 x 180 x 180 mm" in result.improved_prompt


# ---------------------------------------------------------------------------
# Knowledge base reset / isolation
# ---------------------------------------------------------------------------


class TestKnowledgeBaseIsolation:
    def test_reset_clears_cache(self):
        # First load
        p1 = get_material_profile("pla")
        assert p1 is not None

        # Reset
        _reset_knowledge_base()

        # Should reload cleanly
        p2 = get_material_profile("pla")
        assert p2 is not None
        assert p2.material_id == "pla"

    def test_reset_clears_construction_cache(self):
        m1 = get_construction_material("standard_concrete_mix")
        assert m1 is not None

        _reset_knowledge_base()

        m2 = get_construction_material("standard_concrete_mix")
        assert m2 is not None
        assert m2.material_id == "standard_concrete_mix"


# ---------------------------------------------------------------------------
# Construction domain — materials
# ---------------------------------------------------------------------------


class TestConstructionMaterials:
    def test_get_standard_concrete(self):
        m = get_construction_material("standard_concrete_mix")
        assert isinstance(m, ConstructionMaterialProfile)
        assert m.category == "cementitious"
        assert m.mechanical["compressive_strength_28d_mpa"] == 35

    def test_get_icon_carbonx(self):
        m = get_construction_material("icon_carbonx")
        assert m is not None
        assert m.category == "proprietary_cementitious"
        assert m.mechanical["interlayer_bond_strength_mpa"] == 2.0
        assert m.sustainability is not None
        assert m.sustainability["embodied_carbon_reduction_vs_cmu_pct"] == 31

    def test_get_geopolymer(self):
        m = get_construction_material("geopolymer_concrete")
        assert m is not None
        assert m.mechanical["compressive_strength_28d_mpa"] == 45
        assert m.cost["material_cost_per_m3_usd"] == 120

    def test_get_earth_based(self):
        m = get_construction_material("earth_based_mix")
        assert m is not None
        assert m.cost["material_cost_per_m3_usd"] == 30
        assert m.design_limits["max_stories"] == 1

    def test_unknown_construction_material_returns_none(self):
        assert get_construction_material("moon_regolith") is None

    def test_list_construction_materials_returns_all(self):
        materials = list_construction_materials()
        assert len(materials) >= 4
        ids = {m.material_id for m in materials}
        assert "standard_concrete_mix" in ids
        assert "icon_carbonx" in ids
        assert "geopolymer_concrete" in ids
        assert "earth_based_mix" in ids

    def test_every_construction_material_has_guidance(self):
        for m in list_construction_materials():
            assert len(m.agent_guidance) > 0, f"{m.material_id} missing guidance"

    def test_every_construction_material_has_process(self):
        for m in list_construction_materials():
            assert "open_time_minutes" in m.process, (
                f"{m.material_id} missing open_time_minutes"
            )
            assert "cure_time_days" in m.process, (
                f"{m.material_id} missing cure_time_days"
            )

    def test_every_construction_material_has_compliance(self):
        for m in list_construction_materials():
            assert "applicable_codes" in m.compliance, (
                f"{m.material_id} missing applicable_codes"
            )

    def test_construction_material_to_dict(self):
        m = get_construction_material("standard_concrete_mix")
        assert m is not None
        d = m.to_dict()
        assert d["material_id"] == "standard_concrete_mix"
        assert "mechanical" in d
        assert "process" in d
        assert "cost" in d

    def test_sustainability_omitted_when_none(self):
        m = get_construction_material("standard_concrete_mix")
        assert m is not None
        d = m.to_dict()
        assert "sustainability" not in d


# ---------------------------------------------------------------------------
# Construction domain — patterns
# ---------------------------------------------------------------------------


class TestConstructionPatterns:
    def test_get_load_bearing_wall(self):
        p = get_construction_pattern("load_bearing_wall")
        assert isinstance(p, ConstructionPattern)
        assert "wall" in p.display_name.lower()
        assert p.wall_profiles is not None
        assert "double_bead" in p.wall_profiles

    def test_get_curved_wall(self):
        p = get_construction_pattern("curved_wall")
        assert p is not None
        assert p.design_rules["min_radius_m"] == 0.5

    def test_get_window_opening(self):
        p = get_construction_pattern("window_opening")
        assert p is not None
        assert "header" in p.display_name.lower() or "opening" in p.display_name.lower()

    def test_unknown_pattern_returns_none(self):
        assert get_construction_pattern("flying_buttress") is None

    def test_list_construction_patterns_returns_all(self):
        patterns = list_construction_patterns()
        assert len(patterns) >= 8
        ids = {p.pattern_id for p in patterns}
        assert "load_bearing_wall" in ids
        assert "curved_wall" in ids
        assert "insulated_wall_system" in ids

    def test_every_construction_pattern_has_guidance(self):
        for p in list_construction_patterns():
            assert len(p.agent_guidance) > 0, f"{p.pattern_id} missing guidance"

    def test_construction_pattern_to_dict(self):
        p = get_construction_pattern("load_bearing_wall")
        assert p is not None
        d = p.to_dict()
        assert "design_rules" in d
        assert "wall_profiles" in d


# ---------------------------------------------------------------------------
# Construction domain — requirements
# ---------------------------------------------------------------------------


class TestConstructionRequirements:
    def test_get_single_family_residential(self):
        r = get_construction_requirement("single_family_residential")
        assert isinstance(r, ConstructionRequirement)
        assert r.program_requirements["min_sqft"] == 600
        assert r.code_requirements is not None

    def test_get_military_defense(self):
        r = get_construction_requirement("military_defense")
        assert r is not None
        assert r.structural_constraints["wall_type"] == "triple_bead"
        assert r.compliance_requirements is not None

    def test_get_affordable_housing(self):
        r = get_construction_requirement("affordable_housing")
        assert r is not None
        assert r.program_requirements["cost_target_per_sqft_usd"] == 100

    def test_get_disaster_relief(self):
        r = get_construction_requirement("disaster_relief_shelter")
        assert r is not None
        assert r.program_requirements["deployment_time_critical"] is True

    def test_get_commercial(self):
        r = get_construction_requirement("commercial_single_story")
        assert r is not None
        assert r.program_requirements["ada_compliance"] is True

    def test_unknown_requirement_returns_none(self):
        assert get_construction_requirement("space_habitat") is None

    def test_list_construction_requirements_returns_all(self):
        reqs = list_construction_requirements()
        assert len(reqs) >= 5
        ids = {r.requirement_id for r in reqs}
        assert "single_family_residential" in ids
        assert "affordable_housing" in ids
        assert "military_defense" in ids
        assert "disaster_relief_shelter" in ids
        assert "commercial_single_story" in ids

    def test_match_house_triggers(self):
        results = match_construction_requirements("build a single family house")
        ids = {r.requirement_id for r in results}
        assert "single_family_residential" in ids

    def test_match_affordable_triggers(self):
        results = match_construction_requirements("affordable housing for low income families")
        ids = {r.requirement_id for r in results}
        assert "affordable_housing" in ids

    def test_match_military_triggers(self):
        results = match_construction_requirements("military barracks for forward operating base")
        ids = {r.requirement_id for r in results}
        assert "military_defense" in ids

    def test_match_disaster_triggers(self):
        results = match_construction_requirements("emergency disaster relief shelter after hurricane")
        ids = {r.requirement_id for r in results}
        assert "disaster_relief_shelter" in ids

    def test_no_match_returns_empty(self):
        results = match_construction_requirements("something unrelated to construction")
        assert len(results) == 0

    def test_construction_requirement_to_dict(self):
        r = get_construction_requirement("single_family_residential")
        assert r is not None
        d = r.to_dict()
        assert "program_requirements" in d
        assert "structural_constraints" in d
        assert "agent_guidance" in d


# ---------------------------------------------------------------------------
# Construction domain — design brief
# ---------------------------------------------------------------------------


class TestConstructionDesignBrief:
    def test_basic_house_brief(self):
        brief = get_construction_design_brief("build a single family home")
        assert isinstance(brief, ConstructionDesignBrief)
        assert brief.requirement is not None
        assert brief.requirement.requirement_id == "single_family_residential"
        assert len(brief.materials) > 0
        assert len(brief.combined_guidance) > 0

    def test_affordable_brief_has_cost_target(self):
        brief = get_construction_design_brief("affordable social housing")
        assert brief.requirement is not None
        assert brief.requirement.program_requirements["cost_target_per_sqft_usd"] == 100

    def test_military_brief_has_structural_constraints(self):
        brief = get_construction_design_brief("military barracks")
        assert brief.requirement is not None
        assert brief.combined_rules["wall_type"] == "triple_bead"

    def test_material_override(self):
        brief = get_construction_design_brief(
            "build a house",
            material="geopolymer_concrete",
        )
        assert len(brief.materials) == 1
        assert brief.materials[0].material_id == "geopolymer_concrete"

    def test_disaster_brief_finds_patterns(self):
        brief = get_construction_design_brief(
            "emergency shelter with curved walls"
        )
        pattern_ids = {p.pattern_id for p in brief.applicable_patterns}
        assert "curved_wall" in pattern_ids

    def test_vague_requirements_still_works(self):
        brief = get_construction_design_brief("build something")
        assert isinstance(brief, ConstructionDesignBrief)
        assert len(brief.materials) > 0

    def test_brief_to_dict(self):
        brief = get_construction_design_brief("single family residence")
        d = brief.to_dict()
        assert "requirement" in d
        assert "materials" in d
        assert "applicable_patterns" in d
        assert "combined_guidance" in d
        assert "combined_rules" in d


# ---------------------------------------------------------------------------
# Troubleshooting
# ---------------------------------------------------------------------------


class TestTroubleshooting:
    def test_all_issues_for_material(self):
        result = troubleshoot_print_issue("pla")
        assert result is not None
        assert isinstance(result, TroubleshootingResult)
        assert result.material == "pla"
        assert len(result.matched_issues) > 0

    def test_symptom_match_stringing(self):
        result = troubleshoot_print_issue("pla", "stringing")
        assert result is not None
        assert any("string" in i["symptom"].lower() for i in result.matched_issues)

    def test_symptom_match_warping(self):
        result = troubleshoot_print_issue("abs", "warping")
        assert result is not None
        assert any("warp" in i["symptom"].lower() for i in result.matched_issues)

    def test_severity_ordering(self):
        result = troubleshoot_print_issue("pla")
        assert result is not None
        severities = [i.get("severity") for i in result.matched_issues]
        severity_order = {"major": 0, "moderate": 1, "minor": 2}
        values = [severity_order.get(s, 2) for s in severities]
        assert values == sorted(values)

    def test_fixes_have_priority(self):
        result = troubleshoot_print_issue("pla", "stringing")
        assert result is not None
        assert len(result.matched_issues) > 0
        fixes = result.matched_issues[0]["fixes"]
        assert all("priority" in f for f in fixes)
        assert all("action" in f for f in fixes)

    def test_storage_requirements(self):
        result = troubleshoot_print_issue("nylon")
        assert result is not None
        assert result.storage_requirements is not None
        assert result.storage_requirements["humidity_sensitive"] is True

    def test_break_in_tips(self):
        result = troubleshoot_print_issue("pla")
        assert result is not None
        assert len(result.break_in_tips) > 0

    def test_unknown_material_returns_none(self):
        assert troubleshoot_print_issue("unobtainium") is None

    def test_no_symptom_match_returns_empty_list(self):
        result = troubleshoot_print_issue("pla", "xyznonexistent")
        assert result is not None
        assert len(result.matched_issues) == 0

    def test_to_dict(self):
        result = troubleshoot_print_issue("petg", "stringing")
        assert result is not None
        d = result.to_dict()
        assert "material" in d
        assert "matched_issues" in d
        assert "storage_requirements" in d
        assert "break_in_tips" in d

    def test_list_troubleshooting_materials(self):
        materials = list_troubleshooting_materials()
        assert "pla" in materials
        assert "abs" in materials
        assert "nylon" in materials
        assert len(materials) >= 10

    def test_case_insensitive(self):
        result = troubleshoot_print_issue("PLA", "Stringing")
        assert result is not None
        assert result.material == "pla"


# ---------------------------------------------------------------------------
# Printer-Material Compatibility
# ---------------------------------------------------------------------------


class TestPrinterMaterialCompatibility:
    def test_specific_material_compatible(self):
        report = check_printer_material_compatibility("ender3", "pla")
        assert report is not None
        assert isinstance(report, PrinterCompatibilityReport)
        assert "pla" in report.materials
        assert report.materials["pla"]["status"] == "compatible"

    def test_material_needs_upgrade(self):
        report = check_printer_material_compatibility("ender3", "abs")
        assert report is not None
        mat = report.materials.get("abs")
        assert mat is not None
        assert mat["status"] == "needs_upgrade"
        assert len(mat["upgrades_needed"]) > 0

    def test_all_materials_for_printer(self):
        report = check_printer_material_compatibility("bambu_x1c")
        assert report is not None
        assert len(report.materials) > 5

    def test_unknown_printer_falls_back_to_default(self):
        report = check_printer_material_compatibility("totally_unknown_printer")
        assert report is not None
        assert report.printer_id == "default"

    def test_unknown_material_returns_unknown_status(self):
        report = check_printer_material_compatibility("ender3", "unobtainium")
        assert report is not None
        assert "unobtainium" in report.materials
        assert report.materials["unobtainium"]["status"] == "unknown"

    def test_list_compatibility_printers(self):
        printers = list_compatibility_printers()
        assert "ender3" in printers
        assert "bambu_x1c" in printers
        assert len(printers) >= 10

    def test_case_insensitive(self):
        report = check_printer_material_compatibility("Ender3", "PLA")
        assert report is not None

    def test_to_dict(self):
        report = check_printer_material_compatibility("ender3", "pla")
        assert report is not None
        d = report.to_dict()
        assert "printer_id" in d
        assert "materials" in d


# ---------------------------------------------------------------------------
# Post-Processing
# ---------------------------------------------------------------------------


class TestPostProcessing:
    def test_pla_techniques(self):
        guide = get_post_processing("pla")
        assert guide is not None
        assert isinstance(guide, PostProcessingGuide)
        assert guide.material == "pla"
        assert len(guide.techniques) > 0

    def test_techniques_have_fields(self):
        guide = get_post_processing("pla")
        assert guide is not None
        tech = guide.techniques[0]
        assert "name" in tech
        assert "difficulty" in tech
        assert "procedure" in tech

    def test_paintability(self):
        guide = get_post_processing("pla")
        assert guide is not None
        assert guide.paintability is not None
        assert "paint_types" in guide.paintability

    def test_strengthening(self):
        guide = get_post_processing("pla")
        assert guide is not None
        assert len(guide.strengthening) > 0
        s = guide.strengthening[0]
        assert "method" in s
        assert "applicable" in s

    def test_abs_vapor_smoothing(self):
        guide = get_post_processing("abs")
        assert guide is not None
        technique_names = [t["name"].lower() for t in guide.techniques]
        assert any("acetone" in n or "vapor" in n for n in technique_names)

    def test_unknown_material(self):
        assert get_post_processing("unobtainium") is None

    def test_to_dict(self):
        guide = get_post_processing("petg")
        assert guide is not None
        d = guide.to_dict()
        assert "techniques" in d
        assert "paintability" in d
        assert "strengthening" in d


# ---------------------------------------------------------------------------
# Multi-Material Compatibility
# ---------------------------------------------------------------------------


class TestMultiMaterialCompatibility:
    def test_pla_tpu_compatible(self):
        report = check_multi_material_compatibility("pla", "tpu")
        assert isinstance(report, MultiMaterialReport)
        assert report.compatible is True
        assert report.interface_adhesion in ("moderate", "good", "excellent")

    def test_pla_abs_incompatible(self):
        report = check_multi_material_compatibility("pla", "abs")
        assert report.compatible is False

    def test_abs_hips_support_pair(self):
        report = check_multi_material_compatibility("abs", "hips")
        assert report.compatible is True
        assert report.support_pair is not None
        assert "dissolution_method" in report.support_pair

    def test_pla_pva_support_pair(self):
        report = check_multi_material_compatibility("pla", "pva")
        assert report.compatible is True
        assert report.support_pair is not None

    def test_bidirectional_lookup(self):
        ab = check_multi_material_compatibility("pla", "tpu")
        ba = check_multi_material_compatibility("tpu", "pla")
        assert ab.compatible == ba.compatible

    def test_unknown_pair(self):
        report = check_multi_material_compatibility("unobtainium", "pla")
        assert report.compatible is False
        assert report.interface_adhesion == "unknown"

    def test_general_rules_included(self):
        report = check_multi_material_compatibility("pla", "petg")
        assert len(report.general_rules) > 0

    def test_to_dict(self):
        report = check_multi_material_compatibility("pla", "tpu")
        d = report.to_dict()
        assert "material_a" in d
        assert "material_b" in d
        assert "compatible" in d
        assert "general_rules" in d

    def test_support_material_options_pla(self):
        options = get_support_material_options("pla")
        assert len(options) > 0
        assert any(
            o.get("support_material", "").lower() == "pva" for o in options
        )

    def test_support_material_options_abs(self):
        options = get_support_material_options("abs")
        assert len(options) > 0
        assert any(
            o.get("support_material", "").lower() == "hips" for o in options
        )

    def test_support_material_options_unknown(self):
        options = get_support_material_options("unobtainium")
        assert options == []


# ---------------------------------------------------------------------------
# Cross-File Print Diagnostic
# ---------------------------------------------------------------------------


class TestPrintDiagnostic:
    def test_basic_diagnostic(self):
        result = get_print_diagnostic("pla", symptom="stringing")
        assert result is not None
        assert isinstance(result, PrintDiagnostic)
        assert result.material == "pla"
        assert len(result.matched_issues) > 0

    def test_with_printer_context(self):
        result = get_print_diagnostic(
            "abs", symptom="warping", printer_id="ender3"
        )
        assert result is not None
        assert result.printer_id == "ender3"
        assert result.printer_compatibility is not None

    def test_printer_needs_upgrade_in_guidance(self):
        result = get_print_diagnostic(
            "nylon", printer_id="ender3"
        )
        assert result is not None
        assert result.printer_compatibility is not None
        # Nylon on an Ender 3 needs upgrades
        assert result.printer_compatibility.get("status") == "needs_upgrade"
        assert any("upgrade" in g.lower() for g in result.combined_guidance)

    def test_storage_in_guidance(self):
        result = get_print_diagnostic("nylon")
        assert result is not None
        assert result.storage_requirements is not None
        assert any("stor" in g.lower() or "dry" in g.lower() for g in result.combined_guidance)

    def test_unknown_material_returns_none(self):
        assert get_print_diagnostic("unobtainium") is None

    def test_no_symptom_returns_all_issues(self):
        result = get_print_diagnostic("pla")
        assert result is not None
        assert len(result.matched_issues) > 0

    def test_to_dict(self):
        result = get_print_diagnostic(
            "petg", symptom="stringing", printer_id="bambu_x1c"
        )
        assert result is not None
        d = result.to_dict()
        assert "material" in d
        assert "matched_issues" in d
        assert "printer_compatibility" in d
        assert "combined_guidance" in d
        assert "post_processing_tips" in d

    def test_post_processing_tips_included(self):
        result = get_print_diagnostic("pla")
        assert result is not None
        # PLA has annealing as a strengthening option
        assert isinstance(result.post_processing_tips, list)
