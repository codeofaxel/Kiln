"""A sharp corner is not a wall.

Text a product generator sinks into a part leaves sharp corners of the part
between the strokes of a letter: at the crotch of an m, the material between
the stem and the arch narrows to a point.  The wall check measured that point
(0.11 mm on a "Home" keychain, 2026-10-23) and called the part "Won't print as
it is", with advice nobody could act on: "Thicken it to at least 0.8 mm".  A
corner whose two faces close into an edge is now told apart from a wall whose
faces stay apart; every wall thinner than the nozzle is still a wall.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln.printability import analyze_printability, verdict  # noqa: E402

NOZZLE = 0.4


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _prism(outline, z0: float, z1: float, *, max_edge: float = 0.3):
    """A closed vertical prism over a convex outline (anticlockwise), its
    sides split into a grid no coarser than *max_edge*, so the wall check
    has faces near every corner to measure from."""
    pts = np.asarray(outline, dtype=float)
    ring = []
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        n = max(1, int(np.ceil(np.linalg.norm(b - a) / max_edge)))
        ring += [a + (b - a) * k / n for k in range(n)]
    ring = np.asarray(ring)
    layers = max(1, int(np.ceil((z1 - z0) / max_edge)))
    m = len(ring)
    centre = ring.mean(axis=0)
    verts = np.vstack(
        [np.c_[ring, np.full(m, z)] for z in np.linspace(z0, z1, layers + 1)]
        + [np.array([[centre[0], centre[1], z0], [centre[0], centre[1], z1]])]
    )
    bottom, top = len(verts) - 2, len(verts) - 1
    faces = []
    for layer in range(layers):
        for i in range(m):
            j = (i + 1) % m
            a, b = layer * m + i, layer * m + j
            faces += [[a, b, b + m], [a, b + m, a + m]]
    for i in range(m):
        j = (i + 1) % m
        faces += [[bottom, j, i], [top, layers * m + i, layers * m + j]]
    mesh = trimesh.Trimesh(verts, np.asarray(faces), process=True)
    if mesh.volume < 0:
        mesh.invert()
    assert mesh.is_volume
    return mesh


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _spike(angle_deg: float):
    """A block with an upright spike of *angle_deg* sticking out of one side."""
    half = np.tan(np.radians(angle_deg / 2.0))
    spike = _prism([(4.5, -6.5 * half), (11.0, 0.0), (4.5, 6.5 * half)], 0.0, 4.0)
    return trimesh.boolean.union([_box((-5, -5, 0), (5, 5, 4)), spike])


def _crotch():
    """A plate with two strokes sunk 0.8 mm into its top, the second leaning
    off the first by 27 degrees from where they join: the plate between them,
    above the join, narrows to a point, as at the crotch of a letter."""
    plate = _box((-12, -12, 0), (12, 12, 3))
    stem = _prism([(-0.5, -6.0), (0.5, -6.0), (0.5, 6.0), (-0.5, 6.0)], 2.2, 3.5)
    arm = _prism([(-0.5, 0.0), (0.5, 0.0), (0.5, 9.0), (-0.5, 9.0)], 2.2, 3.5)
    arm.apply_transform(trimesh.transformations.rotation_matrix(np.radians(-27.0), (0, 0, 1)))
    arm.apply_translation((0.5, -4.0, 0.0))
    return plate.difference(trimesh.boolean.union([stem, arm]))


def _fin(width: float, *, point: bool = False, top: float | None = None):
    """A plate with an upright fin *width* thick: square-ended, ending in a
    point, or narrowing to *top* at its top."""
    plate = _box((-15, -10, 0), (15, 10, 2))
    if top is not None:
        section = _prism([(0.0, -width / 2), (5.01, -top / 2), (5.01, top / 2), (0.0, width / 2)], -8.0, 8.0)
        fin = trimesh.Trimesh(np.asarray(section.vertices)[:, [2, 1, 0]] + (0.0, 0.0, 1.99), section.faces)
        if fin.volume < 0:
            fin.invert()
    elif point:
        fin = _prism([(-8, -width / 2), (6, -width / 2), (9, 0.0), (6, width / 2), (-8, width / 2)], 1.99, 7.0)
    else:
        fin = _box((-8, -width / 2, 1.99), (8, width / 2, 7.0))
    return trimesh.boolean.union([plate, fin])


def _notch():
    """A plate with a V-shaped notch sunk into its top: the notch's air
    narrows to an edge."""
    plate = _box((-10, -10, 0), (10, 10, 3))
    half = 3.0 * np.tan(np.radians(12.5))
    return plate.difference(_prism([(-3.0, -2 * half), (3.01, 0.0), (-3.0, 2 * half)], 2.0, 3.5))


def _report(tmp_path: Path, name: str, mesh):
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    return analyze_printability(str(path), nozzle_diameter=NOZZLE, material="pla")


class TestACornerIsNotAWall:
    @pytest.mark.parametrize("angle", [15.0, 27.0])
    def test_a_sharp_spike_prints(self, tmp_path, angle):
        report = _report(tmp_path, f"spike_{angle:g}", _spike(angle))
        walls = report.thin_walls
        assert (walls.thin_wall_count, walls.problematic_regions) == (0, []), walls
        assert walls.min_wall_thickness_mm >= NOZZLE, walls
        assert verdict(report)[0] == "ready", verdict(report)

    def test_the_crotch_of_a_sunk_letter_prints(self, tmp_path):
        report = _report(tmp_path, "crotch", _crotch())
        assert report.thin_walls.problematic_regions == [], report.thin_walls
        assert verdict(report)[0] == "ready", verdict(report)

    def test_the_air_at_a_notch_tip_is_not_a_narrow_gap(self, tmp_path):
        cavities = _report(tmp_path, "notch", _notch()).cavities
        assert cavities.min_cavity_width_mm == 0.0 or cavities.min_cavity_width_mm >= NOZZLE, cavities

    def test_no_corner_reaches_the_walls_a_material_floor_lists(self):
        from kiln.printability import _analyze_thin_walls

        tris = np.asarray(_spike(15.0).triangles).tolist()
        walls = _analyze_thin_walls(tris, [], nozzle_diameter=NOZZLE, thin_below_mm=1.2)
        assert walls.threshold_basis == "material"
        assert walls.problematic_regions == [], walls
        assert walls.min_wall_thickness_mm >= 1.2, walls


class TestAWallIsStillAWall:
    def test_a_fin_thinner_than_the_nozzle(self, tmp_path):
        report = _report(tmp_path, "fin", _fin(0.3))
        assert report.thin_walls.min_wall_thickness_mm == pytest.approx(0.3, abs=0.02)
        assert verdict(report)[0] == "wont_print"

    def test_a_fin_ending_in_a_point_is_judged_by_its_wall_not_its_point(self, tmp_path):
        report = _report(tmp_path, "pointed_fin", _fin(0.3, point=True))
        assert report.thin_walls.min_wall_thickness_mm == pytest.approx(0.3, abs=0.02)
        assert {round(r["thickness_mm"], 1) for r in report.thin_walls.problematic_regions} == {0.3}
        assert verdict(report)[0] == "wont_print"

    def test_a_fin_narrowing_to_a_flat_top(self, tmp_path):
        report = _report(tmp_path, "tapered_fin", _fin(0.39, top=0.25))
        walls = report.thin_walls
        assert walls.min_wall_thickness_mm == pytest.approx(0.25, abs=0.02)
        # A wall from its base to its top: its run ends 0.25 mm wide, so no
        # part of it is a corner, the thick foot included.
        assert max(r["thickness_mm"] for r in walls.problematic_regions) > 0.33, walls.problematic_regions
        assert verdict(report)[0] == "wont_print"


# ---------------------------------------------------------------------------
# The corner reading's bound: an upload can be cut very fine
# ---------------------------------------------------------------------------


def _grooved_plate(grooves: int, splits: int) -> np.ndarray:
    """A 100 x 40 x 4 mm plate with *grooves* V grooves across its top, each
    20 degrees and 1.5 mm deep, every face split in four *splits* times: the
    air of every groove narrows to an edge, so every chord across one is a
    corner to walk.  One body, as ``(T, 3, 3)`` triangles."""
    plate = _box((0, 0, 0), (100, 40, 4))
    half = np.tan(np.radians(10.0)) * 1.5
    cutters = []
    for k in range(grooves):
        x = (k + 0.5) * 100.0 / grooves
        ends = [[x - half, y, 4.01] for y in (-1, 41)] + [[x + half, y, 4.01] for y in (-1, 41)]
        ends += [[x, y, 2.5] for y in (-1, 41)]
        cutters.append(trimesh.convex.convex_hull(np.asarray(ends)))
    part = plate.difference(trimesh.boolean.union(cutters))
    for _ in range(splits):
        part = part.subdivide()
    assert part.is_volume and len(part.split(only_watertight=False)) == 1
    return np.asarray(part.triangles)


@pytest.fixture
def corner_reads(monkeypatch):
    """How many triangles the corner walks read a point against, in all."""
    from kiln import printability as _p

    real = _p._nearest_crossings
    reads = [0]

    def counted(points, directions, v0, edge1, edge2, *, window):
        reads[0] += points.shape[0] * v0.shape[0]
        return real(points, directions, v0, edge1, edge2, window=window)

    monkeypatch.setattr(_p, "_nearest_crossings", counted, raising=False)
    return reads


class TestTheCornerReadingIsBounded:
    def test_a_part_too_fine_to_read_whole_takes_no_walk_and_keeps_its_gaps(self, corner_reads):
        """Past the cap the cast reads a sample of a body, and a walk read
        against a sample can find a run ending where it goes on.  A hosted
        upload of 160 grooves cut into 123,648 triangles takes no corner walk
        at all, in bounded memory and time, and every groove stays a narrow
        gap: never cleared for want of a reading."""
        import time
        import tracemalloc

        from kiln import printability as _p

        tris = _grooved_plate(160, 3)
        assert len(tris) > _p._THIN_WALL_INTERSECTION_TRI_CAP
        tracemalloc.start()
        started = time.perf_counter()
        try:
            gaps = _p._analyze_cavity_widths(tris, [], nozzle_diameter=NOZZLE)
            took = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert corner_reads[0] == 0
        assert gaps.problematic_regions and gaps.min_cavity_width_mm < NOZZLE, gaps
        assert peak < 400e6, peak
        assert took < 60.0, took

    def test_the_walks_of_one_analysis_read_no_more_than_their_budget(self, tmp_path, corner_reads, monkeypatch):
        """Read in full, every groove of a plate of 19,392 triangles is a
        corner.  With the budget lowered below what that takes, the walls and
        the gaps of the whole analysis read no more than it, and the grooves
        left unread stay narrow gaps."""
        from kiln import printability as _p

        tris = _grooved_plate(100, 2)
        assert _p._analyze_cavity_widths(tris, [], nozzle_diameter=NOZZLE).problematic_regions == []
        read_in_full = corner_reads[0]
        corner_reads[0] = 0
        monkeypatch.setattr(_p, "_CORNER_READ_BUDGET", read_in_full // 8, raising=False)
        path = tmp_path / "grooves.stl"
        trimesh.Trimesh(tris.reshape(-1, 3), np.arange(len(tris) * 3).reshape(-1, 3)).export(path)
        report = analyze_printability(str(path), nozzle_diameter=NOZZLE, material="pla")
        assert 0 < corner_reads[0] <= read_in_full // 8, (corner_reads[0], read_in_full)
        assert report.cavities.problematic_regions, report.cavities


# ---------------------------------------------------------------------------
# What the rule stands on, measured in the slicer Kiln drives
# ---------------------------------------------------------------------------


def _prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    mac = Path("/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")
    return found or (str(mac) if mac.exists() else None)


_MOVE = re.compile(r"^G1 X(-?[\d.]+) Y(-?[\d.]+)(?: Z[-\d.]+)? E([\d.]+)")


def _layers(gcode: Path) -> dict[float, np.ndarray]:
    """Each layer's height and the ``(x0, y0, x1, y1, width)`` of every
    extrusion in it."""
    out: dict[float, list[tuple[float, ...]]] = {}
    layer, width, x, y = None, 0.0, 0.0, 0.0
    for line in gcode.read_text().splitlines():
        if line.startswith(";Z:"):
            layer = float(line[3:])
        elif line.startswith(";WIDTH:"):
            width = float(line[7:])
        elif line.startswith(("G1", "G0")):
            hit = _MOVE.match(line)
            nx = float(re.search(r"X(-?[\d.]+)", line).group(1)) if " X" in line else x
            ny = float(re.search(r"Y(-?[\d.]+)", line).group(1)) if " Y" in line else y
            if hit and layer is not None:
                out.setdefault(layer, []).append((x, y, nx, ny, width))
            x, y = nx, ny
    return {z: np.asarray(beads, dtype=float).reshape(-1, 5) for z, beads in out.items()}


def _covered(point: np.ndarray, beads: np.ndarray) -> bool:
    a, b, w = beads[:, 0:2], beads[:, 2:4], beads[:, 4]
    ab = b - a
    t = np.clip(((point - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-12), 0.0, 1.0)
    return bool((np.linalg.norm(a + t[:, None] * ab - point, axis=1) <= w / 2.0).any())


@pytest.mark.skipif(_prusaslicer() is None, reason="PrusaSlicer is not installed")
def test_the_premise_a_slicer_prints_a_corner_and_leaves_out_a_hairline_fin(tmp_path):
    """Kiln's own slice of a 27 degree spike and of a 0.1 mm fin: the spike is
    printed to within a millimetre of its point, the fin not at all."""
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    profile = resolve_slicer_profile("bambu_a1")

    def sliced(name: str, mesh) -> Path:
        mesh.apply_translation((128.0, 128.0, 0.0))
        stl = tmp_path / f"{name}.stl"
        mesh.export(stl)
        result = slice_file(
            str(stl),
            profile=profile,
            output_dir=str(tmp_path),
            output_name=f"{name}.gcode",
            extra_args=["--dont-arrange"],
        )
        return Path(result.output_path)

    layers = _layers(sliced("spike", _spike(27.0)))
    middle = layers[min(layers, key=lambda z: abs(z - 2.0))]
    point = np.array([128.0 + 11.0, 128.0])
    reach = next(s for s in np.arange(0.0, 3.0, 0.02) if _covered(point - (s, 0.0), middle))
    assert reach < 1.0, f"the spike's print stops {reach:.2f} mm short of its point"

    # Above the plate, nothing is laid on the fin's line.
    above = [b for z, b in _layers(sliced("fin", _fin(0.1))).items() if z > 2.3]
    on_fin = [
        b[(np.abs((b[:, 0] + b[:, 2]) / 2 - 128.0) < 7.5) & (np.abs((b[:, 1] + b[:, 3]) / 2 - 128.0) < 0.3)]
        for b in above
    ]
    assert sum(len(b) for b in on_fin) == 0, "the 0.1 mm fin was printed"
