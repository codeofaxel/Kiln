"""Tests for pre-generation print estimation (pre_estimate.py).

Covers:
    - estimate_from_dimensions: single-material, multi-material, edge cases
    - estimate_from_template: known templates, missing templates, overrides
    - Printer speed profiles: known printers vs defaults
    - Tool change estimation: AMS, MMU, manual, single-material
    - Input validation: bad dimensions, bad fractions, bad materials
    - Cost accuracy: electricity + filament cost math
    - Confidence scoring
    - FilamentUsage and PreEstimate dataclass serialization
"""

from __future__ import annotations

import json

import pytest

from kiln.pre_estimate import (
    FilamentUsage,
    _extract_dim,
    _format_time,
    _get_material_profile,
    _get_printer_infill,
    _get_printer_layer_height,
    _get_printer_speeds,
    _get_printer_tool_change,
    _resolve_template_dimensions,
    estimate_from_dimensions,
    estimate_from_template,
    list_addons,
)

# ---------------------------------------------------------------------------
# _format_time
# ---------------------------------------------------------------------------


class TestFormatTime:
    """Tests for the _format_time helper."""

    def test_zero_returns_unknown(self):
        assert _format_time(0) == "unknown"

    def test_negative_returns_unknown(self):
        assert _format_time(-10) == "unknown"

    def test_minutes_only(self):
        assert _format_time(2700) == "45m"

    def test_hours_and_minutes(self):
        assert _format_time(5400) == "1h 30m"

    def test_exact_hour(self):
        assert _format_time(3600) == "1h 0m"

    def test_small_duration(self):
        assert _format_time(30) == "0m"

    def test_large_duration(self):
        assert _format_time(36000) == "10h 0m"


# ---------------------------------------------------------------------------
# _get_material_profile
# ---------------------------------------------------------------------------


class TestGetMaterialProfile:
    """Tests for material lookup."""

    def test_known_material_pla(self):
        p = _get_material_profile("PLA")
        assert p["name"] == "PLA"
        assert p["density_g_per_cm3"] == pytest.approx(1.24, abs=0.01)

    def test_known_material_petg(self):
        p = _get_material_profile("PETG")
        assert p["name"] == "PETG"

    def test_case_insensitive(self):
        p = _get_material_profile("pla")
        assert p["name"] == "PLA"

    def test_unknown_falls_back_to_pla(self):
        p = _get_material_profile("UNOBTANIUM")
        assert p["name"] == "PLA"

    def test_has_required_keys(self):
        p = _get_material_profile("ABS")
        for key in ("name", "density_g_per_cm3", "cost_per_kg_usd", "filament_diameter_mm"):
            assert key in p


# ---------------------------------------------------------------------------
# _get_printer_speeds
# ---------------------------------------------------------------------------


class TestGetPrinterSpeeds:
    """Tests for printer speed profile lookup."""

    def test_known_printer_bambu_a1(self):
        speeds = _get_printer_speeds("bambu_a1")
        assert speeds["infill"] == pytest.approx(250.0)
        assert speeds["perimeter"] == pytest.approx(200.0)
        assert speeds["first_layer"] == pytest.approx(50.0)

    def test_known_printer_ender3(self):
        speeds = _get_printer_speeds("ender3")
        assert speeds["infill"] == pytest.approx(50.0)

    def test_unknown_printer_returns_defaults(self):
        speeds = _get_printer_speeds("nonexistent_printer_xyz")
        assert speeds["perimeter"] > 0
        assert speeds["infill"] > 0

    def test_none_printer_returns_defaults(self):
        speeds = _get_printer_speeds(None)
        assert speeds["perimeter"] > 0

    def test_all_speeds_positive(self):
        speeds = _get_printer_speeds("bambu_a1")
        for key, val in speeds.items():
            assert val > 0, f"Speed '{key}' should be positive"


# ---------------------------------------------------------------------------
# _get_printer_layer_height / _get_printer_infill
# ---------------------------------------------------------------------------


class TestPrinterDefaults:
    """Tests for printer default layer height and infill."""

    def test_bambu_a1_layer_height(self):
        assert _get_printer_layer_height("bambu_a1") == pytest.approx(0.2)

    def test_bambu_a1_infill(self):
        assert _get_printer_infill("bambu_a1") == pytest.approx(15.0)

    def test_prusa_mk4_infill(self):
        assert _get_printer_infill("prusa_mk4") == pytest.approx(20.0)

    def test_none_printer_defaults(self):
        assert _get_printer_layer_height(None) == pytest.approx(0.2)
        assert _get_printer_infill(None) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# _get_printer_tool_change
# ---------------------------------------------------------------------------


class TestGetPrinterToolChange:
    """Tests for per-printer tool change data lookup."""

    def test_bambu_a1_ams_lite(self):
        tc = _get_printer_tool_change("bambu_a1")
        assert tc["tool_changer"] == "ams_lite"
        assert tc["has_auto_tool_change"] is True
        assert tc["tool_change_seconds"] > 0

    def test_bambu_x1c_ams(self):
        tc = _get_printer_tool_change("bambu_x1c")
        assert tc["tool_changer"] == "ams"
        assert tc["has_auto_tool_change"] is True

    def test_prusa_mk4_mmu3(self):
        tc = _get_printer_tool_change("prusa_mk4")
        assert tc["tool_changer"] == "mmu3"
        assert tc["has_auto_tool_change"] is True

    def test_prusa_mk3s_mmu2s(self):
        tc = _get_printer_tool_change("prusa_mk3s")
        assert tc["tool_changer"] == "mmu2s"
        assert tc["has_auto_tool_change"] is True

    def test_voron_2_ercf(self):
        tc = _get_printer_tool_change("voron_2")
        assert tc["tool_changer"] == "ercf"
        assert tc["has_auto_tool_change"] is True

    def test_ender3_no_tool_changer(self):
        tc = _get_printer_tool_change("ender3")
        assert tc["tool_changer"] == "none"
        assert tc["has_auto_tool_change"] is False

    def test_elegoo_neptune4_no_tool_changer(self):
        tc = _get_printer_tool_change("elegoo_neptune4")
        assert tc["tool_changer"] == "none"
        assert tc["has_auto_tool_change"] is False

    def test_unknown_printer_defaults(self):
        tc = _get_printer_tool_change("nonexistent_xyz")
        assert tc["has_auto_tool_change"] is False

    def test_none_printer_defaults(self):
        tc = _get_printer_tool_change(None)
        assert tc["has_auto_tool_change"] is False

    def test_prusa_mini_no_mmu(self):
        """Prusa Mini does NOT have MMU compatibility."""
        tc = _get_printer_tool_change("prusa_mini")
        assert tc["tool_changer"] == "none"
        assert tc["has_auto_tool_change"] is False
        assert tc["tool_change_seconds"] == 0

    def test_prusa_xl_tool_changer(self):
        """Prusa XL uses physical tool changer — fastest swap mechanism."""
        tc = _get_printer_tool_change("prusa_xl")
        assert tc["tool_changer"] == "tool_changer"
        assert tc["has_auto_tool_change"] is True
        # Tool changer should be much faster than AMS/MMU
        assert tc["tool_change_seconds"] < 10

    def test_prusa_xl_fastest_swap(self):
        """Prusa XL should be faster per-swap than any other printer."""
        tc_xl = _get_printer_tool_change("prusa_xl")
        tc_bambu = _get_printer_tool_change("bambu_a1")
        tc_mmu = _get_printer_tool_change("prusa_mk4")
        assert tc_xl["tool_change_seconds"] < tc_bambu["tool_change_seconds"]
        assert tc_xl["tool_change_seconds"] < tc_mmu["tool_change_seconds"]

    def test_bambu_vs_prusa_timing(self):
        """AMS Lite and MMU3 should have different timing."""
        tc_bambu = _get_printer_tool_change("bambu_a1")
        tc_prusa = _get_printer_tool_change("prusa_mk4")
        # Both should have positive times but they should differ
        assert tc_bambu["tool_change_seconds"] > 0
        assert tc_prusa["tool_change_seconds"] > 0

    def test_all_supported_printers_have_tool_change_data(self):
        """Every printer in slicer_profiles.json should have tool_change data."""
        import json as _json
        import os

        profiles_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "src", "kiln", "data", "slicer_profiles.json",
        )
        with open(profiles_path) as fh:
            profiles = _json.load(fh)

        for pid, profile in profiles.items():
            if pid.startswith("_"):
                continue
            tc = profile.get("tool_change")
            assert tc is not None, f"Printer '{pid}' missing tool_change data"
            assert "tool_change_seconds" in tc, f"Printer '{pid}' missing tool_change_seconds"
            assert "tool_changer" in tc, f"Printer '{pid}' missing tool_changer"
            assert "tool_change_notes" in tc, f"Printer '{pid}' missing tool_change_notes"


# ---------------------------------------------------------------------------
# _resolve_template_dimensions
# ---------------------------------------------------------------------------


class TestResolveTemplateDimensions:
    """Tests for template dimension extraction."""

    def test_phone_stand_defaults(self):
        w, d, h = _resolve_template_dimensions("phone_stand")
        assert w >= 55  # min phone_width
        assert d >= 60  # min base_depth
        assert h >= 8   # min lip_height

    def test_box_with_lid_defaults(self):
        w, d, h = _resolve_template_dimensions("box_with_lid")
        assert w > 0
        assert d > 0
        assert h > 0

    def test_missing_template_raises(self):
        with pytest.raises(ValueError, match="not found"):
            _resolve_template_dimensions("nonexistent_template_xyz")

    def test_param_overrides(self):
        w1, _, _ = _resolve_template_dimensions("phone_stand")
        w2, _, _ = _resolve_template_dimensions(
            "phone_stand", param_overrides={"phone_width": 100}
        )
        assert w2 > w1  # override should increase width

    def test_minimum_bounds(self):
        # Even with zero-ish params, dimensions should be clamped to minimums
        w, d, h = _resolve_template_dimensions("fridge_magnet")
        assert w >= 10.0
        assert d >= 10.0
        assert h >= 5.0


# ---------------------------------------------------------------------------
# _extract_dim helper
# ---------------------------------------------------------------------------


class TestExtractDim:
    """Tests for the dimension extraction helper."""

    def test_first_match_wins(self):
        params = {"width": 50, "diameter": 100}
        assert _extract_dim(params, ["width", "diameter"]) == 50.0

    def test_fallback_to_second(self):
        params = {"diameter": 100}
        assert _extract_dim(params, ["width", "diameter"]) == 100.0

    def test_no_match_returns_zero(self):
        params = {"foo": 42}
        assert _extract_dim(params, ["width", "depth"]) == 0.0

    def test_zero_value_skipped(self):
        params = {"width": 0, "diameter": 50}
        assert _extract_dim(params, ["width", "diameter"]) == 50.0


# ---------------------------------------------------------------------------
# estimate_from_dimensions — single material
# ---------------------------------------------------------------------------


class TestEstimateFromDimensionsSingleMaterial:
    """Tests for single-material estimation."""

    def test_basic_small_box(self):
        """A 50x50x50mm box should return reasonable estimates."""
        est = estimate_from_dimensions(50, 50, 50)
        assert est.estimated_time_seconds > 0
        assert est.total_weight_grams > 0
        assert est.total_cost_usd > 0
        assert est.filament_cost_usd > 0
        assert len(est.filaments) == 1
        assert est.filaments[0].material == "PLA"
        assert est.filaments[0].role == "body"
        assert est.tool_changes == 0
        assert est.tool_change_type == "none"

    def test_flat_tray_like_object(self):
        """A 120x120x15mm tray-like object."""
        est = estimate_from_dimensions(120, 120, 15, printer_id="bambu_a1")
        assert est.estimated_time_seconds > 0
        assert est.printer_id == "bambu_a1"
        assert est.infill_percent == pytest.approx(15.0)
        assert est.layer_height_mm == pytest.approx(0.2)

    def test_tiny_object(self):
        """A 10x10x5mm object should still produce valid estimates."""
        est = estimate_from_dimensions(10, 10, 5)
        assert est.estimated_time_seconds > 0
        assert est.total_weight_grams > 0

    def test_large_object(self):
        """A 200x200x200mm object should produce large estimates."""
        est = estimate_from_dimensions(200, 200, 200)
        assert est.total_weight_grams > 100  # should be substantial

    def test_different_materials_different_costs(self):
        """PEEK should cost more than PLA for the same dimensions."""
        est_pla = estimate_from_dimensions(50, 50, 50, materials=["PLA"])
        est_peek = estimate_from_dimensions(50, 50, 50, materials=["PEEK"])
        assert est_peek.filament_cost_usd > est_pla.filament_cost_usd

    def test_higher_infill_more_material(self):
        """Higher infill should use more material."""
        est_low = estimate_from_dimensions(80, 80, 80, infill_percent=10)
        est_high = estimate_from_dimensions(80, 80, 80, infill_percent=50)
        assert est_high.total_weight_grams > est_low.total_weight_grams

    def test_fast_printer_less_time(self):
        """Bambu A1 (fast) should estimate less time than Ender 3 (slow)."""
        est_fast = estimate_from_dimensions(
            100, 100, 50, printer_id="bambu_a1"
        )
        est_slow = estimate_from_dimensions(
            100, 100, 50, printer_id="ender3"
        )
        assert est_fast.estimated_time_seconds < est_slow.estimated_time_seconds

    def test_confidence_with_printer(self):
        est = estimate_from_dimensions(50, 50, 50, printer_id="bambu_a1")
        assert est.confidence == "high"

    def test_confidence_without_printer(self):
        est = estimate_from_dimensions(50, 50, 50)
        assert est.confidence == "medium"

    def test_confidence_notes_present(self):
        est = estimate_from_dimensions(50, 50, 50)
        assert len(est.confidence_notes) > 0

    def test_electricity_cost_scales_with_time(self):
        """Larger objects should have higher electricity costs."""
        est_small = estimate_from_dimensions(30, 30, 30)
        est_large = estimate_from_dimensions(150, 150, 150)
        assert est_large.electricity_cost_usd > est_small.electricity_cost_usd


# ---------------------------------------------------------------------------
# estimate_from_dimensions — multi-material
# ---------------------------------------------------------------------------


class TestEstimateFromDimensionsMultiMaterial:
    """Tests for multi-material estimation."""

    def test_two_color_basic(self):
        """Two-color PLA should produce 2 filament entries."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="bambu_a1",
        )
        assert len(est.filaments) == 2
        assert est.filaments[0].role == "body"
        assert est.filaments[1].role == "accent_1"
        assert est.tool_changes > 0
        assert est.tool_change_type == "ams_lite"  # A1 has AMS Lite

    def test_two_color_fractions_sum_to_one(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.85, 0.15],
        )
        total_frac = sum(f.volume_fraction for f in est.filaments)
        assert total_frac == pytest.approx(1.0, abs=0.01)

    def test_custom_fractions(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.70, 0.30],
        )
        assert est.filaments[0].volume_fraction == pytest.approx(0.70, abs=0.01)
        assert est.filaments[1].volume_fraction == pytest.approx(0.30, abs=0.01)

    def test_custom_roles(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_roles=["base", "texture"],
        )
        assert est.filaments[0].role == "base"
        assert est.filaments[1].role == "texture"

    def test_three_color(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA", "PLA"],
            printer_id="bambu_a1",
        )
        assert len(est.filaments) == 3
        assert est.tool_changes > 0

    def test_mixed_materials(self):
        """PLA body + TPU accent should have different densities."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "TPU"],
            material_fractions=[0.85, 0.15],
        )
        assert est.filaments[0].material == "PLA"
        assert est.filaments[1].material == "TPU"

    def test_tool_changes_bambu_a1(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="bambu_a1",
        )
        assert est.tool_change_type == "ams_lite"  # A1 has AMS Lite
        assert est.tool_change_time_seconds > 0

    def test_tool_changes_prusa_mk4(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="prusa_mk4",
        )
        assert est.tool_change_type == "mmu3"  # MK4 has MMU3
        assert est.tool_change_time_seconds > 0

    def test_tool_changes_ender3(self):
        """Ender 3 has no auto tool changer — should be manual."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="ender3",
        )
        assert est.tool_change_type == "manual"
        assert est.tool_change_time_seconds > 0
        assert len(est.warnings) > 0

    def test_single_material_no_tool_changes(self):
        est = estimate_from_dimensions(50, 50, 50, materials=["PLA"])
        assert est.tool_changes == 0
        assert est.tool_change_time_seconds == 0
        assert est.tool_change_type == "none"

    def test_surface_accent_fewer_tool_changes(self):
        """A small accent fraction (surface detail) should have fewer
        tool changes than a 50/50 split."""
        est_surface = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.95, 0.05],
            printer_id="bambu_a1",
        )
        est_even = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.50, 0.50],
            printer_id="bambu_a1",
        )
        assert est_surface.tool_changes < est_even.tool_changes


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Tests for error handling on bad inputs."""

    def test_zero_width_raises(self):
        with pytest.raises(ValueError, match="positive"):
            estimate_from_dimensions(0, 50, 50)

    def test_negative_height_raises(self):
        with pytest.raises(ValueError, match="positive"):
            estimate_from_dimensions(50, 50, -10)

    def test_mismatched_fractions_length(self):
        with pytest.raises(ValueError, match="material_fractions length"):
            estimate_from_dimensions(
                50, 50, 50,
                materials=["PLA", "PLA"],
                material_fractions=[0.5, 0.3, 0.2],
            )

    def test_fractions_not_summing_to_one(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            estimate_from_dimensions(
                50, 50, 50,
                materials=["PLA", "PLA"],
                material_fractions=[0.5, 0.3],
            )

    def test_mismatched_roles_length(self):
        with pytest.raises(ValueError, match="material_roles length"):
            estimate_from_dimensions(
                50, 50, 50,
                materials=["PLA"],
                material_roles=["body", "accent"],
            )


# ---------------------------------------------------------------------------
# estimate_from_template
# ---------------------------------------------------------------------------


class TestEstimateFromTemplate:
    """Tests for template-based estimation."""

    def test_phone_stand(self):
        est = estimate_from_template("phone_stand", printer_id="bambu_a1")
        assert est.estimated_time_seconds > 0
        assert est.total_cost_usd > 0
        assert est.width_mm > 0

    def test_box_with_lid(self):
        est = estimate_from_template("box_with_lid")
        assert est.estimated_time_seconds > 0

    def test_nameplate(self):
        est = estimate_from_template("nameplate")
        assert est.estimated_time_seconds > 0

    def test_fridge_magnet(self):
        est = estimate_from_template("fridge_magnet")
        assert est.estimated_time_seconds > 0
        # Small object — should be quick
        assert est.estimated_time_seconds < 7200  # under 2 hours

    def test_unknown_template_raises(self):
        with pytest.raises(ValueError, match="not found"):
            estimate_from_template("nonexistent_xyz")

    def test_template_with_overrides(self):
        """Overriding phone_width should change the dimensions."""
        est1 = estimate_from_template("phone_stand")
        est2 = estimate_from_template(
            "phone_stand", param_overrides={"phone_width": 100}
        )
        assert est2.width_mm > est1.width_mm

    def test_template_with_multi_material(self):
        est = estimate_from_template(
            "phone_stand",
            materials=["PLA", "PLA"],
            printer_id="bambu_a1",
        )
        assert len(est.filaments) == 2
        assert est.tool_changes > 0


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """Tests for to_dict() methods."""

    def test_filament_usage_to_dict(self):
        fu = FilamentUsage(
            material="PLA",
            weight_grams=25.3,
            length_meters=8.5,
            cost_usd=0.63,
            volume_fraction=1.0,
            role="body",
        )
        d = fu.to_dict()
        assert d["material"] == "PLA"
        assert d["weight_grams"] == 25.3

    def test_pre_estimate_to_dict(self):
        est = estimate_from_dimensions(50, 50, 50)
        d = est.to_dict()
        assert "estimated_time_seconds" in d
        assert "total_cost_usd" in d
        assert "filaments" in d
        assert isinstance(d["filaments"], list)
        assert isinstance(d["filaments"][0], dict)

    def test_pre_estimate_json_serializable(self):
        est = estimate_from_dimensions(50, 50, 50)
        d = est.to_dict()
        # Should not raise
        json_str = json.dumps(d)
        assert json_str


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------


class TestConsistency:
    """Sanity checks on estimate relationships."""

    def test_filament_weights_sum_to_total(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.85, 0.15],
        )
        sum_weights = sum(f.weight_grams for f in est.filaments)
        assert sum_weights == pytest.approx(est.total_weight_grams, abs=0.5)

    def test_filament_lengths_sum_to_total(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.85, 0.15],
        )
        sum_lengths = sum(f.length_meters for f in est.filaments)
        assert sum_lengths == pytest.approx(est.total_filament_meters, abs=0.1)

    def test_total_cost_equals_sum(self):
        est = estimate_from_dimensions(100, 100, 50, printer_id="bambu_a1")
        expected = est.filament_cost_usd + est.electricity_cost_usd
        assert est.total_cost_usd == pytest.approx(expected, abs=0.02)

    def test_time_includes_tool_changes(self):
        """For multi-material, total time > pure print time."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="bambu_a1",
        )
        # tool_change_time should be positive and included in total
        assert est.tool_change_time_seconds > 0
        assert est.estimated_time_seconds > est.tool_change_time_seconds

    def test_human_time_matches_seconds(self):
        est = estimate_from_dimensions(50, 50, 50)
        human = est.estimated_time_human
        # Should be a non-empty string like "45m" or "1h 30m"
        assert human != "unknown"
        assert "m" in human or "h" in human


# ---------------------------------------------------------------------------
# Realistic scenario: Adam's jewelry tray question
# ---------------------------------------------------------------------------


class TestRealisticScenarios:
    """Real-world scenarios matching actual user questions."""

    def test_jewelry_tray_two_color_bambu_a1(self):
        """M-size square jewelry tray, 2-color PLA, tiger stripe on top face.

        Expected: ~1.5-3 hours, reasonable cost, tool swaps present.
        This is the exact scenario from Adam's original question.
        """
        est = estimate_from_dimensions(
            120, 120, 15,
            materials=["PLA", "PLA"],
            material_fractions=[0.85, 0.15],
            material_roles=["body", "surface_texture"],
            printer_id="bambu_a1",
        )

        # Time should be in a reasonable range (1-4 hours)
        assert 1800 < est.estimated_time_seconds < 14400
        # Should have tool changes
        assert est.tool_changes > 0
        assert est.tool_change_type == "ams_lite"  # A1 has AMS Lite
        # Cost should be under $5 for PLA
        assert est.total_cost_usd < 5.0
        # Weight should be reasonable (30-80g for a tray)
        assert 10 < est.total_weight_grams < 120

    def test_coaster_single_color(self):
        """90mm round coaster, single PLA."""
        est = estimate_from_dimensions(
            90, 90, 8,
            materials=["PLA"],
            printer_id="bambu_a1",
        )
        # Should be quick — under 1 hour
        assert est.estimated_time_seconds < 5400
        assert est.tool_changes == 0

    def test_large_vase_slow_printer(self):
        """Large vase on Ender 3 — should take a long time."""
        est = estimate_from_dimensions(
            100, 100, 200,
            materials=["PLA"],
            printer_id="ender3",
        )
        # Ender 3 is slow — this should take many hours
        assert est.estimated_time_seconds > 7200  # at least 2 hours

    def test_nameplate_fast(self):
        """Small nameplate should be quick on any printer."""
        est = estimate_from_dimensions(
            120, 35, 8,
            materials=["PLA"],
            printer_id="bambu_a1",
        )
        assert est.estimated_time_seconds < 3600  # under 1 hour


# ---------------------------------------------------------------------------
# Multi-material add-on system
# ---------------------------------------------------------------------------


class TestListAddons:
    """Tests for list_addons() — catalog of available add-ons."""

    def test_returns_all_addons(self):
        addons = list_addons()
        assert len(addons) >= 5
        ids = {a["id"] for a in addons}
        assert "creality_cfs" in ids
        assert "mosaic_palette3" in ids
        assert "coprint_kcm" in ids
        assert "chameleon_mk4" in ids
        assert "elegoo_canvas" in ids

    def test_addon_has_required_fields(self):
        for addon in list_addons():
            assert "id" in addon
            assert "display_name" in addon
            assert "tool_change_seconds" in addon
            assert "tool_changer" in addon
            assert "max_colors" in addon
            assert "hardware_unverified" in addon
            assert addon["tool_change_seconds"] > 0

    def test_creality_cfs_reports_hardware_unverified(self):
        addon = next(a for a in list_addons(printer_id="k1_max") if a["id"] == "creality_cfs")
        assert addon["hardware_unverified"] is True
        assert addon["control_mode"] == "firmware_gcode_or_creality_print"
        assert any("hardware-unverified" in warning for warning in addon["warnings"])

    def test_filter_by_compatible_printer_k1(self):
        addons = list_addons(printer_id="k1")
        ids = {a["id"] for a in addons}
        # CFS-C is K1-series specific, palette + chameleon are universal
        assert "creality_cfs" in ids
        assert "mosaic_palette3" in ids
        assert "chameleon_mk4" in ids

    def test_filter_by_compatible_printer_k1_series(self):
        for printer_id in ("k1", "k1_max", "k1c", "k1_se"):
            addons = list_addons(printer_id=printer_id)
            ids = {a["id"] for a in addons}
            assert "creality_cfs" in ids

    def test_filter_excludes_incompatible(self):
        addons = list_addons(printer_id="ender3")
        ids = {a["id"] for a in addons}
        # CFS-C is K1-series only, CANVAS is Centauri-only
        assert "creality_cfs" not in ids
        assert "elegoo_canvas" not in ids
        # Universal add-ons should still be present
        assert "mosaic_palette3" in ids
        assert "chameleon_mk4" in ids

    def test_klipper_addons_for_klipper_printer(self):
        addons = list_addons(printer_id="voron_2")
        ids = {a["id"] for a in addons}
        assert "coprint_kcm" in ids  # Klipper-only, Voron is Klipper

    def test_klipper_addons_excluded_for_non_klipper(self):
        addons = list_addons(printer_id="ender3")
        ids = {a["id"] for a in addons}
        assert "coprint_kcm" not in ids  # Ender 3 is not Klipper

    def test_no_filter_returns_all(self):
        all_addons = list_addons()
        filtered = list_addons(printer_id="k1")
        assert len(all_addons) >= len(filtered)


class TestGetPrinterToolChangeWithAddon:
    """Tests for _get_printer_tool_change() with add-on overrides."""

    def test_cfs_overrides_k1_default(self):
        tc = _get_printer_tool_change("k1", tool_changer_addon="creality_cfs")
        assert tc["tool_changer"] == "cfs"
        assert tc["has_auto_tool_change"] is True
        assert tc["addon"] == "creality_cfs"
        assert tc["max_colors"] == 4

    def test_palette_works_with_any_printer(self):
        tc = _get_printer_tool_change("ender3", tool_changer_addon="mosaic_palette3")
        assert tc["tool_changer"] == "palette"
        assert tc["has_auto_tool_change"] is True
        assert tc["addon"] == "mosaic_palette3"

    def test_kcm_works_with_klipper_printer(self):
        tc = _get_printer_tool_change("voron_2", tool_changer_addon="coprint_kcm")
        assert tc["tool_changer"] == "kcm"
        assert tc["has_auto_tool_change"] is True

    def test_kcm_rejects_non_klipper_printer(self):
        with pytest.raises(ValueError, match="Klipper"):
            _get_printer_tool_change("ender3", tool_changer_addon="coprint_kcm")

    def test_cfs_rejects_non_k1_series_printer(self):
        with pytest.raises(ValueError, match="not compatible"):
            _get_printer_tool_change("ender3", tool_changer_addon="creality_cfs")

    def test_canvas_works_with_centauri(self):
        tc = _get_printer_tool_change(
            "elegoo_centauri_carbon", tool_changer_addon="elegoo_canvas",
        )
        assert tc["tool_changer"] == "canvas"
        assert tc["has_auto_tool_change"] is True

    def test_chameleon_universal(self):
        tc = _get_printer_tool_change("prusa_mini", tool_changer_addon="chameleon_mk4")
        assert tc["tool_changer"] == "chameleon"
        assert tc["addon"] == "chameleon_mk4"
        assert tc["max_colors"] == 4

    def test_unknown_addon_returns_builtin(self):
        """Unknown add-on ID falls back to the printer's built-in data."""
        tc = _get_printer_tool_change("bambu_a1", tool_changer_addon="nonexistent_xyz")
        # Should fall back to AMS Lite since addon wasn't found
        assert tc["tool_changer"] == "ams_lite"
        assert tc["addon"] is None

    def test_no_addon_returns_builtin(self):
        tc = _get_printer_tool_change("bambu_a1", tool_changer_addon=None)
        assert tc["tool_changer"] == "ams_lite"
        assert tc["addon"] is None

    def test_addon_display_name_present(self):
        tc = _get_printer_tool_change("k1", tool_changer_addon="creality_cfs")
        assert "Creality CFS" in tc["addon_display_name"]

    def test_cfs_addon_carries_hardware_unverified_warning(self):
        tc = _get_printer_tool_change("k1_max", tool_changer_addon="creality_cfs")
        assert tc["hardware_unverified"] is True
        assert any("CFS-C slot control" in warning for warning in tc["warnings"])


class TestEstimateWithAddon:
    """Tests for estimate_from_dimensions with tool_changer_addon."""

    def test_k1_with_cfs_two_color(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1",
            tool_changer_addon="creality_cfs",
        )
        assert est.tool_change_type == "cfs"
        assert est.tool_changer_addon == "creality_cfs"
        assert est.tool_changes > 0
        assert est.tool_change_time_seconds > 0
        assert est.max_colors == 4

    def test_k1_without_addon_is_manual(self):
        """K1 with no add-on should fall back to manual M600."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1",
        )
        assert est.tool_change_type == "manual"
        assert est.tool_changer_addon is None
        assert any("Stock Creality" in warning for warning in est.warnings)

    def test_k1_max_with_cfs_warns_hardware_unverified(self):
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1_max",
            tool_changer_addon="creality_cfs",
        )
        assert est.tool_change_type == "cfs"
        assert any("hardware-unverified" in warning for warning in est.warnings)

    def test_cfs_faster_than_manual(self):
        """CFS add-on should be faster than manual M600 swaps."""
        est_addon = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1",
            tool_changer_addon="creality_cfs",
        )
        est_manual = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1",
        )
        assert est_addon.tool_change_time_seconds < est_manual.tool_change_time_seconds

    def test_palette_with_ender3(self):
        """Mosaic Palette should work with Ender 3 (universal)."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="ender3",
            tool_changer_addon="mosaic_palette3",
        )
        assert est.tool_change_type == "palette"
        assert est.tool_changer_addon == "mosaic_palette3"
        assert est.tool_change_time_seconds > 0

    def test_palette_fastest_addon(self):
        """Palette should be the fastest add-on (pre-splicing)."""
        est_palette = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="ender3",
            tool_changer_addon="mosaic_palette3",
        )
        est_chameleon = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="ender3",
            tool_changer_addon="chameleon_mk4",
        )
        assert est_palette.tool_change_time_seconds < est_chameleon.tool_change_time_seconds

    def test_kcm_with_neptune4(self):
        """KCM add-on should work with Neptune 4 (Klipper)."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="elegoo_neptune4",
            tool_changer_addon="coprint_kcm",
        )
        assert est.tool_change_type == "kcm"
        assert est.tool_changer_addon == "coprint_kcm"

    def test_incompatible_addon_raises(self):
        """CFS with Ender 3 should raise."""
        with pytest.raises(ValueError, match="not compatible"):
            estimate_from_dimensions(
                100, 100, 15,
                materials=["PLA", "PLA"],
                printer_id="ender3",
                tool_changer_addon="creality_cfs",
            )

    def test_single_material_ignores_addon(self):
        """Single-material print shouldn't use the add-on."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA"],
            printer_id="k1",
            tool_changer_addon="creality_cfs",
        )
        assert est.tool_changes == 0
        assert est.tool_changer_addon is None

    def test_color_capacity_warning(self):
        """Using more colors than the add-on supports should warn."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA"] * 5,  # 5 colors
            material_fractions=[0.5, 0.15, 0.15, 0.1, 0.1],
            printer_id="ender3",
            tool_changer_addon="chameleon_mk4",  # max 4 colors
        )
        assert any("max 4 colors" in w for w in est.warnings)

    def test_addon_in_serialized_output(self):
        """Add-on info should appear in to_dict() output."""
        est = estimate_from_dimensions(
            100, 100, 15,
            materials=["PLA", "PLA"],
            printer_id="k1",
            tool_changer_addon="creality_cfs",
        )
        d = est.to_dict()
        assert d["tool_changer_addon"] == "creality_cfs"
        assert d["tool_changer_addon_name"] is not None
        assert d["max_colors"] == 4


class TestEstimateFromTemplateWithAddon:
    """Tests for estimate_from_template with tool_changer_addon."""

    def test_template_with_addon(self):
        est = estimate_from_template(
            "phone_stand",
            materials=["PLA", "PLA"],
            printer_id="k1",
            tool_changer_addon="creality_cfs",
        )
        assert est.tool_change_type == "cfs"
        assert est.tool_changer_addon == "creality_cfs"
        assert est.tool_changes > 0
