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
        """A 30x30x10mm PP part is never called secure and gets adhesion recommendations.

        PP has terrible adhesion on standard build surfaces, and the
        force-balance model's peel force is small for compact geometries,
        so the verdict is withheld from secure for the plastic alone, and
        PP is flagged via material-specific recommendations too.
        """
        stl_path = str(tmp_path / "pp_box.stl")
        _write_box_stl(stl_path, 30, 30, 10)

        report = analyze_printability(stl_path, material="pp")
        assert report.adhesion_force is not None
        assert report.adhesion_force.risk_level == "marginal"
        assert report.adhesion_force.secure_withheld_for == "PP"
        recs = " ".join(report.adhesion_force.recommendations).lower()
        assert "build sheet" in recs and "standard build surface" in recs, recs

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
# so some rows are flagged harder than their reality on this tier.
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
    ("PETG tall tower",       "petg",    20,  20, 300, "secure"),
    # ── SHOULD-BE-FLAGGED PRINTS ──
    # Was "secure" by guess; its 4 mm footing cannot hold the nozzle's
    # sideways push at the top of a 200 mm part, so it is flagged.
    ("PLA candleholder",      "pla",      4,   4, 200, "likely_detach"),
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
# The push test catches the PP / Nylon / ABS columns the peel balance
# alone used to miss; the rows flagged harder than their reality are
# in the safe direction.  A part in PP, nylon, PC or ABS is never called
# secure on a standard build surface, on any tier, so those compact rows
# read marginal.
_FREE_TIER_EXPECTED: dict[str, str] = {
    "3DBenchy": "secure", "Phone stand": "secure", "Cal cube": "secure",
    "Mini figurine": "secure", "Cookie cutter": "secure", "Lithophane": "secure",
    "Desk organizer": "secure", "LEGO brick": "marginal",
    "Tool handle (compact)": "marginal", "Helmet visor (large)": "marginal",
    "Phone case": "secure", "Water bottle holder": "secure",
    "Nylon snap-fit": "marginal", "Nylon gear": "marginal", "TPU phone bumper": "secure",
    "PP gasket (flat)": "marginal", "PP cup": "marginal", "PP small clip": "marginal",
    "Tall PLA vase": "secure", "PLA pen holder": "secure",
    "PLA candleholder": "likely_detach",
    "PETG tall tower": "secure",
    "Hairlike PLA tower": "likely_detach", "PP test tall tower": "likely_detach",
    "PP needle pillar": "likely_detach", "PETG ultra-tall thin": "likely_detach",
    "PP narrow column": "likely_detach",
    "Skyscraper PLA": "likely_detach",  # over-flagged on public defaults
    "Nylon thin tower": "likely_detach",
    "ABS tall thin": "likely_detach",
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
    so PLA prints look as thermally-stressed as ABS prints.
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


# ---------------------------------------------------------------------------
# The push test: a tall part on a narrow footing
# ---------------------------------------------------------------------------


def _tool_reply(stl_path: str, material: str = "pla") -> dict:
    """The analyze_printability TOOL's reply: the words an agent reads."""
    from kiln.plugins.printability_tools import plugin

    tools: dict = {}

    class _Mcp:
        def tool(self, *_a, **_kw):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    reply = tools["analyze_printability"](stl_path, material=material)
    assert reply.get("success") is True, reply
    return reply


def _says_how_to_hold_it(reply: dict) -> bool:
    """The sentence names the hold-down the report itself decided."""
    rec = reply["report"]["adhesion"]
    if rec["use_raft"]:
        return "raft" in reply["message"]
    return rec["brim_width_mm"] > 0 and f"{rec['brim_width_mm']} mm brim" in reply["message"]


def _mesh_stl(path, mesh) -> str:
    """*mesh* written standing on the bed."""
    mesh.apply_translation((0.0, 0.0, -mesh.bounds[0][2]))
    mesh.export(str(path))
    return str(path)


def test_a_part_on_a_narrow_footing_is_told_it_can_come_off_the_bed(tmp_path, _force_free_tier):
    """A 4 x 4 x 200 mm PLA pillar stands on all of its footprint, so the
    contact bands never see it, and it used to be "Ready to print." and
    nothing else.  Its footing cannot hold the nozzle's sideways push at
    its full height: the tool's sentence now says it is likely to come off
    the bed and how to hold it down.  So does a wall 1.2 mm thick standing
    on edge, and a 10 mm rod 50 mm tall is told it could.  A wide pillar,
    and an open tube that stands on its rim (stable because the rim is
    wide, though it touches little of the bed), are told nothing of the
    kind.
    """
    import trimesh

    pillar = str(tmp_path / "pillar.stl")
    _write_box_stl(pillar, 4, 4, 200)
    reply = _tool_reply(pillar)
    force = reply["report"]["adhesion_force"]
    assert force["risk_level"] == "likely_detach", force
    assert force["will_detach"] is True, force
    assert "likely to come off the bed" in reply["message"], reply["message"]
    assert _says_how_to_hold_it(reply), (reply["message"], reply["report"]["adhesion"])

    edge_on = _tool_reply(_mesh_stl(tmp_path / "fin.stl", trimesh.creation.box(extents=(150.0, 1.2, 40.0))))
    assert "likely to come off the bed" in edge_on["message"], edge_on["message"]
    assert _says_how_to_hold_it(edge_on), edge_on["message"]
    rod = _tool_reply(_mesh_stl(tmp_path / "rod.stl", trimesh.creation.cylinder(radius=5.0, height=50.0, sections=96)))
    assert "could come off the bed" in rod["message"], rod["message"]
    assert _says_how_to_hold_it(rod), rod["message"]

    wide = str(tmp_path / "wide.stl")
    _write_box_stl(wide, 40, 40, 200)
    reply = _tool_reply(wide)
    assert reply["message"].startswith("Ready to print."), reply["message"]
    assert "come off the bed" not in reply["message"], reply["message"]
    assert reply["report"]["adhesion_force"]["risk_level"] == "secure"

    tube = trimesh.creation.annulus(r_min=18.0, r_max=20.0, height=150.0, sections=96)
    tube = _tool_reply(_mesh_stl(tmp_path / "tube.stl", tube))
    assert tube["report"]["adhesion_force"]["risk_level"] == "secure", tube["report"]["adhesion_force"]
    assert "come off the bed" not in tube["message"], tube["message"]


def test_a_long_thin_wall_is_held_only_near_the_push(tmp_path, _force_free_tier):
    """A push at one point of a long thin wall is held by the stretch of its
    footing near that point, not by its whole length.  A wall 150 x 2 x 20 mm
    on edge used to read secure on the strength of all 150 mm and was told
    "Ready to print."; it is now likely to come off the bed and is told how
    to hold it down.  So is a 2 mm fin running 100 mm out from a block it is
    part of: its free end is held by its own footing, not the block's.  A
    flat strip lying on the bed, wider than it is tall, keeps the whole of
    its length.  The estimate with no contact to measure, read as a strip as
    long as the part, says the same of the wall.
    """
    import trimesh

    from kiln import printability as _p

    wall_path = _mesh_stl(tmp_path / "wall.stl", trimesh.creation.box(extents=(150.0, 2.0, 20.0)))
    wall = _tool_reply(wall_path)
    force = wall["report"]["adhesion_force"]
    assert force["risk_level"] == "likely_detach" and force["will_detach"] is True, force
    # The push test's own number, read where it is worked out: a reply about a
    # check Kiln Pro tuned leaves it out (kiln.printability._served).
    assert _p.analyze_printability(wall_path).adhesion_force.tipping_ratio < 1.0
    assert "likely to come off the bed" in wall["message"], wall["message"]
    assert _says_how_to_hold_it(wall), wall["message"]

    block = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    fin = trimesh.creation.box(extents=(100.0, 2.0, 20.0))
    fin.apply_translation((60.0, 0.0, 0.0))
    finned = _tool_reply(_mesh_stl(tmp_path / "fin.stl", block.union(fin)))
    assert finned["report"]["adhesion_force"]["risk_level"] == "likely_detach", finned["report"]["adhesion_force"]
    assert "come off the bed" in finned["message"], finned["message"]
    assert _says_how_to_hold_it(finned), finned["message"]

    strip = _tool_reply(_mesh_stl(tmp_path / "strip.stl", trimesh.creation.box(extents=(150.0, 4.0, 1.0))))
    assert strip["report"]["adhesion_force"]["risk_level"] == "secure", strip["report"]["adhesion_force"]
    assert "come off the bed" not in strip["message"], strip["message"]

    bbox = {"x_min": -75.0, "x_max": 75.0, "y_min": -1.0, "y_max": 1.0, "z_min": 0.0, "z_max": 20.0}
    unmeasured = _p._estimate_adhesion_force(300.0, bbox, material="pla")
    assert unmeasured.risk_level == "likely_detach" and unmeasured.will_detach, unmeasured


def test_tuning_never_loosens_the_push_test(tmp_path, monkeypatch):
    """Per-material physics that says the plastic grips the bed far better
    than the public floor, and a judgment overlay asking for a gentler
    push, still leave a 4 x 4 x 200 mm pillar expected to come off the bed:
    tuning may make the push test stricter, never looser, so a paid answer
    is never told less about it than a free one.
    """
    from kiln import printability as _p

    monkeypatch.setattr(
        _p, "_material_physics_from_overlay",
        lambda mat: {"adhesion_strength": 5.0, "shrinkage_strain": 0.0001, "stress_factor": 0.1},
    )
    overlay = {"adhesion_force": {**_p._ADHESION_FORCE_PUBLIC_DEFAULTS, "nozzle_push_force_n": 1e-6}}
    import kiln.design_intelligence as _di

    monkeypatch.setattr(
        _di, "load_pro_overlay_or_empty",
        lambda kind: overlay if kind == "printability_judgment" else {},
    )
    pillar = str(tmp_path / "pillar.stl")
    _write_box_stl(pillar, 4, 4, 200)
    reply = _tool_reply(pillar)
    force = reply["report"]["adhesion_force"]
    assert force["risk_level"] == "likely_detach", force
    assert "likely to come off the bed" in reply["message"], reply["message"]
    assert _says_how_to_hold_it(reply), (reply["message"], reply["report"]["adhesion"])


# ---------------------------------------------------------------------------
# The stretch check's budget: an uploaded outline can be cut very fine
# ---------------------------------------------------------------------------


def _fine_wall(segments: int, *, bowed: bool = False) -> list:
    """The bed contact of a wall 150 x 2 mm whose long sides are cut into
    *segments* pieces each, as one fan of triangles; *bowed* bends the sides
    out by 0.05 mm so that no two pieces lie on one straight line."""
    import numpy as np

    x = np.linspace(0.0, 150.0, segments)
    sag = 0.05 * (1.0 - ((x - 75.0) / 75.0) ** 2) if bowed else np.zeros(segments)
    ring = np.vstack([np.stack([x, -sag], 1), np.stack([x[::-1], 2.0 + sag[::-1]], 1)])
    corners = [(float(a), float(b), 0.0) for a, b in ring]
    return [(corners[0], corners[i + 1], corners[i]) for i in range(1, len(corners) - 1)]


def _comb(teeth: int, *, pitch: float = 0.004, length: float = 100.0) -> list:
    """The bed contact of a comb: *teeth* teeth half a *pitch* wide and
    *length* long off a 1 mm spine, all one patch."""
    half = pitch / 2.0
    edge = sorted({round(k * pitch, 9) for k in range(teeth)} | {round(k * pitch + half, 9) for k in range(teeth)})
    tris = []
    for k in range(teeth):
        y0, y1 = k * pitch, k * pitch + half
        a, b, c, d = (0.0, y0, 0.0), (length, y0, 0.0), (length, y1, 0.0), (0.0, y1, 0.0)
        tris += [(a, b, c), (a, c, d)]
    for ya, yb in zip(edge, edge[1:]):
        a, b, c, d = (-1.0, ya, 0.0), (0.0, ya, 0.0), (0.0, yb, 0.0), (-1.0, yb, 0.0)
        tris += [(a, b, c), (a, c, d)]
    return tris


def test_a_hostile_outline_is_read_in_bounded_memory_and_time():
    """A hosted upload can cut a wall's bed outline into hundreds of
    thousands of edges.  The footing's stretch is read in bounded memory and
    time whatever their number: a wall whose sides are cut 50,000 times reads
    exactly what the same wall uncut reads, and a comb of 20,000 teeth, too
    costly to read within the budget, is read as holding nothing, never as
    secure."""
    import time
    import tracemalloc

    from kiln import printability as _p

    uncut = _p._footing_section_modulus(_fine_wall(2), 0.0, height=100.0)
    for tris, expected in ((_fine_wall(50_000), uncut), (_comb(20_000), 0.0)):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            got = _p._footing_section_modulus(tris, 0.0, height=100.0)
            took = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert got == pytest.approx(expected, rel=1e-9, abs=1e-12), (len(tris), got, expected)
        assert peak < 150e6, (len(tris), peak)
        assert took < 30.0, (len(tris), took)


def test_a_part_too_costly_to_measure_is_never_read_more_secure(tmp_path, monkeypatch):
    """Past its work budget the stretch check gives up rather than read the
    footing at fewer places, so a budgeted answer can only fall below the
    exact one, never rise above it.  With the budget lowered until these
    parts trip it, every budget gives a footing no stronger than the exact
    one, and the tool's verdict is no more secure; with room enough the
    exact answer comes back."""
    import trimesh

    from kiln import printability as _p

    block = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    fin = trimesh.creation.box(extents=(100.0, 2.0, 20.0))
    fin.apply_translation((60.0, 0.0, 0.0))
    finned = block.union(fin)
    finned.apply_translation((0.0, 0.0, -finned.bounds[0][2]))
    parts = {
        "bowed wall": (_fine_wall(2_000, bowed=True), 40.0),
        "comb": (_comb(300, pitch=0.2), 40.0),
        "fin on a block": ([tuple(map(tuple, t)) for t in finned.triangles], 20.0),
    }
    budgets = (0, 2_000, 20_000, 200_000, 2_000_000)
    for name, (tris, height) in parts.items():
        exact = _p._footing_section_modulus(tris, 0.0, height=height)
        readings = []
        for budget in budgets:
            monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", budget)
            readings.append(_p._footing_section_modulus(tris, 0.0, height=height))
        monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", 10**12)
        assert _p._footing_section_modulus(tris, 0.0, height=height) == exact, name
        assert all(r <= exact for r in readings), (name, exact, readings)
        assert min(readings) < exact, (name, exact, readings)  # the budget really tripped

    severity = {"secure": 0, "marginal": 1, "likely_detach": 2}
    path = _mesh_stl(tmp_path / "finned.stl", finned)
    monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", 10**12)
    exact = analyze_printability(path, material="pla").adhesion_force
    monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", 0)
    tripped = analyze_printability(path, material="pla").adhesion_force
    assert tripped.tipping_ratio <= exact.tipping_ratio, (tripped, exact)
    assert severity[tripped.risk_level] >= severity[exact.risk_level], (tripped, exact)


# ---------------------------------------------------------------------------
# Separate footings: each stands alone until a layer joins it to another
# ---------------------------------------------------------------------------


def _box_at(lo, hi):
    """A box from corner *lo* to corner *hi*."""
    import trimesh

    box = trimesh.creation.box(extents=[b - a for a, b in zip(lo, hi, strict=True)])
    box.apply_translation([(a + b) / 2.0 for a, b in zip(lo, hi, strict=True)])
    return box


def test_separate_footings_are_judged_alone_until_a_layer_joins_them(tmp_path, monkeypatch, _force_free_tier):
    """A part standing on more than one patch of the bed prints each patch
    as a column of its own until a layer joins them.  A channel 150 x 40 x
    20 mm lying open side down stands on two 2 mm rails that print as two
    thin walls for 18 mm before its top joins them: it used to read secure
    on the strength of both rails together, with a 5 mm brim, and is now
    likely to come off the bed, with an 8 mm brim, as a lone 2 mm wall is.
    Two 2 mm fins standing apart in one file, which nothing joins, read the
    same.  A plate whose bottom a shallow slot splits in two is joined one
    millimetre up and stays secure, "Ready to print." and nothing more.  And
    past the budget of the reading that finds where columns join, every
    patch is judged alone at the part's full height: never more secure.
    """
    from kiln import printability as _p

    channel = _box_at((0, 0, 0), (150, 40, 20)).difference(_box_at((-1, 2, -1), (151, 38, 18)))
    reply = _tool_reply(_mesh_stl(tmp_path / "channel.stl", channel))
    force = reply["report"]["adhesion_force"]
    assert force["risk_level"] == "likely_detach" and force["will_detach"] is True, force
    assert reply["report"]["adhesion"]["brim_width_mm"] == 8, reply["report"]["adhesion"]
    assert "8 mm brim" in reply["message"], reply["message"]

    import trimesh

    fins = _mesh_stl(
        tmp_path / "fins.stl",
        trimesh.util.concatenate([_box_at((0, 0, 0), (100, 2, 20)), _box_at((0, 30, 0), (100, 32, 20))]),
    )
    reply = _tool_reply(fins)
    assert reply["report"]["adhesion_force"]["risk_level"] == "likely_detach", reply["report"]["adhesion_force"]
    assert "likely to come off the bed" in reply["message"] and _says_how_to_hold_it(reply), reply["message"]

    slotted = _mesh_stl(
        tmp_path / "slotted.stl",
        _box_at((0, 0, 0), (40, 40, 3)).difference(_box_at((19, -1, -1), (21, 41, 1))),
    )
    reply = _tool_reply(slotted)
    assert reply["report"]["adhesion_force"]["risk_level"] == "secure", reply["report"]["adhesion_force"]
    assert reply["message"].startswith("Ready to print.") and "come off the bed" not in reply["message"], reply["message"]
    assert reply["report"]["adhesion"]["brim_width_mm"] == 0, reply["report"]["adhesion"]

    severity = {"secure": 0, "marginal": 1, "likely_detach": 2}
    for path in (slotted, fins):
        exact = analyze_printability(path, material="pla").adhesion_force
        monkeypatch.setattr(_p, "_JOIN_WORK", 0)
        tripped = analyze_printability(path, material="pla").adhesion_force
        monkeypatch.undo()
        assert tripped.tipping_ratio <= exact.tipping_ratio, (path, tripped, exact)
        assert severity[tripped.risk_level] >= severity[exact.risk_level], (path, tripped, exact)


def test_a_small_post_is_told_what_its_footing_holds(tmp_path, _force_free_tier):
    """A 4 x 4 mm post on its own reads secure to 2.5 mm tall, could come
    off the bed from 3 mm, and is likely to from 6 mm; PrusaSlicer 2.9.4
    warns of low bed adhesion on the same post from 3 mm in PLA."""
    def band(height: float) -> tuple[str, str]:
        path = str(tmp_path / f"post_{height}.stl")
        _write_box_stl(path, 4, 4, height)
        reply = _tool_reply(path)
        return reply["report"]["adhesion_force"]["risk_level"], reply["message"]

    for height, expected in ((2.5, "secure"), (3.0, "marginal"), (5.0, "marginal"), (6.0, "likely_detach"), (10.0, "likely_detach")):
        level, message = band(height)
        assert level == expected, (height, level, message)
    assert "could come off the bed" in band(3.0)[1]
    assert "likely to come off the bed" in band(6.0)[1]


def _bed_of_pins(across: int, *, deck: bool) -> "np.ndarray":
    """*across* squared pins, 1 mm square and 10 mm tall on a 2 mm pitch,
    under a 3 mm deck when *deck*: the mesh as ``(T, 3, 3)`` triangles."""
    import numpy as np

    def box(lo, hi):
        (x0, y0, z0), (x1, y1, z1) = lo, hi
        v = np.array([
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ])
        faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                 (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
        return v[np.array(faces)]

    pin = box((0.0, 0.0, 0.0), (1.0, 1.0, 10.0))
    at = np.array([(2.0 * i, 2.0 * j, 0.0) for i in range(across) for j in range(across)])
    tris = (pin[None] + at[:, None, None, :]).reshape(-1, 3, 3)
    if deck:
        tris = np.concatenate([tris, box((-1.0, -1.0, 10.0), (2.0 * across, 2.0 * across, 13.0))])
    return tris


def test_a_part_on_many_footings_is_read_in_bounded_memory_and_time():
    """A hosted upload can stand on thousands of separate footings.  A bed
    of 19,600 pins 1 mm square under a deck is read in bounded memory and
    time and reads what one of its pins reads alone; a bed of 42,025 pins,
    too many to read within the budget, is read as holding nothing, never as
    secure."""
    import time
    import tracemalloc

    from kiln import printability as _p

    one_pin = _p._footing_section_modulus(_bed_of_pins(1, deck=False), 0.0, height=13.0)
    assert one_pin > 0.0
    for across, expected in ((140, one_pin), (205, 0.0)):
        tris = _bed_of_pins(across, deck=True)
        tracemalloc.start()
        started = time.perf_counter()
        try:
            got = _p._footing_section_modulus(tris, 0.0, height=13.0)
            took = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert got == pytest.approx(expected, rel=1e-9, abs=1e-12), (across, got, expected)
        assert peak < 400e6, (across, peak)
        assert took < 60.0, (across, took)


# ---------------------------------------------------------------------------
# Frames: a side held at both corners holds a push as a held wall does
# ---------------------------------------------------------------------------


def _frame(x: float, y: float, wall: float, height: float):
    """A frame *x* by *y* with walls *wall* thick and no floor."""
    return _box_at((0, 0, 0), (x, y, height)).difference(_box_at((wall, wall, -1), (x - wall, y - wall, height + 1)))


def _wall_along(points, wall: float, height: float):
    """A wall *wall* thick along the closed polyline *points* ``(n, 2)``."""
    import numpy as np
    import trimesh

    n = len(points)
    d = np.roll(points, -1, axis=0) - np.roll(points, 1, axis=0)
    nrm = np.c_[d[:, 1], -d[:, 0]] / np.linalg.norm(d, axis=1)[:, None]
    if np.mean(np.sum((points - points.mean(0)) * nrm, axis=1)) < 0:
        nrm = -nrm
    out, inn = points + nrm * wall / 2, points - nrm * wall / 2
    verts = np.vstack([np.c_[out, np.zeros(n)], np.c_[out, np.full(n, height)],
                       np.c_[inn, np.zeros(n)], np.c_[inn, np.full(n, height)]])
    faces = []
    for k in range(n):
        j = (k + 1) % n
        faces += [[k, j, n + j], [k, n + j, n + k], [2 * n + k, 3 * n + j, 2 * n + j], [2 * n + k, 3 * n + k, 3 * n + j],
                  [n + k, n + j, 3 * n + j], [n + k, 3 * n + j, 3 * n + k], [k, 2 * n + j, j], [k, 2 * n + k, 2 * n + j]]
    mesh = trimesh.Trimesh(verts, faces)
    mesh.fix_normals()
    return mesh


def _rounded_frame(x: float, y: float, wall: float, height: float, radius: float, per_arc: int = 12):
    """A frame *x* by *y* with walls *wall* thick, corners rounded to the
    outer *radius*, and no floor."""
    import numpy as np

    hx, hy, rm = x / 2 - wall / 2, y / 2 - wall / 2, radius - wall / 2
    pts = []
    for cx, cy, a0 in ((hx - rm, hy - rm, 0.0), (-(hx - rm), hy - rm, np.pi / 2),
                       (-(hx - rm), -(hy - rm), np.pi), (hx - rm, -(hy - rm), 3 * np.pi / 2)):
        for k in range(per_arc + 1):
            a = a0 + (np.pi / 2) * k / per_arc
            pts.append((cx + rm * np.cos(a), cy + rm * np.sin(a)))
    pts = np.array(pts)
    keep = np.r_[True, np.linalg.norm(np.diff(pts, axis=0), axis=1) > 1e-9]
    return _wall_along(pts[keep], wall, height)


def test_a_frames_side_is_held_as_a_wall_held_at_both_ends_is(tmp_path, _force_free_tier):
    """A frame or an open box with no floor stands on its outline, and each
    of its sides is a wall held at both ends by the sides at right angles
    to it.  A 120 x 90 mm frame with 2 mm walls 20 mm tall used to read
    secure on the strength of its whole outline and was told "Ready to
    print."; a side of it holds a push at one point of its top the way a
    long wall held at both ends does, so it is now likely to come off the
    bed and is told so, with an 8 mm brim; the same frame with rounded
    corners reads the same, and the 3 mm frame 8 mm tall could come off.
    A frame whose walls are thicker than it is tall, a box with a floor, an
    open tube, a ring and a lone wall read as before.
    """
    import trimesh

    thin = _tool_reply(_mesh_stl(tmp_path / "frame2.stl", _frame(120, 90, 2, 20)))
    force = thin["report"]["adhesion_force"]
    assert force["risk_level"] == "likely_detach" and force["will_detach"] is False, force
    assert thin["report"]["adhesion"]["brim_width_mm"] == 8, thin["report"]["adhesion"]
    assert "likely to come off the bed" in thin["message"] and _says_how_to_hold_it(thin), thin["message"]

    rounded = _tool_reply(_mesh_stl(tmp_path / "rounded.stl", _rounded_frame(120, 90, 2, 20, 10)))
    assert rounded["report"]["adhesion_force"]["risk_level"] == "likely_detach", rounded["report"]["adhesion_force"]
    assert "likely to come off the bed" in rounded["message"], rounded["message"]

    low = _tool_reply(_mesh_stl(tmp_path / "frame3.stl", _frame(120, 90, 3, 8)))
    assert low["report"]["adhesion_force"]["risk_level"] == "marginal", low["report"]["adhesion_force"]
    assert "could come off the bed" in low["message"] and _says_how_to_hold_it(low), low["message"]

    unchanged = {
        "thick": _frame(120, 90, 10, 8),
        "floored": _box_at((0, 0, 0), (120, 90, 20)).difference(_box_at((2, 2, 2), (118, 88, 21))),
        "tube": trimesh.creation.annulus(r_min=18.0, r_max=20.0, height=150.0, sections=96),
        "ring": trimesh.creation.annulus(r_min=18.0, r_max=20.0, height=20.0, sections=96),
    }
    for name, mesh in unchanged.items():
        reply = _tool_reply(_mesh_stl(tmp_path / f"{name}.stl", mesh))
        assert reply["report"]["adhesion_force"]["risk_level"] == "secure", (name, reply["report"]["adhesion_force"])
        assert reply["report"]["adhesion"]["brim_width_mm"] == 0, (name, reply["report"]["adhesion"])
        assert "come off the bed" not in reply["message"], (name, reply["message"])
    wall = analyze_printability(_mesh_stl(tmp_path / "wall.stl", trimesh.creation.box(extents=(150.0, 2.0, 20.0))), material="pla")
    assert 0.35 < wall.adhesion_force.tipping_ratio < 0.5, wall.adhesion_force  # its free end still governs


def _fine_frame(segments: int, *, bowed: bool = False) -> list:
    """The bed contact of a 120 x 90 frame with 2 mm walls, as four strips
    whose long edges are cut into *segments* pieces (a multiple of 180, so
    that the strips meet along shared edges); *bowed* bends each outer edge
    out by 0.05 mm so that no two of its pieces lie on one straight line."""
    import numpy as np

    assert segments % 180 == 0
    tris: list = []

    def strip(lo, hi, axis, outer_at):
        """The strip from corner *lo* to corner *hi*, cut along *axis*, its
        outer edge at *outer_at* on the other axis."""
        other = 1 - axis
        t = np.linspace(0.0, 1.0, segments + 1)
        along = lo[axis] + (hi[axis] - lo[axis]) * t
        sag = 0.05 * (1.0 - (2.0 * t - 1.0) ** 2) if bowed else np.zeros(segments + 1)
        sign = -1.0 if outer_at == 0.0 else 1.0
        outer = outer_at + sign * sag
        inner = hi[other] if outer_at == lo[other] else lo[other]
        for k in range(segments):
            quad = []
            for a, o in ((along[k], outer[k]), (along[k + 1], outer[k + 1]), (along[k + 1], inner), (along[k], inner)):
                pt = [0.0, 0.0, 0.0]
                pt[axis], pt[other] = float(a), float(o)
                quad.append(tuple(pt))
            tris.append((quad[0], quad[1], quad[2]))
            tris.append((quad[0], quad[2], quad[3]))

    strip((0.0, 0.0), (2.0, 90.0), 1, 0.0)       # left, the whole height
    strip((118.0, 0.0), (120.0, 90.0), 1, 120.0)  # right
    strip((2.0, 0.0), (118.0, 2.0), 0, 0.0)       # bottom, between them
    strip((2.0, 88.0), (118.0, 90.0), 0, 90.0)    # top
    return tris


def test_a_frame_cut_fine_is_read_in_bounded_memory_and_time(monkeypatch):
    """A hosted upload can cut a frame's outline into hundreds of thousands
    of edges.  A 120 x 90 frame whose sides are cut 18,000 times each reads
    exactly what the uncut frame reads; one whose cut sides bow, so that no
    two pieces lie on one line, reads no more secure than it, in bounded
    memory and time; and past the budget the footing reads as holding
    nothing, never more secure."""
    import time
    import tracemalloc

    from kiln import printability as _p

    uncut = _p._footing_section_modulus(_fine_frame(180), 0.0, height=20.0)
    whole = _p._footing_section_modulus(_fine_frame(180), 0.0)  # the outline read whole, without a height
    assert 0.0 < uncut < whole / 10.0, (uncut, whole)  # its sides decide, not its whole outline
    for tris, bound in ((_fine_frame(18_000), uncut), (_fine_frame(18_000, bowed=True), uncut)):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            got = _p._footing_section_modulus(tris, 0.0, height=20.0)
            took = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert 0.0 < got <= bound * (1.0 + 1e-9), (len(tris), got, bound)
        assert peak < 150e6, (len(tris), peak)
        assert took < 30.0, (len(tris), took)
    exact = uncut
    readings = []
    for budget in (0, 2_000, 20_000, 200_000):
        monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", budget)
        readings.append(_p._footing_section_modulus(_fine_frame(180), 0.0, height=20.0))
    monkeypatch.setattr(_p, "_HELD_STRETCH_WORK", 10**12)
    assert _p._footing_section_modulus(_fine_frame(180), 0.0, height=20.0) == exact
    assert all(r <= exact for r in readings), (exact, readings)
    assert min(readings) < exact, (exact, readings)


# ---------------------------------------------------------------------------
# Plastics Kiln does not call secure on a standard build surface
# ---------------------------------------------------------------------------


def test_a_plastic_that_holds_a_standard_bed_weakly_is_never_called_secure(tmp_path, _force_free_tier):
    """A part in PP, nylon, PC or ABS is never called secure: its hold on a
    standard build surface is not vouched for, whatever its footing holds.
    A 20 x 20 x 300 mm PP column, a 40 mm PC cube and a 30 mm ABS cube all
    read secure on the public floor and were told "Ready to print."; each
    is now told the plastic holds a standard build surface weakly and could
    come off the bed, with a 5 mm brim, and the reply names the plastic.
    A PLA cube of the same size stays secure, and a part the push test
    already expects to come off keeps that stronger verdict.
    """
    cases = {"pp": (20, 20, 300), "pc": (40, 40, 40), "abs": (30, 30, 30), "abs_esd": (30, 30, 30), "pa6": (30, 30, 30)}
    names = {"pp": "PP", "pc": "PC", "abs": "ABS", "abs_esd": "ABS", "pa6": "Nylon"}
    for material, (w, d, h) in cases.items():
        path = str(tmp_path / f"{material}.stl")
        _write_box_stl(path, w, d, h)
        reply = _tool_reply(path, material=material)
        force = reply["report"]["adhesion_force"]
        assert force["risk_level"] == "marginal", (material, force)
        assert force["secure_withheld_for"] == names[material], (material, force)
        assert reply["report"]["adhesion"]["brim_width_mm"] >= 5, (material, reply["report"]["adhesion"])
        assert f"{names[material]} holds a standard build surface weakly, so it could come off the bed" in reply["message"], reply["message"]
        assert _says_how_to_hold_it(reply), reply["message"]
        assert any("standard build surface" in r for r in force["recommendations"]), force["recommendations"]
        assert "Kiln Pro" not in reply["message"], reply["message"]

    pla = str(tmp_path / "pla.stl")
    _write_box_stl(pla, 30, 30, 30)
    reply = _tool_reply(pla, material="pla")
    assert reply["report"]["adhesion_force"]["risk_level"] == "secure", reply["report"]["adhesion_force"]
    assert reply["report"]["adhesion_force"]["secure_withheld_for"] == ""
    assert "come off the bed" not in reply["message"], reply["message"]

    tower = str(tmp_path / "pp_tower.stl")
    _write_box_stl(tower, 2, 2, 250)
    reply = _tool_reply(tower, material="pp")
    assert reply["report"]["adhesion_force"]["risk_level"] == "likely_detach", reply["report"]["adhesion_force"]
    assert reply["report"]["adhesion_force"]["secure_withheld_for"] == ""
    assert "likely to come off the bed" in reply["message"], reply["message"]
