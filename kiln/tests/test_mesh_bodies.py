"""The one reading of how many pieces a mesh is and which are sealed.

Covers: pieces laid on each other stay separate pieces and are reported as
touching, never as pinched edges of one; the same model reads the same in
meters and in millimeters; corners a glTF splits along its seams are joined
before closure is judged; open edges unseal a piece.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from kiln.mesh_bodies import is_sealed, read_bodies, read_mesh, read_triangles, weld


def _box(lo: tuple[float, float, float], hi: tuple[float, float, float], splits: int = 2) -> trimesh.Trimesh:
    lo_a, hi_a = np.asarray(lo, float), np.asarray(hi, float)
    box = trimesh.creation.box(extents=hi_a - lo_a)
    for _ in range(splits):
        box = box.subdivide()
    box.apply_translation((lo_a + hi_a) / 2)
    return box


def _stack(scale: float = 1.0, *, nudge: float = 0.0) -> trimesh.Trimesh:
    """A 20 x 20 x 10 block with a 4 mm slab laid on its top face, the way a
    coat is modelled on a body: the slab's underside reuses the block's top
    corners.  *nudge* lifts the slab's underside by that fraction of its
    width, the few-ulp disagreement two exporters' copies of one corner have."""
    block = _box((0, 0, 0), (20, 20, 10))
    slab = _box((0, 0, 10), (20, 20, 14))
    v = slab.vertices.copy()
    v[np.isclose(v[:, 2], 10.0), 2] += nudge * 20.0
    slab = trimesh.Trimesh(v, slab.faces, process=False)
    both = trimesh.util.concatenate([block, slab])
    both.apply_scale(scale)
    return both


def _soup(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Every triangle with its own corners, as an STL stores them."""
    tris = mesh.triangles.reshape(-1, 3)
    return trimesh.Trimesh(tris, np.arange(len(tris)).reshape(-1, 3), process=False)


class TestPiecesThatTouch:
    def test_a_slab_on_a_block_is_two_sealed_pieces(self):
        reading = read_mesh(_soup(_stack()))

        assert reading.piece_count == 2
        assert reading.sealed
        assert reading.pinch_edges == 0
        assert reading.open_edges == 0
        assert reading.touching_edges > 0
        assert reading.touching[0][:2] == (0, 1)

    def test_touching_is_said_as_touching_not_as_a_defect(self):
        lines = read_mesh(_soup(_stack())).describe()

        assert len(lines) == 1
        assert "sit on each other" in lines[0]
        assert "pinched" not in lines[0]

    def test_triangles_lying_back_to_back_are_not_one_solid(self):
        """Two opposite-facing triangles on the same corners: each belongs
        to its own piece, so the block keeps its top and the slab its base."""
        reading = read_mesh(_soup(_stack()))
        block, slab = reading.pieces[0], reading.pieces[1]

        assert block.sealed and slab.sealed
        assert block.faces == slab.faces == 192


class TestUnitsDoNotChangeTheReading:
    @pytest.mark.parametrize("nudge", [0.0, 2e-7])
    def test_meters_and_millimeters_read_alike(self, nudge):
        mm = read_mesh(_soup(_stack(1.0, nudge=nudge)))
        m = read_mesh(_soup(_stack(0.001, nudge=nudge)))

        assert (mm.piece_count, mm.touching_edges, mm.pinch_edges, mm.open_edges) == (
            m.piece_count, m.touching_edges, m.pinch_edges, m.open_edges,
        )
        assert mm.piece_count == 2

    def test_weld_radius_scales_with_the_part(self):
        tri = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
        _, _, small = weld(tri * 0.001, np.array([[0, 1, 2]]))
        _, _, large = weld(tri * 1000.0, np.array([[0, 1, 2]]))

        assert large / small == pytest.approx(1e6)


def _block_with_fin(fin_width: float) -> trimesh.Trimesh:
    """A 20 x 10 block with a fin *fin_width* wide and 5 tall on its top, the
    profile extruded 20 deep: one closed surface, every edge used twice."""
    w = fin_width
    ring = [(0, 0), (20, 0), (20, 10), (10 + w, 10), (10 + w, 15), (10, 15), (10, 10), (0, 10)]
    n = len(ring)
    v = np.array([(x, y, z) for z in (0.0, 20.0) for x, y in ring], float)
    cap = [(0, 1, 2), (0, 2, 3), (0, 3, 6), (0, 6, 7), (6, 3, 4), (6, 4, 5)]
    faces = [(a, c, b) for a, b, c in cap] + [(a + n, b + n, c + n) for a, b, c in cap]
    for i in range(n):
        j = (i + 1) % n
        faces += [(i, j, j + n), (i, j + n, i + n)]
    return trimesh.Trimesh(v, np.array(faces), process=False)


class TestTheWeldJoinsCopiesNotCorners:
    """The weld joins the copies of one point a file wrote, and never closes
    a side a triangle holds apart: a surface finer than the radius reads as
    written."""

    def test_a_fin_finer_than_the_radius_is_read_as_written(self):
        block = _block_with_fin(0.0)
        fin = 2.0 * weld(block.vertices, block.faces)[2]  # two weld radii
        part = _soup(_block_with_fin(fin))
        assert read_bodies(part.vertices, part.faces, tolerance=0.0).sealed  # as the file is

        reading = read_mesh(part)
        assert (reading.piece_count, reading.solid_count) == (1, 1)
        assert reading.sealed and reading.pinch_edges == 0 and reading.open_edges == 0

    def test_a_side_shorter_than_the_radius_is_never_closed(self):
        """A needle: one side of a tetrahedron split half a radius from its
        end.  Its two short-sided triangles keep three corners."""
        tet = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], float)
        tol = weld(tet, np.array([[0, 1, 2]]))[2]
        needle = np.vstack([tet, [[0.5 * tol, 0, 0]]])  # corner 4, on side 0-1
        faces = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
        split = np.array([[0, 2, 4], [4, 2, 1], [0, 4, 3], [4, 1, 3], [1, 2, 3], [0, 3, 2]])
        assert read_bodies(tet, faces).sealed and read_bodies(needle, split, tolerance=0.0).sealed

        _, welded, _ = weld(needle, split)
        assert all(len(set(face)) == 3 for face in welded.tolist())

        # One copy of the needle's end corner written a rounding off, as an
        # STL stores every triangle's own corners: the crack closes, and the
        # needle keeps its short side.
        soup = needle[split].reshape(-1, 3)
        soup[0] += (1e-4 * tol, 0.0, 0.0)  # triangle 0's copy of corner 0
        soup_faces = np.arange(len(soup)).reshape(-1, 3)
        assert not read_bodies(soup, soup_faces, tolerance=0.0).sealed
        reading = read_bodies(soup, soup_faces)
        assert reading.sealed and reading.pieces[0].faces == 6

    def test_copies_of_one_corner_in_one_triangle_are_joined(self):
        """The same split a thousandth of a radius from its end, one corner
        written twice in one triangle: the two are one corner, and the two
        triangles that held both are left with no area."""
        tet = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], float)
        tol = weld(tet, np.array([[0, 1, 2]]))[2]
        copied = np.vstack([tet, [[1e-3 * tol, 0, 0]]])
        split = np.array([[0, 2, 4], [4, 2, 1], [0, 4, 3], [4, 1, 3], [1, 2, 3], [0, 3, 2]])

        _, welded, _ = weld(copied, split)
        assert sum(len(set(face)) < 3 for face in welded.tolist()) == 2
        assert read_bodies(copied, split).sealed

    def test_a_chain_of_near_corners_never_closes_a_side(self):
        """Corner B lies within the radius of both ends of another triangle's
        side A-C, which is longer than the radius: joining B to each would
        close A-C, so none of the three join."""
        t = 1e-3
        points = np.array([
            [0, 0, 0], [1.5 * t, 0, 0], [0, 10, 0],          # A, C, and a far corner
            [0.75 * t, 0, 0], [5, 5, 5], [5, 6, 5],          # B, and its triangle's far corners
        ], float)
        _, welded, _ = weld(points, np.array([[0, 1, 2], [3, 4, 5]]), tolerance=t)
        assert len(set(welded[0].tolist())) == 3

    def test_a_crack_a_rounding_left_is_still_closed(self):
        """One copy of one corner written a ten-thousandth of a radius off:
        two edges are open until it joins its copies, and then the box seals."""
        box = _soup(_box((0, 0, 0), (10, 10, 10), splits=0))
        v = box.vertices.copy()
        copies = np.nonzero(np.all(v == v[0], axis=1))[0]
        v[copies[1]] += (1e-4 * weld(v, box.faces)[2], 0.0, 0.0)

        assert not read_bodies(v, box.faces, tolerance=0.0).sealed
        assert read_bodies(v, box.faces).sealed


class TestSeamsAndHoles:
    def test_corners_split_along_a_seam_still_seal(self):
        """A glTF writes a corner once per texture island; the box is still
        closed, though a plain edge check calls it open."""
        seamed = _soup(_box((0, 0, 0), (10, 10, 10), splits=0))

        assert not seamed.is_watertight
        assert is_sealed(seamed)

    def test_an_open_box_is_not_sealed(self):
        box = _box((0, 0, 0), (10, 10, 10), splits=0)
        keep = np.ones(len(box.faces), bool)
        keep[0] = False
        open_box = trimesh.Trimesh(box.vertices, box.faces[keep], process=False)
        reading = read_mesh(open_box)

        assert reading.piece_count == 1
        assert not reading.sealed
        assert reading.open_edges == 3

    def test_a_triangle_soup_reads_like_the_mesh(self):
        mesh = _stack()

        assert read_triangles(mesh.triangles).piece_count == read_mesh(mesh).piece_count

    def test_an_empty_mesh_has_no_pieces(self):
        reading = read_bodies(np.zeros((0, 3)), np.zeros((0, 3), int))

        assert reading.piece_count == 0
        assert not reading.sealed
