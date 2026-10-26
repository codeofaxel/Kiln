"""The structural analysis reads a file's units the way every other door does.

Blender exports in metres, so its donut reads 0.088 mm across.  Every
printability, validation and mesh door answers that file with the units
question (``kiln.generation.validation.unit_verdict``) and presents no other
measurement as the part's.  The structural analysis did not: it judged the
0.088 mm file against risk sizes meant for real parts, so the same donut gave
1 critical risk and 0 warnings in metres and 1 critical and 20 warnings in
millimetres, and every door around it (the plan, the risk list, the
reinforcements, the slicer settings, the design audit, the generation
feedback) said those counts as the part's.

Since 2026-10-23 the analysis reads every file through one entry
(``kiln.design_reasoning._read_part``): a file smaller than anything a
printer makes is answered with the units question, nothing is rescaled, and
no risk is judged, counted, scored or acted on until its size is settled.
Each test below calls the real tool and checks the metres file and the same
object in millimetres side by side.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("trimesh", reason="the corpus files are read with trimesh")

from tests import _real_exports as rx  # noqa: E402

METRES = ("donut_m.stl", "donut.glb")
MM = "donut_mm.stl"

#: Every tool that reaches the structural analysis on a file, with the
#: inputs it needs besides the file.  The first five say risk counts; the
#: rest say what the analysis found in their own words, each tested below.
DOORS = {
    "analyze_structural_risks": {},
    "recommend_design_reinforcements": {},
    "assess_load_bearing": {},
    "design_improvement_plan": {},
    "infer_print_settings": {},
    "apply_design_reinforcements": {},
    "audit_original_design": {"requirements": "holds a donut"},
    "analyze_generation_feedback": {"original_prompt": "a donut"},
    "improve_generation_prompt": {"original_prompt": "a donut"},
}


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _door(name: str, rel: str, tmp_path: Path) -> tuple[dict, Path]:
    staged = rx.stage(rel, tmp_path / Path(rel).stem / "in")
    out = tmp_path / Path(rel).stem / "out"
    out.mkdir(parents=True, exist_ok=True)
    args = {"file_path": str(staged), **DOORS[name]}
    if name == "apply_design_reinforcements":
        args["output_path"] = str(out / "reinforced.stl")
    return _call(name, **args), staged


def _sentences(reply: dict) -> list[str]:
    return [text for _where, text in rx._sentences(reply)]


#: The doors whose reply counts risks or reinforcements.
COUNTING = ("analyze_structural_risks", "recommend_design_reinforcements", "design_improvement_plan",
            "apply_design_reinforcements", "audit_original_design")


@pytest.mark.parametrize("rel", METRES)
@pytest.mark.parametrize("door", COUNTING)
def test_a_metres_file_is_answered_with_its_units_question_and_no_count(door, rel, tmp_path):
    reply, _staged = _door(door, rel, tmp_path)
    assert reply.get("success"), reply
    assert rx.asks_units(reply) and "0.088mm" in rx.all_text(reply), reply
    claims = rx.read_claims(reply)
    assert claims.structural == [], f"{door}({rel}) counts structural risks it did not judge: {claims.structural}"
    for text in _sentences(reply):
        assert not rx._SAYS_STRUCTURAL_COUNTS.search(text), f"{door}({rel}): {text}"
    assert not reply.get("risks") and not reply.get("reinforcements"), reply

    # The same object in millimetres is counted as before.
    judged, _ = _door(door, MM, tmp_path)
    assert judged.get("success") and not rx.asks_units(judged), judged
    assert rx.read_claims(judged).structural or any(
        rx._SAYS_STRUCTURAL_COUNTS.search(t) for t in _sentences(judged)), judged


@pytest.mark.parametrize("rel", METRES)
def test_the_plan_is_the_units_question_and_nothing_counted(rel, tmp_path):
    from kiln.design_reasoning import analyze_structural_risks, generate_improvement_plan, recommend_reinforcements
    from kiln.generation.validation import unit_verdict

    staged = str(rx.stage(rel, tmp_path / "in"))
    assert analyze_structural_risks(staged) == [] and recommend_reinforcements(staged) == []
    plan = generate_improvement_plan(staged)
    assert plan.risks == [] and plan.reinforcements == [] and plan.load_analysis is None
    assert (plan.critical_count, plan.warning_count, plan.overall_structural_score) == (None, None, None)
    # The question is the summary, in the words of the one judgement every door uses.
    assert plan.summary.startswith(unit_verdict(0.088).describe_unchanged()[:40]), plan.summary
    assert plan.summary == plan.units_question

    mm = generate_improvement_plan(str(rx.stage(MM, tmp_path / "mm")))
    assert not mm.units_question and isinstance(mm.critical_count, int) and mm.risks


def test_the_plan_door_opens_with_the_question(tmp_path):
    reply, _ = _door("design_improvement_plan", "donut_m.stl", tmp_path)
    assert reply["summary"].startswith("This model measures 0.088mm at its largest"), reply["summary"]
    assert "Nothing was rescaled" in reply["summary"], reply["summary"]
    assert reply["critical_count"] is None and reply["overall_structural_score"] is None, reply
    judged, _ = _door("design_improvement_plan", MM, tmp_path)
    assert isinstance(judged["critical_count"], int) and judged["summary"][0].isdigit(), judged["summary"]


def test_reinforcing_a_metres_file_writes_nothing_and_leaves_it_as_it_was(tmp_path):
    reply, staged = _door("apply_design_reinforcements", "donut_m.stl", tmp_path)
    before = hashlib.sha256(staged.read_bytes()).hexdigest()
    assert reply["applied"] == [] and reply["skipped"] == [], reply
    assert not (tmp_path / "donut_m" / "out" / "reinforced.stl").exists(), "a reinforced copy was written"
    assert reply["output_path"] == str(staged), reply
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == before
    assert reply["summary"].startswith("This model measures") and "Nothing was reinforced" in reply["summary"]
    assert reply["before_score"] is None and reply["after_score"] is None, reply


@pytest.mark.parametrize("rel", METRES)
def test_slicer_settings_for_a_metres_file_are_defaults_that_claim_nothing(rel, tmp_path):
    reply, _ = _door("infer_print_settings", rel, tmp_path)
    judged, _ = _door("infer_print_settings", MM, tmp_path)
    assert reply["confidence"] == "low", reply
    assert (reply["support_reason"], reply["brim_reason"], reply["orientation_reason"]) == ("", "", ""), reply
    assert (reply["perimeters"], reply["infill_percent"]) == (3, 20), reply  # PLA's defaults
    assert judged["support_reason"] and judged["confidence"] != "low", judged


@pytest.mark.parametrize("rel", METRES)
def test_the_load_analysis_recommends_no_orientation_for_a_part_it_did_not_judge(rel, tmp_path):
    reply, _ = _door("assess_load_bearing", rel, tmp_path)
    judged, _ = _door("assess_load_bearing", MM, tmp_path)
    assert reply["recommended_print_orientation"] == "" and reply["load_surfaces"] == [], reply
    assert reply["units_question"].startswith("This model measures"), reply
    assert judged["recommended_print_orientation"] and judged["load_surfaces"], judged


def test_the_audit_says_the_structural_check_did_not_run(tmp_path):
    reply, _ = _door("audit_original_design", "donut_m.stl", tmp_path)
    gate = next(g for g in reply["gates"] if g["name"] == "structural_integrity")
    assert gate["checked"] is False and gate["passed"] is False and gate["severity"] == "info", gate
    assert gate["message"].startswith("This model measures"), gate["message"]
    assert reply["ready_for_print"] is False

    judged, _ = _door("audit_original_design", MM, tmp_path)
    gate = next(g for g in judged["gates"] if g["name"] == "structural_integrity")
    assert gate["checked"] is True and gate["message"][0].isdigit(), gate


@pytest.mark.parametrize("rel", METRES)
def test_generation_feedback_on_a_metres_file_opens_with_the_question(rel, tmp_path):
    reply, _ = _door("analyze_generation_feedback", rel, tmp_path)
    assert reply["message"].startswith("This model measures"), reply["message"]
    assert "No issues detected" not in reply["message"], reply["message"]
    improved, _ = _door("improve_generation_prompt", rel, tmp_path)
    assert improved["message"].startswith("This model measures"), improved["message"]
    # The same object in millimetres gets the structural feedback.
    judged, _ = _door("analyze_generation_feedback", MM, tmp_path)
    assert any(f.get("feedback_type") == "structural" for f in judged["feedback"]), judged
