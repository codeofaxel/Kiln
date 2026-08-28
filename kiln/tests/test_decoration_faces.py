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
        def tool(self):
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
