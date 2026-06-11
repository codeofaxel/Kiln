"""Regression: the free load-bearing nudge must carry a SAFETY caveat,
not just an upsell.

A "this holds X" answer on a printed part, with only a marketing nudge
and no "calculation isn't a tested part / don't bet a life on it" line,
is the get-sued gap this locks.  The nudge is shared by assess_load_bearing,
get_joint_recommendation, validate_assembly, and estimate_structural_load,
so removing the caveat silently regresses all four.
"""
from kiln.load_bearing_detector import _build_upgrade_recommendation


def test_nudge_warns_to_physically_test_and_names_real_danger():
    warning = _build_upgrade_recommendation(["noun 'bracket'"])["warning"].lower()
    # The calculation is not a tested part — say so.
    assert "load-test" in warning or "test of your actual" in warning
    # Name the high-consequence uses, not a vague "consult a professional".
    assert "hurt someone" in warning or "engineer" in warning
    # The Pro funnel link survives the rewrite.
    assert "kiln3d.com" in warning
