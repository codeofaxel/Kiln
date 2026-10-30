"""Tests for decoration face provenance — the carve records which faces it created.

The scenario pinned throughout is the one that motivated the module: a
deboss whose mark has a CLOSED outline (a ring), leaving an untouched
island of original surface enclosed by the carve.  Crease-angle
segmentation cannot tell that island from the recess floor; the recorded
face set must.

Coverage:
    - a deboss-shaped diff yields a non-empty face set (floors + walls)
    - the enclosed island and re-triangulated surface fragments are NOT
      claimed as decoration faces
    - floors/walls split by the decorated face's normal
    - sidecar round-trip via record/load
    - painting via paint_decoration_faces colors exactly the recorded
      faces (island untouched)
    - a stale mesh (content changed after recording) refuses loudly
    - the compile hook's scad-import wiring records without OpenSCAD
    - the distance pass is exact across batches, and its memory stays flat
      however many point/triangle pairs meet
"""

from __future__ import annotations

import os
import struct
from typing import Any

import pytest

from kiln.decoration_faces import (
    compute_decoration_faces,
    load_decoration_faces,
    load_mesh_triangles,
    record_decoration_faces,
    sidecar_path_for,
)

# ---------------------------------------------------------------------------
# Synthetic deboss geometry
# ---------------------------------------------------------------------------
#
# Original: a 20x20x10 box (12 triangles).
#
# "Decorated": the box after a simulated boolean that debossed a square
# ring 1 mm deep into the top face.  The Manifold backend preserves
# non-intersected triangles verbatim, so the 10 non-top triangles are
# copied bit-identical; the top face is rebuilt as:
#   - surround: top surface between the face edge and the ring's outer
#     edge (re-triangulated original surface — new by hash, on-surface)
#   - island:   top surface inside the ring (the enclosed island)
#   - floor:    the ring's recess floor at z=9
#   - walls:    the ring's vertical recess sides from z=10 to z=9
#
# Only floor + walls are decoration-created geometry.


def _quad(a, b, c, d) -> list[tuple]:
    """Two triangles for the quad a-b-c-d (in order)."""
    return [(a, b, c), (a, c, d)]


def _box_triangles() -> list[tuple]:
    lo, hi, z0, z1 = -10.0, 10.0, 0.0, 10.0
    tris: list[tuple] = []
    # bottom (z=0), then four sides
    tris += _quad((lo, lo, z0), (lo, hi, z0), (hi, hi, z0), (hi, lo, z0))
    tris += _quad((lo, lo, z0), (hi, lo, z0), (hi, lo, z1), (lo, lo, z1))
    tris += _quad((hi, lo, z0), (hi, hi, z0), (hi, hi, z1), (hi, lo, z1))
    tris += _quad((hi, hi, z0), (lo, hi, z0), (lo, hi, z1), (hi, hi, z1))
    tris += _quad((lo, hi, z0), (lo, lo, z0), (lo, lo, z1), (lo, hi, z1))
    # top (z=10)
    tris += _quad((lo, lo, z1), (hi, lo, z1), (hi, hi, z1), (lo, hi, z1))
    return tris


def _decorated_triangles() -> dict[str, list[tuple]]:
    """The carved box, grouped so tests can address each part by name."""
    lo, hi, top, floor_z = -10.0, 10.0, 10.0, 9.0
    ro, ri = 3.0, 1.5  # ring outer / inner half-widths

    base = _box_triangles()[:10]  # bottom + sides, preserved verbatim

    surround: list[tuple] = []
    surround += _quad((lo, ro, top), (hi, ro, top), (hi, hi, top), (lo, hi, top))
    surround += _quad((lo, lo, top), (hi, lo, top), (hi, -ro, top), (lo, -ro, top))
    surround += _quad((lo, -ro, top), (-ro, -ro, top), (-ro, ro, top), (lo, ro, top))
    surround += _quad((ro, -ro, top), (hi, -ro, top), (hi, ro, top), (ro, ro, top))

    island = _quad((-ri, -ri, top), (ri, -ri, top), (ri, ri, top), (-ri, ri, top))

    floor: list[tuple] = []
    floor += _quad((-ro, ri, floor_z), (ro, ri, floor_z), (ro, ro, floor_z), (-ro, ro, floor_z))
    floor += _quad((-ro, -ro, floor_z), (ro, -ro, floor_z), (ro, -ri, floor_z), (-ro, -ri, floor_z))
    floor += _quad((-ro, -ri, floor_z), (-ri, -ri, floor_z), (-ri, ri, floor_z), (-ro, ri, floor_z))
    floor += _quad((ri, -ri, floor_z), (ro, -ri, floor_z), (ro, ri, floor_z), (ri, ri, floor_z))

    walls: list[tuple] = []
    for a, b in (
        ((-ro, -ro), (ro, -ro)), ((ro, -ro), (ro, ro)),
        ((ro, ro), (-ro, ro)), ((-ro, ro), (-ro, -ro)),
        ((-ri, -ri), (ri, -ri)), ((ri, -ri), (ri, ri)),
        ((ri, ri), (-ri, ri)), ((-ri, ri), (-ri, -ri)),
    ):
        walls += _quad(
            (a[0], a[1], top), (b[0], b[1], top),
            (b[0], b[1], floor_z), (a[0], a[1], floor_z),
        )

    return {
        "base": base,
        "surround": surround,
        "island": island,
        "floor": floor,
        "walls": walls,
    }


def _write_stl(triangles: list[tuple], path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))


@pytest.fixture
def carved_pair(tmp_path):
    """(original_path, decorated_path, part-name → decorated indices)."""
    original = str(tmp_path / "jar.stl")
    decorated = str(tmp_path / "jar_deboss.stl")
    _write_stl(_box_triangles(), original)

    parts = _decorated_triangles()
    ordered: list[tuple] = []
    index_of: dict[str, list[int]] = {}
    for name in ("base", "surround", "island", "floor", "walls"):
        index_of[name] = list(range(len(ordered), len(ordered) + len(parts[name])))
        ordered.extend(parts[name])
    _write_stl(ordered, decorated)
    return original, decorated, index_of


# ---------------------------------------------------------------------------
# Engine: compute_decoration_faces
# ---------------------------------------------------------------------------


class TestComputeDecorationFaces:
    def test_deboss_yields_nonempty_face_set(self, carved_pair):
        original, decorated, idx = carved_pair
        result = compute_decoration_faces(original, decorated)
        assert result["face_indices"], "a real carve must record faces"
        assert result["triangle_count"] == sum(len(v) for v in idx.values())
        assert result["stats"]["decoration_area_mm2"] > 0

    def test_exactly_floor_and_walls_claimed(self, carved_pair):
        original, decorated, idx = carved_pair
        result = compute_decoration_faces(original, decorated)
        assert set(result["face_indices"]) == set(idx["floor"] + idx["walls"])

    def test_enclosed_island_not_claimed(self, carved_pair):
        """The bug this module exists to fix: the untouched surface island
        enclosed by the carve must never be recorded as decoration."""
        original, decorated, idx = carved_pair
        claimed = set(compute_decoration_faces(original, decorated)["face_indices"])
        assert not claimed & set(idx["island"])
        assert not claimed & set(idx["surround"])
        assert not claimed & set(idx["base"])

    def test_floor_wall_split_by_face_normal(self, carved_pair):
        original, decorated, idx = carved_pair
        result = compute_decoration_faces(
            original, decorated, face_normal=(0.0, 0.0, 1.0)
        )
        assert set(result["floor_indices"]) == set(idx["floor"])
        assert set(result["wall_indices"]) == set(idx["walls"])

    def test_identical_meshes_yield_empty_set(self, carved_pair, tmp_path):
        original, _, _ = carved_pair
        copy = str(tmp_path / "copy.stl")
        _write_stl(_box_triangles(), copy)
        result = compute_decoration_faces(original, copy)
        assert result["face_indices"] == []


# ---------------------------------------------------------------------------
# Sidecar: record / load round-trip and staleness
# ---------------------------------------------------------------------------


def _brute_force_distances(points, triangles):
    """Every point against every triangle -- the answer the grid must give."""
    import numpy as np

    from kiln.decoration_faces import _point_triangle_distances

    pp = np.repeat(np.arange(len(points)), len(triangles))
    tt = np.tile(np.arange(len(triangles)), len(points))
    out = np.full(len(points), np.inf)
    np.minimum.at(out, pp, _point_triangle_distances(points[pp], triangles[tt]))
    return out


class TestDistancePass:
    def test_batched_pass_matches_brute_force(self, monkeypatch):
        """Seven pairs per batch still gives every near point its exact
        nearest distance: batching changes when pairs are measured, never
        which pairs are."""
        import numpy as np

        import kiln.decoration_faces as df

        rng = np.random.default_rng(7)
        triangles = rng.uniform(0.0, 20.0, size=(80, 3, 3))
        # Points on the triangles, nudged off them, so most have a true
        # nearest distance inside the grid's exact radius.
        owners = rng.integers(0, len(triangles), size=600)
        bary = rng.dirichlet((1.0, 1.0, 1.0), size=600)
        points = np.einsum("ij,ijk->ik", bary, triangles[owners])
        points += rng.uniform(-1.5, 1.5, size=points.shape)

        monkeypatch.setattr(df, "_MAX_PAIRS_PER_BATCH", 7)
        got = df._min_distance_to_mesh(points, triangles)

        expected = _brute_force_distances(points, triangles)
        near = expected <= df._GRID_CELL_MM
        assert near.sum() > 400
        np.testing.assert_allclose(got[near], expected[near], rtol=0, atol=1e-12)
        # A point beyond the exact radius may only ever read as farther away,
        # never as lying on the surface.
        assert (got[~near] > df._GRID_CELL_MM).all()

    def test_memory_stays_flat_however_many_pairs_meet(self):
        """Every point here sees all 600 triangles: 1.2M pairs, which the
        all-at-once pass held as ~500 MB of temporaries (a textured cup's
        real diff reached 1.3 GB).  Batched, it stays near one batch."""
        import tracemalloc

        import numpy as np

        from kiln.decoration_faces import _min_distance_to_mesh

        # 600 stacked triangles inside one 4 x 4 x 3 mm block, so every
        # point's 3x3x3 cell neighbourhood holds all of them.
        corner = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [0.0, 4.0, 0.0]])
        lift = np.zeros((600, 1, 3))
        lift[:, 0, 2] = np.linspace(0.0, 3.0, 600)
        triangles = corner[None, :, :] + lift
        points = np.random.default_rng(11).uniform(
            (0.0, 0.0, 0.0), (4.0, 4.0, 3.0), size=(2_000, 3)
        )

        tracemalloc.start()
        try:
            # Every point is within 3 mm of the stack (the block's far
            # corner is 2.8 mm from the nearest edge): a 5 mm reach keeps
            # all 600 triangles in play for every point.
            distances = _min_distance_to_mesh(points, triangles, reach=5.0)
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert np.isfinite(distances).all()
        assert peak < 200 * 1024 * 1024


def _cap_spokes(radius: float = 45.0, sections: int = 96, z: float = 7.0):
    """A cylinder cap as a slicer exports it: one fan of long thin
    triangles from the centre to the rim (the coaster the carve hit)."""
    import numpy as np

    angles = np.linspace(0.0, 2 * np.pi, sections, endpoint=False)
    rim = np.stack([radius * np.cos(angles), radius * np.sin(angles), np.full(sections, z)], axis=1)
    centre = np.array([0.0, 0.0, z])
    return np.stack([np.broadcast_to(centre, rim.shape), rim, np.roll(rim, -1, axis=0)], axis=1)


class TestDistancePassOnLongThinTriangles:
    def test_a_point_finds_every_spoke_within_reach(self):
        """Exact against brute force for points over a fan of spokes, at the
        classification's own reach and at the default one, near the centre
        (where every spoke meets) and out by the rim."""
        import numpy as np

        import kiln.decoration_faces as df

        cap = _cap_spokes()
        rng = np.random.default_rng(3)
        r = np.sqrt(rng.uniform(0.0, 1.0, 3000)) * 46.0
        t = rng.uniform(0.0, 2 * np.pi, 3000)
        points = np.stack([r * np.cos(t), r * np.sin(t), 7.0 + rng.uniform(-1.5, 1.5, 3000)], axis=1)
        expected = _brute_force_distances(points, cap)
        for reach in (df.DISTANCE_EPS_MM, df._GRID_CELL_MM):
            got = df._min_distance_to_mesh(points, cap, reach=reach)
            near = expected <= reach
            assert near.sum() > 50, reach
            np.testing.assert_allclose(got[near], expected[near], rtol=0, atol=1e-12)
            assert np.isinf(got[~near]).all(), reach

    def test_a_cap_of_spokes_is_measured_in_seconds_not_minutes(self):
        """400k points just over a 96-spoke cap: each point's candidates are
        the spokes passing near it, not every spoke whose bounding box
        covers it.  Filed by bounding box, this pass took about 9 s on a
        laptop (a million-triangle coaster carve: 22 s); filed by surface it
        takes a fraction of one, so the budget below leaves room for slow CI."""
        import time

        import numpy as np

        import kiln.decoration_faces as df

        cap = _cap_spokes()
        rng = np.random.default_rng(4)
        r = np.sqrt(rng.uniform(0.0, 1.0, 400_000)) * 44.0
        t = rng.uniform(0.0, 2 * np.pi, 400_000)
        points = np.stack([r * np.cos(t), r * np.sin(t), np.full(400_000, 7.3)], axis=1)
        start = time.monotonic()
        distances = df._min_distance_to_mesh(points, cap)
        elapsed = time.monotonic() - start
        np.testing.assert_allclose(distances, 0.3, atol=1e-9)
        assert elapsed < 3.0, f"{elapsed:.1f} s"


class TestSurfaceSamplesStayInProportion:
    def test_a_long_thin_triangle_costs_its_area_not_its_length_squared(self):
        """A 200 mm sliver 4 mm wide (a tray cap's spoke) is covered by
        samples in proportion to its 400 mm2, not 200^2: the search's memory
        on a large flat part was the samples, and a length-squared grid laid
        ten million over a 200 x 140 mm tray."""
        import numpy as np

        import kiln.decoration_faces as df

        sliver = np.array([[[0.0, 0.0, 0.0], [200.0, -2.0, 0.0], [200.0, 2.0, 0.0]]])
        samples, owner = df._surface_samples(sliver, df._SAMPLE_SPACING_MM)
        area = 400.0
        assert len(samples) <= 4.0 * area / df._SAMPLE_SPACING_MM**2 + 2000, len(samples)
        # Every point of the sliver is within the spacing of a sample.
        probes = np.random.default_rng(9).dirichlet((1.0, 1.0, 1.0), size=300) @ sliver[0]
        nearest = np.array([np.min(np.linalg.norm(samples - q, axis=1)) for q in probes])
        assert (nearest <= df._SAMPLE_SPACING_MM + 1e-9).all()
        assert (owner == 0).all()

    def test_past_the_sample_cap_the_search_coarsens_and_stays_exact(self, monkeypatch):
        import numpy as np

        import kiln.decoration_faces as df

        monkeypatch.setattr(df, "_MAX_SURFACE_SAMPLES", 2_000)
        cap = _cap_spokes()
        assert df._sample_spacing(cap) > df._SAMPLE_SPACING_MM
        rng = np.random.default_rng(6)
        r = np.sqrt(rng.uniform(0.0, 1.0, 2000)) * 46.0
        t = rng.uniform(0.0, 2 * np.pi, 2000)
        points = np.stack([r * np.cos(t), r * np.sin(t), 7.0 + rng.uniform(-0.2, 0.2, 2000)], axis=1)
        expected = _brute_force_distances(points, cap)
        got = df._min_distance_to_mesh(points, cap, reach=df.DISTANCE_EPS_MM)
        near = expected <= df.DISTANCE_EPS_MM
        assert near.sum() > 100
        np.testing.assert_allclose(got[near], expected[near], rtol=0, atol=1e-12)
        assert np.isinf(got[~near]).all()


class TestStlFacesReadWithoutTrimesh:
    def test_an_stl_is_read_in_file_order_without_building_a_mesh(self, carved_pair, monkeypatch):
        """A trimesh mesh holds reference cycles and outlives the read until
        the collector runs; the face record reads an STL as arrays instead."""
        import kiln.decoration_faces as df
        import kiln.mesh_frame as mesh_frame

        _original, decorated, _ = carved_pair
        expected = df.load_mesh_triangles(decorated)

        def refuse(*_a, **_k):
            raise AssertionError("an STL was loaded through trimesh")

        monkeypatch.setattr(mesh_frame, "load_mesh", refuse)
        got = df.load_mesh_triangles(decorated)
        assert got.shape == expected.shape
        assert (got == expected).all()


class TestCapReadBeforeTheWork:
    """A mesh past MAX_TRACKED_FACES was always skipped, but only after the
    whole diff had run; the count is now read from the file first."""

    def _spy(self, monkeypatch):
        import kiln.decoration_faces as df

        ran: list[bool] = []

        def fake_compute(*_a, **_k):
            ran.append(True)
            return {"triangle_count": 10**9, "face_indices": [], "stats": {}}

        monkeypatch.setattr(df, "compute_decoration_faces", fake_compute)
        monkeypatch.setattr(df, "MAX_TRACKED_FACES", 5)
        return ran

    def test_a_binary_mesh_past_the_cap_never_runs_the_diff(self, carved_pair, monkeypatch):
        original, decorated, _ = carved_pair
        ran = self._spy(monkeypatch)
        assert record_decoration_faces(original, decorated) is None
        assert ran == []
        assert not os.path.isfile(sidecar_path_for(decorated))

    def test_an_ascii_mesh_is_counted_across_chunk_edges(self, tmp_path, monkeypatch):
        import kiln.decoration_faces as df

        tris = _box_triangles()
        text = "solid part\n" + "".join(
            "facet normal 0 0 0\nouter loop\n"
            + "".join(f"vertex {x} {y} {z}\n" for (x, y, z) in tri)
            + "endloop\nendfacet\n"
            for tri in tris
        ) + "endsolid part\n"
        path = tmp_path / "part.stl"
        path.write_text(text)
        monkeypatch.setattr(df, "_FACET_COUNT_CHUNK", 37)  # cuts many "endfacet"s in two
        assert df._declared_triangle_count(str(path)) == len(tris)
        ran = self._spy(monkeypatch)
        assert record_decoration_faces(str(path), str(path)) is None
        assert ran == []


class TestSidecar:
    def test_record_and_load_roundtrip(self, carved_pair):
        original, decorated, idx = carved_pair
        record = record_decoration_faces(
            original,
            decorated,
            decoration={"mode": "deboss", "depth_mm": 1.0},
            face_normal=(0, 0, 1),
        )
        assert record is not None
        assert os.path.isfile(sidecar_path_for(decorated))

        loaded, err = load_decoration_faces(decorated)
        assert err is None
        assert loaded["face_indices"] == record["face_indices"]
        assert loaded["decoration"]["mode"] == "deboss"
        assert loaded["_meta"]["schema"] == "kiln.decoration_faces"

    def test_missing_sidecar_reports_reason(self, carved_pair):
        _, decorated, _ = carved_pair
        loaded, err = load_decoration_faces(decorated)
        assert loaded is None
        assert "no decoration face record" in err

    def test_stale_mesh_refuses_loudly(self, carved_pair):
        original, decorated, _ = carved_pair
        assert record_decoration_faces(original, decorated) is not None
        # Change the mesh content after recording — one more triangle.
        tris = _box_triangles() + _decorated_triangles()["floor"]
        _write_stl(tris, decorated)
        loaded, err = load_decoration_faces(decorated)
        assert loaded is None
        assert "STALE" in err

    def test_kill_switch_disables_recording(self, carved_pair, monkeypatch):
        original, decorated, _ = carved_pair
        monkeypatch.setenv("KILN_DECORATION_FACE_TRACKING", "off")
        assert record_decoration_faces(original, decorated) is None
        assert not os.path.isfile(sidecar_path_for(decorated))

    def test_recording_never_raises_on_garbage(self, tmp_path):
        bad = str(tmp_path / "not_a_mesh.stl")
        with open(bad, "w") as fh:
            fh.write("garbage")
        assert record_decoration_faces(bad, bad) is None


# ---------------------------------------------------------------------------
# Compile hook: the scad-import wiring (no OpenSCAD needed)
# ---------------------------------------------------------------------------


class TestCompileHookWiring:
    def test_scad_import_is_diffed_and_recorded(self, carved_pair):
        from kiln.emboss_generator import _record_face_provenance

        original, decorated, idx = carved_pair
        scad_text = (
            'difference() {\n'
            f'  import("{original}");\n'
            '  translate([0,0,9]) cube(6, center=true);\n'
            '}\n'
        )
        record = _record_face_provenance(
            scad_text, decorated, decoration_meta={"mode": "deboss"},
            face_normal=(0, 0, 1),
        )
        assert record is not None
        assert set(record["face_indices"]) == set(idx["floor"] + idx["walls"])
        assert os.path.isfile(sidecar_path_for(decorated))

    def test_svg_import_alone_records_nothing(self, carved_pair):
        from kiln.emboss_generator import _record_face_provenance

        _, decorated, _ = carved_pair
        record = _record_face_provenance(
            'linear_extrude(2) import("logo.svg");',
            decorated,
            decoration_meta=None,
            face_normal=None,
        )
        assert record is None

    def test_missing_source_mesh_records_nothing(self, carved_pair):
        from kiln.emboss_generator import _record_face_provenance

        _, decorated, _ = carved_pair
        record = _record_face_provenance(
            'difference() { import("/nonexistent/gone.stl"); cube(1); }',
            decorated,
            decoration_meta=None,
            face_normal=None,
        )
        assert record is None


# ---------------------------------------------------------------------------
# Painting door: paint_decoration_faces
# ---------------------------------------------------------------------------


def _call_paint_tool(**kwargs: Any) -> dict:
    from kiln.plugins.color_tools import _ColorToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    _ColorToolsPlugin().register(_FakeMcp())
    return tools["paint_decoration_faces"](**kwargs)


class TestPaintDecorationFaces:
    def test_paints_only_recorded_faces(self, carved_pair, tmp_path):
        original, decorated, idx = carved_pair
        record = record_decoration_faces(
            original, decorated, face_normal=(0, 0, 1)
        )
        out = str(tmp_path / "painted.3mf")
        result = _call_paint_tool(
            model_path=decorated, color="#F72323", output_path=out
        )
        assert result["success"] is True, result.get("error")
        assert result["painted_triangles"] == len(record["face_indices"])
        assert result["total_triangles"] == len(load_mesh_triangles(decorated))
        # The enclosed island stays body-colored: painted count equals the
        # carve set, which the engine tests prove excludes the island.
        assert result["painted_triangles"] < result["total_triangles"]
        assert os.path.isfile(result["output_path"])

    def test_floors_target_paints_floors_only(self, carved_pair, tmp_path):
        original, decorated, idx = carved_pair
        record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
        result = _call_paint_tool(
            model_path=decorated,
            target="floors",
            output_path=str(tmp_path / "floors.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert result["painted_triangles"] == len(idx["floor"])

    def test_no_sidecar_is_loud_refusal(self, carved_pair):
        _, decorated, _ = carved_pair
        result = _call_paint_tool(model_path=decorated)
        assert result["success"] is False
        assert result["code"] == "NO_FACE_RECORD"

    def test_stale_mesh_is_loud_refusal_not_wrong_paint(self, carved_pair):
        original, decorated, _ = carved_pair
        assert record_decoration_faces(original, decorated) is not None
        tris = _box_triangles() + _decorated_triangles()["floor"]
        _write_stl(tris, decorated)
        result = _call_paint_tool(model_path=decorated)
        assert result["success"] is False
        assert "STALE" in result["error"]

    def test_unknown_target_rejected(self, carved_pair):
        original, decorated, _ = carved_pair
        record_decoration_faces(original, decorated)
        result = _call_paint_tool(model_path=decorated, target="ceilings")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Paint provenance: painting writes back what it painted
# ---------------------------------------------------------------------------


class TestPaintEventRecording:
    def test_paint_tool_records_color_in_sidecar(self, carved_pair, tmp_path):
        from kiln.decoration_faces import load_decoration_faces

        original, decorated, _ = carved_pair
        record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
        result = _call_paint_tool(
            model_path=decorated,
            color="#F72323",
            output_path=str(tmp_path / "painted.3mf"),
        )
        assert result["success"] is True
        assert result["paint_recorded"] is True

        loaded, err = load_decoration_faces(decorated)
        assert err is None, "paint annotation must not break the hash gate"
        painted = loaded["painted"]
        assert painted["color"] == "#F72323"
        assert painted["target"] == "all"
        assert painted["output"] == "painted.3mf"
        assert painted["output_sha256"]

    def test_repaint_keeps_latest_color_only(self, carved_pair, tmp_path):
        from kiln.decoration_faces import load_decoration_faces

        original, decorated, _ = carved_pair
        record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
        _call_paint_tool(
            model_path=decorated, color="#112233",
            output_path=str(tmp_path / "a.3mf"),
        )
        _call_paint_tool(
            model_path=decorated, color="#F72323", target="floors",
            output_path=str(tmp_path / "b.3mf"),
        )
        loaded, _ = load_decoration_faces(decorated)
        assert loaded["painted"]["color"] == "#F72323"
        assert loaded["painted"]["target"] == "floors"

    def test_paint_event_never_raises_without_sidecar(self, carved_pair, tmp_path):
        from kiln.decoration_faces import record_paint_event

        _, decorated, _ = carved_pair
        out = record_paint_event(
            decorated, color="#FFFFFF", target="all",
            output_path=str(tmp_path / "x.3mf"),
        )
        assert out is None


# ---------------------------------------------------------------------------
# Chained carves: a second decoration must not orphan the first
# ---------------------------------------------------------------------------
#
# Second carve on the already-decorated box: a square recess debossed
# into the top face at (5..8, 5..8), away from the ring.  The simulated
# boolean:
#   - preserves verbatim: base, island, ring walls, 3 of the 4 ring
#     floor quads
#   - re-triangulates ONE ring floor quad (flipped diagonal) — new by
#     hash but lying ON the first carve's floor
#   - rebuilds the surround (top surface) with a hole for the new recess
#   - adds the new recess floor + walls
#
# The final mesh's record must cover BOTH carves: the preserved ring
# geometry (carried forward by hash), the re-triangulated ring floor
# fragment (rescued by distance to the prior carve's faces), and the new
# recess.  Before chain carry-forward, the record held only the second
# carve — "paint everything I carved" painted half the plate and
# reported success.


def _chained_triangles() -> dict[str, list[tuple]]:
    """The twice-carved box, grouped so tests can address each part."""
    lo, hi, top, floor_z = -10.0, 10.0, 10.0, 9.0
    ro = 3.0  # ring outer half-width (matches _decorated_triangles)
    b0, b1 = 5.0, 8.0  # second recess bounds

    first = _decorated_triangles()

    # One ring floor quad re-triangulated with the flipped diagonal:
    # same quad surface, different triangles.
    a, b, c, d = (-ro, 1.5, floor_z), (ro, 1.5, floor_z), (ro, ro, floor_z), (-ro, ro, floor_z)
    retri_floor = [(b, c, d), (b, d, a)]
    kept_floor = first["floor"][2:]  # the other 3 quads, verbatim

    surround2: list[tuple] = []
    surround2 += _quad((lo, 8.0, top), (hi, 8.0, top), (hi, hi, top), (lo, hi, top))
    surround2 += _quad((lo, b0, top), (b0, b0, top), (b0, b1, top), (lo, b1, top))
    surround2 += _quad((b1, b0, top), (hi, b0, top), (hi, b1, top), (b1, b1, top))
    surround2 += _quad((lo, ro, top), (hi, ro, top), (hi, b0, top), (lo, b0, top))
    surround2 += _quad((lo, lo, top), (hi, lo, top), (hi, -ro, top), (lo, -ro, top))
    surround2 += _quad((lo, -ro, top), (-ro, -ro, top), (-ro, ro, top), (lo, ro, top))
    surround2 += _quad((ro, -ro, top), (hi, -ro, top), (hi, ro, top), (ro, ro, top))

    floor2 = _quad((b0, b0, floor_z), (b1, b0, floor_z), (b1, b1, floor_z), (b0, b1, floor_z))
    walls2: list[tuple] = []
    for p, q in (
        ((b0, b0), (b1, b0)), ((b1, b0), (b1, b1)),
        ((b1, b1), (b0, b1)), ((b0, b1), (b0, b0)),
    ):
        walls2 += _quad(
            (p[0], p[1], top), (q[0], q[1], top),
            (q[0], q[1], floor_z), (p[0], p[1], floor_z),
        )

    return {
        "base": first["base"],
        "island": first["island"],
        "kept_floor": kept_floor,
        "retri_floor": retri_floor,
        "walls": first["walls"],
        "surround2": surround2,
        "floor2": floor2,
        "walls2": walls2,
    }


@pytest.fixture
def chained_pair(carved_pair, tmp_path):
    """(decorated_path, twice_decorated_path, part-name → indices in the final mesh)."""
    _original, decorated, _index_of = carved_pair
    decorated2 = str(tmp_path / "jar_deboss_twice.stl")

    parts = _chained_triangles()
    ordered: list[tuple] = []
    index_of: dict[str, list[int]] = {}
    for name in (
        "base", "island", "kept_floor", "retri_floor",
        "walls", "surround2", "floor2", "walls2",
    ):
        index_of[name] = list(range(len(ordered), len(ordered) + len(parts[name])))
        ordered.extend(parts[name])
    _write_stl(ordered, decorated2)
    return decorated, decorated2, index_of


class TestChainedCarves:
    def test_prior_carve_faces_carried_forward(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        record_decoration_faces(original, decorated)
        record = record_decoration_faces(decorated, decorated2)
        assert record is not None
        faces = set(record["face_indices"])
        # Second carve's own geometry is recorded...
        assert set(idx2["floor2"]) <= faces
        assert set(idx2["walls2"]) <= faces
        # ...and the FIRST carve's preserved geometry is carried forward.
        assert set(idx2["kept_floor"]) <= faces
        assert set(idx2["walls"]) <= faces
        # Untouched surface stays unclaimed.
        assert not faces & set(idx2["island"])
        assert not faces & set(idx2["surround2"])
        assert not faces & set(idx2["base"])

    def test_retriangulated_prior_floor_rescued(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        record_decoration_faces(original, decorated)
        record = record_decoration_faces(decorated, decorated2)
        # The re-cut ring floor quad lies ON the prior carve's surface —
        # new by hash, distance ~0 to the pre-carve mesh.  It must be
        # attributed to the decoration, not dropped as a fragment.
        assert set(idx2["retri_floor"]) <= set(record["face_indices"])

    def test_decoration_history_lists_both_steps(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, _idx2 = chained_pair
        record_decoration_faces(original, decorated, decoration={"text": "line 0"})
        record = record_decoration_faces(
            decorated, decorated2, decoration={"text": "line 1"}
        )
        steps = record.get("decorations")
        assert isinstance(steps, list) and len(steps) == 2
        assert steps[0]["decoration"] == {"text": "line 0"}
        assert steps[1]["decoration"] == {"text": "line 1"}

    def test_no_prior_sidecar_records_second_carve_only(self, carved_pair, chained_pair):
        _original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        # First carve never recorded: nothing to carry, no invention.
        record = record_decoration_faces(decorated, decorated2)
        faces = set(record["face_indices"])
        assert set(idx2["floor2"]) <= faces
        assert not faces & set(idx2["kept_floor"])
        assert not faces & set(idx2["walls"])

    def test_floor_wall_split_merged_across_steps(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
        record = record_decoration_faces(
            decorated, decorated2, face_normal=(0, 0, 1)
        )
        floors = set(record["floor_indices"])
        walls = set(record["wall_indices"])
        assert set(idx2["floor2"]) <= floors
        assert set(idx2["kept_floor"]) <= floors
        assert set(idx2["retri_floor"]) <= floors
        assert set(idx2["walls2"]) <= walls
        assert set(idx2["walls"]) <= walls

    def test_split_omitted_when_prior_step_lacks_it(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, _idx2 = chained_pair
        record_decoration_faces(original, decorated)  # no face_normal: no split
        record = record_decoration_faces(
            decorated, decorated2, face_normal=(0, 0, 1)
        )
        # Publishing a floors/walls split that silently omits the prior
        # carve's floors would recreate the half-paint bug one target
        # deeper — omit the split instead.
        assert "floor_indices" not in record
        assert "wall_indices" not in record

    def test_chained_record_keeps_per_step_faces(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        record_decoration_faces(original, decorated)
        record = record_decoration_faces(decorated, decorated2)
        steps = record["decorations"]
        step0 = set(steps[0]["face_indices"])
        step1 = set(steps[1]["face_indices"])
        # Step 0 owns the first carve's geometry — preserved AND re-cut.
        assert set(idx2["kept_floor"]) <= step0
        assert set(idx2["walls"]) <= step0
        assert set(idx2["retri_floor"]) <= step0
        # Step 1 owns the second carve's recess, and nothing of the first.
        assert set(idx2["floor2"]) <= step1
        assert set(idx2["walls2"]) <= step1
        assert not step0 & step1
        # The union is exactly the record's face set.
        assert step0 | step1 == set(record["face_indices"])


class TestStepColors:
    def _chained_record(self, carved_pair, chained_pair):
        original, decorated, _ = carved_pair
        _, decorated2, idx2 = chained_pair
        record_decoration_faces(original, decorated, decoration={"text": "ring"})
        record = record_decoration_faces(
            decorated, decorated2, decoration={"text": "recess"}
        )
        return decorated2, idx2, record

    def test_each_step_painted_its_own_color(self, carved_pair, chained_pair, tmp_path):
        decorated2, _idx2, record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2,
            step_colors={"0": "#F72323", "1": "#FFFFFF"},
            base_color="#161616",
            output_path=str(tmp_path / "two_tone.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert result["painted_triangles"] == len(record["face_indices"])
        assert "#F72323" in result["colors"]
        assert "#FFFFFF" in result["colors"]
        listing = result["decorations"]
        assert [s["color"] for s in listing] == ["#F72323", "#FFFFFF"]
        assert listing[0]["decoration"] == {"text": "ring"}

    def test_unnamed_steps_stay_base(self, carved_pair, chained_pair, tmp_path):
        decorated2, _idx2, record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2,
            step_colors={1: "#FFFFFF"},
            output_path=str(tmp_path / "one_step.3mf"),
        )
        assert result["success"] is True, result.get("error")
        step1 = record["decorations"][1]["face_indices"]
        assert result["painted_triangles"] == len(step1)
        assert result["decorations"][0]["color"] is None

    def test_bad_step_index_lists_the_steps(self, carved_pair, chained_pair):
        decorated2, _idx2, _record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2, step_colors={"7": "#FFFFFF"}
        )
        assert result["success"] is False
        assert "2 decoration steps" in result["error"]
        assert len(result["decorations"]) == 2

    def test_step_colors_with_floors_target_refused(self, carved_pair, chained_pair):
        decorated2, _idx2, _record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2,
            target="floors",
            step_colors={"0": "#FFFFFF"},
        )
        assert result["success"] is False
        assert "floors/walls" in result["error"]

    def test_bad_hex_refused(self, carved_pair, chained_pair):
        decorated2, _idx2, _record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2, step_colors={"0": "red"}
        )
        assert result["success"] is False
        assert "#RRGGBB" in result["error"]

    def test_single_step_record_is_step_zero(self, carved_pair, tmp_path):
        original, decorated, idx = carved_pair
        record_decoration_faces(original, decorated)
        result = _call_paint_tool(
            model_path=decorated,
            step_colors={"0": "#FFFFFF"},
            output_path=str(tmp_path / "single.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert result["painted_triangles"] == len(idx["floor"]) + len(idx["walls"])
        assert len(result["decorations"]) == 1

    def test_paint_event_records_step_colors(self, carved_pair, chained_pair, tmp_path):
        decorated2, _idx2, _record = self._chained_record(carved_pair, chained_pair)
        result = _call_paint_tool(
            model_path=decorated2,
            step_colors={"0": "#F72323", "1": "#FFFFFF"},
            output_path=str(tmp_path / "recorded.3mf"),
        )
        assert result["success"] is True
        updated, err = load_decoration_faces(decorated2)
        assert err is None
        assert updated["painted"]["step_colors"] == {
            "0": "#F72323", "1": "#FFFFFF",
        }


class TestPaintSaysWhetherTheColourIsLoaded:
    def test_paint_door_carries_the_advisory(self, carved_pair, tmp_path, monkeypatch):
        from kiln import server

        original, decorated, idx = carved_pair
        record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
        seen: dict[str, Any] = {}

        def fake(colours, *, printer_name=None, adapter=None):
            seen["colours"] = list(colours)
            return {"verdict": "mismatch", "message": "No red loaded on default."}

        monkeypatch.setattr(server, "_spool_advisory", fake)
        result = _call_paint_tool(
            model_path=decorated, color="#F72323", base_color="#FFFFFF",
            output_path=str(tmp_path / "painted.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert [c["colour"] for c in result["colour_availability"]["colours"]] == ["#F72323", "#FFFFFF"]
        assert "ams_advisory" not in result
        assert seen["colours"] == ["#F72323", "#FFFFFF"]
