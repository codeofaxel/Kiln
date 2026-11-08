"""A thread's grooves stay clear of supports; a real overhang beside it is held.

A screw thread's flanks can read as overhang and turn supports on, filling
its grooves; four causes of that, each guarded here:

* only downward regions holding a flat face were span-checked, and a screw
  thread's flanks (60 degrees) are never flat;
* faces leaning up out of the plate band were counted as overhangs and kept
  out of every region, so a thread's start at the foot of a rod could never
  be cleared;
* the bridge shortcut cleared the whole part when its lowest ceiling was a
  bridge (the screw's hex socket roof), whatever hung elsewhere;
* once supports are on for anything, the slicer supports every slope it finds,
  a thread included, at any threshold.

So these tests judge the verdict on parts built here, then slice the real
parts and read the G-code: a threaded part alone gets no supports at all, and
a threaded part with a shelf gets them under the shelf and nowhere near the
thread.  Everything is judged on public Kiln's 45-degree floor, what a plain
install judges with.  The slicing half needs PrusaSlicer and OpenSCAD (public
CI has neither) and the bundled BOSL2.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh", reason="needs trimesh to build and measure the parts")
np = pytest.importorskip("numpy")

from kiln.printability import judge_supports, support_verdict  # noqa: E402

LIBRARIES = Path(__file__).resolve().parents[1] / "src" / "kiln" / "data" / "scad_libraries"


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


def _orca_family() -> dict[str, str]:
    """The installed slicers that take the Orca command line, by name."""
    found = {}
    for name, command, mac in (
        ("Bambu Studio", "bambu-studio", "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        ("OrcaSlicer", "orca-slicer", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
    ):
        exe = shutil.which(command) or (mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None)
        if exe:
            found[name] = exe
    return found


_OPENSCAD = _openscad()
_PRUSA = _prusaslicer()
_ORCA_FAMILY = _orca_family()
needs_openscad = pytest.mark.skipif(_OPENSCAD is None, reason="OpenSCAD not installed on this machine")
needs_slicer = pytest.mark.skipif(_PRUSA is None, reason="PrusaSlicer not installed: public CI has no slicer")


@pytest.fixture(autouse=True)
def _public_floor(monkeypatch):
    """Judge on public Kiln's own 45-degree floor, whatever is installed."""
    import kiln.design_intelligence as design_intelligence

    monkeypatch.setattr(design_intelligence, "load_pro_overlay_or_empty", lambda kind: {})


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------

#: A base plate with a wall at its far end whose top shelf runs 10 mm past
#: the wall: an overhang that needs supports, 22 mm or more from the thread.
_SHELF = """
module base_and_shelf() {
    translate([0, -8, 0]) cube([44, 16, 3]);
    translate([30, -8, 3]) cube([4, 16, 20]);
    translate([30, -8, 20]) cube([14, 16, 3]);
}
"""
#: Where the shelf overhangs, in the part's own frame: x, y and the height
#: of its underside.
_UNDER_SHELF = ((34.0, 44.0), (-8.0, 8.0), 20.0)

_BOSL2 = "include <BOSL2/std.scad>\ninclude <BOSL2/threading.scad>\ninclude <BOSL2/screws.scad>\n"

#: The component catalog's thread examples, and each with the shelf beside
#: it: (SCAD, the thread's axis, its height, its outer radius).  The M5
#: screw stands as the catalog orients it, head on the plate, socket open.
CATALOG = {
    "M8 rod": ("threaded_rod(d=8, pitch=1.25, l=20);", (0, 0), (-10, 10), 4.0),
    "M8 nut": ("threaded_nut(nutwidth=13, id=8, h=6, pitch=1.25, $slop=0.15);", (0, 0), (-3, 3), 4.31),
    "M5 screw": ('screw("M5,20", head="socket", drive="hex", orient=DOWN, anchor=TOP);', (0, 0), (5.2, 25), 2.45),
}
WITH_SHELF = {
    "M8 rod and a shelf": (
        "union() { base_and_shelf(); translate([8, 0, 3]) threaded_rod(d=8, pitch=1.25, l=20, anchor=BOTTOM); }",
        (8, 0), (3, 23), 4.0),
    "M5 screw and a shelf": (
        'union() { screw("M5,20", head="socket", drive="hex", orient=DOWN, anchor=TOP);'
        " difference() { translate([3, -8, 0]) cube([41, 16, 3]); cylinder(d=4.6, h=7, center=true, $fn=6); }"
        " translate([30, -8, 3]) cube([4, 16, 20]); translate([30, -8, 20]) cube([14, 16, 3]); }",
        (0, 0), (5.2, 25), 2.45),
}


def _build(code: str, tmp_path: Path, monkeypatch, name: str = "part") -> Path:
    """*code* compiled as Kiln compiles a design: the curve rule applied and
    the bundled libraries on OpenSCAD's path."""
    from kiln.curve_resolution import apply_rule
    from kiln.openscad_runner import run_openscad

    monkeypatch.setenv("OPENSCADPATH", str(LIBRARIES))
    scad, stl = tmp_path / f"{name}.scad", tmp_path / f"{name}.stl"
    scad.write_text(apply_rule(_BOSL2 + _SHELF + code), encoding="utf-8")
    result = run_openscad([_OPENSCAD, "-o", str(stl), str(scad)], timeout=600, cwd=str(tmp_path),
                          output_path=str(stl))
    assert result.returncode == 0 and stl.is_file(), (result.stderr or "")[-800:]
    return stl


def _revolved(profile, path: Path) -> Path:
    trimesh.creation.revolve(np.asarray(profile, dtype=float), sections=96).export(str(path))
    return path


def _flared_post(reach: float, path: Path) -> Path:
    """A 10 mm post whose top flares out *reach* mm at 60 degrees past
    vertical under a 2 mm cap: a slope hanging off a wall."""
    rise = reach / math.tan(math.radians(60.0))
    return _revolved([(0, 0), (5, 0), (5, 10), (5 + reach, 10 + rise), (5 + reach, 12 + rise), (0, 12 + rise)], path)


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class TestTheVerdict:
    def test_a_steep_lip_a_slicer_closes_needs_no_supports(self, tmp_path):
        """A 60-degree lip 1.5 mm deep closes from the wall it hangs off, as a
        thread's flank closes from its root.  It had no flat face, so it was
        never span-checked and called for supports."""
        judged = judge_supports(str(_flared_post(1.5, tmp_path / "lip.stl")))
        assert judged.overhangs.overhang_triangle_count > 0, "the lip is an overhang"
        assert support_verdict(judged) is None
        assert not judged.support_kept_off_faces, "nothing to keep supports off when there are none"

    def test_a_steep_shelf_past_reach_still_needs_them(self, tmp_path):
        """The same slope 12 mm deep hangs in the air: it is held."""
        judged = judge_supports(str(_flared_post(12.0, tmp_path / "shelf.stl")))
        assert support_verdict(judged), "a 12 mm slope off a wall needs supports"

    def test_the_bridge_shortcut_clears_ceilings_never_slopes(self, tmp_path):
        """An 8 mm bridge a slicer spans, carrying a 60-degree flare 3 mm wide
        above it.  The shortcut that calls the bridge spannable cleared the
        whole part, the flare with it."""
        left = trimesh.creation.box((3, 12, 15))
        left.apply_translation((-5.5, 0, 7.5))
        right = trimesh.creation.box((3, 12, 15))
        right.apply_translation((5.5, 0, 7.5))
        deck = trimesh.creation.box((14, 12, 3))
        deck.apply_translation((0, 0, 16.5))
        rise = 3 / math.tan(math.radians(60.0))
        flare = trimesh.creation.revolve(np.array(
            [(0, 17.9), (1.5, 17.9), (1.5, 20), (4.5, 20 + rise), (4.5, 22 + rise), (0, 22 + rise)]), sections=96)
        pytest.importorskip("manifold3d", reason="trimesh needs manifold3d to join the solids")
        path = tmp_path / "bridge_and_flare.stl"
        trimesh.boolean.union([left, right, deck, flare], engine="manifold").export(str(path))
        judged = judge_supports(str(path))
        assert judged.supports.likely_substituted_by_bridge, "the deck is still a bridge the slicer spans"
        assert support_verdict(judged), "the flare above the bridge still needs supports"

    @needs_openscad
    @pytest.mark.parametrize("name", list(CATALOG))
    def test_a_thread_alone_needs_no_supports(self, tmp_path, monkeypatch, name):
        code, *_ = CATALOG[name]
        judged = judge_supports(str(_build(code, tmp_path, monkeypatch)))
        assert judged.overhangs.overhang_triangle_count > 0, f"{name}: its flanks are overhangs"
        assert support_verdict(judged) is None, f"{name}: {support_verdict(judged)}"

    @needs_openscad
    @pytest.mark.parametrize("name", list(WITH_SHELF))
    def test_a_shelf_beside_a_thread_is_held_and_the_thread_kept_off(self, tmp_path, monkeypatch, name):
        code, axis, (z0, z1), outer = WITH_SHELF[name]
        stl = _build(code, tmp_path, monkeypatch)
        judged = judge_supports(str(stl))
        assert support_verdict(judged) == "overhangs=2 face(s), sticks out 10.0mm", (
            f"{name}: the shelf's two faces need supports, the thread's none: {support_verdict(judged)}")
        # The faces as the verdict indexed them.
        from kiln.printability import _load_mesh_for_analysis

        tris = np.asarray(_load_mesh_for_analysis(str(stl))[0], dtype=float)
        centres = tris.mean(axis=1)
        normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        unit_nz = normals[:, 2] / np.maximum(np.linalg.norm(normals, axis=1), 1e-12)
        downward = unit_nz < -math.sin(math.radians(45.0)) + 1e-6
        on_thread = (np.hypot(centres[:, 0] - axis[0], centres[:, 1] - axis[1]) <= outer + 0.2) & (
            centres[:, 2] > z0 + 0.5) & (centres[:, 2] < z1)
        kept = np.zeros(len(tris), dtype=bool)
        kept[judged.support_kept_off_faces] = True
        flanks = np.flatnonzero(downward & on_thread)
        assert len(flanks) > 100, f"{name}: the thread's flanks were not found"
        assert kept[flanks].all(), f"{name}: {int((~kept[flanks]).sum())} flank faces left to the slicer"
        (x0, x1), (y0, y1), under = _UNDER_SHELF
        shelf = np.flatnonzero((centres[:, 0] > x0) & (centres[:, 0] < x1) & (centres[:, 1] > y0)
                               & (centres[:, 1] < y1) & np.isclose(centres[:, 2], under) & downward)
        assert len(shelf) and not kept[shelf].any(), f"{name}: the shelf's underside was kept off supports"


# ---------------------------------------------------------------------------
# The file the slicer is handed
# ---------------------------------------------------------------------------


def test_the_kept_off_faces_go_to_the_slicer_painted(tmp_path):
    """The faces kept off supports are painted as blockers in both slicer
    families' spellings, on the part's own facets, placed where the slicer
    would have arranged the mesh."""
    from kiln.slicer_supports import SliceSupports, kept_off_input

    stl = _flared_post(1.5, tmp_path / "lip.stl")
    mesh = trimesh.load(str(stl), process=False)
    decision = SliceSupports(enabled=True, decided_by="analysis", reason="test", kept_off=(0, 1, 2))
    path, painted = kept_off_input(str(stl), decision, str(tmp_path), bed_centre=(128.0, 128.0))
    assert painted == 3
    model = zipfile.ZipFile(path).read("3D/3dmodel.model").decode()
    rows = re.findall(r"<triangle [^>]*/>", model)
    assert len(rows) == len(mesh.faces)
    assert sum('slic3rpe:custom_supports="8"' in r and 'paint_supports="8"' in r for r in rows) == 3
    lo, hi = mesh.bounds
    move = re.search(r'transform="1 0 0 0 1 0 0 0 1 (\S+) (\S+) (\S+)"', model).groups()
    assert float(move[0]) + (lo[0] + hi[0]) / 2 == pytest.approx(128.0)
    assert float(move[1]) + (lo[1] + hi[1]) / 2 == pytest.approx(128.0)
    assert float(move[2]) + lo[2] == pytest.approx(0.0, abs=1e-6)
    unpainted = SliceSupports(enabled=True, decided_by="analysis", reason="test")
    assert kept_off_input(str(stl), unpainted, str(tmp_path), bed_centre=(128.0, 128.0)) is None


# ---------------------------------------------------------------------------
# What the slicer lays down
# ---------------------------------------------------------------------------

_SUPPORT_TYPES = {"Support material", "Support material interface"}


def _support_points(gcode: str) -> np.ndarray:
    """Every support extrusion, sampled every 0.1 mm: rows of (x, y, z)."""
    pts, x, y, z, kind = [], 0.0, 0.0, 0.0, ""
    number = re.compile(r"([XYE])(-?\d*\.?\d+)")
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";Z:"):
                z = float(line[3:])
            elif line.startswith(";TYPE:"):
                kind = line[6:].strip()
            elif line.startswith("G1"):
                words = dict(number.findall(line.split(";")[0]))
                nx, ny = float(words.get("X", x)), float(words.get("Y", y))
                if kind in _SUPPORT_TYPES and float(words.get("E", 0)) > 0 and (nx, ny) != (x, y):
                    n = max(2, int(math.hypot(nx - x, ny - y) / 0.1) + 1)
                    t = np.linspace(0.0, 1.0, n)
                    pts.append(np.stack([x + (nx - x) * t, y + (ny - y) * t, np.full(n, z)], axis=1))
                x, y = nx, ny
    return np.concatenate(pts) if pts else np.zeros((0, 3))


@pytest.fixture
def _slice(tmp_path, monkeypatch):
    """Slice a part through Kiln's own slicing step, as every door does."""
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    profile = resolve_slicer_profile("bambu_a1")
    shape = re.search(r"^bed_shape\s*=\s*(.+)$", Path(profile).read_text(), re.M).group(1)
    corners = np.array([[float(v) for v in p.split("x")] for p in shape.split(",")])
    centre = (corners.min(axis=0) + corners.max(axis=0)) / 2

    def slice_(stl: Path, slicer_path: str | None = _PRUSA):
        """The slice result, and the shift from the part's frame to the bed's."""
        lo, hi = trimesh.load(str(stl)).bounds
        shift = np.array([centre[0] - (lo[0] + hi[0]) / 2, centre[1] - (lo[1] + hi[1]) / 2, -lo[2]])
        result = slice_file(str(stl), output_dir=str(tmp_path / "out"), profile=profile,
                            slicer_path=slicer_path, timeout=900)
        return result, shift

    return slice_


def _near_thread(points: np.ndarray, shift, axis, height, outer) -> int:
    """Support samples within a millimetre of the thread's crest, over its height."""
    if not len(points):
        return 0
    ax = np.asarray(axis, dtype=float) + shift[:2]
    r = np.hypot(points[:, 0] - ax[0], points[:, 1] - ax[1])
    z0, z1 = height[0] + shift[2], height[1] + shift[2]
    return int(((r <= outer + 1.0) & (points[:, 2] >= z0) & (points[:, 2] <= z1)).sum())


@needs_slicer
@needs_openscad
class TestWhatTheSlicerLaysDown:
    @pytest.mark.parametrize("name", list(CATALOG))
    def test_a_thread_alone_is_sliced_without_supports(self, tmp_path, monkeypatch, _slice, name):
        code, *_ = CATALOG[name]
        result, _shift = _slice(_build(code, tmp_path, monkeypatch))
        assert not result.supports.enabled, f"{name}: {result.supports.note}"
        assert len(_support_points(result.output_path)) == 0, f"{name}: support in the file"

    @pytest.mark.parametrize("name", list(WITH_SHELF))
    def test_the_shelf_is_held_and_the_thread_is_left_alone(self, tmp_path, monkeypatch, _slice, name):
        code, axis, height, outer = WITH_SHELF[name]
        result, shift = _slice(_build(code, tmp_path, monkeypatch))
        points = _support_points(result.output_path)
        near = _near_thread(points, shift, axis, height, outer)
        assert near == 0, f"{name}: {near} support samples on or in the thread"
        (x0, x1), (y0, y1), under = _UNDER_SHELF
        held = ((points[:, 0] > x0 + shift[0]) & (points[:, 0] < x1 + shift[0]) & (points[:, 1] > y0 + shift[1])
                & (points[:, 1] < y1 + shift[1]) & (points[:, 2] < under + shift[2]))
        assert held.sum() > 1000, f"{name}: almost no support under the shelf ({int(held.sum())} samples)"
        assert result.supports.enabled and result.supports.kept_off_painted > 0, result.supports.note
        assert "parts that print on their own" in result.supports.note


# ---------------------------------------------------------------------------
# The answer matches the file, whichever slicer wrote it
# ---------------------------------------------------------------------------

#: A 10 mm post under a 40 mm cap, by the height of the cap's underside:
#: tall enough for a support with a body, and so low the slicer writes the
#: whole support as interface layers.
_CAPS = {"a support with a body": 10.0, "a support that is all interface": 0.8}


@pytest.mark.skipif(not _ORCA_FAMILY, reason="neither Bambu Studio nor OrcaSlicer is installed")
@pytest.mark.parametrize("slicer", list(_ORCA_FAMILY) or ["none installed"])
@pytest.mark.parametrize("cap", list(_CAPS))
def test_supports_the_slicer_placed_are_never_reported_as_none(tmp_path, _slice, slicer, cap):
    """A Bambu Studio slice with supports, and an all-interface support
    from OrcaSlicer, must not come back "the part needs supports but the
    slicer placed none" with the support in the file."""
    under = _CAPS[cap]
    part = _revolved([(0, 0), (5, 0), (5, under), (20, under), (20, under + 3), (0, under + 3)], tmp_path / "cap.stl")
    result, _shift = _slice(part, _ORCA_FAMILY[slicer])
    labels = set(re.findall(r"^;\s*(?:TYPE|FEATURE)\s*:\s*(Support.*?)\s*$",
                            Path(result.output_path).read_text(errors="replace"), re.M))
    assert labels, f"{slicer}: the slice carries no support to read"
    assert result.supports.enabled, f"{slicer} wrote {sorted(labels)}; Kiln answered: {result.supports.note}"
    assert "placed none" not in result.supports.note


# ---------------------------------------------------------------------------
# Kiln's own library threads (45-degree flanks)
# ---------------------------------------------------------------------------


def _library_cuts_real_teeth() -> bool:
    from kiln.generation.scad_library import get_library_source

    return "function _kiln_thread_tooth(" in get_library_source()


#: Library parts the generator makes: (call, thread axis, thread height, outer radius).
LIBRARY = {
    "bolt": ("union() { cylinder(d=12, h=4); translate([0, 0, 4]) external_thread(diameter=10, length=16, pitch=1.5); }",
             (0, 0), (4, 20), 5.0),
    "nut": ("internal_thread(diameter=10, length=12, pitch=1.5, wall=3);", (0, 0), (0, 12), 5.3),
    "tapped hole": ("difference() { cylinder(d=20, h=10); translate([0, 0, -0.5]) "
                    "threaded_hole(diameter=10, pitch=1.5, depth=11); }", (0, 0), (0, 10), 5.3),
    "jar and lid": ("screw_container();", (0, 0), (42, 50), 21.2),
    "bolt and a shelf": ("union() { base_and_shelf(); translate([8, 0, 3]) "
                         "external_thread(diameter=10, length=16, pitch=1.5); }", (8, 0), (3, 19), 5.0),
}


@needs_slicer
@needs_openscad
@pytest.mark.parametrize("name", list(LIBRARY))
def test_library_threads_are_left_alone(tmp_path, monkeypatch, _slice, name):
    """Kiln's library threads lean exactly 45 degrees, the public floor's own
    limit: a slicer handed that angle supports them (measured: 236 support
    moves on the bolt beside a shelf), so beside a shelf they go over painted."""
    if not _library_cuts_real_teeth():
        pytest.skip("this tree's SCAD library still draws the old ribbon threads")
    from kiln.curve_resolution import apply_rule
    from kiln.generation.scad_library import get_library_source
    from kiln.openscad_runner import run_openscad

    call, axis, height, outer = LIBRARY[name]
    scad, stl = tmp_path / "part.scad", tmp_path / "part.stl"
    scad.write_text(apply_rule(get_library_source() + "\n\n// === USER-GENERATED CODE ===\n\n" + _SHELF + call),
                    encoding="utf-8")
    built = run_openscad([_OPENSCAD, "-o", str(stl), str(scad)], timeout=600, cwd=str(tmp_path),
                         output_path=str(stl))
    assert built.returncode == 0, (built.stderr or "")[-800:]
    result, shift = _slice(stl)
    points = _support_points(result.output_path)
    assert _near_thread(points, shift, axis, height, outer) == 0, f"{name}: support on or in the thread"
    if name.endswith("shelf"):
        assert result.supports.enabled and result.supports.kept_off_painted > 0, result.supports.note
    else:
        assert not result.supports.enabled, f"{name}: {result.supports.note}"
