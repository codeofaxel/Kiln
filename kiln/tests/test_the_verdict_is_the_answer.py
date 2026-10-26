"""The printability verdict is the answer, at every door; the score is not said.

Since 2026-10-20 the check answers ready, prints with supports, or won't
print, and keeps its 0-100 score for comparing versions of a part.  The
doors around it kept the score as the answer: messages read "Printability
score 50/100 (grade F)" beside the verdict, and the gates passed and failed
on the number, so a part that only needs supports failed the mesh
validation at the bar of 40 and was a critical blocker in the design audit,
while a part resting on a point read "Print-ready" in the pre-print check
with a warning, because a printability failure there was only a warning.

Every gate now reads :func:`kiln.printability.passes`: a part that prints,
as it is or with supports, passes whatever it scores; one that won't print
fails whatever it scores; only a part with no single cause named is judged
on its score, against the gate's own bar.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

#: A score or grade said in a sentence, the shapes the doors used.
_SCORE_SAID = re.compile(r"\d{1,3}\s*/\s*100|\bgrade\s+[A-F]\b|\([A-F]\)", re.IGNORECASE)


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


@pytest.fixture()
def mushroom(tmp_path) -> str:
    """An 80 mm cap on a 3 mm stem: prints with supports, and scores 36."""
    cap = _box((-40, -40, 25), (40, 40, 27))
    for _ in range(4):
        cap = cap.subdivide()
    path = tmp_path / "mushroom.stl"
    trimesh.util.concatenate([_box((-1.5, -1.5, 0), (1.5, 1.5, 25)), cap,
                              _box((-40, -40, 27), (-36, 40, 40))]).export(path)
    return str(path)


@pytest.fixture()
def cone_on_its_tip(tmp_path) -> str:
    cone = trimesh.creation.cone(radius=10, height=20, sections=48)
    cone.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    cone.apply_translation((0, 0, 20))
    path = tmp_path / "cone.stl"
    cone.export(path)
    return str(path)


@pytest.fixture()
def block(tmp_path) -> str:
    path = tmp_path / "block.stl"
    _box((0, 0, 0), (20, 20, 10)).export(path)
    return str(path)


def _call(name: str, **args):
    from kiln import server
    from tests import _real_exports as rx

    return rx.call(server.mcp, name, args)


def test_the_fixtures_are_what_they_say(mushroom, cone_on_its_tip, block):
    from kiln.printability import analyze_printability, verdict

    tall = analyze_printability(mushroom)
    assert verdict(tall)[0] == "prints_with_supports" and tall.score < 40
    assert verdict(analyze_printability(cone_on_its_tip))[0] == "wont_print"
    assert verdict(analyze_printability(block))[0] == "ready"


def test_passes_reads_the_verdict_and_the_bar_only_where_no_cause_is_named():
    from kiln.printability import passes

    assert passes("ready", 61, bar=90) and passes("prints_with_supports", 5, bar=60)
    assert not passes("wont_print", 100, bar=0)
    assert passes("check", 45, bar=40) and not passes("check", 39, bar=40)
    assert not passes("", 100, bar=0)  # the check never ran


@pytest.mark.parametrize("part", ["mushroom", "cone_on_its_tip", "block"])
def test_no_door_says_the_score(part, request, tmp_path):
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    path = request.getfixturevalue(part)
    replies = {
        "analyze_printability": _call("analyze_printability", file_path=path),
        "validate_and_prepare_mesh": _call("validate_and_prepare_mesh", file_path=path,
                                           build_volume_x=256, build_volume_y=256, build_volume_z=256),
        "validate_and_prepare": {"message": run_full_validation_pipeline(path)["summary"]},
    }
    for door, reply in replies.items():
        said = reply.get("message", "")
        assert said and not _SCORE_SAID.search(said), f"{door}: {said}"
    report = replies["analyze_printability"]["report"]
    assert isinstance(report["score"], int) and report["grade"] in "ABCDF"


def test_a_part_that_prints_with_supports_passes_every_gate_whatever_it_scores(mushroom):
    from kiln.original_design import audit_original_design
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    mesh = _call("validate_and_prepare_mesh", file_path=mushroom,
                 build_volume_x=256, build_volume_y=256, build_volume_z=256)
    assert mesh["passed"] is True, mesh["message"]
    assert mesh.get("verdict") == "prints_with_supports"

    full = run_full_validation_pipeline(mushroom)
    check = next(c for c in full["checks"] if c["name"] == "printability")
    assert check["passed"] is True and check["severity"] == "info", check
    assert full["ready_to_print"] is True
    assert full["summary"].startswith("Print-ready with supports"), full["summary"]

    audit = audit_original_design(mushroom, "a mushroom")
    gate = next(g for g in audit.gates if g.name == "printability")
    assert gate.severity != "critical" and gate.message.startswith("Prints with supports")


def test_a_part_that_wont_print_fails_every_gate(cone_on_its_tip):
    from kiln.original_design import audit_original_design
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    full = run_full_validation_pipeline(cone_on_its_tip)
    assert full["ready_to_print"] is False, full["summary"]
    check = next(c for c in full["checks"] if c["name"] == "printability")
    assert check["passed"] is False and check["severity"] == "error", check
    assert full["summary"].startswith("Not ready."), full["summary"]

    mesh = _call("validate_and_prepare_mesh", file_path=cone_on_its_tip,
                 build_volume_x=256, build_volume_y=256, build_volume_z=256)
    assert mesh["passed"] is False and mesh.get("verdict") == "wont_print"
    assert "Won't print as it is" in mesh["message"]

    audit = audit_original_design(cone_on_its_tip, "a cone")
    gate = next(g for g in audit.gates if g.name == "printability")
    assert gate.severity == "critical" and not audit.ready_for_print


def test_the_command_line_says_the_verdict_alone(cone_on_its_tip):
    from click.testing import CliRunner

    from kiln.cli.main import cli

    out = CliRunner().invoke(cli, ["validate", cone_on_its_tip]).output
    assert out.startswith("Won't print as it is"), out
    assert not _SCORE_SAID.search(out), out


_EXPECTED = {"mushroom": "prints_with_supports", "cone_on_its_tip": "wont_print", "block": "ready"}


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_every_print_door_carries_the_verdict(part, request):
    """The block every print door attaches as ``validation`` names the verdict.

    ``slice_and_print``, ``run_quick_print``, ``generate_and_print`` and the
    pipelines all hand out :attr:`PrintGate.summary`; it carried the scores
    and no verdict, so a print door's reply said how the part scored and not
    whether it prints.
    """
    from kiln.pipelines import _validate_mesh_step
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    path = request.getfixturevalue(part)
    gate = gate_for_print(path)
    block = gate.summary
    assert block is not None
    assert block["printability_verdict"] == _EXPECTED[part], block
    text = block["printability_verdict_text"]
    assert text and text == gate.report["printability_verdict_text"]
    assert not _SCORE_SAID.search(text), text

    step, _ = _validate_mesh_step(path, printer_id="", label="test")
    assert step.data["printability_verdict"] == _EXPECTED[part]
    assert step.data["printability_verdict_text"] == text


def _fake_slice(tmp_path):
    from unittest.mock import MagicMock

    from kiln.slicer import SliceResult
    from kiln.slicer_filament import resolve_slice_filament

    gcode = tmp_path / "out.gcode"
    gcode.write_text(";LAYER_CHANGE\nG1 X1 E1\n; filament used [g] = 12.5\n")
    return MagicMock(return_value=SliceResult(
        success=True, output_path=str(gcode), slicer="prusa-slicer",
        message="Sliced", filament=resolve_slice_filament("PLA"),
    ))


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_the_check_doors_carry_the_verdict_at_the_top(part, request, tmp_path):
    """``validate_and_prepare``, ``validate_and_prepare_mesh`` and
    ``slice_and_estimate`` name the verdict in the same two keys at the top of
    their reply, beside the ``verdict`` key the last two already shipped, and
    the message says that sentence."""
    from unittest.mock import patch

    from kiln.printability import analyze_printability, verdict

    path = request.getfixturevalue(part)
    key, text = verdict(analyze_printability(path))
    assert key == _EXPECTED[part]

    mesh = _call("validate_and_prepare_mesh", file_path=path,
                 build_volume_x=256, build_volume_y=256, build_volume_z=256)
    with patch("kiln.slicer.slice_file", _fake_slice(tmp_path)):
        estimate = _call("slice_and_estimate", input_path=path, material="PLA")
    assert estimate.get("success"), estimate
    for door, reply in {"validate_and_prepare_mesh": mesh, "slice_and_estimate": estimate}.items():
        assert reply.get("printability_verdict") == key == reply["verdict"], (door, reply)
        assert reply.get("printability_verdict_text") == text, (door, reply)
        assert text.rstrip(".") in reply["message"], (door, reply["message"])

    full = _call("validate_and_prepare", input_path=path)
    assert full["printability_verdict"] == key, full
    assert full["printability_verdict_text"] == text


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_the_support_estimates_lead_with_the_verdict(part, request):
    """``estimate_supports`` and ``estimate_support_material`` read the one
    support answer and said only "Supports needed.": a part resting on a
    point read "No supports needed." with nothing saying it won't print.
    Each now carries the verdict and why it needs supports, and says the
    verdict before the volume."""
    from kiln.auto_orient import estimate_supports
    from kiln.printability import analyze_printability, support_verdict, verdict

    path = request.getfixturevalue(part)
    report = analyze_printability(path, include_hole_detection=False)
    key, text = verdict(report)
    assert key == _EXPECTED[part]
    reason = support_verdict(report)

    engine = estimate_supports(path).to_dict()
    assert engine["printability_verdict"] == key, engine
    assert engine["printability_verdict_text"] == text
    assert engine["support_reason"] == reason

    tool = _call("estimate_supports", file_path=path)
    material = _call("estimate_support_material", file_path=path)
    for door, reply in {"estimate_supports": tool, "estimate_support_material": material}.items():
        assert reply.get("success"), (door, reply)
        assert reply.get("printability_verdict") == key, (door, reply)
        assert reply.get("printability_verdict_text") == text, (door, reply)
        said = reply["message"]
        assert said.startswith(text), (door, said)
        assert "Estimated support volume" in said and not _SCORE_SAID.search(said), (door, said)
    assert material["support_reason"] == reason


#: ``check_print_readiness``'s own vocabulary for each fixture, and its
#: ``can_print``.  A cone on its tip reads ``wont_print`` and cannot print:
#: the verdict beside it says it won't print, and so does every print door.
_READINESS = {"mushroom": ("printable_with_supports", True),
              "cone_on_its_tip": ("wont_print", False),
              "block": ("ready_to_print", True)}


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_the_readiness_check_carries_the_verdict(part, request, monkeypatch):
    """``check_print_readiness`` returned the score and grade of the report
    it analyzed and not its verdict, so a part resting on a point read
    "ready_to_print" with nothing saying it won't print as it is.

    Until 2026-10-24 this test pinned that cone at ``ready_to_print`` on
    purpose: the check judged only the mesh, and the verdict rode beside it
    deciding nothing, so an agent reading ``can_print: true`` was told to
    print a part every print door refuses.  The pin is reversed, not
    dropped: a part the verdict says won't print now reads ``wont_print``
    with ``can_print`` false, and the other two fixtures keep their words."""
    import sys

    from kiln.generation.validation import can_print_now
    from kiln.printability import analyze_printability, verdict

    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", None)
    path = request.getfixturevalue(part)
    key, text = verdict(analyze_printability(path, material="pla"))
    assert key == _EXPECTED[part]

    engine = can_print_now(path)
    tool = _call("check_print_readiness", file_path=path)
    for door, reply in {"can_print_now": engine, "check_print_readiness": tool}.items():
        assert reply.get("printability_verdict") == key, (door, reply)
        assert reply.get("printability_verdict_text") == text, (door, reply)
        assert (reply["verdict"], reply["can_print"]) == _READINESS[part], (door, reply)
        assert isinstance(reply["printability_score"], int)


@pytest.fixture()
def bare_mushroom(tmp_path) -> str:
    """An 80 mm cap on a 3 mm stem and nothing else: prints with supports."""
    path = tmp_path / "bare_mushroom.stl"
    trimesh.util.concatenate([_box((-1.5, -1.5, 0), (1.5, 1.5, 25)),
                              _box((-40, -40, 25), (40, 40, 27))]).export(path)
    return str(path)


@pytest.fixture()
def empty_file(tmp_path) -> str:
    """A binary STL header that holds no triangles."""
    path = tmp_path / "empty.stl"
    path.write_bytes(b"\0" * 80 + (0).to_bytes(4, "little"))
    return str(path)


@pytest.fixture()
def too_big_slab(tmp_path) -> str:
    """A 300 mm slab, wider than the 256 mm bed it is asked about."""
    path = tmp_path / "slab.stl"
    _box((0, 0, 0), (300, 300, 5)).export(path)
    return str(path)


@pytest.mark.parametrize("part", ["block", "bare_mushroom", "cone_on_its_tip",
                                  "empty_file", "too_big_slab"])
def test_can_print_says_what_the_print_door_does(part, request):
    """``can_print`` is true exactly when the pre-print gate every print door
    runs (:func:`gate_for_print`) would send the file on, asked about the same
    file, printer and material.  ``check_print_readiness`` runs fewer steps
    than that gate (no repair, no support or structural assessment), so this
    pins five shapes: a clean part, one that prints with
    supports, one that won't print as it is, a file with no part, and a part
    too big for the bed."""
    from kiln.generation.validation import can_print_now
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    path = request.getfixturevalue(part)
    readiness = can_print_now(path, printer_id="bambu_x1c", material="pla")
    gate = gate_for_print(path, printer_id="bambu_x1c", material="pla")
    assert readiness["can_print"] is (gate.reason is None), (readiness, gate.reason)


def test_a_part_the_verdict_asks_to_check_still_prints(block, monkeypatch):
    """A part under the printable line with no single cause named (the
    ``check`` verdict) is printed by the pre-print gate with a warning, not
    refused, so ``can_print`` stays true for it: the readiness check refuses
    only what the print door refuses.  It reads ``check_before_printing``,
    the verdict's own words, rather than ``ready_to_print`` beside a sentence
    asking for a check."""
    import kiln.printability as printability
    from kiln.generation.validation import can_print_now
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    real = printability.analyze_printability

    def under_the_line(*args, **kwargs):
        report = real(*args, **kwargs)
        report.score, report.grade, report.printable = 45, "F", False
        report.recommendations = ["Lay the part flatter on the bed."] + list(report.recommendations)
        return report

    monkeypatch.setattr(printability, "analyze_printability", under_the_line)
    readiness = can_print_now(block, printer_id="bambu_x1c", material="pla")
    assert readiness["printability_verdict"] == "check", readiness
    assert readiness["verdict"] == "check_before_printing", readiness
    assert readiness["can_print"] is True
    gate = gate_for_print(block, printer_id="bambu_x1c", material="pla")
    assert gate.reason is None, gate.reason
    assert gate.report["printability_verdict"] == "check"


# --- defects a lap found (2026-10-24; each fails at e0b5a815) ---------------
# The lap walked every registered door on a part that won't print, one that
# prints with supports and one that is ready, and read the replies.  Two doors
# judged the part and never named the verdict; one let the slicer's stderr be
# the whole answer about a part Kiln already knew would not print.


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_the_scorecard_and_the_ai_prep_name_the_verdict(part, request):
    """``mesh_quality_scorecard`` graded the part's printability (score, grade,
    notes) and ``prepare_ai_model_for_print`` said ``ready_to_print``; neither
    carried the verdict the grade and the flag were read from, so a reader got
    a letter or a boolean where every other door gives the sentence."""
    from kiln.printability import analyze_printability, verdict

    path = request.getfixturevalue(part)
    key, text = verdict(analyze_printability(path))

    card = _call("mesh_quality_scorecard", file_path=path)
    assert card.get("success"), card
    assert card["printability"]["printability_verdict"] == key, card["printability"]
    assert card["printability"]["printability_verdict_text"] == text
    assert card["printability_verdict"] == key and card["printability_verdict_text"] == text

    prep = _call("prepare_ai_model_for_print", input_path=path)
    assert prep.get("printability_verdict") == key, prep
    assert prep.get("printability_verdict_text") == text, prep
    assert prep["ready_to_print"] is (key in ("ready", "prints_with_supports")), prep


def test_a_slice_the_slicer_refuses_still_says_kiln_s_verdict(cone_on_its_tip, block):
    """PrusaSlicer refuses a cone on its tip with "There is an object with no
    extrusions in the first layer", and ``slice_and_estimate`` handed that line
    back as the whole answer.  Kiln's own check had the reason in words; the
    reply now leads with it and carries the verdict, with the slicer's words
    after.  A part Kiln calls ready keeps the slicer's error as the message."""
    from unittest.mock import patch

    from kiln.printability import analyze_printability, verdict
    from kiln.slicer import SlicerError

    refusal = SlicerError("Slicer exited with code 1. stderr: There is an object with no extrusions in the first layer.")
    with patch("kiln.slicer.slice_file", side_effect=refusal):
        reply = _call("slice_and_estimate", input_path=cone_on_its_tip, material="PLA")
    key, text = verdict(analyze_printability(cone_on_its_tip))
    assert key == "wont_print"
    assert reply.get("success") is False, reply
    assert reply["printability_verdict"] == key and reply["printability_verdict_text"] == text, reply
    assert reply["error"]["message"].startswith(text), reply["error"]
    assert "no extrusions in the first layer" in reply["error"]["message"]
    assert not _SCORE_SAID.search(reply["error"]["message"])

    with patch("kiln.slicer.slice_file", side_effect=SlicerError("boom")):
        fine = _call("slice_and_estimate", input_path=block, material="PLA")
    assert fine.get("success") is False
    assert fine["printability_verdict"] == "ready", fine
    assert fine["error"]["message"].startswith("Failed to slice model: boom"), fine["error"]
