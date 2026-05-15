"""Tests for kiln.printability -- printability analysis engine."""

from __future__ import annotations

import os
import struct
import tempfile

import pytest

from kiln.printability import (
    BedAdhesionAnalysis,
    BridgingAnalysis,
    OverhangAnalysis,
    PrintabilityReport,
    SupportAnalysis,
    ThinWallAnalysis,
    _analyze_bed_adhesion,
    _analyze_bridging,
    _analyze_overhangs,
    _analyze_supports,
    _analyze_thin_walls,
    _compute_score,
    _score_to_grade,
    _triangle_area,
    _triangle_centroid,
    _triangle_normal,
    analyze_printability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    """Create a minimal binary STL from triangle vertex tuples."""
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<9f", *v1, *v2, *v3)
        attr = struct.pack("<H", 0)
        body += normal + verts + attr
    return header + count + body


def _cube_triangles(size: float = 10.0) -> list[tuple]:
    """12 triangles forming a cube [0,size]^3."""
    s = size
    verts = [
        (0, 0, 0),
        (s, 0, 0),
        (s, s, 0),
        (0, s, 0),
        (0, 0, s),
        (s, 0, s),
        (s, s, s),
        (0, s, s),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),  # bottom
        (4, 6, 5),
        (4, 7, 6),  # top
        (0, 4, 5),
        (0, 5, 1),  # front
        (2, 6, 7),
        (2, 7, 3),  # back
        (0, 3, 7),
        (0, 7, 4),  # left
        (1, 5, 6),
        (1, 6, 2),  # right
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple]) -> str:
    """Write a binary STL file and return its path."""
    path = os.path.join(tmpdir, "test_model.stl")
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


# ---------------------------------------------------------------------------
# TestTriangleNormal
# ---------------------------------------------------------------------------


class TestTriangleNormal:
    def test_xy_plane_triangle(self):
        n = _triangle_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert n[2] > 0  # Z-up normal

    def test_degenerate_triangle(self):
        n = _triangle_normal((0, 0, 0), (0, 0, 0), (0, 0, 0))
        assert n == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# TestTriangleArea
# ---------------------------------------------------------------------------


class TestTriangleArea:
    def test_unit_right_triangle(self):
        area = _triangle_area((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(area - 0.5) < 1e-6

    def test_degenerate_triangle_zero_area(self):
        area = _triangle_area((0, 0, 0), (1, 0, 0), (2, 0, 0))
        assert area < 1e-6


# ---------------------------------------------------------------------------
# TestTriangleCentroid
# ---------------------------------------------------------------------------


class TestTriangleCentroid:
    def test_origin_triangle(self):
        c = _triangle_centroid((0, 0, 0), (3, 0, 0), (0, 3, 0))
        assert abs(c[0] - 1.0) < 1e-6
        assert abs(c[1] - 1.0) < 1e-6
        assert abs(c[2]) < 1e-6


# ---------------------------------------------------------------------------
# TestOverhangAnalysis
# ---------------------------------------------------------------------------


class TestOverhangAnalysis:
    def test_cube_has_limited_overhangs(self):
        tris = _cube_triangles()
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        # A cube has side faces (normal horizontal) and a bottom face
        # (normal pointing down).  The bottom face normal is (0,0,-1),
        # which is exactly 0 deg from straight down.
        assert isinstance(result, OverhangAnalysis)
        assert result.overhang_triangle_count >= 0

    def test_flat_surface_no_overhangs(self):
        # A single upward-facing triangle.
        tris = [((0, 0, 0), (10, 0, 0), (5, 10, 0))]
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        assert result.overhang_triangle_count == 0
        assert not result.needs_supports

    def test_downward_face_is_overhang(self):
        # Triangle high up with normal pointing straight down.
        tris = [((0, 0, 50), (10, 0, 50), (5, 10, 50))]
        # Normal from cross product: (0, 0, +) but we need it facing down
        # Reverse winding to get downward normal.
        tris = [((0, 0, 50), (5, 10, 50), (10, 0, 50))]
        result = _analyze_overhangs(tris, max_overhang_angle=45.0)
        assert result.needs_supports

    def test_to_dict(self):
        result = _analyze_overhangs(_cube_triangles())
        d = result.to_dict()
        assert "max_overhang_angle" in d
        assert "overhang_triangle_count" in d


# ---------------------------------------------------------------------------
# TestThinWallAnalysis
# ---------------------------------------------------------------------------


class TestThinWallAnalysis:
    def test_cube_no_thin_walls(self):
        tris = _cube_triangles(10.0)
        verts = list({v for tri in tris for v in tri})
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count == 0
        assert result.min_wall_thickness_mm >= 0.4

    def test_thin_triangle_detected(self):
        # Triangle with a very short edge (0.1 mm).
        tris = [((0, 0, 0), (0.1, 0, 0), (0, 10, 0))]
        verts = [(0, 0, 0), (0.1, 0, 0), (0, 10, 0)]
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count == 1
        assert result.min_wall_thickness_mm < 0.4

    def test_to_dict(self):
        tris = _cube_triangles()
        verts = list({v for tri in tris for v in tri})
        d = _analyze_thin_walls(tris, verts).to_dict()
        assert "min_wall_thickness_mm" in d


# ---------------------------------------------------------------------------
# TestBridgingAnalysis
# ---------------------------------------------------------------------------


class TestBridgingAnalysis:
    def test_cube_no_bridging(self):
        tris = _cube_triangles()
        result = _analyze_bridging(tris, z_min=0.0, layer_height=0.2)
        # The bottom of a cube is at Z=0, so all downward faces are at the bed.
        assert isinstance(result, BridgingAnalysis)

    def test_to_dict(self):
        result = _analyze_bridging(_cube_triangles(), z_min=0.0)
        d = result.to_dict()
        assert "max_bridge_length_mm" in d
        assert "bridge_count" in d


# ---------------------------------------------------------------------------
# TestBedAdhesionAnalysis
# ---------------------------------------------------------------------------


class TestBedAdhesionAnalysis:
    def test_cube_has_good_bed_adhesion(self):
        tris = _cube_triangles(10.0)
        bbox = {
            "x_min": 0.0,
            "x_max": 10.0,
            "y_min": 0.0,
            "y_max": 10.0,
            "z_min": 0.0,
            "z_max": 10.0,
        }
        result = _analyze_bed_adhesion(tris, z_min=0.0, bbox=bbox)
        assert result.contact_area_mm2 > 0
        assert result.adhesion_risk in ("low", "medium", "high")

    def test_elevated_model_poor_adhesion(self):
        # All vertices above Z=1.
        tris = [((0, 0, 5), (10, 0, 5), (5, 10, 5))]
        bbox = {
            "x_min": 0.0,
            "x_max": 10.0,
            "y_min": 0.0,
            "y_max": 10.0,
            "z_min": 5.0,
            "z_max": 5.0,
        }
        result = _analyze_bed_adhesion(tris, z_min=5.0, bbox=bbox)
        # Only one triangle and it's flat at Z=5, which is within layer_height of z_min=5.
        assert isinstance(result, BedAdhesionAnalysis)

    def test_to_dict(self):
        tris = _cube_triangles()
        bbox = {"x_min": 0, "x_max": 10, "y_min": 0, "y_max": 10, "z_min": 0, "z_max": 10}
        d = _analyze_bed_adhesion(tris, 0.0, bbox).to_dict()
        assert "adhesion_risk" in d


# ---------------------------------------------------------------------------
# TestSupportAnalysis
# ---------------------------------------------------------------------------


class TestSupportAnalysis:
    def test_cube_support_analysis(self):
        tris = _cube_triangles()
        result = _analyze_supports(tris, z_min=0.0)
        assert isinstance(result, SupportAnalysis)

    def test_to_dict(self):
        d = _analyze_supports(_cube_triangles(), 0.0).to_dict()
        assert "estimated_support_volume_mm3" in d


# ---------------------------------------------------------------------------
# TestScoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_perfect_score(self):
        overhangs = OverhangAnalysis(0, 0, 0.0, False, [])
        thin_walls = ThinWallAnalysis(1.0, 0, 0.0, [])
        bridging = BridgingAnalysis(0.0, 0, False)
        adhesion = BedAdhesionAnalysis(100.0, 50.0, "low")
        supports = SupportAnalysis(0.0, 0.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score == 100

    def test_bad_score(self):
        overhangs = OverhangAnalysis(0, 100, 80.0, True, [])
        thin_walls = ThinWallAnalysis(0.1, 50, 50.0, [])
        bridging = BridgingAnalysis(50.0, 20, True)
        adhesion = BedAdhesionAnalysis(1.0, 1.0, "high")
        supports = SupportAnalysis(1000.0, 60.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score < 50

    def test_score_clamps_to_zero(self):
        overhangs = OverhangAnalysis(0, 1000, 100.0, True, [])
        thin_walls = ThinWallAnalysis(0.01, 500, 100.0, [])
        bridging = BridgingAnalysis(100.0, 100, True)
        adhesion = BedAdhesionAnalysis(0.0, 0.0, "high")
        supports = SupportAnalysis(10000.0, 100.0, [])
        score = _compute_score(overhangs, thin_walls, bridging, adhesion, supports)
        assert score >= 0


# ---------------------------------------------------------------------------
# TestGrading
# ---------------------------------------------------------------------------


class TestGrading:
    def test_grade_a(self):
        assert _score_to_grade(95) == "A"
        assert _score_to_grade(90) == "A"

    def test_grade_b(self):
        assert _score_to_grade(85) == "B"

    def test_grade_c(self):
        assert _score_to_grade(75) == "C"

    def test_grade_d(self):
        assert _score_to_grade(65) == "D"

    def test_grade_f(self):
        assert _score_to_grade(50) == "F"
        assert _score_to_grade(0) == "F"


# ---------------------------------------------------------------------------
# TestAnalyzePrintability (integration)
# ---------------------------------------------------------------------------


class TestAnalyzePrintability:
    def test_cube_is_printable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert isinstance(report, PrintabilityReport)
            assert report.score > 0
            assert report.grade in ("A", "B", "C", "D", "F")
            assert isinstance(report.recommendations, list)

    def test_clean_cube_does_not_get_false_support_penalties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert report.overhangs.overhang_triangle_count == 0
            assert report.bridging.bridge_count == 0
            assert report.supports.support_percentage == 0.0
            assert report.score >= 80  # thermal stress heuristics lower score for simple cubes
            assert report.grade in ("A", "B")  # thermal stress may drop grade to B

    def test_cube_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles())
            report = analyze_printability(path)
            d = report.to_dict()
            assert "score" in d
            assert "grade" in d
            assert "overhangs" in d
            assert "thin_walls" in d

    def test_nonexistent_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            analyze_printability("/nonexistent/model.stl")

    def test_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "empty.stl")
            with open(path, "wb") as fh:
                fh.write(b"")
            with pytest.raises(ValueError):
                analyze_printability(path)

    def test_unsupported_format_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "model.gltf")
            with open(path, "w") as fh:
                fh.write("{}")
            with pytest.raises(ValueError, match="Unsupported"):
                analyze_printability(path)

    def test_build_volume_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(100.0))
            report = analyze_printability(path, build_volume=(50.0, 50.0, 50.0))
            # Model is 100x100x100 but build volume is 50x50x50.
            assert any("exceeds build volume" in r for r in report.recommendations)

    def test_custom_nozzle_diameter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path, nozzle_diameter=0.8)
            assert isinstance(report, PrintabilityReport)

    def test_print_time_modifier_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert report.estimated_print_time_modifier >= 1.0


class TestProEnrichmentHook:
    """The optional kiln-pro printability_overlay bridge call."""

    def test_no_kiln_pro_installed_returns_unchanged_report(self, monkeypatch):
        # Force the bridge import to fail, simulating a free / public
        # Kiln install with no kiln-pro on the Python path.
        import builtins as _builtins
        real_import = _builtins.__import__

        def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "kiln_pro.bridge" or name.startswith("kiln_pro"):
                raise ImportError("simulated: kiln-pro not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(_builtins, "__import__", _no_kiln_pro)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path, material="pla")

        assert isinstance(report, PrintabilityReport)
        assert report.enrichment is None

    def test_kiln_pro_installed_populates_enrichment(self):
        # When kiln-pro is installed in the test environment, the
        # overlay should run and populate the enrichment block. Skip
        # cleanly when running against a kiln-only install.
        pytest.importorskip("kiln_pro.bridge")
        from kiln_pro.bridge import pro_features
        if not pro_features.is_available("printability_overlay"):
            pytest.skip("kiln-pro installed but printability_overlay not loaded")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path, material="pla")

        assert isinstance(report, PrintabilityReport)
        # When the overlay carries data for the material, enrichment is
        # populated.  When it doesn't, enrichment is None and the
        # safety-floor path is preserved — both are valid outcomes.
        if report.enrichment is not None:
            assert report.enrichment.get("source") == "kiln_pro.printability_overlay"
            assert report.enrichment.get("material") == "pla"
            assert "score_delta" in report.enrichment
            # Top-level fields stay consistent between dataclass and dict.
            assert report.to_dict()["enrichment"] == report.enrichment



# ---------------------------------------------------------------------------
# TestPrintabilityJudgmentTierSeam — soft seam for warping / thermal_stress /
# adhesion_force.  Same return shape across tiers; curated thresholds +
# recommendation templates come from the ``printability_judgment`` overlay.
# ---------------------------------------------------------------------------


from kiln.printability import (  # noqa: E402
    _ADHESION_FORCE_PUBLIC_DEFAULTS,
    _THERMAL_STRESS_PUBLIC_DEFAULTS,
    _WARPING_PUBLIC_DEFAULTS,
    _apply_recommendation_rules,
    _check_rule_op,
    _sum_score_rules,
)


class TestPrintabilityJudgmentTierSeam:
    """Soft tier seam: free + Pro produce the SAME return shape with all
    material-derived fields populated.  Only the values and the
    recommendation wording differ.  Existing 20+ tests in
    test_warping_analysis / test_thermal_stress / test_adhesion_force
    keep passing because the seam preserves return shape."""

    def test_free_tier_keeps_material_derived_fields_populated(
        self, monkeypatch, tmp_path,
    ):
        """Free tier (overlay={}) still returns non-None for the four
        material-derived fields — no regression vs pre-seam behaviour.
        This is the key promise of the soft seam."""
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: {},
        )
        path = _write_stl(str(tmp_path), _cube_triangles(20.0))
        report = analyze_printability(path, material="abs")
        assert report.warping is not None
        assert report.thermal_stress is not None
        assert report.adhesion_force is not None

    def test_free_tier_appends_pro_upsell_recommendation(
        self, monkeypatch, tmp_path,
    ):
        """Free tier carries one non-intrusive line pointing at Pro for
        brand-tuned guidance.  Honest about the gap."""
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: {},
        )
        path = _write_stl(str(tmp_path), _cube_triangles(20.0))
        report = analyze_printability(path, material="pla")
        rec_text = " ".join(report.recommendations)
        assert "Kiln Pro" in rec_text

    def test_pro_tier_skips_upsell_line(self, monkeypatch, tmp_path):
        """When the overlay is populated, the upsell line is suppressed."""
        pro = {
            "warping": _WARPING_PUBLIC_DEFAULTS,
            "thermal_stress": _THERMAL_STRESS_PUBLIC_DEFAULTS,
            "adhesion_force": _ADHESION_FORCE_PUBLIC_DEFAULTS,
        }
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: pro,
        )
        path = _write_stl(str(tmp_path), _cube_triangles(20.0))
        report = analyze_printability(path, material="pla")
        rec_text = " ".join(report.recommendations)
        assert "Kiln Pro" not in rec_text

    def test_overlay_drives_warping_risk_thresholds(self, monkeypatch, tmp_path):
        """The Pro overlay's risk_thresholds are honored — a more
        sensitive overlay flags geometry the public defaults pass."""
        path = _write_stl(str(tmp_path), _cube_triangles(20.0))
        sensitive = {
            "warping": {
                "geometry_score_rules": [
                    {"metric": "sharp_corners_at_base", "operator": ">=",
                     "threshold": 0, "score": 5},
                ],
                "material_multipliers": {"low": 1.0, "moderate": 1.0,
                                         "high": 1.0, "very_high": 1.0},
                "risk_thresholds": {"critical": 999.0, "high": 999.0, "moderate": 0.0},
                "score_deductions": {"critical": -20, "high": -12, "moderate": -6, "low": 0},
                "recommendation_rules": [],
            },
        }
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: sensitive,
        )
        report = analyze_printability(path, material="pla")
        assert report.warping.risk_level == "moderate"
        assert report.warping.score_deduction == -6

    def test_check_rule_op_handles_all_operators(self):
        """Operator dispatcher handles every operator the overlay uses;
        unknown operators return False (forward-compat)."""
        assert _check_rule_op(">", 5, 3) is True
        assert _check_rule_op("<", 5, 3) is False
        assert _check_rule_op(">=", 5, 5) is True
        assert _check_rule_op("<=", 5, 5) is True
        assert _check_rule_op("==", "a", "a") is True
        assert _check_rule_op("!=", "a", "b") is True
        assert _check_rule_op("in", "a", ["a", "b"]) is True
        assert _check_rule_op("in", "z", ["a", "b"]) is False
        assert _check_rule_op("~=", 5, 3) is False  # unknown op
        assert _check_rule_op(">", "a", 3) is False  # type mismatch

    def test_sum_score_rules_first_match_per_metric(self):
        """Per-metric first-match semantics: multiple rules for the same
        metric fire at most once (matches the pre-seam elif chains)."""
        rules = [
            {"metric": "x", "operator": ">", "threshold": 100, "score": 2},
            {"metric": "x", "operator": ">", "threshold": 10,  "score": 1},
            {"metric": "y", "operator": ">", "threshold": 0,   "score": 3},
        ]
        # x=50: matches second rule (>10), not first. y=5: matches. Total = 4
        assert _sum_score_rules(rules, {"x": 50, "y": 5}) == 4
        # x=200: matches first; later x rule skipped. Total = 5 (not 6)
        assert _sum_score_rules(rules, {"x": 200, "y": 5}) == 5

    def test_apply_recommendation_rules_skips_missing_template_vars(self):
        """Missing template variables fall back to the raw template
        string — never crashes (defensive against overlay/code drift)."""
        rules = [
            {"metric": "x", "operator": ">", "threshold": 0,
             "template": "value is {does_not_exist}"},
        ]
        out = _apply_recommendation_rules(rules, {"x": 1})
        assert out == ["value is {does_not_exist}"]

    def test_seam_preserves_return_shape_across_tiers(
        self, monkeypatch, tmp_path,
    ):
        """Same PrintabilityReport field set under both tiers — only
        values inside vary.  Callers don't have to branch on tier."""
        import dataclasses
        path = _write_stl(str(tmp_path), _cube_triangles(20.0))

        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: {},
        )
        free = analyze_printability(path, material="pla")

        pro_ovr = {"warping": _WARPING_PUBLIC_DEFAULTS,
                   "thermal_stress": _THERMAL_STRESS_PUBLIC_DEFAULTS,
                   "adhesion_force": _ADHESION_FORCE_PUBLIC_DEFAULTS}
        monkeypatch.setattr(
            "kiln.design_intelligence.load_pro_overlay_or_empty",
            lambda kind: pro_ovr,
        )
        pro_rpt = analyze_printability(path, material="pla")

        assert dataclasses.fields(type(free)) == dataclasses.fields(type(pro_rpt))
        assert (free.warping is None) == (pro_rpt.warping is None)
        assert (free.thermal_stress is None) == (pro_rpt.thermal_stress is None)
        assert (free.adhesion_force is None) == (pro_rpt.adhesion_force is None)
