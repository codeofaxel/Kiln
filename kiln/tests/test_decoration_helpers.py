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
    """Default 0.4mm nozzle floor is 1.2mm (matches kiln-pro CLAUDE.md
    empirical floor for the Bambu A1)."""
    from kiln.decoration_helpers import _depth_legibility_floor_mm

    assert _depth_legibility_floor_mm(0.4) == pytest.approx(1.2)


def test_depth_legibility_floor_scales_with_nozzle_diameter():
    """3x rule: a 0.6mm Prusa MK4 nozzle bumps the floor to 1.8mm,
    a 0.25mm precision nozzle drops it to 0.75mm."""
    from kiln.decoration_helpers import _depth_legibility_floor_mm

    assert _depth_legibility_floor_mm(0.6) == pytest.approx(1.8)
    assert _depth_legibility_floor_mm(0.25) == pytest.approx(0.75)
    assert _depth_legibility_floor_mm(0.8) == pytest.approx(2.4)


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
