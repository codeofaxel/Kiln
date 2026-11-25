"""A brim never joins two pieces of a part printed in place apart unsaid.

A brim is drawn round every piece the first layer prints.  Where two pieces
of a part printed in place stand closer at the bed than two brims are wide,
the brims meet in the gap between them and the pieces print joined there.
The sliced file's first layer is read for it: a profile Kiln wrote gets the
widest brim that keeps out of the gap, or none where none fits, and the
answer says so; a person's own profile is left as it is and the answer says
what to set.

The slicing half asks for an 8 mm brim through the ``reslice_with_overrides``
door, as an agent following the advice to add a brim does, with every slicer
installed, and reads the first layer back.  It skips without a slicer:
public CI has none.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

trimesh = pytest.importorskip("trimesh")
np = pytest.importorskip("numpy")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

#: The pin's outline turned about its axis, and the bore it hangs in, 0.3 mm
#: from it all round: the joint of a print-in-place hinge.
_PIN = [(0, 0), (3.1, 0), (3.6, 0.5), (2.4, 2.5), (3.6, 5), (3.6, 5.6), (5.5, 7.5), (5.5, 10.5), (0, 10.5)]
_BORE = [(0, -1), (4.4, -1), (4.4, 0), (3.9, 0.5), (2.7, 2.5), (3.9, 5), (4.9, 6), (0, 6)]
_EYE_RADIUS = 7.0


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _turned(profile, at):
    mesh = trimesh.creation.revolve(np.array(profile, dtype=float), sections=72)
    mesh.apply_translation((at[0], at[1], 0.0))
    return mesh


def _hinged_pair(path: Path, half_span: float) -> tuple[Path, list[tuple[float, float]]]:
    """A bar with a pin at each end, each hanging in its own eye, printed in
    place: three pieces, the two eyes ``2 * half_span - 14`` mm apart at the
    bed.  Returns the file and the joints' axes."""
    axes = [(-half_span, 0.0), (half_span, 0.0)]
    eyes = []
    for axis in axes:
        eye = trimesh.creation.cylinder(radius=_EYE_RADIUS, height=5.0, sections=72)
        eye.apply_translation((axis[0], axis[1], 2.5))
        eyes.append(eye.difference(_turned(_BORE, axis)))
    bar = trimesh.creation.box(extents=(2 * half_span, 11.0, 3.0))
    bar.apply_translation((0.0, 0.0, 9.0))
    for axis in axes:
        bar = bar.union(_turned(_PIN, axis))
    trimesh.util.concatenate([*eyes, bar]).export(path)
    return path, axes


def _two_blocks(path: Path, gap: float) -> Path:
    """Two blocks standing *gap* mm apart, side by side, as one file."""
    a = trimesh.creation.box(extents=(20.0, 20.0, 12.0))
    a.apply_translation((-10.0 - gap / 2, 0.0, 6.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 12.0))
    b.apply_translation((10.0 + gap / 2, 0.0, 6.0))
    trimesh.util.concatenate([a, b]).export(path)
    return path


def _first_layer_file(box, brim: list[tuple[float, float, float, float]], *, z: float = 0.2) -> str:
    """A sliced file: the part's outer wall round *box*, and the brim lines
    *brim* (x0, y0, x1, y1) on the first layer, labelled as Orca labels them."""
    x0, y0, x1, y1 = box
    lines = ["M83", f"G1 Z{z:.3f}", "; FEATURE: Outer wall", f"G1 X{x0:.3f} Y{y0:.3f}"]
    lines += [f"G1 X{x:.3f} Y{y:.3f} E1" for x, y in ((x1, y0), (x1, y1), (x0, y1), (x0, y0))]
    lines.append("; FEATURE: Brim")
    for bx0, by0, bx1, by1 in brim:
        lines += [f"G1 X{bx0:.3f} Y{by0:.3f}", f"G1 X{bx1:.3f} Y{by1:.3f} E0.5"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Reading the first layer
# ---------------------------------------------------------------------------


def test_a_brim_in_the_gap_between_two_pieces_joins_them(tmp_path):
    from kiln.slicer import brim_joins_pieces

    stl, _axes = _hinged_pair(tmp_path / "pair.stl", half_span=9.0)
    lo, hi = trimesh.load(str(stl), force="mesh").bounds
    box = (lo[0], lo[1], hi[0], hi[1])
    gcode = tmp_path / "joined.gcode"
    # A line across the 4 mm between the two eyes, at the bed.
    gcode.write_text(_first_layer_file(box, [(-2.0, 0.0, 2.0, 0.0)]), encoding="utf-8")
    seen = brim_joins_pieces(str(gcode), str(stl), 8.0)
    assert seen is not None and seen.joined, seen
    assert seen.gap_mm == pytest.approx(4.0, abs=0.2)
    assert 0.0 < seen.keep_mm < 2.0, seen


def test_a_brim_round_the_outside_alone_joins_nothing(tmp_path):
    from kiln.slicer import BrimJoin, brim_joins_pieces

    stl, _axes = _hinged_pair(tmp_path / "pair.stl", half_span=9.0)
    lo, hi = trimesh.load(str(stl), force="mesh").bounds
    box = (lo[0], lo[1], hi[0], hi[1])
    gcode = tmp_path / "outside.gcode"
    # A line hugging the left eye's far side: it touches that eye and no other piece.
    gcode.write_text(_first_layer_file(box, [(-16.25, -3.0, -16.25, 3.0)]), encoding="utf-8")
    assert brim_joins_pieces(str(gcode), str(stl), 8.0) == BrimJoin(joined=False)


def test_a_brim_round_a_pin_in_its_eye_joins_nothing(tmp_path):
    """The pin stands inside its eye's bore, where a brim drawn round the
    outside never reaches: the eye's wall is between them."""
    from kiln.slicer import BrimJoin, brim_joins_pieces

    stl, _axes = _hinged_pair(tmp_path / "pair.stl", half_span=9.0)
    lo, hi = trimesh.load(str(stl), force="mesh").bounds
    box = (lo[0], lo[1], hi[0], hi[1])
    ring = [(-9.0 + 7.25 * math.cos(t0), 7.25 * math.sin(t0), -9.0 + 7.25 * math.cos(t1), 7.25 * math.sin(t1))
            for t0, t1 in zip(np.linspace(0.5, 5.8, 40)[:-1], np.linspace(0.5, 5.8, 40)[1:], strict=True)]
    gcode = tmp_path / "ring.gcode"
    gcode.write_text(_first_layer_file(box, ring), encoding="utf-8")
    assert brim_joins_pieces(str(gcode), str(stl), 8.0) == BrimJoin(joined=False)


def test_one_piece_is_never_joined(tmp_path):
    from kiln.slicer import BrimJoin, brim_joins_pieces

    stl = tmp_path / "block.stl"
    trimesh.creation.box(extents=(20.0, 20.0, 12.0)).export(stl)
    gcode = tmp_path / "one.gcode"
    gcode.write_text(_first_layer_file((-10.0, -10.0, 10.0, 10.0), [(-12.0, 0.0, -11.0, 0.0)]), encoding="utf-8")
    assert brim_joins_pieces(str(gcode), str(stl), 8.0) == BrimJoin(joined=False)


def test_a_file_that_cannot_be_placed_is_not_read(tmp_path):
    from kiln.slicer import brim_joins_pieces

    stl, _axes = _hinged_pair(tmp_path / "pair.stl", half_span=9.0)
    gcode = tmp_path / "elsewhere.gcode"
    # The file's part is another size: it cannot be the same part, where it stands.
    gcode.write_text(_first_layer_file((0.0, 0.0, 80.0, 80.0), [(-2.0, 0.0, 2.0, 0.0)]), encoding="utf-8")
    assert brim_joins_pieces(str(gcode), str(stl), 8.0) is None


# ---------------------------------------------------------------------------
# What the answer says
# ---------------------------------------------------------------------------


class _Result:
    success = True
    message = "Sliced."
    brim = None


@pytest.mark.parametrize(("kept", "again", "said", "drawn"), [
    (0.0, None, "No brim fits in that gap, so this slice has none", 0.0),
    (1.7, False, "so it is drawn 1.7 mm wide, which keeps it out of that gap", 1.7),
    (1.7, True, "still shows brim in that gap", 1.7),
    (1.7, None, "could not be read from the sliced file", 1.7),
    (None, None, "Set `brim_width` to 1.7 mm or less before printing", 8.0),
])
def test_the_answer_says_what_the_brim_did(kept, again, said, drawn):
    from kiln.slicer import BrimJoin, _say_brim_joined

    read_again = None if again is None else BrimJoin(joined=again, gap_mm=4.0 if again else None, keep_mm=1.7)
    result = _Result()
    _say_brim_joined(result, 8.0, BrimJoin(joined=True, gap_mm=4.04, keep_mm=1.7), kept, read_again)
    assert said in result.message, result.message
    assert "4.0 mm apart at the bed" in result.message
    assert result.brim["drawn_mm"] == drawn and result.brim["said"] in result.message
    assert "—" not in result.message


def test_the_bed_edge_is_judged_by_the_brim_the_slice_drew():
    """A brim the slice drew narrower, or left off, is the brim the check at
    the bed's edge measures: never the width that was asked for."""
    from kiln.plugins.slicer_tools import _drawn_brim_mm

    class _Narrowed:
        brim = {"asked_mm": 8.0, "drawn_mm": 0.0, "joins_pieces": False, "said": "..."}

    class _AsAsked:
        brim = None

    assert _drawn_brim_mm(_Narrowed(), 8.0) == 0.0
    assert _drawn_brim_mm(_AsAsked(), 8.0) == 8.0


def test_nothing_is_said_of_a_brim_that_joins_nothing():
    from kiln.slicer import BrimJoin, _say_brim_joined

    result = _Result()
    _say_brim_joined(result, 8.0, BrimJoin(joined=False), None, None)
    assert result.message == "Sliced." and result.brim is None


# ---------------------------------------------------------------------------
# What every slicer lays down
# ---------------------------------------------------------------------------


def _installed_slicers() -> dict[str, str]:
    """The slicers on this machine Kiln drives, by name."""
    found = {}
    for name, commands, mac in (
        ("PrusaSlicer", ("prusa-slicer", "PrusaSlicer", "prusaslicer"),
         "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"),
        ("Bambu Studio", ("bambu-studio",), "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        ("OrcaSlicer", ("orca-slicer",), "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
    ):
        exe = next((shutil.which(c) for c in commands if shutil.which(c)), None)
        if exe is None and os.path.isfile(mac) and os.access(mac, os.X_OK):
            exe = mac
        if exe:
            found[name] = exe
    return found


_SLICERS = _installed_slicers()
needs_a_slicer = pytest.mark.skipif(not _SLICERS, reason="no slicer installed: public CI has none")

_MOVE = re.compile(r"G0*[0-3](?=[ \t])")
_WORD = re.compile(r"([XYZE])(-?\d*\.?\d+)")
_LABEL = re.compile(r";\s*(?:TYPE|FEATURE)\s*:\s*(.+)")


def _first_layer(gcode: str) -> tuple[np.ndarray, np.ndarray]:
    """The first layer's brim (or PrusaSlicer's skirt/brim) samples and the
    whole file's outer-wall samples, every fifth of a millimetre."""
    brim: list[tuple[float, float, float]] = []
    walls: list[tuple[float, float, float]] = []
    x = y = z = last_e = 0.0
    relative, kind = True, ""
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                label = _LABEL.match(line)
                if label:
                    kind = label.group(1).strip().lower()
                continue
            if line.startswith("M82"):
                relative = False
            elif line.startswith("M83"):
                relative = True
            elif line.startswith("G92"):
                last_e = float(dict(_WORD.findall(line.split(";")[0])).get("E", last_e))
            if not _MOVE.match(line):
                continue
            words = dict(_WORD.findall(line.split(";")[0]))
            nx, ny, nz = float(words.get("X", x)), float(words.get("Y", y)), float(words.get("Z", z))
            pushed = 0.0
            if "E" in words:
                pushed = float(words["E"]) if relative else float(words["E"]) - last_e
                last_e = last_e if relative else float(words["E"])
            if pushed > 0.0 and (nx, ny) != (x, y):
                steps = max(1, int(math.hypot(nx - x, ny - y) / 0.2))
                run = [(x + (nx - x) * s / steps, y + (ny - y) * s / steps, nz) for s in range(steps + 1)]
                if "brim" in kind or "skirt" in kind:
                    brim.extend(run)
                elif "outer wall" in kind or "external perimeter" in kind:
                    walls.extend(run)
            x, y, z = nx, ny, nz
    brim_a = np.array(brim, dtype=float).reshape(-1, 3)
    if len(brim_a):
        brim_a = brim_a[brim_a[:, 2] <= brim_a[:, 2].min() + 0.01]
    return brim_a, np.array(walls, dtype=float).reshape(-1, 3)


def _in_the_part_frame(stl: Path, points: np.ndarray, walls: np.ndarray) -> np.ndarray:
    """*points* moved from the bed to the part's own frame: the shift that
    puts the file's walls over the part's box."""
    lo, hi = trimesh.load(str(stl), force="mesh").bounds
    assert len(walls), "the sliced file has no walls"
    shift = (walls[:, :2].min(axis=0) + walls[:, :2].max(axis=0)) / 2 - (lo[:2] + hi[:2]) / 2
    moved = points.copy()
    moved[:, :2] -= shift
    return moved


@pytest.fixture
def reslice(tmp_path, monkeypatch):
    """The reslice_with_overrides door, as an agent calls it to add a brim."""
    from kiln import monitor_twin
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    tools: dict = {}

    class _Mcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_Mcp())

    def call(stl: Path, slicer: str, brim_mm: float) -> dict:
        with patch("kiln.server._check_auth", return_value=None):
            result = tools["reslice_with_overrides"](
                input_path=str(stl), printer_id="bambu_p1s", overrides={"brim_width": f"{brim_mm:g}"},
                output_dir=str(tmp_path / "out"), slicer_path=_SLICERS[slicer], material="PLA",
            )
        assert result.get("success"), result
        return result

    return call


@needs_a_slicer
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_no_brim_runs_between_two_pieces(tmp_path, reslice, slicer):
    """The two eyes stand 4 mm apart at the bed and each pin stands in its
    eye's bore.  Each slicer draws an 8 mm brim its own way: through the gap
    between the eyes, or round each pin inside its bore, or round the outside
    alone.  Whichever it is, the sliced file carries no brim inside a bore and
    none that runs across from one eye to the other, and a brim the slice
    drew narrower or left off is said."""
    stl, axes = _hinged_pair(tmp_path / "pair.stl", half_span=9.0)
    result = reslice(stl, slicer, 8.0)
    brim, walls = _first_layer(result.get("raw_gcode_path") or result["output_path"])
    points = _in_the_part_frame(stl, brim, walls) if len(brim) else brim.reshape(-1, 3)
    in_a_bore = sum(int((np.hypot(points[:, 0] - x, points[:, 1] - y) < 4.6).sum()) for x, y in axes)
    assert in_a_bore == 0, f"{slicer}: {in_a_bore} brim samples inside a bore, round its pin"
    band = (np.abs(points[:, 0]) < 2.0) & (np.abs(points[:, 1]) < 5.0)
    left, right = points[band & (points[:, 0] < 0.0), 0], points[band & (points[:, 0] >= 0.0), 0]
    if len(left) and len(right):
        apart = float(right.min() - left.max())
        assert apart > 0.4, f"{slicer}: the brim runs across the gap between the eyes ({apart:.2f} mm clear)"
    said = result.get("brim")
    if said is not None:
        assert said["drawn_mm"] < 8.0 and said["said"] in result["message"], said


@needs_a_slicer
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_a_brim_left_off_where_none_fits(tmp_path, reslice, slicer):
    """Two blocks stand 0.5 mm apart: any brim reaches from one to the other,
    so the slice draws none and says the part stands on its own footprint."""
    stl = _two_blocks(tmp_path / "blocks.stl", 0.5)
    result = reslice(stl, slicer, 8.0)
    brim, walls = _first_layer(result.get("raw_gcode_path") or result["output_path"])
    points = _in_the_part_frame(stl, brim, walls) if len(brim) else brim
    # Nothing on the first layer within 2 mm of both blocks: a skirt stands farther off.
    near_both = (np.abs(points[:, 0]) < 2.25) & (np.abs(points[:, 1]) < 12.0) if len(points) else np.zeros(0, bool)
    assert int(near_both.sum()) == 0, f"{slicer}: {int(near_both.sum())} brim samples reach both blocks"
    said = result.get("brim") or {}
    assert said.get("drawn_mm") == 0.0, said
    assert "so this slice has none" in result["message"], result["message"]
