"""Tests for kiln.printability -- printability analysis engine."""

from __future__ import annotations

import math
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


def _make_slope_wedge_triangles(
    overhang_deg: float,
    *,
    base_w: float = 30.0,
    base_d: float = 30.0,
    height: float = 20.0,
) -> list[tuple]:
    """A closed parallelogram prism with two outward-leaning side walls.

    Cross-section: trapezoid widening upward such that the side walls
    lean ``overhang_deg`` from vertical.  When ``overhang_deg`` is 0
    the walls are vertical (no overhangs); when ``overhang_deg`` is
    near 90 the walls approach horizontal ceilings.

    Returns 12 triangles forming a manifold solid suitable for
    ``_analyze_overhangs`` plus the winding normalizer (the bbox
    centre is offset from each face's centroid, so the mesh-centre
    heuristic produces stable orientation).  Used by the
    floating-point-precision regression tests around the 45°
    threshold AND the material-aware-threshold regression tests.
    """
    import math
    h_shift = height * math.tan(math.radians(overhang_deg))
    v = [
        (0.0, 0.0, 0.0),
        (base_w, 0.0, 0.0),
        (base_w, base_d, 0.0),
        (0.0, base_d, 0.0),
        (-h_shift, 0.0, height),
        (base_w + h_shift, 0.0, height),
        (base_w + h_shift, base_d, height),
        (-h_shift, base_d, height),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (3, 7, 6), (3, 6, 2),
        (1, 2, 6), (1, 6, 5),
        (0, 4, 7), (0, 7, 3),
    ]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple]) -> str:
    """Write a binary STL file and return its path."""
    path = os.path.join(tmpdir, "test_model.stl")
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


def _outward_cube_triangles(size: float = 10.0) -> list[tuple]:
    """12 triangles forming a cube [0,size]^3 with OUTWARD-facing
    normals — the convention real-world CAD STLs use.

    Distinct from ``_cube_triangles`` which uses the opposite winding
    (and was sufficient for the per-analysis tests in this file because
    ``analyze_printability`` normalizes winding before consuming
    triangles).  ``detect_holes`` re-parses the file independently
    from disk and does NOT normalize, so it sees the raw winding —
    and the inward-faced ``_cube_triangles`` looks geometrically
    indistinguishable from three cylindrical features.  Use this
    helper for any test that exercises hole detection on a "no
    holes" mesh.
    """
    s = size
    p = [
        (0.0, 0.0, 0.0),
        (s, 0.0, 0.0),
        (s, s, 0.0),
        (0.0, s, 0.0),
        (0.0, 0.0, s),
        (s, 0.0, s),
        (s, s, s),
        (0.0, s, s),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),       # bottom -Z
        (4, 5, 6), (4, 6, 7),       # top +Z
        (0, 1, 5), (0, 5, 4),       # front -Y
        (1, 2, 6), (1, 6, 5),       # right +X
        (2, 3, 7), (2, 7, 6),       # back +Y
        (3, 0, 4), (3, 4, 7),       # left -X
    ]
    return [(p[a], p[b], p[c]) for a, b, c in faces]


def _hole_side_wall_z(
    cx: float,
    cy: float,
    radius: float,
    z_bottom: float,
    z_top: float,
    segments: int = 24,
) -> list[tuple]:
    """Inward-facing cylindrical side walls — a Z-axis hole.

    Winding mirrors the helper in ``kiln/tests/test_detect_holes.py``
    so this file stays self-contained — face normals point INWARD
    toward the hole's axis, which is what detect_holes requires.
    """
    triangles: list[tuple] = []
    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        bl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_bottom)
        br = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_bottom)
        tl = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z_top)
        tr = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z_top)
        # Reversed winding -> normal points inward (toward axis at cx,cy).
        triangles.append((bl, tr, br))
        triangles.append((bl, tl, tr))
    return triangles


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

    def test_exact_45deg_slope_classified_as_overhang(self):
        """A wall leaning exactly 45° from vertical is the canonical
        rule-of-thumb edge case — and floating-point precision turned
        it into a silent miss.

        ``math.acos(0.7071067811865475)`` returns
        ``0.7853981633974484`` radians, which converts to
        ``45.00000000000001°``.  ``90 - that = 44.99999999999999°``.
        A strict ``overhang_angle < 45.0`` filter rejected the result
        by one ULP, so a true 45° slope was classified ``max=0.0,
        needs_supports=False``.

        The fix is an epsilon-tolerant comparison
        (``overhang_angle + 1e-9 < max_overhang_angle``).  This test
        pins the corrected behavior so the regression cannot return.

        Uses a full closed wedge geometry (12 triangles, parallelogram
        prism with two outward-leaning side walls) so the winding
        normalizer's mesh-center heuristic operates on real geometry,
        not on a 3-vertex degenerate where bbox-center and centroid
        coincide.
        """
        tris = _make_slope_wedge_triangles(overhang_deg=45.0)
        result = _analyze_overhangs(tris, max_overhang_angle=45.0, z_min=0.0)
        assert result.overhang_triangle_count > 0, (
            f"a true 45° slope must count as an overhang at the 45° "
            f"threshold (FP quirk regression); got count="
            f"{result.overhang_triangle_count}, max_overhang_angle="
            f"{result.max_overhang_angle}"
        )
        assert 44.99 <= result.max_overhang_angle <= 45.01

    def test_46deg_slope_still_classified_as_overhang(self):
        """Non-regression: a 46° slope (clearly above the 45° floor)
        must still register as an overhang after the epsilon fix.  Pins
        that the epsilon didn't accidentally widen the filter."""
        tris = _make_slope_wedge_triangles(overhang_deg=46.0)
        result = _analyze_overhangs(tris, max_overhang_angle=45.0, z_min=0.0)
        assert result.overhang_triangle_count > 0
        assert 45.99 <= result.max_overhang_angle <= 46.01

    def test_44deg_slope_not_classified_as_overhang(self):
        """Non-regression: a 44° slope (clearly below the 45° floor)
        must NOT register as an overhang after the epsilon fix.  Pins
        that the 1e-9 epsilon is narrow enough not to swallow legit
        sub-threshold geometry."""
        tris = _make_slope_wedge_triangles(overhang_deg=44.0)
        result = _analyze_overhangs(tris, max_overhang_angle=45.0, z_min=0.0)
        assert result.overhang_triangle_count == 0, (
            f"a 44° slope (below 45° threshold) must NOT count as "
            f"overhang; got count={result.overhang_triangle_count}, "
            f"max_overhang_angle={result.max_overhang_angle}"
        )


# ---------------------------------------------------------------------------
# TestOverhangMaterialAwareThreshold — soft tier seam.  Free tier
# consumes the universal 45° rule via _OVERHANGS_PUBLIC_DEFAULTS;
# Pro+ overlay supplies per-material limits (TPU 35, PLA 50, …) via
# the printability_judgment overlay's ``overhangs`` block.
# ---------------------------------------------------------------------------


class TestOverhangMaterialAwareThreshold:
    """The overlay-aware lookup mirrors the existing pattern used by
    ``_analyze_warping`` and ``_analyze_thermal_stress`` — caller can
    inject ``overlay`` + ``material`` and the function looks up the
    per-material threshold without knowing any specific values."""

    def test_free_tier_default_45deg_when_no_overlay(self):
        """Free tier (no overlay) keeps the universal 45° rule.  Pins
        the no-regression contract for installs without kiln-pro."""
        tris = _make_slope_wedge_triangles(overhang_deg=40.0)
        r = _analyze_overhangs(tris, z_min=0.0, material="TPU")
        assert r.overhang_triangle_count == 0
        assert not r.needs_supports

    def test_free_tier_default_45deg_when_overlay_lacks_overhangs_block(self):
        """An overlay dict without an ``overhangs`` block also falls
        through to 45°.  Pins the safe-default contract when an older
        Pro+ overlay schema is loaded."""
        tris = _make_slope_wedge_triangles(overhang_deg=40.0)
        overlay_without_overhangs = {"warping": {"risk_thresholds": {}}}
        r = _analyze_overhangs(
            tris, z_min=0.0, material="TPU",
            overlay=overlay_without_overhangs,
        )
        assert r.overhang_triangle_count == 0

    def test_pro_tier_tpu_at_36deg_flagged_via_overlay(self):
        """Pro+ overlay TPU=35° → 36° slope on TPU must register —
        the exact behavior the material-aware floor unlocks vs the
        universal 45° rule."""
        tris = _make_slope_wedge_triangles(overhang_deg=36.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 45.0,
                "material_limits_deg": {"TPU": 35, "PLA": 50},
            },
        }
        r = _analyze_overhangs(
            tris, z_min=0.0, material="TPU", overlay=overlay,
        )
        assert r.overhang_triangle_count > 0
        assert 35.99 <= r.max_overhang_angle <= 36.01

    def test_pro_tier_pla_at_48deg_not_flagged_via_overlay(self):
        """Pro+ overlay PLA=50° → 48° slope on PLA must NOT register —
        the forgiving side of per-material tuning that removes the
        universal 45° rule's PLA false positives."""
        tris = _make_slope_wedge_triangles(overhang_deg=48.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 45.0,
                "material_limits_deg": {"TPU": 35, "PLA": 50},
            },
        }
        r = _analyze_overhangs(
            tris, z_min=0.0, material="PLA", overlay=overlay,
        )
        assert r.overhang_triangle_count == 0

    def test_unknown_material_falls_back_to_overlay_default(self):
        """A material absent from ``material_limits_deg`` falls back
        to the overlay's ``default_limit_deg``.  Pins the fallback
        chain so forgetting an entry is safe."""
        tris = _make_slope_wedge_triangles(overhang_deg=44.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 40.0,
                "material_limits_deg": {"PLA": 50},
            },
        }
        r = _analyze_overhangs(
            tris, z_min=0.0, material="Unobtanium", overlay=overlay,
        )
        assert r.overhang_triangle_count > 0

    def test_material_lookup_is_case_insensitive(self):
        """Overlay keys mix UPPERCASE legacy ('PLA', 'CF-PETG') with
        lowercase catalog ('pla_plus', 'tpu_85a').  Callers pass
        material in either case — Kiln tools typically use lowercase
        from materials.json; tests often uppercase.  The lookup must
        be case-insensitive so ``material='tpu'`` matches the 'TPU'
        overlay key and doesn't silently fall through to the universal
        45 deg default (which would disable the per-material tier
        seam entirely for that material).

        Mirrors the ``_normalize_material_key`` helper in
        kiln_pro/printability_overlay/data_loader.py."""
        tris = _make_slope_wedge_triangles(overhang_deg=36.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 45.0,
                "material_limits_deg": {"TPU": 35},
            },
        }
        for mat in ("TPU", "tpu", "Tpu", "tPU"):
            r = _analyze_overhangs(
                tris, z_min=0.0, material=mat, overlay=overlay,
            )
            assert r.overhang_triangle_count > 0, (
                f"material='{mat}' must match overlay key 'TPU' "
                f"case-insensitively; got count="
                f"{r.overhang_triangle_count}"
            )

    def test_material_lookup_folds_dash_underscore_space(self):
        """The overlay carries entries in both ``UPPERCASE+dash`` (e.g.
        'CF-PETG') and ``lowercase+underscore`` (e.g. 'cf_petg')
        conventions.  The lookup must collapse all four delimiter
        styles so any caller convention matches any overlay entry.

        Same scope as kiln-pro's ``_normalize_material_key``."""
        tris = _make_slope_wedge_triangles(overhang_deg=42.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 45.0,
                "material_limits_deg": {"CF-PETG": 40},
            },
        }
        for mat in ("CF-PETG", "cf-petg", "cf_petg", "CF_PETG",
                    "Cf-Petg", "cf petg"):
            r = _analyze_overhangs(
                tris, z_min=0.0, material=mat, overlay=overlay,
            )
            assert r.overhang_triangle_count > 0, (
                f"material='{mat}' must match overlay key 'CF-PETG' "
                f"across delimiter conventions; got count="
                f"{r.overhang_triangle_count}"
            )

    def test_explicit_max_overhang_angle_overrides_overlay(self):
        """Explicit ``max_overhang_angle`` bypasses the overlay
        lookup.  Pins the priority order: caller > overlay > public."""
        tris = _make_slope_wedge_triangles(overhang_deg=44.0)
        overlay = {
            "overhangs": {
                "default_limit_deg": 35.0,
                "material_limits_deg": {"TPU": 30},
            },
        }
        r = _analyze_overhangs(
            tris, z_min=0.0, material="TPU", overlay=overlay,
            max_overhang_angle=50.0,
        )
        assert r.overhang_triangle_count == 0


class TestSupportsAndOverhangsAgreeOnThreshold:
    """analyze_printability MUST pass the same per-material overhang
    threshold to both _analyze_overhangs (the verdict) and
    _analyze_supports (the volume estimate).  Without coordination,
    the report contradicts itself for warp-prone materials:
    ``overhangs.needs_supports=True`` but
    ``supports.estimated_support_volume_mm3=0`` because the supports
    estimator's old hardcoded 45° default is above the per-material
    threshold for TPU (35°) / PP (40°).
    """

    def test_tpu_sub_45_overhang_produces_consistent_report(self, tmp_path):
        """A 40° slope on TPU.  Per-material lookup says 35°, so the
        overhang is real.  Without the shared-threshold fix,
        supports.estimated_support_volume_mm3 stays at 0 because
        _analyze_supports defaults to 45°.  With the fix, supports
        reports a non-zero volume — the user gets a consistent
        verdict + estimate pair."""
        import math
        # 40° outward-leaning wedge.
        h_shift = 20.0 * math.tan(math.radians(40.0))
        v = [
            (0.0, 0.0, 0.0), (30.0, 0.0, 0.0),
            (30.0, 30.0, 0.0), (0.0, 30.0, 0.0),
            (-h_shift, 0.0, 20.0), (30.0 + h_shift, 0.0, 20.0),
            (30.0 + h_shift, 30.0, 20.0), (-h_shift, 30.0, 20.0),
        ]
        faces = [
            (0, 2, 1), (0, 3, 2),
            (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4),
            (3, 7, 6), (3, 6, 2),
            (1, 2, 6), (1, 6, 5),
            (0, 4, 7), (0, 7, 3),
        ]
        tris = [(v[a], v[b], v[c]) for a, b, c in faces]
        stl = tmp_path / "wedge_40.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="TPU")
        if not report.overhangs.needs_supports:
            # The overlay isn't loaded in this environment (kiln-pro
            # not installed, or its overhangs block is absent).  Skip
            # the consistency check — there's nothing to be consistent
            # about when both analyses run at the universal 45° floor.
            return
        # When overhang says needs_supports, supports estimate must
        # also reflect a non-zero volume — same threshold drove both.
        assert report.supports.estimated_support_volume_mm3 > 0.0, (
            f"shared-threshold contract broken: overhangs.needs_supports="
            f"{report.overhangs.needs_supports} (max_ovh="
            f"{report.overhangs.max_overhang_angle}) but "
            f"supports.estimated_support_volume_mm3="
            f"{report.supports.estimated_support_volume_mm3} (should be > 0 "
            f"since both analyses now use the per-material threshold)"
        )


class TestResolveOverhangThreshold:
    """Unit-level coverage of the lookup helper that backs the
    shared-threshold contract in analyze_printability."""

    def test_explicit_caller_wins(self):
        from kiln.printability import _resolve_overhang_threshold
        overlay = {"overhangs": {"material_limits_deg": {"TPU": 35}}}
        assert _resolve_overhang_threshold(50.0, "TPU", overlay) == 50.0

    def test_per_material_lookup_used_when_no_explicit(self):
        from kiln.printability import _resolve_overhang_threshold
        overlay = {"overhangs": {"material_limits_deg": {"TPU": 35},
                                  "default_limit_deg": 45.0}}
        assert _resolve_overhang_threshold(None, "TPU", overlay) == 35.0

    def test_default_used_for_unknown_material(self):
        from kiln.printability import _resolve_overhang_threshold
        overlay = {"overhangs": {"material_limits_deg": {"PLA": 50},
                                  "default_limit_deg": 42.0}}
        assert _resolve_overhang_threshold(None, "Unobtanium", overlay) == 42.0

    def test_universal_45_when_no_overlay(self):
        from kiln.printability import _resolve_overhang_threshold
        assert _resolve_overhang_threshold(None, "TPU", None) == 45.0
        assert _resolve_overhang_threshold(None, "TPU", {}) == 45.0

    def test_case_insensitive_match(self):
        from kiln.printability import _resolve_overhang_threshold
        overlay = {"overhangs": {"material_limits_deg": {"TPU": 35}}}
        assert _resolve_overhang_threshold(None, "tpu", overlay) == 35.0
        assert _resolve_overhang_threshold(None, "Tpu", overlay) == 35.0


# ---------------------------------------------------------------------------
# TestThinWallAnalysis
# ---------------------------------------------------------------------------


class TestThinWallAnalysis:
    def test_cube_no_thin_walls(self):
        # Use the outward-faced cube so the ray-cast measurement's
        # inward rays actually probe the cube's interior — the default
        # ``_cube_triangles`` fixture has inward normals (a historical
        # quirk pre-dating the ray-cast measurement).
        tris = _outward_cube_triangles(10.0)
        verts = list({v for tri in tris for v in tri})
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        # A solid 10 mm cube has no thin walls (no wall thinner than nozzle).
        assert result.thin_wall_count == 0
        # ``min_wall_thickness_mm`` carries the smallest measured wall
        # regardless of the nozzle threshold so the kiln-pro overlay can
        # compare against per-material structural floors.  The 0.0 sentinel
        # is reserved for measurement failure on degenerate meshes; a
        # successful measurement on a solid 10 mm cube reads a meaningful
        # number (the inward ray hits the opposite face, ~10 mm).
        assert result.min_wall_thickness_mm > 0.0

    def test_isolated_thin_triangle_is_not_a_wall(self):
        # A single isolated triangle with a short edge does NOT constitute
        # a thin wall — there is no opposing surface for the ray-cast
        # measurement to hit.  The prior edge-length proxy flagged this
        # as a thin wall because it counted short edges; the new
        # measurement correctly returns no signal because no wall exists.
        tris = [((0, 0, 0), (0.1, 0, 0), (0, 10, 0))]
        verts = [(0, 0, 0), (0.1, 0, 0), (0, 10, 0)]
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count == 0
        assert result.min_wall_thickness_mm == 0.0

    def test_hollow_box_thin_walls_detected(self):
        # A hollow box with 0.3 mm walls (below the 0.4 mm nozzle floor)
        # MUST be flagged by the ray-cast measurement.  The prior proxy
        # missed walls in the [0.3 mm, 2.0 mm] band on coarse meshes; the
        # new measurement reads the actual wall thickness regardless of
        # tessellation density.
        outer = 20.0
        wall = 0.3
        inner = outer - 2 * wall
        o = outer / 2.0
        i = inner / 2.0
        ov = [
            (-o, -o, -o), (o, -o, -o), (o, o, -o), (-o, o, -o),
            (-o, -o, o), (o, -o, o), (o, o, o), (-o, o, o),
        ]
        iv = [
            (-i, -i, -i), (i, -i, -i), (i, i, -i), (-i, i, -i),
            (-i, -i, i), (i, -i, i), (i, i, i), (-i, i, i),
        ]
        # Outer faces (CCW outward), inner faces (CCW inward).
        outer_idx = [
            (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
            (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
        ]
        inner_idx = [
            (0, 2, 3), (0, 1, 2), (4, 6, 5), (4, 7, 6),
            (0, 5, 1), (0, 4, 5), (2, 7, 3), (2, 6, 7),
            (1, 6, 2), (1, 5, 6), (0, 3, 7), (0, 7, 4),
        ]
        tris = [tuple(ov[i] for i in face) for face in outer_idx]
        tris += [tuple(iv[i] for i in face) for face in inner_idx]
        verts = ov + iv
        result = _analyze_thin_walls(tris, verts, nozzle_diameter=0.4)
        assert result.thin_wall_count > 0
        # Measured thickness should match the actual wall thickness
        # within ray-cast precision (the offset and self-hit epsilons
        # bound the reading slightly below the geometric value).
        assert abs(result.min_wall_thickness_mm - wall) < 0.05

    def test_tessellation_invariance(self):
        # The same physical 2 mm wall must measure ~2 mm regardless of
        # how the mesh is tessellated.  The prior edge-length proxy
        # produced different ``min_wall_thickness_mm`` values for the
        # same physical wall at different subdivision densities — a 2 mm
        # cube split into 16×16 quads reported 0.125 mm.  The ray-cast
        # measurement reads geometry, not edge length.
        def hollow_box_tris(subdiv_iters):
            outer, wall = 20.0, 2.0
            inner = outer - 2 * wall
            o = outer / 2.0
            i = inner / 2.0
            ov = [
                (-o, -o, -o), (o, -o, -o), (o, o, -o), (-o, o, -o),
                (-o, -o, o), (o, -o, o), (o, o, o), (-o, o, o),
            ]
            iv = [
                (-i, -i, -i), (i, -i, -i), (i, i, -i), (-i, i, -i),
                (-i, -i, i), (i, -i, i), (i, i, i), (-i, i, i),
            ]
            outer_idx = [
                (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
                (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
                (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
            ]
            inner_idx = [
                (0, 2, 3), (0, 1, 2), (4, 6, 5), (4, 7, 6),
                (0, 5, 1), (0, 4, 5), (2, 7, 3), (2, 6, 7),
                (1, 6, 2), (1, 5, 6), (0, 3, 7), (0, 7, 4),
            ]
            tris = [tuple(ov[i] for i in face) for face in outer_idx]
            tris += [tuple(iv[i] for i in face) for face in inner_idx]
            for _ in range(subdiv_iters):
                new_tris = []
                for a, b, c in tris:
                    ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
                    bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2, (b[2] + c[2]) / 2)
                    ca = ((c[0] + a[0]) / 2, (c[1] + a[1]) / 2, (c[2] + a[2]) / 2)
                    new_tris.append((a, ab, ca))
                    new_tris.append((ab, b, bc))
                    new_tris.append((ca, bc, c))
                    new_tris.append((ab, bc, ca))
                tris = new_tris
            return tris
        # Sample-rays alone shouldn't push the measurement off — the
        # measurement is the GEOMETRIC distance to the next surface, not
        # an estimate.
        for subdiv in [0, 1, 2, 3]:
            tris = hollow_box_tris(subdiv)
            result = _analyze_thin_walls(tris, [], nozzle_diameter=0.4)
            # 2 mm wall is well above the 0.4 mm nozzle threshold, so
            # the measurement reports no thin walls at every subdivision.
            assert result.thin_wall_count == 0, (
                f"subdiv={subdiv}: 2 mm wall should not be flagged "
                f"(got count={result.thin_wall_count} at this density)"
            )
            # And the measured min wall thickness lands at ~2 mm — the
            # heart of tessellation-invariance.  Across 24 / 96 / 384 /
            # 1536 triangles the answer must stay within ±5 % of the
            # true wall, not drift with mesh density.
            assert abs(result.min_wall_thickness_mm - 2.0) < 0.1, (
                f"subdiv={subdiv}: tessellation-invariant measurement "
                f"should read ~2.0 mm, got {result.min_wall_thickness_mm}"
            )

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
# TestBridgeAwareOverhangVerdict — analyze_printability downgrades
# needs_supports=True → False when ALL THREE bridge-substitution
# conditions align (bbox heuristic + bridge-length + horizontal
# overhang).  Pins the 2026-05-17 PrusaSlicer cross-validation
# finding that 5/64 audit cases (square_bridge / U_upside_down /
# tabletop) get false-positive support flags otherwise.
# ---------------------------------------------------------------------------


def _make_short_bridge_triangles(span: float = 8.0, depth: float = 12.0) -> list[tuple]:
    """π-shape (two pillars + horizontal top); top underside is a
    horizontal overhang.  ``span`` ≤ 10mm keeps the bridge below the
    universal slicing limit; bbox short axis stays ≤ 30mm to match
    main's _likely_bridge_substituted heuristic.
    """
    pillar_w = 3.0
    pillar_h = 15.0
    top_thick = 3.0
    z_top = pillar_h + top_thick
    py2 = depth / 2
    pillar_xs = (-(span / 2 + pillar_w), span / 2)
    cs: list[tuple[float, float]] = [
        (pillar_xs[0], 0.0),
        (pillar_xs[0] + pillar_w, 0.0),
        (pillar_xs[0] + pillar_w, pillar_h),
        (pillar_xs[1], pillar_h),
        (pillar_xs[1], 0.0),
        (pillar_xs[1] + pillar_w, 0.0),
        (pillar_xs[1] + pillar_w, z_top),
        (pillar_xs[0], z_top),
    ]
    n = len(cs)
    v: list[tuple[float, float, float]] = []
    for (x, z) in cs:
        v.append((x, -py2, z))
    for (x, z) in cs:
        v.append((x, py2, z))
    faces: list[tuple[int, int, int]] = []
    for i in range(n):
        j = (i + 1) % n
        a, b, c, d = i, j, n + j, n + i
        faces.append((a, b, c))
        faces.append((a, c, d))
    for i in range(2, n):
        faces.append((0, i - 1, i))
        faces.append((n + 0, n + i, n + i - 1))
    return [(v[a], v[b], v[c]) for a, b, c in faces]


class TestBridgeAwareOverhangVerdict:
    def test_short_bridge_downgrades_verdict(self, tmp_path):
        """8mm horizontal bridge with no other overhangs.  bbox short
        axis is 12mm (≤30mm gate passes); overhang regions cluster
        within the bbox (clustering gate passes); overhang is 90°
        horizontal (horizontal gate passes).  Two-gate correlator
        downgrades needs_supports to False."""
        tris = _make_short_bridge_triangles(span=8.0)
        stl = tmp_path / "short_bridge.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        assert report.overhangs.max_overhang_angle >= 89.0
        assert report.supports.likely_substituted_by_bridge
        assert not report.overhangs.needs_supports, (
            f"short bridge must downgrade verdict to no-supports-needed; "
            f"got max_ovh={report.overhangs.max_overhang_angle}, "
            f"likely_sub={report.supports.likely_substituted_by_bridge}, "
            f"needs={report.overhangs.needs_supports}"
        )

    def test_wide_bbox_disables_bridge_substitution(self, tmp_path):
        """A π-shape stretched to 35mm wide in BOTH bbox axes —
        bbox short axis (35mm) exceeds main's 30mm gate, so
        _likely_bridge_substituted returns False.  Correlator must
        NOT downgrade because the bbox-substitution heuristic
        disagrees."""
        # span 35 means each pillar is at ±17.5; bbox spans 41.5 in X.
        # depth 35 makes bbox short axis 35 → above the 30mm gate.
        tris = _make_short_bridge_triangles(span=35.0, depth=35.0)
        stl = tmp_path / "wide_bridge.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        assert report.overhangs.max_overhang_angle >= 89.0
        assert not report.supports.likely_substituted_by_bridge, (
            f"35mm bbox short axis must trip main's >30mm gate; "
            f"got likely_sub={report.supports.likely_substituted_by_bridge}"
        )
        assert report.overhangs.needs_supports

    def test_steep_non_horizontal_overhang_keeps_supports(self, tmp_path):
        """60° outward-leaning wedge.  Bridging is fundamentally only
        applicable to near-horizontal overhangs; a steep slope is NOT
        bridgeable regardless of length.  Correlator must NOT downgrade."""
        import math
        height = 10.0
        base_w = 8.0
        base_d = 10.0
        h_shift = height * math.tan(math.radians(60.0))
        v = [
            (0.0, 0.0, 0.0), (base_w, 0.0, 0.0),
            (base_w, base_d, 0.0), (0.0, base_d, 0.0),
            (-h_shift, 0.0, height), (base_w + h_shift, 0.0, height),
            (base_w + h_shift, base_d, height), (-h_shift, base_d, height),
        ]
        faces = [
            (0, 2, 1), (0, 3, 2),
            (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4),
            (3, 7, 6), (3, 6, 2),
            (1, 2, 6), (1, 6, 5),
            (0, 4, 7), (0, 7, 3),
        ]
        tris = [(v[a], v[b], v[c]) for a, b, c in faces]
        stl = tmp_path / "wedge_60.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        assert 55.0 <= report.overhangs.max_overhang_angle <= 65.0
        assert report.overhangs.needs_supports

    def test_no_overhang_stays_no_supports(self, tmp_path):
        """No-overhang baseline.  Correlator must not flip a verdict
        that was already False."""
        tris = _outward_cube_triangles(20.0)
        stl = tmp_path / "cube.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        assert not report.overhangs.needs_supports

    def test_downgrade_surfaces_slicer_bridge_recommendation(self, tmp_path):
        """When the correlator downgrades needs_supports=True → False,
        the report must explain WHY via a top-of-list recommendation
        so the user isn't left wondering whether their horizontal
        overhang was missed.  Silent downgrade is worse UX than the
        pre-fix 'needs supports + likely_substituted_by_bridge' pair —
        at least the old version told the user something."""
        tris = _make_short_bridge_triangles(span=8.0)
        stl = tmp_path / "short_bridge.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        # Downgrade fired.
        assert not report.overhangs.needs_supports
        # And a recommendation explains the bridging delegation.
        bridge_recs = [
            r for r in report.recommendations
            if "bridge" in r.lower() and "horizontal overhang" in r.lower()
        ]
        assert bridge_recs, (
            f"bridge-substitution downgrade must surface a "
            f"slicer-will-bridge recommendation; got: "
            f"{report.recommendations}"
        )
        # The recommendation should sit FIRST so the user sees it
        # ahead of generic guidance.
        assert "bridge" in report.recommendations[0].lower()

    def test_no_downgrade_means_no_bridge_recommendation(self, tmp_path):
        """A 60° wedge (no downgrade should fire).  The bridge
        recommendation must NOT appear — adding it on every steep
        overhang would be noise."""
        import math
        a = math.radians(60.0)
        h_shift = 10.0 * math.tan(a)
        v = [
            (0.0, 0.0, 0.0), (8.0, 0.0, 0.0),
            (8.0, 10.0, 0.0), (0.0, 10.0, 0.0),
            (-h_shift, 0.0, 10.0), (8.0 + h_shift, 0.0, 10.0),
            (8.0 + h_shift, 10.0, 10.0), (-h_shift, 10.0, 10.0),
        ]
        faces = [
            (0, 2, 1), (0, 3, 2),
            (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4),
            (3, 7, 6), (3, 6, 2),
            (1, 2, 6), (1, 6, 5),
            (0, 4, 7), (0, 7, 3),
        ]
        tris = [(v[a], v[b], v[c]) for a, b, c in faces]
        stl = tmp_path / "wedge_60.stl"
        with open(stl, "wb") as f:
            f.write(_make_binary_stl(tris))
        report = analyze_printability(str(stl), material="PLA")
        bridge_recs = [
            r for r in report.recommendations
            if "slicer will" in r.lower() and "bridge" in r.lower()
        ]
        assert not bridge_recs, (
            f"non-downgrade case must NOT have a bridge recommendation; "
            f"got: {bridge_recs}"
        )


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

    def test_clean_cube_carries_measured_wall(self):
        """A clean 20 mm cube has no thin walls (no wall thinner than
        nozzle) but its ``min_wall_thickness_mm`` carries the measured
        thickness — the inward ray from each face hits the opposite
        face at roughly the cube extent.  The kiln-pro overlay reads
        this value to compare against per-material structural floors
        (e.g. flag a 1 mm PLA wall against the 1.2 mm structural floor
        even when it's above the 0.4 mm nozzle); the 0.0 sentinel is
        reserved for measurement failure on degenerate meshes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _outward_cube_triangles(20.0))
            for nozzle in (0.2, 0.4, 0.6, 0.8):
                report = analyze_printability(path, nozzle_diameter=nozzle)
                assert report.thin_walls.thin_wall_count == 0
                # Measurement should report the actual wall thickness
                # (the cube's diameter when probed from any face).
                assert report.thin_walls.min_wall_thickness_mm > 0.0, (
                    f"nozzle={nozzle}: measurement should report cube "
                    f"extent, not the 0.0 sentinel"
                )

    def test_triangle_count_populated_on_report(self):
        """``PrintabilityReport.triangle_count`` exposes the mesh's
        triangle count so coverage-aware consumers (kiln-pro's
        ``analysis_notes`` field) can branch on mesh density.  Field
        defaults to 0 only when constructed directly without going
        through ``analyze_printability``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(20.0))
            report = analyze_printability(path)
            # A simple cube STL has 12 triangles (2 per face × 6 faces).
            assert report.triangle_count == 12
            # And to_dict() round-trips the field for kiln-pro overlay
            # consumption.
            assert report.to_dict()["triangle_count"] == 12

    def test_print_time_modifier_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0))
            report = analyze_printability(path)
            assert report.estimated_print_time_modifier >= 1.0

    def test_hole_free_mesh_reports_empty_holes_list(self):
        """A solid cube has no cylindrical features — the holes list is
        present but empty, never None.

        Uses ``_outward_cube_triangles`` (not ``_cube_triangles``)
        because detect_holes re-parses the STL from disk and reads its
        raw winding; the inward-faced default cube fixture looks like
        three cylindrical features under that geometry-only view.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _outward_cube_triangles(10.0))
            report = analyze_printability(path)
            assert report.holes == []
            # The field is also reachable via to_dict() so the kiln-pro
            # overlay's report.get("holes") read finds the same value.
            assert report.to_dict()["holes"] == []

    def test_mesh_with_z_axis_hole_populates_holes_list(self):
        """A Z-axis cylindrical hole shows up in report.holes with the
        documented shape (position, diameter_mm, depth_mm, axis,
        triangle_count)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tris = _hole_side_wall_z(
                cx=10.0, cy=10.0, radius=2.5,
                z_bottom=0.0, z_top=10.0, segments=24,
            )
            path = _write_stl(tmpdir, tris)
            report = analyze_printability(path)
            assert len(report.holes) == 1
            h = report.holes[0]
            assert h["axis"] == "z"
            assert h["diameter_mm"] == pytest.approx(5.0, abs=0.3)
            assert h["depth_mm"] == pytest.approx(10.0, abs=0.05)
            assert set(h["position"].keys()) == {"x_mm", "y_mm", "z_mm"}

    def test_include_hole_detection_false_skips_detector(self):
        """Opt-out: when ``include_hole_detection=False``, the report
        carries an empty holes list even when the mesh has a hole.
        Perf-critical callers can use this to avoid the second mesh
        parse."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tris = _hole_side_wall_z(
                cx=10.0, cy=10.0, radius=2.5,
                z_bottom=0.0, z_top=10.0, segments=24,
            )
            path = _write_stl(tmpdir, tris)
            report = analyze_printability(path, include_hole_detection=False)
            assert report.holes == []

    def test_sub_floor_round_bore_recommends_enlarge_or_drill(self):
        """A sub-floor 24-segment hole produces the round-bore
        recommendation that mentions drilling after printing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tris = _hole_side_wall_z(
                cx=10.0, cy=10.0, radius=0.3,
                z_bottom=0.0, z_top=10.0, segments=24,
            )
            path = _write_stl(tmpdir, tris)
            report = analyze_printability(path)
            sub_floor_recs = [
                r for r in report.recommendations
                if "0.8 mm hole-detection floor" in r
            ]
            assert len(sub_floor_recs) == 1, (
                f"expected exactly one sub_floor recommendation; "
                f"got {sub_floor_recs!r}"
            )
            assert "drill after printing" in sub_floor_recs[0], (
                f"round-bore recommendation must offer drilling "
                f"workaround; got {sub_floor_recs[0]!r}"
            )

    def test_sub_floor_polygonal_pocket_recommends_no_drill(self):
        """A sub-floor 6-segment hex pocket produces a recommendation
        that explicitly says drilling won't help — wording must
        differ from the round-bore case."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tris = _hole_side_wall_z(
                cx=10.0, cy=10.0, radius=0.3,
                z_bottom=0.0, z_top=10.0, segments=6,
            )
            path = _write_stl(tmpdir, tris)
            report = analyze_printability(path)
            poly_recs = [
                r for r in report.recommendations
                if "polygonal pocket" in r
            ]
            assert len(poly_recs) == 1, (
                f"expected exactly one polygonal sub_floor "
                f"recommendation; got {poly_recs!r}"
            )
            assert "Drilling won't help" in poly_recs[0], (
                f"polygonal pocket recommendation must NOT offer "
                f"drilling; got {poly_recs[0]!r}"
            )
            # And the round-bore recommendation must NOT also fire —
            # otherwise the user sees two contradictory notices.
            round_recs = [
                r for r in report.recommendations
                if "drill after printing" in r
            ]
            assert round_recs == [], (
                f"polygonal-only sub_floor must not also fire the "
                f"round-bore recommendation; got {round_recs!r}"
            )


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
# TestMaterialPhysicsBridge — public Kiln no longer ships per-material
# stress / adhesion / shrinkage tables.  Each call site now goes through
# a bridge helper that consults the kiln-pro overlay when present and
# falls back to a single conservative default otherwise.  This test class
# pins the free-vs-Pro contract.
# ---------------------------------------------------------------------------


from kiln.printability import (  # noqa: E402
    _DEFAULT_ADHESION_STRENGTH,
    _DEFAULT_SHRINKAGE_STRAIN,
    _DEFAULT_STRESS_FACTOR,
    _material_adhesion_strength,
    _material_physics_from_overlay,
    _material_shrinkage_strain,
    _material_stress_factor,
)


def _force_no_kiln_pro(monkeypatch):
    """Make ``from kiln_pro.bridge import pro_features`` raise ImportError.

    Simulates a free / public Kiln install where kiln-pro isn't on the
    Python path.  Used to assert the public-default fallback.
    """
    import builtins as _builtins

    real_import = _builtins.__import__

    def _stub(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "kiln_pro.bridge" or name.startswith("kiln_pro"):
            raise ImportError("simulated: kiln-pro not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(_builtins, "__import__", _stub)


def _stub_overlay(monkeypatch, materials: dict[str, dict]) -> None:
    """Plug a fake kiln-pro overlay into the bridge so the helpers see
    a Pro-tier environment with the supplied per-material physics dict.
    """
    import types

    fake_overlay = types.SimpleNamespace(
        lookup_material=lambda mat: materials.get(mat.lower()) if mat else None,
    )
    fake_features = types.SimpleNamespace(
        is_available=lambda feat: feat == "printability_overlay",
        printability_overlay=fake_overlay,
    )
    fake_bridge = types.ModuleType("kiln_pro.bridge")
    fake_bridge.pro_features = fake_features
    fake_pkg = types.ModuleType("kiln_pro")
    fake_pkg.bridge = fake_bridge
    monkeypatch.setitem(__import__("sys").modules, "kiln_pro", fake_pkg)
    monkeypatch.setitem(__import__("sys").modules, "kiln_pro.bridge", fake_bridge)


class TestMaterialPhysicsBridge:
    """Free vs Pro contract for the three per-material physics helpers."""

    # -- Free tier (no kiln-pro on the path) --------------------------

    @pytest.mark.parametrize(
        "material",
        ["pla", "PLA", "abs", "ABS", "nylon", "pc", "tpu", "petg",
         "unknown", "", None],
    )
    def test_free_tier_stress_factor_is_always_default(self, monkeypatch, material):
        """Every material returns the conservative default — uniform,
        material-agnostic.  Free-tier reports do not differentiate
        between PLA and ABS at the thermal-stress level; per-material
        differentiation arrives via the kiln-pro overlay bridge.
        """
        _force_no_kiln_pro(monkeypatch)
        assert _material_stress_factor(material) == _DEFAULT_STRESS_FACTOR

    @pytest.mark.parametrize(
        "material", ["pla", "abs", "nylon", "tpu", "unknown", None],
    )
    def test_free_tier_adhesion_strength_is_always_default(
        self, monkeypatch, material,
    ):
        _force_no_kiln_pro(monkeypatch)
        assert _material_adhesion_strength(material) == _DEFAULT_ADHESION_STRENGTH

    @pytest.mark.parametrize(
        "material", ["pla", "abs", "nylon", "tpu", "unknown", None],
    )
    def test_free_tier_shrinkage_strain_is_always_default(
        self, monkeypatch, material,
    ):
        _force_no_kiln_pro(monkeypatch)
        assert _material_shrinkage_strain(material) == _DEFAULT_SHRINKAGE_STRAIN

    def test_free_tier_overlay_helper_returns_empty_dict(self, monkeypatch):
        """The shared bridge helper returns ``{}`` so callers fall
        through to their default branch."""
        _force_no_kiln_pro(monkeypatch)
        assert _material_physics_from_overlay("pla") == {}

    # -- Pro tier (kiln-pro overlay mocked available) ------------------

    def test_pro_tier_pla_stress_factor_is_06(self, monkeypatch):
        """PLA at Pro tier — overlay returns the curated 0.6."""
        _stub_overlay(monkeypatch, {"pla": {"stress_factor": 0.6}})
        assert _material_stress_factor("PLA") == pytest.approx(0.6)

    def test_pro_tier_abs_stress_factor_is_15(self, monkeypatch):
        """ABS at Pro tier — overlay returns the curated 1.5."""
        _stub_overlay(monkeypatch, {"abs": {"stress_factor": 1.5}})
        assert _material_stress_factor("ABS") == pytest.approx(1.5)

    def test_pro_tier_nylon_stress_factor_is_16(self, monkeypatch):
        """Nylon at Pro tier — overlay returns the curated 1.6."""
        _stub_overlay(monkeypatch, {"nylon": {"stress_factor": 1.6}})
        assert _material_stress_factor("Nylon") == pytest.approx(1.6)

    def test_pro_tier_unknown_material_returns_default(self, monkeypatch):
        """If a material isn't in the overlay (lookup returns None), the
        public default takes over.  Pro tier should not crash on an
        unknown alias — it should degrade to the safety floor."""
        _stub_overlay(monkeypatch, {"pla": {"stress_factor": 0.6}})
        assert _material_stress_factor("Unobtanium") == _DEFAULT_STRESS_FACTOR

    def test_pro_tier_partial_overlay_uses_defaults_for_missing_fields(
        self, monkeypatch,
    ):
        """Overlay entry exists but lacks one field — that field falls
        back to the default while present fields stay overlay-sourced."""
        _stub_overlay(
            monkeypatch,
            {"pla": {"stress_factor": 0.6}},  # no adhesion / shrinkage
        )
        assert _material_stress_factor("pla") == pytest.approx(0.6)
        assert _material_adhesion_strength("pla") == _DEFAULT_ADHESION_STRENGTH
        assert _material_shrinkage_strain("pla") == _DEFAULT_SHRINKAGE_STRAIN

    def test_pro_tier_overlay_lookup_failure_degrades_to_default(
        self, monkeypatch,
    ):
        """If the overlay's ``lookup_material`` raises, the helpers must
        not propagate — they return the conservative default so the
        public path never breaks because of a Pro-side bug.
        """
        import types

        def _boom(_mat):
            raise RuntimeError("simulated overlay corruption")

        fake_overlay = types.SimpleNamespace(lookup_material=_boom)
        fake_features = types.SimpleNamespace(
            is_available=lambda feat: feat == "printability_overlay",
            printability_overlay=fake_overlay,
        )
        fake_bridge = types.ModuleType("kiln_pro.bridge")
        fake_bridge.pro_features = fake_features
        fake_pkg = types.ModuleType("kiln_pro")
        fake_pkg.bridge = fake_bridge
        monkeypatch.setitem(__import__("sys").modules, "kiln_pro", fake_pkg)
        monkeypatch.setitem(
            __import__("sys").modules, "kiln_pro.bridge", fake_bridge,
        )

        assert _material_stress_factor("pla") == _DEFAULT_STRESS_FACTOR
        assert _material_adhesion_strength("pla") == _DEFAULT_ADHESION_STRENGTH
        assert _material_shrinkage_strain("pla") == _DEFAULT_SHRINKAGE_STRAIN

    def test_pro_tier_pro_features_unavailable_degrades_to_default(
        self, monkeypatch,
    ):
        """``pro_features`` exists but ``is_available('printability_overlay')``
        returns False — the helpers must respect that and fall back."""
        import types

        fake_features = types.SimpleNamespace(
            is_available=lambda feat: False,
            printability_overlay=None,
        )
        fake_bridge = types.ModuleType("kiln_pro.bridge")
        fake_bridge.pro_features = fake_features
        fake_pkg = types.ModuleType("kiln_pro")
        fake_pkg.bridge = fake_bridge
        monkeypatch.setitem(__import__("sys").modules, "kiln_pro", fake_pkg)
        monkeypatch.setitem(
            __import__("sys").modules, "kiln_pro.bridge", fake_bridge,
        )

        assert _material_stress_factor("pla") == _DEFAULT_STRESS_FACTOR
        assert _material_adhesion_strength("pla") == _DEFAULT_ADHESION_STRENGTH
        assert _material_shrinkage_strain("pla") == _DEFAULT_SHRINKAGE_STRAIN

    def test_pro_tier_full_overlay_drives_all_three_helpers(self, monkeypatch):
        """End-to-end: a populated overlay drives every helper at once."""
        _stub_overlay(
            monkeypatch,
            {
                "abs": {
                    "stress_factor": 1.5,
                    "adhesion_strength": 0.08,
                    "shrinkage_strain": 0.008,
                },
            },
        )
        assert _material_stress_factor("ABS") == pytest.approx(1.5)
        assert _material_adhesion_strength("ABS") == pytest.approx(0.08)
        assert _material_shrinkage_strain("ABS") == pytest.approx(0.008)


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


from kiln.printability import BundlePrintabilityFindings  # noqa: E402


class TestBundlePrintabilityFindings:
    """Adapter that lets consumers read printability from an inspection
    bundle without re-running ``analyze_printability``.  Exposes the same
    ``.printable / .score / .grade / .recommendations / .to_dict()``
    surface as :class:`PrintabilityReport`."""

    def test_from_bundle_findings_reads_each_field(self):
        findings = {
            "score": 72,
            "grade": "C",
            "printable": True,
            "recommendations": ["increase wall count"],
        }
        f = BundlePrintabilityFindings.from_bundle_findings(findings)
        assert f.printable is True
        assert f.score == 72
        assert f.grade == "C"
        assert f.recommendations == ["increase wall count"]

    def test_printable_defaults_from_score_when_field_absent(self):
        """If the bundle doesn't carry an explicit ``printable``, derive
        it from score (≥50 = printable).  Same rule the SimpleNamespace
        shim used."""
        high = BundlePrintabilityFindings.from_bundle_findings(
            {"score": 80, "grade": "B"}
        )
        low = BundlePrintabilityFindings.from_bundle_findings(
            {"score": 30, "grade": "F"}
        )
        assert high.printable is True
        assert low.printable is False

    def test_to_dict_returns_raw_findings_not_adapter_fields(self):
        """The audit pipes printability.to_dict() into its details
        payload — that payload must stay bundle-faithful so downstream
        consumers see every channel-specific field, not just the
        narrow adapter surface."""
        findings = {
            "score": 88,
            "grade": "A",
            "printable": True,
            "recommendations": ["lower nozzle"],
            "channel_specific_evidence": {"overhangs_total_mm2": 4.2},
        }
        f = BundlePrintabilityFindings.from_bundle_findings(findings)
        out = f.to_dict()
        assert out["score"] == 88
        assert out["channel_specific_evidence"] == {"overhangs_total_mm2": 4.2}

    def test_to_dict_returns_a_copy_not_an_alias(self):
        """Caller mutations to the returned dict must NOT propagate into
        the adapter's stored findings."""
        findings = {"score": 60, "grade": "D"}
        f = BundlePrintabilityFindings.from_bundle_findings(findings)
        out = f.to_dict()
        out["score"] = 0
        assert f.to_dict()["score"] == 60

    def test_recommendations_default_empty_when_field_missing(self):
        f = BundlePrintabilityFindings.from_bundle_findings(
            {"score": 50, "grade": "D"}
        )
        assert f.recommendations == []

    def test_missing_score_treated_as_zero(self):
        """Defensive: a malformed bundle (no score) shouldn't crash the
        adapter — score falls to 0, printable falls to False."""
        f = BundlePrintabilityFindings.from_bundle_findings({})
        assert f.score == 0
        assert f.printable is False
        assert f.grade == "F"

    def test_audit_call_sites_compatible_with_printability_report(self):
        """Every attribute the audit reads off ``printability`` exists
        on the adapter — pins the duck-typing contract."""
        f = BundlePrintabilityFindings.from_bundle_findings(
            {
                "score": 65,
                "grade": "C",
                "printable": True,
                "recommendations": ["raise temp"],
            }
        )
        # These are the only attributes ``audit_original_design`` reads.
        # If anyone adds a new read in original_design.py, this test
        # is where they'll discover the adapter needs to grow.
        _ = f.printable
        _ = f.score
        _ = f.grade
        _ = f.recommendations
        _ = f.to_dict()
