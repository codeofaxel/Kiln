"""Tests for warping risk analysis in the printability engine."""

import json
import struct


def _write_box_stl(path: str, x: float, y: float, z: float) -> None:
    """Write a binary STL rectangular prism with dimensions x*y*z."""
    # Box centered on XY, sitting on Z=0
    hx, hy = x / 2, y / 2
    # 8 vertices
    v = [
        (-hx, -hy, 0),
        (hx, -hy, 0),
        (hx, hy, 0),
        (-hx, hy, 0),  # bottom
        (-hx, -hy, z),
        (hx, -hy, z),
        (hx, hy, z),
        (-hx, hy, z),  # top
    ]
    # 12 triangles (2 per face), with normals
    tris = [
        # Bottom (Z=0, normal down)
        ((0, 0, -1), v[0], v[2], v[1]),
        ((0, 0, -1), v[0], v[3], v[2]),
        # Top (Z=z, normal up)
        ((0, 0, 1), v[4], v[5], v[6]),
        ((0, 0, 1), v[4], v[6], v[7]),
        # Front (Y=-hy)
        ((0, -1, 0), v[0], v[1], v[5]),
        ((0, -1, 0), v[0], v[5], v[4]),
        # Back (Y=hy)
        ((0, 1, 0), v[2], v[3], v[7]),
        ((0, 1, 0), v[2], v[7], v[6]),
        # Left (X=-hx)
        ((-1, 0, 0), v[0], v[4], v[7]),
        ((-1, 0, 0), v[0], v[7], v[3]),
        # Right (X=hx)
        ((1, 0, 0), v[1], v[2], v[6]),
        ((1, 0, 0), v[1], v[6], v[5]),
    ]

    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(tris)))
        for normal, v1, v2, v3 in tris:
            f.write(struct.pack("<3f", *normal))
            f.write(struct.pack("<3f", *v1))
            f.write(struct.pack("<3f", *v2))
            f.write(struct.pack("<3f", *v3))
            f.write(struct.pack("<H", 0))


class TestWarpingAnalysis:
    """Tests for warping risk analysis in the printability engine."""

    def test_flat_plate_high_warp_risk(self, tmp_path):
        """A wide flat plate (200x200x2mm) with ABS should score high/critical warp risk."""
        stl = str(tmp_path / "plate.stl")
        _write_box_stl(stl, 200.0, 200.0, 2.0)

        from kiln.printability import analyze_printability

        # ABS has high warping tendency — big flat plate + ABS = high risk
        report = analyze_printability(stl, material="abs")

        assert report.warping is not None
        assert report.warping.risk_level in ("high", "critical")
        assert report.warping.score_deduction < 0
        assert len(report.warping.large_flat_surfaces) > 0

    def test_flat_plate_pla_moderate_risk(self, tmp_path):
        """A wide flat plate with PLA should be moderate risk (PLA has low warp tendency)."""
        stl = str(tmp_path / "plate_pla.stl")
        _write_box_stl(stl, 200.0, 200.0, 2.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="pla")

        assert report.warping is not None
        # PLA has low warping tendency so even a big plate stays moderate
        assert report.warping.risk_level in ("low", "moderate")
        assert report.warping.material_warping_tendency == "low"
        assert len(report.warping.large_flat_surfaces) > 0

    def test_cube_low_warp_risk(self, tmp_path):
        """A small cube (20x20x20mm) should be low warp risk."""
        stl = str(tmp_path / "cube.stl")
        _write_box_stl(stl, 20.0, 20.0, 20.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="pla")

        assert report.warping is not None
        assert report.warping.risk_level == "low"
        assert report.warping.score_deduction == 0

    def test_tall_thin_part_high_ratio(self, tmp_path):
        """A tall narrow part (10x10x100mm) should flag height-to-base ratio."""
        stl = str(tmp_path / "tower.stl")
        _write_box_stl(stl, 10.0, 10.0, 100.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="pla")

        assert report.warping is not None
        assert report.warping.height_to_base_ratio >= 9.0  # 100/10 = 10
        # Even with low-warp PLA, extreme ratio should bump risk
        assert report.warping.risk_level in ("moderate", "high", "critical")

    def test_material_affects_risk(self, tmp_path):
        """Same geometry, different materials should produce different risk levels."""
        stl = str(tmp_path / "medium_plate.stl")
        _write_box_stl(stl, 100.0, 100.0, 5.0)

        from kiln.printability import analyze_printability

        pla_report = analyze_printability(stl, material="pla")
        abs_report = analyze_printability(stl, material="abs")

        assert pla_report.warping is not None
        assert abs_report.warping is not None
        assert pla_report.warping.material_warping_tendency == "low"
        assert abs_report.warping.material_warping_tendency in ("high", "very_high")
        # ABS should have equal or higher risk than PLA for same geometry
        assert abs_report.warping.score_deduction <= pla_report.warping.score_deduction

    def test_recommendations_include_brim(self, tmp_path):
        """High warp risk should recommend adding a brim."""
        stl = str(tmp_path / "wide_plate.stl")
        _write_box_stl(stl, 200.0, 200.0, 2.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="abs")

        assert report.warping is not None
        recs_text = " ".join(report.warping.recommendations).lower()
        assert "brim" in recs_text

    def test_recommendations_include_chamber(self, tmp_path):
        """ABS with high warp risk should recommend an enclosed chamber."""
        stl = str(tmp_path / "abs_plate.stl")
        _write_box_stl(stl, 200.0, 200.0, 3.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="abs")

        assert report.warping is not None
        recs_text = " ".join(report.warping.recommendations).lower()
        assert "enclos" in recs_text or "chamber" in recs_text

    def test_warping_integrated_in_printability_score(self, tmp_path, monkeypatch):
        """Warping deductions should appear in the overall public printability
        score.

        Tests PUBLIC-tier behavior — this test was previously passing only
        because a Pro-overlay bug (thin-wall over-firing on every clean
        mesh) accidentally deducted 12 points alongside the legitimate
        public warping deduction, masking the fact that Pro's score-cap
        formula erases public-side deductions when Pro has no penalties
        of its own.  The 2026-05-17 thin-wall audit fixed the over-firing
        bug, exposing the cap issue.  This test now isolates from Pro
        enrichment so it actually tests what its name claims.
        """
        # Force-disable Pro enrichment so we test the PUBLIC score path
        # in isolation.  Without this, kiln-pro's overlay layer can
        # raise the enriched_score back above the public deduction
        # (see ``enrich_printability_report``'s cap formula).
        try:
            import kiln_pro.bridge as _bridge

            class _NoPro:
                def is_available(self, _name):
                    return False

            monkeypatch.setattr(_bridge, "pro_features", _NoPro())
        except ImportError:
            pass  # kiln-pro not installed; nothing to disable

        stl = str(tmp_path / "big_plate.stl")
        _write_box_stl(stl, 200.0, 200.0, 2.0)

        from kiln.printability import analyze_printability

        # A huge flat plate with ABS should have significant score impact
        report = analyze_printability(stl, material="abs")

        assert report.warping is not None
        assert report.warping.score_deduction < 0
        # Score should be lower than 100 (warping + possibly other deductions)
        assert report.score < 100

    def test_unknown_material_defaults_moderate(self, tmp_path):
        """Unknown material should default to moderate warping tendency."""
        stl = str(tmp_path / "box.stl")
        _write_box_stl(stl, 50.0, 50.0, 20.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="unobtanium_3000")

        assert report.warping is not None
        assert report.warping.material_warping_tendency == "moderate"

    def test_warping_report_serializable(self, tmp_path):
        """WarpingAnalysis.to_dict() should produce a serializable dict."""
        stl = str(tmp_path / "test.stl")
        _write_box_stl(stl, 50.0, 50.0, 20.0)

        from kiln.printability import analyze_printability

        report = analyze_printability(stl, material="pla")

        assert report.warping is not None
        d = report.warping.to_dict()
        assert isinstance(d, dict)
        # Should be JSON serializable
        json.dumps(d)

    def test_backward_compat_no_material(self, tmp_path):
        """analyze_printability without material param should still work."""
        stl = str(tmp_path / "compat.stl")
        _write_box_stl(stl, 30.0, 30.0, 30.0)

        from kiln.printability import analyze_printability

        # Call without material kwarg -- should default to "pla"
        report = analyze_printability(stl)

        assert report.warping is not None
        assert report.warping.material_warping_tendency == "low"
