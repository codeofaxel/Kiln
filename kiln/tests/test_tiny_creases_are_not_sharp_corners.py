"""An inside corner shorter than a printed line is not a sharp corner.

Where two rounds of different size meet, the CAD leaves a crease a fraction
of a millimetre long with a steep turn across it.  A printed corner is never
sharper than the bead that forms it, so a crease that short is not in the
print, and until 2026-10-24 the structural analysis counted each one as a
sharp inside corner to round: reinforcing Autodesk Fusion's enclosure left
seven of them, under 0.35 mm each, beside the four screw-hole floors it leaves
sharp on purpose, and told the person it had eleven corners left.

The synthetic parts are joined the way a real part is (a boolean union); the
enclosure is reinforced through the tool a person calls, as a plain install
answers it.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests import _real_exports as rx  # noqa: E402
from tests.test_sharp_corners_are_inside_corners import _box, _call, _save, _union  # noqa: E402

PLATE = ((0, 0, 0), (20, 20, 3))


def _bump(side_mm):
    """A cube ``side_mm`` across standing on the plate: four inside edges at
    its foot, ``4 * side_mm`` of edge in all."""
    return _union(_box(*PLATE), _box((10, 10, 3), (10 + side_mm, 10 + side_mm, 3 + side_mm)))


def _corners(path):
    reply = _call("analyze_structural_risks", file_path=path)
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "sharp_corner"]


def test_a_crease_shorter_than_a_printed_line_is_not_counted(tmp_path):
    assert _corners(_save(_bump(0.08), tmp_path, "crease")) == []  # 0.32 mm of edge


def test_a_corner_a_printed_line_long_still_is(tmp_path):
    (corner,) = _corners(_save(_bump(0.2), tmp_path, "small"))  # 0.8 mm of edge
    assert "0.8 mm of edge" in corner["description"]
    (big,) = _corners(_save(_bump(2.0), tmp_path, "big"))
    assert "8.0 mm of edge" in big["description"]


def test_reinforcing_the_enclosure_leaves_only_the_hole_floors_it_kept_sharp(tmp_path, monkeypatch):
    """Autodesk Fusion's enclosure, rounded through the tool: what is left is
    the floor of each of its four screw holes, 12.6 mm of edge each, and the
    creases where its rounds meet are not corners."""
    from kiln.step_import import _ocp_available

    if not _ocp_available():
        pytest.skip("the CAD kernel is not installed; the enclosure is a STEP file")
    from tests.test_reinforcement_rounds_inside_corners import _reinforce

    step = rx.stage("enclosure_v1.step", tmp_path / "enclosure")
    reply = _reinforce(step, tmp_path, monkeypatch)
    left = _corners(reply["output_path"])
    assert len(left) == 4, [c["description"] for c in left]
    for corner in left:
        assert corner["location_mm"][2] == pytest.approx(23.0, abs=0.05)  # the hole floors
        assert "12.6 mm of edge" in corner["description"]
    assert "4 sharp inside corners, which the reinforcement did not reach" in reply["summary"], reply["summary"]
