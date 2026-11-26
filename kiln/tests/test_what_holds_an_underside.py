"""What holds a downward face up, read from the shape and not from how its
surface was cut into triangles.

Two rules, each with the case that keeps it honest:

* A ceiling that close above the part's own material (a hinge pin in its
  knuckle's bore, a print-in-place part over its neighbour) needs no
  supports, the same as one that close above the bed: what lies below
  catches any sag, and a support could not fit in the gap.  A pin a
  millimetre over its sleeve, or over open air, still needs them.
* A face going down from a sloped edge of an underside holds that edge,
  however the face was cut: the same wall cut two ways gets one answer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from kiln.printability import _analyze_downward_regions, analyze_printability  # noqa: E402

_ALONG_X = trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0])


def _rod(radius: float, length: float, x_centre: float, z: float) -> trimesh.Trimesh:
    return trimesh.creation.cylinder(radius=radius, height=length, sections=64).apply_transform(
        _ALONG_X,
    ).apply_translation([x_centre, 0, z])


def _box(extents, centre) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=extents).apply_translation(centre)


def _pin(tmp_path: Path, gap: float | None) -> Path:
    """A 4 mm pin running 16 mm between two posts, 2 mm over the bed; with
    *gap*, inside a sleeve whose bore stands *gap* mm clear of it all round,
    printed in place as a separate body."""
    held = trimesh.boolean.union(
        [_box([4, 8, 8], [2, 0, 4]), _box([4, 8, 8], [22, 0, 4]), _rod(2.0, 24, 12, 4.0)],
        engine="manifold",
    )
    parts = [held]
    if gap is not None:
        parts.append(_box([15.4, 8, 8], [12, 0, 4]).difference(_rod(2.0 + gap, 15.6, 12, 4.0), engine="manifold"))
    path = tmp_path / "pin.stl"
    trimesh.util.concatenate(parts).export(path)
    return path


def test_a_pin_close_over_its_sleeve_needs_no_supports(tmp_path: Path):
    report = analyze_printability(str(_pin(tmp_path, gap=0.3)), material="PLA")
    assert not report.bridging.needs_supports_for_bridges
    assert not report.overhangs.needs_supports


def test_a_pin_a_millimetre_over_its_sleeve_still_needs_supports(tmp_path: Path):
    report = analyze_printability(str(_pin(tmp_path, gap=1.0)), material="PLA")
    assert report.bridging.needs_supports_for_bridges


def test_a_pin_over_open_air_still_needs_supports(tmp_path: Path):
    report = analyze_printability(str(_pin(tmp_path, gap=None)), material="PLA")
    assert report.bridging.needs_supports_for_bridges


# A downward deck 6 mm across and 2 mm deep, rising 0.5 mm per mm of depth,
# 8 mm over the bed, between two vertical walls going down from its sloped
# side edges.
_DECK = [((0, 0, 8), (0, 2, 9), (6, 2, 9)), ((0, 0, 8), (6, 2, 9), (6, 0, 8))]
_RIGHT_WALL = [((6, 0, 8), (6, 2, 9), (6, 2, 0)), ((6, 0, 8), (6, 2, 0), (6, 0, 0))]
_LEFT_WALL = {
    "corner to corner": [((0, 0, 8), (0, 2, 0), (0, 2, 9)), ((0, 0, 8), (0, 0, 0), (0, 2, 0))],
    # The face on the edge keeps the edge's low corner as its lowest one.
    "fanned under the edge": [
        ((0, 0, 8), (0, 1.5, 8.6), (0, 2, 9)),
        ((0, 0, 8), (0, 0, 0), (0, 1.5, 8.6)),
        ((0, 1.5, 8.6), (0, 0, 0), (0, 2, 0)),
        ((0, 1.5, 8.6), (0, 2, 0), (0, 2, 9)),
    ],
}


@pytest.mark.parametrize("cut", sorted(_LEFT_WALL))
def test_a_sloped_edge_is_held_however_its_wall_is_cut(cut: str):
    regions = _analyze_downward_regions(_DECK + _LEFT_WALL[cut] + _RIGHT_WALL, 0.0, layer_height=0.2)
    deck = [r for r in regions if 0 in r.triangle_indices]
    assert len(deck) == 1
    assert not deck[0].needs_supports
    assert deck[0].span_mm == pytest.approx(6.0)
