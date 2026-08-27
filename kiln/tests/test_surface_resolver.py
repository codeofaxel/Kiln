"""Behavioural pins for :mod:`kiln.surface_intelligence`'s face resolution.

Five fixtures, each a shape that once fooled a face detector somewhere:
a plain plate, a tray whose rim competes with its floor, a hollow body
whose interior wall faces the same way as its outer wall, a recessed
underside, and a tilted canvas that naive world-Z clustering shreds.
The expected numbers are measured engine behaviour — if a change here
moves them, the change altered which face the tools decorate, and that
is a decision, not a refactor.

All meshes are written as binary STL (float32, like every STL in the
wild) into pytest tmp dirs; one fixture round-trips through 3MF with a
non-identity build transform to pin that transforms are applied.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from kiln.surface_intelligence import (
    find_named_face,
    resolve_decoratable_face,
)

# ---------------------------------------------------------------------------
# Mesh builders — one triangle per 3 vertex triples, written verbatim
# ---------------------------------------------------------------------------


def _write_stl(path: Path, triangles: list[tuple]) -> str:
    """Write raw triangles as binary STL, recomputing normals from winding."""
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            u = [b[i] - a[i] for i in range(3)]
            v = [c[i] - a[i] for i in range(3)]
            n = [
                u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0],
            ]
            length = math.sqrt(sum(t * t for t in n)) or 1.0
            fh.write(struct.pack("<3f", *[t / length for t in n]))
            for pt in (a, b, c):
                fh.write(struct.pack("<3f", *pt))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _upward_quad(x0, x1, y0, y1, z):
    """A quad on the z=`z` plane facing +Z."""
    return [
        ((x0, y0, z), (x1, y0, z), (x1, y1, z)),
        ((x0, y0, z), (x1, y1, z), (x0, y1, z)),
    ]


def _downward_quad(x0, x1, y0, y1, z):
    """A quad on the z=`z` plane facing -Z (an underside)."""
    return [
        ((x0, y0, z), (x1, y1, z), (x1, y0, z)),
        ((x0, y0, z), (x0, y1, z), (x1, y1, z)),
    ]


def _front_quad(x0, x1, z0, z1, y):
    """A quad on the y=`y` plane facing -Y (the "front" wall of a body)."""
    return [
        ((x0, y, z0), (x1, y, z0), (x1, y, z1)),
        ((x0, y, z0), (x1, y, z1), (x0, y, z1)),
    ]


def _tilted_strip(x0, x1, t0, t1):
    """A 45-degree planar strip, rising in +y and +z together, so its
    own-normal offset is constant while its world Z is not."""
    return [
        ((x0, t0, t0), (x1, t0, t0), (x1, t1, t1)),
        ((x0, t0, t0), (x1, t1, t1), (x0, t1, t1)),
    ]


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def test_selects_a_coasters_visible_top_plane(tmp_path):
    path = _write_stl(tmp_path / "coaster.stl", _upward_quad(-45, 45, -45, 45, 7))
    face = resolve_decoratable_face(path)

    assert face["face_name"] == "top"
    assert face["normal"] == (0.0, 0.0, 1.0)
    assert face["center"][2] == pytest.approx(7, abs=0.005)
    assert face["plane_min"] == pytest.approx(7, abs=0.005)
    assert face["plane_max"] == pytest.approx(7, abs=0.005)


def test_selects_a_trays_interior_floor_instead_of_its_raised_rim(tmp_path):
    floor = _upward_quad(-57.6, 57.6, -37.6, 37.6, 2.4)
    rim = (
        _upward_quad(-60, 60, 37.6, 40, 20)
        + _upward_quad(-60, 60, -40, -37.6, 20)
        + _upward_quad(-60, -57.6, -37.6, 37.6, 20)
        + _upward_quad(57.6, 60, -37.6, 37.6, 20)
    )
    path = _write_stl(tmp_path / "tray.stl", floor + rim)
    face = resolve_decoratable_face(path)

    assert face["face_name"] == "top"
    assert face["center"][2] == pytest.approx(2.4, abs=0.005)
    assert face["plane_min"] == pytest.approx(2.4, abs=0.005)
    assert face["plane_max"] == pytest.approx(2.4, abs=0.005)
    assert face["plane_max"] != pytest.approx(20, abs=0.005)


def test_selects_a_hollow_bodys_outer_wall_not_its_interior_wall(tmp_path):
    # A hollow body's outer front wall and the interior surface of its
    # back wall both face -Y.  Merged, they average to a centroid at the
    # body's Y midline — a carve placed there touches no material and the
    # boolean silently no-ops.  Area cannot break the tie either: the
    # front wall has a window, so the unbroken INTERIOR plane is genuinely
    # the larger of the two (2400mm2 vs 1600mm2).  Only "outermost along
    # the face's own normal" gets it right.
    outer_front_wall = (
        _front_quad(-30, 30, 0, 10, 0)
        + _front_quad(-30, 30, 30, 40, 0)
        + _front_quad(-30, -20, 10, 30, 0)
        + _front_quad(20, 30, 10, 30, 0)
    )
    interior_back_wall = _front_quad(-30, 30, 0, 40, 60)
    path = _write_stl(tmp_path / "cup.stl", outer_front_wall + interior_back_wall)
    face = resolve_decoratable_face(path)

    assert face["face_name"] == "front"
    assert face["center"][1] == pytest.approx(0, abs=0.005)
    assert face["center"][1] != pytest.approx(36, abs=0.005)  # merged-average midline
    assert face["area_mm2"] == pytest.approx(1600, abs=0.5)


def test_mirrors_the_floor_rule_on_a_bottom_face(tmp_path):
    # The bottom half of the shared selector — the mirror of the tray
    # rule.  Two equal downward-facing planes: the outer underside at z=0
    # and the ceiling of a recess at z=5.  The resolver takes the HIGHEST
    # for bottom, exactly as it takes the lowest for top.  Equal areas
    # mean nothing but that rule can break the tie.
    underside = _downward_quad(-20, 20, -12.5, 12.5, 0)
    recess_ceiling = _downward_quad(-20, 20, -12.5, 12.5, 5)
    path = _write_stl(tmp_path / "recess.stl", underside + recess_ceiling)
    face = resolve_decoratable_face(path)

    assert face["face_name"] == "bottom"
    assert face["center"][2] == pytest.approx(5, abs=0.005)
    assert face["area_mm2"] == pytest.approx(1000, abs=0.5)


def test_keeps_a_tilted_planar_face_whole_instead_of_shredding_it(tmp_path):
    # Splitting on world Z would cut this 45-degree canvas at every strip
    # seam (centroids 1.67mm apart, then 3.33mm — over the 1.5mm gap) and
    # keep a one-eighth sliver.  Keying on the group's OWN normal gives
    # every triangle the same offset, so the canvas stays one surface.
    triangles = []
    for i in range(8):
        triangles += _tilted_strip(-30, 30, i * 5, (i + 1) * 5)
    path = _write_stl(tmp_path / "canvas.stl", triangles)
    face = resolve_decoratable_face(path)

    # 60mm wide x 40*sqrt(2)mm along the slope, undivided.
    assert face["area_mm2"] == pytest.approx(60 * 40 * math.sqrt(2), abs=0.5)
    assert face["plane_max"] - face["plane_min"] == pytest.approx(0, abs=0.005)
    assert face["center"][2] == pytest.approx(20, abs=0.005)


# ---------------------------------------------------------------------------
# Door order and 3MF
# ---------------------------------------------------------------------------


def _tube(tmp_path):
    """A 60x60x80 square tube, open top and bottom: four outer walls, four
    inner walls, NO top- or bottom-facing geometry at all."""
    x0, x1, y0, y1, z0, z1, t = 0, 60, 0, 60, 0, 80, 3
    i0, i1, j0, j1 = x0 + t, x1 - t, y0 + t, y1 - t

    def quad(a, b, c, d):
        return [(a, b, c), (a, c, d)]

    tris = quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))
    tris += quad((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1))
    tris += quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))
    tris += quad((x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1))
    tris += quad((i0, j0, z0), (i0, j0, z1), (i1, j0, z1), (i1, j0, z0))
    tris += quad((i1, j1, z0), (i1, j1, z1), (i0, j1, z1), (i0, j1, z0))
    tris += quad((i1, j0, z0), (i1, j0, z1), (i1, j1, z1), (i1, j1, z0))
    tris += quad((i0, j1, z0), (i0, j1, z1), (i0, j0, z1), (i0, j0, z0))
    return _write_stl(tmp_path / "tube.stl", tris)


def test_auto_prefers_top_and_only_then_falls_back(tmp_path):
    # A tube with no top-facing geometry exercises the fallback door: the
    # named "top" attempt raises, and largest-flat resolves the front
    # outer wall — NOT the best face a smarter picker might choose.  The
    # resolver's contract is the doors' order, verbatim.
    path = _tube(tmp_path)
    with pytest.raises(ValueError):
        find_named_face(path, "top")

    face = resolve_decoratable_face(path)
    assert face["face_name"] == "front"
    assert face["center"] == (30.0, 0.0, 40.0)
    assert face["area_mm2"] == pytest.approx(4800, abs=0.5)


def test_named_face_skips_the_top_preference(tmp_path):
    path = _tube(tmp_path)
    face = resolve_decoratable_face(path, "back")
    assert face["face_name"] == "back"
    assert face["center"][1] == pytest.approx(60, abs=0.005)


def test_3mf_build_transform_is_applied(tmp_path):
    # A 3MF places its objects via per-item build transforms.  A face
    # measured on untransformed vertices would sit in the wrong spot, so
    # pin that the loader lands the face in TRANSFORMED space: the same
    # tube shifted (+100, +200, +300) must resolve the same front wall at
    # the shifted position, including the plane band along the normal.
    np = pytest.importorskip("numpy")
    trimesh = pytest.importorskip("trimesh")

    stl_path = _tube(tmp_path)
    mesh = trimesh.load(stl_path, force="mesh", process=False)
    transform = np.eye(4)
    transform[:3, 3] = [100.0, 200.0, 300.0]
    scene = trimesh.Scene()
    scene.add_geometry(mesh, transform=transform)
    threemf_path = tmp_path / "tube_shifted.3mf"
    scene.export(threemf_path)

    face = resolve_decoratable_face(str(threemf_path))
    assert face["face_name"] == "front"
    assert face["center"][0] == pytest.approx(130.0, abs=0.005)
    assert face["center"][1] == pytest.approx(200.0, abs=0.005)
    assert face["center"][2] == pytest.approx(340.0, abs=0.005)
    # front normal is (0,-1,0); the shifted wall at y=200 projects to -200.
    assert face["plane_max"] == pytest.approx(-200.0, abs=0.005)
