"""kiln.triangle_index finds every triangle that can answer, and the printability
searches built on it answer exactly as testing every triangle does.

Covered: the XY grid's box and point queries against a brute-force box test
(slivers, huge and degenerate triangles, points on corners), its one entry
per triangle, the ray boxes against every-triangle casts (shared edges and
corners, axis-parallel rays, the angle filter), and the three searches -- the
part below an overhang, the inside-the-solid test, the loose pieces -- against
the every-triangle versions, down to the whole report.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

import kiln.printability as printability
from kiln.fusion_check import _MAX_PAIRS, _judge, judge_reading
from kiln.mesh_bodies import read_triangles
from kiln.triangle_index import RayBoxes, XYIndex

trimesh = pytest.importorskip("trimesh")


def _soup(seed: int = 0) -> np.ndarray:
    """Triangles of every awkward kind: small, long slivers, huge, zero-area,
    vertical, on both sides of the origin."""
    rng = np.random.default_rng(seed)
    small = rng.uniform(-60, 60, (3000, 1, 3)) + rng.uniform(-1, 1, (3000, 3, 3))
    slivers = rng.uniform(-60, 60, (400, 1, 3)) + rng.uniform(-40, 40, (400, 1, 3)) * np.array([[0.0], [1.0], [1.001]])
    huge = rng.uniform(-200, 200, (20, 3, 3))
    flat = rng.uniform(-60, 60, (50, 1, 3)).repeat(3, axis=1)
    vertical = rng.uniform(-60, 60, (100, 3, 3))
    vertical[:, :, :2] = vertical[:, :1, :2] + np.array([0.0, 0.5, 1.0])[None, :, None] * rng.uniform(-5, 5, (100, 1, 2))
    return np.concatenate([small, slivers, huge, flat, vertical]).astype(np.float64)


def _closed_meshes() -> list[np.ndarray]:
    sphere = trimesh.creation.icosphere(subdivisions=5, radius=20.0)
    boxes = trimesh.util.concatenate([
        trimesh.creation.box(extents=(30.0, 30.0, 30.0)),
        trimesh.creation.box(extents=(10.0, 10.0, 50.0)),
    ])
    return [np.asarray(m.triangles, dtype=np.float64) for m in (sphere, boxes)]


def _rays(tris: np.ndarray, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Random rays, rays from corner to corner (through shared edges and
    corners), and axis-parallel rays."""
    rng = np.random.default_rng(seed)
    corners = tris.reshape(-1, 3)
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    o1 = rng.uniform(lo, hi, (300, 3))
    d1 = rng.normal(size=(300, 3))
    a = corners[rng.integers(0, len(corners), 200)]
    b = corners[rng.integers(0, len(corners), 200)]
    keep = np.linalg.norm(b - a, axis=1) > 1e-6
    o2 = a[keep] - 0.5 * (b[keep] - a[keep])
    d2 = b[keep] - a[keep]
    o3 = rng.uniform(lo, hi, (60, 3))
    d3 = np.repeat(np.eye(3), 20, axis=0) * rng.choice([-1.0, 1.0], (60, 1))
    origins = np.concatenate([o1, o2, o3])
    directions = np.concatenate([d1, d2, d3])
    return origins, directions / np.linalg.norm(directions, axis=1)[:, None]


class TestXYIndex:
    def test_a_box_query_returns_exactly_the_boxes_it_overlaps(self):
        tris = _soup()
        index = XYIndex(tris)
        rng = np.random.default_rng(2)
        for _ in range(300):
            x0, y0 = rng.uniform(-250, 250, 2)
            w, h = rng.exponential(10.0, 2)
            x1, y1 = x0 + w, y0 + h
            want = np.flatnonzero(
                (index.lo[:, 0] <= x1) & (index.hi[:, 0] >= x0) & (index.lo[:, 1] <= y1) & (index.hi[:, 1] >= y0)
            )
            assert np.array_equal(index.in_box(x0, y0, x1, y1), want)

    def test_point_queries_match_the_box_query_at_each_point(self):
        tris = _soup()
        index = XYIndex(tris)
        rng = np.random.default_rng(3)
        points = np.concatenate([
            rng.uniform(-250, 250, (200, 2)),
            tris[rng.integers(0, len(tris), 100), 0, :2],
            tris[rng.integers(0, len(tris), 100)].mean(axis=1)[:, :2],
        ])
        point_of, tri_of = index.at_points(points)
        bounds = np.searchsorted(point_of, np.arange(len(points) + 1))
        for j, (x, y) in enumerate(points):
            assert np.array_equal(tri_of[bounds[j]:bounds[j + 1]], index.in_box(x, y, x, y))

    def test_every_triangle_is_filed_once_whatever_its_shape(self):
        tris = _soup()
        assert XYIndex(tris).entries == len(tris)

    def test_a_mesh_of_mostly_zero_size_triangles_is_still_answered_exactly(self):
        rng = np.random.default_rng(6)
        points = rng.uniform(-500, 500, (10_000, 1, 3)).repeat(3, axis=1)
        normal = rng.uniform(-500, 500, (100, 1, 3)) + rng.uniform(-2, 2, (100, 3, 3))
        tris = np.concatenate([points, normal])
        index = XYIndex(tris)
        for _ in range(100):
            x0, y0 = rng.uniform(-520, 520, 2)
            x1, y1 = x0 + rng.exponential(3.0), y0 + rng.exponential(3.0)
            want = np.flatnonzero(
                (index.lo[:, 0] <= x1) & (index.hi[:, 0] >= x0) & (index.lo[:, 1] <= y1) & (index.hi[:, 1] >= y0)
            )
            assert np.array_equal(index.in_box(x0, y0, x1, y1), want)
        assert index.entries == len(tris)

    def test_an_empty_mesh_has_nothing_anywhere(self):
        index = XYIndex(np.empty((0, 3, 3)))
        assert index.in_box(-1.0, -1.0, 1.0, 1.0).size == 0
        assert index.at_points(np.zeros((3, 2)))[0].size == 0


class TestRayBoxes:
    @pytest.mark.parametrize("dot", [None, 0.85])
    @pytest.mark.parametrize("which", ["soup", "sphere", "boxes"])
    def test_casts_through_the_boxes_hit_what_every_triangle_does(self, dot, which):
        tris = {"soup": _soup(), "sphere": _closed_meshes()[0], "boxes": _closed_meshes()[1]}[which]
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        origins, directions = _rays(tris)
        every = printability._raycast_min_distances(
            origins, directions, v0, e1, e2, min_abs_perpendicular_dot=dot, return_hit=True,
        )
        boxed = printability._raycast_min_distances(
            origins, directions, v0, e1, e2, min_abs_perpendicular_dot=dot, return_hit=True,
            boxes=RayBoxes(v0, e1, e2),
        )
        assert np.isfinite(every[0]).sum() > 50
        assert np.array_equal(every[0], boxed[0])
        assert np.array_equal(every[1], boxed[1])

    def test_no_triangles_no_candidates(self):
        boxes = RayBoxes(np.empty((0, 3)), np.empty((0, 3)), np.empty((0, 3)))
        assert [c.size for c in boxes.candidates(np.zeros((2, 3)), np.ones((2, 3)))] == [0, 0]
        assert [c.size for c in boxes.candidates(np.zeros((2, 3)), np.ones((2, 3)), reach=np.inf)] == [0, 0]

    def test_a_box_query_holds_every_triangle_whose_box_overlaps(self):
        tris = _soup()
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        boxes = RayBoxes(v0, e1, e2)
        lo, hi = tris.min(axis=1), tris.max(axis=1)
        rng = np.random.default_rng(9)
        for _ in range(200):
            low = rng.uniform(-220, 220, 3)
            high = low + rng.exponential(8.0, 3)
            want = np.flatnonzero(np.all(hi >= low, axis=1) & np.all(lo <= high, axis=1))
            got = boxes.in_box(low, high)
            assert np.all(np.diff(got) > 0)
            assert np.array_equal(got[np.all(hi[got] >= low, axis=1) & np.all(lo[got] <= high, axis=1)], want)

    @pytest.mark.parametrize("window", [0.5, 5.0, 50.0, np.inf])
    @pytest.mark.parametrize("which", ["soup", "sphere", "boxes"])
    def test_lines_through_the_boxes_cross_what_every_triangle_does(self, window, which):
        """Both ways along each line, within the window: the same nearest
        crossings, the same inside-or-out."""
        tris = {"soup": _soup(), "sphere": _closed_meshes()[0], "boxes": _closed_meshes()[1]}[which]
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        points, directions = _rays(tris, seed=7)
        # And lines from just off the surface, so a short window crosses too.
        rng = np.random.default_rng(8)
        off = tris[rng.integers(0, len(tris), 300)].mean(axis=1) + rng.normal(0.0, 0.1, (300, 3))
        aim = rng.normal(size=(300, 3))
        points = np.concatenate([points, off])
        directions = np.concatenate([directions, aim / np.linalg.norm(aim, axis=1)[:, None]])
        every = printability._nearest_crossings(points, directions, v0, e1, e2, window=window)
        boxed = printability._nearest_crossings(
            points, directions, v0, e1, e2, window=window,
            candidates=list(RayBoxes(v0, e1, e2).candidates(points, directions, reach=window)),
        )
        assert np.isfinite(every[1]).sum() > 20 and np.isfinite(every[2]).sum() > 20
        for a, b in zip(every, boxed, strict=True):
            assert np.array_equal(a, b)


class _EveryFace:
    """An index that hands back every face for every point: the search as it
    was before the index."""

    def __init__(self, tris: np.ndarray) -> None:
        self._n = len(tris)

    def at_points(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return np.repeat(np.arange(len(xy)), self._n), np.tile(np.arange(self._n), len(xy))


class TestTheSearchesAnswerAsBefore:
    def test_the_part_below_an_overhang_reads_the_same(self):
        from tests.test_printability_reads_what_can_answer import _as_list, _blocks_over_a_slab

        organic = trimesh.creation.icosphere(subdivisions=4, radius=15.0)
        organic.vertices += np.random.default_rng(4).normal(0.0, 0.6, organic.vertices.shape)
        organic.apply_translation((0.0, 0.0, 15.0))
        seen: set[bool] = set()
        for tris in (_blocks_over_a_slab(6), np.asarray(organic.triangles, dtype=np.float64)):
            triangles = _as_list(tris)
            z_min = float(tris[:, :, 2].min())
            regions = printability._analyze_downward_regions(triangles, z_min, layer_height=0.2, tris=tris)
            indexed = printability._summarize_regions(triangles, regions, z_min, layer_height=0.2, tris=tris)
            with mock.patch.object(printability, "XYIndex", _EveryFace):
                every = printability._summarize_regions(triangles, regions, z_min, layer_height=0.2, tris=tris)
            assert len(regions) > 5
            assert [s.to_dict() for s in indexed] == [s.to_dict() for s in every]
            seen |= {s.over_model for s in every}
        # Both answers were exercised: regions over the part and over the plate.
        assert seen == {True, False}

    def test_inside_the_solid_counts_as_every_triangle_does(self):
        tris = _closed_meshes()[0]
        assert len(tris) >= printability._INSIDE_INDEX_MIN_TRIANGLES
        rng = np.random.default_rng(5)
        points = np.concatenate([
            rng.uniform(-25, 25, (400, 3)),
            tris[rng.integers(0, len(tris), 100), 0],
            tris[rng.integers(0, len(tris), 100)].mean(axis=1),
        ])
        test = printability._InsideMeshTest(tris)
        every = (test._crossings(points, slice(None)) % 2) == 1
        assert every.any() and not every.all()
        assert np.array_equal(test(points), every)

    def test_loose_pieces_are_judged_as_before(self):
        parts = []
        for i in range(12):
            cube = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
            cube.apply_translation((1.5 * i, 0.0, 1.0 + (0.6 if i % 3 == 0 else 0.0)))
            parts.append(cube)
        tris = np.asarray(trimesh.util.concatenate(parts).triangles, dtype=np.float64)
        triangles = [tuple(map(tuple, t)) for t in tris.tolist()]
        reading = read_triangles(triangles)
        bodies = [
            trimesh.Trimesh(
                vertices=tris[reading.face_piece == i].reshape(-1, 3),
                faces=np.arange(int((reading.face_piece == i).sum()) * 3).reshape(-1, 3),
                process=True,
            )
            for i in range(reading.piece_count)
        ]
        before = _judge(bodies, list(reading.solid_of), expect_single_body=None, max_pairs=_MAX_PAIRS)
        assert judge_reading(triangles, reading) == before

    def test_a_reading_of_a_different_mesh_is_refused(self):
        triangles = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))] * 4
        reading = read_triangles(triangles[:2])
        with pytest.raises(IndexError):
            judge_reading(triangles, reading)

    def test_the_whole_report_does_not_change(self, tmp_path):
        from tests.test_printability_reads_what_can_answer import _blocks_over_a_slab

        blocks = _blocks_over_a_slab(8)
        sphere = trimesh.creation.icosphere(subdivisions=5, radius=10.0)
        sphere.apply_translation((0.0, 0.0, 30.0))
        mesh = trimesh.util.concatenate([
            trimesh.Trimesh(vertices=blocks.reshape(-1, 3), faces=np.arange(len(blocks) * 3).reshape(-1, 3)),
            sphere,
        ])
        path = tmp_path / "part.stl"
        mesh.export(path)
        indexed = printability.analyze_printability(str(path), material="pla").to_dict()
        with mock.patch.object(printability, "_RAY_INDEX_MIN_TARGETS", 10**12), \
                mock.patch.object(printability, "_INSIDE_INDEX_MIN_TRIANGLES", 10**12):
            every = printability.analyze_printability(str(path), material="pla").to_dict()
        assert indexed == every


class TestATangledMeshCostsNoMoreThanBefore:
    """A mesh can be tangled past any real part (huge triangles overlapping
    everywhere), so a ray meets every box.  Reading every triangle must not
    make such a mesh cost more than it did when every big mesh was read
    from a sample."""

    @staticmethod
    def _soup_and_lines():
        tris = _soup()
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        origins, directions = _rays(tris)
        return v0, e1, e2, origins, directions, RayBoxes(v0, e1, e2)

    def test_a_cast_tests_a_ray_against_no_more_than_the_cap(self, monkeypatch):
        v0, e1, e2, origins, directions, boxes = self._soup_and_lines()
        cap = 64
        assert max(c.size for c in boxes.candidates(origins, directions)) > cap
        monkeypatch.setattr(printability, "_RAY_CANDIDATE_CAP", cap)
        tested = []
        real = printability._raycast_min_distances

        def counted(o, d, a, b, c, **kw):
            if kw.get("boxes") is None:
                tested.append(a.shape[0])
            return real(o, d, a, b, c, **kw)

        monkeypatch.setattr(printability, "_raycast_min_distances", counted)
        printability._raycast_min_distances(origins, directions, v0, e1, e2, return_hit=True, boxes=boxes)
        assert tested and max(tested) <= cap

    def test_a_line_reading_that_cannot_fit_stops_counting_and_keeps_nothing(self, monkeypatch):
        v0, e1, e2, origins, directions, boxes = self._soup_and_lines()
        sizes = [c.size for c in boxes.candidates(origins, directions, reach=50.0)]
        busy = np.flatnonzero(np.asarray(sizes) > 0)
        origins, directions = origins[busy], directions[busy]
        made = []
        real = boxes.candidates

        def counting(*args, **kwargs):
            for cand in real(*args, **kwargs):
                made.append(cand.size)
                yield cand

        monkeypatch.setattr(boxes, "candidates", counting)
        budget = printability._CornerBudget(reads=sizes[busy[0]])  # room for the first point only
        found = printability._crossings_read(origins, directions, (v0, e1, e2, boxes), window=50.0, budget=budget)
        assert found is None and budget.left == 0
        assert len(made) == 2, "counting went on past the point that did not fit"

    def test_a_line_point_past_the_cap_is_not_read(self, monkeypatch):
        v0, e1, e2, origins, directions, boxes = self._soup_and_lines()
        biggest = max(c.size for c in boxes.candidates(origins, directions, reach=50.0))
        monkeypatch.setattr(printability, "_RAY_CANDIDATE_CAP", biggest - 1)
        budget = printability._CornerBudget(reads=10**12)
        found = printability._crossings_read(origins, directions, (v0, e1, e2, boxes), window=50.0, budget=budget)
        assert found is None and budget.left == 0
