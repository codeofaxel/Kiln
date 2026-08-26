"""Regression: side-face decoration on a HOLLOW body must land on the wall.

A hollow pen cup's outer FRONT wall (y=0) and the interior surface of its
BACK wall both face −Y.  Grouped by normal alone they merged into one
"front" face whose area-weighted centroid floats in the body's interior,
so the deboss cutter was placed off the material and ``difference()``
removed nothing — while the tool still reported success (measured
2026-08-25 on a 72mm pen cup: cutter at the mesh Y midline instead of the
y=0 wall; on a centered mesh, near y=0 instead of the y=−36 wall).

The engine now splits same-normal groups into distinct parallel planes
(clustering by offset along the group's own normal) and, for side faces,
picks the outermost plane — the exterior wall.  These tests pin:

* face resolution to the true wall plane on hollow bodies, in both the
  positive-quadrant and centered coordinate conventions;
* the no-op detector (`meshes_geometrically_identical`) that stops an
  unchanged mesh from shipping as a successful decoration;
* the curved-body hint when a named side face resolves to a narrow facet;
* (slow, OpenSCAD) a front-face deboss on a hollow cup actually removes
  volume.
"""
from __future__ import annotations

import math
import struct

import pytest

from kiln.emboss_generator import (
    mesh_signature,
    meshes_geometrically_identical,
)
from kiln.surface_intelligence import find_named_face

# ---------------------------------------------------------------------------
# Mesh builders (binary STL, no deps)
# ---------------------------------------------------------------------------


def _box_tris(lo, hi, invert=False):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [
        (x0, y0, z0), (x1, y0, z0), (x0, y1, z0), (x1, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x0, y1, z1), (x1, y1, z1),
    ]
    quads = [
        [0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4],
        [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5],
    ]
    tris = []
    for q in quads:
        a, b, c, d = (v[i] for i in q)
        if invert:
            tris += [(a, c, b), (a, d, c)]
        else:
            tris += [(a, b, c), (a, c, d)]
    return tris


def _write_stl(path, tris, shift=(0.0, 0.0, 0.0)):
    with open(path, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for x, y, z in t:
                f.write(struct.pack(
                    "<3f", x + shift[0], y + shift[1], z + shift[2]
                ))
            f.write(struct.pack("<H", 0))
    return path


def _hollow_cup_tris():
    """72x72x92 outer shell with a 3mm-wall interior cavity."""
    return _box_tris((0, 0, 0), (72, 72, 92)) + _box_tris(
        (3, 3, 3), (69, 69, 92.001), invert=True
    )


@pytest.fixture()
def cup_stl(tmp_path):
    return _write_stl(str(tmp_path / "cup.stl"), _hollow_cup_tris())


@pytest.fixture()
def cup_centered_stl(tmp_path):
    return _write_stl(
        str(tmp_path / "cup_centered.stl"),
        _hollow_cup_tris(),
        shift=(-36.0, -36.0, 0.0),
    )


# ---------------------------------------------------------------------------
# Face resolution on hollow bodies
# ---------------------------------------------------------------------------


class TestHollowBodySideFaces:
    # face_name -> (axis index, wall coordinate) for the positive-quadrant cup
    _WALLS = {
        "front": (1, 0.0),
        "back": (1, 72.0),
        "left": (0, 0.0),
        "right": (0, 72.0),
    }

    @pytest.mark.parametrize("face_name", sorted(_WALLS))
    def test_side_face_center_is_on_the_exterior_wall(self, cup_stl, face_name):
        axis, wall = self._WALLS[face_name]
        face = find_named_face(cup_stl, face_name)
        assert face["center"][axis] == pytest.approx(wall, abs=0.05), (
            f"{face_name} face centroid must sit on the exterior wall "
            f"plane, not inside the hollow body"
        )

    def test_front_face_on_centered_mesh(self, cup_centered_stl):
        # Same cup translated to [-36,36]^2 — the wall moves, the fix
        # must follow actual geometry, not any coordinate convention.
        face = find_named_face(cup_centered_stl, "front")
        assert face["center"][1] == pytest.approx(-36.0, abs=0.05)

    def test_auto_face_never_lands_on_an_interior_plane(self, cup_stl):
        # Both doors funnel through the same plane selector — the auto
        # door kept the interior-wall bug for a while after the named
        # door lost it.
        from kiln.surface_intelligence import find_largest_flat_face

        face = find_largest_flat_face(cup_stl)
        name = face["face_name"]
        if name in self._WALLS:
            axis, wall = self._WALLS[name]
            assert face["center"][axis] == pytest.approx(wall, abs=0.05)

    def test_smaller_outer_wall_still_beats_larger_interior_plane(
        self, tmp_path
    ):
        # A front wall with a window cut through it is SMALLER than the
        # unbroken interior back-wall surface behind it.  Area must not
        # be the primary key for side faces — the exterior plane wins.
        def _quad(a, b, c, d):
            return [(a, b, c), (a, c, d)]

        t = []
        for x0, x1, z0, z1 in [
            (0, 72, 0, 16), (0, 72, 76, 92), (0, 11, 16, 76), (61, 72, 16, 76),
        ]:
            t += _quad((x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1))
        # interior back-wall surface (faces −Y, y=69), unbroken and larger
        t += _quad((3, 69, 3), (69, 69, 3), (69, 69, 92), (3, 69, 92))
        t += _quad((0, 0, 0), (72, 0, 0), (72, 72, 0), (0, 72, 0))
        stl = _write_stl(str(tmp_path / "windowed.stl"), t)

        face = find_named_face(stl, "front")
        assert face["center"][1] == pytest.approx(0.0, abs=0.05)

        from kiln.surface_intelligence import find_largest_flat_face

        auto = find_largest_flat_face(stl)
        if auto["face_name"] == "front":
            assert auto["center"][1] == pytest.approx(0.0, abs=0.05)

    def test_front_face_spans_the_full_wall(self, cup_stl):
        face = find_named_face(cup_stl, "front")
        assert face["width_mm"] == pytest.approx(72.0, abs=0.1)
        assert face["height_mm"] == pytest.approx(92.0, abs=0.1)
        assert face.get("curvature_warning") is None


class TestCurvedBodyHint:
    def test_cylinder_front_carries_curvature_warning(self, tmp_path):
        # Open-top faceted cylinder shell: "front" is one narrow facet.
        n, r, h = 64, 36.0, 92.0
        tris = []
        for i in range(n):
            a0 = 2 * math.pi * i / n
            a1 = 2 * math.pi * (i + 1) / n
            p0 = (r * math.cos(a0), r * math.sin(a0))
            p1 = (r * math.cos(a1), r * math.sin(a1))
            a = (p0[0], p0[1], 0.0)
            b = (p1[0], p1[1], 0.0)
            c = (p1[0], p1[1], h)
            d = (p0[0], p0[1], h)
            tris += [(a, b, c), (a, c, d), ((0.0, 0.0, 0.0), b, a)]
        cyl = _write_stl(str(tmp_path / "cyl.stl"), tris)

        face = find_named_face(cyl, "front")
        assert face.get("curvature_warning"), (
            "a named side face on a curved shell must warn that it "
            "resolved to a facet"
        )
        assert face["width_mm"] < 0.5 * (2 * r)


# ---------------------------------------------------------------------------
# No-op detection
# ---------------------------------------------------------------------------


class TestNoopDetection:
    def test_identical_meshes_are_flagged(self, cup_stl, tmp_path):
        import shutil

        copy = str(tmp_path / "copy.stl")
        shutil.copy(cup_stl, copy)
        assert meshes_geometrically_identical(cup_stl, copy) is True

    def test_changed_volume_is_not_flagged(self, cup_stl, tmp_path):
        # Same triangle count, thinner cup: volume differs -> not a no-op.
        thinner = _box_tris((0, 0, 0), (72, 70, 92)) + _box_tris(
            (3, 3, 3), (69, 67, 92.001), invert=True
        )
        other = _write_stl(str(tmp_path / "thin.stl"), thinner)
        assert meshes_geometrically_identical(cup_stl, other) is False

    def test_unparseable_mesh_is_not_branded_noop(self, cup_stl, tmp_path):
        bogus = tmp_path / "bogus.stl"
        bogus.write_bytes(b"not an stl")
        assert meshes_geometrically_identical(cup_stl, str(bogus)) is False

    def test_signature_reports_count_and_volume(self, cup_stl):
        count, volume = mesh_signature(cup_stl)
        assert count == 24
        # 72*72*92 minus 66x66x89.001 cavity ~= 89_233 mm^3
        assert volume == pytest.approx(72 * 72 * 92 - 66 * 66 * 89.001, rel=0.01)


# ---------------------------------------------------------------------------
# Full pipeline (slow, requires OpenSCAD): deboss must remove volume
# ---------------------------------------------------------------------------

try:
    from kiln.emboss_generator import _find_openscad

    _find_openscad()
    _OPENSCAD = True
except Exception:  # pragma: no cover — OpenSCAD not installed
    _OPENSCAD = False


@pytest.mark.slow
@pytest.mark.skipif(not _OPENSCAD, reason="OpenSCAD not installed")
def test_front_deboss_on_hollow_cup_removes_volume(cup_stl, tmp_path):
    from kiln.emboss_generator import compile_embossed_model, generate_emboss_scad

    face = find_named_face(cup_stl, "front")
    scad = generate_emboss_scad(
        model_path=cup_stl,
        content_info={"type": "openscad_text", "text": "KILN"},
        face=face,
        output_dir=str(tmp_path),
        depth_mm=1.25,
        mode="deboss",
        scale=0.55,
    )
    comp = compile_embossed_model(
        scad["scad_path"], scad["output_stl_path"], timeout=600
    )
    assert comp.get("success"), comp.get("error")

    _, vol_in = mesh_signature(cup_stl)
    _, vol_out = mesh_signature(scad["output_stl_path"])
    assert vol_out < vol_in - 1.0, (
        f"deboss removed no material (volume {vol_in:.1f} -> {vol_out:.1f})"
    )
    assert not meshes_geometrically_identical(cup_stl, scad["output_stl_path"])
