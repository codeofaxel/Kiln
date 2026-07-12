"""Regression: a text/SVG deboss must remove material on EVERY cardinal
face — not just the flat top/bottom.

The deboss prism has to penetrate the body along the FACE NORMAL.  On the
flat faces (top/bottom) the normal is ±world-Z, so a world-Z shift on the
outer translate does that.  On the side faces (front/back/left/right)
world-Z lies IN the face plane, so a world-Z shift slides the prism
sideways and it never enters the body — ``difference()`` removes nothing
and the deboss silently no-ops.  (Measured 2026-07-10: "KILN" on a
120x80x50 front face carved 0 vertices; back the same.  left/right carved
but 1mm vertically misplaced by the same useless shift.)  The engine now
shifts side-face debosses along the face normal via the post-rotation
inner translate, exactly like arbitrary (non-cardinal) normals.

This proves it the only honest way for face-frame geometry: carve through
the REAL pipeline (``generate_emboss_scad`` -> OpenSCAD compile) and read
the output STL back, confirming a carve floor exists one depth below the
surface, INSIDE the body, on all six faces.  A regression to a world-Z
shift re-breaks front/back and this goes red.
"""
from __future__ import annotations

import os

import pytest

try:
    from kiln.emboss_generator import (
        _find_openscad,
        compile_embossed_model,
        generate_emboss_scad,
    )
    from kiln.surface_intelligence import _parse_stl, find_named_face

    _KILN_REAL = callable(compile_embossed_model)
except Exception:  # pragma: no cover — kiln stubbed by another suite
    _KILN_REAL = False

if _KILN_REAL:
    try:
        _find_openscad()
        _OPENSCAD = True
    except Exception:
        _OPENSCAD = False
else:
    _OPENSCAD = False

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _KILN_REAL, reason="real kiln emboss engine unavailable"),
    pytest.mark.skipif(not _OPENSCAD, reason="OpenSCAD not installed"),
]

_BOX = (120.0, 80.0, 50.0)
_DEPTH = 1.0

# face -> (normal axis index, outward sign, in-plane coord of the surface)
_FACE_GEOM = {
    "top": (2, +1, 50.0),
    "bottom": (2, -1, 0.0),
    "front": (1, -1, 0.0),
    "back": (1, +1, 80.0),
    "left": (0, -1, 0.0),
    "right": (0, +1, 120.0),
}


def _write_box_stl(path: str, sx: float, sy: float, sz: float) -> None:
    v = [(x, y, z) for z in (0, sz) for y in (0, sy) for x in (0, sx)]
    quads = [
        ((0, 0, -1), [0, 2, 3, 1]),
        ((0, 0, 1), [4, 5, 7, 6]),
        ((0, -1, 0), [0, 1, 5, 4]),
        ((0, 1, 0), [2, 6, 7, 3]),
        ((-1, 0, 0), [0, 4, 6, 2]),
        ((1, 0, 0), [1, 3, 7, 5]),
    ]
    with open(path, "w") as f:
        f.write("solid box\n")
        for n, q in quads:
            for tri in ([q[0], q[1], q[2]], [q[0], q[2], q[3]]):
                f.write(f"facet normal {n[0]} {n[1]} {n[2]}\n outer loop\n")
                for i in tri:
                    f.write(f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}\n")
                f.write(" endloop\nendfacet\n")
        f.write("endsolid box\n")


def _carve_text(face_name: str, tmp: str) -> list[tuple[float, float, float]]:
    model = os.path.join(tmp, "model.stl")
    _write_box_stl(model, *_BOX)
    face = find_named_face(model, face_name)
    scad = generate_emboss_scad(
        model_path=model,
        content_info={"type": "openscad_text", "text": "KILN"},
        face=face,
        output_dir=tmp,
        depth_mm=_DEPTH,
        mode="deboss",
        scale=0.6,
    )
    compiled = compile_embossed_model(
        scad["scad_path"], scad["output_stl_path"], timeout=300,
    )
    assert compiled.get("success"), f"{face_name}: compile failed: {compiled.get('error')}"
    return [v for t in _parse_stl(scad["output_stl_path"]) for v in t["vertices"]]


def _carve_floor_vertices(face_name: str, verts) -> int:
    """Vertices sitting one depth below the face surface (along the inward
    normal) — the signature of material actually removed from the body."""
    axis, sign, surf = _FACE_GEOM[face_name]
    floor = surf - sign * _DEPTH  # inward = -sign
    return sum(1 for p in verts if abs(p[axis] - floor) < 0.25)


@pytest.mark.parametrize("face_name", list(_FACE_GEOM))
def test_text_deboss_removes_material_on_every_cardinal_face(face_name, tmp_path):
    verts = _carve_text(face_name, str(tmp_path))
    n = _carve_floor_vertices(face_name, verts)
    assert n > 20, (
        f"{face_name} face: text deboss carved {n} vertices at the "
        f"carve floor — a silent no-op (the prism missed the body)"
    )
