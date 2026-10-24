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
