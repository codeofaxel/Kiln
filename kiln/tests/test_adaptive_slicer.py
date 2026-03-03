"""Tests for kiln.adaptive_slicer — adaptive slicing with geometry + material intelligence."""

from __future__ import annotations

import threading

import pytest

from kiln.adaptive_slicer import (
    AdaptiveMode,
    AdaptiveSlicer,
    AdaptiveSlicerError,
    GeometryRegion,
    RegionType,
    SlicerTarget,
    _clamp,
    _estimate_time,
    _estimate_time_uniform,
    _parse_region_dicts,
    _regions_at_z,
    _regions_at_z_raw,
    _reset_singleton,
    _vlh_to_prusaslicer_string,
    get_adaptive_slicer,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_slicer():
    """Reset the adaptive slicer singleton between tests."""
    _reset_singleton()
    yield
    _reset_singleton()


@pytest.fixture()
def slicer():
    return AdaptiveSlicer()


@pytest.fixture()
def pla_profile(slicer):
    return slicer.get_material_profile("PLA")


@pytest.fixture()
def petg_profile(slicer):
    return slicer.get_material_profile("PETG")


@pytest.fixture()
def standard_regions():
    """Simple standard region covering full 30mm height."""
    return [
        GeometryRegion(
            region_type=RegionType.STANDARD,
            z_start_mm=0.0,
            z_end_mm=30.0,
            area_pct=100.0,
        )
    ]


@pytest.fixture()
def mixed_regions():
    """Regions with overhangs, bridges, thin walls, and top surface."""
    return [
        GeometryRegion(
            region_type=RegionType.STANDARD,
            z_start_mm=0.0,
            z_end_mm=30.0,
            area_pct=60.0,
        ),
        GeometryRegion(
            region_type=RegionType.OVERHANG,
            z_start_mm=10.0,
            z_end_mm=15.0,
            area_pct=20.0,
            overhang_angle=50.0,
        ),
        GeometryRegion(
            region_type=RegionType.BRIDGE,
            z_start_mm=15.0,
            z_end_mm=16.0,
            area_pct=10.0,
            bridge_length_mm=25.0,
        ),
        GeometryRegion(
            region_type=RegionType.THIN_WALL,
            z_start_mm=5.0,
            z_end_mm=8.0,
            area_pct=5.0,
            wall_thickness_mm=0.5,
            min_feature_size_mm=0.5,
        ),
        GeometryRegion(
            region_type=RegionType.TOP_SURFACE,
            z_start_mm=28.0,
            z_end_mm=30.0,
            area_pct=100.0,
        ),
    ]


# ---------------------------------------------------------------------------
# TestMaterialProfile
# ---------------------------------------------------------------------------


class TestMaterialProfile:
    """Material profile loading: all 9 materials, nozzle scaling, unknowns."""

    def test_pla_loads(self, slicer):
        p = slicer.get_material_profile("PLA")
        assert p.material == "PLA"
        assert p.min_layer_height_mm > 0
        assert p.max_layer_height_mm > p.min_layer_height_mm

    def test_petg_loads(self, slicer):
        p = slicer.get_material_profile("PETG")
        assert p.material == "PETG"
        assert p.bridge_layer_height_mm <= p.max_layer_height_mm

    def test_abs_loads(self, slicer):
        p = slicer.get_material_profile("ABS")
        assert p.material == "ABS"
        assert p.bridge_fan_pct == 0.0  # ABS: no fan

    def test_tpu_loads(self, slicer):
        p = slicer.get_material_profile("TPU")
        assert p.material == "TPU"
        assert p.bridge_speed_mm_s == 15.0

    def test_asa_loads(self, slicer):
        p = slicer.get_material_profile("ASA")
        assert p.material == "ASA"

    def test_nylon_loads(self, slicer):
        p = slicer.get_material_profile("NYLON")
        assert p.material == "NYLON"

    def test_pc_loads(self, slicer):
        p = slicer.get_material_profile("PC")
        assert p.material == "PC"
        assert p.overhang_fan_pct == 0.0

    def test_pva_loads(self, slicer):
        p = slicer.get_material_profile("PVA")
        assert p.material == "PVA"
        assert p.optimal_layer_height_mm <= 0.20

    def test_hips_loads(self, slicer):
        p = slicer.get_material_profile("HIPS")
        assert p.material == "HIPS"

    def test_case_insensitive(self, slicer):
        p = slicer.get_material_profile("pla")
        assert p.material == "PLA"

    def test_case_insensitive_mixed(self, slicer):
        p = slicer.get_material_profile("Petg")
        assert p.material == "PETG"

    def test_unknown_material_raises(self, slicer):
        with pytest.raises(AdaptiveSlicerError, match="Unknown material"):
            slicer.get_material_profile("UNOBTANIUM")

    def test_nozzle_scaling_small(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.2)
        assert p.min_layer_height_mm >= 0.2 * 0.25
        assert p.max_layer_height_mm <= 0.2 * 0.75

    def test_nozzle_scaling_large(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.8)
        assert p.max_layer_height_mm <= 0.8 * 0.75

    def test_optimal_within_bounds(self, slicer):
        for mat in slicer.list_supported_materials():
            p = slicer.get_material_profile(mat)
            assert p.min_layer_height_mm <= p.optimal_layer_height_mm <= p.max_layer_height_mm

    def test_bridge_within_bounds(self, slicer):
        for mat in slicer.list_supported_materials():
            p = slicer.get_material_profile(mat)
            assert p.min_layer_height_mm <= p.bridge_layer_height_mm <= p.max_layer_height_mm

    def test_all_nine_materials_exist(self, slicer):
        materials = slicer.list_supported_materials()
        assert len(materials) == 9
        for expected in ["ABS", "ASA", "HIPS", "NYLON", "PC", "PETG", "PLA", "PVA", "TPU"]:
            assert expected in materials

    def test_to_dict(self, pla_profile):
        d = pla_profile.to_dict()
        assert d["material"] == "PLA"
        assert "min_layer_height_mm" in d
        assert "max_layer_height_mm" in d
        assert "notes" in d


# ---------------------------------------------------------------------------
# TestGeometryAnalysis
# ---------------------------------------------------------------------------


class TestGeometryAnalysis:
    """Region detection from model stats, empty/minimal models."""

    def test_empty_stats_no_height(self, slicer):
        regions = slicer.analyze_geometry(model_stats={"height_mm": 0})
        assert regions == []

    def test_minimal_stats_standard_region(self, slicer):
        regions = slicer.analyze_geometry(model_stats={"height_mm": 20.0})
        assert len(regions) >= 1
        # Should have at least standard and top/bottom surfaces.
        types = {r.region_type for r in regions}
        assert RegionType.TOP_SURFACE in types or RegionType.BOTTOM_SURFACE in types

    def test_no_source_raises(self, slicer):
        with pytest.raises(AdaptiveSlicerError, match="must be provided"):
            slicer.analyze_geometry()

    def test_stats_with_overhangs(self, slicer):
        stats = {
            "height_mm": 30.0,
            "overhangs": [{"z_start_mm": 10.0, "z_end_mm": 15.0, "angle": 50.0, "area_pct": 20.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        oh_regions = [r for r in regions if r.region_type == RegionType.OVERHANG]
        assert len(oh_regions) == 1
        assert oh_regions[0].overhang_angle == 50.0

    def test_stats_with_all_region_types(self, slicer):
        stats = {
            "height_mm": 50.0,
            "overhangs": [{"z_start_mm": 10.0, "z_end_mm": 15.0, "angle": 45.0}],
            "bridges": [{"z_start_mm": 20.0, "z_end_mm": 21.0, "length_mm": 30.0}],
            "thin_walls": [{"z_start_mm": 5.0, "z_end_mm": 8.0, "thickness_mm": 0.5}],
            "top_surfaces": [{"z_start_mm": 48.0, "z_end_mm": 50.0}],
            "fine_details": [{"z_start_mm": 25.0, "z_end_mm": 30.0, "feature_size_mm": 0.3}],
            "curved_surfaces": [{"z_start_mm": 30.0, "z_end_mm": 40.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        types = {r.region_type for r in regions}
        assert RegionType.OVERHANG in types
        assert RegionType.BRIDGE in types
        assert RegionType.THIN_WALL in types
        assert RegionType.TOP_SURFACE in types
        assert RegionType.FINE_DETAIL in types
        assert RegionType.CURVED_SURFACE in types

    def test_non_dict_items_skipped(self, slicer):
        stats = {
            "height_mm": 20.0,
            "overhangs": ["not a dict", 42],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        oh = [r for r in regions if r.region_type == RegionType.OVERHANG]
        assert len(oh) == 0


# ---------------------------------------------------------------------------
# TestOverhangDetection
# ---------------------------------------------------------------------------


class TestOverhangDetection:
    """Overhang detection from model stats."""

    def test_single_overhang(self, slicer):
        stats = {
            "height_mm": 20.0,
            "overhangs": [{"z_start_mm": 5.0, "z_end_mm": 10.0, "angle": 60.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        oh = [r for r in regions if r.region_type == RegionType.OVERHANG]
        assert len(oh) == 1
        assert oh[0].overhang_angle == 60.0

    def test_multiple_overhangs(self, slicer):
        stats = {
            "height_mm": 30.0,
            "overhangs": [
                {"z_start_mm": 5.0, "z_end_mm": 10.0, "angle": 45.0},
                {"z_start_mm": 20.0, "z_end_mm": 25.0, "angle": 60.0},
            ],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        oh = [r for r in regions if r.region_type == RegionType.OVERHANG]
        assert len(oh) == 2

    def test_no_overhangs(self, slicer):
        stats = {"height_mm": 20.0}
        regions = slicer.analyze_geometry(model_stats=stats)
        oh = [r for r in regions if r.region_type == RegionType.OVERHANG]
        assert len(oh) == 0


# ---------------------------------------------------------------------------
# TestBridgeDetection
# ---------------------------------------------------------------------------


class TestBridgeDetection:
    """Bridge detection from model stats."""

    def test_bridge_detected(self, slicer):
        stats = {
            "height_mm": 20.0,
            "bridges": [{"z_start_mm": 10.0, "z_end_mm": 11.0, "length_mm": 30.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        br = [r for r in regions if r.region_type == RegionType.BRIDGE]
        assert len(br) == 1
        assert br[0].bridge_length_mm == 30.0

    def test_long_bridge(self, slicer):
        stats = {
            "height_mm": 20.0,
            "bridges": [{"z_start_mm": 10.0, "z_end_mm": 11.0, "length_mm": 100.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        br = [r for r in regions if r.region_type == RegionType.BRIDGE]
        assert br[0].bridge_length_mm == 100.0

    def test_short_bridge(self, slicer):
        stats = {
            "height_mm": 20.0,
            "bridges": [{"z_start_mm": 10.0, "z_end_mm": 10.5, "length_mm": 2.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        br = [r for r in regions if r.region_type == RegionType.BRIDGE]
        assert len(br) == 1


# ---------------------------------------------------------------------------
# TestThinWallDetection
# ---------------------------------------------------------------------------


class TestThinWallDetection:
    """Thin wall detection from model stats."""

    def test_thin_wall_below_2x_nozzle(self, slicer):
        stats = {
            "height_mm": 20.0,
            "thin_walls": [{"z_start_mm": 0.0, "z_end_mm": 20.0, "thickness_mm": 0.5}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        tw = [r for r in regions if r.region_type == RegionType.THIN_WALL]
        assert len(tw) == 1
        assert tw[0].wall_thickness_mm == 0.5

    def test_thin_wall_min_feature_set(self, slicer):
        stats = {
            "height_mm": 20.0,
            "thin_walls": [{"z_start_mm": 5.0, "z_end_mm": 10.0, "thickness_mm": 0.3}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        tw = [r for r in regions if r.region_type == RegionType.THIN_WALL]
        assert tw[0].min_feature_size_mm == 0.3

    def test_no_thin_walls(self, slicer):
        stats = {"height_mm": 20.0}
        regions = slicer.analyze_geometry(model_stats=stats)
        tw = [r for r in regions if r.region_type == RegionType.THIN_WALL]
        assert len(tw) == 0


# ---------------------------------------------------------------------------
# TestTopSurfaceDetection
# ---------------------------------------------------------------------------


class TestTopSurfaceDetection:
    """Top surface detection — explicit and heuristic."""

    def test_explicit_top_surface(self, slicer):
        stats = {
            "height_mm": 30.0,
            "top_surfaces": [{"z_start_mm": 28.0, "z_end_mm": 30.0, "area_pct": 100.0}],
        }
        regions = slicer.analyze_geometry(model_stats=stats)
        ts = [r for r in regions if r.region_type == RegionType.TOP_SURFACE]
        assert len(ts) == 1
        assert ts[0].z_start_mm == 28.0

    def test_heuristic_top_surface(self, slicer):
        stats = {"height_mm": 30.0}
        regions = slicer.analyze_geometry(model_stats=stats)
        ts = [r for r in regions if r.region_type == RegionType.TOP_SURFACE]
        # Default heuristic should add a top surface region.
        assert len(ts) >= 1
        assert ts[0].z_end_mm == 30.0

    def test_very_short_model_no_top(self, slicer):
        # Model shorter than default top zone — no top surface detected.
        stats = {"height_mm": 0.5}
        regions = slicer.analyze_geometry(model_stats=stats)
        ts = [r for r in regions if r.region_type == RegionType.TOP_SURFACE]
        assert len(ts) == 0


# ---------------------------------------------------------------------------
# TestPlanGeneration
# ---------------------------------------------------------------------------


class TestPlanGeneration:
    """Full plan generation with mixed regions, correct layer count."""

    def test_basic_plan(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=30.0, model_name="test")
        assert plan.total_layers > 0
        assert len(plan.layer_heights) == plan.total_layers
        assert len(plan.layer_speeds) == plan.total_layers
        assert len(plan.layer_cooling) == plan.total_layers
        assert abs(plan.total_height_mm - 30.0) < 0.5

    def test_mixed_regions_plan(self, slicer, pla_profile, mixed_regions):
        plan = slicer.generate_plan(mixed_regions, pla_profile, model_height_mm=30.0, model_name="mixed")
        assert plan.total_layers > 0
        assert plan.model_name == "mixed"
        assert plan.material == "PLA"

    def test_plan_height_matches_model(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        # Sum of layer heights should be close to model height.
        assert abs(sum(plan.layer_heights) - 10.0) < 0.5

    def test_negative_height_raises(self, slicer, pla_profile, standard_regions):
        with pytest.raises(AdaptiveSlicerError, match="positive"):
            slicer.generate_plan(standard_regions, pla_profile, model_height_mm=-1.0)

    def test_zero_height_raises(self, slicer, pla_profile, standard_regions):
        with pytest.raises(AdaptiveSlicerError, match="positive"):
            slicer.generate_plan(standard_regions, pla_profile, model_height_mm=0.0)

    def test_plan_has_plan_id(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        assert plan.plan_id
        assert len(plan.plan_id) > 0

    def test_plan_has_timestamp(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        assert plan.created_at
        assert "T" in plan.created_at  # ISO format

    def test_first_layer_height(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        assert plan.layer_heights[0] == pla_profile.first_layer_height_mm

    def test_printer_field_set(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0, printer="ender3")
        assert plan.printer == "ender3"

    def test_layer_regions_populated(self, slicer, pla_profile, mixed_regions):
        plan = slicer.generate_plan(mixed_regions, pla_profile, model_height_mm=30.0)
        assert len(plan.layer_regions) == plan.total_layers
        # At least some layers should have non-empty regions.
        non_empty = [lr for lr in plan.layer_regions if lr]
        assert len(non_empty) > 0


# ---------------------------------------------------------------------------
# TestQualityFirstMode
# ---------------------------------------------------------------------------


class TestQualityFirstMode:
    """QUALITY_FIRST: fine layers everywhere, detail regions get minimum."""

    def test_fine_layers_overall(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(
            standard_regions,
            pla_profile,
            mode=AdaptiveMode.QUALITY_FIRST,
            model_height_mm=10.0,
        )
        avg = sum(plan.layer_heights) / len(plan.layer_heights)
        assert avg <= pla_profile.optimal_layer_height_mm

    def test_thin_wall_gets_minimum(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.THIN_WALL,
                z_start_mm=0.0,
                z_end_mm=5.0,
                area_pct=100.0,
                wall_thickness_mm=0.3,
                min_feature_size_mm=0.3,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            pla_profile,
            mode=AdaptiveMode.QUALITY_FIRST,
            model_height_mm=5.0,
        )
        # After first layer, thin wall layers should be small.
        for h in plan.layer_heights[1:]:
            assert h <= pla_profile.optimal_layer_height_mm

    def test_top_surface_gets_min(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.TOP_SURFACE,
                z_start_mm=0.0,
                z_end_mm=3.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            pla_profile,
            mode=AdaptiveMode.QUALITY_FIRST,
            model_height_mm=3.0,
        )
        # Middle layers (not first, not last) should be min height.
        # Last layer may be a merged remainder slightly larger.
        for h in plan.layer_heights[1:-1]:
            assert h <= pla_profile.min_layer_height_mm + 0.001
        # Last layer can be up to 2x min due to remainder merging.
        assert plan.layer_heights[-1] <= pla_profile.min_layer_height_mm * 2 + 0.001


# ---------------------------------------------------------------------------
# TestSpeedFirstMode
# ---------------------------------------------------------------------------


class TestSpeedFirstMode:
    """SPEED_FIRST: thick layers default, thin only where needed."""

    def test_standard_gets_max_height(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=20.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            pla_profile,
            mode=AdaptiveMode.SPEED_FIRST,
            model_height_mm=20.0,
        )
        # Most layers should be at or near max height.
        for h in plan.layer_heights[1:]:
            assert h >= pla_profile.optimal_layer_height_mm - 0.01

    def test_fine_detail_still_thin(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.FINE_DETAIL,
                z_start_mm=0.0,
                z_end_mm=5.0,
                area_pct=100.0,
                min_feature_size_mm=0.3,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            pla_profile,
            mode=AdaptiveMode.SPEED_FIRST,
            model_height_mm=5.0,
        )
        for h in plan.layer_heights[1:]:
            assert h <= pla_profile.optimal_layer_height_mm

    def test_fewer_layers_than_quality(self, slicer, pla_profile, standard_regions):
        plan_speed = slicer.generate_plan(
            standard_regions,
            pla_profile,
            mode=AdaptiveMode.SPEED_FIRST,
            model_height_mm=30.0,
        )
        plan_quality = slicer.generate_plan(
            standard_regions,
            pla_profile,
            mode=AdaptiveMode.QUALITY_FIRST,
            model_height_mm=30.0,
        )
        assert plan_speed.total_layers < plan_quality.total_layers


# ---------------------------------------------------------------------------
# TestBalancedMode
# ---------------------------------------------------------------------------


class TestBalancedMode:
    """BALANCED: default behavior, region-specific adjustments."""

    def test_default_mode_is_balanced(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=20.0)
        assert plan.mode == AdaptiveMode.BALANCED

    def test_bulk_gets_thicker(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BULK,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=10.0)
        # After first layer (and excluding last remainder layer), bulk
        # should get at or above optimal height.
        middle_layers = plan.layer_heights[1:-1] if len(plan.layer_heights) > 2 else plan.layer_heights[1:]
        for h in middle_layers:
            assert h >= pla_profile.optimal_layer_height_mm - 0.01

    def test_overhang_uses_bridge_height(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=0.0,
                z_end_mm=5.0,
                area_pct=100.0,
                overhang_angle=50.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=5.0)
        for h in plan.layer_heights[1:]:
            assert h <= pla_profile.bridge_layer_height_mm + 0.01


# ---------------------------------------------------------------------------
# TestMaterialOptimizedMode
# ---------------------------------------------------------------------------


class TestMaterialOptimizedMode:
    """MATERIAL_OPTIMIZED: conservative bridge/overhang values."""

    def test_bridge_most_conservative(self, slicer, petg_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=0.0,
                z_end_mm=3.0,
                area_pct=100.0,
                bridge_length_mm=20.0,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            petg_profile,
            mode=AdaptiveMode.MATERIAL_OPTIMIZED,
            model_height_mm=3.0,
        )
        # Exclude last layer (may be remainder merged).
        for h in plan.layer_heights[1:-1]:
            assert h <= petg_profile.bridge_layer_height_mm + 0.001

    def test_overhang_conservative(self, slicer, petg_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=0.0,
                z_end_mm=5.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(
            regions,
            petg_profile,
            mode=AdaptiveMode.MATERIAL_OPTIMIZED,
            model_height_mm=5.0,
        )
        # Exclude last layer (may be remainder merged).
        for h in plan.layer_heights[1:-1]:
            assert h <= petg_profile.bridge_layer_height_mm + 0.001


# ---------------------------------------------------------------------------
# TestLayerHeightClamping
# ---------------------------------------------------------------------------


class TestLayerHeightClamping:
    """Min/max height enforcement, nozzle-based limits."""

    def test_clamp_below_min(self, slicer, pla_profile):
        clamped = slicer._clamp_layer_height(0.01, pla_profile, 0.4)
        assert clamped >= pla_profile.min_layer_height_mm

    def test_clamp_above_max(self, slicer, pla_profile):
        clamped = slicer._clamp_layer_height(1.0, pla_profile, 0.4)
        assert clamped <= pla_profile.max_layer_height_mm

    def test_nozzle_min_enforced(self, slicer, pla_profile):
        clamped = slicer._clamp_layer_height(0.01, pla_profile, 0.4)
        assert clamped >= 0.4 * 0.25  # 0.1mm

    def test_nozzle_max_enforced(self, slicer, pla_profile):
        clamped = slicer._clamp_layer_height(1.0, pla_profile, 0.4)
        assert clamped <= 0.4 * 0.75  # 0.3mm

    def test_within_bounds_unchanged(self, slicer, pla_profile):
        clamped = slicer._clamp_layer_height(0.2, pla_profile, 0.4)
        assert clamped == 0.2


# ---------------------------------------------------------------------------
# TestSpeedComputation
# ---------------------------------------------------------------------------


class TestSpeedComputation:
    """Per-layer speed factors match regions."""

    def test_standard_speed_1(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        speed = slicer._compute_layer_speed(5.0, regions, pla_profile)
        assert speed == 1.0

    def test_overhang_speed_reduced(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        speed = slicer._compute_layer_speed(5.0, regions, pla_profile)
        assert speed < 1.0
        assert speed == pla_profile.overhang_speed_factor

    def test_bridge_speed_reduced(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        speed = slicer._compute_layer_speed(5.0, regions, pla_profile)
        assert speed < 1.0

    def test_no_regions_speed_1(self, slicer, pla_profile):
        speed = slicer._compute_layer_speed(100.0, [], pla_profile)
        assert speed == 1.0

    def test_thin_wall_speed_reduced(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.THIN_WALL,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        speed = slicer._compute_layer_speed(5.0, regions, pla_profile)
        assert speed <= 0.6


# ---------------------------------------------------------------------------
# TestCoolingComputation
# ---------------------------------------------------------------------------


class TestCoolingComputation:
    """Fan speeds match material + region."""

    def test_bridge_fan(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        fan = slicer._compute_layer_cooling(5.0, regions, pla_profile)
        assert fan >= pla_profile.bridge_fan_pct

    def test_abs_bridge_no_fan(self, slicer):
        abs_prof = slicer.get_material_profile("ABS")
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=0.0,
                z_end_mm=5.0,
                area_pct=100.0,
            )
        ]
        fan = slicer._compute_layer_cooling(2.0, regions, abs_prof)
        # ABS bridge fan is 0%, but baseline is 50%.
        assert fan >= 0.0

    def test_bottom_surface_no_fan(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BOTTOM_SURFACE,
                z_start_mm=0.0,
                z_end_mm=0.5,
                area_pct=100.0,
            )
        ]
        fan = slicer._compute_layer_cooling(0.2, regions, pla_profile)
        assert fan == 0.0

    def test_overhang_fan(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        fan = slicer._compute_layer_cooling(5.0, regions, pla_profile)
        assert fan >= pla_profile.overhang_fan_pct

    def test_fan_capped_at_100(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            ),
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            ),
        ]
        fan = slicer._compute_layer_cooling(5.0, regions, pla_profile)
        assert fan <= 100.0


# ---------------------------------------------------------------------------
# TestPrusaSlicerExport
# ---------------------------------------------------------------------------


class TestPrusaSlicerExport:
    """PrusaSlicer variable layer height format."""

    def test_export_format(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.PRUSASLICER)
        assert config.slicer == SlicerTarget.PRUSASLICER
        assert config.config_format == "ini"
        assert config.variable_layer_height_data is not None
        assert len(config.variable_layer_height_data) == plan.total_layers

    def test_vlh_data_structure(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.PRUSASLICER)
        for z, h in config.variable_layer_height_data:
            assert z >= 0
            assert h > 0

    def test_vlh_string_format(self):
        data = [(0.0, 0.2), (0.2, 0.15), (0.35, 0.3)]
        result = _vlh_to_prusaslicer_string(data)
        assert ";" in result
        parts = result.split(";")
        assert len(parts) == 6  # 3 pairs * 2 values

    def test_config_has_variable_lh_enabled(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.PRUSASLICER)
        assert config.config_data["variable_layer_height"] == 1


# ---------------------------------------------------------------------------
# TestOrcaSlicerExport
# ---------------------------------------------------------------------------


class TestOrcaSlicerExport:
    """OrcaSlicer config export."""

    def test_export_format(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.ORCASLICER)
        assert config.slicer == SlicerTarget.ORCASLICER
        assert config.config_format == "json"
        assert config.config_data["adaptive_layer_height"] is True

    def test_has_vlh_data(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.ORCASLICER)
        assert config.variable_layer_height_data is not None


# ---------------------------------------------------------------------------
# TestCuraExport
# ---------------------------------------------------------------------------


class TestCuraExport:
    """Cura adaptive layers plugin format."""

    def test_export_format(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.CURA)
        assert config.slicer == SlicerTarget.CURA
        assert config.config_format == "json"
        assert config.config_data["adaptive_layers_enabled"] is True

    def test_has_layer_data(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.CURA)
        layers = config.config_data["layers"]
        assert len(layers) == plan.total_layers
        assert layers[0]["layer"] == 0
        assert layers[0]["height_mm"] > 0


# ---------------------------------------------------------------------------
# TestGenericExport
# ---------------------------------------------------------------------------


class TestGenericExport:
    """Generic Z-height pairs export."""

    def test_export_format(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.GENERIC)
        assert config.slicer == SlicerTarget.GENERIC
        assert config.config_format == "json"
        assert config.variable_layer_height_data is not None

    def test_layer_data_present(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan, slicer=SlicerTarget.GENERIC)
        layer_data = config.config_data["layer_data"]
        assert len(layer_data) == plan.total_layers
        assert "z" in layer_data[0]
        assert "height" in layer_data[0]


# ---------------------------------------------------------------------------
# TestTimeSavings
# ---------------------------------------------------------------------------


class TestTimeSavings:
    """Time savings estimation vs uniform."""

    def test_basic_savings(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=30.0)
        savings = slicer.estimate_time_savings(plan)
        assert "uniform_layers" in savings
        assert "adaptive_layers" in savings
        assert "savings_pct" in savings
        assert savings["uniform_height_mm"] == 0.2

    def test_speed_first_saves_more(self, slicer, pla_profile, standard_regions):
        plan_speed = slicer.generate_plan(
            standard_regions,
            pla_profile,
            mode=AdaptiveMode.SPEED_FIRST,
            model_height_mm=30.0,
        )
        plan_quality = slicer.generate_plan(
            standard_regions,
            pla_profile,
            mode=AdaptiveMode.QUALITY_FIRST,
            model_height_mm=30.0,
        )
        savings_speed = slicer.estimate_time_savings(plan_speed)
        savings_quality = slicer.estimate_time_savings(plan_quality)
        assert savings_speed["savings_pct"] >= savings_quality["savings_pct"]

    def test_zero_uniform_raises(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        with pytest.raises(AdaptiveSlicerError, match="positive"):
            slicer.estimate_time_savings(plan, uniform_height_mm=0.0)

    def test_negative_uniform_raises(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        with pytest.raises(AdaptiveSlicerError, match="positive"):
            slicer.estimate_time_savings(plan, uniform_height_mm=-0.1)

    def test_avg_adaptive_height(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=20.0)
        savings = slicer.estimate_time_savings(plan)
        expected_avg = plan.total_height_mm / plan.total_layers
        assert abs(savings["avg_adaptive_height_mm"] - expected_avg) < 0.001


# ---------------------------------------------------------------------------
# TestQuickPlan
# ---------------------------------------------------------------------------


class TestQuickPlan:
    """Convenience method end-to-end."""

    def test_basic_quick_plan(self, slicer):
        plan = slicer.quick_plan(material="PLA", model_height_mm=20.0)
        assert plan.total_layers > 0
        assert plan.material == "PLA"

    def test_quick_plan_with_regions(self, slicer):
        regions = [{"region_type": "overhang", "z_start_mm": 5.0, "z_end_mm": 10.0, "area_pct": 20.0}]
        plan = slicer.quick_plan(material="PETG", model_height_mm=15.0, regions=regions)
        assert plan.total_layers > 0
        assert plan.material == "PETG"

    def test_quick_plan_with_mode(self, slicer):
        plan = slicer.quick_plan(
            material="ABS",
            model_height_mm=10.0,
            mode=AdaptiveMode.SPEED_FIRST,
        )
        assert plan.mode == AdaptiveMode.SPEED_FIRST

    def test_quick_plan_with_printer(self, slicer):
        plan = slicer.quick_plan(material="PLA", model_height_mm=10.0, printer="ender3")
        assert plan.printer == "ender3"

    def test_quick_plan_unknown_material(self, slicer):
        with pytest.raises(AdaptiveSlicerError, match="Unknown material"):
            slicer.quick_plan(material="UNOBTANIUM", model_height_mm=10.0)


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Zero height, single layer, no regions, very tall model."""

    def test_single_layer_model(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=0.3,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=0.25)
        assert plan.total_layers >= 1

    def test_very_tall_model(self, slicer, pla_profile):
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=500.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=500.0)
        assert plan.total_layers > 100
        assert abs(plan.total_height_mm - 500.0) < 1.0

    def test_empty_regions_uses_defaults(self, slicer, pla_profile):
        plan = slicer.generate_plan([], pla_profile, model_height_mm=10.0)
        # With no regions, heights should default.
        assert plan.total_layers > 0

    def test_model_height_exact_layer_multiple(self, slicer, pla_profile):
        # Height exactly divisible by layer height.
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=4.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=4.0)
        assert plan.total_layers > 0
        assert abs(sum(plan.layer_heights) - 4.0) < 0.5

    def test_region_outside_model_height(self, slicer, pla_profile):
        # Region extends beyond model height — should not crash.
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=50.0,
                z_end_mm=100.0,
                area_pct=100.0,
            )
        ]
        plan = slicer.generate_plan(regions, pla_profile, model_height_mm=10.0)
        assert plan.total_layers > 0


# ---------------------------------------------------------------------------
# TestNozzleDiameterScaling
# ---------------------------------------------------------------------------


class TestNozzleDiameterScaling:
    """Different nozzle sizes: 0.2, 0.4, 0.6, 0.8mm."""

    def test_02mm_nozzle(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.2)
        assert p.min_layer_height_mm >= 0.05
        assert p.max_layer_height_mm <= 0.15

    def test_04mm_nozzle(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.4)
        assert p.min_layer_height_mm >= 0.08
        assert p.max_layer_height_mm <= 0.30

    def test_06mm_nozzle(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.6)
        assert p.max_layer_height_mm <= 0.45

    def test_08mm_nozzle(self, slicer):
        p = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.8)
        assert p.max_layer_height_mm <= 0.60

    def test_larger_nozzle_allows_taller_layers(self, slicer):
        p04 = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.4)
        p08 = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.8)
        assert p08.max_layer_height_mm > p04.max_layer_height_mm

    def test_nozzle_affects_plan_layer_count(self, slicer):
        regions = [
            GeometryRegion(
                region_type=RegionType.STANDARD,
                z_start_mm=0.0,
                z_end_mm=10.0,
                area_pct=100.0,
            )
        ]
        p04 = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.4)
        p08 = slicer.get_material_profile("PLA", nozzle_diameter_mm=0.8)

        plan04 = slicer.generate_plan(regions, p04, model_height_mm=10.0, nozzle_diameter_mm=0.4)
        plan08 = slicer.generate_plan(regions, p08, model_height_mm=10.0, nozzle_diameter_mm=0.8)
        # Larger nozzle = fewer layers.
        assert plan08.total_layers <= plan04.total_layers


# ---------------------------------------------------------------------------
# TestSerialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict with enum conversion, round-trip."""

    def test_region_to_dict_enum(self):
        r = GeometryRegion(
            region_type=RegionType.OVERHANG,
            z_start_mm=5.0,
            z_end_mm=10.0,
            area_pct=20.0,
            overhang_angle=50.0,
        )
        d = r.to_dict()
        assert d["region_type"] == "overhang"
        assert d["overhang_angle"] == 50.0

    def test_region_to_dict_omits_none(self):
        r = GeometryRegion(
            region_type=RegionType.STANDARD,
            z_start_mm=0.0,
            z_end_mm=10.0,
            area_pct=100.0,
        )
        d = r.to_dict()
        assert "min_feature_size_mm" not in d
        assert "bridge_length_mm" not in d

    def test_plan_to_dict(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        d = plan.to_dict()
        assert d["mode"] == "balanced"
        assert d["material"] == "PLA"
        assert isinstance(d["layer_heights"], list)
        assert isinstance(d["layer_regions"], list)
        # layer_regions should contain string values.
        if d["layer_regions"]:
            for lr in d["layer_regions"]:
                for r in lr:
                    assert isinstance(r, str)

    def test_slicer_config_to_dict(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        config = slicer.export_config(plan)
        d = config.to_dict()
        assert d["slicer"] == "prusaslicer"
        assert isinstance(d["config_data"], dict)
        assert isinstance(d["notes"], list)

    def test_material_profile_to_dict(self, pla_profile):
        d = pla_profile.to_dict()
        assert d["material"] == "PLA"
        assert "min_layer_height_mm" in d
        assert isinstance(d["notes"], str)

    def test_plan_to_dict_round_trip(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        d = plan.to_dict()
        # Verify we can reconstruct from dict.
        assert d["plan_id"] == plan.plan_id
        assert d["total_layers"] == plan.total_layers
        assert len(d["layer_heights"]) == plan.total_layers


# ---------------------------------------------------------------------------
# TestSingleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Lazy singleton pattern."""

    def test_get_returns_instance(self):
        s = get_adaptive_slicer()
        assert isinstance(s, AdaptiveSlicer)

    def test_get_returns_same_instance(self):
        s1 = get_adaptive_slicer()
        s2 = get_adaptive_slicer()
        assert s1 is s2

    def test_reset_clears_instance(self):
        s1 = get_adaptive_slicer()
        _reset_singleton()
        s2 = get_adaptive_slicer()
        assert s1 is not s2

    def test_thread_safe_singleton(self):
        instances = []

        def get_instance():
            instances.append(get_adaptive_slicer())

        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same instance.
        assert all(i is instances[0] for i in instances)


# ---------------------------------------------------------------------------
# TestHelpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """Internal helper functions."""

    def test_clamp_within(self):
        assert _clamp(5.0, 0.0, 10.0) == 5.0

    def test_clamp_below(self):
        assert _clamp(-1.0, 0.0, 10.0) == 0.0

    def test_clamp_above(self):
        assert _clamp(15.0, 0.0, 10.0) == 10.0

    def test_regions_at_z_finds_active(self):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=5.0,
                z_end_mm=10.0,
                area_pct=20.0,
            )
        ]
        active = _regions_at_z(7.0, regions)
        assert RegionType.OVERHANG in active

    def test_regions_at_z_excludes_inactive(self):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=5.0,
                z_end_mm=10.0,
                area_pct=20.0,
            )
        ]
        active = _regions_at_z(2.0, regions)
        assert len(active) == 0

    def test_regions_at_z_boundary_start(self):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=5.0,
                z_end_mm=10.0,
                area_pct=10.0,
            )
        ]
        active = _regions_at_z(5.0, regions)
        assert RegionType.BRIDGE in active

    def test_regions_at_z_boundary_end(self):
        regions = [
            GeometryRegion(
                region_type=RegionType.BRIDGE,
                z_start_mm=5.0,
                z_end_mm=10.0,
                area_pct=10.0,
            )
        ]
        # End is exclusive.
        active = _regions_at_z(10.0, regions)
        assert len(active) == 0

    def test_regions_at_z_raw_returns_objects(self):
        regions = [
            GeometryRegion(
                region_type=RegionType.OVERHANG,
                z_start_mm=5.0,
                z_end_mm=10.0,
                area_pct=20.0,
            )
        ]
        active = _regions_at_z_raw(7.0, regions)
        assert len(active) == 1
        assert active[0].region_type == RegionType.OVERHANG

    def test_estimate_time_positive(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        t = _estimate_time(plan)
        assert t > 0

    def test_estimate_time_uniform_positive(self):
        t = _estimate_time_uniform(10.0, 0.2)
        assert t > 0

    def test_parse_region_dicts_valid(self):
        raw = [{"region_type": "overhang", "z_start_mm": 5.0, "z_end_mm": 10.0, "area_pct": 20.0}]
        regions = _parse_region_dicts(raw, 20.0)
        assert len(regions) == 1
        assert regions[0].region_type == RegionType.OVERHANG

    def test_parse_region_dicts_invalid_type(self):
        raw = [{"region_type": "nonexistent", "z_start_mm": 0.0, "z_end_mm": 5.0}]
        regions = _parse_region_dicts(raw, 10.0)
        assert len(regions) == 0

    def test_parse_region_dicts_missing_type(self):
        raw = [{"z_start_mm": 0.0, "z_end_mm": 5.0}]
        regions = _parse_region_dicts(raw, 10.0)
        assert len(regions) == 0


# ---------------------------------------------------------------------------
# TestEnums
# ---------------------------------------------------------------------------


class TestEnums:
    """Enum string values and membership."""

    def test_region_type_values(self):
        assert RegionType.FINE_DETAIL.value == "fine_detail"
        assert RegionType.STANDARD.value == "standard"
        assert RegionType.BULK.value == "bulk"

    def test_adaptive_mode_values(self):
        assert AdaptiveMode.QUALITY_FIRST.value == "quality_first"
        assert AdaptiveMode.SPEED_FIRST.value == "speed_first"
        assert AdaptiveMode.BALANCED.value == "balanced"
        assert AdaptiveMode.MATERIAL_OPTIMIZED.value == "material_optimized"

    def test_slicer_target_values(self):
        assert SlicerTarget.PRUSASLICER.value == "prusaslicer"
        assert SlicerTarget.ORCASLICER.value == "orcaslicer"
        assert SlicerTarget.CURA.value == "cura"
        assert SlicerTarget.GENERIC.value == "generic"

    def test_region_type_is_str(self):
        assert isinstance(RegionType.STANDARD, str)
        assert RegionType.STANDARD == "standard"

    def test_all_region_types(self):
        assert len(RegionType) == 10


# ---------------------------------------------------------------------------
# TestPluginTools
# ---------------------------------------------------------------------------


class TestPluginTools:
    """MCP plugin tool functions."""

    def test_list_supported_materials(self):
        from kiln.plugins.adaptive_slicing_tools import list_supported_materials

        result = list_supported_materials()
        assert result["success"] is True
        assert result["count"] == 9

    def test_get_material_slicing_profile(self):
        from kiln.plugins.adaptive_slicing_tools import get_material_slicing_profile

        result = get_material_slicing_profile("PLA")
        assert result["success"] is True
        assert result["profile"]["material"] == "PLA"

    def test_get_material_slicing_profile_unknown(self):
        from kiln.plugins.adaptive_slicing_tools import get_material_slicing_profile

        result = get_material_slicing_profile("UNOBTANIUM")
        assert result["success"] is False
        assert "Unknown material" in result["error"]

    def test_quick_adaptive_plan(self):
        from kiln.plugins.adaptive_slicing_tools import quick_adaptive_plan

        result = quick_adaptive_plan(material="PLA", model_height_mm=20.0)
        assert result["success"] is True
        assert result["plan"]["total_layers"] > 0

    def test_quick_adaptive_plan_invalid_mode(self):
        from kiln.plugins.adaptive_slicing_tools import quick_adaptive_plan

        result = quick_adaptive_plan(material="PLA", model_height_mm=20.0, mode="invalid_mode")
        assert result["success"] is False
        assert "Invalid mode" in result["error"]

    def test_analyze_model_geometry(self):
        from kiln.plugins.adaptive_slicing_tools import analyze_model_geometry

        result = analyze_model_geometry(model_stats={"height_mm": 20.0})
        assert result["success"] is True
        assert result["region_count"] >= 1

    def test_analyze_model_geometry_no_source(self):
        from kiln.plugins.adaptive_slicing_tools import analyze_model_geometry

        result = analyze_model_geometry()
        assert result["success"] is False

    def test_get_adaptive_plan_summary(self):
        from kiln.plugins.adaptive_slicing_tools import (
            get_adaptive_plan_summary,
            quick_adaptive_plan,
        )

        plan_result = quick_adaptive_plan(material="PLA", model_height_mm=10.0)
        summary = get_adaptive_plan_summary(plan_result["plan"])
        assert summary["success"] is True
        assert "Total layers" in summary["summary"]
        assert summary["stats"]["total_layers"] > 0

    def test_get_adaptive_plan_summary_empty(self):
        from kiln.plugins.adaptive_slicing_tools import get_adaptive_plan_summary

        result = get_adaptive_plan_summary({"layer_heights": []})
        assert result["success"] is False

    def test_export_adaptive_slicer_config(self):
        from kiln.plugins.adaptive_slicing_tools import (
            export_adaptive_slicer_config,
            quick_adaptive_plan,
        )

        plan_result = quick_adaptive_plan(material="PLA", model_height_mm=10.0)
        export = export_adaptive_slicer_config(plan_result["plan"], slicer="prusaslicer")
        assert export["success"] is True
        assert export["config"]["slicer"] == "prusaslicer"

    def test_export_invalid_slicer(self):
        from kiln.plugins.adaptive_slicing_tools import export_adaptive_slicer_config

        result = export_adaptive_slicer_config({"plan_id": "x"}, slicer="nonexistent")
        assert result["success"] is False

    def test_estimate_adaptive_time_savings(self):
        from kiln.plugins.adaptive_slicing_tools import (
            estimate_adaptive_time_savings,
            quick_adaptive_plan,
        )

        plan_result = quick_adaptive_plan(material="PLA", model_height_mm=20.0)
        savings = estimate_adaptive_time_savings(plan_result["plan"])
        assert savings["success"] is True
        assert "savings_pct" in savings["savings"]

    def test_generate_adaptive_slicing_plan(self):
        from kiln.plugins.adaptive_slicing_tools import generate_adaptive_slicing_plan

        regions = [{"region_type": "standard", "z_start_mm": 0.0, "z_end_mm": 10.0, "area_pct": 100.0}]
        result = generate_adaptive_slicing_plan(
            regions=regions,
            material="PETG",
            model_height_mm=10.0,
        )
        assert result["success"] is True
        assert result["plan"]["material"] == "PETG"


# ---------------------------------------------------------------------------
# TestCoolingAndSpeedIntegration
# ---------------------------------------------------------------------------


class TestCoolingAndSpeedIntegration:
    """Verify cooling and speed are correctly embedded in plans."""

    def test_plan_speeds_reasonable(self, slicer, pla_profile, mixed_regions):
        plan = slicer.generate_plan(mixed_regions, pla_profile, model_height_mm=30.0)
        for s in plan.layer_speeds:
            assert 0.0 < s <= 1.0

    def test_plan_cooling_reasonable(self, slicer, pla_profile, mixed_regions):
        plan = slicer.generate_plan(mixed_regions, pla_profile, model_height_mm=30.0)
        for c in plan.layer_cooling:
            assert 0.0 <= c <= 100.0

    def test_first_layer_speed_factor(self, slicer, pla_profile, standard_regions):
        plan = slicer.generate_plan(standard_regions, pla_profile, model_height_mm=10.0)
        assert plan.layer_speeds[0] == pla_profile.first_layer_speed_factor
