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

The first fix swapped z_offset from ``-depth_mm`` to ``+extrude_height``,
which landed the prism inside the material but 0.1mm too far in — see
:func:`test_deboss_on_bottom_face_places_prism_inside_material`.  The
current behaviour is ``z_offset = +depth_mm``, the exact mirror of the top
face: depth into the body, 0.1mm of overlap left proud of the surface.

Every test here except the last reads the generated SCAD text.  That is
useful for pinning the arithmetic, and it is also how two depth bugs
survived in this code — so
:func:`test_every_cardinal_face_carves_the_depth_requested` compiles real
geometry and measures the cut instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiln.emboss_generator import generate_emboss_scad


def _openscad_available() -> bool:
    try:
        subprocess.run(["openscad", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


needs_openscad = pytest.mark.skipif(
    not _openscad_available(), reason="OpenSCAD required for real text compiles"
)


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


@needs_openscad
def test_deboss_on_bottom_face_places_prism_inside_material(tmp_path: Path) -> None:
    """The translate Z on a bottom-face deboss should push the prism UP so it
    overlaps with the body material above the face — by exactly the requested
    depth, no more.

    Two bugs have lived on this line.  First, z_offset = -depth_mm placed the
    prism at Z=[-1.7, -0.8] for a tray with cz=0, entirely outside the tray,
    so the subtraction was a silent no-op.  The fix for that overshot to
    z_offset = extrude_height (depth + 0.1), which put the prism at Z=[0, 0.9]
    — 0.9mm of penetration for 0.8mm requested, and the 0.1mm overlap that
    exists to keep the prism from ending exactly ON the surface ended up
    exactly on it.  This assertion previously froze that second bug in place
    while this docstring described the correct behaviour.

    Correct: z_offset = depth_mm, giving Z=[-0.1, 0.8] — 0.8mm into the
    material and 0.1mm proud of the surface, the mirror of the top face.
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

    # - translate Z should equal +depth_mm (0.8)
    # - rotate([180, 0, 0]) should be present for bottom face normal
    assert "rotate([180, 0, 0])" in scad, f"Expected bottom-face rotation:\n{scad}"
    # Translate line contains Z=0.800000 (= depth_mm)
    assert "0.800000])" in scad, (
        f"Expected translate Z == +0.8 (depth_mm) for bottom deboss; got:\n{scad}"
    )
    # The two historical bugs: prism outside the body, and prism pushed a
    # full extrude_height in (0.1mm too deep, zero overlap left proud).
    assert "-0.800000])" not in scad, (
        f"Buggy translate Z=-0.8 should no longer be emitted:\n{scad}"
    )
    assert "0.900000])" not in scad, (
        f"Buggy translate Z=+0.9 (extrude_height) cuts 0.1mm too deep:\n{scad}"
    )


@needs_openscad
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


@needs_openscad
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


@needs_openscad
def test_every_cardinal_face_carves_the_depth_requested(tmp_path: Path) -> None:
    """Measure the cut, do not read the SCAD.

    Every test guarding this code path asserted on generated TEXT, which is
    why two depth bugs survived in it for months: a bottom-face deboss cut
    1.3mm for 1.2mm requested, and a left/right deboss carved exactly 1.0mm
    for ANY requested depth because the two side rotation clauses were
    swapped and the prism pointed into the body, trading the roles of the
    cut depth and the 1.0mm outward overshoot.

    The side-face case is the reason 1.2 is used here rather than the more
    obvious 1.0: at depth 1.0 the swapped prism is symmetric and the bug is
    invisible, which is exactly what the existing side-face test chose.
    """
    import subprocess

    import numpy as np
    import trimesh

    from kiln.decoration_helpers import emboss_text_lines_on_face

    body_scad = tmp_path / "body.scad"
    body_scad.write_text("cube([60,50,40], center=false);\n")
    body = tmp_path / "body.stl"
    subprocess.run(
        ["openscad", "-o", str(body), str(body_scad)],
        check=True,
        capture_output=True,
        timeout=180,
    )

    requested = 1.2
    # face name -> (axis, outer coordinate, inward sign)
    faces = {
        "top": (2, 40.0, -1),
        "bottom": (2, 0.0, 1),
        "left": (0, 0.0, 1),
        "right": (0, 60.0, -1),
    }
    for face, (axis, outer, inward) in faces.items():
        out = emboss_text_lines_on_face(
            str(body),
            ["AB"],
            face_name=face,
            mode="deboss",
            depth_mm=requested,
            output_dir=str(tmp_path / f"o_{face}"),
        )
        mesh = trimesh.load(out)
        # The body's own outer surfaces must not move — a "depth" measured
        # against a surface that shifted would be measuring the wrong thing.
        assert mesh.bounds[0].tolist() == pytest.approx(
            [0.0, 0.0, 0.0], abs=1e-6
        )
        assert mesh.bounds[1].tolist() == pytest.approx(
            [60.0, 50.0, 40.0], abs=1e-6
        )
        flat = np.abs(mesh.face_normals[:, axis]) > 0.999
        coords = mesh.triangles_center[flat][:, axis]
        recessed = coords[(coords - outer) * inward > 1e-6]
        assert len(recessed) > 0, f"{face}: no recessed floor — nothing carved"
        floor = recessed.min() if inward > 0 else recessed.max()
        measured = abs(floor - outer)
        assert measured == pytest.approx(requested, abs=0.02), (
            f"{face} face carved {measured:.4f}mm for {requested}mm requested"
        )
