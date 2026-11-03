"""A sub-nozzle ridge standing on solid is surface detail, not a wall.

--- defects a lap found (2026-11-03; fails at 16bd6a93) ---

The 1.5.1 lap made a pen cup with a photo carved into its floor and a name
on its wall, then asked Kiln whether it prints: "Won't print as it is: a
wall measures 0.31 mm".  The 0.31 mm was one ray between two pits of the
carved photo, a ridge a millimetre tall on two millimetres of floor, and
the print door would have refused the cup Kiln had just made.  The same cup
with only the photo, or only the name, passed: the ridges were there each
time, and whether a ray landed on one depended on the mesh.  The thin-wall
rule knew walls and corners and slivers, and had no word for a ridge: a
feature the slicer drops while the part under it prints whole.

Now a chord thinner than the nozzle is first asked what stands behind it.
A feature that ends within a couple of millimetres on one side and whose
solid thickens to bulk a step down the other side is DETAIL: counted and
named as detail the print will lose, never a wall that stops the print.  A
wall is thin all the way down.
"""
from __future__ import annotations

import numpy as np
import trimesh

from kiln import printability as P


def _union(*parts):
    mesh = trimesh.boolean.union(list(parts), engine="manifold")
    assert mesh.is_volume, "fixture must be one closed solid"
    return mesh


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _plate_with_pocket():
    """30 x 30 x 3 plate with a 20 x 20 pocket 1.6 mm deep in its top: the
    floor a photo is carved into."""
    plate = _box((0, 0, 0), (30, 30, 3))
    pocket = _box((5, 5, 1.4), (25, 25, 3.01))
    return trimesh.boolean.difference([plate, pocket], engine="manifold")


def _measure(mesh, nozzle=0.4):
    tris = [tuple(tuple(float(c) for c in v) for v in mesh.triangles[i]) for i in range(len(mesh.triangles))]
    verts = [tuple(float(c) for c in v) for v in mesh.vertices]
    return P._analyze_thin_walls(tris, verts, nozzle_diameter=nozzle)


def test_a_ridge_on_a_carved_floor_is_detail_not_a_wall():
    ridge = _box((6, 14.85, 1.4), (24, 15.15, 2.4))  # 0.3 wide, 1.0 tall, on the pocket floor
    mesh = _union(_plate_with_pocket(), ridge)
    walls = _measure(mesh)
    thin = [r for r in walls.problematic_regions if r["thickness_mm"] < 0.4]
    assert not thin, f"the ridge was read as a wall: {thin}"
    assert walls.fine_detail_count > 0, "the ridge was not even seen"
    assert 0.2 < walls.fine_detail_min_mm < 0.4
    assert walls.min_wall_thickness_mm >= 1.0, walls.min_wall_thickness_mm


def test_a_free_standing_thin_wall_is_still_a_wall():
    wall = _box((6, 14.85, 3), (24, 15.15, 18))  # 0.3 wide, 15 tall, standing on the plate
    mesh = _union(_box((0, 0, 0), (30, 30, 3)), wall)
    walls = _measure(mesh)
    thin = [r for r in walls.problematic_regions if r["thickness_mm"] < 0.4]
    assert thin, "a 0.3 mm wall fifteen millimetres tall must still be a wall"
    assert walls.fine_detail_count == 0


def test_the_print_door_passes_the_ridge_and_refuses_the_wall(tmp_path):
    from kiln.printability import analyze_printability, stops

    ridge = _union(_plate_with_pocket(), _box((6, 14.85, 1.4), (24, 15.15, 2.4)))
    wall = _union(_box((0, 0, 0), (30, 30, 3)), _box((6, 14.85, 3), (24, 15.15, 18)))
    ridge.export(str(tmp_path / "ridge.stl"))
    wall.export(str(tmp_path / "wall.stl"))
    assert stops(analyze_printability(str(tmp_path / "ridge.stl"))) == []
    assert any("thinner than" in s for s in stops(analyze_printability(str(tmp_path / "wall.stl"))))
