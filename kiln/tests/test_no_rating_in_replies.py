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
    # The search this file relies on, on the shapes the doors used to send.
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
