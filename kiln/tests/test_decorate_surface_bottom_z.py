"""Regression tests for decorate_surface Bug 5 — face='bottom' Z overlap.

Prior to this fix, calling ``generate_emboss_scad(..., mode='deboss')`` on a
bottom-facing face (normal.z < -0.9) produced SCAD that placed the text
prism entirely BELOW the model:

    rotate([180, 0, 0])         # prism now Z=[-h, 0]
    translate([tx, ty, cz-depth_mm])  # shifts DOWN again

For a tray with bottom-face center at Z=0, depth_mm=0.8, extrude_height=0.9,
this landed the prism at Z=[-1.7, -0.8] — zero overlap with tray Z=[0, 18] —
and ``difference()`` silently removed nothing.  The bottom of the tray came
out completely flat with no engraving.

Fix (emboss_generator.py): for faces with normal.z < -0.9, swap z_offset
from ``-depth_mm`` to ``+extrude_height`` so the flipped prism lands inside
the material instead of further outside it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kiln.emboss_generator import generate_emboss_scad


def _make_bottom_face(center_z: float = 0.0, w: float = 160.0, h: float = 160.0) -> dict:
    """Fabricate a face descriptor for a tray's exterior bottom."""
    return {
        "face_name": "bottom",
        "center": [0.0, 0.0, center_z],
        "normal": [0.0, 0.0, -1.0],
        "width_mm": w,
        "height_mm": h,
        "area_mm2": w * h,
    }


def _make_top_face(center_z: float = 7.0, w: float = 90.0, h: float = 90.0) -> dict:
    """Fabricate a face descriptor for a coaster's top."""
    return {
        "face_name": "top",
        "center": [0.0, 0.0, center_z],
        "normal": [0.0, 0.0, 1.0],
        "width_mm": w,
        "height_mm": h,
        "area_mm2": w * h,
    }


def test_deboss_on_bottom_face_places_prism_inside_material(tmp_path: Path) -> None:
    """The translate Z on a bottom-face deboss should push the prism UP so it
    overlaps with the body material above the face.

    Prior bug: z_offset = -depth_mm placed prism at Z=[-1.7, -0.8] for a tray
    with cz=0, outside the tray entirely.  Post-fix: z_offset = extrude_height
    places prism at Z=[0, 0.9], penetrating 0.8mm into the tray floor.
    """
    # Create a minimal dummy STL so import() in the generated SCAD has a target.
    dummy_stl = tmp_path / "dummy.stl"
    dummy_stl.write_bytes(b"solid dummy\nendsolid dummy\n")

    content_info = {
        "type": "openscad_text",
        "text": "Hello",
        "font": "Liberation Sans",
    }
    face = _make_bottom_face()

    result = generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info=content_info,
        face=face,
        output_dir=str(tmp_path),
        scale=0.7,
        depth_mm=0.8,
        mode="deboss",
    )

    scad = Path(result["scad_path"]).read_text()

    # Post-fix expectations:
    # - translate Z should equal +extrude_height (0.9), NOT -depth_mm (-0.8)
    # - rotate([180, 0, 0]) should be present for bottom face normal
    assert "rotate([180, 0, 0])" in scad, f"Expected bottom-face rotation:\n{scad}"
    # Translate line contains Z=0.900000 (= extrude_height = depth + 0.1)
    assert "0.900000])" in scad, (
        f"Expected translate Z == +0.9 (extrude_height) for bottom deboss; got:\n{scad}"
    )
    # Old buggy Z of -0.8 must not appear in the translate
    assert "-0.800000])" not in scad, (
        f"Buggy translate Z=-0.8 should no longer be emitted:\n{scad}"
    )


def test_deboss_on_top_face_still_works(tmp_path: Path) -> None:
    """Top-face deboss behavior must not regress — it was already correct."""
    dummy_stl = tmp_path / "dummy.stl"
    dummy_stl.write_bytes(b"solid dummy\nendsolid dummy\n")

    content_info = {
        "type": "openscad_text",
        "text": "Hi",
        "font": "Liberation Sans",
    }
    face = _make_top_face(center_z=7.0)

    result = generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info=content_info,
        face=face,
        output_dir=str(tmp_path),
        scale=0.7,
        depth_mm=0.8,
        mode="deboss",
    )

    scad = Path(result["scad_path"]).read_text()

    # Top face: no rotate needed (normal already +Z)
    assert "rotate([180, 0, 0])" not in scad
    # Translate Z should be cz - depth_mm = 7 - 0.8 = 6.2
    assert "6.200000])" in scad, (
        f"Expected translate Z == cz - depth_mm (6.2) for top deboss; got:\n{scad}"
    )


def test_emboss_on_bottom_face_protrudes_outward(tmp_path: Path) -> None:
    """Emboss on a bottom face should protrude AWAY from material (below the
    tray) — z_offset should be 0 so the prism sits at Z=[-h, 0] outside body.
    """
    dummy_stl = tmp_path / "dummy.stl"
    dummy_stl.write_bytes(b"solid dummy\nendsolid dummy\n")

    content_info = {
        "type": "openscad_text",
        "text": "Hi",
        "font": "Liberation Sans",
    }
    face = _make_bottom_face()

    result = generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info=content_info,
        face=face,
        output_dir=str(tmp_path),
        scale=0.7,
        depth_mm=0.8,
        mode="emboss",
    )

    scad = Path(result["scad_path"]).read_text()

    assert "rotate([180, 0, 0])" in scad
    assert "0.000000])" in scad, (
        f"Expected emboss translate Z == cz+0 for bottom; got:\n{scad}"
    )
