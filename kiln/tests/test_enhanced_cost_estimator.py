"""Tests for enhanced print cost estimation."""

import json
import struct

import pytest


def _write_box_stl(path: str, x: float, y: float, z: float) -> None:
    """Write a binary STL rectangular prism with dimensions x*y*z."""
    hx, hy = x / 2, y / 2
    v = [
        (-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0),
        (-hx, -hy, z), (hx, -hy, z), (hx, hy, z), (-hx, hy, z),
    ]
    tris = [
        ((0, 0, -1), v[0], v[2], v[1]), ((0, 0, -1), v[0], v[3], v[2]),
        ((0, 0, 1), v[4], v[5], v[6]), ((0, 0, 1), v[4], v[6], v[7]),
        ((0, -1, 0), v[0], v[1], v[5]), ((0, -1, 0), v[0], v[5], v[4]),
        ((0, 1, 0), v[2], v[3], v[7]), ((0, 1, 0), v[2], v[7], v[6]),
        ((-1, 0, 0), v[0], v[4], v[7]), ((-1, 0, 0), v[0], v[7], v[3]),
        ((1, 0, 0), v[1], v[2], v[6]), ((1, 0, 0), v[1], v[6], v[5]),
    ]
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for normal, v1, v2, v3 in tris:
            f.write(struct.pack("<3f", *normal))
            f.write(struct.pack("<3f", *v1))
            f.write(struct.pack("<3f", *v2))
            f.write(struct.pack("<3f", *v3))
            f.write(struct.pack("<H", 0))


class TestEnhancedCostEstimator:
    """Tests for enhanced cost estimation from mesh geometry."""

    def test_estimate_from_mesh_basic(self, tmp_path):
        """A 20x20x20mm cube with PLA should have reasonable weight and cost."""
        stl = str(tmp_path / "cube.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        estimate = estimator.estimate_from_mesh(stl, material="PLA")

        assert estimate.filament_weight_grams > 0
        assert estimate.total_cost_usd > 0
        assert estimate.material == "PLA"
        # A 20mm cube at 20% infill should weigh roughly 5-15g
        assert 1.0 < estimate.filament_weight_grams < 30.0

    def test_estimate_from_mesh_with_supports(self, tmp_path):
        """Support cost should be added when include_supports=True."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 30.0, 30.0, 30.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        without = estimator.estimate_from_mesh(stl, include_supports=False)
        with_supports = estimator.estimate_from_mesh(stl, include_supports=True)

        # With supports should cost >= without supports
        assert with_supports.total_cost_usd >= without.total_cost_usd
        assert with_supports.support_weight_grams >= 0

    def test_estimate_from_mesh_with_brim(self, tmp_path):
        """Brim adhesion should add small cost."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        no_brim = estimator.estimate_from_mesh(stl, adhesion_type="none")
        with_brim = estimator.estimate_from_mesh(stl, adhesion_type="brim")

        assert with_brim.adhesion_weight_grams > 0
        assert with_brim.adhesion_cost_usd > 0
        # Brim adds a tiny cost — compare unrounded breakdown sums
        brim_total = sum(with_brim.cost_breakdown.values())
        no_brim_total = sum(no_brim.cost_breakdown.values())
        assert brim_total > no_brim_total

    def test_estimate_from_mesh_with_raft(self, tmp_path):
        """Raft should cost more than brim for same part."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        brim = estimator.estimate_from_mesh(stl, adhesion_type="brim")
        raft = estimator.estimate_from_mesh(stl, adhesion_type="raft")

        assert raft.adhesion_weight_grams > brim.adhesion_weight_grams
        assert raft.adhesion_cost_usd > brim.adhesion_cost_usd

    def test_cost_breakdown_sums_to_total(self, tmp_path):
        """cost_breakdown values should sum to total_cost_usd."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 40.0, 40.0, 40.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        estimate = estimator.estimate_from_mesh(
            stl, include_supports=True, adhesion_type="brim"
        )

        breakdown_sum = sum(estimate.cost_breakdown.values())
        assert abs(breakdown_sum - estimate.total_cost_usd) < 0.02  # rounding tolerance

    def test_different_materials_different_costs(self, tmp_path):
        """PLA vs PETG vs ABS should produce different costs."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 30.0, 30.0, 30.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        pla = estimator.estimate_from_mesh(stl, material="PLA")
        petg = estimator.estimate_from_mesh(stl, material="PETG")

        # Different materials should produce different costs
        assert pla.filament_cost_usd != petg.filament_cost_usd

    def test_new_materials_exist(self):
        """Verify new materials are in BUILTIN_MATERIALS."""
        from kiln.cost_estimator import BUILTIN_MATERIALS

        for mat in ["PLA+", "CF-PLA", "SILK-PLA", "HIPS", "PVA", "PP", "PEEK"]:
            assert mat in BUILTIN_MATERIALS, f"{mat} missing from BUILTIN_MATERIALS"

    def test_infill_affects_cost(self, tmp_path):
        """100% infill should cost more than 20% infill."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 30.0, 30.0, 30.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        low = estimator.estimate_from_mesh(stl, infill_percent=20.0)
        high = estimator.estimate_from_mesh(stl, infill_percent=100.0)

        assert high.filament_weight_grams > low.filament_weight_grams
        assert high.total_cost_usd > low.total_cost_usd

    def test_cost_analysis_in_printability(self, tmp_path):
        """CostAnalysis should appear in PrintabilityReport."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 30.0, 30.0, 30.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="pla")

        assert report.cost is not None
        assert report.cost.estimated_cost_usd > 0
        assert report.cost.weight_grams > 0
        assert isinstance(report.cost.cost_breakdown, dict)
        assert "filament" in report.cost.cost_breakdown

    def test_cost_saving_recommendations(self, tmp_path):
        """Cost recommendations should be generated for prints."""
        stl = str(tmp_path / "large_box.stl")
        _write_box_stl(stl, 100.0, 100.0, 100.0)

        from kiln.printability import analyze_printability

        # Use expensive material at high infill to trigger recommendations
        report = analyze_printability(stl, material="petg", infill_percent=50.0)

        assert report.cost is not None
        assert len(report.cost.cost_saving_recommendations) > 0

    def test_cost_estimate_serializable(self, tmp_path):
        """CostEstimate.to_dict() should produce JSON-serializable output."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        estimate = estimator.estimate_from_mesh(stl)

        d = estimate.to_dict()
        assert isinstance(d, dict)
        json.dumps(d)  # Should not raise

    def test_estimate_from_mesh_unknown_material(self, tmp_path):
        """Unknown material should default to PLA with a warning."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        estimate = estimator.estimate_from_mesh(stl, material="unobtanium")

        assert estimate.material == "PLA"
        assert any("unknown" in w.lower() or "unobtanium" in w.lower() for w in estimate.warnings)

    def test_zero_volume_mesh_raises(self, tmp_path):
        """A degenerate mesh with zero volume should raise ValueError."""
        # Write a flat triangle (zero volume)
        stl = str(tmp_path / "flat.stl")
        with open(stl, "wb") as f:
            f.write(b"\x00" * 80)
            f.write(struct.pack("<I", 1))
            # Single flat triangle
            f.write(struct.pack("<3f", 0, 0, 1))  # normal
            f.write(struct.pack("<3f", 0, 0, 0))
            f.write(struct.pack("<3f", 10, 0, 0))
            f.write(struct.pack("<3f", 5, 10, 0))
            f.write(struct.pack("<H", 0))

        from kiln.cost_estimator import CostEstimator

        estimator = CostEstimator()
        with pytest.raises(ValueError, match="volume"):
            estimator.estimate_from_mesh(stl)
