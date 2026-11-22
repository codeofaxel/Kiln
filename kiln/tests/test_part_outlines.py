"""A part's outlines by height, measured from meshes whose answers are known."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.part_outlines import MAX_OUTLINES, PartOutlinesError, outlines_by_height  # noqa: E402


def _box(size: tuple[float, float, float], corner: tuple[float, float, float]) -> "trimesh.Trimesh":
    box = trimesh.creation.box(extents=size)
    box.apply_translation([c + s / 2.0 for c, s in zip(corner, size, strict=True)])
    return box


def _save(tmp_path: Path, *meshes: "trimesh.Trimesh", name: str = "part.stl") -> str:
    path = tmp_path / name
    trimesh.util.concatenate(list(meshes)).export(path)
    return str(path)


def _at(part: dict, z: float) -> list[dict]:
    return min(part["levels"], key=lambda level: abs(level["z_mm"] - z))["outlines"]


def test_a_box_is_one_outline_with_a_rectangles_section_properties(tmp_path):
    part = outlines_by_height(_save(tmp_path, _box((30, 20, 40), (5, 7, 0))))
    assert part["height_mm"] == pytest.approx(40.0)
    (outline,) = _at(part, 20.0)
    assert outline["area_mm2"] == pytest.approx(600.0, rel=1e-6)
    assert outline["length_mm"] == pytest.approx(100.0, rel=1e-6)
    assert outline["box"] == pytest.approx([5, 7, 35, 27])
    assert outline["centre"] == pytest.approx([20.0, 17.0])
    # b h^3 / 12 about each axis through the centre.
    assert outline["moment_min_mm4"] == pytest.approx(30 * 20**3 / 12.0, rel=1e-6)
    assert outline["moment_max_mm4"] == pytest.approx(20 * 30**3 / 12.0, rel=1e-6)


def test_heights_are_measured_up_from_the_parts_lowest_point(tmp_path):
    part = outlines_by_height(_save(tmp_path, _box((10, 10, 20), (0, 0, 55))))
    assert part["height_mm"] == pytest.approx(20.0)
    assert 0 < part["levels"][0]["z_mm"] < 1 and part["levels"][-1]["z_mm"] < 20


def test_two_legs_are_two_outlines_until_they_meet(tmp_path):
    path = _save(tmp_path, _box((5, 5, 20.1), (0, 0, 0)), _box((5, 5, 20.1), (15, 0, 0)), _box((20, 5, 10), (0, 0, 20)))
    part = outlines_by_height(path)
    assert sorted(o["area_mm2"] for o in _at(part, 10.0)) == pytest.approx([25.0, 25.0])
    assert [o["area_mm2"] for o in _at(part, 26.0)] == pytest.approx([100.0])


def test_a_hole_is_taken_out_of_the_outline_it_is_in(tmp_path):
    tube = trimesh.creation.annulus(r_min=5.0, r_max=10.0, height=20.0, sections=256)
    (outline,) = _at(outlines_by_height(_save(tmp_path, tube)), 10.0)
    assert outline["area_mm2"] == pytest.approx(math.pi * (100 - 25), rel=2e-3)
    assert outline["moment_min_mm4"] == pytest.approx(math.pi * (10**4 - 5**4) / 4.0, rel=5e-3)


def test_a_post_standing_inside_a_ring_is_its_own_outline(tmp_path):
    tube = trimesh.creation.annulus(r_min=8.0, r_max=10.0, height=20.0, sections=128)
    post = trimesh.creation.cylinder(radius=2.0, height=20.0, sections=64)
    areas = sorted(o["area_mm2"] for o in _at(outlines_by_height(_save(tmp_path, tube, post)), 10.0))
    assert areas == pytest.approx([math.pi * 4, math.pi * (100 - 64)], rel=5e-3)


def test_two_bodies_left_overlapping_are_counted_once(tmp_path):
    # A small box wholly inside a big one: the same plastic, not a second shape.
    part = outlines_by_height(_save(tmp_path, _box((30, 30, 20), (0, 0, 0)), _box((6, 6, 20), (10, 10, 0))))
    (outline,) = _at(part, 10.0)
    assert outline["area_mm2"] == pytest.approx(900.0)


def test_only_the_largest_outlines_of_a_crowded_level_are_reported_and_the_rest_counted(tmp_path):
    pins = [_box((2, 2, 10), (4.0 * i, 0, 0)) for i in range(MAX_OUTLINES + 3)]
    level = min(outlines_by_height(_save(tmp_path, *pins))["levels"], key=lambda one: abs(one["z_mm"] - 5.0))
    assert len(level["outlines"]) == MAX_OUTLINES and level["more"] == 3


def test_triangles_that_are_not_numbers_are_left_out(tmp_path, monkeypatch):
    import numpy as np

    from kiln import decoration_faces

    path = _save(tmp_path, _box((10, 10, 10), (0, 0, 0)))
    clean = np.asarray(decoration_faces.load_mesh_triangles(path), dtype=float)
    dirty = np.concatenate([clean, [[[0, 0, np.nan], [1, 0, 5], [0, 1, 5]], [[np.inf, 0, 2], [3, 0, 8], [0, 3, 8]]]])
    monkeypatch.setattr(decoration_faces, "load_mesh_triangles", lambda _path: dirty)
    part = outlines_by_height(path)
    assert part["height_mm"] == pytest.approx(10.0)
    (outline,) = _at(part, 5.0)
    assert outline["area_mm2"] == pytest.approx(100.0)


def test_a_file_that_is_not_a_mesh_is_refused_in_words(tmp_path):
    path = tmp_path / "empty.stl"
    path.write_bytes(b"")
    with pytest.raises(PartOutlinesError):
        outlines_by_height(str(path))
