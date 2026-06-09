"""A glued joint surfaces an adhesive recommendation from validate_assembly —
the cited recommendation inline for Pro+, an upgrade nudge for free tier. The
recommendation itself is computed + tier-gated in kiln-pro; public Kiln only
renders the result (or the nudge), so it works with or without the pro package.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from kiln.assembly import _adhesive_hint_for_joint, validate_joint


def _glued(mat_a: str = "PLA", mat_b: str = "PLA"):
    iface = SimpleNamespace(
        joint_type="glued",
        part_a_id="a",
        part_b_id="b",
        clearance_mm=0.1,
        fastener_spec=None,
    )
    parts = [
        SimpleNamespace(part_id="a", material=mat_a),
        SimpleNamespace(part_id="b", material=mat_b),
    ]
    return iface, parts


def test_glued_joint_free_tier_gets_upgrade_nudge():
    iface, parts = _glued()
    with patch("kiln.assembly._adhesive_hint_for_joint", return_value=None):
        jv = validate_joint(iface, parts)
    joined = " ".join(jv.recommendations)
    assert "recommend_adhesive" in joined
    assert "kiln3d.com/pricing" in joined
    assert "adhesive_recommendation" in jv.design_rules_checked


def test_glued_joint_pro_gets_cited_recommendation_inline():
    iface, parts = _glued("PLA", "ABS")
    rec = "Glued PLA↔ABS: Kiln Pro recommends Loctite HY 4090 — call recommend_adhesive..."
    with patch("kiln.assembly._adhesive_hint_for_joint", return_value=rec):
        jv = validate_joint(iface, parts)
    joined = " ".join(jv.recommendations)
    assert "Loctite HY 4090" in joined
    # Pro+ sees the recommendation, not the upgrade rope.
    assert "kiln3d.com/pricing" not in joined


def test_adhesive_hint_helper_degrades_silently_without_kiln_pro():
    # No kiln-pro installed → ImportError → None (never an exception).
    with patch.dict(sys.modules, {"kiln_pro.adhesives": None}):
        assert _adhesive_hint_for_joint("PLA", "PLA") is None
