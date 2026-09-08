"""Placed content must land on the PART, not merely inside the face's box.

Two defects, one root, found on a license-plate frame on 2026-09-07.

The frame's top face is a ring with one deep rail — 29.7 mm at the top,
12.7 mm elsewhere — so its area centroid sits 18.4 mm above the middle of
its outline.  ``generate_emboss_scad`` clamped placement offsets against
``±height/2`` (i.e. about the OUTLINE's middle) and then applied them
from ``face["center"]`` (the CENTROID).  The two disagreed by exactly the
asymmetry: top-rail text asked for at y=67.7 landed at 86.1 and was
sheared by the outer edge at 82.55; bottom-rail text landed in the window
and carved air.  Every symmetric product was fine, because on a disc or a
rectangle the two centres are the same point — which is why it survived.

The second defect is that even a correct anchor cannot make "inside the
bounding box" mean "on the part".  A ring's window IS inside its bbox.

So this file pins two things:

1. Placement anchors on ``bbox_center`` — the point the clamp already
   reasons about — and symmetric faces are unchanged.
2. After every clamp, the content's footprint is ray-cast INTO the body.
   Carving NOTHING — no material under any of it — is refused
   (``ContentOffFaceError``); a partial miss is disclosed as a warning.
   The split is measured, not chosen: a legitimate 40%-open vent grille
   and text bridging a frame's window both leave 26.7% of the footprint
   supported, so no threshold above zero can separate a defect from a
   vented product.  Leaving the face's OUTER edge is a different failure
   and is already impossible — ``_clamp_offsets`` bounds every offset
   inside the bbox, which item 1 makes the right box.  The cast is
   against the whole mesh, not the face's own triangles, so a second
   line of text landing on the floor a first deboss carved is fine.

Meshes are written inline as ASCII STL so the geometry-level tests need
no OpenSCAD.  Windings are self-corrected against a wanted normal, and
the first test proves the resolver reads them the way this file thinks
it wrote them.  Tests that COMPILE (chained deboss, ground-truth rotation,
final landing position) skip without OpenSCAD.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import kiln.emboss_generator as _eg
import pytest
from kiln.emboss_generator import _rotation_for_normal, generate_emboss_scad
from kiln.surface_intelligence import resolve_decoratable_face

# Resolved lazily so this file COLLECTS against an engine that predates the
# fix — the A/B then fails on behaviour, which is the honest failure.
ContentOffFaceError = getattr(_eg, "ContentOffFaceError", AssertionError)


def _missed(*a, **k):
    """The unsupported sample points, for tests that only care that some
    exist.  Lazily resolved for the same reason as the exception."""
    return _eg.footprint_material_coverage(*a, **k)["missed"]


def _coverage(*a, **k):
    return _eg.footprint_material_coverage(*a, **k)["coverage"]


def _rotation_matrix_for_normal(n):
    return _eg._rotation_matrix_for_normal(n)


def _apply_rotation(m, v):
    return _eg._apply_rotation(m, v)

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="OpenSCAD not installed"
)

# ---------------------------------------------------------------------
# The US plate frame that found the bug — restated, not imported, so
# these tests judge the engine against a known geometry rather than
# against whatever kiln-pro derives today.
# ---------------------------------------------------------------------
FRAME_W, FRAME_H, FRAME_T = 317.5, 165.1, 5.0
WIN_W, WIN_H = 292.1, 122.675
TOP_RAIL, SIDE_RAIL = 29.725, 12.7
WIN_CY = (SIDE_RAIL - TOP_RAIL) / 2.0          # window pushed down: -8.5125
TOP_RAIL_CY = FRAME_H / 2.0 - TOP_RAIL / 2.0   # 67.6875
CENTROID_Y = (WIN_W * WIN_H * -WIN_CY) / (FRAME_W * FRAME_H - WIN_W * WIN_H)


# ---------------------------------------------------------------------
# Inline ASCII-STL writers
# ---------------------------------------------------------------------
def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _quad(p0, p1, p2, p3, want_normal):
    """Two triangles for a planar quad, wound so the normal faces *want_normal*."""
    n = _cross(_sub(p1, p0), _sub(p2, p0))
    if _dot(n, want_normal) < 0:
        p1, p3 = p3, p1
    return [(p0, p1, p2), (p0, p2, p3)]


def _tris_to_stl(tris, path):
    lines = ["solid t"]
    for a, b, c in tris:
        lines.append("  facet normal 0 0 0\n    outer loop")
        for v in (a, b, c):
            lines.append(f"      vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        lines.append("    endloop\n  endfacet")
    lines.append("endsolid t")
    Path(path).write_text("\n".join(lines))
    return str(path)


def _box_tris(x0, x1, y0, y1, z0, z1):
    t = []
    t += _quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1))
    t += _quad((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (0, 0, -1))
    t += _quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0))
    t += _quad((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0))
    t += _quad((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0))
    t += _quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0))
    return t


def _ring_tris(x0, x1, y0, y1, wx0, wx1, wy0, wy1, z0, z1):
    """A rectangular plate with a rectangular THROUGH-window, axis z."""
    t = []
    for z, n in ((z1, (0, 0, 1)), (z0, (0, 0, -1))):
        # four strips around the window
        t += _quad((x0, y0, z), (x1, y0, z), (x1, wy0, z), (x0, wy0, z), n)
        t += _quad((x0, wy1, z), (x1, wy1, z), (x1, y1, z), (x0, y1, z), n)
        t += _quad((x0, wy0, z), (wx0, wy0, z), (wx0, wy1, z), (x0, wy1, z), n)
        t += _quad((wx1, wy0, z), (x1, wy0, z), (x1, wy1, z), (wx1, wy1, z), n)
    # outer walls
    t += _quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0))
    t += _quad((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0))
    t += _quad((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0))
    t += _quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0))
    # inner walls face INTO the window
    t += _quad((wx0, wy0, z0), (wx1, wy0, z0), (wx1, wy0, z1), (wx0, wy0, z1), (0, 1, 0))
    t += _quad((wx0, wy1, z0), (wx1, wy1, z0), (wx1, wy1, z1), (wx0, wy1, z1), (0, -1, 0))
    t += _quad((wx0, wy0, z0), (wx0, wy1, z0), (wx0, wy1, z1), (wx0, wy0, z1), (1, 0, 0))
    t += _quad((wx1, wy0, z0), (wx1, wy1, z0), (wx1, wy1, z1), (wx1, wy0, z1), (-1, 0, 0))
    return t


def _permute_xz_to_xy(tris):
    """Map a z-axis ring into a y-axis one: (x, y, z) -> (x, z, y).

    Swapping two axes reflects the mesh and reverses every winding; the
    writer re-winds each quad against its wanted normal, so the tris are
    re-emitted through ``_quad`` rather than copied.
    """
    out = []
    for a, b, c in tris:
        pa, pb, pc = ((a[0], a[2], a[1]), (b[0], b[2], b[1]), (c[0], c[2], c[1]))
        n = _cross(_sub(b, a), _sub(c, a))
        wanted = (n[0], n[2], n[1])
        nn = _cross(_sub(pb, pa), _sub(pc, pa))
        out.append((pa, pb, pc) if _dot(nn, wanted) >= 0 else (pa, pc, pb))
    return out


def _disc_tris(r, h, n=96):
    import math

    ring = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    t = []
    for i in range(n):
        (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
        t.append(((0, 0, h), (ax, ay, h), (bx, by, h)))
        t.append(((0, 0, 0), (bx, by, 0), (ax, ay, 0)))
        t += _quad((ax, ay, 0), (bx, by, 0), (bx, by, h), (ax, ay, h), (ax + bx, ay + by, 0))
    return t


def _grille_tris():
    """100x60x4 plate with nine 6mm through-slots — ~40% open.

    Built as ten solid bars rather than a plate minus slots: the ray cast
    only asks "is there material under this point", so a union of bars is
    the same answer with none of the boolean bookkeeping.
    """
    t = []
    edges = [-50.0]
    for i in range(-4, 5):
        edges += [i * 10 - 3, i * 10 + 3]
    edges.append(50.0)
    for a, b in zip(edges[0::2], edges[1::2], strict=True):
        if b > a:
            t += _box_tris(a, b, -30, 30, 0.0, 4.0)
    return t


@pytest.fixture(scope="module")
def meshes(tmp_path_factory):
    d = tmp_path_factory.mktemp("material")
    hw, hh = FRAME_W / 2.0, FRAME_H / 2.0
    us = _ring_tris(
        -hw, hw, -hh, hh,
        -WIN_W / 2.0, WIN_W / 2.0, WIN_CY - WIN_H / 2.0, WIN_CY + WIN_H / 2.0,
        0.0, FRAME_T,
    )
    sym = _ring_tris(-266, 266, -61, 61, -256, 256, -51, 51, 0.0, 5.0)
    box = _box_tris(0, 100, 0, 60, 0, 4)
    # 100 wide (x), 60 tall (z), 8 deep (y) with a 60x30 window through the
    # FRONT face — a ring whose axis is y.
    fw = _permute_xz_to_xy(_ring_tris(-50, 50, 0, 60, -30, 30, 15, 45, -4.0, 4.0))
    return {
        "us_ring": _tris_to_stl(us, d / "us_ring.stl"),
        "grille": _tris_to_stl(_grille_tris(), d / "grille.stl"),
        "sym_ring": _tris_to_stl(sym, d / "sym_ring.stl"),
        "box": _tris_to_stl(box, d / "box.stl"),
        "front_window": _tris_to_stl(fw, d / "front_window.stl"),
        "disc": _tris_to_stl(_disc_tris(40.0, 4.0), d / "disc.stl"),
        "dir": d,
    }


def _text(size: float | None = None, text: str = "KILN") -> dict:
    c = {"type": "openscad_text", "text": text, "font": "Liberation Sans:style=Bold"}
    if size:
        c["font_size"] = size
    return c


def _outer_translate(scad_path: str) -> tuple[float, float, float]:
    """The first translate in the emitted SCAD is the placement anchor."""
    m = re.search(r"translate\(\[([-\d.]+), ([-\d.]+), ([-\d.]+)\]\)", Path(scad_path).read_text())
    assert m, "no translate in emitted SCAD"
    return tuple(float(v) for v in m.groups())  # type: ignore[return-value]


def _deboss_floor_bbox(stl_path: str, thickness: float):
    """XY bbox of vertices strictly between the bed and the top surface —
    the floor of a deboss — or None if nothing was carved."""
    pts = []
    raw = Path(stl_path).read_bytes()
    if raw[:5] == b"solid":
        for line in raw.decode(errors="replace").splitlines():
            if "vertex" in line:
                _, x, y, z = line.split()
                if 0.01 < float(z) < thickness - 0.01:
                    pts.append((float(x), float(y)))
    else:
        (n,) = struct.unpack_from("<I", raw, 80)
        for i in range(n):
            base = 84 + i * 50
            for k in range(3):
                x, y, z = struct.unpack_from("<fff", raw, base + 12 + k * 12)
                if 0.01 < z < thickness - 0.01:
                    pts.append((x, y))
    if not pts:
        return None
    return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts))


# =====================================================================
# 1. Two centres
# =====================================================================
class TestTwoCentres:
    def test_writer_and_resolver_agree_on_the_frame(self, meshes):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        assert f["normal"][2] > 0.99
        assert f["width_mm"] == pytest.approx(FRAME_W)
        assert f["height_mm"] == pytest.approx(FRAME_H)

    def test_asymmetric_ring_has_two_different_centres(self, meshes):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        # The centroid rides up toward the deep rail; the outline's middle
        # does not move.  This 18.4 mm IS the bug's magnitude.
        assert f["center"][1] == pytest.approx(CENTROID_Y, abs=0.02)
        assert f["center"][1] > 18.0
        assert f["bbox_center"] == pytest.approx((0.0, 0.0, FRAME_T), abs=1e-3)

    @pytest.mark.parametrize("name", ["box", "sym_ring", "disc"])
    def test_symmetric_faces_have_coincident_centres(self, meshes, name):
        f = resolve_decoratable_face(meshes[name], "top")
        for a, b in zip(f["center"], f["bbox_center"], strict=True):
            assert a == pytest.approx(b, abs=1e-3)


# =====================================================================
# 2. The anchor
# =====================================================================
class TestPlacementAnchor:
    def test_offsets_are_applied_from_the_outline_centre(self, meshes, tmp_path):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        r = generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=_text(16), face=f,
            output_dir=str(tmp_path), offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )
        tx, ty, _ = _outer_translate(r["scad_path"])
        # Not 18.39.  With the centroid as anchor the rail text lands at
        # 86.1 and is sheared by the outer edge at 82.55.
        assert (tx, ty) == pytest.approx((0.0, 0.0), abs=1e-3)

    def test_symmetric_faces_anchor_where_they_always_did(self, meshes, tmp_path):
        # On a symmetric face both centres coincide, so the anchor cannot
        # move.  The ring's text goes on its rail — at (0, 0) it would be
        # in the window, and the material check would (rightly) refuse it.
        for name, oy in (("box", 0.0), ("sym_ring", 61.0 - 5.0)):
            f = resolve_decoratable_face(meshes[name], "top")
            r = generate_emboss_scad(
                model_path=meshes[name], content_info=_text(8), face=f,
                output_dir=str(tmp_path / name), offset_y_mm=oy, min_edge_margin_mm=0.0,
            )
            tx, ty, _ = _outer_translate(r["scad_path"])
            assert (tx, ty) == pytest.approx(f["center"][:2], abs=1e-3)

    def test_a_face_dict_without_bbox_center_falls_back_to_center(self, tmp_path):
        # Hand-built dicts (older callers, this repo's own unit tests) carry
        # only ``center``.  They keep working; the check stands aside when
        # the mesh cannot be read and says so in the log.
        face = {"normal": [0, 0, 1], "center": [5.0, 5.0, 10.0], "width_mm": 10.0, "height_mm": 10.0, "face_name": "top"}
        r = generate_emboss_scad(
            model_path=str(tmp_path / "nonexistent.stl"), content_info=_text(4),
            face=face, output_dir=str(tmp_path),
        )
        assert _outer_translate(r["scad_path"])[:2] == pytest.approx((5.0, 5.0))


# =====================================================================
# 3. The material check
# =====================================================================
class TestFootprintMustLandOnThePart:
    def test_text_in_the_window_is_refused(self, meshes, tmp_path):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        # Inside the 317x165 bbox — the clamp is happy — and over air.
        with pytest.raises(ContentOffFaceError, match="would carve nothing"):
            generate_emboss_scad(
                model_path=meshes["us_ring"], content_info=_text(8), face=f,
                output_dir=str(tmp_path), offset_y_mm=0.0, min_edge_margin_mm=0.0,
            )

    def test_text_too_tall_for_its_rail_is_disclosed(self, meshes, tmp_path):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        # Centred on the rail, but 40 mm of glyph on a 29.7 mm rail spills
        # into the window.  Most of it still carves, so this is a warning,
        # not a refusal — the same shape a vent grille produces.
        r = generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=_text(40), face=f,
            output_dir=str(tmp_path), offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )
        assert any("crosses an opening" in w for w in r.get("warnings") or []), r.get("warnings")

    def test_rail_text_that_fits_is_accepted(self, meshes, tmp_path):
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        r = generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=_text(16), face=f,
            output_dir=str(tmp_path), offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )
        assert os.path.isfile(r["scad_path"])

    def test_edge_to_edge_is_accepted_and_a_fifth_of_a_millimetre_over_is_not(self, meshes):
        # The primitive, driven directly so the footprint is exact.  Content
        # sized to the rail on purpose (a frame's rail text at margin 0)
        # must not be refused for a hair it never had; content that really
        # overhangs must be.
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        a = f["bbox_center"]
        assert _missed(meshes["us_ring"], f, a, 100.0, TOP_RAIL, 0.0, TOP_RAIL_CY) == []
        assert _missed(meshes["us_ring"], f, a, 100.0, TOP_RAIL + 0.2, 0.0, TOP_RAIL_CY)

    def test_an_overhang_on_one_side_only_is_caught(self, meshes):
        f = resolve_decoratable_face(meshes["box"], "top")
        a = f["bbox_center"]
        # 50 wide, shifted so the right edge sits at x=50.3 on a face that
        # ends at 50.  The left edge is 25 mm inside.  Corner-only sampling
        # would still catch this; the grid catches a hole in the middle too.
        assert _missed(meshes["box"], f, a, 50.0, 20.0, 25.3, 0.0)
        assert _missed(meshes["box"], f, a, 50.0, 20.0, 24.9, 0.0) == []

    def test_content_bridging_the_window_is_seen_not_just_its_corners(self, meshes):
        # A tall vertical mark centred on the frame: 20 wide, 160 tall.  All
        # four corners land on the rails (top rail from y=52.8, bottom rail
        # to y=-69.85), and everything between them is window.  A check
        # that sampled only corners would pass it and carve nothing for
        # most of its height; the interior grid is what refuses it.
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        a = f["bbox_center"]
        missed = _missed(meshes["us_ring"], f, a, 20.0, 160.0, 0.0, 0.0)
        assert missed
        # ...and the corners themselves were fine — this is an INTERIOR
        # miss, which is the point.
        assert not any(abs(sy) > 79.0 for _, sy in missed)

    def test_bottom_face_is_judged_in_its_flipped_frame(self, meshes, tmp_path):
        # rotate([180,0,0]) sends local +y to world -y, so the deep rail —
        # at world +y — is reached with a NEGATIVE offset from underneath.
        # A sign error here would put the text in the window, so the two
        # halves of this test are what pin the flip.
        r = generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=_text(12), face=f0 if (f0 := resolve_decoratable_face(meshes["us_ring"], "bottom")) else None,
            output_dir=str(tmp_path / "ok"), offset_y_mm=-TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )
        assert not (r.get("warnings") or [])
        with pytest.raises(ContentOffFaceError):
            generate_emboss_scad(
                model_path=meshes["us_ring"], content_info=_text(12), face=f0,
                output_dir=str(tmp_path / "bad"), offset_y_mm=0.0, min_edge_margin_mm=0.0,
            )

    def test_a_side_face_window_is_seen_in_the_engines_own_frame(self, meshes, tmp_path):
        # The engine's rotation for FRONT maps local +y to world +z.  A
        # check that guessed the frame from the face's own axes could agree
        # here and disagree on LEFT/RIGHT; reproducing the rotation makes
        # it right everywhere.  This face has a 60x30 window in the middle.
        f = resolve_decoratable_face(meshes["front_window"], "front")
        with pytest.raises(ContentOffFaceError):
            generate_emboss_scad(
                model_path=meshes["front_window"], content_info=_text(10), face=f,
                output_dir=str(tmp_path / "win"), offset_y_mm=0.0, min_edge_margin_mm=0.0,
            )
        # ...and the same content one grid step outside the window still
        # carves, so the refusal above is about the hole, not the face.
        generate_emboss_scad(  # solid strip below the window
            model_path=meshes["front_window"], content_info=_text(8), face=f,
            output_dir=str(tmp_path / "low"), offset_y_mm=-22.0, min_edge_margin_mm=0.0,
        )
        generate_emboss_scad(  # solid pillar beside it
            model_path=meshes["front_window"], content_info=_text(6, "I"), face=f,
            output_dir=str(tmp_path / "side"), offset_x_mm=40.0, min_edge_margin_mm=0.0,
        )

    def test_svg_content_is_guarded_too(self, meshes, tmp_path):
        svg = tmp_path / "logo.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40"><rect width="100" height="40"/></svg>')
        info = {"type": "svg", "svg_path": str(svg), "width": 100, "height": 40, "aspect_ratio": 2.5}
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        with pytest.raises(ContentOffFaceError, match="svg content"):
            generate_emboss_scad(
                model_path=meshes["us_ring"], content_info=info, face=f,
                output_dir=str(tmp_path / "win"), scale=0.3, offset_y_mm=0.0, min_edge_margin_mm=0.0,
            )
        generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=info, face=f,
            output_dir=str(tmp_path / "rail"), absolute_size_mm=40.0, offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )

    def test_heightmap_content_is_guarded_too(self, meshes, tmp_path):
        # The third door.  A photo is a heightmap driving ``surface()``;
        # its footprint is the target box, and the same closure judges it.
        dat = tmp_path / "flat.dat"
        dat.write_text("\n".join(" ".join("1" for _ in range(4)) for _ in range(4)))
        info = {"type": "heightmap", "dat_path": str(dat), "width_px": 4, "height_px": 4, "aspect_ratio": 1.0}
        f = resolve_decoratable_face(meshes["us_ring"], "top")
        with pytest.raises(ContentOffFaceError, match="heightmap content"):
            generate_emboss_scad(
                model_path=meshes["us_ring"], content_info=info, face=f,
                output_dir=str(tmp_path / "win"), scale=0.3, offset_y_mm=0.0, min_edge_margin_mm=0.0,
            )
        generate_emboss_scad(
            model_path=meshes["us_ring"], content_info=info, face=f,
            output_dir=str(tmp_path / "rail"), absolute_size_mm=20.0, offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )

    def test_a_vented_part_warns_and_is_never_refused(self, meshes, tmp_path):
        """The case that set the threshold.  A 40%-open vent grille leaves
        exactly as much of the footprint supported (26.7%) as text bridging
        a frame's window does — measured, on real geometry.  So a vented
        part must warn and carve, never be refused, and this is the test
        that fails if anyone later "tightens" the rule to a fraction."""
        f = resolve_decoratable_face(meshes["grille"], "top")
        cov = _coverage(meshes["grille"], f, f["bbox_center"], 60.0, 20.0, 0.0, 0.0)
        assert 0.0 < cov < 0.5, cov
        r = generate_emboss_scad(
            model_path=meshes["grille"], content_info=_text(14), face=f,
            output_dir=str(tmp_path), min_edge_margin_mm=0.0,
        )
        assert any("crosses an opening" in w for w in r.get("warnings") or [])

    def test_round_face_no_false_refusal(self, meshes, tmp_path):
        # The disc is the product the existing guards protect best.  Auto
        # text and an oversized explicit size (clamped by the size and rim
        # guards) must both still be accepted — the material check judges
        # what the earlier guards left, never what was asked.
        f = resolve_decoratable_face(meshes["disc"], "top")
        generate_emboss_scad(model_path=meshes["disc"], content_info=_text(), face=f, output_dir=str(tmp_path / "auto"), scale=0.85)
        generate_emboss_scad(model_path=meshes["disc"], content_info=_text(60), face=f, output_dir=str(tmp_path / "big"))

    def test_round_face_corner_the_outline_lies_about_is_refused(self, meshes):
        # An 80 mm disc's bbox is an 80 mm square; its corners are air.  A
        # 30x20 mark pushed to (24, 24) has its far corner at (39, 34) —
        # inside the square (the offset clamp allows it) and 51.8 mm from
        # the centre (off the disc).  A logo dragged to a coaster's corner.
        f = resolve_decoratable_face(meshes["disc"], "top")
        a = f["bbox_center"]
        assert _missed(meshes["disc"], f, a, 30.0, 20.0, 24.0, 24.0)
        assert _missed(meshes["disc"], f, a, 30.0, 20.0, 10.0, 10.0) == []


# =====================================================================
# 4. Compiled truth — what OpenSCAD actually does with the SCAD
# =====================================================================
@pytest.fixture(scope="module")
def compiled_meshes(tmp_path_factory):
    """Watertight meshes for tests that COMPILE a boolean.  The inline
    STL writer is fine for the resolver and the ray cast, but its strips
    meet at T-junctions and CGAL will not difference() against those."""
    if shutil.which("openscad") is None:
        pytest.skip("OpenSCAD not installed")
    d = tmp_path_factory.mktemp("compiled")
    hw, hh = FRAME_W / 2.0, FRAME_H / 2.0
    scad = {
        "us_ring": (
            f"difference(){{ translate([{-hw},{-hh},0]) cube([{FRAME_W},{FRAME_H},{FRAME_T}]); "
            f"translate([{-WIN_W / 2.0},{WIN_CY - WIN_H / 2.0},-1]) cube([{WIN_W},{WIN_H},{FRAME_T + 2}]); }}"
        ),
        "box": "cube([100,60,4]);",
    }
    out = {}
    for name, src in scad.items():
        sp, st = d / f"{name}.scad", d / f"{name}.stl"
        sp.write_text(src)
        subprocess.run(["openscad", "-o", str(st), str(sp)], capture_output=True, check=True)
        out[name] = str(st)
    return out


@needs_openscad
class TestCompiledTruth:
    def test_rotation_matrix_mirrors_the_scad_clause(self, tmp_path):
        """The material check predicts the footprint with a matrix that
        MUST agree with the ``rotate(...)`` clause the SCAD emits.  Judged
        by OpenSCAD itself: an asymmetric marker is pushed through the
        clause and its compiled position compared to the matrix."""
        probe = (5.0, 2.0, 0.0)
        normals = [
            (0, 0, 1), (0, 0, -1), (0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0),
            (0, -0.34202, 0.93969), (0.3, 0.4, 0.866),
        ]
        for i, n in enumerate(normals):
            clause = _rotation_for_normal(list(n)).strip()
            sp = tmp_path / f"p{i}.scad"
            st = tmp_path / f"p{i}.stl"
            sp.write_text(f"{clause} translate([{probe[0]},{probe[1]},{probe[2]}]) cube(0.02,center=true);")
            subprocess.run(["openscad", "-o", str(st), str(sp)], capture_output=True, check=True)
            raw = st.read_bytes()
            xs = ys = zs = 0.0
            cnt = 0
            if raw[:5] == b"solid":
                for line in raw.decode(errors="replace").splitlines():
                    if "vertex" in line:
                        _, x, y, z = line.split()
                        xs += float(x)
                        ys += float(y)
                        zs += float(z)
                        cnt += 1
            else:
                (nt,) = struct.unpack_from("<I", raw, 80)
                for k in range(nt):
                    for j in range(3):
                        x, y, z = struct.unpack_from("<fff", raw, 84 + k * 50 + 12 + j * 12)
                        xs += x
                        ys += y
                        zs += z
                        cnt += 1
            truth = (xs / cnt, ys / cnt, zs / cnt)
            mine = _apply_rotation(_rotation_matrix_for_normal(list(n)), probe)
            assert mine == pytest.approx(truth, abs=1e-3), f"normal {n}"

    def test_rail_text_compiles_onto_the_rail(self, compiled_meshes, tmp_path):
        """The finding, end to end: text asked for at the top rail's centre
        is carved there, fully on the rail.  Pre-fix it was carved at
        y=82.26..82.55 — a 0.3 mm sliver at the outer edge."""
        f = resolve_decoratable_face(compiled_meshes["us_ring"], "top")
        r = generate_emboss_scad(
            model_path=compiled_meshes["us_ring"], content_info=_text(16), face=f,
            output_dir=str(tmp_path), depth_mm=1.2, offset_y_mm=TOP_RAIL_CY, min_edge_margin_mm=0.0,
        )
        subprocess.run(["openscad", "-o", r["output_stl_path"], r["scad_path"]], capture_output=True, check=True)
        bb = _deboss_floor_bbox(r["output_stl_path"], FRAME_T)
        assert bb is not None, "nothing was carved"
        _, _, y0, y1 = bb
        rail_bottom = FRAME_H / 2.0 - TOP_RAIL
        assert rail_bottom < y0 and y1 < FRAME_H / 2.0, (y0, y1)
        assert (y0 + y1) / 2.0 == pytest.approx(TOP_RAIL_CY, abs=0.5)

    def test_a_second_deboss_over_a_carved_floor_is_not_refused(self, compiled_meshes, tmp_path):
        """Chained decorations land on floors the first pass carved.  A
        containment test against the top face's own triangles would refuse
        them; the ray cast finds the floor and lets them through."""
        f = resolve_decoratable_face(compiled_meshes["box"], "top")
        r1 = generate_emboss_scad(
            model_path=compiled_meshes["box"], content_info=_text(14, "AB"), face=f,
            output_dir=str(tmp_path / "one"), depth_mm=1.0, min_edge_margin_mm=0.0,
        )
        subprocess.run(["openscad", "-o", r1["output_stl_path"], r1["scad_path"]], capture_output=True, check=True)
        f2 = resolve_decoratable_face(r1["output_stl_path"], "top")
        generate_emboss_scad(
            model_path=r1["output_stl_path"], content_info=_text(14, "CD"), face=f2,
            output_dir=str(tmp_path / "two"), depth_mm=1.0, offset_x_mm=6.0, min_edge_margin_mm=0.0,
        )
