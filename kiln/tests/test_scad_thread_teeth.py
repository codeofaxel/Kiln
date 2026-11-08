"""Every thread the SCAD library makes has real teeth: cut, sliced and mated.

A thread module that hulls pairs of flat discs makes each "tooth" a helical
ribbon a few hundredths of a millimetre tall: a vertical cut shows flakes
that stop short of the crest, the G-code puts plastic past the crest on only
a sliver of each layer, a nut comes out a plain hole, and a jar has a plain
band where its thread should be, and no lid.

Watertightness and component counts prove none of this: every such part
passes both.  So these tests judge the teeth, three ways:

* a vertical cut through the compiled part shows a tooth of the stated depth,
  45-degree flanks and the stated axial width;
* the G-code Kiln's own slicing path writes from it reaches the crest on every
  mid-thread layer, and all the way round over one pitch of layers (skipped
  where no slicer is installed: public CI has none);
* each mating pair -- bolt in nut, bolt in a tapped hole, lid on jar -- sits
  the library's clearance apart on every surface, with no overlap.

Every part is compiled the way the generator compiles it: the whole library
put ahead of the call, Kiln's curve rule applied, through ``run_openscad``.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from pathlib import Path

import pytest

from kiln.curve_resolution import apply_rule
from kiln.generation.scad_library import get_library_source
from kiln.openscad_runner import run_openscad

trimesh = pytest.importorskip("trimesh", reason="needs trimesh to measure the solid")
np = pytest.importorskip("numpy")


def _openscad() -> str | None:
    exe = shutil.which("openscad")
    if exe:
        return exe
    mac = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    return mac if os.path.isfile(mac) else None


def _prusaslicer() -> str | None:
    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


_OPENSCAD = _openscad()
_PRUSA = _prusaslicer()

pytestmark = pytest.mark.skipif(_OPENSCAD is None, reason="OpenSCAD not installed on this machine")

#: The library's mating clearance, radial, on every surface (threads.scad).
CLEARANCE = 0.3
#: Height step of the vertical cuts, mm.
DZ = 0.005
#: Vertical cuts taken at these angles (degrees): none on a ridge's end face.
ANGLES = (37, 109, 181, 253, 325)


# ---------------------------------------------------------------------------
# Building and measuring
# ---------------------------------------------------------------------------


def _compile(code: str, tmp_path: Path, name: str = "part"):
    """The part *code* makes, built as the generator builds it."""
    source = apply_rule(get_library_source() + "\n\n// === USER-GENERATED CODE ===\n\n" + code)
    scad, stl = tmp_path / f"{name}.scad", tmp_path / f"{name}.stl"
    scad.write_text(source, encoding="utf-8")
    result = run_openscad([_OPENSCAD, "-o", str(stl), str(scad)], timeout=600,
                          cwd=str(tmp_path), output_path=str(stl))
    assert result.returncode == 0, (result.stderr or "")[-800:]
    return trimesh.load(str(stl))


def _surface(mesh, theta: float, zs, *, outer: bool):
    """Radius of the part's outer (or inner) surface at each height in *zs*,
    read off a vertical cut along the half-plane at *theta* degrees."""
    t = math.radians(theta)
    along = np.array([math.cos(t), math.sin(t), 0.0])
    segs = trimesh.intersections.mesh_plane(
        mesh, plane_normal=[-math.sin(t), math.cos(t), 0.0], plane_origin=[0, 0, 0])
    rho = segs @ along
    keep = (rho > 0).all(axis=1)
    rho, z = rho[keep], segs[keep][:, :, 2]
    lo, hi = z.min(axis=1), z.max(axis=1)
    out = np.full(len(zs), np.nan)
    for j, height in enumerate(zs):
        hit = (lo <= height) & (height <= hi) & (hi > lo)
        if hit.any():
            r = rho[hit, 0] + (height - z[hit, 0]) / (z[hit, 1] - z[hit, 0]) * (rho[hit, 1] - rho[hit, 0])
            out[j] = r.max() if outer else r.min()
    return out


def _whole_runs(mask) -> list[int]:
    """Lengths of the runs of True that end inside *mask* at both ends."""
    runs, start = [], None
    for i, on in enumerate(mask):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if start > 0:
                runs.append(i - start)
            start = None
    return runs


def _assert_teeth(mesh, *, outer: bool, crest: float, root: float, spacing: float,
                  z0: float, z1: float, label: str) -> None:
    """Every cut through z0..z1 shows teeth from *root* to *crest*: 45-degree
    flanks, and half the spacing wide at half their depth."""
    zs = np.arange(z0, z1, DZ)
    middle = (crest + root) / 2
    for theta in ANGLES:
        r = _surface(mesh, theta, zs, outer=outer)
        assert not np.isnan(r).any(), f"{label}: the cut at {theta} deg misses the part"
        tip, base = (r.max(), r.min()) if outer else (r.min(), r.max())
        where = f"{label}, cut at {theta} deg"
        assert tip == pytest.approx(crest, abs=0.03), f"{where}: teeth reach {tip:.3f} mm, crest is {crest}"
        assert base == pytest.approx(root, abs=0.03), f"{where}: roots at {base:.3f} mm, root is {root}"
        steepest = float(np.max(np.abs(np.diff(r))) / DZ)
        assert steepest <= 1.1, f"{where}: a flank runs {math.degrees(math.atan(steepest)):.0f} deg from the axis"
        widths = [n * DZ for n in _whole_runs(r > middle if outer else r < middle)]
        assert widths, f"{where}: no whole tooth crosses half depth"
        for width in widths:
            assert width == pytest.approx(spacing / 2, abs=0.05), (
                f"{where}: a tooth {width:.3f} mm tall at half depth, expected {spacing / 2}")


def _jar_and_lid_as_made(mesh):
    """screw_container() makes the jar and, beside it, its lid."""
    bodies = sorted(mesh.split(only_watertight=False), key=lambda b: b.bounds[:, 0].mean())
    assert len(bodies) == 2, f"screw_container() made {len(bodies)} part(s): a jar and its lid expected"
    return bodies


def _jar_and_lid(mesh):
    """The jar where it stands, and the lid moved onto the jar's axis."""
    jar, lid = _jar_and_lid_as_made(mesh)
    lid = lid.copy()
    lid.apply_translation([*(-lid.bounds[:, :2].mean(axis=0)), 0])
    return jar, lid


def _run_center(r, zs, pick) -> float:
    """Height of the middle of the first whole run where ``pick(r)`` holds."""
    mask = pick(r)
    for i in range(1, len(mask)):
        if mask[i] and not mask[i - 1]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            if j < len(mask):
                return float((zs[i] + zs[j - 1]) / 2)
    raise AssertionError("no whole crest in the cut")


def _seated(jar, lid, *, height: float, wall: float, pitch: float):
    """The lid screwed home: turned over, floor on the rim, and turned about
    the axis to where its ridge sits centred between the neck's teeth (the
    one place a lid screwed down can be), found from the two parts' own
    cuts."""
    lid = lid.copy()
    lid.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0]))
    lid.apply_translation([0, 0, height + wall])
    zs = np.arange(height - 6 * pitch, height, DZ)
    neck = _surface(jar, 0, zs, outer=True)
    crest = _run_center(neck, zs, lambda r: r > np.nanmax(neck) - 0.02)
    inner = _surface(lid, 0, zs, outer=False)
    tip = _run_center(inner, zs, lambda r: r < np.nanmin(inner) + 0.02)
    # Turning the lid by psi lowers its ridge psi / 360 pitches.
    psi = 360 * (((tip - (crest + pitch / 2)) / pitch) % 1)
    lid.apply_transform(trimesh.transformations.rotation_matrix(math.radians(psi), [0, 0, 1]))
    return lid


def _assert_mate(inner_part, outer_part, *, z0: float, z1: float, label: str) -> None:
    """*inner_part* inside *outer_part*: no overlap, and CLEARANCE between
    them on every ray from the axis through z0..z1."""
    overlap = inner_part.intersection(outer_part)
    volume = float(overlap.volume) if len(overlap.faces) else 0.0
    assert volume < 0.01, f"{label}: the parts overlap by {volume:.3f} mm3"
    zs = np.arange(z0, z1, 0.01)
    for theta in ANGLES:
        gap = _surface(outer_part, theta, zs, outer=False) - _surface(inner_part, theta, zs, outer=True)
        assert not np.isnan(gap).any(), f"{label}: the cut at {theta} deg misses a part"
        assert gap.min() == pytest.approx(CLEARANCE, abs=0.02), f"{label} at {theta} deg: closest {gap.min():.3f} mm"
        assert gap.max() == pytest.approx(CLEARANCE, abs=0.02), f"{label} at {theta} deg: widest {gap.max():.3f} mm"


# ---------------------------------------------------------------------------
# The tooth in the cut
# ---------------------------------------------------------------------------


class TestTheToothInTheCut:
    def test_bolt(self, tmp_path):
        mesh = _compile("external_thread(diameter=10, length=16, pitch=1.5);", tmp_path)
        _assert_teeth(mesh, outer=True, crest=5.0, root=4.4, spacing=1.5, z0=3, z1=13, label="external_thread")

    def test_two_start_bolt(self, tmp_path):
        """Two starts, 3 mm per turn: the teeth are 1.5 mm apart and sized for that."""
        mesh = _compile("external_thread(diameter=10, length=16, pitch=3, starts=2);", tmp_path)
        _assert_teeth(mesh, outer=True, crest=5.0, root=4.4, spacing=1.5, z0=3, z1=13, label="two-start")

    def test_nut(self, tmp_path):
        mesh = _compile("internal_thread(diameter=10, length=12, pitch=1.5, wall=3);", tmp_path)
        _assert_teeth(mesh, outer=False, crest=4.7, root=5.3, spacing=1.5, z0=2, z1=10, label="internal_thread")

    def test_tapped_hole(self, tmp_path):
        mesh = _compile("difference() { cylinder(d=20, h=10); translate([0, 0, -0.5]) "
                        "threaded_hole(diameter=10, pitch=1.5, depth=11); }", tmp_path)
        _assert_teeth(mesh, outer=False, crest=4.7, root=5.3, spacing=1.5, z0=1, z1=9, label="threaded_hole nut")

    def test_tapped_hole_cutter_on_its_own_is_a_threaded_rod(self, tmp_path):
        """Left as a solid, the cutter is a real rod, sized as the hole it cuts."""
        mesh = _compile("threaded_hole(diameter=10, pitch=1.5, depth=20);", tmp_path)
        _assert_teeth(mesh, outer=True, crest=5.3, root=4.7, spacing=1.5, z0=2, z1=18,
                      label="threaded_hole solid")

    def test_bottle_cap(self, tmp_path):
        mesh = _compile("bottle_thread(outer_diameter=30, height=10, pitch=3, wall=2);", tmp_path)
        _assert_teeth(mesh, outer=False, crest=13.8, root=15.0, spacing=3, z0=3, z1=8.5, label="bottle_thread")

    def test_jar_neck_and_lid(self, tmp_path):
        jar, lid = _jar_and_lid(_compile("screw_container();", tmp_path))
        # Defaults: a 40 mm jar 50 mm tall, 3 mm pitch, 2 mm walls; the
        # neck's teeth stand 1.2 mm proud of the wall over its top 8 mm.
        _assert_teeth(jar, outer=True, crest=21.2, root=20.0, spacing=3, z0=43, z1=49.5, label="jar neck")
        # The lid's bore sits the clearance outside the neck's crests.
        _assert_teeth(lid, outer=False, crest=20.3, root=21.5, spacing=3, z0=3, z1=8.5, label="lid")


# ---------------------------------------------------------------------------
# The mate
# ---------------------------------------------------------------------------


@pytest.fixture
def _booleans():
    pytest.importorskip("manifold3d", reason="trimesh needs manifold3d to intersect solids")


@pytest.mark.usefixtures("_booleans")
class TestTheMate:
    def test_bolt_in_nut(self, tmp_path):
        bolt = _compile("external_thread(diameter=10, length=12, pitch=1.5);", tmp_path, "bolt")
        nut = _compile("internal_thread(diameter=10, length=12, pitch=1.5, wall=3);", tmp_path, "nut")
        _assert_mate(bolt, nut, z0=1.5, z1=10.5, label="bolt in internal_thread")

    def test_bolt_in_tapped_hole(self, tmp_path):
        bolt = _compile("translate([0, 0, -0.5]) external_thread(diameter=10, length=11, pitch=1.5);",
                        tmp_path, "bolt")
        nut = _compile("difference() { cylinder(d=20, h=10); translate([0, 0, -0.5]) "
                       "threaded_hole(diameter=10, pitch=1.5, depth=11); }", tmp_path, "nut")
        _assert_mate(bolt, nut, z0=1.0, z1=9.0, label="bolt in threaded_hole")

    def test_lid_on_jar(self, tmp_path):
        jar, lid = _jar_and_lid(_compile("screw_container();", tmp_path))
        seated = _seated(jar, lid, height=50, wall=2, pitch=3)
        _assert_mate(jar, seated, z0=45, z1=47, label="lid on jar")


# ---------------------------------------------------------------------------
# What the slicer lays down
# ---------------------------------------------------------------------------


_SKIPPED_TYPES = {"Skirt/Brim", "Custom", "Support material", "Support material interface"}


def _extrusions(gcode: str):
    """Every extrusion of the part: rows of (x0, y0, x1, y1, layer top, width)."""
    rows, x, y, z, width, kind = [], 0.0, 0.0, 0.0, 0.0, ""
    relative, last_e = True, 0.0
    number = re.compile(r"([XYE])(-?\d*\.?\d+)")
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";Z:"):
                z = float(line[3:])
            elif line.startswith(";WIDTH:"):
                width = float(line[7:])
            elif line.startswith(";TYPE:"):
                kind = line[6:].strip()
            elif line.startswith("M82"):
                relative = False
            elif line.startswith("M83"):
                relative = True
            elif line.startswith("G92"):
                found = re.search(r"E(-?\d*\.?\d+)", line)
                last_e = float(found.group(1)) if found else last_e
            elif line.startswith(("G0 ", "G1 ")):
                words = dict(number.findall(line.split(";")[0]))
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                if "E" in words:
                    e = float(words["E"])
                    advance = e if relative else e - last_e
                    if not relative:
                        last_e = e
                    if advance > 0 and (nx, ny) != (x, y) and kind not in _SKIPPED_TYPES:
                        rows.append((x, y, nx, ny, z, width))
                x, y = nx, ny
    return np.array(rows)


def _edges(rows, center, top: float, *, outer: bool):
    """The plastic's farthest (or nearest) edge from the axis at *center* on
    the layer whose top is *top*, per degree around it."""
    lay = rows[np.isclose(rows[:, 4], top)]
    edge = np.full(360, -np.inf if outer else np.inf)
    for x0, y0, x1, y1, _top, width in lay:
        steps = max(2, int(math.hypot(x1 - x0, y1 - y0) / 0.05) + 1)
        t = np.linspace(0, 1, steps)
        px, py = x0 + (x1 - x0) * t - center[0], y0 + (y1 - y0) * t - center[1]
        r = np.hypot(px, py)
        deg = (np.degrees(np.arctan2(py, px)) % 360).astype(int) % 360
        if outer:
            np.maximum.at(edge, deg, r + width / 2)
        else:
            np.minimum.at(edge, deg, r - width / 2)
    return edge


@pytest.fixture
def _sliced(tmp_path, monkeypatch):
    """Slice a mesh centred on the bed through Kiln's own slicing step."""
    if _PRUSA is None:
        pytest.skip("PrusaSlicer not installed: public CI has no slicer")
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    profile = resolve_slicer_profile("bambu_a1")
    bed = re.search(r"^bed_shape\s*=\s*(.+)$", Path(profile).read_text(), re.M)
    corners = np.array([[float(v) for v in p.split("x")] for p in bed.group(1).split(",")])
    middle = (corners.min(axis=0) + corners.max(axis=0)) / 2

    def slice_(mesh, axis_xy):
        """G-code rows, and where the axis at *axis_xy* (mesh XY) landed."""
        lo, hi = mesh.bounds
        offset = np.array([middle[0] - (lo[0] + hi[0]) / 2, middle[1] - (lo[1] + hi[1]) / 2, -lo[2]])
        placed = mesh.copy()
        placed.apply_translation(offset)
        path = tmp_path / f"placed_{len(list(tmp_path.glob('placed_*')))}.stl"
        placed.export(str(path))
        out = slice_file(str(path), output_dir=str(tmp_path / "out"), profile=profile,
                         slicer_path=_PRUSA, extra_args=["--dont-arrange"], timeout=600)
        return _extrusions(out.output_path), np.asarray(axis_xy) + offset[:2]

    return slice_


def _mid_layers(rows, z0: float, z1: float) -> list[float]:
    tops = sorted({round(float(t), 3) for t in rows[:, 4]})
    return [t for t in tops if z0 <= t <= z1]


def _assert_round_the_circle(rows, center, layers, *, outer: bool, crest: float, pitch: float, label: str):
    """The crest is reached on every layer, and over each pitch of layers in
    every 10-degree sector."""
    edges = {t: _edges(rows, center, t, outer=outer) for t in layers}
    for t, edge in edges.items():
        seen = edge[np.isfinite(edge)]
        tip = seen.max() if outer else seen.min()
        assert tip == pytest.approx(crest, abs=0.1), f"{label}: layer {t} reaches {tip:.3f} mm, crest is {crest}"
    height = layers[1] - layers[0]
    per_pitch = math.ceil(pitch / height) + 1
    for i in range(len(layers) - per_pitch + 1):
        stack = np.stack([edges[t] for t in layers[i:i + per_pitch]])
        best = stack.max(axis=0) if outer else stack.min(axis=0)
        sectors = best.reshape(36, 10).max(axis=1) if outer else best.reshape(36, 10).min(axis=1)
        short = np.flatnonzero(sectors < crest - 0.15) if outer else np.flatnonzero(sectors > crest + 0.15)
        assert not len(short), (
            f"{label}: layers {layers[i]}..{layers[i + per_pitch - 1]} never reach the crest in "
            f"{len(short)} of 36 ten-degree sectors")


class TestWhatTheSlicerLaysDown:
    def test_bolt_reaches_the_crest_all_round(self, tmp_path, _sliced):
        """The incident's own part: an M10 x 1.5 thread on a 12 mm base."""
        mesh = _compile("union() { cylinder(d=12, h=4); translate([0, 0, 4]) "
                        "external_thread(diameter=10, length=16, pitch=1.5); }", tmp_path)
        rows, center = _sliced(mesh, (0, 0))
        layers = _mid_layers(rows, 7, 17)
        _assert_round_the_circle(rows, center, layers, outer=True, crest=5.0, pitch=1.5, label="bolt")
        for t in layers:
            edge = _edges(rows, center, t, outer=True)
            beyond = int(np.sum(edge > 4.7))
            assert beyond >= 120, f"bolt layer {t}: plastic past the pitch circle on only {beyond} of 360 degrees"

    def test_tapped_hole_reaches_its_root_all_round(self, tmp_path, _sliced):
        mesh = _compile("difference() { cylinder(d=20, h=10); translate([0, 0, -0.5]) "
                        "threaded_hole(diameter=10, pitch=1.5, depth=11); }", tmp_path)
        rows, center = _sliced(mesh, (0, 0))
        layers = _mid_layers(rows, 2, 8)
        # Inside a hole the plastic's nearest edge is what is printed: the
        # groove's root is the farthest point of that edge.
        edges = {t: _edges(rows, center, t, outer=False) for t in layers}
        for t, edge in edges.items():
            assert edge.min() == pytest.approx(4.7, abs=0.1), f"tapped hole layer {t}: bore edge {edge.min():.3f}"
            assert edge.max() == pytest.approx(5.3, abs=0.1), f"tapped hole layer {t}: groove reaches {edge.max():.3f}"
        height = layers[1] - layers[0]
        per_pitch = math.ceil(1.5 / height) + 1
        for i in range(len(layers) - per_pitch + 1):
            best = np.stack([edges[t] for t in layers[i:i + per_pitch]]).max(axis=0)
            assert (best.reshape(36, 10).max(axis=1) > 5.3 - 0.15).all(), f"groove misses sectors from layer {layers[i]}"

    def test_jar_and_lid_reach_their_crests_all_round(self, tmp_path, _sliced):
        mesh = _compile("screw_container();", tmp_path)
        jar_xy, lid_xy = (body.bounds[:, :2].mean(axis=0) for body in _jar_and_lid_as_made(mesh))
        rows, jar_center = _sliced(mesh, jar_xy)
        lid_center = jar_center + (lid_xy - jar_xy)
        neck = rows[np.hypot(rows[:, 0] - jar_center[0], rows[:, 1] - jar_center[1]) < 25]
        _assert_round_the_circle(neck, jar_center, _mid_layers(neck, 44, 48.5), outer=True,
                                 crest=21.2, pitch=3, label="jar neck")
        cap = rows[np.hypot(rows[:, 0] - lid_center[0], rows[:, 1] - lid_center[1]) < 25]
        _assert_round_the_circle(cap, lid_center, _mid_layers(cap, 3.5, 8), outer=False,
                                 crest=20.3, pitch=3, label="lid")


# ---------------------------------------------------------------------------
# Closed solids at every size: watertightness and component counts, kept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "bodies"), [
    ("external_thread(diameter=4, length=8, pitch=0.7);", 1),
    ("external_thread(diameter=80, length=12, pitch=3);", 1),
    ("external_thread(diameter=10, length=2, pitch=1.5);", 1),        # under one turn
    ("external_thread(diameter=10, length=12, pitch=3, starts=2);", 1),
    ("internal_thread(diameter=4, length=6, pitch=0.7, wall=2);", 1),
    ("internal_thread(diameter=80, length=10, pitch=3, wall=3);", 1),
    ("internal_thread(diameter=10, length=8, pitch=3, wall=3, starts=2);", 1),
    ("difference() { cylinder(d=8, h=6); translate([0, 0, -0.5]) threaded_hole(4, 0.7, 7); }", 1),
    ("bottle_thread(outer_diameter=10, height=6, pitch=2, wall=1.6);", 1),
    ("bottle_thread(outer_diameter=80, height=14, pitch=4, wall=2.5);", 1),
    ("screw_container(outer_d=20, height=20, wall=1.6, thread_pitch=2);", 2),
    ("screw_container(outer_d=80, height=40, wall=2.5, thread_pitch=4);", 2),
])
def test_every_size_is_closed(tmp_path, code, bodies):
    mesh = _compile(code, tmp_path)
    parts = mesh.split(only_watertight=False)
    assert len(parts) == bodies, f"{code}: {len(parts)} bodies"
    for part in parts:
        assert part.is_watertight and part.is_winding_consistent and part.volume > 0, code
