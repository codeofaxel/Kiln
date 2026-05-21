"""Tests for the 2026-05-17 public-surface extensions on
SupportAnalysis + analyze_printability:

- ``SupportAnalysis.likely_substituted_by_bridge`` flag
- ``support_percentage`` clamped to <=100%
- ``analyze_printability(..., slicer_style=...)`` kwarg propagates
"""

from __future__ import annotations

import math
import os
import tempfile

import pytest

from kiln.printability import (
    SupportAnalysis,
    _analyze_supports,
    _likely_bridge_substituted,
    analyze_printability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_stl(triangles, path):
    """Write minimal ASCII STL (normals don't matter for this test)."""
    lines = ["solid t"]
    for tri in triangles:
        lines.append("facet normal 0 0 1")
        lines.append(" outer loop")
        for v in tri:
            lines.append(f"  vertex {v[0]} {v[1]} {v[2]}")
        lines.append(" endloop")
        lines.append("endfacet")
    lines.append("endsolid t")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _tabletop_stl(path: str):
    """50×50 tabletop on 4 legs — the canonical bridge-substitution case."""
    # 4 legs at corners + top
    tris = []
    leg_w = 5.0
    leg_h = 20.0
    top_dim = 50.0
    top_h = 5.0

    def cuboid(x0, y0, z0, dx, dy, dz):
        x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
        return [
            ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0)),
            ((x0, y0, z0), (x1, y1, z0), (x1, y0, z0)),
            ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1)),
            ((x0, y0, z1), (x1, y1, z1), (x0, y1, z1)),
            ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),
            ((x0, y0, z0), (x0, y1, z1), (x0, y1, z0)),
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1)),
            ((x1, y0, z0), (x1, y1, z1), (x1, y0, z1)),
            ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1)),
            ((x0, y0, z0), (x1, y0, z1), (x0, y0, z1)),
            ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),
            ((x0, y1, z0), (x1, y1, z1), (x1, y1, z0)),
        ]

    half = top_dim / 2 - leg_w / 2
    for cx, cy in [(-half, -half), (half, -half), (-half, half), (half, half)]:
        tris += cuboid(cx - leg_w / 2, cy - leg_w / 2, 0, leg_w, leg_w, leg_h)
    tris += cuboid(-top_dim / 2, -top_dim / 2, leg_h, top_dim, top_dim, top_h)
    _write_stl(tris, path)


def _long_thin_overhang_stl(path: str):
    """Wide base + tall thin post + long thin overhang at top — the
    "support_percentage > 100%" case before clamping."""
    tris = []

    def cuboid(x0, y0, z0, dx, dy, dz):
        x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
        return [
            ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0)),
            ((x0, y0, z0), (x1, y1, z0), (x1, y0, z0)),
            ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1)),
            ((x0, y0, z1), (x1, y1, z1), (x0, y1, z1)),
            ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),
            ((x0, y0, z0), (x0, y1, z1), (x0, y1, z0)),
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1)),
            ((x1, y0, z0), (x1, y1, z1), (x1, y0, z1)),
            ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1)),
            ((x0, y0, z0), (x1, y0, z1), (x0, y0, z1)),
            ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),
            ((x0, y1, z0), (x1, y1, z1), (x1, y1, z0)),
        ]

    tris += cuboid(-25, -25, 0, 50, 50, 5)  # base
    tris += cuboid(-4, -4, 5, 8, 8, 25)     # post
    tris += cuboid(-15, -4, 30, 30, 8, 4)   # overhang
    _write_stl(tris, path)


# ---------------------------------------------------------------------------
# _likely_bridge_substituted — two-sided-anchor geometry test
# ---------------------------------------------------------------------------


def _cuboid_tris(x0, y0, z0, dx, dy, dz):
    """The 12 triangles of an axis-aligned box."""
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    return [
        ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0)),
        ((x0, y0, z0), (x1, y1, z0), (x1, y0, z0)),
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1)),
        ((x0, y0, z1), (x1, y1, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x0, y1, z1), (x0, y1, z0)),
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1)),
        ((x1, y0, z0), (x1, y1, z1), (x1, y0, z1)),
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1)),
        ((x0, y0, z0), (x1, y0, z1), (x0, y0, z1)),
        ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1)),
        ((x0, y1, z0), (x1, y1, z1), (x1, y1, z0)),
    ]


# A flat overhang slab underside at z=10, footprint x in [0,30] y in [0,10].
_OVERHANG_SLAB = [
    ((0.0, 0.0, 10.0), (30.0, 0.0, 10.0), (30.0, 10.0, 10.0)),
    ((0.0, 0.0, 10.0), (30.0, 10.0, 10.0), (0.0, 10.0, 10.0)),
]


def test_bridge_substitution_empty_overhang_returns_false():
    """No overhang triangles — nothing to bridge-substitute."""
    assert _likely_bridge_substituted([], []) is False


def test_bridge_substitution_island_returns_false():
    """An overhang with no part material below it is a floating
    island, not a bridge — it needs supports."""
    assert _likely_bridge_substituted(_OVERHANG_SLAB, _OVERHANG_SLAB) is False


def test_bridge_substitution_cantilever_returns_false():
    """An overhang anchored on ONE side only — a cantilever — cannot
    be bridged: there is no second anchor.  Returns False so the
    verdict keeps 'needs supports'."""
    column = _cuboid_tris(0.0, 0.0, 0.0, 5.0, 10.0, 10.0)
    assert _likely_bridge_substituted(_OVERHANG_SLAB, _OVERHANG_SLAB + column) is False


def test_bridge_substitution_central_post_returns_false():
    """A central post (the cantilever-T pattern) anchors the middle,
    not the ends — the overhang extends past it on both sides and
    cannot be bridged."""
    post = _cuboid_tris(12.5, 0.0, 0.0, 5.0, 10.0, 10.0)
    assert _likely_bridge_substituted(_OVERHANG_SLAB, _OVERHANG_SLAB + post) is False


def test_bridge_substitution_two_sided_bridge_returns_true():
    """An overhang anchored on TWO opposing sides with a clear gap
    between them — a genuine bridge the slicer fills on its own."""
    left = _cuboid_tris(0.0, 0.0, 0.0, 5.0, 10.0, 10.0)
    right = _cuboid_tris(25.0, 0.0, 0.0, 5.0, 10.0, 10.0)
    all_tris = _OVERHANG_SLAB + left + right
    assert _likely_bridge_substituted(_OVERHANG_SLAB, all_tris) is True


# ---------------------------------------------------------------------------
# _analyze_supports — bridge flag end-to-end
# ---------------------------------------------------------------------------


def test_supports_floating_overhang_keeps_bridge_flag_false():
    """A bare floating overhang with nothing beneath it is not a
    bridge — the flag stays False so the verdict keeps 'needs
    supports'."""
    tris = [
        ((0.0, 0.0, 10.0), (5.0, 0.0, 10.0), (5.0, 5.0, 10.0)),
    ]
    result = _analyze_supports(tris, z_min=0.0)
    assert result.likely_substituted_by_bridge is False


# ---------------------------------------------------------------------------
# support_percentage clamp
# ---------------------------------------------------------------------------


def test_support_percentage_never_exceeds_100():
    """Headline regression: pre-clamp, E01-style geometry (long thin
    overhang above wide base) could report 116% support percentage.
    Post-clamp, the value is capped at 100.0."""
    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        path = f.name
    try:
        _long_thin_overhang_stl(path)
        report = analyze_printability(path, material="PLA")
        assert report.supports.support_percentage <= 100.0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# analyze_printability slicer_style kwarg
# ---------------------------------------------------------------------------


def test_analyze_printability_accepts_slicer_style_kwarg():
    """The kwarg exists, is positional-keyword-only, and defaults to 'grid'."""
    import inspect
    sig = inspect.signature(analyze_printability)
    assert "slicer_style" in sig.parameters
    assert sig.parameters["slicer_style"].default == "grid"


def test_analyze_printability_slicer_style_propagates_to_overlay():
    """When kiln-pro implements slicer_style support, the slicer_style
    propagates into the enrichment block's supports_calibration.

    The kiln-pro overlay landed slicer_style + supports_calibration on a
    later branch than the one currently installed in some environments;
    when the running kiln-pro is older the enrichment block omits the
    ``supports_calibration`` key and falls back to legacy enrichment
    fields. We probe for the feature with an actual call and skip if
    absent rather than asserting against a kiln-pro version we cannot
    guarantee is installed.
    """
    try:
        from kiln_pro.bridge import pro_features
        if not pro_features.is_available("printability_overlay"):
            pytest.skip("kiln-pro printability_overlay not available")
    except ImportError:
        pytest.skip("kiln-pro not installed in this environment")

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        path = f.name
    try:
        _tabletop_stl(path)
        # Feature probe: does this kiln-pro produce supports_calibration?
        probe = analyze_printability(path, material="PLA", slicer_style="grid")
        if probe.enrichment is None or "supports_calibration" not in probe.enrichment:
            pytest.skip(
                "kiln-pro printability_overlay installed but does not "
                "implement supports_calibration in the enrichment block; "
                "feature shipped on a later overlay engine version."
            )

        report_grid = probe
        report_organic = analyze_printability(path, material="PLA", slicer_style="organic")

        # Both reports should have enrichment with supports_calibration
        for r in (report_grid, report_organic):
            assert r.enrichment is not None
            assert "supports_calibration" in r.enrichment

        cal_grid = report_grid.enrichment["supports_calibration"]
        cal_organic = report_organic.enrichment["supports_calibration"]

        assert cal_grid["slicer_style"] == "grid"
        assert cal_organic["slicer_style"] == "organic"
        # Organic divisor (5) is larger than Grid (2), so calibrated
        # volume should be smaller for organic on the same naive input.
        assert cal_organic["calibrated_volume_mm3"] < cal_grid["calibrated_volume_mm3"]
    finally:
        os.unlink(path)
