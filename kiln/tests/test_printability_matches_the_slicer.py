"""Kiln says "won't print" for what the slicer drops, and for nothing else.

Found 2026-11-05.  The thin-wall check stopped every wall thinner than the
nozzle, saying a slicer drops it, while the slicer Kiln drives laid such walls
down as one thin line.  No test had ever asked a slicer.  This file does: it
builds shapes well clear of the slicers' lines on either side, slices each
with every slicer Kiln can drive that is installed, through Kiln's own
:func:`kiln.slicer.slice_file` and profile (so the wall mode is the one Kiln
states, :data:`kiln.slicer_profiles.WALL_MODE`), reads back what the G-code
lays down, and holds Kiln's verdict to it.  A feature is dropped when the
slice leaves it out, or loses at least a fifth of the layers that cross it;
Kiln stops the print exactly then.  A warning is never a stop.

Two cracks pin the slicer's own crack closing
(:data:`kiln.slicer_profiles.SLICE_CLOSING_RADIUS_MM`): a crack narrower than
twice the radius is filled in the slice, a wider one stays open.

Public CI has no slicer, so here it skips and says why; the pre-push
slicer-pace gate runs it with the slicers on the machine.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln.printability import analyze_printability, stops  # noqa: E402
from kiln.slicer import slice_file  # noqa: E402
from kiln.slicer_orca import ini_to_settings  # noqa: E402
from kiln.slicer_profiles import (  # noqa: E402
    SLICE_CLOSING_RADIUS_MM,
    get_slicer_profile,
    resolve_slicer_profile,
)

NOZZLE = 0.4
#: A feature that loses this share of its layers in the slice is dropped.
LOST_LAYERS_DROP = 0.2


def _find(names: tuple[str, ...], mac: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


#: Every slicer Kiln can drive, by its own name, where this machine has it.
SLICERS: dict[str, str] = {
    name: path
    for name, path in (
        ("prusaslicer", _find(("prusa-slicer", "PrusaSlicer", "prusaslicer"),
                              "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")),
        ("orcaslicer", _find(("orca-slicer", "OrcaSlicer", "orcaslicer"),
                             "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")),
        ("bambustudio", _find(("bambu-studio", "BambuStudio", "bambustudio"),
                              "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")),
    )
    if path
}

pytestmark = pytest.mark.skipif(
    not SLICERS, reason="no slicer installed: this file judges Kiln against a real slice and has none to read"
)


# ---------------------------------------------------------------------------
# The shapes: each on its own plate, each where both slicers agree
# ---------------------------------------------------------------------------


def _box(lo: tuple[float, ...], hi: tuple[float, ...]):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _union(*parts):
    mesh = trimesh.boolean.union(list(parts), engine="manifold")
    assert mesh.is_volume, "fixture must be one closed solid"
    return mesh


def _cut(body, hole):
    mesh = trimesh.boolean.difference([body, hole], engine="manifold")
    assert mesh.is_volume, "fixture must be one closed solid"
    return mesh


@dataclass(frozen=True)
class Shape:
    """A test part, and where its feature stands: an XY box in the part's own
    coordinates and the heights it spans.  ``skin`` features are read by how
    much of their centre line lies under plastic, the rest by any plastic in
    their box."""

    name: str
    mesh: object
    x: tuple[float, float]
    y: tuple[float, float]
    z: tuple[float, float]
    skin: bool = False


def _plate():
    return _box((0, 0, 0), (12, 14, 1))


def _wall(thickness: float) -> Shape:
    wall = _box((6 - thickness / 2, 2, 0.5), (6 + thickness / 2, 12, 7))
    return Shape(f"wall {thickness:.2f} mm", _union(_plate(), wall), (5.4, 6.6), (3, 11), (1.6, 6.4))


def _pin(diameter: float) -> Shape:
    pin = trimesh.creation.cylinder(radius=diameter / 2, height=6.5, sections=48)
    pin.apply_translation((6, 7, 0.5 + 3.25))
    reach = diameter / 2 + 0.6
    return Shape(f"pin {diameter:.2f} mm across", _union(_plate(), pin), (6 - reach, 6 + reach),
                 (7 - reach, 7 + reach), (1.6, 6.4))


def _fin(width: float, length: float) -> Shape:
    fin = _box((6 - width / 2, 7 - length / 2, 0.5), (6 + width / 2, 7 + length / 2, 7))
    return Shape(f"fin {width:.2f} x {length:.2f} mm", _union(_plate(), fin), (5.4, 6.6),
                 (7 - length / 2 - 0.4, 7 + length / 2 + 0.4), (1.6, 6.4))


def _sheet(thickness: float) -> Shape:
    """A flat sheet bridging a 3 mm gap between two blocks, centred on 3.5 mm."""
    lo, hi = 3.5 - thickness / 2, 3.5 + thickness / 2
    mesh = _union(
        _box((0, 0, 0), (20, 14, 1)), _box((3, 4.5, 0.5), (8.5, 9.5, 6)), _box((11.5, 4.5, 0.5), (17, 9.5, 6)),
        _box((8.4, 4.5, lo), (11.6, 9.5, hi)),
    )
    return Shape(f"sheet {thickness:.2f} mm thick", mesh, (8.9, 11.1), (5.0, 9.0), (lo, hi))


SKIN = 0.1  # the outer skin beside each crack: alone, every slicer drops it


def _crack(width: float, *, through_the_top: bool = True) -> tuple[Shape, Shape]:
    """A block with a vertical crack SKIN under one face.  Through the top it
    stays one piece of mesh; sealed inside, the crack is a piece of its own.
    Two readings of one part: the crack's footprint, and the skin's."""
    top = 8.5 if through_the_top else 6.5
    mesh = _cut(_box((0, 0, 0), (12, 12, 8)), _box((SKIN, 3, 1.5), (SKIN + width, 9, top)))
    where = "" if through_the_top else " inside the part"
    crack = Shape(f"crack {width:.2f} mm{where}", mesh, (SKIN, SKIN + width), (4, 8), (2.0, 6.0), skin=True)
    skin = Shape(f"skin beside a {width:.2f} mm crack{where}", mesh, (0.0, SKIN), (4, 8), (2.0, 6.0), skin=True)
    return crack, skin


# At Kiln's own profile and a 0.4 mm nozzle every slicer Kiln drives agrees
# on each of these, a step or more from its line: a wall a quarter of the
# nozzle thick is dropped and one three quarters thick printed; a round pin
# half the nozzle across is dropped and one 2.5 nozzles across printed (the
# band between is erratic); a fin 0.75 nozzle wide is dropped at a quarter of
# a nozzle long and printed at three (from half a nozzle long to three
# quarters the slicers do not agree with each other); a sheet thicker than a
# layer always meets a slice.
DROPPED_WALL = _wall(0.10)
PRINTED_WALL = _wall(0.30)
DROPPED_PIN = _pin(0.20)
PRINTED_PIN = _pin(1.00)
DROPPED_FIN = _fin(0.30, 0.10)
PRINTED_FIN = _fin(0.30, 1.20)
PRINTED_SHEET = _sheet(0.30)
# Crack closing fills a crack narrower than twice the radius, and the skin
# beside a filled crack prints as part of the wall.
SEALED_CRACK, SEALED_SKIN = _crack(1.2 * SLICE_CLOSING_RADIUS_MM)
OPEN_CRACK, OPEN_SKIN = _crack(4.0 * SLICE_CLOSING_RADIUS_MM)
_, INNER_OPEN_SKIN = _crack(4.0 * SLICE_CLOSING_RADIUS_MM, through_the_top=False)

AGREEMENT = (
    DROPPED_WALL, PRINTED_WALL, DROPPED_PIN, PRINTED_PIN, DROPPED_FIN, PRINTED_FIN, PRINTED_SHEET,
    SEALED_SKIN, OPEN_SKIN,
)


# ---------------------------------------------------------------------------
# Reading the G-code back
# ---------------------------------------------------------------------------

_NOT_THE_PART = ("custom", "skirt", "brim", "support", "wipe", "prime")
_MOVE = re.compile(r"^G[123]\b")
_WORD = {axis: re.compile(rf"{axis}(-?[\d.]+)") for axis in "XYE"}


@dataclass(frozen=True)
class Extrusion:
    x0: float
    y0: float
    x1: float
    y1: float
    z: float  # the layer's top
    width: float


def _read_part_extrusions(gcode: str) -> list[Extrusion]:
    """Every move that lays plastic on the part: support, skirts, brims, the
    purge line and the prime tower left out.  Reads both tag spellings
    (``;TYPE:`` / ``; FEATURE:``, ``;WIDTH:`` / ``; LINE_WIDTH:``,
    ``;Z:`` / ``; Z_HEIGHT:``)."""
    out: list[Extrusion] = []
    x = y = z = width = 0.0
    feature = ""
    relative = True
    last_e = 0.0
    for line in gcode.splitlines():
        if line.startswith((";Z:", "; Z_HEIGHT:")):
            z = float(line.split(":", 1)[1])
            continue
        if line.startswith((";WIDTH:", "; LINE_WIDTH:")):
            width = float(line.split(":", 1)[1])
            continue
        if line.startswith((";TYPE:", "; FEATURE:")):
            feature = line.split(":", 1)[1].strip().lower()
            continue
        if line.startswith("M83"):
            relative = True
            continue
        if line.startswith("M82"):
            relative = False
            continue
        if line.startswith("G92"):
            found = _WORD["E"].search(line)
            last_e = float(found.group(1)) if found else last_e
            continue
        if not _MOVE.match(line):
            continue
        code = line.split(";", 1)[0]
        words = {axis: (float(m.group(1)) if (m := rx.search(code)) else None) for axis, rx in _WORD.items()}
        nx = words["X"] if words["X"] is not None else x
        ny = words["Y"] if words["Y"] is not None else y
        if words["E"] is not None:
            pushed = words["E"] if relative else words["E"] - last_e
            if not relative:
                last_e = words["E"]
            if pushed > 0 and (nx, ny) != (x, y) and not any(k in feature for k in _NOT_THE_PART):
                out.append(Extrusion(x, y, nx, ny, round(z, 3), width))
        x, y = nx, ny
    return out


def _distance(e: Extrusion, points: np.ndarray) -> np.ndarray:
    a, b = np.array([e.x0, e.y0]), np.array([e.x1, e.y1])
    ab = b - a
    t = np.clip(((points - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(a + np.outer(t, ab) - points, axis=1)


@dataclass(frozen=True)
class Layers:
    """Where the profile's layers are cut: the first layer's middle, then
    every layer's middle above it."""

    first: float
    height: float

    def cut_of(self, top: float) -> float:
        return self.first / 2 if top <= self.first + 1e-6 else top - self.height / 2

    def cuts_within(self, lo: float, hi: float) -> list[float]:
        cuts, cut = [], self.first / 2
        while cut <= hi + 1e-9:
            if cut >= lo - 1e-9:
                cuts.append(round(cut, 4))
            cut = (self.first + self.height / 2) if cut == self.first / 2 else cut + self.height
        return cuts


@dataclass(frozen=True)
class Sliced:
    shape: Shape
    extrusions: list[Extrusion]
    offset: np.ndarray  # part coordinates -> bed coordinates, read off the G-code
    layers: Layers

    def _on(self, cut: float) -> list[Extrusion]:
        return [e for e in self.extrusions if abs(self.layers.cut_of(e.z) - cut) < 0.02]

    def covered_share(self, cut: float) -> float:
        """How much of the feature's centre line, at one cut, lies under
        laid plastic (each line taken at its own width)."""
        cx = (self.shape.x[0] + self.shape.x[1]) / 2
        line = np.array([[cx, yy] for yy in np.linspace(*self.shape.y, 21)]) + self.offset
        covered = np.zeros(len(line), dtype=bool)
        for e in self._on(cut):
            covered |= _distance(e, line) <= e.width / 2 + 0.005
        return float(covered.mean())

    def present(self, cut: float) -> bool:
        if self.shape.skin:
            return self.covered_share(cut) >= 0.5
        sx, sy = self.shape.x, self.shape.y
        for e in self._on(cut):
            pts = np.linspace((e.x0, e.y0), (e.x1, e.y1), 25) - self.offset
            if ((pts[:, 0] >= sx[0]) & (pts[:, 0] <= sx[1]) & (pts[:, 1] >= sy[0]) & (pts[:, 1] <= sy[1])).any():
                return True
        return False

    def cuts(self) -> list[float]:
        cuts = self.layers.cuts_within(*self.shape.z)
        assert cuts, f"{self.shape.name}: no layer is cut inside it; the fixture is too thin for the profile"
        return cuts

    def lost_share(self) -> float:
        cuts = self.cuts()
        return sum(not self.present(c) for c in cuts) / len(cuts)

    def dropped(self) -> bool:
        return self.lost_share() >= LOST_LAYERS_DROP


def _profile_id() -> str:
    """Kiln's own Bambu A1 profile where its record is on this machine (the
    pre-push gate's run), else the generic one; both slice a 0.4 mm nozzle."""
    return "bambu_a1" if get_slicer_profile("bambu_a1").id == "bambu_a1" else "default"


@pytest.fixture(scope="module")
def work(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("matches_the_slicer")


def _part_file(shape: Shape, work: Path) -> str:
    path = work / f"part_{id(shape.mesh)}.stl"
    if not path.exists():
        shape.mesh.export(path)
    return str(path)


@pytest.fixture(scope="module")
def sliced(work: Path):
    """Each part sliced once per slicer and read back, keyed (slicer, shape)."""
    profile = resolve_slicer_profile(_profile_id())
    settings = ini_to_settings(profile)
    assert float(settings["nozzle_diameter"].split(",")[0]) == NOZZLE, "the shapes are sized for a 0.4 mm nozzle"
    layers = Layers(float(settings.get("first_layer_height", settings["layer_height"])),
                    float(settings["layer_height"]))
    cache: dict[tuple[str, int], tuple[list[Extrusion], np.ndarray]] = {}

    def get(slicer: str, shape: Shape) -> Sliced:
        key = (slicer, id(shape.mesh))
        if key not in cache:
            out_dir = work / f"{slicer}_{id(shape.mesh)}"
            result = slice_file(_part_file(shape, work), output_dir=str(out_dir), profile=profile,
                                slicer_path=SLICERS[slicer], timeout=600)
            extrusions = _read_part_extrusions(Path(result.output_path).read_text(errors="replace"))
            assert [e for e in extrusions if e.z <= 1.0], (
                f"{slicer}: no plastic read back on the plate; the G-code reader does not fit this slicer"
            )
            pts = np.array([[e.x0, e.y0] for e in extrusions] + [[e.x1, e.y1] for e in extrusions])
            lo, hi = shape.mesh.bounds
            cache[key] = (extrusions, (pts.min(0) + pts.max(0)) / 2 - (lo[:2] + hi[:2]) / 2)
        extrusions, offset = cache[key]
        return Sliced(shape, extrusions, offset, layers)

    return get


@pytest.fixture(scope="module")
def kiln_stops(work: Path):
    """Kiln's verdict on each part, read once: the stops its print gate takes."""
    cache: dict[int, list[str]] = {}

    def get(shape: Shape) -> list[str]:
        if id(shape.mesh) not in cache:
            cache[id(shape.mesh)] = stops(analyze_printability(_part_file(shape, work), nozzle_diameter=NOZZLE))
        return cache[id(shape.mesh)]

    return get


# ---------------------------------------------------------------------------
# The verdict and the slice agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slicer", sorted(SLICERS))
@pytest.mark.parametrize("shape", AGREEMENT, ids=lambda s: s.name)
def test_kiln_stops_exactly_what_the_slice_drops(slicer: str, shape: Shape, sliced, kiln_stops) -> None:
    lost = sliced(slicer, shape).lost_share()
    dropped = lost >= LOST_LAYERS_DROP
    said = kiln_stops(shape)
    assert bool(said) == dropped, (
        f"{shape.name}: the {slicer} slice {'drops' if dropped else 'prints'} it (loses {lost:.0%} of its "
        f"layers), and Kiln {'stops the print: ' + said[0] if said else 'lets it through'}"
    )


@pytest.mark.parametrize("slicer", sorted(SLICERS))
def test_a_crack_inside_the_closing_radius_is_filled(slicer: str, sliced) -> None:
    """The premise of the sealed case: the slice lays plastic over the crack."""
    part = sliced(slicer, SEALED_CRACK)
    shares = [part.covered_share(c) for c in part.cuts()]
    assert min(shares) > 0.9, f"{slicer} left part of a {SEALED_CRACK.name} open: {shares}"


@pytest.mark.parametrize("slicer", sorted(SLICERS))
def test_a_crack_past_the_closing_radius_stays_open(slicer: str, sliced) -> None:
    """And the other side of it: a wider crack is not filled."""
    part = sliced(slicer, OPEN_CRACK)
    shares = [part.covered_share(c) for c in part.cuts()]
    assert max(shares) < 0.1, f"{slicer} filled part of a {OPEN_CRACK.name}: {shares}"


@pytest.mark.parametrize("slicer", sorted(SLICERS))
def test_the_crack_sealed_inside_the_part_slices_like_the_open_one(slicer: str, sliced) -> None:
    """The slicer cannot tell the two apart: cut layer by layer, a crack
    sealed inside the part leaves the skin beside it alone just the same."""
    assert sliced(slicer, INNER_OPEN_SKIN).dropped(), f"{slicer} printed the {INNER_OPEN_SKIN.name}"


def test_a_skin_beside_a_crack_inside_the_part_stops_the_print(kiln_stops) -> None:
    """The crack's faces are a piece of the mesh of their own; the thin-wall
    check measures them with the body around them, so it sees the skin."""
    assert kiln_stops(INNER_OPEN_SKIN), f"Kiln lets the {INNER_OPEN_SKIN.name} through; every slice drops it"
