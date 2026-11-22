"""The design-notes doors run on Kiln's servers for a signed-in install, and
an answer made on this computer after the servers missed says why the notes
are not in it, names no plan, and keeps every caution.

``analyze_design_requirements``, ``match_design_requirements``,
``build_generation_prompt``, ``build_parametric_prompt`` and
``audit_original_design`` are named in the bundled manifest's
``served_public`` block.  Signed in, the servers' answer is the answer.
Signed out, offline, unanswered or refused, the tool runs here, through the
real tool dispatcher: each pattern and requirement keeps its caution, and the
answer carries one sentence for the miss's cause (``guidance_note``) and the
miss's fields, never the paid-plan pitch and never a second note.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import kiln.server as srv
from tests._signed_in import sign_in, sign_out

NEED = "outdoor shelf bracket that holds 10 lbs of books, snap fit"
STL = (
    "solid c\n"
    + "".join(
        "facet normal 0 0 0\nouter loop\n"
        + "".join(f"vertex {x} {y} {z}\n" for x, y, z in tri)
        + "endloop\nendfacet\n"
        for tri in (
            ((0, 0, 0), (20, 20, 0), (20, 0, 0)), ((0, 0, 0), (0, 20, 0), (20, 20, 0)),
            ((0, 0, 20), (20, 0, 20), (20, 20, 20)), ((0, 0, 20), (20, 20, 20), (0, 20, 20)),
            ((0, 0, 0), (20, 0, 0), (20, 0, 20)), ((0, 0, 0), (20, 0, 20), (0, 0, 20)),
            ((0, 20, 0), (20, 20, 20), (20, 20, 0)), ((0, 20, 0), (0, 20, 20), (20, 20, 20)),
            ((0, 0, 0), (0, 0, 20), (0, 20, 20)), ((0, 0, 0), (0, 20, 20), (0, 20, 0)),
            ((20, 0, 0), (20, 20, 0), (20, 20, 20)), ((20, 0, 0), (20, 20, 20), (20, 0, 20)),
        )
    )
    + "endsolid c\n"
)
PATTERN_CAUTION = "The print orientation for this pattern is named here"
REQUIREMENT_CAUTION = "Only the starting rules for this requirement are given here"
ON_THE_LINE = (
    "Kiln's design notes for each pattern and requirement come from Kiln's servers, "
    "free with any Kiln account."
)


def _arguments(part) -> dict[str, dict]:
    return {
        "analyze_design_requirements": {"requirements": NEED, "material": "petg"},
        "match_design_requirements": {"description": NEED},
        "build_generation_prompt": {"requirements": NEED, "material": "petg"},
        "build_parametric_prompt": {"requirements": NEED, "material": "petg"},
        "audit_original_design": {"file_path": str(part), "requirements": NEED, "material": "petg"},
    }


DOORS = tuple(_arguments("part.stl"))


class _Servers:
    def __init__(self):
        self.calls: list[str] = []
        self.answer: dict = {}

    def door(self, tool_name, _timeout=None, _asked_by_user=True, _http=None, **_kwargs):
        self.calls.append(tool_name)
        if _http is not None:
            _http["status"] = 200
        return json.loads(json.dumps(self.answer))

    def upload(self, url, **_kwargs):
        class _Up:
            status_code = 200
            headers = {"content-type": "application/json"}

            @staticmethod
            def json():
                return {"status": "success", "file_token": "upload0000000000000000000000001", "format": "stl"}

        return _Up()


@pytest.fixture
def servers(monkeypatch, tmp_path):
    """A signed-in install with no kiln-pro, its notes read from the public
    files alone, and Kiln's servers behind it."""
    import httpx

    from kiln import design_intelligence, material_limits, served_public
    from kiln.mcp_compat import FastMCP

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    # The knowledge base is read again without kiln-pro beside it.
    monkeypatch.setattr(design_intelligence, "_kb", None)
    srv._register_pro_tool_stubs(FastMCP("design-notes-test"))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    sign_in(monkeypatch, tmp_path)
    monkeypatch.setenv("KILN_API_URL", "https://kiln.test")
    monkeypatch.setattr(served_public, "_said", set())
    material_limits._answers.clear()
    material_limits._said.clear()
    fake = _Servers()
    monkeypatch.setattr(srv, "_pro_api_call", fake.door)
    monkeypatch.setattr(httpx, "post", fake.upload)
    yield fake
    served_public._reset_for_tests()
    material_limits._answers.clear()
    material_limits._said.clear()


@pytest.fixture
def part(tmp_path):
    path = tmp_path / "cube.stl"
    path.write_text(STL)
    return path


def _call(name: str, arguments: dict) -> dict:
    return asyncio.run(srv.mcp._tool_manager.call_tool(name, dict(arguments), context=None, convert_result=False))


def _miss(monkeypatch, miss) -> None:
    from kiln import served_public

    monkeypatch.setattr(served_public, "ask", lambda *_a, **_k: (None, miss))


def _cautions(answer: dict) -> tuple[list[dict], list[dict]]:
    brief = answer.get("design_requirements") or answer
    patterns = brief.get("applicable_patterns") or []
    needs = [*(brief.get("functional_constraints") or []), *(answer.get("matched_requirements") or [])]
    return patterns, needs


@pytest.mark.parametrize("name", DOORS)
def test_signed_in_the_servers_answer_is_the_answer(servers, part, name):
    servers.answer = {"success": True, "from": "the servers"}
    out = _call(name, _arguments(part)[name])
    assert servers.calls == [name]
    assert out["from"] == "the servers"
    assert "guidance_note" not in out and "answered_on_this_computer" not in out


@pytest.mark.parametrize("name", DOORS)
def test_signed_out_keeps_every_caution_and_says_why_once(servers, monkeypatch, part, name):
    sign_out()
    monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None), raising=False)
    out = _call(name, _arguments(part)[name])
    assert out["success"] is True
    assert name not in servers.calls
    assert "kiln3d.com/pricing" not in json.dumps(out)
    assert out["guidance_note"] == (
        f"{ON_THE_LINE} Kiln can't get them right now because Kiln is signed out, "
        "so it gives only the starting rules kept on this computer. Sign in and ask again."
    )
    assert out["why"] == "signed_out"
    # One note for the cause, not the general one beside it.
    assert "answered_on_this_computer" not in out
    patterns, needs = _cautions(out)
    for pattern in patterns:
        assert pattern["caution"].startswith(PATTERN_CAUTION)
        assert pattern["caution"].endswith("print a test piece before committing to it.")
    for need in needs:
        assert need["caution"].startswith(REQUIREMENT_CAUTION)
        assert need["caution"].endswith("test the part for this use before you depend on it.")


@pytest.mark.parametrize(
    ("cause", "code", "detail", "cause_words", "fix_words"),
    [
        ("offline", "SERVER_UNREACHABLE", "", "this computer is offline", "Reconnect to the internet and ask again."),
        ("unanswered", "KILN_API_HTTP_ERROR", "", "Kiln's servers didn't answer", "Wait a minute and ask again."),
        ("refused", "SOME_REFUSAL", "Not today.", "Kiln's servers said no", "Not today."),
    ],
)
def test_each_miss_is_said_in_its_own_words(servers, monkeypatch, cause, code, detail, cause_words, fix_words):
    from kiln.served_answer import Miss

    _miss(monkeypatch, Miss(cause, code, detail))
    out = _call("analyze_design_requirements", {"requirements": NEED, "material": "petg"})
    note = out["guidance_note"]
    assert note.startswith(ON_THE_LINE)
    assert cause_words in note and fix_words in note
    assert (out["why"], out["why_code"], out["why_detail"]) == (cause, code, detail)
    assert "kiln3d.com/pricing" not in json.dumps(out)


def test_a_prompt_with_nothing_left_out_carries_no_note(servers, monkeypatch):
    """Said only when a note was left out: a request no pattern or
    requirement matches has nothing missing to name."""
    from kiln.served_answer import Miss

    _miss(monkeypatch, Miss("offline", "SERVER_UNREACHABLE"))
    out = _call("match_design_requirements", {"description": "zzz qqq"})
    assert out["count"] == 0
    assert "guidance_note" not in out and "why" not in out


def test_outside_a_missed_call_the_caution_is_worded_as_it_always_is(monkeypatch):
    """The engine called directly, not through a served door, keeps the shared
    wording (:func:`kiln.design_intelligence.unestablished_caution`) and adds
    no note."""
    from kiln import design_intelligence

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(design_intelligence, "_kb", None)
    worded: list[str] = []

    def shared(kind, what, **_kwargs):
        worded.append(kind)
        return "SHARED WORDING"

    monkeypatch.setattr(design_intelligence, "unestablished_caution", shared)
    matched = design_intelligence.match_requirements(NEED)
    assert matched and all(need.caution == "SHARED WORDING" for need in matched)
    assert worded and set(worded) == {"functional_requirements"}
    assert design_intelligence.with_design_notes_note({"success": True}) == {"success": True}
