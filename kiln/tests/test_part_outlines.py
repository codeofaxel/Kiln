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


def _cyl(radius: float, height: float, z: float = 0.0, x: float = 0.0, y: float = 0.0, sections: int = 32):
    rod = trimesh.creation.cylinder(radius=radius, height=height, sections=sections)
    rod.apply_translation([x, y, z + height / 2.0])
    return rod


def _faces(mesh: trimesh.Trimesh, faces) -> trimesh.Trimesh:
    return trimesh.Trimesh(vertices=mesh.vertices, faces=faces, process=False)


# Round-number boxes: every band of a box is the same one rectangle.
_SIZES = ((4, 4), (20, 20), (100, 2.4), (60, 60), (10, 30), (100, 100), (25, 25))


@pytest.mark.parametrize("height", (50, 100))
@pytest.mark.parametrize("corner", ((0, 0), (-37.5, 12.5)))
def test_every_band_of_a_round_number_box_is_its_one_rectangle(tmp_path, height, corner):
    for width, depth in _SIZES:
        part = outlines_by_height(_save(tmp_path, _box((width, depth, height), (*corner, 0))))
        for level in part["levels"]:
            (outline,) = level["outlines"]
            assert outline["area_mm2"] == pytest.approx(width * depth, rel=1e-6), (width, depth, level["z_mm"])
            assert not level["unread"]


def test_an_inside_out_file_reads_as_the_solid_it_describes(tmp_path):
    box = _box((30, 20, 40), (5, 7, 0))
    clean = outlines_by_height(_save(tmp_path, box, name="clean.stl"))
    inverted = outlines_by_height(_save(tmp_path, _faces(box, box.faces[:, ::-1]), name="inverted.stl"))
    assert inverted["levels"] == clean["levels"]
    ring = trimesh.creation.annulus(r_min=8.0, r_max=10.0, height=20.0, sections=128)
    (outline,) = _at(outlines_by_height(_save(tmp_path, _faces(ring, ring.faces[:, ::-1]), name="ring.stl")), 10.0)
    assert outline["area_mm2"] == pytest.approx(math.pi * (100 - 64), rel=5e-3)


@pytest.mark.parametrize("seed", (0, 1, 2))
def test_a_file_with_some_faces_turned_the_wrong_way_still_reads_its_holes(tmp_path, seed):
    import numpy as np

    ring = trimesh.creation.annulus(r_min=8.0, r_max=10.0, height=40.0, sections=96)
    faces = ring.faces.copy()
    turned = np.random.default_rng(seed).random(len(faces)) < 0.3
    faces[turned] = faces[turned][:, ::-1]
    part = outlines_by_height(_save(tmp_path, _faces(ring, faces)))
    read = [level for level in part["levels"] if not level["unread"]]
    assert len(read) >= 0.9 * len(part["levels"])
    for level in read:
        assert sum(o["area_mm2"] for o in level["outlines"]) == pytest.approx(math.pi * (100 - 64), rel=0.02)


def test_a_face_listed_twice_is_counted_once(tmp_path):
    import numpy as np

    box = _box((30, 20, 40), (5, 7, 0))
    clean = outlines_by_height(_save(tmp_path, box, name="clean.stl"))
    twice = _faces(box, np.vstack([box.faces, box.faces]))
    turned_corners = _faces(box, np.vstack([box.faces, np.roll(box.faces, 1, axis=1)]))
    assert outlines_by_height(_save(tmp_path, twice, name="twice.stl"))["levels"] == clean["levels"]
    assert outlines_by_height(_save(tmp_path, turned_corners, name="turned.stl"))["levels"] == clean["levels"]


def test_a_short_neck_between_two_bands_middles_is_reported(tmp_path):
    # 160 bands of 1.25 mm: the neck sits between two bands' middles.
    column = (_cyl(10, 10.7), _cyl(1.5, 1.0, z=10.7), _cyl(10, 188.3, z=11.7))
    part = outlines_by_height(_save(tmp_path, *column))
    assert min(sum(o["area_mm2"] for o in level["outlines"]) for level in part["levels"]) < 10.0


def test_a_waist_too_thin_to_report_is_unread_not_a_gap(tmp_path):
    part = outlines_by_height(_save(tmp_path, _cyl(5, 30), _cyl(0.28, 1.5, z=30), _cyl(5, 59.5, z=31.5)))
    waist = [level for level in part["levels"] if 30 < level["z_mm"] < 31.5]
    assert waist and all(level["unread"] and not level["outlines"] for level in waist)
    assert not any(level["unread"] for level in part["levels"] if not 29.5 < level["z_mm"] < 32)
    # Beside a whole column the waist's bands still say plastic went unread.
    beside = outlines_by_height(_save(
        tmp_path, _cyl(5, 30), _cyl(0.28, 1.5, z=30), _cyl(5, 59.5, z=31.5), _cyl(5, 91, x=20), name="beside.stl",
    ))
    waist = [level for level in beside["levels"] if 30 < level["z_mm"] < 31.5]
    assert waist and all(level["unread"] and len(level["outlines"]) == 1 for level in waist)


def test_a_gap_between_two_bodies_is_empty_and_not_unread(tmp_path):
    part = outlines_by_height(_save(tmp_path, _box((10, 10, 10), (0, 0, 0)), _box((10, 10, 10), (0, 0, 15))))
    gap = [level for level in part["levels"] if 10.5 < level["z_mm"] < 14.5]
    assert gap and all(not level["outlines"] and not level["unread"] for level in gap)


def test_a_missing_triangle_is_closed_across_and_its_bands_marked_unread(tmp_path):
    post = _box((3, 3, 40), (0, 0, 0))
    side = int(next(i for i, n in enumerate(post.face_normals) if n[0] > 0.99))
    part = outlines_by_height(_save(tmp_path, _faces(post, [f for i, f in enumerate(post.faces) if i != side])))
    for level in part["levels"]:
        (outline,) = level["outlines"]
        assert outline["area_mm2"] == pytest.approx(9.0, rel=1e-6)
        assert level["unread"]


def test_a_part_too_small_to_read_says_so(tmp_path):
    part = outlines_by_height(_save(tmp_path, _box((0.04, 0.04, 0.1), (0, 0, 0))))
    assert part["read"] is False and "metres" in part["reason"]
    assert all(not level["outlines"] for level in part["levels"])
    assert outlines_by_height(_save(tmp_path, _box((4, 4, 10), (0, 0, 0)), name="ok.stl"))["read"] is True


def test_every_outline_of_a_level_with_many_is_reported(tmp_path):
    posts = [_cyl(5, 80, x=14 * i, y=14 * j, sections=24) for i in range(4) for j in range(4)]
    part = outlines_by_height(_save(tmp_path, *posts, _cyl(1, 80, x=70, sections=24)))
    for level in part["levels"]:
        assert len(level["outlines"]) == 17 and level["more"] == 0


def test_a_crowded_level_keeps_its_thinnest_outline_and_counts_the_rest(tmp_path):
    posts = [_box((2, 2, 10), (4.0 * i, 0, 0)) for i in range(MAX_OUTLINES + 6)]
    part = outlines_by_height(_save(tmp_path, *posts, _box((0.6, 0.6, 10), (0, 8, 0))))
    for level in part["levels"]:
        assert len(level["outlines"]) == MAX_OUTLINES and level["more"] == 7
        assert min(o["area_mm2"] for o in level["outlines"]) == pytest.approx(0.36)


def test_the_whole_part_stays_within_its_outline_budget(tmp_path, monkeypatch):
    from kiln import part_outlines

    monkeypatch.setattr(part_outlines, "MAX_PART_OUTLINES", 100)
    posts = [_box((2, 2, 10), (4.0 * i, 0, 0)) for i in range(30)]
    part = outlines_by_height(_save(tmp_path, *posts, _box((0.6, 0.6, 10), (0, 8, 0))))
    assert sum(len(level["outlines"]) for level in part["levels"]) <= 100
    for level in part["levels"]:
        assert len(level["outlines"]) + level["more"] == 31
        assert min(o["area_mm2"] for o in level["outlines"]) == pytest.approx(0.36)


def test_two_rods_left_crossing_read_as_the_larger_on_every_band(tmp_path):
    part = outlines_by_height(_save(tmp_path, _cyl(4, 80, sections=64), _cyl(4.05, 80, x=1, sections=64)))
    for level in part["levels"]:
        (outline,) = level["outlines"]
        assert outline["area_mm2"] == pytest.approx(math.pi * 4.05**2, rel=5e-3)


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
