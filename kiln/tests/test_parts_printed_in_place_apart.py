"""Separate parts printed in place: what is said of their gap, and where
their supports stand.

Two contracts, each read from real output.

The gap.  Parts that stand apart without touching are said to print in
place together only when the gap between them is one that prints as a gap.
A gap under the smallest one moving parts are given
(:func:`kiln.assembly.moving_gap_floor_mm`, the one home of that figure) is
said as parts that may print stuck together, by the gap measured and never
by the figure; parts closer than a printer keeps apart are said to print
stuck together, and are never called loose.

The supports.  A file whose parts print in place a clearance apart gets its
supports from the plate alone wherever the plate serves every point that
needs one, so none stands on one part to hold up another.  Where the plate
does not serve them all the supports stand on the model, and the answer
says to break out any that stands between two parts.  The read of the
plate's reach counts its work before doing any: a face or a boundary of any
size, or with a corner that is not a number, is refused at once.  The
slicing half reads the G-code of every slicer installed and skips without
one.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln.printability import analyze_printability, judge_supports, verdict  # noqa: E402
from kiln.slicer_supports import decide_supports  # noqa: E402

MOVING = "the way parts that move have to"
PLATE_ONLY_KEY = "support_material_buildplate_only"


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _write(path: Path, meshes: list[Any]) -> Path:
    trimesh.util.concatenate(meshes).export(path)
    return path


# ---------------------------------------------------------------------------
# The gap
# ---------------------------------------------------------------------------


def _slider_and_slot(clear: float, dx: float = 0.0) -> list[Any]:
    """A block with a T-shaped slot closed at both ends, and a T-shaped
    slider printed in place in it *clear* mm from it all round: both stand
    on the plate and nothing joins them."""
    block = _box((-20, -12, 0), (20, 12, 10))
    block = block.difference(_box((-15, -7, -1), (15, 7, 6))).difference(_box((-15, -3, 5.9), (15, 3, 11)))
    slider = _box((-10, -7 + clear, 0), (10, 7 - clear, 6 - clear)).union(
        _box((-10, -3 + clear, 5.5 - clear), (10, 3 - clear, 12))
    )
    for mesh in (block, slider):
        mesh.apply_translation((dx, 0, 0))
    return [block, slider]


def _with_a_motion_note(path: Path, meshes: list[Any], names: list[str], moving: list[str]) -> Path:
    """*meshes* written to *path* with the note a proved motion keeps
    beside the file: each named part's run of triangles, and which move."""
    from kiln import mechanism_stage

    _write(path, meshes)
    runs, first = [], 0
    for mesh in meshes:
        runs.append([first, len(mesh.faces)])
        first += len(mesh.faces)
    parts = {name: {"spans": [run]} for name, run in zip(names, runs, strict=True)}
    block = {"kind": mechanism_stage.KIND, "playable": False, "parts": parts, "moving_parts": moving}
    assert mechanism_stage.record(str(path), block)
    return path


def _said(path: Path) -> str:
    return verdict(analyze_printability(str(path)))[1]


class TestTheGapIsSaidAsItIs:
    def test_moving_parts_are_told_they_print_in_place_only_at_a_gap_that_prints(self, tmp_path):
        import kiln.assembly as assembly

        floor = assembly._DEFAULT_JOINT_CLEARANCES["clearance_fit"][0]
        for clear in (0.1, 0.2, 0.4):
            meshes = _slider_and_slot(clear)
            said = _said(_with_a_motion_note(tmp_path / f"m{clear}.stl", meshes, ["block", "slider"], ["slider"]))
            assert f"They stand {clear:g} mm apart without touching" in said, said
            if clear < floor:
                assert "may print stuck together or too stiff to move: widen the gap." in said, said
                assert MOVING not in said, said
                assert "print in place together" not in said, said
            else:
                assert f"so they print in place together with nothing joining them, {MOVING}." in said, said
                assert "stuck" not in said, said
            # The person's own gap is said; the figure it is held to is not.
            assert f"{floor:g} mm" not in said, said

    def test_parts_not_known_to_move_are_told_too(self, tmp_path):
        tight = _said(_write(tmp_path / "tight.stl", _slider_and_slot(0.2)))
        assert "They stand 0.2 mm apart without touching, with nothing joining them, and at that gap they may " \
               "print stuck together." in tight, tight
        assert "If they are meant to come apart, widen the gap." in tight
        assert "fine for a designed clearance" not in tight
        clear = _said(_write(tmp_path / "clear.stl", _slider_and_slot(0.4)))
        assert "fine for a designed clearance" in clear, clear
        assert "stuck" not in clear

    def test_parts_closer_than_a_printer_keeps_apart_are_not_called_loose(self, tmp_path):
        for name, meshes in (
            ("slider", _slider_and_slot(0.03)),
            ("blocks", [_box((0, 0, 0), (20, 20, 10)), _box((20.03, 0, 0), (40, 20, 10))]),
        ):
            said = _said(_write(tmp_path / f"{name}.stl", meshes))
            # Counted as they print: one part, the count every reply says.
            assert "It prints as one solid, though it is drawn as 2 pieces: two of them sit 0.03 mm apart, " \
                   "closer than a printer keeps apart, so they print stuck together." in said, said
            assert "separate parts" not in said, said
            assert "comes off the plate on its own" not in said, said
            assert "print in place together" not in said, said

    def test_one_tight_gap_among_clear_ones_is_said_where_it_is(self, tmp_path):
        meshes = _slider_and_slot(0.2) + _slider_and_slot(0.4, dx=60.0)
        path = _with_a_motion_note(tmp_path / "two.stl", meshes, ["a", "b", "c", "d"], ["b", "d"])
        said = _said(path)
        assert "They stand 0.2 to 0.4 mm apart without touching" in said, said
        assert "Where the gap is 0.2 mm they may print stuck together or too stiff to move: widen it there." in said

    def test_a_piece_in_the_air_too_close_to_the_part(self, tmp_path):
        lid = [_box((0, 0, 0), (30, 30, 10)), _box((5, 5, 10.2), (15, 15, 14))]
        moving = _said(_with_a_motion_note(tmp_path / "moving.stl", lid, ["box", "lid"], ["lid"]))
        assert "1 piece sits 0.2 mm from the part, joined to nothing, and at that gap it may print stuck to it. " \
               "Parts that move need a gap that prints as a gap: widen it." in moving, moving
        assert MOVING not in moving
        still = _said(_write(tmp_path / "still.stl", lid))
        assert "at that gap it may print stuck to it. If it is meant to come off or move, widen the gap." in still
        assert "fine for a designed clearance" not in still

    def test_the_gap_is_held_to_the_clearance_fit_band_and_no_other_figure(self, tmp_path, monkeypatch):
        import kiln.assembly as assembly

        path = _with_a_motion_note(tmp_path / "s.stl", _slider_and_slot(0.4), ["block", "slider"], ["slider"])
        assert MOVING in _said(path)
        low, high = assembly._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        monkeypatch.setitem(assembly._DEFAULT_JOINT_CLEARANCES, "clearance_fit", (0.5, high))
        widened = _said(path)
        assert "may print stuck together or too stiff to move" in widened, widened
        assert MOVING not in widened
        assert assembly.moving_gap_floor_mm() == 0.5 and low < 0.5


# ---------------------------------------------------------------------------
# The supports: what is decided and said
# ---------------------------------------------------------------------------

#: A 4 mm block, and a deck on two posts 34 mm apart that crosses 3 mm over
#: it: the near post stands 0.4 mm from the block.  The plate is clear under
#: the rest of the deck.
_BLOCK = ((0.0, 0.0, 0.0), (4.0, 20.0, 5.0))


def _deck_over_a_block(path: Path) -> Path:
    deck = _box((-6, 0, 0), (-0.4, 20, 10)).union(_box((34, 0, 0), (40, 20, 10))).union(_box((-6, 0, 8), (40, 20, 10)))
    return _write(path, [_box(*_BLOCK), deck])


#: A 40 mm block, and a lid on a leg 0.4 mm beside it whose slab reaches
#: 35 mm over the block, 2 mm above it: nothing under the slab but the block.
_UNDER_THE_LID = ((5.0, 40.0), (5.0, 35.0), 5.0, 7.0)


def _lid_over_a_block(path: Path) -> Path:
    lid = _box((40.4, 0, 0), (45, 40, 9)).union(_box((5, 5, 7), (45, 35, 9)))
    return _write(path, [_box((0, 0, 0), (40, 40, 5)), lid])


def _cap_over_its_own_base(path: Path) -> Path:
    """One part: a cap on a stem over its own wider base."""
    part = _box((-15, -15, 0), (15, 15, 4)).union(_box((-2, -2, 4), (2, 2, 12))).union(_box((-12, -12, 12), (12, 12, 14)))
    return _write(path, [part])


class TestWhereTheSupportsStand:
    def test_parts_apart_get_supports_from_the_plate_where_it_serves_them_all(self, tmp_path):
        path = str(_deck_over_a_block(tmp_path / "deck.stl"))
        decision = decide_supports(path, material="PLA")
        assert decision.enabled and decision.settings[PLATE_ONLY_KEY] == "1"
        assert decision.note.endswith("asked for from the plate only, as its parts print in place apart from each other")
        judged = judge_supports(path, material="pla")
        assert judged.bridging.needs_supports_for_bridges
        assert (judged.parts_apart, judged.plate_reaches_supports) == (True, True)

    def test_supports_stay_on_the_model_where_the_plate_cannot_serve(self, tmp_path):
        path = str(_lid_over_a_block(tmp_path / "lid.stl"))
        decision = decide_supports(path, material="PLA")
        assert decision.enabled and decision.settings[PLATE_ONLY_KEY] == "0"
        assert decision.note.endswith("break out any support that stands on one part to hold up another")
        judged = judge_supports(path, material="pla")
        assert (judged.parts_apart, judged.plate_reaches_supports) == (True, False)

    def test_one_part_alone_is_never_read_as_parts_apart(self, tmp_path):
        """A part whose plate cannot serve its supports, and a part whose
        plate can: neither is parts apart, so both keep supports that may
        stand on the model."""
        tee = _write(tmp_path / "tee.stl", [_box((-3, -3, 0), (3, 3, 20)).union(_box((-20, -3, 18), (20, 3, 20)))])
        for path in (_cap_over_its_own_base(tmp_path / "cap.stl"), tee):
            decision = decide_supports(str(path), material="PLA")
            assert decision.enabled and decision.settings[PLATE_ONLY_KEY] == "0"
            assert "print in place apart" not in decision.note
            judged = judge_supports(str(path), material="pla")
            assert (judged.parts_apart, judged.plate_reaches_supports) == (False, False)

    def test_the_advice_to_use_supports_says_what_to_watch_for(self, tmp_path):
        deck = analyze_printability(str(_deck_over_a_block(tmp_path / "deck.stl"))).recommendations
        asked = [line for line in deck if "nable supports" in line]
        assert asked and asked[0].endswith(
            "Its parts print in place apart from each other, so use supports from the build plate only: "
            "one that stands on a part to hold up another locks the two together."
        ), deck
        assert sum("print in place apart" in line for line in deck) == 1, deck
        lid = analyze_printability(str(_lid_over_a_block(tmp_path / "lid.stl"))).recommendations
        told = [line for line in lid if "print in place apart" in line]
        assert len(told) == 1 and told[0].endswith(
            "some of these supports have to stand on the model: break out any that stands on one part to hold "
            "up another, or the two stay locked together."
        ), lid
        alone = analyze_printability(str(_cap_over_its_own_base(tmp_path / "cap.stl"))).recommendations
        assert not any("print in place apart" in line for line in alone), alone

    def test_every_door_that_says_the_pieces_says_it(self, tmp_path):
        """The verdict's sentence carries it beside the pieces, and the
        support assessment among its recommendations."""
        from kiln.support_assessment import assess_support_feasibility

        deck, lid, cap = (
            str(_deck_over_a_block(tmp_path / "deck.stl")), str(_lid_over_a_block(tmp_path / "lid.stl")),
            str(_cap_over_its_own_base(tmp_path / "cap.stl")),
        )
        from_the_plate = (
            "Use supports from the build plate only: one that stands on a part to hold up another locks the "
            "two together."
        )
        break_out = (
            "Some of its supports have to stand on the model: break out any that stands on one part to hold up "
            "another, or the two stay locked together."
        )
        assert _said(Path(deck)).endswith(f"fine for a designed clearance. If they were meant to be one solid, "
                                          f"sink them at least 0.4 mm into each other. {from_the_plate}")
        assert _said(Path(lid)).endswith(break_out)
        assert "locked together" not in _said(Path(cap)) and "locks the two" not in _said(Path(cap))
        assert any(line.endswith(f"so u{from_the_plate[1:]}")
                   for line in assess_support_feasibility(stl_path=deck, material="pla").recommendations)
        assert any("break out any that stands on one part to hold up another" in line
                   for line in assess_support_feasibility(stl_path=lid, material="pla").recommendations)
        assert not any("print in place apart" in line
                       for line in assess_support_feasibility(stl_path=cap, material="pla").recommendations)


# ---------------------------------------------------------------------------
# The supports: the read of the plate's reach is bounded
# ---------------------------------------------------------------------------


@pytest.fixture
def within_seconds():
    """Fails the test if its body runs past ten seconds, where the platform
    has an alarm to say so: a read that is refused does no work first."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def too_long(_signum, _frame):
        raise AssertionError("the read ran past ten seconds")

    before = signal.signal(signal.SIGALRM, too_long)
    signal.setitimer(signal.ITIMER_REAL, 10.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, before)


def _one_face(a, b, c) -> np.ndarray:
    return np.array([[a, b, c]], dtype=np.float64)


_SMALL = ((0.0, 0.0, 5.0), (6.0, 0.0, 5.0), (0.0, 6.0, 5.0))


class TestTheReadIsBounded:
    @pytest.mark.parametrize("far", [1e7, 1e12, 1e19, 1e150, 1e300])
    def test_an_enormous_face_is_refused_before_any_grid_is_laid(self, within_seconds, far):
        from kiln.printability import _grid_points

        assert len(_grid_points(_one_face(*_SMALL))) == 21  # five steps along its longest edge
        enormous = _one_face((0.0, 0.0, 5.0), (far, 0.0, 5.0), (0.0, far, 5.0))
        assert _grid_points(enormous) is None
        assert _grid_points(np.concatenate([_one_face(*_SMALL), enormous])) is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_corner_that_is_not_a_number_is_refused(self, within_seconds, bad):
        from kiln.printability import _grid_points

        for at in range(3):
            corners = [list(c) for c in _SMALL]
            corners[at][0] = bad
            assert _grid_points(_one_face(*corners)) is None
            assert _grid_points(np.concatenate([_one_face(*_SMALL), _one_face(*corners)])) is None

    @pytest.mark.parametrize("run", [1e12, 1e19, 1e300, float("inf"), float("nan")])
    def test_an_enormous_boundary_is_refused_before_it_is_walked(self, within_seconds, run):
        """A patch over another face, with a plate-clear side, whose one
        supported boundary segment runs *run* mm."""
        from kiln.printability import _points_plate_supports_miss
        from kiln.triangle_index import XYIndex

        deck = np.array([
            [(0.0, 0.0, 8.0), (40.0, 0.0, 8.0), (40.0, 20.0, 8.0)],
            [(0.0, 0.0, 8.0), (40.0, 20.0, 8.0), (0.0, 20.0, 8.0)],
        ])
        under = np.array([[(0.0, 0.0, 2.0), (12.0, 0.0, 2.0), (12.0, 20.0, 2.0)],
                          [(0.0, 0.0, 2.0), (12.0, 20.0, 2.0), (0.0, 20.0, 2.0)]])
        tris = np.concatenate([deck, under])
        own = np.array([True, True, False, False])
        pa, d_seg, supported = np.array([[-50.0, -5.0]]), np.array([[0.0, run]]), np.array([True])
        assert _points_plate_supports_miss(deck, tris, XYIndex(tris), own, pa, d_seg, supported, 0.2) is None

    def test_a_part_with_a_corner_that_is_not_a_number_is_not_read(self, within_seconds):
        from kiln.printability import _DownwardRegion, _plate_reaches_every_support

        tris = np.array([_SMALL, ((0.0, 0.0, 0.0), (6.0, 0.0, 0.0), (float("nan"), 6.0, 0.0))], dtype=np.float64)
        patch = _DownwardRegion(
            triangle_indices=[0], flat_indices=[0], span_mm=20.0, needs_supports=True, bbox_span_mm=6.0,
            boundary=(np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0, dtype=bool)),
        )
        assert _plate_reaches_every_support(tris, [patch], [], 0.2) is False


# ---------------------------------------------------------------------------
# The supports: what the slicer lays down
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


def _support_points(gcode: str) -> np.ndarray:
    """Every support extrusion in the file, sampled every quarter
    millimetre: rows of (x, y, z).  Reads each driven slicer's labels and
    relative or absolute extrusion."""
    points: list[tuple[float, float, float]] = []
    x = y = z = last_e = 0.0
    relative, support = True, False
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                label = _LABEL.match(line)
                if label:
                    support = label.group(1).strip().lower().startswith("support")
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
            if support and pushed > 0.0 and (nx, ny) != (x, y):
                steps = max(1, int(math.hypot(nx - x, ny - y) / 0.25))
                points.extend((x + (nx - x) * s / steps, y + (ny - y) * s / steps, nz) for s in range(steps + 1))
            x, y, z = nx, ny, nz
    return np.array(points, dtype=float).reshape(-1, 3)


@pytest.fixture
def _slice(tmp_path, monkeypatch):
    """Slice a part through Kiln's own slicing step, as every door does:
    the result, and the support samples in the part's own frame."""
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

    def slice_(stl: Path, slicer: str):
        lo, hi = trimesh.load(str(stl)).bounds
        shift = np.array([centre[0] - (lo[0] + hi[0]) / 2, centre[1] - (lo[1] + hi[1]) / 2, -lo[2]])
        result = slice_file(str(stl), output_dir=str(tmp_path / "out"), profile=profile,
                            slicer_path=_SLICERS[slicer], material="PLA", timeout=900)
        return result, _support_points(result.output_path) - shift

    return slice_


@needs_a_slicer
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
class TestWhatTheSlicerLaysDown:
    def test_no_support_stands_on_one_part_to_hold_up_another(self, tmp_path, _slice, slicer):
        result, points = _slice(_deck_over_a_block(tmp_path / "deck.stl"), slicer)
        assert result.supports.enabled, result.supports.note
        (x0, y0, _z0), (x1, y1, top) = _BLOCK
        over_the_block = ((points[:, 0] > x0) & (points[:, 0] < x1) & (points[:, 1] > y0) & (points[:, 1] < y1)
                          & (points[:, 2] > top))
        assert int(over_the_block.sum()) == 0, f"{slicer}: {int(over_the_block.sum())} support samples over the block"
        # The deck is still held where the plate is clear under it.
        held = (points[:, 0] > x1 + 2.0) & (points[:, 0] < 32.0) & (points[:, 2] < 8.0)
        assert int(held.sum()) > 1000, f"{slicer}: almost no support under the deck ({int(held.sum())} samples)"
        assert "from the plate only" in result.supports.note

    def test_a_slab_only_the_other_part_can_hold_up_keeps_its_supports(self, tmp_path, _slice, slicer):
        result, points = _slice(_lid_over_a_block(tmp_path / "lid.stl"), slicer)
        assert result.supports.enabled, result.supports.note
        (x0, x1), (y0, y1), bottom, top = _UNDER_THE_LID
        under_the_slab = ((points[:, 0] > x0) & (points[:, 0] < x1) & (points[:, 1] > y0) & (points[:, 1] < y1)
                          & (points[:, 2] > bottom) & (points[:, 2] < top))
        assert int(under_the_slab.sum()) > 1000, f"{slicer}: the slab is left on nothing"
        assert "break out any support that stands on one part to hold up another" in result.supports.note
