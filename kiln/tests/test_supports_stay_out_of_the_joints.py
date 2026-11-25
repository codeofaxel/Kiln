"""Supports stay out of the joints between parts printed in place.

Parts that print in place apart meet in joints: here a captive pin hanging
from a bar into the bore of an eye, with a relief where the gap meets the
plate, as a claw or a pair of lazy tongs is drawn.  A slice whose supports
Kiln decided keeps every slicer's supports out of those gaps, and the answer
says the joints carry none only when the sliced file shows none there, and
says so plainly when the file cannot be read for it.

The slicing half slices through the ``slice_model`` door with every slicer
installed, with no printer named and for a Bambu Lab P1S, with public Kiln's
overhang figure and with the plastic's own figure as a signed-in install is
served it, and counts the support the G-code lays inside each bore.  It
skips without a slicer: public CI has none.

The same door, asked for a P1S with the part off its bed, centres the part
and says where it stands: the bed-fit block judges the copy that is sliced,
and the G-code it describes prints on the bed.
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

from kiln.printability import judge_supports  # noqa: E402
from kiln.slicer_supports import SliceSupports, decide_supports, witnessed  # noqa: E402

#: The pin's outline turned about its axis, (radius, height): a relief at the
#: plate, a waist that holds it captive, and a head under the bar.
_PIN = [(0, 0), (3.1, 0), (3.6, 0.5), (2.4, 2.5), (3.6, 5), (3.6, 5.6), (5.5, 7.5), (5.5, 10.5), (0, 10.5)]
#: The bore it hangs in, 0.3 mm from it all round, with the same relief.
_BORE = [(0, -1), (4.4, -1), (4.4, 0), (3.9, 0.5), (2.7, 2.5), (3.9, 5), (4.9, 6), (0, 6)]
#: How far from a joint's axis a support stands inside its bore.
_IN_A_BORE_MM = 4.6
_HALF_SPAN = 25.0


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


def _two_pins(path: Path, *, at: tuple[float, float] = (0.0, 0.0)) -> tuple[Path, list[tuple[float, float]]]:
    """A bar with a pin at each end, each hanging in its own eye, printed in
    place: three parts, two joints, and a 36 mm bridge under the bar with
    nothing but the plate below it.  Returns the file and the joints' axes."""
    axes = [(at[0] - _HALF_SPAN, at[1]), (at[0] + _HALF_SPAN, at[1])]
    eyes = []
    for axis in axes:
        eye = trimesh.creation.cylinder(radius=7.0, height=5.0, sections=72)
        eye.apply_translation((axis[0], axis[1], 2.5))
        eyes.append(eye.difference(_turned(_BORE, axis)))
    bar = trimesh.creation.box(extents=(2 * _HALF_SPAN, 11.0, 3.0))
    bar.apply_translation((at[0], at[1], 9.0))
    for axis in axes:
        bar = bar.union(_turned(_PIN, axis))
    trimesh.util.concatenate([*eyes, bar]).export(path)
    return path, axes


# ---------------------------------------------------------------------------
# The faces in the joints
# ---------------------------------------------------------------------------


def test_the_faces_in_the_joints_are_kept_clear_and_none_needs_support(tmp_path):
    stl, axes = _two_pins(tmp_path / "pins.stl")
    decision = decide_supports(str(stl), material="PLA", nozzle_diameter=0.4, layer_height=0.2)
    assert decision.enabled and decision.parts_apart, decision.note
    assert decision.in_joints, "no face was read as in a joint"

    from kiln.printability import _load_mesh_for_analysis

    tris = np.asarray(_load_mesh_for_analysis(str(stl))[0], dtype=float)
    faces = tris[list(decision.in_joints)]
    normals = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    assert (normals[:, 2] < 0).all(), "a face in a joint does not face down"
    assert (faces[:, :, 2].max(axis=1) > 0.0).all(), "a face resting on the plate was read as in a joint"
    # Every one lies inside a bore: the relief, the waist, nothing of the bar.
    centres = faces.mean(axis=1)
    reach = np.min([np.hypot(centres[:, 0] - x, centres[:, 1] - y) for x, y in axes], axis=0)
    assert (reach < _IN_A_BORE_MM + 0.5).all() and (centres[:, 2] < 5.0).all()
    # Both sides of each joint: the pin's relief and the bore's.
    assert (reach < 3.6).any() and (reach > 3.9).any()
    judged = judge_supports(str(stl), material="PLA", nozzle_diameter=0.4, layer_height=0.2)
    held = {i for region in judged.downward_regions if region.needs_supports for i in region.triangle_indices}
    assert held, "the bar's bridge was not read as needing supports"
    assert not held & set(decision.in_joints), "a face that needs supports would be kept clear"


def test_one_part_alone_has_no_joints(tmp_path):
    bar = trimesh.creation.box(extents=(50.0, 11.0, 3.0))
    bar.apply_translation((0.0, 0.0, 9.0))
    for axis in ((-_HALF_SPAN, 0.0), (_HALF_SPAN, 0.0)):
        bar = bar.union(_turned(_PIN, axis))
    bar.export(tmp_path / "bar.stl")
    decision = decide_supports(str(tmp_path / "bar.stl"), material="PLA", nozzle_diameter=0.4, layer_height=0.2)
    assert not decision.parts_apart and decision.in_joints == ()
    assert "joints" not in decision.note


def test_a_file_that_cannot_be_read_has_no_joints_read(tmp_path):
    from kiln.slicer_supports import faces_in_joints

    junk = tmp_path / "junk.stl"
    junk.write_text("solid nothing\nendsolid nothing\n")
    assert faces_in_joints(str(junk), None) is None


# ---------------------------------------------------------------------------
# What the answer says, read from the file
# ---------------------------------------------------------------------------


def _decided(tmp_path) -> SliceSupports:
    """The pins' decision with the faces painted, placed on a 200 mm bed."""
    from kiln.slicer_supports import placed_for_reading

    stl, _axes = _two_pins(tmp_path / "pins.stl")
    decision = decide_supports(str(stl), material="PLA", nozzle_diameter=0.4, layer_height=0.2)
    painted = (decision.kept_off_painted or 0) + len(decision.in_joints)
    return placed_for_reading(str(stl), _with(decision, kept_off_painted=painted), bed_centre=(100.0, 100.0))


def _with(decision: SliceSupports, **changes) -> SliceSupports:
    from dataclasses import replace

    return replace(decision, **changes)


def _file(box, support: list[tuple[float, float, float]], *, walls: bool = True) -> str:
    """A sliced file: the part's outer wall round *box* when *walls*, and a
    short support line at each of *support*."""
    x0, y0, x1, y1 = box
    lines = ["M83"]
    if walls:
        lines += ["G1 Z0.2", ";TYPE:External perimeter", f"G1 X{x0:.3f} Y{y0:.3f}"]
        lines += [f"G1 X{x:.3f} Y{y:.3f} E1" for x, y in ((x1, y0), (x1, y1), (x0, y1), (x0, y0))]
    for x, y, z in support:
        lines += [f"G1 Z{z:.3f}", ";TYPE:Support material", f"G1 X{x - 0.2:.3f} Y{y:.3f}", f"G1 X{x + 0.2:.3f} Y{y:.3f} E0.02"]
    return "\n".join(lines) + "\n"


def _under_a_joint(decision: SliceSupports) -> tuple[float, float, float]:
    """A point a little under the widest face in the joints above the
    relief, as placed: where a support in the gap of a bore stands."""
    tris = np.asarray(decision.joints_clear, dtype=float)
    tris = tris[tris[:, :, 2].min(axis=1) >= 0.5]
    span = np.ptp(tris[:, :, 0], axis=1) * np.ptp(tris[:, :, 1], axis=1)
    widest = tris[int(np.argmax(span))]
    x, y, z = widest.mean(axis=0)
    return float(x), float(y), float(z - 0.3)


def test_nothing_is_said_of_the_joints_before_a_file_is_read(tmp_path):
    decision = _decided(tmp_path)
    assert decision.joints_clear and decision.placed_box is not None
    assert "joints" not in decision.note
    assert "in_joints" not in decision.to_dict() and "on_joints" not in decision.to_dict()


@pytest.mark.parametrize(("name", "said"), [
    ("clear", "and none stands inside the joints between them"),
    ("in_a_joint", "the slicer still put some inside the joints between them"),
    ("no_part_in_the_file", "could not be read from the sliced file"),
    ("another_part_in_the_file", "could not be read from the sliced file"),
])
def test_the_answer_says_what_the_file_shows_in_the_joints(tmp_path, name, said):
    decision = _decided(tmp_path)
    box = decision.placed_box
    beside = (box[0] - 10.0, box[1] - 10.0, 0.2)
    if name == "clear":
        text = _file(box, [beside])
    elif name == "in_a_joint":
        text = _file(box, [_under_a_joint(decision)])
    elif name == "no_part_in_the_file":
        text = _file(box, [_under_a_joint(decision)], walls=False)
    else:
        text = _file((box[0], box[1], box[0] + 20.0, box[1] + 20.0), [_under_a_joint(decision)])
    gcode = tmp_path / f"{name}.gcode"
    gcode.write_text(text, encoding="utf-8")
    seen = witnessed(decision, str(gcode))
    assert said in seen.note, f"{name}: {seen.note}"
    if name != "clear":
        assert "none stands inside the joints" not in seen.note


def test_a_part_the_slicer_moved_is_read_where_it_printed(tmp_path):
    decision = _decided(tmp_path)
    x0, y0, x1, y1 = decision.placed_box
    moved = (x0 + 30.0, y0 - 12.0, x1 + 30.0, y1 - 12.0)
    x, y, z = _under_a_joint(decision)
    gcode = tmp_path / "moved.gcode"
    gcode.write_text(_file(moved, [(x + 30.0, y - 12.0, z)]), encoding="utf-8")
    assert witnessed(decision, str(gcode)).on_joints > 0
    gcode.write_text(_file(moved, [(x, y, z)]), encoding="utf-8")
    assert witnessed(decision, str(gcode)).on_joints == 0


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


def _extrusion(gcode: str) -> tuple[np.ndarray, np.ndarray]:
    """Every support extrusion and every wall extrusion in the file,
    sampled every fifth of a millimetre: rows of (x, y, z)."""
    support: list[tuple[float, float, float]] = []
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
                if kind.startswith("support"):
                    support.extend(run)
                elif "wall" in kind or "perimeter" in kind:
                    walls.extend(run)
            x, y, z = nx, ny, nz
    return np.array(support, dtype=float).reshape(-1, 3), np.array(walls, dtype=float).reshape(-1, 3)


def _in_the_part_frame(stl: Path, support: np.ndarray, walls: np.ndarray) -> np.ndarray:
    """*support* moved from the bed to the part's own frame: the shift that
    puts the file's walls over the part's box, checked to land them on it."""
    lo, hi = trimesh.load(str(stl), force="mesh").bounds
    assert len(walls), "the sliced file has no walls"
    shift = (walls[:, :2].min(axis=0) + walls[:, :2].max(axis=0)) / 2 - (lo[:2] + hi[:2]) / 2
    size = walls[:, :2].max(axis=0) - walls[:, :2].min(axis=0)
    assert np.allclose(size, (hi - lo)[:2], atol=1.5), f"the part was turned or scaled in the file: {size}"
    moved = support.copy()
    moved[:, :2] -= shift
    return moved


@pytest.fixture
def slice_model(tmp_path, monkeypatch):
    """The slice_model door, as an agent calls it."""
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

    def call(stl: Path, slicer: str, printer: str | None) -> dict:
        with patch("kiln.server._check_auth", return_value=None):
            result = tools["slice_model"](
                input_path=str(stl), output_dir=str(tmp_path / "out"), material="PLA",
                printer_id=printer, slicer_path=_SLICERS[slicer],
            )
        assert result.get("success"), result
        return result

    return call


def _served(monkeypatch, degrees: float) -> None:
    """The plastic's own overhang figure, as Kiln's servers give a
    signed-in install."""
    from kiln import _pro_material_limits_bridge as bridge

    answer = {"known": True, "material_id": "pla",
              "limits": {"overhang_unsupported_deg": {"value": degrees, "basis": "plastic"}}}
    monkeypatch.setattr(bridge, "ask", lambda *_a, **_k: (answer, None))


@needs_a_slicer
@pytest.mark.parametrize("figure", ["public", "served"])
@pytest.mark.parametrize("printer", [None, "bambu_p1s"])
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_no_support_stands_inside_a_joint(tmp_path, monkeypatch, slice_model, slicer, printer, figure):
    if figure == "served":
        _served(monkeypatch, 55.0)
    stl, axes = _two_pins(tmp_path / "pins.stl")
    result = slice_model(stl, slicer, printer)
    supports = result["supports"]
    assert supports["enabled"], supports["note"]
    raw = result.get("raw_gcode_path") or result["output_path"]
    points = _in_the_part_frame(stl, *_extrusion(raw))
    in_a_bore = sum(int((np.hypot(points[:, 0] - x, points[:, 1] - y) < _IN_A_BORE_MM).sum()) for x, y in axes)
    assert in_a_bore == 0, f"{slicer}: {in_a_bore} support samples inside the bores; {supports['note']}"
    # The bar is still held across the plate between the eyes.
    under_the_bar = (np.abs(points[:, 0]) < _HALF_SPAN - 9.0) & (np.abs(points[:, 1]) < 4.0)
    assert int(under_the_bar.sum()) > 500, f"{slicer}: almost no support under the bar"
    assert "none stands inside the joints between them" in supports["note"], supports["note"]


@pytest.mark.skipif("PrusaSlicer" not in _SLICERS, reason="needs PrusaSlicer")
def test_a_centred_part_is_judged_where_it_is_sliced(tmp_path, slice_model):
    """A P1S's bed has its corner at the origin, so the pins drawn about the
    origin stand off it; Kiln centres them, and every word of the bed-fit
    block is about the copy it sliced."""
    from kiln.printers.bed_fit import printer_bed_volume

    stl, _axes = _two_pins(tmp_path / "pins.stl")
    result = slice_model(stl, "PrusaSlicer", "bambu_p1s")
    fit = result["bed_fit"]
    assert fit["auto_centered"] is True, fit
    assert fit["ok"] is True and fit["error_code"] is None and not fit["error_message"], fit
    assert "may crash" not in str(result) and "auto_center=True" not in str(result)
    assert not fit.get("slicer_moved_the_part"), fit
    # The G-code itself prints on the bed.
    support, walls = _extrusion(result.get("raw_gcode_path") or result["output_path"])
    printed = np.vstack([support, walls])
    width, depth, _height = printer_bed_volume("bambu_p1s")
    assert printed[:, 0].min() >= 0.0 and printed[:, 1].min() >= 0.0
    assert printed[:, 0].max() <= width and printed[:, 1].max() <= depth
