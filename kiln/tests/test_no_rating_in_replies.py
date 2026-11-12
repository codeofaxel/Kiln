"""No reply carries a part's rating.

Kiln keeps the printability score and grade, and the quick mesh check's
0-100, in the process; a reply says the verdict and the reasons
(:data:`kiln.printability.RATING_KEYS`).  Each door here is called through
its registered tool, on a real part, and its whole reply is searched, so a
door that builds its own dict cannot carry the rating back out.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.printability import RATING_KEYS  # noqa: E402

#: Every name a reply has carried a rating under, beyond RATING_KEYS.
_RATING_NAMES = RATING_KEYS | {
    "overall_score", "overall_grade", "enriched_score",
    "mesh_check_score_a", "mesh_check_score_b", "mesh_check_score_delta",
}
#: A bare ``score`` or ``grade`` is the rating only in a printability block.
_BLOCK_MARKERS = frozenset({
    "printability_verdict", "verdict_text", "printable", "printable_after",
    "overhangs", "thin_walls", "bed_adhesion",
})


def ratings_in(node: Any, path: str = "") -> list[str]:
    """Every place in *node* a rating rides, as a dotted path."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            bare = key in ("score", "grade")
            in_block = any(marker in node for marker in _BLOCK_MARKERS)
            if key in _RATING_NAMES and value not in (None, "") and (in_block or not bare):
                found.append(here)
            found += ratings_in(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += ratings_in(item, f"{path}[{index}]")
    return found


def test_the_search_finds_a_rating_where_one_rides():
    # The search this file relies on, on the shapes a reply could carry a rating in.
    assert ratings_in({"report": {"printable": True, "score": 88, "grade": "B"}}) == [
        "report.score", "report.grade",
    ]
    assert ratings_in({"printability_score": 88, "mesh_check_score": 70}) == [
        "printability_score", "mesh_check_score",
    ]
    assert ratings_in({"structural": {"score": 80, "notes": []}}) == []
    assert ratings_in({"results": [{"name": "M3", "score": 0.9}]}) == []


@pytest.fixture
def server(monkeypatch):
    # Public Kiln alone: what a paid add-on serves inside a report is that
    # package's to decide, and its own suite judges it.  Its submodules are
    # blocked too, since one already imported would still answer.
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)

    from kiln import server as srv
    from kiln import terms

    terms.record_acceptance(method="test")
    getattr(srv, "_ensure_internal_tool_plugins_registered", lambda: None)()
    return srv


def _call(srv: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    from kiln.mcp_compat import tool_result_blocks

    out = asyncio.run(srv.mcp.call_tool(name, args))
    return json.loads(next(b.text for b in tool_result_blocks(out) if hasattr(b, "text")))


@pytest.fixture
def parts(tmp_path):
    """A plain block and a taller one: real parts every door can judge."""
    block = trimesh.creation.box(extents=(30.0, 20.0, 10.0))
    block.apply_translation((0, 0, 5.0))
    taller = trimesh.creation.box(extents=(30.0, 20.0, 14.0))
    taller.apply_translation((0, 0, 7.0))
    a, b = tmp_path / "block.stl", tmp_path / "taller.stl"
    block.export(a)
    taller.export(b)
    return str(a), str(b)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("analyze_printability", {"file_path": "{a}"}),
        ("mesh_quality_scorecard", {"file_path": "{a}"}),
        ("analyze_mesh_geometry", {"file_path": "{a}"}),
        ("analyze_warping_risk", {"file_path": "{a}", "material": "abs"}),
        ("compare_mesh_versions", {"file_a": "{a}", "file_b": "{b}"}),
        ("auto_orient_model", {"file_path": "{copy}"}),
        ("optimize_print_orientation", {"file_path": "{copy}"}),
        ("validate_and_prepare_mesh", {"file_path": "{a}"}),
        ("validate_and_prepare", {"input_path": "{a}"}),
    ],
)
def test_no_door_hands_back_a_rating(server, parts, tmp_path, tool, args):
    a, b = parts
    copy = str(tmp_path / "copy.stl")
    shutil.copyfile(a, copy)
    filled = {k: v.format(a=a, b=b, copy=copy) if isinstance(v, str) else v for k, v in args.items()}

    reply = _call(server, tool, filled)

    assert reply.get("success", True) is not False, reply
    assert ratings_in(reply) == [], f"{tool} carried a rating: {ratings_in(reply)}"


def test_the_failure_prediction_says_no_mesh_check_rating(parts):
    from kiln.generation.validation import predict_print_failures

    assert ratings_in(predict_print_failures(parts[0])) == []


def test_a_scorecard_says_each_area_in_words(server, parts):
    reply = _call(server, "mesh_quality_scorecard", {"file_path": parts[0]})
    body = reply.get("data", reply)
    for area in ("structural", "efficiency", "quality"):
        assert set(body[area]) == {"notes"}, body[area]
    assert body["printability_verdict"] in {"ready", "prints_with_supports", "wont_print", "check"}


def test_the_report_keeps_its_rating_in_the_process(parts):
    # The rating is still worked out: Kiln compares versions and gates a part
    # with no named cause on it.  It just never leaves.
    from kiln.printability import analyze_printability

    report = analyze_printability(parts[0])
    assert 0 <= report.score <= 100 and report.grade in {"A", "B", "C", "D", "F"}
    assert not set(report.to_dict()) & RATING_KEYS


# --- the rating if placed, the command line, and what tools promise ---


@pytest.fixture
def oversized(tmp_path):
    """A plate wider than a Bambu A1's bed: it fails where it sits and would
    print well placed, so its report says both."""
    plate = trimesh.creation.box(extents=(400.0, 400.0, 10.0))
    plate.apply_translation((0, 0, 5.0))
    path = tmp_path / "oversized.stl"
    plate.export(path)
    return str(path)


#: The rating a part would have where it sits fine: the same score and letter
#: as the report's own whenever nothing is wrong with where it sits.
_IF_PLACED = ("score_if_placed", "grade_if_placed")


def _keys_at_any_depth(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_at_any_depth(v)}
    if isinstance(node, list):
        return {k for item in node for k in _keys_at_any_depth(item)}
    return set()


@pytest.mark.parametrize("which", ["block", "oversized"])
def test_the_placement_block_carries_no_rating(server, parts, oversized, which):
    """Where the part sits says whether it prints once placed, never the
    score or letter it would have there (``score_if_placed`` /
    ``grade_if_placed``), which equal the report's own whenever nothing is
    wrong with where it sits.  Every door that serves the report
    (validate_and_prepare_mesh, audit_original_design, kiln validate)
    carries this same block."""
    path = parts[0] if which == "block" else oversized
    args = {"file_path": path, **({"printer_id": "bambu_a1"} if which == "oversized" else {})}

    reply = _call(server, "analyze_printability", args)

    placement = reply["report"]["placement"]
    assert not set(_IF_PLACED) & _keys_at_any_depth(reply), placement
    assert isinstance(placement["printable_if_placed"], bool), placement
    if which == "oversized":
        assert reply["verdict"] == "wont_print" and placement["faults"], reply["message"]


def test_the_pipeline_door_carries_no_placement_rating(server, oversized):
    reply = _call(server, "validate_and_prepare_mesh", {"file_path": oversized, "printer_id": "bambu_a1"})

    assert reply.get("success") is True, reply
    assert not set(_IF_PLACED) & _keys_at_any_depth(reply)


def test_kiln_validate_json_carries_no_rating(server, parts):
    """The command line's door: ``kiln validate --json`` prints the report,
    and no rating.  (``server`` keeps a paid add-on out of the process, as
    above.)"""
    from click.testing import CliRunner

    from kiln.cli.main import cli

    out = CliRunner().invoke(cli, ["validate", parts[0], "--json"])

    assert out.exit_code == 0, out.output
    printed = json.loads(out.output)
    assert ratings_in(printed) == [], ratings_in(printed)
    assert not set(_IF_PLACED) & _keys_at_any_depth(printed)


def test_no_tool_tells_an_agent_it_returns_a_printability_score(server):
    """No tool description tells an agent to expect a printability score or
    grade: an agent told to expect a number reports one it never received."""
    import re

    promise = re.compile(
        r"printability scor(?:e|es|ed|ing)|printability grade|numeric printability", re.IGNORECASE,
    )
    tools = asyncio.run(server.mcp.list_tools())
    promised = {t.name for t in tools if promise.search(t.description or "")}

    assert promised == set(), sorted(promised)


def test_iterate_design_says_no_rating_of_its_best_round(server, parts, monkeypatch):
    """iterate_design keeps its rounds' quick mesh check in the process,
    where it picks the best round and decides when to stop, and its reply
    carries none of it, under ``best_score`` or any other name."""
    from types import SimpleNamespace

    import kiln.server as srv_module

    block = parts[0]
    job = SimpleNamespace(
        id="round-1", error=None, status=SimpleNamespace(value="succeeded"),
        to_dict=lambda: {"id": "round-1", "status": "succeeded"},
    )
    made = SimpleNamespace(local_path=block, to_dict=lambda: {"local_path": block})
    provider = SimpleNamespace(
        name="openscad", generate=lambda *a, **k: job, download_result=lambda job_id: made,
    )
    monkeypatch.setattr(srv_module, "_get_generation_provider", lambda name: provider)

    reply = _call(server, "iterate_design", {"prompt": "cube([30, 20, 10]);", "max_iterations": 1})

    assert reply.get("success") is True, reply
    assert "best_score" not in reply, reply.get("best_score")
    assert ratings_in(reply) == [], ratings_in(reply)


# --- the design audit: its tally stays in the process, its reasons go out ---

#: The design audit's tally under every name a record carried it by.  It
#: starts from the printability rating, so it stays in the process too.
_TALLY = frozenset({
    "readiness_score", "readiness_grade", "best_readiness_score", "best_readiness_grade",
})
_REQUIREMENTS = "a sturdy desk part"


@pytest.fixture
def pillar(tmp_path):
    """A pillar 80 mm tall on a 10 mm square: nothing stops it outright, and
    the audit still holds it back (it may tip, it would lie down, it needs a
    brim)."""
    post = trimesh.creation.box(extents=(10.0, 10.0, 80.0))
    post.apply_translation((0, 0, 40.0))
    path = tmp_path / "pillar.stl"
    post.export(path)
    return str(path)


@pytest.mark.parametrize("which", ["block", "pillar"])
def test_the_design_audit_says_its_reasons_never_its_tally(server, parts, pillar, which):
    path = parts[0] if which == "block" else pillar

    reply = _call(server, "audit_original_design", {"file_path": path, "requirements": _REQUIREMENTS})

    assert reply.get("success") is True, reply
    assert not _TALLY & _keys_at_any_depth(reply), sorted(_TALLY & _keys_at_any_depth(reply))
    assert ratings_in(reply) == [], ratings_in(reply)
    assert all(isinstance(r, str) and r for r in reply["readiness_reasons"]), reply["readiness_reasons"]
    if which == "block":
        assert reply["ready_for_print"] is True and reply["readiness_reasons"] == [], reply


def test_a_design_held_back_with_no_blocker_is_told_why(server, pillar):
    """No one thing stops the pillar and it is not ready: the reply names
    each thing that counts against it, in words, and the message says them."""
    reply = _call(server, "audit_original_design", {"file_path": pillar, "requirements": _REQUIREMENTS})

    assert reply["ready_for_print"] is False and reply["blockers"] == [], reply["blockers"]
    reasons = reply["readiness_reasons"]
    turn = next(g["message"] for g in reply["gates"] if g["name"] == "orientation_efficiency")
    assert turn in reasons, reasons
    assert "Not ready for print. Together these count against it:" in reply["message"], reply["message"]
    for reason in reasons:
        assert reason.rstrip(".") in reply["message"], (reason, reply["message"])


def test_the_generation_record_carries_no_tally():
    """generate_model_with_provider replies with this record's to_dict(): the
    best attempt's tally and each attempt's stay in the process."""
    from kiln.original_design import OriginalDesignGeneration, OriginalDesignGenerationAttempt

    attempt = OriginalDesignGenerationAttempt(
        attempt_number=1, prompt_used="a desk part", provider="gemini", status="audited",
        ready_for_print=True, readiness_score=94, readiness_grade="A",
        audit={"ready_for_print": True, "readiness_reasons": []},
    )
    record = OriginalDesignGeneration(
        requirements_text="a desk part", provider_requested="auto", provider_used="gemini",
        provider_selection_reason="", material=None, printer_model=None, style=None,
        max_attempts=1, attempts_made=1, ready_for_print=True, best_attempt_number=1,
        best_readiness_score=94, best_readiness_grade="A", best_result_path=None,
        summary="Best of 1 attempt(s), via gemini.", next_actions=[], design_requirements={},
        initial_prompt={}, attempts=[attempt],
    )

    assert not _TALLY & _keys_at_any_depth(record.to_dict())
    assert record.best_readiness_score == 94 and attempt.readiness_score == 94  # kept in the process
