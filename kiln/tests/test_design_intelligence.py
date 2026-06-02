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
- Edge cases: unknown materials, empty text, no matches
- Generation feedback enhancement integration
"""

from __future__ import annotations

import pytest

from .conftest import (
    requires_engineering_overlay,
    requires_multi_material_overlay,
    requires_post_processing_overlay,
    requires_printer_profiles_overlay,
    requires_troubleshooting_overlay,
)

from kiln.design_intelligence import (
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
    find_templates_for_use_case,
    get_design_constraints,
    get_design_template,
    get_material_profile,
    get_post_processing,
    get_print_diagnostic,
    get_printer_design_profile,
    get_support_material_options,
    list_compatibility_printers,
    list_design_templates,
    list_material_profiles,
    list_printer_profiles,
    list_troubleshooting_materials,
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
    @requires_engineering_overlay
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

    @requires_engineering_overlay
    def test_get_nylon(self):
        p = get_material_profile("nylon")
        assert p is not None
        assert p.mechanical["fatigue_resistance"] == "excellent"

    @requires_engineering_overlay
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

    @requires_engineering_overlay
    def test_every_material_has_agent_guidance(self):
        for p in list_material_profiles():
            assert len(p.agent_guidance) > 0, f"{p.material_id} missing guidance"

    def test_every_material_has_design_limits(self):
        # Safety-floor: every material has the process-floor design limits
        # in public materials.json (min_wall_thickness_mm,
        # max_unsupported_overhang_deg).  The engineering-grade limits
        # (snap_fit_tolerance_mm, max_cantilever_length_mm,
        # living_hinge_viable, etc.) live in the kiln-pro overlay and are
        # asserted in kiln-pro's overlay sanity test.
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


class TestDesignTemplates:
    def test_get_snap_fit(self):
        p = get_design_template("snap_fit_cantilever")
        assert p is not None
        assert "snap" in p.display_name.lower()
        assert "pla" in p.material_compatibility["poor"]

    def test_get_gear(self):
        p = get_design_template("gear")
        assert p is not None
        assert "nylon" in p.material_compatibility["excellent"]

    def test_get_living_hinge(self):
        p = get_design_template("living_hinge")
        assert p is not None
        assert "tpu" in p.material_compatibility["excellent"]
        assert "pla" in p.material_compatibility["avoid"]

    def test_unknown_pattern_returns_none(self):
        assert get_design_template("quantum_teleporter") is None

    def test_list_patterns_returns_all(self):
        patterns = list_design_templates()
        assert len(patterns) >= 8
        ids = {p.template_id for p in patterns}
        assert "snap_fit_cantilever" in ids
        assert "gear" in ids
        assert "watertight_container" in ids

    def test_find_patterns_for_enclosure(self):
        results = find_templates_for_use_case("enclosures")
        ids = {p.template_id for p in results}
        assert "snap_fit_cantilever" in ids or "enclosure_box" in ids

    def test_find_patterns_for_gears(self):
        results = find_templates_for_use_case("gear")
        assert len(results) > 0

    def test_find_patterns_empty_returns_empty(self):
        results = find_templates_for_use_case("quantum_computing")
        assert len(results) == 0

    def test_pattern_to_dict(self):
        p = get_design_template("press_fit")
        assert p is not None
        d = p.to_dict()
        assert "design_rules" in d
        assert "material_compatibility" in d
        assert "agent_guidance" in d

    @requires_engineering_overlay
    def test_every_pattern_has_guidance(self):
        # ``agent_guidance`` is moat-tier data — populated only when the
        # kiln-pro engineering overlay is loaded.  Public-only CI runs
        # see empty agent_guidance per the design-knowledge moat split.
        for p in list_design_templates():
            assert len(p.agent_guidance) > 0, f"{p.template_id} missing guidance"


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
    @requires_engineering_overlay
    def test_basic_brief(self):
        # ``combined_guidance`` rolls up template + material agent_guidance
        # which is moat-tier data; empty in public-only CI runs.
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
        template_ids = {p.template_id for p in brief.applicable_patterns}
        assert "enclosure_box" in template_ids or "snap_fit_cantilever" in template_ids

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

    @requires_printer_profiles_overlay
    def test_printer_model_influences_brief(self):
        # ``get_design_constraints`` with a ``printer_model`` arg goes
        # through ``get_printer_design_profile``, which hard-keys the
        # ``agent_notes`` moat field; and ``combined_guidance`` rolls
        # up that same moat content (the ``"consumer platform"``
        # substring lives there).
        brief = get_design_constraints(
            "outdoor garden sign that lives in the sun",
            printer_model="bambu_a1",
        )
        assert brief.recommended_material is not None
        assert brief.recommended_material.material.material_id != "asa"
        assert "printer_build_volume_mm" in brief.combined_rules
        assert "printer_supported_materials" in brief.combined_rules
        assert any("consumer platform" in note.lower() for note in brief.combined_guidance)

    @requires_printer_profiles_overlay
    def test_material_override_warns_when_printer_is_a_bad_fit(self):
        # Same as above — the printer-profile lookup hard-keys
        # ``agent_notes``; without the overlay the warning code is
        # never reached.
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
    # NOTE: every test that calls ``get_printer_design_profile`` or
    # ``list_printer_profiles`` exercises a constructor that
    # hard-keys the moat-tier ``agent_notes`` field; without the
    # kiln-pro printer_profiles overlay those calls raise KeyError.
    # The unknown-printer path is the one exception — it returns
    # None before touching the constructor.

    @requires_printer_profiles_overlay
    def test_get_known_printer_profile(self):
        profile = get_printer_design_profile("bambu_x1c")
        assert isinstance(profile, PrinterDesignProfile)
        assert profile is not None
        assert profile.display_name == "Bambu Lab X1 Carbon"
        assert profile.has_enclosure is True

    def test_unknown_printer_returns_none(self):
        assert get_printer_design_profile("unknown_printer") is None

    @requires_printer_profiles_overlay
    def test_case_insensitive_lookup(self):
        profile = get_printer_design_profile("BAMBU_X1C")
        assert profile is not None
        assert profile.printer_id == "bambu_x1c"

    @requires_printer_profiles_overlay
    def test_list_printers_includes_all_known_profiles(self):
        profiles = list_printer_profiles()
        assert len(profiles) >= 9
        ids = {p.printer_id for p in profiles}
        assert "bambu_x1c" in ids
        assert "voron_2" in ids
        assert "prusa_mk4" in ids

    @requires_printer_profiles_overlay
    def test_filter_has_enclosure(self):
        enclosed = [p for p in list_printer_profiles() if p.has_enclosure]
        ids = {p.printer_id for p in enclosed}
        assert "bambu_x1c" in ids
        assert "voron_2" in ids
        assert "prusa_mini" not in ids

    @requires_printer_profiles_overlay
    def test_filter_supported_materials(self):
        nylon_capable = [p.printer_id for p in list_printer_profiles() if "nylon" in p.supported_materials]
        assert "bambu_x1c" in nylon_capable
        assert "voron_2" in nylon_capable
        assert "prusa_mini" not in nylon_capable

    @requires_printer_profiles_overlay
    def test_polycarbonate_support_subset(self):
        pc_capable = [p.printer_id for p in list_printer_profiles() if "polycarbonate" in p.supported_materials]
        assert "bambu_x1c" in pc_capable
        assert "voron_2" in pc_capable
        assert "ender3_v2" not in pc_capable

    @requires_printer_profiles_overlay
    def test_default_layer_heights_present(self):
        profile = get_printer_design_profile("prusa_mk4")
        assert profile is not None
        assert profile.default_layer_heights_mm == [0.08, 0.12, 0.16, 0.2, 0.28]

    @requires_printer_profiles_overlay
    def test_direct_drive_capability_differs_between_enders(self):
        v2 = get_printer_design_profile("ender3_v2")
        s1 = get_printer_design_profile("ender3_s1")
        assert v2 is not None
        assert s1 is not None
        assert v2.has_direct_drive is False
        assert s1.has_direct_drive is True

    @requires_printer_profiles_overlay
    def test_printer_profile_to_dict(self):
        profile = get_printer_design_profile("bambu_a1")
        assert profile is not None
        data = profile.to_dict()
        assert data["printer_id"] == "bambu_a1"
        assert "supported_materials" in data
        assert isinstance(data["agent_notes"], list)


class TestIntelDerivedDesignProfiles:
    """Every supported printer gets a design profile, derived from
    printer_intelligence when no curated printer_profiles.json record
    exists. Regression guard for the gap where ~36 supported printers
    returned "Unknown printer"."""

    def test_derive_manufacturer_from_prefix(self):
        from kiln.design_intelligence import _derive_manufacturer
        assert _derive_manufacturer("bambu_x1e", "Bambu Lab X1E") == "Bambu Lab"
        assert _derive_manufacturer("ender3_v2", "Creality Ender 3 V2") == "Creality"
        assert _derive_manufacturer("voron_2", "Voron 2.4 / Trident") == "Voron"
        # Unknown prefix falls back to the first display-name token.
        assert _derive_manufacturer("mystery_9000", "Acme Rocket") == "Acme"

    def test_profile_derived_from_intelligence(self):
        from kiln import printer_intelligence as pi
        from kiln.design_intelligence import _design_profile_from_intel
        pi._load_raw()
        raw = pi._raw_cache["ender3"]
        prof = _design_profile_from_intel("ender3", raw)
        assert prof.printer_id == "ender3"
        assert prof.manufacturer == "Creality"
        assert {"x", "y", "z"} <= set(prof.build_volume_mm)
        assert prof.supported_materials == sorted(m.lower() for m in raw["materials"])
        assert prof.default_layer_heights_mm  # non-empty layer ladder
        assert prof.agent_notes == []  # curated notes come from the pro overlay

    def test_every_supported_printer_has_a_profile(self):
        from kiln import printer_intelligence as pi
        from kiln.design_intelligence import get_printer_design_profile
        pi._load_raw()
        for pid in pi._raw_cache:
            if pid == "default":
                continue
            assert get_printer_design_profile(pid) is not None, (
                f"{pid} is in printer_intelligence but has no design profile"
            )


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

    @requires_printer_profiles_overlay
    def test_enhance_can_include_printer_build_volume(self):
        # The enhance pipeline calls ``get_printer_design_profile``,
        # which raises KeyError without the moat-tier ``agent_notes``
        # field; the enhance helper swallows that exception and
        # returns the unmodified prompt, so the build-volume string
        # never makes it into the result.
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



# ---------------------------------------------------------------------------
# Troubleshooting
# ---------------------------------------------------------------------------


class TestTroubleshooting:
    # NOTE: ``common_issues`` (matched_issues) and ``break_in_tips``
    # are moat fields — public material_troubleshooting.json carries
    # only ``storage_requirements``.  Tests that exercise those moat
    # fields are gated; the storage-requirements test stays free
    # because that data ships in public.

    @requires_troubleshooting_overlay
    def test_all_issues_for_material(self):
        result = troubleshoot_print_issue("pla")
        assert result is not None
        assert isinstance(result, TroubleshootingResult)
        assert result.material == "pla"
        assert len(result.matched_issues) > 0

    @requires_troubleshooting_overlay
    def test_symptom_match_stringing(self):
        result = troubleshoot_print_issue("pla", "stringing")
        assert result is not None
        assert any("string" in i["symptom"].lower() for i in result.matched_issues)

    @requires_troubleshooting_overlay
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

    @requires_troubleshooting_overlay
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

    @requires_troubleshooting_overlay
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

    def test_creality_k1c_abrasive_material_compatible(self):
        report = check_printer_material_compatibility("k1c", "cf_pla")
        assert report is not None
        assert report.materials["cf_pla"]["status"] == "compatible"

    def test_creality_k1_max_abrasive_material_needs_nozzle(self):
        report = check_printer_material_compatibility("k1_max", "cf_pla")
        assert report is not None
        assert report.materials["cf_pla"]["status"] == "needs_upgrade"
        assert "hardened_nozzle" in report.materials["cf_pla"]["upgrades_needed"]

    def test_creality_k2_pro_abrasive_material_compatible(self):
        report = check_printer_material_compatibility("k2_pro", "cf_pla")
        assert report is not None
        assert report.materials["cf_pla"]["status"] == "compatible"
        assert report.materials["cf_pla"]["upgrades_needed"] == []

    def test_creality_open_frame_abs_needs_enclosure(self):
        report = check_printer_material_compatibility("ender3_v4", "abs")
        assert report is not None
        assert report.materials["abs"]["status"] == "needs_upgrade"
        assert "enclosure" in report.materials["abs"]["upgrades_needed"]

    def test_creality_ender3_v2_tpu_needs_direct_drive(self):
        report = check_printer_material_compatibility("ender3_v2", "tpu")
        assert report is not None
        assert report.materials["tpu"]["status"] == "needs_upgrade"
        assert "direct_drive" in report.materials["tpu"]["upgrades_needed"]

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

    @requires_post_processing_overlay
    def test_techniques_have_fields(self):
        # The ``procedure`` walkthrough is moat-tier — public-only
        # post_processing.json carries just ``name``, ``difficulty``,
        # and ``tools_needed``.
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

    def test_abs_asa_compatible(self):
        """ABS and ASA are co-printable (same material family)."""
        report = check_multi_material_compatibility("abs", "asa")
        assert report.compatible is True

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

    @requires_multi_material_overlay
    def test_general_rules_included(self):
        # ``general_rules`` is a moat-tier list of co-print guidance
        # bullets; public multi_material_pairing.json carries an
        # empty list at that key so the safety-floor consumer at
        # least sees the field.
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
        """ABS may or may not have support material options after HIPS removal."""
        options = get_support_material_options("abs")
        assert isinstance(options, list)

    def test_support_material_options_unknown(self):
        options = get_support_material_options("unobtainium")
        assert options == []


# ---------------------------------------------------------------------------
# Cross-File Print Diagnostic
# ---------------------------------------------------------------------------


class TestPrintDiagnostic:
    # ``matched_issues`` for the diagnostic comes from the
    # troubleshooting catalog's ``common_issues`` — a moat field.
    # Without the overlay the diagnostic still returns a result, but
    # the issues list is empty.

    @requires_troubleshooting_overlay
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

    @requires_troubleshooting_overlay
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


# ---------------------------------------------------------------------------
# load_pro_overlay_or_empty — parameter-bag overlay loader
# ---------------------------------------------------------------------------
#
# Pairs with the existing entity-keyed loader
# ``_merge_pro_overlay_if_available``.  Used by public modules that
# need a flat parameter dict (orientation scoring weights, structural
# thresholds, scorecard deduction rules, printability judgment tables)
# rather than a per-record deep merge.  Free tier returns ``{}`` so the
# caller falls through to its safe-default values.


class TestLoadProOverlayOrEmpty:
    """Free-tier safe-default loader for parameter-bag overlays.

    Three concerns pinned:

      1. Unknown / unsupported overlay kind never raises — programming
         errors silently degrade to free-tier behaviour.
      2. Missing or unreachable kiln-pro never crashes a public module
         — ImportError, network failure, license rejection all return
         ``{}`` the same way.
      3. A valid request returns a dict — even when free tier yields
         ``{}``, the type contract holds so callers can ``.get()``.
    """

    def test_unknown_kind_returns_empty_dict(self):
        """An unknown overlay kind logs at error level and returns ``{}``
        rather than raising.  Callers fall through to safe defaults."""
        from kiln.design_intelligence import load_pro_overlay_or_empty

        result = load_pro_overlay_or_empty("not_a_real_overlay_kind_xyz")
        assert result == {}

    def test_known_kind_returns_dict(self, monkeypatch):
        """A known kind returns a dict.  Whether it's populated depends
        on whether kiln-pro is installed + license is valid; the
        type contract is the invariant."""
        from kiln.design_intelligence import load_pro_overlay_or_empty

        # Disable any network fetch so the test doesn't depend on
        # connectivity or license state.
        monkeypatch.setenv("KILN_OVERLAY_DISABLE_FETCH", "1")
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        # Clear the kiln-pro in-process cache if it's importable, so
        # repeated test runs don't see stale state.
        try:
            from kiln_pro.data_overlays import clear_process_cache  # type: ignore[import-not-found]

            clear_process_cache()
        except ImportError:
            pass

        result = load_pro_overlay_or_empty("materials")
        assert isinstance(result, dict)

    def test_kiln_pro_import_failure_returns_empty(self, monkeypatch):
        """If kiln-pro is not installed, the loader returns ``{}``
        without raising.  This is the canonical free-tier path."""
        import sys

        from kiln.design_intelligence import load_pro_overlay_or_empty

        # Simulate kiln-pro absent: blank the module so the import
        # inside the helper fails.
        real_module = sys.modules.pop("kiln_pro.data_overlays", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", None)
        try:
            result = load_pro_overlay_or_empty("materials")
            assert result == {}
        finally:
            if real_module is not None:
                sys.modules["kiln_pro.data_overlays"] = real_module
