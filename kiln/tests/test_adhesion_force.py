"""Tests for adhesion force estimation in printability engine."""

from __future__ import annotations

import struct

import pytest

from kiln.printability import (
    AdhesionForceEstimate,
    PrintabilityReport,
    analyze_printability,
)


def _overlay_available() -> bool:
    """True when kiln-pro's per-material printability overlay is loaded.

    A subset of the assertions below describe Pro-tier behavior — they
    expect curated per-material values (e.g. PP adhesion_strength=0.03,
    shrinkage=0.018) that ``_material_physics_from_overlay`` only
    returns when the kiln-pro package is installed and its overlay
    module is reachable.  In a clean public-Kiln environment the
    defaults are uniform (adhesion_strength=0.10, shrinkage=0.005) and
    those assertions are inapplicable, so the affected tests skip.
    """
    try:
        from kiln_pro.bridge import pro_features  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        return bool(pro_features.is_available("printability_overlay"))
    except Exception:  # noqa: BLE001
        return False


_pro_overlay_required = pytest.mark.skipif(
    not _overlay_available(),
    reason="requires kiln-pro printability_overlay for per-material physics",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdhesionForce:
    def test_small_cube_secure_adhesion(self, tmp_path):
        """A 20x20x20mm PLA cube should have secure adhesion."""
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 20, 20, 20)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        assert report.adhesion_force.force_ratio > 1.0
        assert report.adhesion_force.will_detach is False

    @_pro_overlay_required
    def test_tall_narrow_poor_adhesion(self, tmp_path):
        """A 2x2x250mm PP tower should have poor adhesion.

        The adhesion force model uses peel_force = shrinkage * longest_xy * z * 0.01.
        To trigger marginal/likely_detach, we need force_ratio < 3.0.  PP has
        the weakest adhesion_strength (0.03) and highest shrinkage (0.018), so
        a tiny base with extreme height is required.
        - adhesion = 4 * 0.03 = 0.12 N
        - peel = 0.018 * 2 * 250 * 0.01 = 0.09 N
        - ratio = 0.12 / 0.09 = 1.33 -> marginal
        """
        stl_path = str(tmp_path / "tower.stl")
        _write_box_stl(stl_path, 2, 2, 250)

        report = analyze_printability(stl_path, material="pp")
        assert report.adhesion_force is not None
        # Tiny contact area + extreme height + PP shrinkage -> poor adhesion.
        assert report.adhesion_force.risk_level in ("marginal", "likely_detach")

    def test_pp_terrible_adhesion(self, tmp_path):
        """A 30x30x10mm PP part is still 'secure' but gets adhesion recommendations.

        PP has terrible adhesion on standard build surfaces (adhesion_strength=0.03),
        but the force-balance model's peel force is small for compact geometries.
        The implementation instead flags PP via material-specific recommendations
        regardless of the force ratio.
        """
        stl_path = str(tmp_path / "pp_box.stl")
        _write_box_stl(stl_path, 30, 30, 10)

        report = analyze_printability(stl_path, material="pp")
        assert report.adhesion_force is not None
        # PP gets a recommendation about specialized build sheets even when secure.
        recs = " ".join(report.adhesion_force.recommendations).lower()
        assert "pp" in recs or "adhesion" in recs or "build" in recs

    @_pro_overlay_required
    def test_pla_vs_abs_adhesion(self, tmp_path):
        """PLA should have better adhesion than ABS for the same geometry."""
        stl_path = str(tmp_path / "box.stl")
        _write_box_stl(stl_path, 30, 30, 30)

        report_pla = analyze_printability(stl_path, material="pla")
        report_abs = analyze_printability(stl_path, material="abs")

        assert report_pla.adhesion_force is not None
        assert report_abs.adhesion_force is not None
        # PLA should have a higher force_ratio (better adhesion).
        assert report_pla.adhesion_force.force_ratio > report_abs.adhesion_force.force_ratio

    def test_large_flat_part_good_adhesion(self, tmp_path):
        """A 100x100x5mm flat plate with PLA should have excellent adhesion."""
        stl_path = str(tmp_path / "plate.stl")
        _write_box_stl(stl_path, 100, 100, 5)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        # Large contact area (10,000mm2), low height -> very secure.
        assert report.adhesion_force.force_ratio > 1.0
        assert report.adhesion_force.will_detach is False
        assert report.adhesion_force.risk_level == "secure"

    def test_adhesion_force_in_printability_report(self, tmp_path):
        """analyze_printability() should populate adhesion_force field."""
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 20, 20, 20)

        report = analyze_printability(stl_path, material="pla")
        assert isinstance(report, PrintabilityReport)
        assert report.adhesion_force is not None
        assert isinstance(report.adhesion_force, AdhesionForceEstimate)
        # Verify expected fields exist.
        assert hasattr(report.adhesion_force, "adhesion_force_n")
        assert hasattr(report.adhesion_force, "peel_force_n")
        assert hasattr(report.adhesion_force, "force_ratio")
        assert hasattr(report.adhesion_force, "will_detach")
        assert hasattr(report.adhesion_force, "risk_level")
        assert hasattr(report.adhesion_force, "score_deduction")
        assert hasattr(report.adhesion_force, "recommendations")

    @_pro_overlay_required
    def test_will_detach_flag(self, tmp_path):
        """A geometry that triggers will_detach=True should recommend brim/raft.

        PP with a 1x1x500mm tower:
        - adhesion = 1 * 0.03 = 0.03 N
        - peel = 0.018 * 1 * 500 * 0.01 = 0.09 N
        - ratio = 0.03 / 0.09 = 0.33 -> likely_detach, will_detach=True
        """
        stl_path = str(tmp_path / "tower.stl")
        _write_box_stl(stl_path, 1, 1, 500)

        report = analyze_printability(stl_path, material="pp")
        assert report.adhesion_force is not None
        assert report.adhesion_force.will_detach is True
        recs = " ".join(report.adhesion_force.recommendations).lower()
        assert any(
            kw in recs for kw in ("brim", "raft", "detach", "adhesion")
        ), f"Expected adhesion recommendations, got: {report.adhesion_force.recommendations}"

    @_pro_overlay_required
    def test_adhesion_score_deduction(self, tmp_path):
        """A part with likely_detach should get score_deduction of -10.

        PP with a 1x1x500mm tower triggers will_detach=True.
        """
        stl_path = str(tmp_path / "tower.stl")
        _write_box_stl(stl_path, 1, 1, 500)

        report = analyze_printability(stl_path, material="pp")
        assert report.adhesion_force is not None
        assert report.adhesion_force.risk_level == "likely_detach"
        assert report.adhesion_force.score_deduction == -10

    def test_force_ratio_calculation(self, tmp_path):
        """Verify force_ratio = adhesion_force_n / peel_force_n."""
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 20, 20, 20)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        expected_ratio = report.adhesion_force.adhesion_force_n / report.adhesion_force.peel_force_n
        assert abs(report.adhesion_force.force_ratio - expected_ratio) < 0.01, (
            f"force_ratio {report.adhesion_force.force_ratio} != "
            f"adhesion/peel {expected_ratio}"
        )

    def test_geometry_guard_flags_extreme_aspect_ratio(self, tmp_path):
        """Geometry guard: aspect_ratio > 50 forces secure → marginal.

        Pure-geometry check that fires regardless of material or
        overlay availability.  Catches the failure mode where the
        force-balance model says ``secure`` but the bounding-box
        aspect ratio is extreme enough that dynamic peel stress
        will detach the print in practice.  Runs in clean CI
        without needing the kiln-pro overlay.

        Test geometry: 1x1x100mm tower (aspect ratio 100, well
        above the 50 threshold).  Force balance with public
        defaults would otherwise rate this "secure" because the
        small contact area also means small peel force.
        """
        stl_path = str(tmp_path / "thin_tower.stl")
        _write_box_stl(stl_path, 1, 1, 100)

        # PLA is intentional: PLA's strong adhesion means without
        # the geometry guard, even PLA at this aspect ratio passes
        # the force-balance check.  This pins the guard's effect.
        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        assert report.adhesion_force.risk_level in ("marginal", "likely_detach"), (
            f"Geometry guard should have flagged aspect ratio 100, "
            f"got risk_level={report.adhesion_force.risk_level}"
        )
        recs = " ".join(report.adhesion_force.recommendations).lower()
        assert any(
            kw in recs for kw in ("aspect", "tall", "narrow", "geometry")
        ), (
            f"Expected geometry-based recommendation, got: "
            f"{report.adhesion_force.recommendations}"
        )

    def test_geometry_guard_leaves_normal_geometry_secure(self, tmp_path):
        """Geometry guard does NOT downgrade compact prints to marginal.

        A 30x30x30 PLA cube has aspect ratio 1.0 — far below the
        50 threshold — so the guard must not fire.  Pins the
        no-false-positive side of the contract.
        """
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 30, 30, 30)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        assert report.adhesion_force.risk_level == "secure", (
            f"Compact geometry must remain 'secure', got "
            f"risk_level={report.adhesion_force.risk_level}"
        )

    def test_model_confidence_high_on_clear_extreme(self, tmp_path):
        """model_confidence is 'high' on a clearly-secure cube.

        A 30x30x30 PLA cube produces a force ratio well above 10,
        which is the upper boundary of the "approximate" middle
        range.  Pins the contract that high-confidence verdicts
        survive any future tuning of the boundary.
        """
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 30, 30, 30)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        assert report.adhesion_force.model_confidence == "high", (
            f"Cube ratio {report.adhesion_force.force_ratio} should be "
            f"high-confidence secure; got model_confidence="
            f"{report.adhesion_force.model_confidence}"
        )

    def test_model_confidence_approximate_when_geometry_guard_fires(self, tmp_path):
        """Geometry-guard upgrade flips model_confidence to 'approximate'.

        When the force-balance model says "secure" but the
        geometry guard upgrades to "marginal", the verdict was
        produced by a heuristic on top of an uncertain model —
        so the confidence band must reflect that.  Pins the
        contract so callers can branch on confidence to soften
        wording in agent replies.
        """
        stl_path = str(tmp_path / "tower.stl")
        _write_box_stl(stl_path, 1, 1, 100)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        assert report.adhesion_force.risk_level in ("marginal", "likely_detach")
        assert report.adhesion_force.model_confidence == "approximate", (
            f"Geometry-guarded verdict should be 'approximate', got "
            f"model_confidence={report.adhesion_force.model_confidence}"
        )
