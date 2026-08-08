"""Regression tests for ``kiln.decoration_helpers``.

Pins the 2026-05-03 fixes:

- :func:`fit_text_to_strip` returns ``fits=False`` when text would
  shrink below the FDM legibility floor.
- :func:`emboss_text_lines_on_face` chooses sizes that respect the
  hierarchy (primary > secondary > tertiary) — the bug class was
  "CEO ends up bigger than Josh Beckham" because the engine's
  width/height-coupled auto-sizer width-clamped the long line and
  height-clamped the short one.
- The same helper produces visible deboss cuts on non-cardinal faces
  (the wedge angled face) — the bug class was a 0.16mm face-centroid
  jitter on the second deboss pass that left the prism entirely
  inside the body, leaving the face surface untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# fit_text_to_strip
# ---------------------------------------------------------------------------


def test_fit_text_to_strip_short_text_uses_height_cap():
    """A 4-char string in a 24mm × 300mm strip is height-limited."""
    from kiln.decoration_helpers import fit_text_to_strip

    verdict = fit_text_to_strip("KILN", strip_width_mm=300.0, strip_height_mm=24.0)
    assert verdict["fits"] is True
    assert verdict["constraint"] == "height"
    # 24 × 0.85 = 20.4
    assert verdict["font_size_mm"] == pytest.approx(20.4, rel=0.01)


def test_fit_text_to_strip_long_text_uses_width_cap():
    """A 12-char string in a wide-and-short strip is width-limited."""
    from kiln.decoration_helpers import fit_text_to_strip

    verdict = fit_text_to_strip(
        "Josh Beckham", strip_width_mm=200.0, strip_height_mm=80.0,
    )
    assert verdict["fits"] is True
    assert verdict["constraint"] == "width"
    # 200 × 0.85 / (12 × 0.6) = 23.6
    assert verdict["font_size_mm"] == pytest.approx(23.6, rel=0.02)


def test_fit_text_to_strip_overlong_text_warns_below_floor():
    """120 chars on a 200mm strip drops below the 4mm legibility floor."""
    from kiln.decoration_helpers import fit_text_to_strip

    verdict = fit_text_to_strip(
        "A" * 120, strip_width_mm=200.0, strip_height_mm=15.0,
    )
    assert verdict["fits"] is False
    assert verdict["constraint"] == "min_floor"
    assert any("legibility floor" in w for w in verdict["warnings"])


# ---------------------------------------------------------------------------
# Hierarchy sizing in emboss_text_lines_on_face
# ---------------------------------------------------------------------------


def _has_openscad() -> bool:
    return bool(shutil.which("openscad")) or os.path.isfile(
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    )


_NEEDS_OPENSCAD = pytest.mark.skipif(
    not _has_openscad(), reason="openscad binary not available",
)


@_NEEDS_OPENSCAD
def test_emboss_text_lines_hierarchy_primary_larger_than_secondary(tmp_path):
    """Two-line layout: primary line ends up larger than secondary.

    Pre-fix bug: 'CEO' (3 chars) auto-sized to 50mm while 'Josh Beckham'
    (12 chars) auto-sized to 22mm because each line was independently
    fit to the face — width-clamping the long line, height-clamping the
    short one.
    """
    # Build a simple cube as the host
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([200, 50, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_lines_on_face

    out_dir = tmp_path / "decorated"
    final_stl = emboss_text_lines_on_face(
        str(body_stl),
        ["Josh Beckham", "CEO"],
        face_name="top",
        mode="deboss",
        depth_mm=1.2,  # at the 0.4mm-nozzle legibility floor
        line_scale=0.7,
        output_dir=str(out_dir),
    )
    assert os.path.isfile(final_stl)

    # Parse the per-line SCAD to verify the primary line's font_size
    # is larger than the secondary's.
    sizes = []
    for sf in sorted(out_dir.glob("*.scad")):
        content = sf.read_text()
        # Each emboss SCAD has a single `size=<number>` attribute.
        import re
        m = re.search(r"size=(?P<n>[\d.]+)", content)
        if m:
            sizes.append(float(m.group("n")))
    assert len(sizes) == 2, f"Expected 2 SCADs, got {len(sizes)}: {sizes}"
    primary, secondary = sizes
    assert primary > secondary, (
        f"Primary 'Josh Beckham' ({primary:.1f}mm) should exceed secondary "
        f"'CEO' ({secondary:.1f}mm) — hierarchy inverted."
    )


# ---------------------------------------------------------------------------
# Non-cardinal face deboss — pinning the wedge fix
# ---------------------------------------------------------------------------


@_NEEDS_OPENSCAD
def test_deboss_on_non_cardinal_face_actually_cuts(tmp_path):
    """Chained deboss on the wedge's angled face produces visible cuts.

    Pre-fix bug: the engine used world-Z shift (z_offset = -depth_mm)
    for non-cardinal faces, leaving the prism entirely inside the body
    — the difference() was a silent no-op.
    """
    # Build a wedge: angled face from front-bottom (y=0,z=0) to
    # back-top (y=50,z=60), width 200.
    body_scad = tmp_path / "wedge.scad"
    body_scad.write_text(
        "$fn=80;\n"
        "rotate([90,0,90])\n"
        "  linear_extrude(height=200)\n"
        "    polygon(points=[[0,0],[50,0],[50,60]]);\n"
    )
    body_stl = tmp_path / "wedge.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    final_stl = emboss_text_on_face(
        body_stl=str(body_stl),
        text="HELLO",
        mode="deboss",
        depth_mm=1.5,
        scale=0.5,
        output_dir=str(out_dir),
    )
    assert os.path.isfile(final_stl)

    # The decorated STL must be larger than the bare wedge — the
    # deboss adds triangles for the engraved letters.  If the deboss
    # silently no-oped, the file size would be identical to the bare.
    bare_size = body_stl.stat().st_size
    decorated_size = Path(final_stl).stat().st_size
    assert decorated_size > bare_size * 1.5, (
        f"Decorated STL ({decorated_size}B) is suspiciously close to "
        f"bare ({bare_size}B) — deboss may be a silent no-op."
    )


@_NEEDS_OPENSCAD
def test_emboss_text_on_face_emits_inner_translate_for_tilted_face(tmp_path):
    """Generated SCAD includes the local-frame inner translate clause.

    Pre-fix bug: only the outer (world-frame) translate was emitted,
    so offsets and depth-shift used world axes — wrong for any face
    whose normal isn't axis-aligned.
    """
    # Wedge with non-cardinal face
    body_scad = tmp_path / "wedge.scad"
    body_scad.write_text(
        "$fn=80;\n"
        "rotate([90,0,90])\n"
        "  linear_extrude(height=200)\n"
        "    polygon(points=[[0,0],[50,0],[50,60]]);\n"
    )
    body_stl = tmp_path / "wedge.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        body_stl=str(body_stl),
        text="X",
        mode="deboss",
        depth_mm=1.5,
        output_dir=str(out_dir),
    )
    # The generated SCAD should have TWO translate(...) clauses for
    # non-cardinal deboss: outer to face center, inner for offsets +
    # depth-shift in face-local frame.
    scad_files = list(out_dir.glob("*.scad"))
    assert scad_files, "Engine produced no SCAD"
    content = scad_files[0].read_text()
    translate_count = content.count("translate(")
    assert translate_count >= 2, (
        f"Expected ≥2 translate() clauses for non-cardinal deboss, "
        f"got {translate_count} — local-frame path may have regressed.\n"
        f"SCAD:\n{content}"
    )


# ---------------------------------------------------------------------------
# Depth legibility floor — printer-nozzle-aware enforcement.
# ---------------------------------------------------------------------------


def _make_cube_stl(stl_path: Path, *, side_mm: float) -> Path:
    """Build a simple cube STL via OpenSCAD as a test fixture host.

    Used by the depth-floor tests below — needs a real STL with a
    detectable "top" face but doesn't care about geometry beyond that.
    """
    scad = stl_path.with_suffix(".scad")
    scad.write_text(f"$fn=80;\ncube([{side_mm}, {side_mm}, 10]);\n")
    subprocess.run(
        ["openscad", "-o", str(stl_path), str(scad)],
        check=True, capture_output=True,
    )
    return stl_path


def test_depth_legibility_floor_default_nozzle_matches_a1_empirical():
    """Default 0.4mm nozzle floor is 1.2mm (matches the empirical
    floor for the Bambu A1)."""
    from kiln.decoration_helpers import _depth_legibility_floor_mm

    assert _depth_legibility_floor_mm(0.4) == pytest.approx(1.2)


def test_depth_legibility_floor_scales_with_nozzle_diameter():
    """3x rule: a 0.6mm Prusa MK4 nozzle bumps the floor to 1.8mm,
    a 0.25mm precision nozzle drops it to 0.75mm."""
    from kiln.decoration_helpers import _depth_legibility_floor_mm

    assert _depth_legibility_floor_mm(0.6) == pytest.approx(1.8)
    assert _depth_legibility_floor_mm(0.25) == pytest.approx(0.75)
    assert _depth_legibility_floor_mm(0.8) == pytest.approx(2.4)


@_NEEDS_OPENSCAD
def test_emboss_below_floor_raises_with_actionable_payload(tmp_path):
    """Sub-floor depth raises DepthBelowLegibilityFloor carrying the
    requested + floor + nozzle so the caller can hand the user a
    specific fix."""
    from kiln.decoration_helpers import (
        DepthBelowLegibilityFloor,
        emboss_text_on_face,
    )

    # Real body STL — a 50mm cube via OpenSCAD avoids fixture coupling.
    body_stl = _make_cube_stl(tmp_path / "cube.stl", side_mm=50.0)
    out_dir = tmp_path / "decorated"

    with pytest.raises(DepthBelowLegibilityFloor) as excinfo:
        emboss_text_on_face(
            str(body_stl),
            "TINY",
            face_name="top",
            mode="deboss",
            depth_mm=0.5,  # below the 1.2mm floor on a 0.4mm nozzle
            nozzle_diameter_mm=0.4,
            output_dir=str(out_dir),
        )

    err = excinfo.value
    assert err.requested_mm == 0.5
    assert err.floor_mm == pytest.approx(1.2)
    assert err.nozzle_diameter_mm == 0.4


@_NEEDS_OPENSCAD
def test_emboss_below_floor_on_larger_nozzle_raises(tmp_path):
    """1.2mm depth is fine on a 0.4mm nozzle but BELOW the 1.8mm floor
    on a 0.6mm Prusa MK4 nozzle — caller passing the wrong nozzle
    diameter mustn't ship a smeared print."""
    from kiln.decoration_helpers import (
        DepthBelowLegibilityFloor,
        emboss_text_on_face,
    )

    body_stl = _make_cube_stl(tmp_path / "cube.stl", side_mm=50.0)
    out_dir = tmp_path / "decorated"

    with pytest.raises(DepthBelowLegibilityFloor):
        emboss_text_on_face(
            str(body_stl),
            "OK_ON_A1",
            face_name="top",
            mode="deboss",
            depth_mm=1.2,
            nozzle_diameter_mm=0.6,  # bumps floor to 1.8
            output_dir=str(out_dir),
        )


@_NEEDS_OPENSCAD
def test_emboss_depth_none_uses_floor_silently(tmp_path):
    """``depth_mm=None`` is the most common caller intent ("make it
    just legible") — engine picks the printer-specific floor without
    raising."""
    from kiln.decoration_helpers import emboss_text_on_face

    body_stl = _make_cube_stl(tmp_path / "cube.stl", side_mm=50.0)
    out_dir = tmp_path / "decorated"

    final_stl = emboss_text_on_face(
        str(body_stl),
        "OK",
        face_name="top",
        mode="deboss",
        depth_mm=None,
        nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )
    assert os.path.isfile(final_stl)
    # The generated SCAD's linear_extrude(height=...) should reflect
    # at least the floor depth (1.2mm), not the prior 0.8mm legacy
    # default.  Engine may add a small (≤0.2mm) buffer above the depth
    # for clean deboss cut intersections — assert ≥ floor and ≤ floor
    # + tolerance.
    scad = next(iter(out_dir.glob("*.scad"))).read_text()
    import re
    m = re.search(r"linear_extrude\s*\(\s*height\s*=\s*([\d.]+)", scad)
    assert m, f"Could not find linear_extrude height in SCAD: {scad}"
    height = float(m.group(1))
    assert 1.2 <= height <= 1.4, (
        f"Expected height in [1.2, 1.4] (floor + buffer), got {height}"
    )


@_NEEDS_OPENSCAD
def test_emboss_text_lines_below_floor_raises(tmp_path):
    """Multi-line path enforces the same floor — the floor check
    happens once at the top, not re-checked per line."""
    from kiln.decoration_helpers import (
        DepthBelowLegibilityFloor,
        emboss_text_lines_on_face,
    )

    body_stl = _make_cube_stl(tmp_path / "cube.stl", side_mm=50.0)
    out_dir = tmp_path / "decorated"

    with pytest.raises(DepthBelowLegibilityFloor):
        emboss_text_lines_on_face(
            str(body_stl),
            ["NAME", "TITLE"],
            face_name="top",
            mode="deboss",
            depth_mm=0.8,  # below 1.2mm floor
            nozzle_diameter_mm=0.4,
            output_dir=str(out_dir),
        )


# ---------------------------------------------------------------------------
# Aspect-ratio-aware flip-axis selection for bottom-face engravings.
# ---------------------------------------------------------------------------


def test_flip_selection_wide_face_picks_rotate():
    """A 130×80 face (soap dish / coaster) gets rotate([180,0,0]) and
    axis="x" — the natural flip is around the long X axis."""
    from kiln.decoration_helpers import select_bottom_face_flip

    verdict = select_bottom_face_flip(face_width_mm=130, face_height_mm=80)
    assert verdict["transformation"] == "rotate([180, 0, 0])"
    assert verdict["flip_axis"] == "x"
    assert verdict["confidence"] == "high"
    assert verdict["self_inspection"]["passed"] is True


def test_flip_selection_tall_face_picks_mirror():
    """A 60×120 face (bookmark / pet-tag-on-vertical-strap) gets
    mirror([1,0,0]) and axis="y" — natural flip is around long Y axis."""
    from kiln.decoration_helpers import select_bottom_face_flip

    verdict = select_bottom_face_flip(face_width_mm=60, face_height_mm=120)
    assert verdict["transformation"] == "mirror([1, 0, 0])"
    assert verdict["flip_axis"] == "y"
    assert verdict["confidence"] == "high"
    assert verdict["self_inspection"]["passed"] is True


def test_flip_selection_square_face_defaults_to_rotate():
    """A square-ish face (aspect ratio < 1.2) defaults to rotate —
    handedness preservation for script fonts / asymmetric logos.
    Confidence drops to medium because either transformation would
    read correctly."""
    from kiln.decoration_helpers import select_bottom_face_flip

    verdict = select_bottom_face_flip(face_width_mm=80, face_height_mm=85)
    assert verdict["transformation"] == "rotate([180, 0, 0])"
    assert verdict["flip_axis"] == "x"
    assert verdict["confidence"] == "medium"


def test_flip_selection_rationale_carries_face_dimensions():
    """The rationale string surfaces the face dimensions so callers
    can include them in agent logs without re-deriving."""
    from kiln.decoration_helpers import select_bottom_face_flip

    verdict = select_bottom_face_flip(face_width_mm=130, face_height_mm=80)
    assert "130" in verdict["rationale"]
    assert "80" in verdict["rationale"]
    assert "X axis" in verdict["rationale"]


def test_self_inspect_rotate_with_x_flip_passes():
    """rotate([180,0,0]) pre-transform + user X-axis physical flip
    leaves text in left-to-right reading order."""
    from kiln.decoration_helpers import _self_inspect_flip_orientation

    result = _self_inspect_flip_orientation("rotate([180, 0, 0])", "x")
    assert result["passed"] is True


def test_self_inspect_mirror_with_y_flip_passes():
    """mirror([1,0,0]) pre-transform + user Y-axis physical flip
    leaves text in left-to-right reading order."""
    from kiln.decoration_helpers import _self_inspect_flip_orientation

    result = _self_inspect_flip_orientation("mirror([1, 0, 0])", "y")
    assert result["passed"] is True


def test_self_inspect_rotate_with_y_flip_fails():
    """rotate pre-transform + Y-axis user flip = text appears reversed.
    Self-inspection catches the mismatch."""
    from kiln.decoration_helpers import _self_inspect_flip_orientation

    result = _self_inspect_flip_orientation("rotate([180, 0, 0])", "y")
    assert result["passed"] is False
    assert "reversed" in result["detail"]


def test_self_inspect_mirror_with_x_flip_fails():
    """mirror pre-transform + X-axis user flip = text appears reversed.
    This is the exact failure mode the swarm refactor inadvertently
    fixed — sub-face inspections for soap_dish / jewelry_tray / ashtray
    all sit in this category before the refactor."""
    from kiln.decoration_helpers import _self_inspect_flip_orientation

    result = _self_inspect_flip_orientation("mirror([1, 0, 0])", "x")
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# Mandatory post-flip preview rendering for bottom-face engravings.
# ---------------------------------------------------------------------------


@_NEEDS_OPENSCAD
def test_bottom_face_emboss_emits_post_flip_preview(tmp_path):
    """Single-line emboss on face_name='bottom' emits flip_preview.png."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 60, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="bottom", mode="deboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    flip_png = out_dir / "flip_preview.png"
    assert flip_png.is_file(), "post-flip preview PNG must be emitted"
    assert flip_png.stat().st_size > 0, "post-flip preview must be non-empty"


@_NEEDS_OPENSCAD
def test_top_face_emboss_does_not_emit_post_flip_preview(tmp_path):
    """face_name='top' is a print-orientation face — no flip needed,
    so no post-flip preview should be emitted (the standard
    inspection-bundle preview already covers it)."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 60, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="top", mode="emboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    flip_png = out_dir / "flip_preview.png"
    assert not flip_png.is_file(), (
        "post-flip preview must NOT be emitted for top-face engravings"
    )


@_NEEDS_OPENSCAD
def test_multiline_bottom_face_emits_single_post_flip_preview(tmp_path):
    """Multi-line emboss renders ONE post-flip preview (against the
    final cumulative STL) — not one per line."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 60, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_lines_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_lines_on_face(
        str(body_stl), ["KILN", "EST 2026"],
        face_name="bottom", mode="deboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    # Exactly one flip_preview.png — no per-line versions.
    flip_png = out_dir / "flip_preview.png"
    assert flip_png.is_file()
    extra_flips = list(out_dir.glob("flip_preview*.png"))
    assert len(extra_flips) == 1, (
        f"expected one flip preview, got {[p.name for p in extra_flips]}"
    )


# ---------------------------------------------------------------------------
# Min-edge-margin clamp — text doesn't kiss the sides of small faces.
# ---------------------------------------------------------------------------


@_NEEDS_OPENSCAD
def test_default_emboss_leaves_visible_edge_margin_on_100mm_face(tmp_path):
    """On a 100mm-wide face, default scale=0.7 produces text occupying
    ≤ 70mm width — leaves ≥ 15mm padding per side.  Tighter defaults
    (e.g. 0.85) make characters hug the edge and visually appear to
    "almost fall off" — addressed in the 2026-05-26 margin-default
    correction."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 60, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="top", mode="emboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    # Auto-sizing happens inside generate_emboss_scad and is recorded
    # in the generated SCAD's text(size=...) attribute.  Verify the
    # chosen font size, multiplied by the char-width factor (0.6 per
    # the engine's convention), leaves ≥ 12mm padding per side.
    scad_files = list(out_dir.glob("*.scad"))
    assert scad_files, "no SCAD was generated"
    import re
    sizes = []
    for sf in scad_files:
        m = re.search(r"size=(?P<n>[\d.]+)", sf.read_text())
        if m:
            sizes.append(float(m.group("n")))
    font_size = max(sizes)
    text_width_mm = len("KILN") * font_size * 0.6
    padding_per_side_mm = (100 - text_width_mm) / 2
    assert padding_per_side_mm >= 12.0, (
        f"text width {text_width_mm:.1f}mm leaves only "
        f"{padding_per_side_mm:.1f}mm per side on a 100mm face — "
        f"should be ≥ 12mm to avoid the 'almost falling off' look."
    )


@_NEEDS_OPENSCAD
def test_min_edge_margin_binds_on_small_face(tmp_path):
    """A 30mm pet-tag-sized face triggers the absolute min-edge-margin
    clamp: scale=0.7 alone would give 21mm wide text with 4.5mm
    padding per side, but the 4mm min-margin clamp pulls it to 22mm
    available width (30 - 8 = 22mm), text fills that with the engine's
    auto-sizer.  Verifies the clamp is at least 3mm per side (slightly
    less than the 4mm constant to allow for the 0.6 char-width
    approximation slack)."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([30, 20, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "ABCD",
        face_name="top", mode="emboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    scad_files = list(out_dir.glob("*.scad"))
    import re
    sizes = []
    for sf in scad_files:
        m = re.search(r"size=(?P<n>[\d.]+)", sf.read_text())
        if m:
            sizes.append(float(m.group("n")))
    font_size = max(sizes)
    text_width_mm = len("ABCD") * font_size * 0.6
    padding_per_side_mm = (30 - text_width_mm) / 2
    assert padding_per_side_mm >= 3.0, (
        f"on a 30mm face, padding {padding_per_side_mm:.1f}mm per side "
        f"is below the floor — min_edge_margin_mm clamp didn't bind."
    )


@_NEEDS_OPENSCAD
def test_zero_min_edge_margin_allows_hug_the_wall_text(tmp_path):
    """Callers who explicitly want edge-to-edge text (license-plate
    frame bands, etc.) override ``min_edge_margin_mm=0.0`` and get
    the proportional-only behavior."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 30, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.emboss_generator import compile_embossed_model, generate_emboss_scad
    from kiln.surface_intelligence import find_named_face

    face = find_named_face(str(body_stl), "top")
    out_dir = tmp_path / "decorated"
    out_dir.mkdir()
    result = generate_emboss_scad(
        model_path=str(body_stl),
        content_info={"type": "openscad_text", "text": "EDGE-TO-EDGE"},
        face=face,
        output_dir=str(out_dir),
        depth_mm=1.2,
        mode="emboss",
        scale=0.95,  # caller asked for tight fit
        min_edge_margin_mm=0.0,  # explicitly opt out of the clamp
    )
    # No clamp warning should have fired (the override means the user
    # accepted the consequences).
    assert not any(
        "edge margin" in w for w in result.get("warnings", [])
    ), f"unexpected margin warning: {result.get('warnings')}"


# ---------------------------------------------------------------------------
# SCAD-injection wiring — smart flip applied to actual engine output.
# ---------------------------------------------------------------------------


@_NEEDS_OPENSCAD
def test_wide_shallow_bottom_face_emits_no_extra_mirror(tmp_path):
    """Wide-shallow bottom face (100×60mm) → helper picks rotate;
    engine's default rotation handles it; no extra mirror in SCAD."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([100, 60, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="bottom", mode="deboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    # Filter out the flip_preview.scad helper — we want the emboss SCAD.
    scad_files = [
        p for p in out_dir.glob("*.scad")
        if not p.name.startswith("flip_preview")
    ]
    assert scad_files
    scad_content = scad_files[0].read_text()
    # Engine emits its standard rotate for face alignment.
    assert "rotate([180, 0, 0])" in scad_content
    # No supplemental mirror — wide-shallow uses the engine default.
    assert "mirror([1, 0, 0])" not in scad_content


@_NEEDS_OPENSCAD
def test_tall_narrow_bottom_face_emits_supplemental_mirror(tmp_path):
    """Tall-narrow bottom face (60×120mm) → helper picks mirror;
    engine emits rotate AND mirror together so text reads correctly
    after the user's Y-axis natural flip."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([60, 120, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="bottom", mode="deboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    scad_files = [
        p for p in out_dir.glob("*.scad")
        if not p.name.startswith("flip_preview")
    ]
    assert scad_files
    scad_content = scad_files[0].read_text()
    # Engine still emits face-aligning rotation.
    assert "rotate([180, 0, 0])" in scad_content
    # AND the supplemental mirror for the long-Y physical flip.
    assert "mirror([1, 0, 0])" in scad_content


@_NEEDS_OPENSCAD
def test_top_face_never_emits_supplemental_mirror(tmp_path):
    """face_name='top' is a print-orientation face — no flip-readable
    contract, no supplemental mirror.  Engine's standard pipeline
    runs unchanged."""
    body_scad = tmp_path / "body.scad"
    body_scad.write_text("$fn=80;\ncube([60, 120, 10]);\n")
    body_stl = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body_stl), str(body_scad)],
        check=True, capture_output=True,
    )

    from kiln.decoration_helpers import emboss_text_on_face

    out_dir = tmp_path / "decorated"
    emboss_text_on_face(
        str(body_stl), "KILN",
        face_name="top", mode="emboss",
        depth_mm=1.2, nozzle_diameter_mm=0.4,
        output_dir=str(out_dir),
    )

    scad_files = [
        p for p in out_dir.glob("*.scad")
        if not p.name.startswith("flip_preview")
    ]
    assert scad_files
    scad_content = scad_files[0].read_text()
    assert "mirror([1, 0, 0])" not in scad_content


# ---------------------------------------------------------------------------
# The text-sizing seam — closed 2026-08-08, measured regression corpus
#
# Before the fix the helper sized lines from a 0.6-per-char guess and the
# engine re-fit the real glyphs to its own unmargined box.  Measured on a
# 70x70 plate: intent 41.65mm of run width shipped as 49.00mm — exactly
# 1/0.85, the visual margin destroyed; ["WWWW", "IIIII"] shipped the
# secondary line LARGER than the primary (9.72mm vs 9.35mm font); and a
# monogram "W" on an 80mm disc shipped its corners 4.58mm past the rim
# with no warning anywhere.
# ---------------------------------------------------------------------------


def _stl_vertices(path):
    """Minimal binary/ASCII STL vertex reader (test-local, stdlib only)."""
    import struct

    with open(path, "rb") as f:
        data = f.read()
    if data[:5] == b"solid" and b"facet" in data[:2000]:
        verts = []
        for line in data.decode("latin1").splitlines():
            t = line.split()
            if t[:1] == ["vertex"]:
                verts.append(tuple(float(x) for x in t[1:4]))
        return verts
    n = struct.unpack("<I", data[80:84])[0]
    verts = []
    off = 84
    for _ in range(n):
        if off + 50 > len(data):
            break
        for k in range(3):
            verts.append(struct.unpack_from("<3f", data, off + 12 + k * 12))
        off += 50
    return verts


def _make_plate70(tmp_path):
    scad = tmp_path / "aplate.scad"
    scad.write_text("translate([0, 0, 2]) cube([70, 70, 4], center=true);")
    stl = tmp_path / "aplate.stl"
    subprocess.run(
        ["openscad", "-o", str(stl), str(scad)], check=True, capture_output=True,
    )
    return str(stl)


def _make_disc80(tmp_path):
    scad = tmp_path / "adisc.scad"
    scad.write_text("cylinder(h=6, d=80, $fn=160);")
    stl = tmp_path / "adisc.stl"
    subprocess.run(
        ["openscad", "-o", str(stl), str(scad)], check=True, capture_output=True,
    )
    return str(stl)


def _text_verts_above(stl_path, top_z):
    return [v for v in _stl_vertices(stl_path) if v[2] > top_z + 0.02]


@_NEEDS_OPENSCAD
def test_seam_requested_margin_ships_to_the_mesh(tmp_path):
    """Property 1: the helper's 0.85 visual margin survives to the mesh.

    Intent on a 70mm face at line_scale 0.7 is a run of 0.85 x 49 =
    41.65mm.  The pre-fix pipeline shipped 49.00mm — exactly 1/0.85
    wider, the margin silently destroyed by the engine's re-fit.
    """
    from kiln.decoration_helpers import emboss_text_lines_on_face

    plate = _make_plate70(tmp_path)
    out = tmp_path / "dec"
    final = emboss_text_lines_on_face(
        plate, ["KILN"], mode="emboss", output_dir=str(out),
    )
    tv = _text_verts_above(final, 4.0)
    assert tv, "no embossed text found above the face"
    xs = [v[0] for v in tv]
    run_w = max(xs) - min(xs)
    assert run_w == pytest.approx(41.65, abs=0.6), (
        f"shipped run {run_w:.2f}mm != intended 41.65mm "
        f"(49.0mm means the 1/0.85 inflation is back)"
    )


@_NEEDS_OPENSCAD
def test_seam_wide_narrow_hierarchy_never_inverts(tmp_path):
    """Property 2: ["WWWW", "IIIII"] — the measured inversion case.

    W really renders 1.31mm of run per font-mm (not 0.6), so the old
    estimate over-sized the primary, the engine clamped it below the
    honoured secondary, and the secondary shipped LARGER (9.72 vs
    9.35mm font; 9.29 vs 8.93mm caps).  Sizes must now come out in
    hierarchy order, in the SCAD and in the mesh.
    """
    import re

    from kiln.decoration_helpers import emboss_text_lines_on_face

    plate = _make_plate70(tmp_path)
    out = tmp_path / "dec"
    final = emboss_text_lines_on_face(
        plate, ["WWWW", "IIIII"], mode="emboss", output_dir=str(out),
    )
    # SCAD half: aplate_emboss.scad (primary) sorts before
    # line_0_emboss.scad (secondary).
    sizes = []
    for sf in sorted(out.glob("*.scad")):
        m = re.search(r"size=([\d.]+)", sf.read_text())
        if m:
            sizes.append(float(m.group(1)))
    assert len(sizes) == 2
    primary, secondary = sizes
    assert primary > secondary, (
        f"hierarchy inverted again: primary {primary}mm <= secondary {secondary}mm"
    )
    assert secondary / primary == pytest.approx(0.7, abs=0.02)

    # Mesh half: the top line's glyphs are taller than the bottom line's.
    tv = _text_verts_above(final, 4.0)
    top_line = [v for v in tv if v[1] > 0]
    bottom_line = [v for v in tv if v[1] < 0]
    assert top_line and bottom_line
    cap_top = max(v[1] for v in top_line) - min(v[1] for v in top_line)
    cap_bottom = max(v[1] for v in bottom_line) - min(v[1] for v in bottom_line)
    assert cap_top > cap_bottom, (
        f"secondary renders taller than primary ({cap_bottom:.2f} vs {cap_top:.2f}mm)"
    )


@_NEEDS_OPENSCAD
def test_seam_round_monogram_stays_inside_the_rim(tmp_path):
    """Property 3: no silent rim clip on a round face.

    The measured failure: a single "W" at line_scale 0.9 on an 80mm
    disc auto-filled the 72mm bbox and shipped its corners at radius
    44.58mm — 4.58mm past the rim, warnings=None.  Every text vertex
    must stay inside the rim now.
    """
    import math

    from kiln.decoration_helpers import emboss_text_lines_on_face

    disc = _make_disc80(tmp_path)
    out = tmp_path / "dec"
    final = emboss_text_lines_on_face(
        disc, ["W"], mode="emboss", output_dir=str(out),
        line_scale=0.9, min_edge_margin_mm=0.0,
    )
    tv = _text_verts_above(final, 6.0)
    assert tv, "no embossed text found above the face"
    max_r = max(math.hypot(v[0], v[1]) for v in tv)
    assert max_r <= 40.0 + 0.05, (
        f"glyph corner at radius {max_r:.2f}mm hangs past the 40mm rim"
    )


@_NEEDS_OPENSCAD
def test_seam_offset_band_survives_and_clears_the_rim(tmp_path):
    """Offsets clamp against the measured text, not the target box.

    Box-based clamping used to yank a +24mm band placement to +4mm on
    an 80mm disc (the box was 72mm tall), silently relocating the
    engraving to the middle of the face.
    """
    import math

    from kiln.decoration_helpers import emboss_text_on_face

    disc = _make_disc80(tmp_path)
    out = tmp_path / "dec"
    out.mkdir()
    final = emboss_text_on_face(
        disc, "WWWWWW", mode="emboss", offset_y_mm=24.0,
        scale=0.9, min_edge_margin_mm=0.0,
        output_dir=str(out), output_stl=str(out / "banded.stl"),
    )
    tv = _text_verts_above(final, 6.0)
    assert tv
    ys = [v[1] for v in tv]
    y_center = (max(ys) + min(ys)) / 2.0
    assert y_center == pytest.approx(24.0, abs=1.0), (
        f"band placement moved: text centered at y={y_center:.1f}, wanted 24.0"
    )
    max_r = max(math.hypot(v[0], v[1]) for v in tv)
    assert max_r <= 40.0 + 0.05


@_NEEDS_OPENSCAD
def test_seam_collect_warnings_carries_engine_clamps(tmp_path):
    """Nothing the engine decides is silent: clamp warnings reach the sink.

    The helper used to drop the engine's warnings list on the floor —
    an offset clamp or a size re-fit happened and the caller saw only a
    bare STL path.
    """
    from kiln.decoration_helpers import emboss_text_on_face

    plate = _make_plate70(tmp_path)
    out = tmp_path / "dec"
    out.mkdir()
    sink = []
    emboss_text_on_face(
        plate, "KILN", mode="emboss", font_size_mm=40.0,
        output_dir=str(out), output_stl=str(out / "clamped.stl"),
        collect_warnings=sink,
    )
    assert any("clamped" in w for w in sink), sink


# ---------------------------------------------------------------------------
# compute_text_line_layout — the sizing math, hermetic (no OpenSCAD)
# ---------------------------------------------------------------------------


def _fake_metrics(monkeypatch, table):
    """Route measure_text_block_mm through a per-text (w/mm, h/mm) table."""
    import kiln.emboss_generator as eg

    def fake(text, font="Liberation Sans:style=Bold", font_size=48.0):
        w1, h1 = table[text]
        return w1 * font_size, h1 * font_size, 0.0, 0.0

    monkeypatch.setattr(eg, "measure_text_block_mm", fake)


def _rect_face(w=70.0, h=70.0):
    return {"width_mm": w, "height_mm": h, "area_mm2": w * h,
            "normal": (0, 0, 1), "center": (0, 0, 4), "face_name": "top"}


def _disc_face(d=80.0):
    import math

    return {"width_mm": d, "height_mm": d,
            "area_mm2": math.pi / 4.0 * d * d,
            "normal": (0, 0, 1), "center": (0, 0, 6), "face_name": "top"}


def test_layout_hierarchy_ratios_survive_measured_fit(monkeypatch):
    from kiln.decoration_helpers import compute_text_line_layout

    _fake_metrics(monkeypatch, {"WWWW": (5.24, 0.96), "IIIII": (1.74, 0.96)})
    layout = compute_text_line_layout(["WWWW", "IIIII"], face=_rect_face())
    s0, s1 = layout["font_sizes_mm"]
    assert s1 / s0 == pytest.approx(0.7, abs=1e-9)
    # The widest line lands exactly on the 0.85 margin of the usable box.
    assert max(layout["line_widths_mm"]) == pytest.approx(
        0.85 * 70.0 * 0.7, abs=1e-6,
    )
    assert layout["measured"] is True


def test_layout_falls_back_to_estimate_without_probe(monkeypatch):
    import kiln.emboss_generator as eg
    from kiln.decoration_helpers import compute_text_line_layout

    def broken(*a, **k):
        raise eg.TextMeasureError("no binary")

    monkeypatch.setattr(eg, "measure_text_block_mm", broken)
    layout = compute_text_line_layout(["KILN"], face=_rect_face())
    assert layout["measured"] is False
    assert any("estimate" in n for n in layout["notes"])
    # Legacy arithmetic: (70*0.7*0.85) / (4*0.6) = 17.35
    assert layout["font_sizes_mm"][0] == pytest.approx(17.35, abs=0.01)


def test_layout_round_face_shrinks_all_lines_by_one_factor(monkeypatch):
    from kiln.decoration_helpers import compute_text_line_layout

    _fake_metrics(monkeypatch, {"W": (1.31, 0.96)})
    layout = compute_text_line_layout(
        ["W"], face=_disc_face(), line_scale=1.0, min_edge_margin_mm=0.0,
    )
    assert any("rim" in n for n in layout["notes"])
    # Corner of the shrunk run sits inside the 40mm rim (with cushion).
    import math

    half_w = layout["line_widths_mm"][0] / 2.0
    half_h = layout["line_heights_mm"][0] / 2.0
    assert math.hypot(half_w, half_h) <= 40.0

    _fake_metrics(monkeypatch, {"W": (1.31, 0.96), "II": (0.6, 0.96)})
    two = compute_text_line_layout(
        ["W", "II"], face=_disc_face(), line_scale=1.0, min_edge_margin_mm=0.0,
    )
    s0, s1 = two["font_sizes_mm"]
    assert s1 / s0 == pytest.approx(0.7, abs=1e-9)  # ratios survive the shrink


def test_layout_floor_refusal_uses_real_widths(monkeypatch):
    from kiln.decoration_helpers import (
        TextDoesNotFitError,
        compute_text_line_layout,
    )

    # 20 wide glyphs on a 40mm face: measured width forces the size far
    # below the 4mm floor.  The old estimate-based check could approve a
    # size the engine then silently clamped below the floor.
    _fake_metrics(monkeypatch, {"W" * 20: (26.2, 0.96)})
    with pytest.raises(TextDoesNotFitError) as exc:
        compute_text_line_layout(["W" * 20], face=_rect_face(40.0, 40.0))
    assert exc.value.verdict["constraint"] == "min_floor"
    assert exc.value.verdict["suggestions"]
