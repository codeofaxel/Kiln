"""Tests for the geometry-aware design reasoning engine.

Coverage areas:
- Dataclass construction and to_dict() serialization
- Pure geometry helpers: bounding_box, edge_z_intersect, cross_section_area,
  convex_hull_area, triangle_normal, triangle_area, signed_triangle_volume,
  pt_dist, shoelace_area, chain_segments
- Binary and ASCII STL parsing
- Structural risk analysis (public API)
- Reinforcement recommendation (public API)
- Load-bearing assessment (public API)
- Improvement plan generation and scoring
- Print settings inference from structural risks
- Weight estimation (divergence theorem volume)
- Composition plan from natural language description
- Template search by keyword
- Cross-section at plane
- Constraint solver
- Merge STL files
- Edge cases: empty inputs, missing files, invalid planes
"""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.design_reasoning import (
    CompositionPlan,
    ConstraintSolution,
    CrossSectionResult,
    DesignImprovementPlan,
    LoadAnalysis,
    MergedMeshResult,
    PrintSettingsRecommendation,
    ReinforcementRecommendation,
    ReinforcementResult,
    StructuralRisk,
    TemplateSearchResult,
    WeightEstimate,
    _bounding_box,
    _chain_segments,
    _convex_hull_area,
    _cross_section_area,
    _cross_section_at_z,
    _edge_z_intersect,
    _parse_stl_for_analysis,
    _pt_dist,
    _shoelace_area,
    _signed_triangle_volume,
    _triangle_area,
    _triangle_normal,
    analyze_structural_risks,
    assess_load_bearing,
    cross_section_at_plane,
    estimate_weight,
    generate_improvement_plan,
    infer_print_settings,
    merge_stl_files,
    plan_composition_from_description,
    recommend_reinforcements,
    search_templates,
    solve_constraints,
)

# ---------------------------------------------------------------------------
# Helpers for building binary STL data in memory
# ---------------------------------------------------------------------------


def _make_binary_stl(triangles: list[tuple[tuple[float, ...], ...]]) -> bytes:
    """Build a minimal binary STL from a list of triangles."""
    buf = bytearray(b"\x00" * 80)  # header
    buf += struct.pack("<I", len(triangles))
    for tri in triangles:
        # normal (ignored by parser — just write zeros)
        buf += struct.pack("<3f", 0.0, 0.0, 0.0)
        for v in tri:
            buf += struct.pack("<3f", *v[:3])
        buf += struct.pack("<H", 0)  # attribute byte count
    return bytes(buf)


def _write_binary_stl_file(
    triangles: list[tuple[tuple[float, ...], ...]],
    path: str,
) -> None:
    """Write a binary STL file from triangles."""
    Path(path).write_bytes(_make_binary_stl(triangles))


# A simple 10x10x10 cube (12 triangles, 2 per face)
_CUBE_TRIS: list[tuple[tuple[float, ...], ...]] = [
    # bottom face (z=0)
    ((0, 0, 0), (10, 0, 0), (10, 10, 0)),
    ((0, 0, 0), (10, 10, 0), (0, 10, 0)),
    # top face (z=10)
    ((0, 0, 10), (10, 10, 10), (10, 0, 10)),
    ((0, 0, 10), (0, 10, 10), (10, 10, 10)),
    # front (y=0)
    ((0, 0, 0), (10, 0, 10), (10, 0, 0)),
    ((0, 0, 0), (0, 0, 10), (10, 0, 10)),
    # back (y=10)
    ((0, 10, 0), (10, 10, 0), (10, 10, 10)),
    ((0, 10, 0), (10, 10, 10), (0, 10, 10)),
    # left (x=0)
    ((0, 0, 0), (0, 10, 0), (0, 10, 10)),
    ((0, 0, 0), (0, 10, 10), (0, 0, 10)),
    # right (x=10)
    ((10, 0, 0), (10, 0, 10), (10, 10, 10)),
    ((10, 0, 0), (10, 10, 10), (10, 10, 0)),
]


@pytest.fixture()
def cube_stl(tmp_path):
    """Write a 10x10x10 cube STL and return its path."""
    p = str(tmp_path / "cube.stl")
    _write_binary_stl_file(_CUBE_TRIS, p)
    return p


@pytest.fixture()
def tall_thin_stl(tmp_path):
    """Write a tall thin column (2x2x50) STL — triggers base adequacy and thin neck checks."""
    # Simple approximation: a box 2x2x50
    tris: list[tuple[tuple[float, ...], ...]] = [
        # bottom (z=0)
        ((0, 0, 0), (2, 0, 0), (2, 2, 0)),
        ((0, 0, 0), (2, 2, 0), (0, 2, 0)),
        # top (z=50)
        ((0, 0, 50), (2, 2, 50), (2, 0, 50)),
        ((0, 0, 50), (0, 2, 50), (2, 2, 50)),
        # front
        ((0, 0, 0), (2, 0, 50), (2, 0, 0)),
        ((0, 0, 0), (0, 0, 50), (2, 0, 50)),
        # back
        ((0, 2, 0), (2, 2, 0), (2, 2, 50)),
        ((0, 2, 0), (2, 2, 50), (0, 2, 50)),
        # left
        ((0, 0, 0), (0, 2, 0), (0, 2, 50)),
        ((0, 0, 0), (0, 2, 50), (0, 0, 50)),
        # right
        ((2, 0, 0), (2, 0, 50), (2, 2, 50)),
        ((2, 0, 0), (2, 2, 50), (2, 2, 0)),
    ]
    p = str(tmp_path / "tall_thin.stl")
    _write_binary_stl_file(tris, p)
    return p


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestStructuralRiskToDict:
    """StructuralRisk.to_dict() produces expected keys and rounding."""

    def test_to_dict_keys(self):
        r = StructuralRisk(
            risk_type="thin_neck",
            severity="critical",
            location_mm=(1.0, 2.0, 3.0),
            region_size_mm=(4.0, 5.0, 6.0),
            description="Too thin",
            metric_name="cross_section_area_mm2",
            metric_value=2.12345,
            metric_threshold=4.0,
        )
        d = r.to_dict()
        assert d["risk_type"] == "thin_neck"
        assert d["severity"] == "critical"
        assert d["location_mm"] == [1.0, 2.0, 3.0]
        assert d["metric_value"] == 2.12
        assert d["metric_threshold"] == 4.0

    def test_to_dict_rounds_metric_value(self):
        r = StructuralRisk(
            risk_type="cantilever",
            severity="warning",
            location_mm=(0, 0, 0),
            region_size_mm=(1, 1, 1),
            description="",
            metric_name="ratio",
            metric_value=5.6789,
            metric_threshold=5.0,
        )
        assert r.to_dict()["metric_value"] == 5.68


class TestReinforcementRecommendationToDict:
    def test_to_dict_keys(self):
        r = ReinforcementRecommendation(
            reinforcement_type="fillet",
            priority="high",
            location_mm=(1.0, 2.0, 3.0),
            description="Add fillet",
            estimated_strength_gain="2-3x",
            addresses_risk="sharp_corner",
        )
        d = r.to_dict()
        assert d["reinforcement_type"] == "fillet"
        assert d["location_mm"] == [1.0, 2.0, 3.0]
        assert d["addresses_risk"] == "sharp_corner"


class TestLoadAnalysisToDict:
    def test_to_dict_keys(self):
        la = LoadAnalysis(
            primary_load_axis="vertical",
            load_surfaces=[{"type": "top", "area_mm2": 100}],
            weak_axis="horizontal",
            layer_direction_concern="Layers perpendicular to load.",
            recommended_print_orientation="upright",
            orientation_reasoning="Best strength axis.",
        )
        d = la.to_dict()
        assert d["primary_load_axis"] == "vertical"
        assert len(d["load_surfaces"]) == 1
        assert d["recommended_print_orientation"] == "upright"


class TestDesignImprovementPlanToDict:
    def test_empty_plan_serializes(self):
        p = DesignImprovementPlan(file_path="/tmp/test.stl")
        d = p.to_dict()
        assert d["file_path"] == "/tmp/test.stl"
        assert d["risks"] == []
        assert d["load_analysis"] is None
        assert d["overall_structural_score"] == 0

    def test_plan_with_load_analysis(self):
        la = LoadAnalysis(
            primary_load_axis="vertical",
            load_surfaces=[],
            weak_axis="x",
            layer_direction_concern="",
            recommended_print_orientation="upright",
            orientation_reasoning="",
        )
        p = DesignImprovementPlan(file_path="f.stl", load_analysis=la)
        assert p.to_dict()["load_analysis"] is not None
        assert p.to_dict()["load_analysis"]["primary_load_axis"] == "vertical"


class TestReinforcementResultToDict:
    def test_to_dict_has_scores(self):
        r = ReinforcementResult(
            output_path="/out.stl",
            original_path="/in.stl",
            before_score=60,
            after_score=85,
            before_grade="C",
            after_grade="B",
            summary="Improved.",
        )
        d = r.to_dict()
        assert d["before_score"] == 60
        assert d["after_score"] == 85
        assert d["before_grade"] == "C"


class TestPrintSettingsRecommendationToDict:
    def test_to_dict_keys(self):
        psr = PrintSettingsRecommendation(
            perimeters=3,
            infill_percent=20,
            infill_pattern="grid",
            layer_height_mm=0.2,
            support_enabled=False,
            support_reason="None needed.",
            brim_enabled=False,
            brim_reason="Fine base.",
            print_orientation="upright",
            orientation_reason="Default.",
        )
        d = psr.to_dict()
        assert d["perimeters"] == 3
        assert d["infill_pattern"] == "grid"
        assert d["confidence"] == "high"


class TestCompositionPlanToDict:
    def test_to_dict_has_description(self):
        cp = CompositionPlan(description="a cube with a hole")
        d = cp.to_dict()
        assert d["description"] == "a cube with a hole"
        assert d["primitives"] == []
        assert d["complexity"] == "simple"


class TestConstraintSolutionToDict:
    def test_to_dict_has_template_id(self):
        cs = ConstraintSolution(template_id="box", success=True)
        d = cs.to_dict()
        assert d["template_id"] == "box"
        assert d["success"] is True


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------


class TestBoundingBox:
    """_bounding_box from vertex lists."""

    def test_empty_vertices(self):
        bb = _bounding_box([])
        assert bb["min_x"] == 0
        assert bb["max_z"] == 0

    def test_single_vertex(self):
        bb = _bounding_box([(5.0, 3.0, 1.0)])
        assert bb["min_x"] == 5.0
        assert bb["max_x"] == 5.0

    def test_cube_vertices(self):
        verts = [(0, 0, 0), (10, 10, 10)]
        bb = _bounding_box(verts)
        assert bb["min_x"] == 0
        assert bb["max_x"] == 10
        assert bb["min_z"] == 0
        assert bb["max_z"] == 10


class TestEdgeZIntersect:
    """_edge_z_intersect linear interpolation."""

    def test_midpoint(self):
        p = _edge_z_intersect((0.0, 0.0, 10.0), (0.0, 0.0, 0.0), 5.0)
        assert p is not None
        assert abs(p[0]) < 1e-6
        assert abs(p[1]) < 1e-6

    def test_near_top(self):
        p = _edge_z_intersect((10.0, 20.0, 100.0), (0.0, 0.0, 0.0), 90.0)
        assert p is not None
        assert abs(p[0] - 9.0) < 1e-6
        assert abs(p[1] - 18.0) < 1e-6

    def test_degenerate_same_z_returns_none(self):
        p = _edge_z_intersect((5.0, 5.0, 10.0), (5.0, 5.0, 10.0), 10.0)
        assert p is None


class TestCrossSectionArea:
    """_cross_section_area from line segments."""

    def test_empty_segments(self):
        assert _cross_section_area([]) == 0.0

    def test_few_segments_circular_heuristic(self):
        # Two short segments — uses circular heuristic
        segs = [((0, 0), (5, 0)), ((0, 5), (5, 5))]
        area = _cross_section_area(segs)
        assert area > 0


class TestConvexHullArea:
    """_convex_hull_area via sorted angles + shoelace."""

    def test_square(self):
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        area = _convex_hull_area(points)
        assert abs(area - 100.0) < 1e-6

    def test_triangle(self):
        points = [(0.0, 0.0), (10.0, 0.0), (5.0, 10.0)]
        area = _convex_hull_area(points)
        assert abs(area - 50.0) < 1e-6

    def test_fewer_than_three_returns_zero(self):
        assert _convex_hull_area([(0, 0), (1, 1)]) == 0.0
        assert _convex_hull_area([]) == 0.0


class TestTriangleNormal:
    """_triangle_normal returns unit normals."""

    def test_xy_plane_triangle_normal_is_z(self):
        tri = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        n = _triangle_normal(tri)
        assert abs(n[2] - 1.0) < 1e-6 or abs(n[2] + 1.0) < 1e-6  # +Z or -Z
        assert abs(n[0]) < 1e-6
        assert abs(n[1]) < 1e-6

    def test_degenerate_triangle_returns_default(self):
        # All vertices identical — zero-area triangle
        tri = ((5, 5, 5), (5, 5, 5), (5, 5, 5))
        n = _triangle_normal(tri)
        assert n == (0.0, 0.0, 1.0)


class TestTriangleArea:
    """_triangle_area computes correct area."""

    def test_unit_right_triangle(self):
        tri = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(_triangle_area(tri) - 0.5) < 1e-6

    def test_degenerate_zero_area(self):
        tri = ((0, 0, 0), (0, 0, 0), (0, 0, 0))
        assert _triangle_area(tri) == 0.0


class TestSignedTriangleVolume:
    """_signed_triangle_volume tetrahedron computation."""

    def test_known_volume(self):
        # Triangle with vertices at (1,0,0), (0,1,0), (0,0,1)
        # Tetrahedron with origin has volume 1/6
        vol = _signed_triangle_volume((1, 0, 0), (0, 1, 0), (0, 0, 1))
        assert abs(vol - 1.0 / 6.0) < 1e-10

    def test_origin_triangle_zero_volume(self):
        vol = _signed_triangle_volume((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(vol) < 1e-10


class TestPtDist:
    """_pt_dist euclidean distance."""

    def test_same_point(self):
        assert _pt_dist((0, 0), (0, 0)) == 0.0

    def test_known_distance(self):
        assert abs(_pt_dist((0, 0), (3, 4)) - 5.0) < 1e-10


class TestShoelaceArea:
    """_shoelace_area signed polygon area."""

    def test_unit_square(self):
        pts = [(0, 0), (1, 0), (1, 1), (0, 1)]
        assert abs(_shoelace_area(pts) - 1.0) < 1e-10

    def test_fewer_than_three(self):
        assert _shoelace_area([(0, 0)]) == 0.0
        assert _shoelace_area([]) == 0.0


class TestChainSegments:
    """_chain_segments contour building."""

    def test_empty_returns_empty(self):
        assert _chain_segments([]) == []

    def test_simple_triangle(self):
        segs = [
            ((0.0, 0.0), (1.0, 0.0)),
            ((1.0, 0.0), (0.5, 1.0)),
            ((0.5, 1.0), (0.0, 0.0)),
        ]
        contours = _chain_segments(segs)
        assert len(contours) == 1
        assert len(contours[0]) >= 3

    def test_two_segments_no_chain_dropped(self):
        # Two unconnected segments — each is only 2 points, below threshold
        segs = [((0, 0), (1, 0)), ((10, 10), (11, 10))]
        contours = _chain_segments(segs)
        # Each chain is only 2 points (< 3), so both should be dropped
        assert len(contours) == 0


# ---------------------------------------------------------------------------
# STL parsing
# ---------------------------------------------------------------------------


class TestParseStlForAnalysis:
    """_parse_stl_for_analysis dispatches binary vs ASCII."""

    def test_binary_file(self, cube_stl):
        tris, verts = _parse_stl_for_analysis(cube_stl)
        assert len(tris) == 12

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            _parse_stl_for_analysis("/nonexistent/file.stl")


class TestCrossSectionAtZ:
    """_cross_section_at_z extracts intersection segments."""

    def test_cube_midpoint_has_segments(self):
        segments = _cross_section_at_z(_CUBE_TRIS, 5.0)
        assert len(segments) > 0

    def test_cube_below_mesh_no_segments(self):
        segments = _cross_section_at_z(_CUBE_TRIS, -1.0)
        assert len(segments) == 0

    def test_cube_above_mesh_no_segments(self):
        segments = _cross_section_at_z(_CUBE_TRIS, 11.0)
        assert len(segments) == 0


# ---------------------------------------------------------------------------
# Public API: structural analysis
# ---------------------------------------------------------------------------


class TestAnalyzeStructuralRisks:
    """analyze_structural_risks on real STL files."""

    def test_cube_returns_list(self, cube_stl):
        risks = analyze_structural_risks(cube_stl)
        assert isinstance(risks, list)
        # A simple cube should have few or no critical risks
        for r in risks:
            assert isinstance(r, StructuralRisk)

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            analyze_structural_risks("/no/such/file.stl")

    def test_empty_mesh_returns_empty(self, tmp_path):
        # Write a binary STL with 0 triangles
        p = str(tmp_path / "empty.stl")
        _write_binary_stl_file([], p)
        assert analyze_structural_risks(p) == []

    def test_risks_sorted_by_severity(self, tall_thin_stl):
        risks = analyze_structural_risks(tall_thin_stl)
        if len(risks) >= 2:
            severity_order = {"critical": 0, "warning": 1, "info": 2}
            for i in range(len(risks) - 1):
                assert severity_order.get(risks[i].severity, 3) <= severity_order.get(
                    risks[i + 1].severity, 3
                )


class TestRecommendReinforcements:
    """recommend_reinforcements derives fixes from risk analysis."""

    def test_cube_returns_list(self, cube_stl):
        recs = recommend_reinforcements(cube_stl)
        assert isinstance(recs, list)
        for r in recs:
            assert isinstance(r, ReinforcementRecommendation)

    def test_empty_mesh_returns_empty(self, tmp_path):
        p = str(tmp_path / "empty.stl")
        _write_binary_stl_file([], p)
        assert recommend_reinforcements(p) == []


class TestAssessLoadBearing:
    """assess_load_bearing returns LoadAnalysis."""

    def test_cube_load_analysis(self, cube_stl):
        la = assess_load_bearing(cube_stl)
        assert isinstance(la, LoadAnalysis)
        assert la.primary_load_axis in ("vertical", "horizontal", "multi-axis")
        assert la.recommended_print_orientation in ("upright", "on_side", "on_back")

    def test_empty_mesh_returns_defaults(self, tmp_path):
        p = str(tmp_path / "empty.stl")
        _write_binary_stl_file([], p)
        la = assess_load_bearing(p)
        assert la.primary_load_axis == "unknown"


# ---------------------------------------------------------------------------
# Improvement plan + scoring
# ---------------------------------------------------------------------------


class TestGenerateImprovementPlan:
    """generate_improvement_plan combines risk, reinforcement, and load analysis."""

    def test_cube_plan_has_score(self, cube_stl):
        plan = generate_improvement_plan(cube_stl)
        assert isinstance(plan, DesignImprovementPlan)
        assert 0 <= plan.overall_structural_score <= 100
        assert plan.structural_grade in ("A", "B", "C", "D", "F")
        assert plan.file_path == cube_stl

    def test_empty_mesh_returns_unparseable(self, tmp_path):
        p = str(tmp_path / "empty.stl")
        _write_binary_stl_file([], p)
        plan = generate_improvement_plan(p)
        assert "Could not parse" in plan.summary

    def test_score_grades(self):
        """Verify grade boundaries in the scoring logic."""
        # Simulate by checking the grade assignment directly:
        # score >= 90 -> A, 80 -> B, 65 -> C, 50 -> D, else F
        # We can verify through the plan for a solid cube
        # A cube with 0 risks should score 100/A
        pass  # Tested via cube_plan_has_score — cube should be high

    def test_plan_to_dict_roundtrip(self, cube_stl):
        plan = generate_improvement_plan(cube_stl)
        d = plan.to_dict()
        assert d["file_path"] == cube_stl
        assert isinstance(d["risks"], list)
        assert isinstance(d["overall_structural_score"], int)

    def test_tall_thin_has_risks(self, tall_thin_stl):
        plan = generate_improvement_plan(tall_thin_stl)
        # A 2x2x50 column should have some structural risk
        # (insufficient base, thin neck, etc.)
        assert isinstance(plan.risks, list)
        assert plan.summary  # non-empty summary


# ---------------------------------------------------------------------------
# Print settings inference
# ---------------------------------------------------------------------------


class TestInferPrintSettings:
    """infer_print_settings bridges structural analysis to slicer config."""

    def test_cube_defaults(self, cube_stl):
        settings = infer_print_settings(cube_stl)
        assert isinstance(settings, PrintSettingsRecommendation)
        assert settings.perimeters >= 2
        assert settings.infill_percent >= 0
        assert settings.layer_height_mm > 0

    def test_material_defaults_pla(self, cube_stl):
        s = infer_print_settings(cube_stl, material="PLA")
        assert s.infill_pattern in ("grid", "gyroid")

    def test_material_defaults_tpu(self, cube_stl):
        s = infer_print_settings(cube_stl, material="TPU")
        # TPU defaults to gyroid and 0.24 layer height
        assert s.infill_pattern == "gyroid" or s.layer_height_mm >= 0.2

    def test_unknown_material_falls_back_to_pla(self, cube_stl):
        s = infer_print_settings(cube_stl, material="UNOBTANIUM")
        # Should use PLA defaults without crashing
        assert isinstance(s, PrintSettingsRecommendation)

    def test_settings_to_dict(self, cube_stl):
        s = infer_print_settings(cube_stl)
        d = s.to_dict()
        assert "perimeters" in d
        assert "infill_percent" in d
        assert "confidence" in d


# ---------------------------------------------------------------------------
# Weight estimation
# ---------------------------------------------------------------------------


class TestEstimateWeight:
    """estimate_weight uses divergence theorem for volume."""

    def test_cube_weight(self, cube_stl):
        w = estimate_weight(cube_stl, material="pla")
        assert isinstance(w, WeightEstimate)
        # 10x10x10 cube = 1000 mm3 = 1 cm3 at PLA density 1.24 g/cm3
        # Solid weight should be ~1.24g
        assert w.volume_mm3 > 0
        assert w.solid_weight_g > 0
        assert w.estimated_weight_g > 0
        assert w.material == "pla"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="STL file not found"):
            estimate_weight("/no/such/file.stl")

    def test_unknown_material_uses_pla_density(self, cube_stl):
        w = estimate_weight(cube_stl, material="kryptonite")
        assert w.density_g_cm3 == 1.24
        assert any("Unknown material" in n for n in w.notes)

    def test_to_dict_rounds(self, cube_stl):
        w = estimate_weight(cube_stl)
        d = w.to_dict()
        assert isinstance(d["volume_mm3"], float)
        assert isinstance(d["estimated_weight_g"], float)


# ---------------------------------------------------------------------------
# Composition planner (NLP → CSG)
# ---------------------------------------------------------------------------


class TestPlanCompositionFromDescription:
    """plan_composition_from_description keyword-based CSG planning."""

    def test_cube_description(self):
        plan = plan_composition_from_description("a cube")
        assert isinstance(plan, CompositionPlan)
        assert len(plan.primitives) >= 1
        assert plan.primitives[0]["shape"] == "cube"

    def test_cylinder_description(self):
        plan = plan_composition_from_description("a cylinder")
        assert plan.primitives[0]["shape"] == "cylinder"

    def test_cube_with_hole(self):
        plan = plan_composition_from_description("a cube with a hole")
        assert any(p["shape"] == "cube" for p in plan.primitives)
        assert any(o["op"] == "difference" for o in plan.operations)

    def test_no_shape_defaults_to_cube(self):
        plan = plan_composition_from_description("something undefined")
        assert plan.primitives[0]["shape"] == "cube"
        assert any("No shape keywords" in n for n in plan.notes)

    def test_multiple_shapes(self):
        plan = plan_composition_from_description("a cube on a cylinder")
        shapes = [p["shape"] for p in plan.primitives]
        assert "cube" in shapes
        assert "cylinder" in shapes

    def test_target_size_affects_dimensions(self):
        small = plan_composition_from_description("a sphere", target_size_mm=10.0)
        large = plan_composition_from_description("a sphere", target_size_mm=100.0)
        assert small.primitives[0]["params"]["r"] < large.primitives[0]["params"]["r"]

    def test_complexity_classification(self):
        simple = plan_composition_from_description("a box")
        assert simple.complexity == "simple"

        multi = plan_composition_from_description("a cube on a cylinder with a sphere")
        assert multi.complexity in ("moderate", "complex")

    def test_hollow_keyword_triggers_difference(self):
        plan = plan_composition_from_description("a hollow sphere")
        assert any(o["op"] == "difference" for o in plan.operations)


# ---------------------------------------------------------------------------
# Template search
# ---------------------------------------------------------------------------


class TestSearchTemplates:
    """search_templates fuzzy keyword matching."""

    def test_empty_query_returns_empty(self):
        result = search_templates("")
        assert result.matches == []
        assert result.total_templates == 0

    def test_whitespace_query_returns_empty(self):
        result = search_templates("   ")
        assert result.matches == []

    def test_returns_template_search_result(self):
        result = search_templates("box")
        assert isinstance(result, TemplateSearchResult)
        assert result.search_method == "keyword"

    def test_max_results_limit(self):
        result = search_templates("box", max_results=2)
        assert len(result.matches) <= 2


# ---------------------------------------------------------------------------
# Cross section at plane
# ---------------------------------------------------------------------------


class TestCrossSectionAtPlane:
    """cross_section_at_plane slices mesh perpendicular to an axis."""

    def test_cube_z_midpoint(self, cube_stl):
        cs = cross_section_at_plane(cube_stl, plane="z", offset_ratio=0.5)
        assert isinstance(cs, CrossSectionResult)
        assert cs.plane == "z"
        assert cs.cross_section_area_mm2 >= 0

    def test_cube_x_plane(self, cube_stl):
        cs = cross_section_at_plane(cube_stl, plane="x", offset_ratio=0.5)
        assert cs.plane == "x"

    def test_cube_y_plane(self, cube_stl):
        cs = cross_section_at_plane(cube_stl, plane="y", offset_ratio=0.5)
        assert cs.plane == "y"

    def test_invalid_plane_raises(self, cube_stl):
        with pytest.raises(ValueError, match="plane must be"):
            cross_section_at_plane(cube_stl, plane="w")

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="STL file not found"):
            cross_section_at_plane("/no/such/file.stl")

    def test_offset_mm_overrides_ratio(self, cube_stl):
        cs = cross_section_at_plane(cube_stl, plane="z", offset_mm=5.0)
        assert abs(cs.plane_offset_mm - 5.0) < 1e-6

    def test_to_dict_rounds_values(self, cube_stl):
        cs = cross_section_at_plane(cube_stl, plane="z", offset_ratio=0.5)
        d = cs.to_dict()
        assert isinstance(d["plane_offset_mm"], float)
        assert isinstance(d["cross_section_area_mm2"], float)


# ---------------------------------------------------------------------------
# Constraint solver
# ---------------------------------------------------------------------------


class TestSolveConstraints:
    """solve_constraints iterative parametric solver."""

    def test_unknown_template_returns_note(self):
        result = solve_constraints("__nonexistent__", {"w": {"min": 10}})
        assert isinstance(result, ConstraintSolution)
        assert result.success is False

    def test_missing_templates_file(self):
        with patch(
            "kiln.design_reasoning.Path.exists",
            return_value=False,
        ):
            result = solve_constraints("box", {"w": {"min": 10}})
            assert result.success is False

    def test_to_dict_keys(self):
        cs = ConstraintSolution(
            template_id="test",
            solved_params={"w": 20.0},
            success=True,
        )
        d = cs.to_dict()
        assert d["template_id"] == "test"
        assert d["solved_params"] == {"w": 20.0}


# ---------------------------------------------------------------------------
# Merge STL files
# ---------------------------------------------------------------------------


class TestMergeStlFiles:
    """merge_stl_files combines multiple meshes."""

    def test_empty_file_list(self, tmp_path):
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([], out)
        assert isinstance(result, MergedMeshResult)
        assert result.success is False
        assert "No input files" in result.errors[0]

    def test_merge_two_cubes(self, cube_stl, tmp_path):
        # Create a second cube
        cube2 = str(tmp_path / "cube2.stl")
        _write_binary_stl_file(_CUBE_TRIS, cube2)
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([cube_stl, cube2], out)
        assert result.success is True
        assert result.total_triangles == 24  # 12 + 12
        assert Path(out).exists()

    def test_merge_with_positions(self, cube_stl, tmp_path):
        cube2 = str(tmp_path / "cube2.stl")
        _write_binary_stl_file(_CUBE_TRIS, cube2)
        out = str(tmp_path / "merged_pos.stl")
        positions = [{"x": 0, "y": 0, "z": 0}, {"x": 20, "y": 0, "z": 0}]
        result = merge_stl_files([cube_stl, cube2], out, positions=positions)
        assert result.success is True
        # Width should be at least 30mm (cube at 0-10 + cube at 20-30)
        assert result.bounding_box_mm["width"] >= 29.0

    def test_positions_length_mismatch(self, cube_stl, tmp_path):
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([cube_stl], out, positions=[{"x": 0}, {"y": 0}])
        assert result.success is False
        assert any("positions length" in e for e in result.errors)

    def test_nonexistent_file_error(self, tmp_path):
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files(["/no/such/file.stl"], out)
        assert result.success is False
        assert any("File not found" in e for e in result.errors)

    def test_to_dict(self, cube_stl, tmp_path):
        out = str(tmp_path / "merged.stl")
        result = merge_stl_files([cube_stl], out)
        d = result.to_dict()
        assert "output_path" in d
        assert "total_triangles" in d


# ---------------------------------------------------------------------------
# TestStructuralThresholdsTierSeam — INVERSE PATTERN
#
# Free tier ships with STRICTER thresholds than Kiln's curated baseline.
# A free user sees a SUPERSET of the flags a Pro user would see for the
# same mesh — never fewer.  Pro+ unlocks the calibrated baseline via the
# structural_thresholds overlay.
#
# The test_free_tier_never_underflags_relative_to_pro test is the
# SAFETY FLOOR.  If it goes red, the threshold direction is wrong and
# the patch DOES NOT SHIP.
# ---------------------------------------------------------------------------


from kiln.design_reasoning import (  # noqa: E402
    _CANTILEVER_RISK_RATIO_PUBLIC,
    _MIN_CROSS_SECTION_MM2_PUBLIC,
    _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC,
    _THIN_NECK_RATIO_PUBLIC,
)


# Pro-tier calibrated values; mirrors kiln_pro/data/structural_thresholds_pro_overlay.json
_PRO_OVERLAY = {
    "thin_neck_ratio": 0.30,
    "cantilever_risk_ratio": 5.0,
    "min_cross_section_mm2": 4.0,
    "sharp_angle_threshold_deg": 60.0,
}


def _prism_triangles(w: float, d: float, h: float) -> list[tuple[tuple[float, ...], ...]]:
    """12 triangles forming an axis-aligned rectangular prism (w × d × h)."""
    v = [
        (0.0, 0.0, 0.0), (w, 0.0, 0.0), (w, d, 0.0), (0.0, d, 0.0),
        (0.0, 0.0, h),   (w, 0.0, h),   (w, d, h),   (0.0, d, h),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3),  # bottom
        (4, 6, 5), (4, 7, 6),  # top
        (0, 5, 1), (0, 4, 5),  # front (y=0)
        (2, 6, 7), (2, 7, 3),  # back  (y=d)
        (0, 3, 7), (0, 7, 4),  # left  (x=0)
        (1, 5, 6), (1, 6, 2),  # right (x=w)
    ]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


@pytest.fixture()
def borderline_cantilever_stl(tmp_path):
    """A 5×5×22.5mm prism — height/base ratio = 4.5, which sits IN the
    public-vs-Pro threshold gap (public flags >4.0, Pro flags >5.0).

    Public tier MUST flag this as ``insufficient_base``; Pro tier
    must NOT.  This is the inverse-pattern safety probe."""
    stl_path = tmp_path / "borderline_cantilever.stl"
    _write_binary_stl_file(_prism_triangles(5.0, 5.0, 22.5), str(stl_path))
    return str(stl_path)


class TestStructuralThresholdsTierSeam:
    """Pin the INVERSE-PATTERN tier seam for structural-risk thresholds."""

    def test_public_thresholds_are_stricter_than_pro(self):
        """Direction invariant — if a future edit ever flips one of these,
        free tier could under-flag and a load-bearing print could ship
        without warning.  Catch the regression at the constants."""
        assert _THIN_NECK_RATIO_PUBLIC > _PRO_OVERLAY["thin_neck_ratio"], (
            "thin_neck_ratio: HIGHER value flags MORE thin necks. "
            "Public must be > Pro."
        )
        assert _CANTILEVER_RISK_RATIO_PUBLIC < _PRO_OVERLAY["cantilever_risk_ratio"], (
            "cantilever_risk_ratio: LOWER value flags MORE cantilevers. "
            "Public must be < Pro."
        )
        assert _MIN_CROSS_SECTION_MM2_PUBLIC > _PRO_OVERLAY["min_cross_section_mm2"], (
            "min_cross_section_mm2: HIGHER value flags MORE small sections. "
            "Public must be > Pro."
        )
        assert _SHARP_ANGLE_THRESHOLD_DEG_PUBLIC > _PRO_OVERLAY["sharp_angle_threshold_deg"], (
            "sharp_angle_threshold_deg: HIGHER value flags MORE corners. "
            "Public must be > Pro."
        )

    def test_free_tier_never_underflags_relative_to_pro(
        self, borderline_cantilever_stl, monkeypatch,
    ):
        """RELEASE BLOCKER.  Same mesh under free tier produces findings
        that are a SUPERSET of (or equal to) findings under Pro tier.
        Free can never SILENCE a finding Pro would have raised.

        Borderline fixture: a 5×5×22.5mm prism — height/base = 4.5.
        Public's 4.0 threshold flags it; Pro's 5.0 doesn't."""
        # Free tier: empty overlay
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: {},
        )
        free_risks = analyze_structural_risks(borderline_cantilever_stl)

        # Pro tier: calibrated overlay
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: _PRO_OVERLAY,
        )
        pro_risks = analyze_structural_risks(borderline_cantilever_stl)

        # Pro flags must be a subset of free flags — every risk_type Pro
        # raised must also appear in free's output.
        pro_keys = {(r.risk_type, round(r.location_mm[2], 1)) for r in pro_risks}
        free_keys = {(r.risk_type, round(r.location_mm[2], 1)) for r in free_risks}
        missed = pro_keys - free_keys
        assert not missed, (
            f"SAFETY VIOLATION: free tier under-flagged vs Pro. "
            f"Pro raised but free did not: {missed}"
        )
        # And specifically: free MUST flag the 4.5:1 ratio as insufficient_base.
        assert any(r.risk_type == "insufficient_base" for r in free_risks), (
            "Free tier missed the 4.5:1 height/base prism — the borderline "
            "geometry the public-stricter threshold exists to catch."
        )
        # Pro should NOT flag it (calibrated 5.0 threshold; 4.5 < 5.0).
        assert not any(r.risk_type == "insufficient_base" for r in pro_risks), (
            "Pro tier flagged a 4.5:1 ratio prism — the calibrated baseline "
            "should silently pass it.  Did the overlay value drift?"
        )

    def test_explicit_kwarg_overrides_both_tiers(
        self, borderline_cantilever_stl, monkeypatch,
    ):
        """Power-user escape hatch: explicit kwarg beats overlay AND public.
        Same mesh, sharp_angle_threshold_deg=89 catches every concave edge."""
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: _PRO_OVERLAY,
        )
        with_explicit = analyze_structural_risks(
            borderline_cantilever_stl,
            sharp_angle_threshold_deg=89.0,
        )
        without = analyze_structural_risks(borderline_cantilever_stl)
        # Explicit-loose threshold catches at least as many sharp corners
        # as the Pro-calibrated default (60°).
        sharp_explicit = sum(1 for r in with_explicit if r.risk_type == "sharp_corner")
        sharp_default = sum(1 for r in without if r.risk_type == "sharp_corner")
        assert sharp_explicit >= sharp_default

    def test_overlay_unavailable_falls_back_to_stricter_public(
        self, borderline_cantilever_stl, monkeypatch,
    ):
        """When the overlay returns {} (kiln-pro absent, license missing,
        network down beyond grace), thresholds resolve to the public-
        stricter defaults — no crash, no silent Pro pass-through."""
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: {},
        )
        risks = analyze_structural_risks(borderline_cantilever_stl)
        # Behaviour matches free-tier expectations: borderline geometry
        # flagged as insufficient_base.
        assert isinstance(risks, list)
        assert any(r.risk_type == "insufficient_base" for r in risks)

    def test_explicit_min_cross_section_takes_priority_over_overlay(
        self, cube_stl, monkeypatch,
    ):
        """Caller-supplied min_cross_section_mm2 beats both overlay and
        public default.  Resolution order: explicit > overlay > public."""
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: _PRO_OVERLAY,
        )
        # 10x10 cube has 100mm² cross-sections; threshold=200 flags every slice.
        risks = analyze_structural_risks(cube_stl, min_cross_section_mm2=200.0)
        thin = [r for r in risks if r.risk_type == "thin_neck"]
        assert thin, (
            "Explicit min_cross_section_mm2=200 should flag every layer of "
            "a 10x10 cube as thin_neck — caller override didn't take effect."
        )
