"""The corners at a part's base are the corners of its footprint.

``sharp_corners_at_base`` counted every triangle corner under 90 degrees in the
bottom 5 mm of the mesh.  That is a count of how the file was cut into
triangles, not of the shape: a round 80 mm coaster read 32, 64, 128 and 512
"sharp corners" at 16, 32, 64 and 256 sections as one exporter cuts it (34 to
514 as the fan below does), and so scored 94 where a square of the same size
scored 100.  The same square cut into finer triangles read 6146 and lost the
same 6 points.

Now the count is read on the outline of the part's bed contact: a place where
that outline turns toward the material by 88 degrees or more within 1 mm of
it.  It is the same for every way of cutting the same shape into triangles,
a circle has none, a square has four, an inside corner and the corners of a
hole never count, and each island of contact is counted on its own.

The advice reads it too.  It used to say "Sharp corners at the base are prone
to curling." past five of the old count, so it fired on nearly every round
part and could never fire on the new count.  It now names the count, and
speaks only when the part's warping risk is moderate or above: a low-risk
part with four corners is not told about them.
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


# ---------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------

Tri = tuple[tuple[float, float, float], ...]


def _write_stl(path: Path, tris: list[Tri]) -> str:
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for tri in tris:
            f.write(struct.pack("<fff", 0.0, 0.0, 0.0))
            for v in tri:
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))
    return str(path)


def _prism(poly, height: float = 5.0, kernel=(0.0, 0.0)) -> list[Tri]:
    """*poly* (counter-clockwise, star-shaped from *kernel*) raised *height*
    mm off the bed: bottom and top fanned from *kernel*, which may be one of
    its corners, and one quad per side."""
    pts = [tuple(map(float, p)) for p in poly]
    kx, ky = map(float, kernel)
    tris: list[Tri] = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1], strict=True):
        if (kx, ky) not in ((ax, ay), (bx, by)):  # a fan from a corner skips its own two sides
            tris.append(((kx, ky, 0.0), (bx, by, 0.0), (ax, ay, 0.0)))
            tris.append(((kx, ky, height), (ax, ay, height), (bx, by, height)))
        tris.append(((ax, ay, 0.0), (bx, by, 0.0), (bx, by, height)))
        tris.append(((ax, ay, 0.0), (bx, by, height), (ax, ay, height)))
    return tris


def _regular(n: int, radius: float) -> list[tuple[float, float]]:
    return [(radius * math.cos(2 * math.pi * k / n), radius * math.sin(2 * math.pi * k / n)) for k in range(n)]


def _star(points: int = 5, outer: float = 25.0, inner: float = 10.0) -> list[tuple[float, float]]:
    out = []
    for k in range(2 * points):
        r = outer if k % 2 == 0 else inner
        a = math.pi / 2 + k * math.pi / points
        out.append((r * math.cos(a), r * math.sin(a)))
    return out


_SQUARE_40 = [(-20, -20), (20, -20), (20, 20), (-20, 20)]
_L_BRACKET = [(-20, -20), (20, -20), (20, -10), (-10, -10), (-10, 20), (-20, 20)]
_CHAMFERED_40 = [(-17, -20), (17, -20), (20, -17), (20, 17), (17, 20), (-17, 20), (-20, 17), (-20, -17)]


def _box_tris(w: float, d: float, h: float) -> list[Tri]:
    """The 12-triangle box every STL exporter writes for a plain block."""
    return _prism([(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)], h, kernel=(-w / 2, -d / 2))


def _mesh_tris(mesh) -> list[Tri]:
    v = np.asarray(mesh.vertices, dtype=float)
    return [tuple(tuple(map(float, v[i])) for i in face) for face in np.asarray(mesh.faces)]


def _extruded(cross_section, height: float) -> list[Tri]:
    from manifold3d import Manifold

    m = Manifold.extrude(cross_section, height).to_mesh()
    v = np.asarray(m.vert_properties)[:, :3]
    return [tuple(tuple(map(float, v[i])) for i in face) for face in np.asarray(m.tri_verts)]


def _corners(tmp_path: Path, tris: list[Tri], name: str = "part") -> int:
    """``sharp_corners_at_base`` as the analysis reads it from the file."""
    from kiln import printability as P

    triangles, vertices, bbox, _z = P._load_mesh_for_analysis(_write_stl(tmp_path / f"{name}.stl", tris))
    return P._analyze_warping(triangles, vertices, bbox).sharp_corners_at_base


def _block_kiln_pro(monkeypatch) -> None:
    """No paid package in reach, so the verdict is public Kiln's own.  Its
    submodules are blocked too: one already imported would still answer a
    ``from`` import with its parent blocked."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def free_tier(monkeypatch):
    _block_kiln_pro(monkeypatch)


def _corner_lines(report) -> list[str]:
    return [line for line in report.warping.recommendations if "outside corner" in line.lower()]


# ---------------------------------------------------------------------------
# 1. A round coaster has no corners, at any resolution
# ---------------------------------------------------------------------------


class TestTheCoaster:
    @pytest.mark.parametrize("sections", [16, 64, 256])
    def test_a_round_coaster_has_no_corners_and_loses_nothing_for_them(self, tmp_path, free_tier, sections):
        from kiln.printability import analyze_printability

        path = _write_stl(tmp_path / f"coaster_{sections}.stl", _prism(_regular(sections, 40.0), 5.0))
        pla = analyze_printability(path, material="pla")
        abs_ = analyze_printability(path, material="abs")

        assert pla.warping.sharp_corners_at_base == 0
        assert pla.score == 100, pla.warping
        assert abs_.warping.sharp_corners_at_base == 0
        assert abs_.warping.risk_level == "moderate"
        assert abs_.adhesion.brim_width_mm == 5


# ---------------------------------------------------------------------------
# 2. A square is four corners however it is cut into triangles
# ---------------------------------------------------------------------------


class TestTheSquare:
    def test_coarse_and_fine_squares_read_four_and_score_alike(self, tmp_path, free_tier):
        from kiln.printability import analyze_printability

        coarse_tris = _box_tris(80.0, 80.0, 5.0)
        mesh = trimesh.Trimesh(*_vertices_faces(coarse_tris), process=True)
        v, f = trimesh.remesh.subdivide_to_size(mesh.vertices, mesh.faces, max_edge=4.0)
        fine_tris = _mesh_tris(trimesh.Trimesh(v, f, process=False))
        assert len(coarse_tris) == 12 and len(fine_tris) > 1000  # the premise

        coarse = analyze_printability(_write_stl(tmp_path / "coarse.stl", coarse_tris), material="pla")
        fine = analyze_printability(_write_stl(tmp_path / "fine.stl", fine_tris), material="pla")

        assert coarse.warping.sharp_corners_at_base == 4
        assert fine.warping.sharp_corners_at_base == 4
        assert fine.score == coarse.score
        assert fine.warping.risk_level == coarse.warping.risk_level


def _vertices_faces(tris: list[Tri]):
    pts = np.asarray(tris, dtype=float).reshape(-1, 3)
    return pts, np.arange(len(pts)).reshape(-1, 3)


# ---------------------------------------------------------------------------
# 3. The shapes, one by one
# ---------------------------------------------------------------------------


def _pure_shapes() -> dict[str, tuple[list[Tri], int]]:
    return {
        "square_80": (_box_tris(80.0, 80.0, 5.0), 4),
        "square_20": (_box_tris(20.0, 20.0, 5.0), 4),
        "triangle": (_prism(_regular(3, 20.0)), 3),
        "hexagon": (_prism(_regular(6, 20.0)), 0),
        "octagon": (_prism(_regular(8, 20.0)), 0),
        "star_5": (_prism(_star()), 5),
        "chamfered_square": (_prism(_CHAMFERED_40), 0),
        "l_bracket": (_prism(_L_BRACKET, kernel=(-15.0, -15.0)), 5),
        "strip_100x5": (_box_tris(100.0, 5.0, 2.0), 4),
    }


@pytest.mark.parametrize("name", sorted(_pure_shapes()))
def test_each_shape_reads_its_own_corners(tmp_path, name):
    tris, expected = _pure_shapes()[name]
    assert _corners(tmp_path, tris, name) == expected


def _manifold_shapes() -> dict[str, tuple[object, float, int]]:
    """Name -> (cross-section, height, corners).  Built with the boolean
    library the other printability tests use, for the shapes with holes and
    rounded corners."""
    from manifold3d import CrossSection, JoinType

    def sq(w, d=None):
        return CrossSection.square([w, d or w], center=True)

    def rounded(r, per_quarter):
        return sq(40 - 2 * r).offset(r, JoinType.Round, 2.0, 4 * per_quarter)

    shapes: dict[str, tuple[object, float, int]] = {}
    for q in (1, 2, 4, 8, 16, 32):
        shapes[f"rounded_r3_q{q}"] = (rounded(3.0, q), 5.0, 0)
    for r, n in ((0.25, 4), (0.5, 4), (0.65, 0), (1.0, 0), (2.0, 0), (5.0, 0)):
        shapes[f"rounded_r{r}_q16"] = (rounded(r, 16), 5.0, n)
    slot = sq(22, 8) + CrossSection.circle(4, 64).translate([-11, 0]) + CrossSection.circle(4, 64).translate([11, 0])
    shapes["slot_plate"] = (sq(60, 30) - slot, 4.0, 4)
    key = CrossSection.circle(5, 64).translate([0, 3]) + sq(4, 10).translate([0, -3])
    shapes["keyhole_plate"] = (sq(40, 40) - key, 4.0, 4)
    shapes["frame"] = (sq(40) - sq(30), 5.0, 4)
    shapes["coaster_fn128"] = (CrossSection.circle(40, 128), 5.0, 0)
    return shapes


_MANIFOLD_NAMES = [
    *(f"rounded_r3_q{q}" for q in (1, 2, 4, 8, 16, 32)),
    *(f"rounded_r{r}_q16" for r in (0.25, 0.5, 0.65, 1.0, 2.0, 5.0)),
    "slot_plate", "keyhole_plate", "frame", "coaster_fn128",
]


@pytest.mark.parametrize("name", _MANIFOLD_NAMES)
def test_holes_and_rounded_corners(tmp_path, name):
    pytest.importorskip("manifold3d")
    cross_section, height, expected = _manifold_shapes()[name]
    assert _corners(tmp_path, _extruded(cross_section, height), name) == expected


def test_each_island_of_contact_counts_its_own_corners(tmp_path):
    """A table on four 6 mm square feet stands on four squares."""
    tris: list[Tri] = []
    for dx in (-27.0, 27.0):
        for dy in (-27.0, 27.0):
            tris += [tuple((x + dx, y + dy, z) for x, y, z in t) for t in _box_tris(6.0, 6.0, 5.0)]
    tris += [tuple((x, y, z + 5.0) for x, y, z in t) for t in _box_tris(60.0, 60.0, 3.0)]
    assert _corners(tmp_path, tris, "table") == 16


def test_parts_touching_at_one_point_each_keep_their_corners(tmp_path):
    """Two 20 mm squares meeting corner to corner, the file's first edge
    leaving the point they share: each is its own ring of four corners,
    whichever edge the outline is read from."""
    shared = (0.0, 0.0)
    right = _prism([shared, (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)], kernel=shared)
    left = _prism([shared, (-20.0, 0.0), (-20.0, -20.0), (0.0, -20.0)][::-1], kernel=shared)
    assert _corners(tmp_path, right + left, "touching") == 8
    assert _corners(tmp_path, left + right, "touching_other_way") == 8


def test_a_sphere_and_a_cone_on_its_tip_have_none(tmp_path):
    sphere = trimesh.creation.uv_sphere(radius=20, count=[32, 64])
    sphere.apply_translation([0, 0, 20])
    cone = trimesh.creation.cone(radius=15, height=30, sections=64)
    cone.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0]))
    cone.apply_translation([0, 0, -cone.bounds[0][2]])
    assert _corners(tmp_path, _mesh_tris(sphere), "sphere") == 0
    assert _corners(tmp_path, _mesh_tris(cone), "cone") == 0


# ---------------------------------------------------------------------------
# 4. What the part is told
# ---------------------------------------------------------------------------


class TestTheAdvice:
    def test_a_low_risk_part_is_not_told_about_its_corners(self, tmp_path, free_tier):
        from kiln.printability import analyze_printability

        cube = analyze_printability(_write_stl(tmp_path / "cube.stl", _box_tris(20.0, 20.0, 20.0)), material="pla")

        assert cube.warping.risk_level == "low" and cube.warping.sharp_corners_at_base == 4  # the premise
        assert _corner_lines(cube) == []

    def test_a_warping_part_is_told_how_many_and_not_handed_a_brim(self, tmp_path, free_tier):
        from kiln.printability import analyze_printability

        plate = analyze_printability(_write_stl(tmp_path / "plate.stl", _box_tris(80.0, 80.0, 10.0)), material="abs")

        assert plate.warping.risk_level in ("moderate", "high", "critical")  # the premise
        lines = _corner_lines(plate)
        assert len(lines) == 1, plate.warping.recommendations
        assert "4" in lines[0]
        assert "brim" not in lines[0].lower()


# ---------------------------------------------------------------------------
# 5. Nothing about how the file is cut moves the count
# ---------------------------------------------------------------------------


_SWEEP_BASES: dict[str, tuple[list[tuple[float, float]], tuple[float, float], int]] = {
    "square": (_SQUARE_40, (0.0, 0.0), 4),
    "triangle": (_regular(3, 20.0), (0.0, 0.0), 3),
    "l_bracket": (_L_BRACKET, (-15.0, -15.0), 5),
    "star_5": (_star(), (0.0, 0.0), 5),
    "hexagon": (_regular(6, 20.0), (0.0, 0.0), 0),
    "circle_64": (_regular(64, 20.0), (0.0, 0.0), 0),
}


def _with_collinear_points(poly, rng) -> list[tuple[float, float]]:
    """Every edge of *poly* with up to three extra points along it."""
    out: list[tuple[float, float]] = []
    for a, b in zip(poly, poly[1:] + poly[:1], strict=True):
        out.append(a)
        for t in sorted(rng.uniform(0.05, 0.95, int(rng.integers(0, 4)))):
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def _finer_bottom(tris: list[Tri], rng, rounds: int = 3) -> list[Tri]:
    """The bottom's triangles split at points inside them, *rounds* times:
    more, smaller triangles, the same outline."""
    for _ in range(rounds):
        out: list[Tri] = []
        for t in tris:
            if max(v[2] for v in t) > 0.0:
                out.append(t)
                continue
            w = rng.dirichlet([1.0, 1.0, 1.0]) * 0.8 + 0.2 / 3.0
            q = tuple(float(sum(w[i] * t[i][k] for i in range(3))) for k in range(3))
            out += [(t[0], t[1], q), (t[1], t[2], q), (t[2], t[0], q)]
        tris = out
    return tris


def _moved(tris: list[Tri], rng, *, mirror: bool) -> list[Tri]:
    a = rng.uniform(0.0, 2.0 * math.pi)
    c, s = math.cos(a), math.sin(a)
    dx, dy, dz = rng.uniform(-80.0, 80.0, 3)
    sx = -1.0 if mirror else 1.0

    def at(v):
        x, y = sx * v[0], v[1]
        return (c * x - s * y + dx, s * x + c * y + dy, v[2] + dz)

    return [tuple(at(v) for v in (t[::-1] if mirror else t)) for t in tris]


@pytest.mark.parametrize("name", sorted(_SWEEP_BASES))
def test_the_count_survives_how_the_file_is_cut(tmp_path, name):
    rng = np.random.default_rng(sum(map(ord, name)))
    poly, kernel, expected = _SWEEP_BASES[name]
    variants = {
        "as drawn": _prism(poly, kernel=kernel),
        "points along the edges": _prism(_with_collinear_points(list(poly), rng), kernel=kernel),
        "finer bottom": _finer_bottom(_prism(poly, kernel=kernel), rng),
        "turned and moved": _moved(_prism(poly, kernel=kernel), rng, mirror=False),
        "mirrored": _moved(_prism(poly, kernel=kernel), rng, mirror=True),
        "all of it": _moved(_finer_bottom(_prism(_with_collinear_points(list(poly), rng), kernel=kernel), rng), rng, mirror=True),
    }
    got = {label: _corners(tmp_path, tris, f"{name}_{i}") for i, (label, tris) in enumerate(variants.items())}
    assert got == dict.fromkeys(variants, expected)


def test_regular_polygons_have_corners_only_below_five_sides(tmp_path):
    got = {n: _corners(tmp_path, _prism(_regular(n, 20.0)), f"poly_{n}") for n in range(3, 13)}
    assert got == {n: (n if n <= 4 else 0) for n in range(3, 13)}


def test_a_circle_has_no_corners_at_any_facet_count(tmp_path):
    rng = np.random.default_rng(2026)
    sections = (12, 16, 24, 32, 48, 64, 96, 128, 256, 512, 1024)
    got = {n: _corners(tmp_path, _prism(_regular(n, float(rng.uniform(2.0, 60.0)))), f"circle_{n}") for n in sections}
    assert got == dict.fromkeys(sections, 0)
