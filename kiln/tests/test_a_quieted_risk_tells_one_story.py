"""A structural risk quieted to a note tells one story in the whole reply.

A risk past the public threshold and short of the tuned one stays in the
reply as a borderline note.  The summary above it, the list it sits in and
the reinforcement that answers it must agree: the summary names the note and
never says nothing was found, and the reinforcement is optional, never a
high-priority fix for a part the summary says has no warning.

The tuning is stood in for with a number no real setting has, read under the
name the structural check reads it by.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")

TIPPING = 5.37  # stand-in for the tuned tipping ratio (public: 4.0)


@pytest.fixture()
def tuned(monkeypatch) -> None:
    import kiln.design_intelligence as di

    monkeypatch.setattr(
        di, "load_pro_overlay_or_empty",
        lambda kind: {"tipping_ratio": TIPPING} if kind == "structural_thresholds" else {},
    )


@pytest.fixture()
def tall_post(tmp_path) -> str:
    """A 10 x 10 mm post 45 mm tall: past the public tipping ratio, short of
    the stand-in."""
    post = trimesh.creation.box(extents=(10.0, 10.0, 45.0))
    post.apply_translation((0.0, 0.0, 22.5))
    path = tmp_path / "tall_post.stl"
    post.export(path)
    return str(path)


@pytest.fixture()
def squat_block(tmp_path) -> str:
    """A 40 x 40 x 10 mm block: nothing for the structural check to find."""
    block = trimesh.creation.box(extents=(40.0, 40.0, 10.0))
    block.apply_translation((0.0, 0.0, 5.0))
    path = tmp_path / "block.stl"
    block.export(path)
    return str(path)


def _call(name: str, **args):
    from kiln import server
    from tests import _real_exports as rx

    return rx.call(server.mcp, name, args)


@pytest.mark.usefixtures("tuned")
def test_the_plan_names_the_note_and_lists_its_fix_as_optional(tall_post):
    plan = _call("design_improvement_plan", file_path=tall_post)
    assert plan.get("success"), plan
    assert [(r["risk_type"], r["severity"]) for r in plan["risks"]] == [("insufficient_base", "info")]
    said = plan["summary"]
    assert "No structural risks found" not in said, said
    assert said.startswith("No structural warnings, 1 borderline feature noted: a tipping risk."), said
    assert "no load was checked" in said, said
    assert said.endswith("1 optional reinforcement listed for the borderline feature."), said
    assert [(r["addresses_risk"], r["priority"]) for r in plan["reinforcements"]] == [("insufficient_base", "low")]
    assert (plan["critical_count"], plan["warning_count"]) == (0, 0)


@pytest.mark.usefixtures("tuned")
def test_the_risk_door_says_the_same_sentence(tall_post):
    reply = _call("analyze_structural_risks", file_path=tall_post)
    assert reply["risk_count"] == 1 and reply["warning_count"] == 0, reply
    assert reply["summary"].startswith("No structural warnings, 1 borderline feature noted: a tipping risk."), reply


def test_the_same_post_untuned_is_a_warning_with_a_recommended_fix(tall_post):
    """The twin: at the public threshold the risk is a warning, and its fix
    keeps its priority."""
    from tests import _real_exports as rx

    with rx.without_kiln_pro():
        plan = _call("design_improvement_plan", file_path=tall_post)
    assert plan["summary"].startswith("1 structural warning: a tipping risk."), plan["summary"]
    assert plan["summary"].endswith("1 reinforcement recommended."), plan["summary"]
    assert [r["priority"] for r in plan["reinforcements"]] == ["high"]


def test_a_part_with_nothing_found_is_still_told_so(squat_block):
    from tests import _real_exports as rx

    with rx.without_kiln_pro():
        plan = _call("design_improvement_plan", file_path=squat_block)
    assert plan["risks"] == [] and plan["reinforcements"] == [], plan
    assert plan["summary"].startswith("No structural risks found."), plan["summary"]


def test_the_change_sentence_never_says_nothing_was_there():
    """Reinforcing a part whose only finding was a note says what it had."""
    from kiln import design_reasoning as dr

    note = dr.StructuralRisk(
        risk_type="insufficient_base", severity="info", location_mm=(0.0, 0.0, 0.0),
        region_size_mm=(10.0, 10.0, 9.0), description="", metric_name="height_to_base_ratio",
        metric_value=4.5, metric_threshold=4.0,
    )
    before = dr.DesignImprovementPlan(file_path="a.stl", risks=[note])
    after = dr.DesignImprovementPlan(file_path="b.stl", risks=[])
    said = dr.describe_structural_change(before, after)
    assert said.startswith("It had no structural warnings, 1 borderline feature noted; it now has no structural risks."), said
    assert dr.describe_structural_change(after, after).startswith("No structural risks before or after."), said
