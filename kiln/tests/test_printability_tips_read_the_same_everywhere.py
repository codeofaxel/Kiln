"""A tip thinner than the nozzle reads the same at every detail level and on
every tier; a wall thinner than the nozzle is still a wall.

A part reads the same at every mesh resolution, on every tier and on every
run: every triangle is read, a tip stands on solid the nozzle lays down, and
a strand's point is read along the strand.  What still stops is what Kiln's
slice drops
(test_printability_judges_what_the_slicer_drops.py): a needle thin for longer
than a tip is deep among it.  A thin bar or rod joining two parts prints, as
one thin line or layer.
"""
from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln import printability as P  # noqa: E402

NOZZLE = 0.4


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _union(*parts):
    mesh = trimesh.boolean.union(list(parts), engine="manifold")
    assert mesh.is_volume, "fixture must be one closed solid"
    return mesh


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _stops(mesh, *, floor=None):
    """The wall check's regions Kiln's slice drops: what stops a print."""
    walls = P._analyze_thin_walls(
        [tuple(map(tuple, t)) for t in np.asarray(mesh.triangles).tolist()],
        [tuple(v) for v in np.asarray(mesh.vertices).tolist()],
        nozzle_diameter=NOZZLE, thin_below_mm=floor,
    )
    return walls.dropped_regions, walls


def _strand_on_a_block():
    """A 10 x 10 x 5 block with a strand on it, a lock of hair: a root
    flaring from 1 mm in radius to 0.15 mm over 3 mm, then 1 mm of strand
    0.3 mm across with a flat end.  Its faces are ringed finely toward the
    end, and few enough that the wall check measures every face.  A point
    whose faces close to nothing is a corner (another test's); a strand's
    blunt end is not."""
    sections = 16
    profile = [(1.0, 0.0), (0.79, 0.75), (0.58, 1.5), (0.36, 2.25), (0.15, 3.0),
               (0.15, 3.25), (0.15, 3.5), (0.15, 3.75), (0.15, 4.0)]
    turn = 2.0 * np.pi * np.arange(sections) / sections
    rings = [np.c_[r * np.cos(turn), r * np.sin(turn), np.full(sections, z)] for r, z in profile]
    verts = np.vstack([*rings, [[0.0, 0.0, 0.0]], [[0.0, 0.0, 4.0]]])
    root, end = len(verts) - 2, len(verts) - 1
    faces = []
    for k in range(len(rings) - 1):
        a, b = k * sections, (k + 1) * sections
        for i in range(sections):
            j = (i + 1) % sections
            faces += [[a + i, a + j, b + j], [a + i, b + j, b + i]]
    top = (len(rings) - 1) * sections
    faces += [[root, (i + 1) % sections, i] for i in range(sections)]
    faces += [[end, top + i, top + (i + 1) % sections] for i in range(sections)]
    strand = trimesh.Trimesh(verts, faces, process=True)
    strand.fix_normals()
    assert strand.is_volume
    strand.apply_translation((5.0, 5.0, 4.99))
    part = _union(_box((0, 0, 0), (10, 10, 5)), strand)
    assert len(part.faces) <= P._THIN_WALL_RAY_SAMPLE_CAP
    return part


def _plate_with_a_tapered_edge():
    """A 20 x 20 plate 1 mm thick whose last 3 mm along x taper to 0.2 mm."""
    section = [(0.0, 0.0), (20.0, 0.0), (20.0, 0.2), (17.0, 1.0), (0.0, 1.0)]
    plate = trimesh.convex.convex_hull([(x, y, z) for x, z in section for y in (0.0, 20.0)])
    for _ in range(4):
        plate = plate.subdivide()
    assert plate.is_volume
    return plate


def _ridge_on_a_carved_floor():
    """A 0.3 x 1.0 mm ridge on the 1.4 mm floor of a pocket in a plate."""
    plate = _box((0, 0, 0), (30, 30, 3))
    pocket = _box((5, 5, 1.4), (25, 25, 3.01))
    ridge = _box((6, 14.85, 1.4), (24, 15.15, 2.4))
    return _union(trimesh.boolean.difference([plate, pocket], engine="manifold"), ridge)


class TestATipIsDetail:
    def test_the_end_of_a_strand_is_detail_not_a_wall(self):
        stops, walls = _stops(_strand_on_a_block())
        assert stops == [], stops
        assert walls.fine_detail_count > 0, "the strand was not even seen"
        assert walls.fine_detail_min_mm < NOZZLE

    def test_a_plate_edge_tapering_out_of_one_millimetre_is_detail(self):
        stops, walls = _stops(_plate_with_a_tapered_edge())
        assert stops == [], stops
        assert walls.fine_detail_count > 0

    def test_the_answer_is_the_same_on_every_tier(self):
        """Kiln Pro measures thin walls against a material's minimum; whether
        a tip stops the print is the nozzle's question on both."""
        for mesh in (_ridge_on_a_carved_floor(), _plate_with_a_tapered_edge()):
            free, _ = _stops(mesh)
            pro, _ = _stops(mesh, floor=1.2)
            assert free == pro == [], (free, pro)

    def test_a_fin_on_a_body_wider_than_the_search_is_detail(self):
        """Behind the fin the body runs 70 mm one way, past where the probe
        looks; that is solid, not an unknown."""
        fin = _box((10.0, 9.85, 30.0), (30.0, 10.15, 31.0))  # 0.3 wide, 1 tall
        part = _union(_box((0, 0, 0), (40, 80, 30)), fin)
        assert len(part.faces) <= P._THIN_WALL_RAY_SAMPLE_CAP
        stops, walls = _stops(part)
        assert stops == [], stops
        assert walls.fine_detail_count > 0, "the fin was not even seen"

    def test_kiln_pros_thicker_walls_never_spend_what_a_stop_needs(self, monkeypatch):
        """Kiln Pro walks the corners of chords up to a material's minimum
        wall as well.  Given only what the free tier's reading of the same
        part needs, Kiln Pro still reaches the same answer about the tip."""
        mesh = _plate_with_a_tapered_edge()
        tris = [tuple(map(tuple, t)) for t in np.asarray(mesh.triangles).tolist()]
        verts = [tuple(v) for v in np.asarray(mesh.vertices).tolist()]
        spent = P._CornerBudget()
        free = P._measure_thin_walls(tris, verts, nozzle_diameter=NOZZLE, corner_budget=spent)[0]
        monkeypatch.setattr(P, "_CORNER_READ_BUDGET", P._CORNER_READ_BUDGET - spent.left)
        pro = P._measure_thin_walls(
            tris, verts, nozzle_diameter=NOZZLE, thin_below_mm=1.2, corner_budget=P._CornerBudget(),
        )[0]
        assert [free.dropped_regions, pro.dropped_regions] == [[], []]

    def test_a_crumb_narrower_than_two_nozzle_widths_is_detail(self):
        """A disc 0.25 mm thick and 0.6 mm across held at its rim by a post:
        read from its far edge, nothing runs into the post and nothing under
        the disc is solid, so only its size says it is detail."""
        disc = trimesh.creation.cylinder(radius=0.3, height=0.25, sections=24)
        disc.apply_translation((5.0, 5.0, 7.125))
        post = trimesh.creation.cylinder(radius=0.3, height=2.1, sections=24)
        post.apply_translation((5.3, 5.0, 6.0))
        part = _union(_box((0, 0, 0), (10, 10, 5)), post, disc)
        assert len(part.faces) <= P._THIN_WALL_RAY_SAMPLE_CAP
        stops, walls = _stops(part)
        assert stops == [], stops
        assert walls.fine_detail_count > 0, "the crumb was not even seen"

    def test_a_ridge_cut_finer_still_reads_as_detail(self):
        mesh = _ridge_on_a_carved_floor()
        while len(mesh.faces) <= 100_000:
            mesh = mesh.subdivide()
        stops, walls = _stops(mesh)
        assert stops == [], stops
        assert walls.fine_detail_count > 0


def _needle_on_a_block():
    needle = trimesh.creation.cylinder(radius=0.15, height=6.0, sections=24)
    needle.apply_translation((5.0, 5.0, 7.99))
    return _union(_box((0, 0, 0), (10, 10, 5)), needle)


class TestAWallIsStillAWall:
    def test_a_needle_thin_for_longer_than_a_tip_stops_the_print(self):
        stops, _ = _stops(_needle_on_a_block())
        assert {r["why"] for r in stops} == {"pin"}, "6 mm of a 0.3 mm needle is more than a tip"

    def test_the_print_door_passes_the_strand_and_refuses_the_needle(self, tmp_path):
        from kiln.printability import analyze_printability, stops

        strand = tmp_path / "strand.stl"
        needle = tmp_path / "needle.stl"
        _strand_on_a_block().export(strand)
        _needle_on_a_block().export(needle)
        assert stops(analyze_printability(str(strand), nozzle_diameter=NOZZLE)) == []
        assert any(s.startswith("A pin") for s in stops(analyze_printability(str(needle), nozzle_diameter=NOZZLE)))
