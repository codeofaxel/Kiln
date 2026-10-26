"""A thin neck is one finding, where the neck is; a fix already made is
never the next step.

The thin-neck check slices a part along its height and judged every slice
alone, so one rod 20 mm tall read as 19 critical risks, each placed at the
middle of the whole part.  And after reinforcing, the summary named the
first thing to do about the worst risk left even when the reinforcement had
just done it: the rounded enclosure was told "First, round the inside
corners with fillets" about the corners rounding could not reach.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


@pytest.fixture()
def dumbbell(tmp_path) -> str:
    """Two 20 mm blocks joined by a 2 x 2 mm rod 20 mm long, its middle at (10, 10)."""
    parts = [_box((0, 0, 0), (20, 20, 10)), _box((9, 9, 10), (11, 11, 30)), _box((0, 0, 30), (20, 20, 40))]
    path = tmp_path / "dumbbell.stl"
    trimesh.util.concatenate(parts).export(path)
    return str(path)


def test_one_rod_is_one_thin_neck_placed_on_the_rod(dumbbell):
    from kiln.design_reasoning import analyze_structural_risks

    necks = [r for r in analyze_structural_risks(dumbbell) if r.risk_type == "thin_neck"]
    assert len(necks) == 1, [r.location_mm for r in necks]
    (neck,) = necks
    x, y, z = neck.location_mm
    assert abs(x - 10.0) < 0.5 and abs(y - 10.0) < 0.5  # the rod, not the middle of the box
    assert 18.0 <= z <= 22.0  # the middle of the rod
    assert neck.region_size_mm[2] >= 18.0  # the neck's own length, not one slice
    assert "and stays that thin from Z=" in neck.description


def _plan(*kinds: tuple[str, str]):
    risks = [SimpleNamespace(risk_type=kind, severity=severity) for kind, severity in kinds]
    return SimpleNamespace(
        risks=risks,
        critical_count=sum(1 for r in risks if r.severity == "critical"),
        warning_count=sum(1 for r in risks if r.severity == "warning"),
    )


def test_a_fix_already_made_is_not_the_next_step():
    from kiln.design_reasoning import describe_structural_change

    before = _plan(*[("sharp_corner", "warning")] * 27)
    after = _plan(*[("sharp_corner", "warning")] * 11)
    rounded = {
        "type": "fillet", "addresses": "sharp_corner",
        "edges": {"left_sharp": [{"corner": "inside", "reason": "it is the floor of a 4 mm hole"}] * 4},
    }
    said = describe_structural_change(before, after, [rounded])
    assert "First, round" not in said, said
    assert "which the reinforcement did not reach" in said and "It left 4 edges sharp on purpose." in said

    # A kind no edit addressed still gets its next step.
    after_too = _plan(*[("sharp_corner", "warning")] * 11, ("thin_neck", "critical"))
    said = describe_structural_change(before, after_too, [rounded])
    assert "First, " in said and "First, round" not in said, said


def test_an_edge_the_kernel_could_not_round_is_not_called_on_purpose():
    from kiln.design_reasoning import describe_structural_change

    before, after = _plan(("sharp_corner", "warning")), _plan(("sharp_corner", "warning"))
    failed = {
        "type": "fillet", "addresses": "sharp_corner",
        "edges": {"left_sharp": [{"corner": "inside", "reason": "the CAD kernel could not build it: x", "failed": True}]},
    }
    said = describe_structural_change(before, after, [failed])
    assert "on purpose" not in said and "The CAD kernel could not round 1 edge." in said, said
