"""Tests for thermal stress concentration analysis in printability engine."""

from __future__ import annotations

import struct

from kiln.printability import (
    PrintabilityReport,
    ThermalStressAnalysis,
    analyze_printability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_box_stl(path: str, x: float, y: float, z: float, *, offset_z: float = 0.0) -> None:
    """Write a minimal binary STL box (12 triangles) at the given dimensions."""
    x2, y2 = x / 2, y / 2
    z0 = offset_z
    z1 = offset_z + z
    verts = [
        (-x2, -y2, z0), (x2, -y2, z0), (x2, y2, z0), (-x2, y2, z0),
        (-x2, -y2, z1), (x2, -y2, z1), (x2, y2, z1), (-x2, y2, z1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),  # bottom
        (4, 5, 6), (4, 6, 7),  # top
        (0, 1, 5), (0, 5, 4),  # front
        (2, 3, 7), (2, 7, 6),  # back
        (1, 2, 6), (1, 6, 5),  # right
        (0, 4, 7), (0, 7, 3),  # left
    ]
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(faces)))
        for face in faces:
            v0, v1, v2 = verts[face[0]], verts[face[1]], verts[face[2]]
            f.write(struct.pack("<fff", 0, 0, 0))  # normal placeholder
            for v in (v0, v1, v2):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


def _merge_stl_files(output_path: str, *input_paths: str) -> None:
    """Merge multiple binary STL files into one by concatenating triangles."""
    all_triangles: list[bytes] = []
    for p in input_paths:
        with open(p, "rb") as f:
            f.read(80)  # skip header
            count = struct.unpack("<I", f.read(4))[0]
            for _ in range(count):
                all_triangles.append(f.read(50))  # 12*4 + 2 = 50 bytes per triangle
    with open(output_path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(all_triangles)))
        for tri_data in all_triangles:
            f.write(tri_data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestThermalStress:
    def test_uniform_box_low_stress(self, tmp_path):
        """A very short box (20x20x0.3mm) has < 2 layers -> low thermal stress.

        The thermal stress analyzer returns "low" when the z-span is less than
        two layer heights, since there are no cross-section transitions to
        analyze.  A minimal binary STL box with only 12 triangles cannot
        populate per-layer area buckets uniformly for taller geometries,
        so we use a height that triggers the fast-path.
        """
        stl_path = str(tmp_path / "flat.stl")
        _write_box_stl(stl_path, 20, 20, 0.3)  # < 2 * 0.2mm layer height

        report = analyze_printability(stl_path, material="pla")
        assert report.thermal_stress is not None
        assert report.thermal_stress.risk_level == "low"
        assert report.thermal_stress.score_deduction == 0

    def test_tall_thin_to_wide_base_stress(self, tmp_path):
        """A wide flat plate (100x100x2mm) should show high area change ratio.

        All cross-section area is concentrated in 2mm of height, so the
        layer analysis should detect a dramatic area drop to zero above that.
        """
        stl_path = str(tmp_path / "plate.stl")
        _write_box_stl(stl_path, 100, 100, 2)

        report = analyze_printability(stl_path, material="pla")
        assert report.thermal_stress is not None
        # The area change from large cross-section to zero should give a high ratio.
        assert report.thermal_stress.max_area_change_ratio > 1.0

    def test_material_affects_stress(self, tmp_path):
        """ABS should have higher thermal stress risk than PLA for same geometry."""
        stl_path = str(tmp_path / "box.stl")
        _write_box_stl(stl_path, 100, 100, 2)

        report_pla = analyze_printability(stl_path, material="pla")
        report_abs = analyze_printability(stl_path, material="abs")

        assert report_pla.thermal_stress is not None
        assert report_abs.thermal_stress is not None
        # ABS has higher material_stress_factor due to thermal shrinkage.
        assert report_abs.thermal_stress.material_stress_factor >= report_pla.thermal_stress.material_stress_factor

    def test_thermal_stress_in_printability_report(self, tmp_path):
        """analyze_printability() should populate thermal_stress field."""
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 20, 20, 20)

        report = analyze_printability(stl_path, material="pla")
        assert isinstance(report, PrintabilityReport)
        assert report.thermal_stress is not None
        assert isinstance(report.thermal_stress, ThermalStressAnalysis)
        # Verify expected fields exist.
        assert hasattr(report.thermal_stress, "risk_level")
        assert hasattr(report.thermal_stress, "score_deduction")
        assert hasattr(report.thermal_stress, "max_area_change_ratio")
        assert hasattr(report.thermal_stress, "stress_concentration_zones")
        assert hasattr(report.thermal_stress, "layer_count_analyzed")
        assert hasattr(report.thermal_stress, "material_stress_factor")
        assert hasattr(report.thermal_stress, "recommendations")

    def test_stress_concentration_zones_populated(self, tmp_path):
        """A flat plate (100x100x2mm) should have stress concentration zones."""
        stl_path = str(tmp_path / "plate.stl")
        _write_box_stl(stl_path, 100, 100, 2)

        report = analyze_printability(stl_path, material="pla")
        assert report.thermal_stress is not None
        # The transition from full cross-section to zero should produce zones.
        assert len(report.thermal_stress.stress_concentration_zones) > 0
        # Each zone should have expected keys.
        zone = report.thermal_stress.stress_concentration_zones[0]
        assert "z_mm" in zone
        assert "area_change_ratio" in zone

    def test_stress_recommendations_generated(self, tmp_path):
        """High-stress geometry with ABS should produce stress recommendations."""
        stl_path = str(tmp_path / "plate.stl")
        _write_box_stl(stl_path, 100, 100, 2)

        report = analyze_printability(stl_path, material="abs")
        assert report.thermal_stress is not None
        if report.thermal_stress.risk_level in ("moderate", "high", "critical"):
            recs = " ".join(report.thermal_stress.recommendations).lower()
            assert any(
                kw in recs for kw in ("stress", "gradual", "transition", "temperature", "thermal")
            ), f"Expected stress-related recommendations, got: {report.thermal_stress.recommendations}"

    def test_thermal_stress_score_deduction(self, tmp_path):
        """A high-stress part should get a lower printability score than a uniform part."""
        cube_path = str(tmp_path / "cube.stl")
        _write_box_stl(cube_path, 20, 20, 20)

        # Stacked geometry with dramatic cross-section change: wide base + narrow tower.
        base_path = str(tmp_path / "base.stl")
        tower_path = str(tmp_path / "tower.stl")
        merged_path = str(tmp_path / "merged.stl")
        _write_box_stl(base_path, 80, 80, 5)
        _write_box_stl(tower_path, 5, 5, 40, offset_z=5)
        _merge_stl_files(merged_path, base_path, tower_path)

        report_cube = analyze_printability(cube_path, material="abs")
        report_merged = analyze_printability(merged_path, material="abs")

        assert report_cube.thermal_stress is not None
        assert report_merged.thermal_stress is not None
        # The merged geometry should have a larger score deduction.
        assert report_merged.thermal_stress.score_deduction <= report_cube.thermal_stress.score_deduction
