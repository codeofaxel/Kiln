"""Supports are never kept off a piece that starts in mid-air.

A slice that carries supports keeps them off the undersides the verdict
measured as printing on their own, so a screw thread beside a shelf is not
packed with support.  Undersides that start in mid-air just above the plate
are an exception to that rule.

An underside that holds a lowest point the first layer does not reach is
left to the slicer whole.  A thread's flank, a lip and a socket's ceiling hold
none, and are kept clear as before (``test_supports_leave_threads_alone.py``).

The read itself can mislead.  A part with a piece that starts a
layer up and nothing else to hold would read as "no supports" and be sliced
without them -- a foot a fraction of a millimetre over the plate, or slivers
left hanging where a hole cuts through a wall's edge.  A part that reads as
needing none is looked at once more for a piece that starts on nothing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from kiln import printability  # noqa: E402
from kiln.printability import _faces_at_a_start_in_mid_air, judge_supports  # noqa: E402

#: Where the first 0.2 mm layer is cut.
_FIRST_CUT = 0.1


def _corners(mesh) -> np.ndarray:
    return np.asarray(mesh.triangles, dtype=np.float64)


def _spike(tip_z: float):
    """A four-sided spike pointing down, its tip at *tip_z*."""
    spike = trimesh.creation.cone(radius=5.0, height=10.0, sections=4)
    spike.apply_transform(trimesh.transformations.rotation_matrix(np.pi, (1, 0, 0)))
    spike.apply_translation((0.0, 0.0, tip_z - spike.bounds[0][2]))
    return spike


def _box(bottom_z: float):
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    box.apply_translation((0.0, 0.0, bottom_z + 5.0))
    return box


# ---------------------------------------------------------------------------
# What a start in mid-air is
# ---------------------------------------------------------------------------


def test_the_tip_of_a_spike_that_hangs_over_the_plate_is_a_start() -> None:
    tris = _corners(_spike(3.0))
    starts = _faces_at_a_start_in_mid_air(tris, _FIRST_CUT)
    assert starts.any() and np.allclose(tris[starts][:, :, 2].min(axis=1), 3.0)


def test_a_level_underside_that_hangs_over_the_plate_is_one_start() -> None:
    """A level patch has no corner below any of its corners: all of it starts."""
    tris = _corners(_box(3.0))
    starts = _faces_at_a_start_in_mid_air(tris, _FIRST_CUT)
    level = np.isclose(tris[:, :, 2], 3.0).all(axis=1)
    assert level.sum() == 2 and starts[level].all()


@pytest.mark.parametrize("bottom", [0.0, 0.05])
def test_what_the_first_layer_reaches_is_no_start(bottom: float) -> None:
    for mesh in (_box(bottom), _spike(bottom)):
        assert not _faces_at_a_start_in_mid_air(_corners(mesh), _FIRST_CUT).any()


def test_a_lip_that_hangs_from_a_wall_is_no_start() -> None:
    """Every corner of the lip's underside has the wall's foot below it."""
    profile = [(0.0, 0.0), (5.0, 0.0), (5.0, 10.0), (9.0, 14.0), (9.0, 16.0), (0.0, 16.0)]
    post = trimesh.creation.revolve(np.asarray(profile, dtype=float), sections=48)
    tris = _corners(post)
    starts = _faces_at_a_start_in_mid_air(tris, _FIRST_CUT)
    assert not starts.any()


def test_an_empty_mesh_has_none() -> None:
    assert _faces_at_a_start_in_mid_air(np.zeros((0, 3, 3)), _FIRST_CUT).shape == (0,)


# ---------------------------------------------------------------------------
# What a slice keeps supports off
# ---------------------------------------------------------------------------


def _tower_shelf_and_ball(tmp_path: Path, ball_bottom: float) -> tuple[str, np.ndarray]:
    """A tower with a 30 mm shelf, which needs supports, and beside it a
    4 mm ball whose lowest point is *ball_bottom* above the plate.  Returns
    the file and which of its judged faces are the ball's."""
    tower = trimesh.creation.box(extents=(20.0, 20.0, 30.0))
    tower.apply_translation((0.0, 0.0, 15.0))
    shelf = trimesh.creation.box(extents=(30.0, 20.0, 3.0))
    shelf.apply_translation((25.0, 0.0, 28.5))
    ball = trimesh.creation.icosphere(subdivisions=3, radius=4.0)
    ball.apply_translation((-20.0, 0.0, ball_bottom + 4.0))
    path = tmp_path / "part.stl"
    trimesh.util.concatenate([tower, shelf, ball]).export(str(path))
    facets, vertices = printability._parse_mesh(str(path))
    judged = np.asarray(printability._prepare_parsed(list(facets), vertices)[0], dtype=np.float64)
    return str(path), judged.mean(axis=1)[:, 0] < -12.0


def test_supports_are_not_kept_off_a_ball_that_starts_a_layer_up(tmp_path: Path, monkeypatch) -> None:
    path, on_ball = _tower_shelf_and_ball(tmp_path, 0.3)

    kept_off = np.asarray(judge_supports(path, material="pla").support_kept_off_faces, dtype=int)
    assert not on_ball[kept_off].any()

    # Without the rule the same reading keeps them off 220 of the ball's faces.
    monkeypatch.setattr(printability, "_faces_at_a_start_in_mid_air", lambda tris, _z: np.zeros(len(tris), dtype=bool))
    before = np.asarray(judge_supports(path, material="pla").support_kept_off_faces, dtype=int)
    assert on_ball[before].sum() > 100


def test_a_ball_that_stands_on_the_plate_is_still_kept_clear(tmp_path: Path) -> None:
    """The rule is about a start the first layer misses, not about balls."""
    path, on_ball = _tower_shelf_and_ball(tmp_path, 0.0)

    kept_off = np.asarray(judge_supports(path, material="pla").support_kept_off_faces, dtype=int)
    assert on_ball[kept_off].sum() > 100


# ---------------------------------------------------------------------------
# What the read says of a piece that starts on nothing
# ---------------------------------------------------------------------------


def _block(low: tuple[float, float, float], high: tuple[float, float, float]):
    block = trimesh.creation.box(extents=np.subtract(high, low))
    block.apply_translation(np.add(low, high) / 2.0)
    return block


#: A 20 mm tower standing on the plate, about the middle of a 256 mm bed.
_TOWER = ((108.0, 118.0, 0.0), (128.0, 138.0, 20.0))


def _read(tmp_path: Path, *others) -> tuple[str | None, str]:
    """What the read says of the tower with *others* beside it, and the file."""
    path = tmp_path / "part.stl"
    trimesh.util.concatenate([_block(*_TOWER), *(_block(low, high) for low, high in others)]).export(str(path))
    return printability.support_verdict(judge_supports(str(path), material="pla")), str(path)


def _second_body(up: float, x: float = 138.0):
    return (x, 118.0, up), (x + 10.0, 128.0, up + 10.0)


def test_a_second_body_a_layer_over_the_plate_needs_supports_and_is_said_as_what_it_is(tmp_path: Path, monkeypatch):
    said, path = _read(tmp_path, _second_body(0.3))
    assert said == "starts in mid-air at 0.3mm"

    # Before, the plate band read it as resting on the plate.
    monkeypatch.setattr(printability, "_starts_on_nothing", lambda *_a, **_k: ([], 0, 0.0))
    assert printability.support_verdict(judge_supports(path, material="pla")) is None


def test_two_of_them_are_counted_and_the_lowest_named(tmp_path: Path) -> None:
    said, _path = _read(tmp_path, _second_body(0.3), _second_body(0.4, x=88.0))
    assert said == "2 pieces start in mid-air, lowest at 0.3mm"


@pytest.mark.parametrize("others, why", [
    ([_second_body(0.0)], "it stands on the plate"),
    ([_second_body(0.05)], "the first layer reaches it"),
    ([((128.0, 123.0, 0.3), (138.0, 133.0, 10.3))], "a ledge that touches the tower grows out of the tower"),
    ([((108.2, 118.2, 0.2), (127.8, 137.8, 19.8))], "the floor of a hollow drawn with its inner skin facing out"),
    ([((113.0, 123.0, 20.0), (123.0, 133.0, 30.0))], "a block resting on the tower"),
    ([((113.0, 123.0, 18.0), (123.0, 133.0, 30.0))], "a block sunk into the tower"),
])
def test_what_a_mesh_draws_as_a_lowest_point_and_a_printer_never_meets_is_not_one(tmp_path: Path, others, why) -> None:
    said, _path = _read(tmp_path, *others)
    assert said is None, why


def test_the_second_look_reads_the_same_a_few_corners_at_a_time(tmp_path: Path, monkeypatch) -> None:
    """A cut can cross every triangle of a mesh, so a foot's corners are
    tried against it a few at a time.  Taken one pair at a time the answers
    are the ones above."""
    monkeypatch.setattr(printability, "_MOST_PAIRS_AT_ONCE", 1)
    assert _read(tmp_path, _second_body(0.3))[0] == "starts in mid-air at 0.3mm"
    for others in ([((128.0, 123.0, 0.3), (138.0, 133.0, 10.3))], [((113.0, 123.0, 18.0), (123.0, 133.0, 30.0))],
                   [((108.2, 118.2, 0.2), (127.8, 137.8, 19.8))]):
        assert _read(tmp_path, *others)[0] is None


def _many(piece: np.ndarray, side: int, pitch: float, at: float = 0.0) -> np.ndarray:
    """*piece* ``(F, 3, 3)`` repeated on a *side* x *side* grid, *pitch* apart."""
    grid = np.stack(np.meshgrid(np.arange(side), np.arange(side)), axis=-1).reshape(-1, 2) * pitch + at
    return (piece[None] + np.concatenate([grid, np.zeros((len(grid), 1))], axis=1)[:, None, None, :]).reshape(-1, 3, 3)


def test_a_mesh_of_tens_of_thousands_of_pieces_is_ranked_in_one_pass() -> None:
    """A mesh can hold as many pieces as it has faces; the ranking stays fast
    at that count, and the largest are still the ones looked at."""
    import time

    tris = _many(_corners(_block((0.0, 0.0, 0.3), (2.0, 2.0, 2.3))), 250, 3.0)

    began = time.perf_counter()
    faces, pieces, lowest = printability._starts_on_nothing(tris, 0.0, 0.2)
    took = time.perf_counter() - began

    assert pieces == printability._MOST_STARTS_PROBED and lowest == pytest.approx(0.3) and len(faces) == 2 * pieces
    assert took < 3.0, f"{took:.1f} s to rank 62,500 pieces"


def test_a_wide_cut_under_a_many_cornered_foot_is_never_held_whole() -> None:
    """One hanging disc with 512 corners to its foot, over a field of thin
    pillars the cut below it crosses 204,800 times: the check answers within
    its memory bound."""
    import tracemalloc

    pillars = _many(_corners(_block((0.0, 0.0, 0.0), (0.4, 0.4, 5.0))), 160, 1.0, at=20.0)
    disc = trimesh.creation.cylinder(radius=6.0, height=2.0, sections=512)
    disc.apply_translation((5.0, 5.0, 4.0))
    tris = np.concatenate([pillars, _corners(disc)])

    tracemalloc.start()
    try:
        _faces, pieces, lowest = printability._starts_on_nothing(tris, 0.0, 0.2)
        peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()

    assert pieces == 1 and lowest == pytest.approx(3.0)
    assert peak_mb < 400.0, f"{peak_mb:.0f} MB at once"


def test_a_part_that_needs_supports_anyway_keeps_its_own_reason(tmp_path: Path) -> None:
    """The second look is only for a part that reads as needing none."""
    said, _path = _read(tmp_path, _second_body(0.3), ((128.0, 118.0, 17.0), (158.0, 138.0, 20.0)))
    assert said is not None and "mid-air" not in said


def _slicers() -> dict[str, str]:
    import shutil

    found = {}
    for name, path in (("PrusaSlicer", "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"),
                       ("Bambu Studio", "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")):
        if Path(path).is_file():
            found[name] = path
    if "PrusaSlicer" not in found and shutil.which("prusa-slicer"):
        found["PrusaSlicer"] = shutil.which("prusa-slicer")
    return found


_SLICERS = _slicers()


@pytest.mark.skipif(not _SLICERS, reason="no slicer installed: public CI has none")
@pytest.mark.parametrize("slicer", list(_SLICERS) or ["none installed"])
def test_the_slice_holds_up_a_piece_that_starts_a_layer_over_the_plate(tmp_path: Path, monkeypatch, slicer: str):
    """Through the slicing step, with the real slicer: supports are on, the
    reply says why, and support is laid under the second body.  With the
    second look switched off the same part is sliced with none."""
    from kiln import monitor_twin
    from kiln.slicer import slice_file
    from kiln.slicer_profiles import resolve_slicer_profile
    from kiln.slicer_supports import gcode_support_lines

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    _said, path = _read(tmp_path, _second_body(0.3))

    def sliced(out: str):
        return slice_file(path, output_dir=str(tmp_path / out), profile=resolve_slicer_profile("bambu_a1"),
                          slicer_path=_SLICERS[slicer], timeout=600, extra_args=["--dont-arrange"] if slicer == "PrusaSlicer" else None)

    result = sliced("held")

    assert result.supports.enabled and result.supports.note == "supports on: starts in mid-air at 0.3mm"
    assert gcode_support_lines(result.output_path) > 0

    monkeypatch.setattr(printability, "_starts_on_nothing", lambda *_a, **_k: ([], 0, 0.0))
    before = sliced("as_it_was")
    assert not before.supports.enabled and gcode_support_lines(before.output_path) == 0

