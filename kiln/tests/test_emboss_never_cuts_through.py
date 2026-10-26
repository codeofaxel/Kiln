"""A deboss is a recess, never a hole: it always leaves a floor.

Measured 2026-10-23 on a product generator's own default: "Read"
debossed at the 1.2 mm legibility depth into a 1.2 mm bookmark came out
as FIVE separate solids — the body, and the middles of the R, e, a and d,
cut free and left standing on the bed.  The printability check said
"Ready to print."  Its callers believed the engine refused a depth the
part could not take; nothing between the legibility rule and the carve
knew how thick the part was.

The engine now measures the part under the content along the face normal
(:func:`kiln.emboss_generator.material_under_footprint`) and refuses
(:class:`kiln.emboss_generator.DebossTooDeepError`) a cut that would leave
less floor than one nozzle width — the line the printability check draws
for every wall.  It does not pick a shallower cut, a raised mark or a
thicker part: each of those changes what the product is, so the caller
decides.

Geometry tests write their meshes inline and need no OpenSCAD.  The test
that carves real text through the door the product generators use skips
without it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import kiln.emboss_generator as _eg
from kiln.surface_intelligence import resolve_decoratable_face


class _NeverRaised(Exception):
    """Stands in for the refusal on an engine that predates it, so this file
    collects there and the A/B fails on behaviour, not on an import."""


DebossTooDeepError = getattr(_eg, "DebossTooDeepError", _NeverRaised)

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="OpenSCAD not installed"
)

NOZZLE = 0.4
LEGIBLE_DEPTH = 3 * NOZZLE  # the legibility floor every text door debosses at


# ---------------------------------------------------------------------
# Inline ASCII STL
# ---------------------------------------------------------------------
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _quad(p0, p1, p2, p3, want_normal):
    """Two triangles for a planar quad, wound to face *want_normal*."""
    if _dot(_cross(_sub(p1, p0), _sub(p2, p0)), want_normal) < 0:
        p1, p3 = p3, p1
    return [(p0, p1, p2), (p0, p2, p3)]


def _box(x0, x1, y0, y1, z0, z1):
    t = []
    t += _quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1))
    t += _quad((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (0, 0, -1))
    t += _quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0))
    t += _quad((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0))
    t += _quad((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0))
    t += _quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0))
    return t


def _pocketed_slab(x0, x1, y0, y1, thick, px0, px1, py0, py1, pocket):
    """A slab with a blind rectangular pocket in its TOP — the floor an
    earlier carve leaves, which a later carve from below must respect."""
    t = []
    z1, zf = thick, thick - pocket
    up, down = (0, 0, 1), (0, 0, -1)
    t += _quad((x0, y0, z1), (x1, y0, z1), (x1, py0, z1), (x0, py0, z1), up)
    t += _quad((x0, py1, z1), (x1, py1, z1), (x1, y1, z1), (x0, y1, z1), up)
    t += _quad((x0, py0, z1), (px0, py0, z1), (px0, py1, z1), (x0, py1, z1), up)
    t += _quad((px1, py0, z1), (x1, py0, z1), (x1, py1, z1), (px1, py1, z1), up)
    t += _quad((px0, py0, zf), (px1, py0, zf), (px1, py1, zf), (px0, py1, zf), up)
    t += _quad((x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0), down)
    t += _quad((x0, y0, 0), (x1, y0, 0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0))
    t += _quad((x0, y1, 0), (x1, y1, 0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0))
    t += _quad((x0, y0, 0), (x0, y1, 0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0))
    t += _quad((x1, y0, 0), (x1, y1, 0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0))
    # Pocket walls face INTO the pocket.
    t += _quad((px0, py0, zf), (px1, py0, zf), (px1, py0, z1), (px0, py0, z1), (0, 1, 0))
    t += _quad((px0, py1, zf), (px1, py1, zf), (px1, py1, z1), (px0, py1, z1), (0, -1, 0))
    t += _quad((px0, py0, zf), (px0, py1, zf), (px0, py1, z1), (px0, py0, z1), (1, 0, 0))
    t += _quad((px1, py0, zf), (px1, py1, zf), (px1, py1, z1), (px1, py0, z1), (-1, 0, 0))
    return t


def _stl(tris, path) -> str:
    lines = ["solid t"]
    for a, b, c in tris:
        lines.append("  facet normal 0 0 0\n    outer loop")
        for v in (a, b, c):
            lines.append(f"      vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        lines.append("    endloop\n  endfacet")
    lines.append("endsolid t")
    Path(path).write_text("\n".join(lines))
    return str(path)


def _text(size: float = 6.0, text: str = "Read") -> dict:
    return {
        "type": "openscad_text", "text": text,
        "font": "Liberation Sans:style=Bold", "font_size": size,
    }


def _scad(stl: str, face_name: str, content: dict, depth: float, out: Path, **kw):
    face = resolve_decoratable_face(stl, face_name, auto="largest")
    return _eg.generate_emboss_scad(
        model_path=stl, content_info=content, face=face, output_dir=str(out),
        depth_mm=depth, mode=kw.pop("mode", "deboss"), scale=0.6,
        min_edge_margin_mm=2.0, nozzle_diameter_mm=NOZZLE, **kw,
    )


# ---------------------------------------------------------------------
# The measurement: what is under the content
# ---------------------------------------------------------------------
class TestMaterialUnderFootprint:
    def test_a_slab_reads_its_own_thickness_at_every_sample(self, tmp_path):
        stl = _stl(_box(0, 30, 0, 60, 0, 1.2), tmp_path / "slab.stl")
        face = resolve_decoratable_face(stl, "top", auto="largest")
        under = _eg.material_under_footprint(
            stl, face, tuple(face.get("bbox_center") or face["center"]),
            16.0, 6.0, 0.0, 0.0,
        )
        assert under["checked"]
        assert under["columns"], "every sample is over the slab"
        for _sx, _sy, stretches in under["columns"]:
            assert len(stretches) == 1
            top, bottom = stretches[0]
            assert top == pytest.approx(0.0, abs=1e-6)
            assert bottom == pytest.approx(-1.2, abs=1e-6)

    def test_two_flush_solids_read_as_one_part(self, tmp_path):
        """Shells sitting on each other print as one part, so the line
        through them is one stretch of material, not two."""
        tris = _box(0, 30, 0, 60, 0, 1.0) + _box(0, 30, 0, 60, 1.0, 2.0)
        stl = _stl(tris, tmp_path / "stack.stl")
        # The real top, stated: a face resolver may also count the two
        # coincident inner faces at z=1, which is not what this pins.
        top = {"normal": [0.0, 0.0, 1.0], "center": [15.0, 30.0, 2.0],
               "width_mm": 30.0, "height_mm": 60.0, "face_name": "top"}
        under = _eg.material_under_footprint(
            stl, top, (15.0, 30.0, 2.0), 10.0, 4.0, 0.0, 0.0,
        )
        assert under["columns"]
        for _sx, _sy, stretches in under["columns"]:
            assert [(round(t, 6), round(b, 6)) for t, b in stretches] == [(0.0, -2.0)]

    def test_a_floor_left_by_an_earlier_carve_is_what_a_carve_from_below_meets(self, tmp_path):
        stl = _stl(
            _pocketed_slab(0, 40, 0, 20, 2.5, 4, 36, 3, 17, LEGIBLE_DEPTH),
            tmp_path / "pocketed.stl",
        )
        face = resolve_decoratable_face(stl, "bottom", auto="largest")
        under = _eg.material_under_footprint(
            stl, face, tuple(face.get("bbox_center") or face["center"]),
            10.0, 4.0, 0.0, 0.0,
        )
        bottoms = {round(stretches[0][1], 4) for _x, _y, stretches in under["columns"]}
        assert bottoms == {round(-(2.5 - LEGIBLE_DEPTH), 4)}


# ---------------------------------------------------------------------
# The rule: a cut that would leave less than one nozzle width is refused
# ---------------------------------------------------------------------
class TestRefusesWhatWouldCutThrough:
    @pytest.mark.parametrize("face_name", ["top", "bottom"])
    def test_a_legibility_deep_cut_into_a_part_that_thin_is_refused(self, tmp_path, face_name):
        stl = _stl(_box(0, 30, 0, 60, 0, 1.2), tmp_path / "slab.stl")
        with pytest.raises(DebossTooDeepError) as caught:
            _scad(stl, face_name, _text(), LEGIBLE_DEPTH, tmp_path)
        err = caught.value
        assert err.thickness_mm == pytest.approx(1.2, abs=1e-6)
        assert err.floor_left_mm == pytest.approx(0.0, abs=1e-6)
        assert err.floor_mm == pytest.approx(NOZZLE)
        assert err.max_depth_mm == pytest.approx(1.2 - NOZZLE, abs=1e-6)
        assert "cut straight through" in str(err)
        assert not list(tmp_path.glob("*.scad")), "a refused deboss writes nothing"

    def test_a_side_face_is_measured_along_its_own_normal(self, tmp_path):
        # A 1.2 mm wall standing up: its big faces point along y.
        stl = _stl(_box(0, 60, 0, 1.2, 0, 30), tmp_path / "wall.stl")
        with pytest.raises(DebossTooDeepError):
            _scad(stl, "front", _text(), LEGIBLE_DEPTH, tmp_path)

    @pytest.mark.parametrize(
        "content",
        [
            {"type": "svg", "svg_path": "logo.svg", "width": 40, "height": 20,
             "aspect_ratio": 2.0},
            {"type": "heightmap", "pgm_path": "photo.pgm", "width_px": 40,
             "height_px": 20, "aspect_ratio": 2.0},
        ],
        ids=["svg", "heightmap"],
    )
    def test_every_content_kind_is_held_to_it(self, tmp_path, content):
        stl = _stl(_box(0, 30, 0, 60, 0, 0.8), tmp_path / "thin.stl")
        with pytest.raises(DebossTooDeepError):
            _scad(stl, "top", content, 0.8, tmp_path)

    def test_a_floor_thinner_than_the_nozzle_is_refused_too(self, tmp_path):
        stl = _stl(_box(0, 30, 0, 60, 0, 1.4), tmp_path / "slab.stl")
        with pytest.raises(DebossTooDeepError) as caught:
            _scad(stl, "top", _text(), LEGIBLE_DEPTH, tmp_path)
        assert caught.value.floor_left_mm == pytest.approx(0.2, abs=1e-6)
        assert "only 0.20 mm of floor" in str(caught.value)
        assert "printability check refuses" in str(caught.value)

    def test_a_carve_from_below_respects_the_floor_one_from_above_left(self, tmp_path):
        stl = _stl(
            _pocketed_slab(0, 40, 0, 20, 2.5, 4, 36, 3, 17, LEGIBLE_DEPTH),
            tmp_path / "pocketed.stl",
        )
        with pytest.raises(DebossTooDeepError) as caught:
            _scad(stl, "bottom", _text(4.0, "KILN"), LEGIBLE_DEPTH, tmp_path)
        assert caught.value.floor_left_mm == pytest.approx(0.1, abs=1e-6)

    def test_a_mark_moved_up_to_a_cover_is_held_to_the_cover(self, tmp_path):
        """Text placed on a face under a thin cover is moved up to meet the
        cover and cut there, so the floor is the cover's, not the face's:
        judged from the face, the 4 mm base under the cover read as room
        for a 1.2 mm cut that went straight through the 0.9 mm cover."""
        tris = _box(0, 80, 0, 40, 0, 4.0) + _box(5, 75, 5, 35, 6.0, 6.9)
        stl = _stl(tris, tmp_path / "covered.stl")
        face = {"normal": [0.0, 0.0, 1.0], "center": [40.0, 20.0, 4.0],
                "width_mm": 80.0, "height_mm": 40.0, "face_name": "top"}
        with pytest.raises(DebossTooDeepError) as caught:
            _eg.generate_emboss_scad(
                model_path=stl, content_info=_text(), face=face,
                output_dir=str(tmp_path), depth_mm=LEGIBLE_DEPTH, mode="deboss",
                scale=0.6, min_edge_margin_mm=2.0, nozzle_diameter_mm=NOZZLE,
            )
        err = caught.value
        assert err.depth_mm == pytest.approx(LEGIBLE_DEPTH)
        assert err.thickness_mm == pytest.approx(0.9, abs=1e-6)
        assert err.max_depth_mm == pytest.approx(0.9 - NOZZLE, abs=1e-6)

    def test_a_part_with_room_for_the_floor_is_carved(self, tmp_path):
        stl = _stl(_box(0, 30, 0, 60, 0, 1.2 + NOZZLE), tmp_path / "slab.stl")
        result = _scad(stl, "top", _text(), LEGIBLE_DEPTH, tmp_path)
        assert Path(result["scad_path"]).is_file()

    def test_a_raised_mark_is_never_refused_for_thinness(self, tmp_path):
        stl = _stl(_box(0, 30, 0, 60, 0, 0.8), tmp_path / "thin.stl")
        result = _scad(stl, "top", _text(), LEGIBLE_DEPTH, tmp_path, mode="emboss")
        assert Path(result["scad_path"]).is_file()


# ---------------------------------------------------------------------
# Behaviour, through the door every text-bearing product generator uses
# ---------------------------------------------------------------------
def _recess_floor_mm(stl_path: str, top_z: float) -> float:
    """How much part is left under the deepest recess: the lowest
    upward-facing surface below the top.  0.0 when there is none — the cut
    went through, and nothing faces up between the bed and the top."""
    import numpy as np
    import trimesh

    mesh = trimesh.load(stl_path, force="mesh")
    up = mesh.face_normals[:, 2] > 0.99
    heights = mesh.triangles_center[up, 2]
    below = heights[heights < top_z - 1e-4]
    return float(np.min(below)) if below.size else 0.0


@needs_openscad
@pytest.mark.parametrize("thickness", [1.2, 1.4, 1.6, 2.0])
def test_text_on_a_thin_body_is_one_solid_with_a_floor_or_refused(tmp_path, thickness):
    """A bookmark-sized slab, "Read" at the legibility depth: whatever the
    engine hands back is ONE solid standing on a floor at least a nozzle
    thick, or it hands back nothing and says why."""
    import trimesh

    from kiln.decoration_helpers import emboss_text_lines_on_face
    from kiln.mesh_bodies import read_mesh

    body = _stl(_box(0, 30, 0, 60, 0, thickness), tmp_path / "body.stl")
    try:
        out = emboss_text_lines_on_face(
            body, ["Read"], face_name="top", mode="deboss",
            nozzle_diameter_mm=NOZZLE, line_scale=0.65,
            output_dir=str(tmp_path / "decorated"),
        )
    except DebossTooDeepError as err:
        assert thickness - LEGIBLE_DEPTH < NOZZLE
        assert err.floor_left_mm < NOZZLE
        return
    solids = read_mesh(trimesh.load(out, force="mesh")).solid_count
    assert solids == 1, f"{thickness} mm body came back as {solids} solids"
    floor = _recess_floor_mm(out, thickness)
    assert floor >= NOZZLE - 1e-3, f"{thickness} mm body kept a {floor:.2f} mm floor"


@needs_openscad
def test_the_public_decoration_door_refuses_a_cut_through_too(tmp_path):
    """``decorate_surface`` reaches the same engine: on a 1.2 mm slab,
    "Read" at 1.2 mm is refused with the reason, where it used to answer
    success with the slab and the loose middles of its letters."""
    import kiln.server as server

    slab = _stl(_box(0, 30, 0, 60, 0, 1.2), tmp_path / "slab.stl")
    res = server.decorate_surface(
        model_path=slab, content="text:Read", face="top", mode="deboss",
        depth_mm=LEGIBLE_DEPTH,
    )
    if isinstance(res, list):
        res = next(r for r in res if isinstance(r, dict))
    assert res.get("success") is False, f"carved a cut through: {res.get('output_stl')}"
    assert res["error"]["code"] == "VALIDATION_ERROR"
    assert "cut straight through" in res["error"]["message"]
