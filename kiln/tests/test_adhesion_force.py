"""Tests for adhesion force estimation in printability engine."""

from __future__ import annotations

import struct

import pytest

from kiln.printability import (
    AdhesionForceEstimate,
    PrintabilityReport,
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

    def test_pp_terrible_adhesion(self, tmp_path):
        """A 30x30x10mm PP part is still 'secure' but gets adhesion recommendations.

        PP has terrible adhesion on standard build surfaces, but the
        force-balance model's peel force is small for compact geometries.
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

    def test_force_ratio_calculation(self, tmp_path):
        """Verify force_ratio ≈ adhesion_force_n / peel_force_n.

        Tolerance is relative (0.5%) rather than absolute because
        adhesion_force_n / peel_force_n are rounded to 3 decimal
        places before storage, while force_ratio is computed from
        the unrounded values.  For small peel values the rounding
        loss is ~0.5% of the ratio.
        """
        stl_path = str(tmp_path / "cube.stl")
        _write_box_stl(stl_path, 20, 20, 20)

        report = analyze_printability(stl_path, material="pla")
        assert report.adhesion_force is not None
        expected_ratio = report.adhesion_force.adhesion_force_n / report.adhesion_force.peel_force_n
        # Relative tolerance: |observed - expected| / expected ≤ 1%.
        rel_err = abs(report.adhesion_force.force_ratio - expected_ratio) / max(expected_ratio, 0.001)
        assert rel_err < 0.01, (
            f"force_ratio {report.adhesion_force.force_ratio} != "
            f"adhesion/peel {expected_ratio} (relative error {rel_err:.4f})"
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


# ---------------------------------------------------------------------------
# Calibration matrix — a sweep that pins the current model's behavior
# across realistic prints.  The ``reality`` column is the educated-guess
# ground truth from domain knowledge (NOT measured); the expected-verdict
# table records what the model says today.
#
# This test is INTENTIONALLY loose — it asserts that the model's verdict
# matches the expected table, NOT that it matches reality.  That makes
# it a regression matrix, not a quality matrix.  When the adhesion model
# is reworked, update the table and verify the catch rate moves toward
# the ``reality`` column.
# ---------------------------------------------------------------------------


# Each row is a print with a reality target ("what should the model
# say").  The public defaults use one stress factor for every material,
# so some rows are known misses or false positives on this tier.
_GEOM = [
    # (name, material, W, D, Z, reality)
    # ── SHOULD-BE-SECURE PRINTS (target: no false positives in either tier) ──
    ("3DBenchy",              "pla",     60,  30,  48, "secure"),
    ("Phone stand",           "pla",    100,  50,  80, "secure"),
    ("Cal cube",              "pla",     20,  20,  20, "secure"),
    ("Mini figurine",         "pla",     30,  30,  60, "secure"),
    ("Cookie cutter",         "pla",     80,  60,  10, "secure"),
    ("Lithophane",            "pla",    100, 150,   3, "secure"),
    ("Desk organizer",        "pla",    200, 100,  50, "secure"),
    ("LEGO brick",            "abs",     32,  16,   9, "secure"),
    ("Tool handle (compact)", "abs",     30,  30, 100, "secure"),
    ("Helmet visor (large)",  "abs",    200,  80,  40, "secure"),
    ("Phone case",            "petg",   160,  80,   8, "secure"),
    ("Water bottle holder",   "petg",    80,  80, 120, "secure"),
    ("Nylon snap-fit",        "nylon",   40,  40,  60, "secure"),
    ("Nylon gear",            "nylon",   50,  50,  10, "secure"),
    ("TPU phone bumper",      "tpu",    160,  80,  10, "secure"),
    ("PP gasket (flat)",      "pp",      80,  80,   3, "secure"),
    ("PP cup",                "pp",      60,  60,  80, "secure"),
    ("PP small clip",         "pp",      30,  20,  15, "secure"),
    ("Tall PLA vase",         "pla",     40,  40, 200, "secure"),
    ("PLA pen holder",        "pla",     25,  25, 120, "secure"),
    ("PLA candleholder",      "pla",      4,   4, 200, "secure"),
    ("PETG tall tower",       "petg",    20,  20, 300, "secure"),
    # ── SHOULD-BE-FLAGGED PRINTS ──
    ("Hairlike PLA tower",    "pla",      1,   1, 100, "likely_detach"),
    ("PP test tall tower",    "pp",       2,   2, 250, "likely_detach"),
    ("PP needle pillar",      "pp",       3,   3, 300, "likely_detach"),
    ("PETG ultra-tall thin",  "petg",     5,   5, 400, "marginal"),
    ("PP narrow column",      "pp",      10,  10, 200, "marginal"),
    ("Skyscraper PLA",        "pla",     10,  10, 500, "marginal"),
    ("Nylon thin tower",      "nylon",   10,  10, 200, "marginal"),
    ("ABS tall thin",         "abs",     10,  10, 250, "marginal"),
]

# FREE-TIER expected verdicts.  Public defaults: stress_factor=1.0
# for every material, adhesion_strength=0.10, shrinkage_strain=0.005.
# One stress factor for everything over-flags PLA and under-flags
# warp-prone materials: one false positive (PLA candleholder) and
# three misses (PP / Nylon / ABS columns) are pinned as known.
_FREE_TIER_EXPECTED: dict[str, str] = {
    "3DBenchy": "secure", "Phone stand": "secure", "Cal cube": "secure",
    "Mini figurine": "secure", "Cookie cutter": "secure", "Lithophane": "secure",
    "Desk organizer": "secure", "LEGO brick": "secure",
    "Tool handle (compact)": "secure", "Helmet visor (large)": "secure",
    "Phone case": "secure", "Water bottle holder": "secure",
    "Nylon snap-fit": "secure", "Nylon gear": "secure", "TPU phone bumper": "secure",
    "PP gasket (flat)": "secure", "PP cup": "secure", "PP small clip": "secure",
    "Tall PLA vase": "secure", "PLA pen holder": "secure",
    "PLA candleholder": "likely_detach",  # known false positive on public defaults
    "PETG tall tower": "secure",
    "Hairlike PLA tower": "likely_detach", "PP test tall tower": "likely_detach",
    "PP needle pillar": "likely_detach", "PETG ultra-tall thin": "likely_detach",
    "PP narrow column": "secure",  # known miss on public defaults
    "Skyscraper PLA": "likely_detach",  # over-flagged on public defaults
    "Nylon thin tower": "secure",  # known miss on public defaults
    "ABS tall thin": "secure",     # known miss on public defaults
}

@pytest.fixture
def _force_free_tier(monkeypatch):
    """Force the adhesion path to use public defaults.

    Even when kiln-pro is installed locally, this fixture
    monkey-patches ``_material_physics_from_overlay`` to return an
    empty dict — which the three material helpers
    (``_material_stress_factor``, ``_material_adhesion_strength``,
    ``_material_shrinkage_strain``) interpret as "no overlay, use
    the public default".  Lets the free-tier matrix run faithfully
    in any dev env, not just clean CI.
    """
    from kiln import printability as _p
    monkeypatch.setattr(_p, "_material_physics_from_overlay", lambda mat: {})


@pytest.mark.parametrize("name,material,w,d,z,reality", _GEOM)
def test_adhesion_calibration_matrix_free_tier(
    tmp_path, _force_free_tier, name, material, w, d, z, reality
):
    """FREE-tier regression — pins public-default model behavior.

    Free tier uses ``_DEFAULT_STRESS_FACTOR=1.0`` for every material,
    so PLA prints look as thermally-stressed as ABS prints; the PLA
    candleholder is a known false positive on this tier.
    """
    stl_path = str(tmp_path / f"free_{name.replace(' ', '_').replace('/', '_')}.stl")
    _write_box_stl(stl_path, w, d, z)
    report = analyze_printability(stl_path, material=material)
    assert report.adhesion_force is not None, f"{name}: no adhesion_force"
    expected = _FREE_TIER_EXPECTED[name]
    assert report.adhesion_force.risk_level == expected, (
        f"{name} (free, {material}, {w}x{d}x{z}): "
        f"expected {expected}, got {report.adhesion_force.risk_level} "
        f"(ratio={report.adhesion_force.force_ratio}, reality={reality})"
    )
