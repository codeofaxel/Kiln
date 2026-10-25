"""The overhang markers are one per overhanging AREA, not sample points.

Until 2026-10-24 ``worst_regions`` held the first ten overhanging triangles
in file order, sorted by angle, cut to five.  A one-cap mushroom therefore
carried five points on one underside (the web chip said "4 overhang areas"
after its own near-duplicate merge), a finer tessellation moved the points
around, and the sentence counted triangles the slicer never counts.

The rule now: overhanging triangles that share an edge are one region.  A
region carries its area-weighted centroid, its steepest angle and its area
in mm2; the five largest by area are reported, largest first.  A one-cap
mushroom is one region at any tessellation, a T-shape is two, and a part
with no overhang has none.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")
from trimesh.creation import box, cone, cylinder  # noqa: E402

from kiln.printability import _analyze_overhangs, analyze_printability  # noqa: E402


def _mushroom(sections: int) -> trimesh.Trimesh:
    """A 5 mm stem under a 15 mm cap: one annular underside."""
    stem = cylinder(radius=5, height=20, sections=sections)
    stem.apply_translation([0, 0, 10])
    cap = cylinder(radius=15, height=5, sections=sections)
    cap.apply_translation([0, 0, 22.5])
    return stem.union(cap)


def _t_shape() -> trimesh.Trimesh:
    """A 10 mm stem under a 40 mm bar: two undersides, one each side."""
    stem = box(extents=[10, 10, 30])
    stem.apply_translation([0, 0, 15])
    bar = box(extents=[40, 10, 10])
    bar.apply_translation([0, 0, 35])
    return stem.union(bar)


def _write(tmp_path: Path, mesh: trimesh.Trimesh, name: str) -> str:
    path = tmp_path / name
    mesh.export(str(path))
    return str(path)


def _regions(mesh: trimesh.Trimesh) -> list[dict[str, float]]:
    z_min = float(mesh.bounds[0][2])
    return _analyze_overhangs(mesh.triangles.tolist(), max_overhang_angle=45.0, z_min=z_min).worst_regions


@pytest.mark.parametrize("sections", [16, 64, 256])
def test_a_one_cap_mushroom_is_one_overhang_area_at_any_tessellation(sections: int) -> None:
    regions = _regions(_mushroom(sections))

    assert len(regions) == 1, regions
    (ring,) = regions
    # The annulus between the stem and the cap's edge, as the facets draw it.
    polygon = lambda r: sections / 2 * r * r * math.sin(2 * math.pi / sections)  # noqa: E731
    assert ring["area_mm2"] == pytest.approx(polygon(15) - polygon(5), rel=0.02)
    assert ring["angle"] == 90.0
    assert (ring["x"], ring["y"]) == pytest.approx((0.0, 0.0), abs=0.5)
    assert ring["z"] == pytest.approx(20.0, abs=0.01)


def test_a_t_shape_is_two_overhang_areas_one_each_side() -> None:
    regions = _regions(_t_shape())

    assert len(regions) == 2, regions
    assert sorted(round(r["x"]) for r in regions) == [-12, 12]
    for wing in regions:
        assert wing["area_mm2"] == pytest.approx(15 * 10, rel=0.01)
        assert wing["z"] == pytest.approx(30.0, abs=0.01)


def test_a_cone_s_sloping_side_is_one_area_all_the_way_round() -> None:
    # A cone on its base, 60 deg from vertical: one sloping skirt.
    hat = cone(radius=20, height=20 / math.tan(math.radians(60)), sections=96)
    # Turned point-down, so the skirt hangs over the post below it.
    hat.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0]))
    hat.apply_translation([0, 0, 40 + 20 / math.tan(math.radians(60))])
    base = cylinder(radius=3, height=40, sections=24)
    base.apply_translation([0, 0, 20])
    regions = _regions(base.union(hat))

    assert len(regions) == 1, regions
    assert regions[0]["angle"] == pytest.approx(60.0, abs=1.0)


def test_regions_are_the_largest_first_and_at_most_five() -> None:
    # Seven caps of different widths on seven posts, in one file: seven
    # undersides.  (One plate on seven posts would be ONE underside: the
    # posts punch holes in it, but it stays joined around them.)
    caps = []
    for i, radius in enumerate((3, 4, 5, 6, 7, 8, 9)):
        post = cylinder(radius=1.5, height=20, sections=24)
        post.apply_translation([i * 25, 0, 10])
        cap = cylinder(radius=radius, height=3, sections=48)
        cap.apply_translation([i * 25, 0, 21.5])
        caps.append(post.union(cap))
    regions = _regions(trimesh.util.concatenate(caps))

    assert len(regions) == 5
    areas = [r["area_mm2"] for r in regions]
    assert areas == sorted(areas, reverse=True)
    # The five widest caps, widest first: their posts stand at x = 150 .. 50.
    assert [round(r["x"]) for r in regions] == [150, 125, 100, 75, 50]
    assert all(r["z"] == pytest.approx(20.0, abs=0.01) for r in regions)


def test_a_part_with_no_overhang_has_no_regions() -> None:
    cube = box(extents=[20, 20, 20])
    cube.apply_translation([0, 0, 10])
    assert _regions(cube) == []


def test_the_report_carries_the_regions_through_the_tool_s_engine(tmp_path: Path) -> None:
    """``analyze_printability`` is what every door and the web stage read."""
    report = analyze_printability(_write(tmp_path, _mushroom(96), "mushroom.stl"))
    regions = report.overhangs.worst_regions

    assert len(regions) == 1, regions
    assert set(regions[0]) == {"x", "y", "z", "angle", "area_mm2"}
    assert report.overhangs.needs_supports is True
