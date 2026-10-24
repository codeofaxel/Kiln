"""A kind of structural risk costs a part's scores once, however often it repeats.

The design audit's readiness score and the improvement plan's structural score
are for comparing versions of a part.  Until 2026-10-23 both charged for every
structural risk the analysis listed, so a part was charged for repetition, not
for what is wrong with it: a sound pegboard with six identical peg roots read
far below the audit's bar and was never ready for print, an Autodesk Fusion
enclosure with 27 inside corners scored 0, and one narrow neck, read at every
height the analysis samples, counted once per height.

Now each kind of risk costs once, at its worst severity.  Different kinds still
add up, so a part that gains a new kind of risk never scores higher, and a
kind that turns critical costs more than it did as a warning.  The risks
themselves are all still listed, counted and named.

Every part here is read through the tool a person calls, as a plain install
answers it.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d", reason="the parts are joined with a boolean union")

from tests import _real_exports as rx  # noqa: E402

_REQUIREMENTS = "a sturdy desk part"


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _union(*parts):
    return trimesh.boolean.union(list(parts), engine="manifold")


#: Square pegs, 6 mm across and 15 mm tall, at these centres on a
#: 100 x 100 x 3 plate.
PEGS = [(x, y) for x in (15, 45, 75) for y in (20, 80)]


def _pegboard(pegs, *extra):
    plate = _box((0, 0, 0), (100, 100, 3))
    return _union(plate, *[_box((x - 3, y - 3, 3), (x + 3, y + 3, 18)) for x, y in pegs], *extra)


def _dumbbell(neck_mm, width_mm):
    """Two 20 mm blocks joined by a square neck ``width_mm`` across and
    ``neck_mm`` long: 1.6 mm across is a warning, 1.2 mm a critical risk."""
    h = width_mm / 2.0
    return _union(
        _box((-10, -10, 0), (10, 10, 10)),
        _box((-h, -h, 10), (h, h, 10 + neck_mm)),
        _box((-10, -10, 10 + neck_mm), (10, 10, 20 + neck_mm)),
    )


def _save(mesh, tmp_path, name):
    path = tmp_path / f"{name}.stl"
    mesh.export(path)
    return str(path)


def _call(name, **args):
    from kiln import server

    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, name, args)
    assert reply.get("success"), reply
    return reply


def _audit(path):
    return _call("audit_original_design", file_path=path, requirements=_REQUIREMENTS)


def _plan(path):
    return _call("design_improvement_plan", file_path=path)


def _kinds(plan):
    """Each kind of counted risk with its worst severity, and the count of each."""
    worst, counts = {}, {}
    for r in plan["risks"]:
        if r["severity"] in ("critical", "warning"):
            kind = r["risk_type"]
            counts[kind] = counts.get(kind, 0) + 1
            if worst.get(kind) != "critical":
                worst[kind] = r["severity"]
    return worst, counts


@pytest.mark.parametrize(
    ("fewer", "more"),
    [
        (lambda: _pegboard(PEGS[:2]), lambda: _pegboard(PEGS[:3])),
    ],
    ids=["a_third_peg_root"],
)
def test_more_of_the_same_risk_costs_nothing_more(fewer, more, tmp_path):
    """A third identical peg root is not a new problem: the part scores what
    the part with fewer does."""
    a, b = _save(fewer(), tmp_path, "fewer"), _save(more(), tmp_path, "more")
    plan_a, plan_b = _plan(a), _plan(b)
    (kinds_a, counts_a), (kinds_b, counts_b) = _kinds(plan_a), _kinds(plan_b)
    # The pair differs only in how often the same kinds repeat.
    assert kinds_a == kinds_b and sum(counts_b.values()) > sum(counts_a.values()), (counts_a, counts_b)
    assert plan_b["overall_structural_score"] == plan_a["overall_structural_score"], (counts_a, counts_b)

    audit_a, audit_b = _audit(a), _audit(b)
    # The readiness score starts from printability, which the pair shares.
    assert audit_a["printability"]["score"] == audit_b["printability"]["score"]
    assert audit_b["readiness_score"] == audit_a["readiness_score"], (counts_a, counts_b)


@pytest.mark.parametrize(
    ("short", "long"),
    [((4, 1.6), (20, 1.6)), ((2, 1.2), (4, 1.2))],
    ids=["a_longer_neck", "a_longer_critical_neck"],
)
def test_a_longer_neck_is_one_neck_and_costs_the_same(short, long, tmp_path):
    """A neck is one finding however long it is, warning or critical, and the
    longer one scores what the shorter does."""
    a, b = _save(_dumbbell(*short), tmp_path, "short"), _save(_dumbbell(*long), tmp_path, "long")
    plan_a, plan_b = _plan(a), _plan(b)
    (kinds_a, counts_a), (kinds_b, counts_b) = _kinds(plan_a), _kinds(plan_b)
    assert counts_a.get("thin_neck") == counts_b.get("thin_neck") == 1, (counts_a, counts_b)
    assert kinds_a == kinds_b, (kinds_a, kinds_b)
    assert plan_b["overall_structural_score"] == plan_a["overall_structural_score"], (counts_a, counts_b)
    assert _audit(b)["readiness_score"] == _audit(a)["readiness_score"], (counts_a, counts_b)


def test_a_pegboard_with_six_peg_roots_is_ready_for_print(tmp_path):
    path = _save(_pegboard(PEGS), tmp_path, "pegboard")
    kinds, counts = _kinds(_plan(path))
    assert counts.get("sharp_corner") == 6 and "critical" not in kinds.values(), counts

    audit = _audit(path)
    assert audit["ready_for_print"] is True, (audit["readiness_score"], counts)
    assert audit["message"].endswith("The design passes the audit."), audit["message"]
    # Every peg root is still named: the score stopped counting repeats, the
    # report did not.
    gate = next(g for g in audit["gates"] if g["name"] == "structural_integrity")
    assert gate["passed"] is True and gate["details"]["warning_count"] == sum(counts.values()), gate
    assert "6 sharp inside corners" in gate["message"], gate["message"]


def test_a_real_fusion_enclosure_is_ready_for_print(tmp_path):
    """Autodesk Fusion's enclosure: a sound box whose many inside corners are
    one thing to round, and no reason not to print it."""
    from kiln.step_import import _ocp_available

    if not _ocp_available():
        pytest.skip("the CAD kernel is not installed; the enclosure is a STEP file")
    path = str(rx.stage("enclosure_v1.step", tmp_path / "enclosure"))
    kinds, counts = _kinds(_plan(path))
    assert kinds == {"sharp_corner": "warning"} and counts["sharp_corner"] > 1, counts

    audit = _audit(path)
    assert audit["ready_for_print"] is True, (audit["readiness_score"], counts)
    gate = next(g for g in audit["gates"] if g["name"] == "structural_integrity")
    assert gate["passed"] is True and gate["severity"] == "warning", gate


def test_a_new_kind_of_risk_always_costs(tmp_path):
    """Different kinds add up: a slender post set among the pegs brings a kind
    of risk the pegboard did not have, and the part scores lower for it; and a
    neck narrow enough to be critical scores lower than one that is a warning."""
    board = _save(_pegboard(PEGS), tmp_path, "pegboard")
    with_post = _save(_pegboard(PEGS, _box((49, 48, 3), (51, 52, 60))), tmp_path, "with_post")
    plan_board, plan_post = _plan(board), _plan(with_post)
    kinds_board, _ = _kinds(plan_board)
    kinds_post, _ = _kinds(plan_post)
    assert set(kinds_board) < set(kinds_post), (kinds_board, kinds_post)
    assert plan_post["overall_structural_score"] < plan_board["overall_structural_score"]
    assert _audit(with_post)["readiness_score"] < _audit(board)["readiness_score"]

    warned = _plan(_save(_dumbbell(4, 1.6), tmp_path, "warned_neck"))
    critical = _plan(_save(_dumbbell(4, 1.2), tmp_path, "critical_neck"))
    assert _kinds(warned)[0]["thin_neck"] == "warning" and _kinds(critical)[0]["thin_neck"] == "critical"
    assert critical["overall_structural_score"] < warned["overall_structural_score"]
