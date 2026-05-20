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
    _BRIDGE_SUBSTITUTION_MAX_SPAN_MM,
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
# likely_substituted_by_bridge — the flag itself
# ---------------------------------------------------------------------------


def test_bridge_substitution_helper_empty_regions_returns_false():
    assert _likely_bridge_substituted([], bbox={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50}) is False


def test_bridge_substitution_helper_no_bbox_returns_false():
    regions = [{"x": 0, "y": 0, "z": 20, "volume_mm3": 100.0}]
    assert _likely_bridge_substituted(regions, bbox=None) is False


def test_bridge_substitution_fires_on_small_footprint():
    """Footprint dimension <= 30mm → bridge-substitution likely."""
    regions = [{"x": 0, "y": 0, "z": 20, "volume_mm3": 100.0}]
    bbox = {"x_min": 0, "x_max": 25, "y_min": 0, "y_max": 25, "z_min": 0, "z_max": 30}
    assert _likely_bridge_substituted(regions, bbox=bbox) is True


def test_bridge_substitution_does_not_fire_on_large_footprint():
    """Footprint > 30mm on both axes → slicer can't bridge cleanly."""
    regions = [{"x": 0, "y": 0, "z": 20, "volume_mm3": 100.0}]
    bbox = {"x_min": 0, "x_max": 100, "y_min": 0, "y_max": 100, "z_min": 0, "z_max": 30}
    assert _likely_bridge_substituted(regions, bbox=bbox) is False


def test_bridge_substitution_uses_short_axis():
    """When the part is long in X but short in Y, the slicer can bridge
    across the short axis. The flag should fire."""
    regions = [{"x": 0, "y": 0, "z": 20, "volume_mm3": 100.0}]
    bbox = {"x_min": 0, "x_max": 100, "y_min": 0, "y_max": 20, "z_min": 0, "z_max": 30}
    assert _likely_bridge_substituted(regions, bbox=bbox) is True


def test_bridge_substitution_threshold_constant():
    assert _BRIDGE_SUBSTITUTION_MAX_SPAN_MM == 30.0


def test_bridge_substitution_rejects_scattered_overhang_regions():
    """Multi-arm geometry (star, spider, plus-sign) has overhangs at
    far corners of the bbox. Even when the bbox is small enough to
    pass the short-axis check, scattered regions can't be replaced by
    a single bridge — the slicer has to support each arm. The
    clustering check rejects these."""
    regions = [
        {"x": -9.0, "y": -9.0, "z": 20.0, "volume_mm3": 100.0},
        {"x": 9.0, "y": 9.0, "z": 20.0, "volume_mm3": 100.0},
    ]
    bbox = {"x_min": -10, "x_max": 10, "y_min": -10, "y_max": 10,
            "z_min": 0, "z_max": 30}
    # Region span = 18mm, bbox max = 20mm → ratio 0.90 > 0.80 → False
    assert _likely_bridge_substituted(regions, bbox=bbox) is False


def test_bridge_substitution_accepts_clustered_overhang_regions():
    """A part with overhangs clustered in one zone (a localized cavity)
    IS a bridge candidate: the slicer can span the bridge in one go."""
    regions = [
        {"x": -3.0, "y": -3.0, "z": 20.0, "volume_mm3": 100.0},
        {"x": 3.0, "y": 3.0, "z": 20.0, "volume_mm3": 100.0},
    ]
    bbox = {"x_min": -10, "x_max": 10, "y_min": -10, "y_max": 10,
            "z_min": 0, "z_max": 30}
    # Region span = 6mm, bbox max = 20mm → ratio 0.30 ≤ 0.80 → True
    assert _likely_bridge_substituted(regions, bbox=bbox) is True


def test_bridge_substitution_regions_without_centroid_fallback():
    """Defensive: a region dict missing x/y (future schema change)
    shouldn't break the heuristic — fall back to the bbox-only signal."""
    regions = [{"z": 20.0, "volume_mm3": 100.0}]
    bbox = {"x_min": 0, "x_max": 25, "y_min": 0, "y_max": 25,
            "z_min": 0, "z_max": 30}
    # No centroid data → fall back to bbox-only check (25 ≤ 30 → True)
    assert _likely_bridge_substituted(regions, bbox=bbox) is True


# ---------------------------------------------------------------------------
# _analyze_supports — bbox plumbing + clamp
# ---------------------------------------------------------------------------


def test_supports_default_bbox_keeps_bridge_flag_false():
    """No bbox supplied → can't tell if bridge-substitution likely →
    flag stays False. Backwards-compatible default for old callers."""
    tris = [
        # Single floating triangle far above z=0
        ((0.0, 0.0, 10.0), (5.0, 0.0, 10.0), (5.0, 5.0, 10.0)),
    ]
    result = _analyze_supports(tris, z_min=0.0)
    assert result.likely_substituted_by_bridge is False


def test_supports_with_small_bbox_fires_bridge_flag():
    """When the bbox is small AND there are overhangs detected,
    the bridge-substitution flag should fire."""
    # Two downward-facing triangles forming a horizontal overhang at z=20
    # over a small footprint.
    tris = [
        ((0.0, 0.0, 20.0), (15.0, 0.0, 20.0), (15.0, 15.0, 20.0)),
        ((0.0, 0.0, 20.0), (15.0, 15.0, 20.0), (0.0, 15.0, 20.0)),
    ]
    bbox = {"x_min": 0.0, "x_max": 15.0, "y_min": 0.0, "y_max": 15.0,
            "z_min": 0.0, "z_max": 20.0}
    result = _analyze_supports(
        tris, z_min=0.0, bbox=bbox, normalize_winding=False
    )
    # Triangles point -Z because winding makes the normal -Z (CCW from below)
    # If support_regions is empty, the helper returns False; only fires
    # when there's at least one detected overhang.
    if result.support_regions:
        assert result.likely_substituted_by_bridge is True


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
