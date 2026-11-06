"""Supports are never kept off a piece that starts in mid-air.

A slice that carries supports keeps them off the undersides the verdict
measured as printing on their own, so a screw thread beside a shelf is not
packed with support.  Measured 2026-11-06 on a real 200 mm figure, that rule
alone went too far: the figure stands on its toes, its heels hang 0.3 mm over
the plate, and with supports kept off them the heels' first outlines were laid
3 to 9 mm from anything below.  The verdict reads a part as resting on the
plate through a band two layers deep; a slicer cuts the first layer at half
its height, and what that cut misses starts a layer up with nothing under it.

So an underside that holds a lowest point the first layer does not reach is
left to the slicer whole.  A thread's flank, a lip and a socket's ceiling hold
none, and are kept clear as before (``test_supports_leave_threads_alone.py``).
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
