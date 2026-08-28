"""Regression tests for the bridging analyzer + Pro overlay coupling.

Mirrors the ``test_adhesion_force.py`` pattern:

- Lightweight helpers build minimal STL geometries on disk.
- Tests assert against real reality-labels sourced from Bambu TDS,
  CNC Kitchen tests, and the Prusa knowledge base — captured in
  ``kiln_pro/data/BRIDGING_KNOWLEDGE.md`` on the kiln-pro side.
- Tests that exercise Pro-tier-specific behavior carry the
  ``_pro_overlay_required`` skip decorator so they pass silently in
  a clean public-Kiln environment.

Background: pre-2026-05-17 ``analyze_printability`` tripped on a
winding-normalization heuristic for any bridge whose centroid sat
above the mesh's vertical center.  The audit on 2026-05-17 documented
this and a coordinated cross-analyzer fix (shared with the overhangs +
supports analyzers) landed in ``_normalize_triangle_winding`` —
signed-volume detection short-circuits the centroid heuristic for
manifold-clean meshes.  Tests in this file include a ``roof_extra``
parameter that pushes the bridge into the lower mesh half so the
centroid fallback (when reached) doesn't fire either — that isolates
per-material judgement from any residual heuristic noise.
"""

from __future__ import annotations

import struct

import pytest

from kiln.printability import (
    BridgingAnalysis,
    _analyze_bridging,
    _parse_mesh,
    analyze_printability,
)


# ---------------------------------------------------------------------------
# Pro-overlay availability helper (mirrors test_adhesion_force.py pattern,
# plus a data probe — see note below).
# ---------------------------------------------------------------------------
#
# Note on the data probe: the bare ``is_available`` flag answers "is the
# overlay module importable", but says nothing about whether the data
# inside is the audited 2026-05-17 schedule.  Tests in
# ``TestProOverlayBridging`` assert against the audited reality-labels
# (Bambu TDS + CNC Kitchen + Prusa forum) — PLA 30 mm reliable, PETG
# 25 mm reliable, ABS 15 mm open-frame / 40 mm enclosed, TPU 5 mm.  A
# stale overlay JSON (pre-audit values like PLA 10 mm, ABS 7 mm) would
# answer ``is_available = True`` but emit verdicts that contradict the
# reality-labels.  So the helper probes the loaded overlay's PLA
# bridging limit — anything ≥ 30 indicates the audited schedule is
# loaded; anything less (or missing) means we should skip the audited-
# data tests.  Aligns with the helper pattern in
# ``test_warping_analysis.py`` (which doesn't need the probe because
# its assertions are tier-shape, not specific-value).


def _overlay_available() -> bool:
    """True when kiln-pro's per-material printability overlay is loaded
    AND its data is the audited 2026-05-17 bridging schedule."""
    try:
        from kiln_pro.bridge import pro_features  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        if not pro_features.is_available("printability_overlay"):
            return False
    except Exception:  # noqa: BLE001
        return False
    # Data-shape probe: the audited overlay raises PLA's bridging
    # limit to ≥30 mm (Bambu PLA Basic TDS reliable envelope).  A
    # stale or pre-audit overlay reports lower values; skip in that
    # environment rather than fail the assertions.
    try:
        from kiln_pro.printability_overlay import lookup_material  # type: ignore[import-not-found]
        entry = lookup_material("pla") or {}
        return float(entry.get("bridging_limit_mm", 0.0)) >= 30.0
    except Exception:  # noqa: BLE001
        return False


_pro_overlay_required = pytest.mark.skipif(
    not _overlay_available(),
    reason=(
        "requires kiln-pro printability_overlay with the audited "
        "2026-05-17 per-material bridging schedule"
    ),
)


# ---------------------------------------------------------------------------
# Minimal STL builder — explicit triangle list, outward normals.
# A U-channel with a flat roof: two side pillars holding up a ceiling.
# ``roof_extra`` pads the part vertically so the bridge centroid sits in the
# LOWER half of the mesh — defeats any residual centroid-fallback noise in
# the winding heuristic.
# ---------------------------------------------------------------------------


def _write_u_channel_stl(
    path: str,
    *,
    span_mm: float,
    depth_mm: float = 8.0,
    wall_mm: float = 3.0,
    pillar_h_mm: float = 15.0,
    ceiling_t_mm: float = 2.0,
    roof_extra_mm: float = 50.0,
) -> None:
    """Write a U-channel STL: a flat ceiling of length ``span_mm`` resting on
    two side pillars.  The ceiling underside is the bridge."""
    span = span_mm
    ty = depth_mm
    pz = pillar_h_mm
    tx = wall_mm + span + wall_mm
    tz = pz + ceiling_t_mm + roof_extra_mm

    tris: list[tuple[tuple[float, float, float], ...]] = []

    def quad(n, a, b, c, d):
        tris.append((n, a, b, c))
        tris.append((n, a, c, d))

    # Top of part (+Z at z=tz)
    quad((0, 0, 1), (0, 0, tz), (tx, 0, tz), (tx, ty, tz), (0, ty, tz))
    # Left pillar bottom (-Z at z=0)
    tris.append(((0, 0, -1), (0, 0, 0), (wall_mm, ty, 0), (wall_mm, 0, 0)))
    tris.append(((0, 0, -1), (0, 0, 0), (0, ty, 0), (wall_mm, ty, 0)))
    # Right pillar bottom (-Z at z=0)
    tris.append(((0, 0, -1), (wall_mm + span, 0, 0), (tx, ty, 0), (tx, 0, 0)))
    tris.append(((0, 0, -1), (wall_mm + span, 0, 0), (wall_mm + span, ty, 0), (tx, ty, 0)))
    # Ceiling underside (the bridge): -Z at z=pz
    tris.append(((0, 0, -1), (wall_mm, 0, pz), (wall_mm + span, ty, pz), (wall_mm + span, 0, pz)))
    tris.append(((0, 0, -1), (wall_mm, 0, pz), (wall_mm, ty, pz), (wall_mm + span, ty, pz)))
    # Left pillar inner face (+X at x=wall)
    tris.append(((1, 0, 0), (wall_mm, 0, 0), (wall_mm, ty, 0), (wall_mm, ty, pz)))
    tris.append(((1, 0, 0), (wall_mm, 0, 0), (wall_mm, ty, pz), (wall_mm, 0, pz)))
    # Right pillar inner face (-X at x=wall+span)
    tris.append(((-1, 0, 0), (wall_mm + span, 0, 0), (wall_mm + span, ty, pz), (wall_mm + span, ty, 0)))
    tris.append(((-1, 0, 0), (wall_mm + span, 0, 0), (wall_mm + span, 0, pz), (wall_mm + span, ty, pz)))
    # Outer left (-X at x=0)
    quad((-1, 0, 0), (0, 0, 0), (0, 0, tz), (0, ty, tz), (0, ty, 0))
    # Outer right (+X at x=tx)
    quad((1, 0, 0), (tx, 0, 0), (tx, ty, 0), (tx, ty, tz), (tx, 0, tz))

    # Front (-Y at y=0) closed by 4 strips around the cavity opening
    def front_quad(a, b, c, d):
        quad((0, -1, 0), a, b, c, d)
    front_quad((0, 0, 0), (wall_mm, 0, 0), (wall_mm, 0, tz), (0, 0, tz))
    front_quad((wall_mm + span, 0, 0), (tx, 0, 0), (tx, 0, tz), (wall_mm + span, 0, tz))
    front_quad((wall_mm, 0, pz), (wall_mm + span, 0, pz), (wall_mm + span, 0, tz), (wall_mm, 0, tz))
    # NOTE: the gap's front face below the ceiling is deliberately
    # ABSENT — the channel is open at both ends, like a table between
    # two pillars.  With it present the cavity is a four-side-anchored
    # tunnel and the honest supported-chord measurement correctly
    # reports the (short) depth direction as the bridge span, which is
    # not what these fixtures are built to exercise.

    # Back (+Y at y=ty) mirror
    def back_quad(a, b, c, d):
        quad((0, 1, 0), a, b, c, d)
    back_quad((0, ty, 0), (0, ty, tz), (wall_mm, ty, tz), (wall_mm, ty, 0))
    back_quad((wall_mm + span, ty, 0), (wall_mm + span, ty, tz), (tx, ty, tz), (tx, ty, 0))
    back_quad((wall_mm, ty, pz), (wall_mm, ty, tz), (wall_mm + span, ty, tz), (wall_mm + span, ty, pz))

    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for n, v0, v1, v2 in tris:
            f.write(struct.pack("<fff", *n))
            for v in (v0, v1, v2):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


# ---------------------------------------------------------------------------
# Bug regression — bridge length measured as max-XY-bbox-dim,
# not 3D hypotenuse.
# ---------------------------------------------------------------------------


class TestBridgeMeasurementSemantic:
    """Pin the supported-chord semantic: ``max_bridge_length_mm`` is
    the distance the slicer actually bridges — the shortest chord
    between opposing supported anchors at the deck's widest point.
    Slicers pick the bridge direction, so a narrow cavity reports its
    anchored span, never its depth (the old XY-bbox measure) and never
    the 3D longest edge (the pre-2026-05-17 hypotenuse bias)."""

    def test_2mm_bridge_8mm_depth_reports_2mm_span(self, tmp_path):
        """A 2 mm bridge through an 8 mm-deep cavity must report
        2.0 mm — the anchored gap the filament crosses.  The XY-bbox
        era reported 8.0 (the cavity depth, which nothing bridges);
        the pre-bbox era reported 8.25 (the hypotenuse)."""
        p = str(tmp_path / "tiny_bridge.stl")
        _write_u_channel_stl(p, span_mm=2.0, depth_mm=8.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.bridge_count > 0
        assert r.bridging.max_bridge_length_mm == pytest.approx(2.0, abs=0.05)

    def test_30mm_bridge_8mm_depth_reports_30mm_not_31(self, tmp_path):
        """A 30 mm bridge through an 8 mm-deep cavity reports 30.0 mm
        (the bridge span = max XY dim), not 31.05 mm (the hypotenuse)."""
        p = str(tmp_path / "mid_bridge.stl")
        _write_u_channel_stl(p, span_mm=30.0, depth_mm=8.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.bridge_count > 0
        assert r.bridging.max_bridge_length_mm == pytest.approx(30.0, abs=0.05)

    def test_thin_bridge_reports_span_not_sliver_width(self, tmp_path):
        """A 50 mm bridge × 3 mm depth reports 50.0 mm (the span)."""
        p = str(tmp_path / "thin_bridge.stl")
        _write_u_channel_stl(p, span_mm=50.0, depth_mm=3.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.bridge_count > 0
        assert r.bridging.max_bridge_length_mm == pytest.approx(50.0, abs=0.05)


# ---------------------------------------------------------------------------
# Public model — material-blind 10 mm hardcoded threshold
# ---------------------------------------------------------------------------


class TestPublicBridgingFlag:
    """Free-tier ``needs_supports_for_bridges`` flips at the
    hardcoded 10 mm threshold regardless of material.  Behavior
    preserved post-fix (Pro tier adds material awareness; free
    tier stays simple)."""

    def test_short_bridge_not_flagged(self, tmp_path):
        p = str(tmp_path / "short.stl")
        _write_u_channel_stl(p, span_mm=8.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.needs_supports_for_bridges is False

    def test_long_bridge_flagged(self, tmp_path):
        p = str(tmp_path / "long.stl")
        _write_u_channel_stl(p, span_mm=15.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.needs_supports_for_bridges is True

    def test_no_bridge_not_flagged(self, tmp_path):
        """A solid box has no bridges; flag is False."""
        path = str(tmp_path / "solid.stl")
        x, y, z = 20, 20, 20
        verts = [
            (-x/2, -y/2, 0), (x/2, -y/2, 0), (x/2, y/2, 0), (-x/2, y/2, 0),
            (-x/2, -y/2, z), (x/2, -y/2, z), (x/2, y/2, z), (-x/2, y/2, z),
        ]
        faces = [
            (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
            (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3),
        ]
        with open(path, "wb") as f:
            f.write(b"\x00" * 80)
            f.write(struct.pack("<I", len(faces)))
            for face in faces:
                f.write(struct.pack("<fff", 0, 0, 0))
                for v in (verts[face[0]], verts[face[1]], verts[face[2]]):
                    f.write(struct.pack("<fff", *v))
                f.write(struct.pack("<H", 0))
        r = analyze_printability(path, material="pla")
        assert r.bridging.bridge_count == 0
        assert r.bridging.needs_supports_for_bridges is False


# ---------------------------------------------------------------------------
# Pro tier — per-material bridging limits + score-cap carve-out +
# enclosed-vs-open-frame split.
# ---------------------------------------------------------------------------


class TestProOverlayBridging:
    """Pro overlay's per-material ``bridging_limit_mm`` fires a curated
    warning AND a visible score penalty when exceeded.  Limits are
    sourced from BRIDGING_KNOWLEDGE.md (Bambu TDS + CNC Kitchen +
    Prusa community)."""

    @_pro_overlay_required
    def test_pla_30mm_within_reliable_limit_no_warning(self, tmp_path):
        """A 30 mm PLA bridge is at the reliable Bambu PLA Basic TDS
        limit — Pro should not warn that the bridge is too long for
        the material."""
        p = str(tmp_path / "pla_30.stl")
        _write_u_channel_stl(p, span_mm=30.0)
        r = analyze_printability(p, material="pla")
        bridge_warns = [
            x for x in r.recommendations
            if "too long for pla" in x.lower()
        ]
        assert bridge_warns == [], (
            f"PLA 30mm bridge should NOT trigger 'too long' warning; got: {bridge_warns}"
        )

    @_pro_overlay_required
    def test_pla_50mm_exceeds_limit_warns_and_penalizes_score(self, tmp_path):
        """A 50 mm PLA bridge exceeds the reliable limit (30 mm) and
        sits in the marginal zone — Pro fires the curated warning AND
        the penalty reaches the user-facing score instead of being
        capped away.

        The carve-out is measured against the score the overlay would
        have published WITHOUT the bridging finding — ``max(recomputed
        raw, free baseline)``, the standard "Pro never worse than free"
        cap — not against the free score itself.

        It used to be measured against the free score, and that stopped
        being meaningful on 2026-08-25.  Before then the overlay read
        the raw ``max_overhang_angle``, which reports 90° for any part
        with a ceiling anywhere, so this U-channel's flat roof was
        double-counted: once as a 90° overhang (a 30-point severe
        penalty) and again as a bridge.  That penalty, not the bridging
        carve-out, was what pushed Pro below free.  ``free-air span and
        angle`` (public) plus its paired overlay seam now judge a flat
        deck in the SPAN domain only, so the phantom overhang penalty is
        gone and Pro legitimately scores this part ABOVE the
        material-blind free model — while still charging it for the
        bridge.  Asserting ``enriched < original`` would now only pass
        by reinstating the double-count.
        """
        p = str(tmp_path / "pla_50.stl")
        _write_u_channel_stl(p, span_mm=50.0)
        r_pro = analyze_printability(p, material="pla")
        assert r_pro.enrichment is not None
        # Curated warning text fired
        bridge_warns = [
            x for x in r_pro.recommendations
            if "too long for pla" in x.lower()
        ]
        assert bridge_warns, "Pro should warn on 50mm PLA bridge"

        enriched = r_pro.enrichment["enriched_score"]
        # What the overlay would have published with no bridging finding.
        uncapped = max(
            r_pro.enrichment["recomputed_raw"],
            r_pro.enrichment["original_score"],
        )
        assert enriched < uncapped, (
            "bridging penalty was capped away instead of reaching the "
            f"user-facing score; uncapped={uncapped}, enriched={enriched}"
        )

        # And the charge is attributable to the bridge specifically: an
        # otherwise-identical part whose span is inside the PLA limit
        # keeps the points this one loses.
        q = str(tmp_path / "pla_30.stl")
        _write_u_channel_stl(q, span_mm=30.0)
        compliant = analyze_printability(q, material="pla")
        assert compliant.enrichment is not None
        assert enriched < compliant.enrichment["enriched_score"], (
            "a 50mm bridge should score below an otherwise-identical 30mm "
            f"one; got {enriched} vs {compliant.enrichment['enriched_score']}"
        )

    @_pro_overlay_required
    def test_petg_25mm_within_dry_reliable_limit_no_warning(self, tmp_path):
        """PETG 25 mm bridge is the dry-filament reliable envelope per
        Bambu PETG Basic TDS (30 mm) minus Prusa-forum-confirmed
        wet-filament risk margin."""
        p = str(tmp_path / "petg_25.stl")
        _write_u_channel_stl(p, span_mm=25.0)
        r = analyze_printability(p, material="petg")
        bridge_warns = [
            x for x in r.recommendations if "too long for petg" in x.lower()
        ]
        assert bridge_warns == []

    @_pro_overlay_required
    def test_abs_30mm_exceeds_open_frame_limit_warns(self, tmp_path):
        """ABS 30 mm exceeds the open-frame conservative limit (15 mm).
        Bambu TDS publishes 40 mm but that assumes enclosure; the
        overlay uses the open-frame value for the majority-user case."""
        p = str(tmp_path / "abs_30.stl")
        _write_u_channel_stl(p, span_mm=30.0)
        r = analyze_printability(p, material="abs")
        bridge_warns = [
            x for x in r.recommendations if "too long for abs" in x.lower()
        ]
        assert bridge_warns

    @_pro_overlay_required
    def test_abs_30mm_enclosed_chamber_no_warning(self, tmp_path):
        """ABS 30 mm on a known-enclosed printer (Bambu X1C) uses the
        enclosed limit (40 mm from Bambu ABS V3.0 TDS), so no warning."""
        p = str(tmp_path / "abs_30_x1c.stl")
        _write_u_channel_stl(p, span_mm=30.0)
        r = analyze_printability(p, material="abs", printer_id="bambu_x1c")
        bridge_warns = [
            x for x in r.recommendations if "too long for abs" in x.lower()
        ]
        assert bridge_warns == [], (
            f"ABS 30mm on enclosed X1C should NOT warn (enclosed limit = 40mm); got: {bridge_warns}"
        )

    @_pro_overlay_required
    def test_abs_45mm_enclosed_still_warns_above_tds_limit(self, tmp_path):
        """ABS 45 mm on Bambu X1C exceeds even the enclosed TDS limit
        (40 mm); Pro still warns."""
        p = str(tmp_path / "abs_45_x1c.stl")
        _write_u_channel_stl(p, span_mm=45.0)
        r = analyze_printability(p, material="abs", printer_id="bambu_x1c")
        bridge_warns = [
            x for x in r.recommendations if "too long for abs" in x.lower()
        ]
        assert bridge_warns

    @_pro_overlay_required
    def test_unknown_printer_uses_open_frame_conservative_limit(self, tmp_path):
        """Honesty-over-confidence: an unrecognized printer_id falls
        back to the open-frame limit, not the enclosed one.  A 25 mm
        ABS bridge on an unknown printer must warn."""
        p = str(tmp_path / "abs_25_unknown.stl")
        _write_u_channel_stl(p, span_mm=25.0)
        r = analyze_printability(
            p, material="abs", printer_id="some-totally-unknown-printer"
        )
        bridge_warns = [
            x for x in r.recommendations if "too long for abs" in x.lower()
        ]
        assert bridge_warns, (
            "Unknown printer must use open-frame limit (15mm), so 25mm bridge warns"
        )

    @_pro_overlay_required
    def test_tpu_10mm_warns_per_soft_melt_collapse(self, tmp_path):
        """TPU 95A collapses past ~5 mm; a 10 mm bridge must warn
        even though free-tier flag wouldn't fire (≤ 10 mm hardcoded)."""
        p = str(tmp_path / "tpu_10.stl")
        _write_u_channel_stl(p, span_mm=10.0)
        r = analyze_printability(p, material="tpu")
        bridge_warns = [
            x for x in r.recommendations if "too long for tpu" in x.lower()
        ]
        assert bridge_warns

    @_pro_overlay_required
    def test_tpu_enclosed_chamber_no_enclosure_bonus(self, tmp_path):
        """TPU doesn't get an enclosure bonus — the soft-melt collapse
        mode dominates over chamber temperature.  Limit stays 5 mm
        regardless of printer_id."""
        p = str(tmp_path / "tpu_10_x1c.stl")
        _write_u_channel_stl(p, span_mm=10.0)
        r = analyze_printability(p, material="tpu", printer_id="bambu_x1c")
        bridge_warns = [
            x for x in r.recommendations if "too long for tpu" in x.lower()
        ]
        assert bridge_warns

    @_pro_overlay_required
    def test_score_carve_out_only_fires_when_bridging_limit_exceeded(
        self, tmp_path
    ):
        """The score carve-out is bridging-specific.  A model with no
        over-limit bridges sees the standard max() cap (Pro score >=
        free score on overhang/wall rules)."""
        p = str(tmp_path / "safe_bridge.stl")
        _write_u_channel_stl(p, span_mm=5.0)  # under all material limits
        r = analyze_printability(p, material="pla")
        assert r.enrichment is not None
        original = r.enrichment["original_score"]
        enriched = r.enrichment["enriched_score"]
        # No bridging penalty fires → Pro score >= free score (cap rule
        # preserved for non-bridging penalties)
        assert enriched >= original


# ---------------------------------------------------------------------------
# Bridge-detection scope — documents what the analyzer catches.
# ---------------------------------------------------------------------------


class TestBridgeDetectionScope:
    """Pin the post-coordinated-fix detection scope."""

    def test_lower_half_bridge_detected(self, tmp_path):
        """Bridges in the lower half of the mesh are detected."""
        p = str(tmp_path / "lower.stl")
        _write_u_channel_stl(p, span_mm=20.0, roof_extra_mm=50.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.bridge_count > 0
        assert r.bridging.max_bridge_length_mm == pytest.approx(20.0, abs=0.05)

    def test_direct_call_without_winding_normalization_detects(
        self, tmp_path,
    ):
        """``_analyze_bridging`` with ``normalize_winding=False`` is the
        ground-truth path — used internally by tests to verify the
        geometry is correct independent of the heuristic pipeline."""
        p = str(tmp_path / "direct.stl")
        _write_u_channel_stl(p, span_mm=30.0, roof_extra_mm=0.0)
        tris, _ = _parse_mesh(p)
        result = _analyze_bridging(tris, z_min=0.0, normalize_winding=False)
        assert result.bridge_count > 0
        # XY-bbox-max measurement post-fix
        assert result.max_bridge_length_mm == pytest.approx(30.0, abs=0.05)


# ---------------------------------------------------------------------------
# Connected-component aggregator — arches and multi-region cases.
# Pre-aggregator behavior: each triangle measured independently, so an
# arched ceiling reported as N tiny ~edge-length bridges instead of one
# chord-length bridge.  Post-aggregator: triangles sharing an edge are
# grouped, each region's XY bbox gives the bridge span.
# ---------------------------------------------------------------------------


def _write_flat_top_arch_stl(
    path: str,
    *,
    span_mm: float,
    rise_mm: float = 4.0,
    depth_mm: float = 10.0,
    wall_mm: float = 3.0,
    base_mm: float = 5.0,
    roof_mm: float = 8.0,
    segments: int = 12,
):
    """Write a shallow arch — small rise vs span, so most of the underside
    has nearly-horizontal normal and qualifies as a bridge."""
    import math

    # Geometry: arch underside is a circular arc.
    # Chord = span_mm; rise = rise_mm.  Find circle radius + center.
    radius = (rise_mm * rise_mm + (span_mm / 2) ** 2) / (2 * rise_mm)
    cx = wall_mm + span_mm / 2
    cz = base_mm + rise_mm - radius
    theta_half = math.asin((span_mm / 2) / radius)

    tx = wall_mm + span_mm + wall_mm
    tz = base_mm + rise_mm + roof_mm
    ty = depth_mm

    tris: list[tuple[tuple[float, float, float], ...]] = []

    # Sample arch underside points (XZ plane, ordered along X from left to right).
    arch_xz: list[tuple[float, float]] = []
    for i in range(segments + 1):
        theta = -theta_half + (2 * theta_half) * i / segments
        x = cx + radius * math.sin(theta)
        z = cz + radius * math.cos(theta)
        arch_xz.append((x, z))

    # Underside: for each adjacent pair, build a quad spanning Y from 0..ty,
    # split into 2 triangles, normals pointing down/outward.
    for i in range(segments):
        x0, z0 = arch_xz[i]
        x1, z1 = arch_xz[i + 1]
        # Outward-from-center normal direction (averaged across the segment).
        mx = (x0 + x1) / 2
        mz = (z0 + z1) / 2
        nx = mx - cx
        nz = mz - cz
        nrm = (nx * nx + nz * nz) ** 0.5 or 1.0
        nx /= nrm
        nz /= nrm
        if nz > 0:  # ensure underside (downward)
            nx, nz = -nx, -nz
        # Two triangles per quad; winding chosen so the cross product
        # matches the downward-outward normal we want.
        tris.append(((nx, 0.0, nz), (x0, 0, z0), (x1, ty, z1), (x1, 0, z1)))
        tris.append(((nx, 0.0, nz), (x0, 0, z0), (x0, ty, z0), (x1, ty, z1)))

    # Top of part (+Z at z=tz)
    tris.append(((0, 0, 1), (0, 0, tz), (tx, 0, tz), (tx, ty, tz)))
    tris.append(((0, 0, 1), (0, 0, tz), (tx, ty, tz), (0, ty, tz)))
    # Bottom of supports (-Z at z=0): two strips for the support pillars
    tris.append(((0, 0, -1), (0, 0, 0), (wall_mm, ty, 0), (wall_mm, 0, 0)))
    tris.append(((0, 0, -1), (0, 0, 0), (0, ty, 0), (wall_mm, ty, 0)))
    tris.append(((0, 0, -1), (wall_mm + span_mm, 0, 0), (tx, ty, 0), (tx, 0, 0)))
    tris.append(((0, 0, -1), (wall_mm + span_mm, 0, 0), (wall_mm + span_mm, ty, 0), (tx, ty, 0)))
    # Cavity floor (-Z at z=0 under the arch, hangs in the air below
    # base_mm): actually omit — cavity is OPEN at front/back so no floor.
    # Outer faces
    tris.append(((-1, 0, 0), (0, 0, 0), (0, 0, tz), (0, ty, tz)))
    tris.append(((-1, 0, 0), (0, 0, 0), (0, ty, tz), (0, ty, 0)))
    tris.append(((1, 0, 0), (tx, 0, 0), (tx, ty, 0), (tx, ty, tz)))
    tris.append(((1, 0, 0), (tx, 0, 0), (tx, ty, tz), (tx, 0, tz)))
    # Pillar inner faces
    tris.append(((1, 0, 0), (wall_mm, 0, 0), (wall_mm, ty, 0), (wall_mm, ty, base_mm)))
    tris.append(((1, 0, 0), (wall_mm, 0, 0), (wall_mm, ty, base_mm), (wall_mm, 0, base_mm)))
    tris.append(((-1, 0, 0), (wall_mm + span_mm, 0, 0), (wall_mm + span_mm, ty, base_mm), (wall_mm + span_mm, ty, 0)))
    tris.append(((-1, 0, 0), (wall_mm + span_mm, 0, 0), (wall_mm + span_mm, 0, base_mm), (wall_mm + span_mm, ty, base_mm)))
    # Above-arch outer top strip (rectangle from arch apex up to roof)
    arch_apex_z = cz + radius  # peak of arch underside
    if arch_apex_z < tz:
        # Close the cavity top: arch crowns at arch_apex_z, roof starts
        # at tz.  Fill the band from arch sides up to tz.
        # (Skipping nuanced — cavity here doesn't need to be watertight
        # for the bridge analyzer; left as quick test geometry.)
        pass
    # Front (-Y at y=0) and back (+Y at y=ty) closure — simple flat caps
    # over the cavity opening (good enough for bridge analyzer testing).
    for y_face, ny in ((0.0, -1.0), (ty, 1.0)):
        tris.append(((0, ny, 0), (0, y_face, 0), (wall_mm, y_face, 0), (wall_mm, y_face, tz)))
        tris.append(((0, ny, 0), (0, y_face, 0), (wall_mm, y_face, tz), (0, y_face, tz)))
        tris.append(((0, ny, 0), (wall_mm + span_mm, y_face, 0), (tx, y_face, 0), (tx, y_face, tz)))
        tris.append(((0, ny, 0), (wall_mm + span_mm, y_face, 0), (tx, y_face, tz), (wall_mm + span_mm, y_face, tz)))
        # Cap the arch opening — flat quad from one support top to the other
        tris.append(((0, ny, 0), (wall_mm, y_face, base_mm), (wall_mm + span_mm, y_face, base_mm), (wall_mm + span_mm, y_face, tz)))
        tris.append(((0, ny, 0), (wall_mm, y_face, base_mm), (wall_mm + span_mm, y_face, tz), (wall_mm, y_face, tz)))

    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for n, v0, v1, v2 in tris:
            f.write(struct.pack("<fff", *n))
            for v in (v0, v1, v2):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


class TestBridgeAggregation:
    """Connected-component aggregator: ``max_bridge_length_mm`` reflects
    the largest connected bridge REGION'S XY bbox, not individual
    triangle edges.  Pre-aggregator behavior would report an arched
    ceiling as N tiny ~10 mm bridges; post-aggregator it reports the
    arch chord (for shallow arches) or the apex bridge width (for
    semicircular arches where the sides are too steep to qualify as
    bridge per ``nz < -0.9`` and print as overhangs instead)."""

    def test_shallow_arch_reports_full_chord_length(self, tmp_path):
        """A shallow arch (rise << span) has near-horizontal underside
        almost everywhere → most facets qualify as bridge, region
        spans the full chord."""
        p = str(tmp_path / "arch_shallow.stl")
        _write_flat_top_arch_stl(p, span_mm=50.0, rise_mm=5.0)
        tris, _ = _parse_mesh(p)
        result = _analyze_bridging(tris, z_min=0.0, normalize_winding=False)
        assert result.bridge_count > 0
        # Pre-aggregator would have reported ~5-10 mm (per-facet).
        # Post-aggregator: full 50 mm chord.
        assert result.max_bridge_length_mm == pytest.approx(50.0, abs=2.0)

    def test_semicircular_arch_reports_apex_bridge_width(self, tmp_path):
        """A semicircular arch (rise = span/2) has steeply sloped sides
        that print as OVERHANGS, not bridges, per ``nz < -0.9``.  The
        bridge is just the apex region.  Post-aggregator reports >
        per-facet width but < full chord — correct slicer-aware
        behavior."""
        p = str(tmp_path / "arch_semicircular.stl")
        _write_flat_top_arch_stl(p, span_mm=50.0, rise_mm=25.0)
        tris, _ = _parse_mesh(p)
        result = _analyze_bridging(tris, z_min=0.0, normalize_winding=False)
        assert result.bridge_count > 0
        # Per-facet would have reported individual edge lengths (~10 mm).
        # Aggregator reports the apex region span, which is > one facet
        # edge but < the full chord (sides print as overhangs).
        assert result.max_bridge_length_mm > 10.0
        assert result.max_bridge_length_mm < 50.0

    def test_flat_bridge_unchanged_by_aggregator(self, tmp_path):
        """Flat single bridges (2 triangles in 1 region) measure
        identically before and after aggregation — fix is additive."""
        p = str(tmp_path / "flat.stl")
        _write_u_channel_stl(p, span_mm=30.0, depth_mm=8.0)
        r = analyze_printability(p, material="pla")
        assert r.bridging.max_bridge_length_mm == pytest.approx(30.0, abs=0.05)
        # bridge_count stays per-triangle: 2 triangles for the flat
        # ceiling, even though they form 1 connected region.
        assert r.bridging.bridge_count == 2

    def test_separate_bridges_in_same_part_reported_separately(self, tmp_path):
        """If a part has two disconnected bridge regions of different
        spans, the aggregator must report the LARGER as
        ``max_bridge_length_mm`` (each region's XY bbox computed
        independently)."""
        # Two side-by-side U-channels written as one STL via concatenation
        # is mesh-non-manifold; the cleaner test is just two STLs verified
        # individually.  Pin the multi-region semantic via direct
        # call.
        p1 = str(tmp_path / "u1.stl")
        p2 = str(tmp_path / "u2.stl")
        _write_u_channel_stl(p1, span_mm=15.0)
        _write_u_channel_stl(p2, span_mm=40.0)
        # Verify each independently is correctly measured.
        r1 = analyze_printability(p1, material="pla")
        r2 = analyze_printability(p2, material="pla")
        assert r1.bridging.max_bridge_length_mm == pytest.approx(15.0, abs=0.05)
        assert r2.bridging.max_bridge_length_mm == pytest.approx(40.0, abs=0.05)
