"""The structural risks are the answer, at every door; the score is not said.

The structural analysis scores a part 0-100 with a grade, and every door
around it said that number as the answer: "No structural risks detected.
Score: 100/100 (A).", "Score improved 45 → 100 (F → A).", "Structural grade
F — using high infill", and the design audit's gate "Structural score 0/100
(F)", which it also passed or failed on (a bar of 60), so a pegboard with
six pegs failed it with no critical risk at all.  A person told "Score:
62/100 (D)" learns nothing to act on.

Since 2026-10-23 a sentence names the risks found, the worst first, and the
first thing to do (``kiln.design_reasoning.describe_structural_risks``, one
wording for every door); the score and grade stay in the report for
comparing versions of a part.  The audit's gate fails on a critical risk and
passes warnings as warnings, the way the printability gate reads its verdict.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402

#: A score or grade said in a sentence, in the shapes the doors used.
_SCORE_SAID = re.compile(
    r"\d{1,3}\s*/\s*100"                    # "Score: 62/100", "Structural score 0/100"
    r"|\([A-F][+-]?\)|\b[A-F]\s*→\s*[A-F]\b"  # "(D)", "(F → A)"
    r"|\b[Gg]rade:?\s+[A-F]\b"              # "Structural grade: D", "grade F"
    r"|\b[Ss]core\s+(?:improved|unchanged)\b"
)


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _union(*parts):
    pytest.importorskip("manifold3d", reason="the part is joined with a boolean union")
    return trimesh.boolean.union(list(parts), engine="manifold")


@pytest.fixture()
def pillar(tmp_path) -> str:
    """A 5 mm square post 60 mm tall on a 7 mm foot, with a 7 mm collar near
    its top: a critical tipping risk, and sharp inside corners where the post
    meets the foot and the collar.  (A bare post has no inside corner since
    2026-10-23, and these tests need warnings beside the critical risk.)"""
    path = tmp_path / "pillar.stl"
    _union(
        _box((-3.5, -3.5, 0), (3.5, 3.5, 2)), _box((-2.5, -2.5, 0), (2.5, 2.5, 60)),
        _box((-3.5, -3.5, 40), (3.5, 3.5, 42)),
    ).export(path)
    return str(path)


@pytest.fixture()
def pegboard(tmp_path) -> str:
    """A plate with six pegs joined to it: warnings at every peg, no critical
    risk.  (Joined since 2026-10-23: pegs merely set on the plate share no edge
    with it, so their roots are not seen as inside corners.)"""
    pegs = [_box((x, y, 3), (x + 4, y + 4, 15)) for x in (10, 40, 70) for y in (10, 70)]
    path = tmp_path / "pegboard.stl"
    _union(_box((0, 0, 0), (100, 100, 3)), *pegs).export(path)
    return str(path)


@pytest.fixture()
def neck(tmp_path) -> str:
    """Two 20 mm blocks joined by a 1.2 mm neck 20 mm long: a critical narrow
    neck, the step into it and inside corners beside it, enough different
    risks to grade D.  (The pillar graded D until 2026-10-23, when a kind of
    risk began to cost the score once however often it repeats.)"""
    path = tmp_path / "neck.stl"
    _union(
        _box((-10, -10, 0), (10, 10, 10)), _box((-0.6, -0.6, 10), (0.6, 0.6, 30)),
        _box((-10, -10, 30), (10, 10, 40)),
    ).export(path)
    return str(path)


@pytest.fixture()
def knob(tmp_path) -> str:
    """Autodesk Fusion's knob, resting on the bed: no structural risk."""
    return str(rx.stage("knob_on_bed_mm.stl", tmp_path / "knob"))


@pytest.fixture()
def donut(tmp_path) -> str:
    """Blender's donut, in millimetres: 8 pieces, one critical risk."""
    return str(rx.stage("donut_mm.stl", tmp_path / "donut"))


def _call(name: str, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _needs_openscad() -> None:
    try:
        from kiln.generation.openscad import _find_openscad

        _find_openscad()
    except Exception:  # noqa: BLE001 -- any failure to find it means it cannot run here
        pytest.skip("OpenSCAD is not installed")


@pytest.mark.parametrize("part", ["donut", "pillar", "pegboard", "neck", "knob"])
def test_no_structural_door_says_the_score(part, request):
    path = request.getfixturevalue(part)
    replies = {
        "design_improvement_plan": _call("design_improvement_plan", file_path=path),
        "infer_print_settings": _call("infer_print_settings", file_path=path),
        "audit_original_design": _call("audit_original_design", file_path=path, requirements="holds a pen"),
    }
    for door, reply in replies.items():
        assert reply.get("success"), (door, reply)
        for where, text in rx._sentences(reply):
            assert not _SCORE_SAID.search(text), f"{door} {where}: {text}"
    # The score and grade stay in the report, for comparing versions.
    plan = replies["design_improvement_plan"]
    assert isinstance(plan["overall_structural_score"], int) and plan["structural_grade"] in "ABCDF"
    assert plan["summary"]


def test_the_plan_names_the_risks_worst_first_and_what_to_do(pillar, knob):
    said = _call("design_improvement_plan", file_path=pillar)["summary"]
    assert said.startswith("1 critical structural risk and "), said
    assert said.index("a tipping risk (critical)") < said.index("2 sharp inside corners"), said
    assert "First, widen the base or add a brim." in said, said
    assert _call("design_improvement_plan", file_path=knob)["summary"] == "No structural risks found."


def test_the_settings_note_names_what_it_compensates_for(neck):
    # Read on the neck, not the pillar: the pillar's two kinds of risk no longer
    # grade D since a kind costs the score once (2026-10-23).
    notes = _call("infer_print_settings", file_path=neck)["special_notes"]
    said = re.compile(r"With \d+ critical structural risks? and \d+ warnings?, using high infill")
    assert any(said.match(n) for n in notes), notes


def test_reinforcing_says_what_the_part_had_and_what_is_left(pillar, tmp_path):
    _needs_openscad()
    from kiln.design_reasoning import generate_improvement_plan

    out = tmp_path / "pillar_reinforced.stl"
    reply = _call("apply_design_reinforcements", file_path=pillar, output_path=str(out))
    assert reply.get("success"), reply
    said = reply["summary"]
    assert re.search(r"[Ii]t had 1 critical structural risk and \d+ warnings?; (it now has|none remain)", said), said
    after = generate_improvement_plan(str(out))
    worse = (after.critical_count, after.warning_count) > (1, generate_improvement_plan(pillar).warning_count)
    assert ("more structural risk than the original" in said) == worse, said
    assert isinstance(reply["before_score"], int) and isinstance(reply["after_score"], int)


def test_the_audit_gate_fails_on_a_critical_risk_not_on_a_score(pillar, pegboard):
    from kiln.original_design import audit_original_design

    warned = audit_original_design(pegboard, "a pegboard")
    gate = next(g for g in warned.gates if g.name == "structural_integrity")
    assert gate.passed is True and gate.severity == "warning", gate
    assert re.match(r"\d+ structural warnings: ", gate.message), gate.message
    # No critical risk; the score stays in the details for comparing versions.
    assert gate.details["critical_count"] == 0 and gate.details["warning_count"] >= 5
    # Was "< 60", the bar the gate used to fail on: since 2026-10-23 a kind of
    # risk costs the score once, so six peg roots no longer sink it under it.
    assert isinstance(gate.details["overall_structural_score"], int)

    blocked = audit_original_design(pillar, "a pillar")
    gate = next(g for g in blocked.gates if g.name == "structural_integrity")
    assert gate.passed is False and gate.severity == "critical", gate
    assert gate.message.startswith("1 critical structural risk"), gate.message
    assert gate.message in blocked.blockers and not blocked.ready_for_print


def test_the_template_sweep_names_the_strongest_variants_risks():
    _needs_openscad()
    reply = _call("optimize_template_params", template_id="hook", samples_per_param=1)
    assert reply.get("success"), reply
    said = reply["summary"]
    assert "best_params is the strongest." in said and not _SCORE_SAID.search(said), said
    assert isinstance(reply["best_score"], int) and reply["best_grade"] in "ABCDF"


def test_the_template_pipeline_says_the_check_not_the_grade():
    _needs_openscad()
    reply = _call("smart_generate_from_template", template_id="hook")
    assert reply.get("success"), reply
    assert "Structural check: " in reply["next_steps"], reply["next_steps"]
    said = [text for _where, text in rx._sentences(reply)] + [reply["structural_analysis"]["summary"]]
    assert not [s for s in said if _SCORE_SAID.search(s)], said
    assert reply["structural_analysis"]["grade"] in "ABCDF"
