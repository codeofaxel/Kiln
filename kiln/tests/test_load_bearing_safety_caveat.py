"""Regression: the free load-bearing nudge must carry a SAFETY caveat,
not just an upsell.

A "this holds X" answer on a printed part, with only a marketing nudge
and no "calculation isn't a tested part / don't bet a life on it" line,
is the get-sued gap this locks.  The nudge is shared by assess_load_bearing,
get_joint_recommendation, validate_assembly, and estimate_structural_load,
so removing the caveat silently regresses all four.
"""
import pytest

from kiln.load_bearing_detector import _build_upgrade_recommendation
from tests._plan_callers import free_caller  # noqa: F401 -- a fixture


@pytest.mark.usefixtures("free_caller")
def test_nudge_warns_to_physically_test_and_names_real_danger():
    warning = _build_upgrade_recommendation(["noun 'bracket'"])["warning"].lower()
    # The calculation is not a tested part — say so.
    assert "load-test" in warning or "test of your actual" in warning
    # Name the high-consequence uses, not a vague "consult a professional".
    assert "hurt someone" in warning or "engineer" in warning
    # The Pro funnel link survives the rewrite.
    assert "kiln3d.com" in warning


@pytest.mark.parametrize("paid", [False, True])
def test_a_load_left_on_it_is_said_at_every_plan(monkeypatch, paid):
    """Printed plastic under a load left on it slowly bends, PLA most of all.
    That is a safety fact: it is said beside the caution to every caller,
    never only inside the offer of the plan that works it out."""
    import kiln.licensing as licensing
    from kiln.load_bearing_detector import attach_load_bearing_nudge

    monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (paid, None if paid else "needs Kiln Pro"))
    note = attach_load_bearing_nudge({"success": True}, force=True)["load_bearing_note"]
    assert "load left on it" in note and "PLA shelf is not safe long-term" in note, note
