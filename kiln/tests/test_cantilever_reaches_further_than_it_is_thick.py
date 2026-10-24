"""A cantilever reaches further than it is thick where it leaves its support.

A member reaching out sideways with nothing under it is read as a cantilever
when it reaches at least 5 mm and at least as far as it is thick at its root
(``test_cantilevers_reach_out_sideways`` holds the first half).  A boss or a
bracket thicker than it is long is neither a lip nor an arm: a load on its
end cannot bend it, and until 2026-10-24 it was warned about, aimed a gusset
at, and cost the part points, the same as a shelf arm.

The parts are joined the way a real part is (a boolean union) and read
through the tool a person calls, as a plain install answers it.
"""

from __future__ import annotations

import re

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests.test_sharp_corners_are_inside_corners import _box, _call, _save, _union  # noqa: E402

#: A wall 10 mm thick, 40 wide, 40 tall; everything below hangs off its face.
WALL = ((0, 0, 0), (10, 40, 40))


def _boss():
    """A 20 x 20 mm boss standing 8 mm out of the wall: thicker than it is long."""
    return _union(_box(*WALL), _box((10, 10, 10), (18, 30, 30)))


def _stub_arm():
    """A 10 mm thick bar reaching 9 mm: still a bracket, not a cantilever."""
    return _union(_box(*WALL), _box((10, 15, 20), (19, 25, 30)))


def _arm():
    """A 4 mm thick arm reaching 30 mm: seven and a half times its thickness."""
    return _union(_box(*WALL), _box((10, 15, 30), (40, 25, 34)))


def _square_arm():
    """A 10 mm square bar reaching 20 mm: twice its thickness, a cantilever."""
    return _union(_box(*WALL), _box((10, 15, 20), (30, 25, 30)))


def _cantilevers(path):
    reply = _call("analyze_structural_risks", file_path=path)
    assert reply.get("success"), reply
    return [r for r in reply["risks"] if r["risk_type"] == "cantilever"]


@pytest.mark.parametrize("build", [_boss, _stub_arm], ids=["boss", "stub_arm"])
def test_a_member_thicker_than_it_is_long_is_not_a_cantilever(build, tmp_path):
    path = _save(build(), tmp_path, "part")
    assert _cantilevers(path) == []
    assert "cantilever" not in _call("design_improvement_plan", file_path=path)["summary"]
    recs = _call("recommend_design_reinforcements", file_path=path)["reinforcements"]
    assert not [r for r in recs if r["addresses_risk"] == "cantilever"]


@pytest.mark.parametrize(("build", "reach", "thick"), [(_arm, 30.0, 4.0), (_square_arm, 20.0, 10.0)],
                         ids=["thin_arm", "square_arm"])
def test_a_member_longer_than_it_is_thick_is_a_cantilever_and_says_the_ratio(build, reach, thick, tmp_path):
    (found,) = _cantilevers(_save(build(), tmp_path, "arm"))
    assert found["severity"] == "warning"
    assert found["metric_value"] == pytest.approx(reach, abs=1.0)
    assert found["region_size_mm"][2] == pytest.approx(thick, abs=0.3)
    said = re.search(r"\(([\d.]+) times its thickness\)", found["description"])
    assert said, found["description"]
    assert float(said.group(1)) == pytest.approx(found["metric_value"] / thick, abs=0.3)


def test_the_boss_and_the_arm_on_one_wall_give_one_cantilever(tmp_path):
    """The arm is found, the boss beside it is not: the rule is read per member."""
    part = _union(_box(*WALL), _box((10, 2, 10), (18, 18, 30)), _box((10, 25, 30), (40, 35, 34)))
    (found,) = _cantilevers(_save(part, tmp_path, "both"))
    assert found["location_mm"][1] == pytest.approx(30.0, abs=0.6)  # the arm's root, not the boss's
    assert found["metric_value"] == pytest.approx(30.0, abs=1.0)
