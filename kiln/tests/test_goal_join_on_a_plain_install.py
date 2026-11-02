"""A model made on this computer joins the goal it names, in the person's Kiln account.

On an install without kiln-pro (every customer's), ``design_session`` keeps
the goal -- and the picture the person showed -- on Kiln's servers.  A make
on THIS computer that names the goal (a marketplace download, an import, an
image-to-3D job, a compile the servers did not answer) used to answer "Saved
goals live with design_session, which is not installed here" and was never
recorded on the goal or compared with its picture.  Now the one join helper
every make door calls sends the model up as ``design_session(verb="join")``
and the reply carries what the servers recorded and their comparison.

These tests walk that path with kiln-pro removed from the import system and
Kiln's servers stood in at the one served door (``kiln.server._pro_api_call``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import kiln.server as srv
from kiln import served_makes
from kiln.arrival import DOWNLOADED, Arrival, record
from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY, join_goal
from kiln.reference_compare import NOT_COMPARED, NOT_COMPARED_WITH_PICTURE, REFERENCE_KEY

_TOKEN = "tok_" + "a" * 28


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def made(tmp_path) -> str:
    path = tmp_path / "skull_planter.stl"
    path.write_text("solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
                    "vertex 0 1 0\nendloop\nendfacet\nendsolid s\n")
    return str(path)


class _Servers:
    """Kiln's servers at the served door: records each ask, answers with *answer*."""

    def __init__(self, answer):
        self.answer = answer
        self.asks: list[tuple[str, dict]] = []
        self.uploads: list[str] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs, _asked_by_user=_asked_by_user)))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer) if isinstance(self.answer, dict) else self.answer

    def upload(self, path, local, form, key, on_miss=None):
        self.uploads.append(str(local))
        return _TOKEN, ""


@pytest.fixture
def servers(monkeypatch):
    def install(answer, *, signed_in=True):
        s = _Servers(answer)
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("bearer", None) if signed_in else (None, None))
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        monkeypatch.setattr(served_makes, "_upload", s.upload)
        return s

    return install


def _joined(**extra):
    block = {
        "brief_id": "goal-1", "joined": True, "version": 2, "version_id": "skull_planter",
        "note": GOAL_NOTES["joined"].format(brief_id="goal-1"),
    }
    return {"status": "success", "message": "ok", SAVED_GOAL_KEY: block, **extra}


def test_a_downloaded_make_joins_its_goal_and_is_compared_on_the_servers(plain_install, servers, made):
    record(made, Arrival(kind=DOWNLOADED, by="MakerWorld", creator="someone",
                         license="CC BY 4.0", url="https://makerworld.com/en/models/1"))
    s = servers(_joined(**{REFERENCE_KEY: {"status": "compared", "findings": ["tail points up"]}}))
    env = {"success": True, "local_path": made}
    join_goal("goal-1", env, made)

    # The model went up, and the goal was asked to take it.
    assert s.uploads == [made]
    tool, sent = s.asks[0]
    assert tool == "design_session"
    assert sent["verb"] == "join" and sent["goal_id"] == "goal-1"
    assert sent["source_artifact_token"] == _TOKEN and "mesh_path" not in sent
    assert sent["_asked_by_user"] is True
    # Where it came from rides along; where it sits on this computer does not.
    assert sent["made_with"] == {
        "file_name": "skull_planter.stl",
        "kind": "downloaded", "by": "MakerWorld", "creator": "someone",
        "license": "CC BY 4.0", "url": "https://makerworld.com/en/models/1",
    }
    assert str(Path(made).parent) not in repr(sent["made_with"])

    assert env[SAVED_GOAL_KEY]["joined"] is True
    assert env[SAVED_GOAL_KEY]["version"] == 2
    assert env[SAVED_GOAL_KEY]["joined_on"] == "kiln_servers"
    assert "not installed here" not in env[SAVED_GOAL_KEY]["note"]
    assert env[REFERENCE_KEY] == {"status": "compared", "findings": ["tail points up"]}


def test_the_servers_comparison_replaces_a_not_compared_an_earlier_door_left(plain_install, servers, made):
    """An image-to-3D make says it was not compared before it joins; the
    goal's picture, compared on the servers, takes that place."""
    servers(_joined(**{REFERENCE_KEY: {"status": "compared"}}))
    env = {"success": True, REFERENCE_KEY: {"status": NOT_COMPARED, "references": ["p"],
                                            "reason": NOT_COMPARED_WITH_PICTURE}}
    join_goal("goal-1", env, made)
    assert env[REFERENCE_KEY] == {"status": "compared"}


def test_a_goal_with_no_picture_says_nothing_about_pictures(plain_install, servers, made):
    servers(_joined())
    env = {"success": True}
    join_goal("goal-1", env, made)
    assert env[SAVED_GOAL_KEY]["joined"] is True
    assert REFERENCE_KEY not in env


def test_a_goal_not_in_this_account_is_said(plain_install, servers, made):
    servers({"status": "error", "code": "NOT_FOUND", "error": "I don't have that goal."})
    env = {"success": True}
    join_goal("goal-1", env, made)
    assert env[SAVED_GOAL_KEY] == {
        "brief_id": "goal-1", "joined": False,
        "note": GOAL_NOTES["not_in_account"].format(brief_id="goal-1"),
    }


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"signed_in": False, "answer": None}, "signed_out"),
        ({"answer": OSError(51, "Network is unreachable")}, None),
        ({"answer": {"status": "error", "code": "SERVER_UNREACHABLE", "error": "down", "retryable": True}}, "unanswered"),
        ({"answer": {"status": "error", "code": "QUOTA", "error": "No more today."}}, "refused"),
    ],
)
def test_every_miss_says_the_make_stands_unjoined_and_uncompared(plain_install, servers, made, setup, why):
    servers(setup.get("answer"), signed_in=setup.get("signed_in", True))
    env = {"success": True}
    assert join_goal("goal-1", env, made) is env
    block = env[SAVED_GOAL_KEY]
    assert block["joined"] is False
    if why:
        assert block["why"] == why
    assert block["why"] in ("offline", "signed_out", "unanswered", "refused")
    assert "did not add it to the goal" in block["note"]
    assert env[REFERENCE_KEY]["status"] == NOT_COMPARED
    assert env[REFERENCE_KEY]["reason"] == block["note"]


def test_a_make_with_no_file_is_said_and_nothing_is_sent(plain_install, servers, tmp_path):
    s = servers(_joined())
    env = {"success": True}
    join_goal("goal-1", env, str(tmp_path / "gone.stl"))
    assert env[SAVED_GOAL_KEY]["note"] == GOAL_NOTES["no_file"].format(brief_id="goal-1")
    assert s.asks == [] and s.uploads == []


def test_a_failed_make_sends_nothing(plain_install, servers, made):
    s = servers(_joined())
    env = {"success": False, "error": "no"}
    join_goal("goal-1", env, made)
    assert SAVED_GOAL_KEY not in env and s.asks == []


def test_the_download_door_joins_through_the_servers(plain_install, servers, made, monkeypatch):
    """The image-to-3D download, as FastMCP calls it, with a goal named."""
    from kiln.generation.base import GenerationResult
    from kiln.plugins.generation_ai_tools import plugin

    class _P:
        name = "fake"
        display_name = "Fake"

        def download_result(self, job_id, output_dir=None):
            return GenerationResult(job_id=job_id, provider="fake", local_path=made, format="stl",
                                    file_size_bytes=Path(made).stat().st_size, prompt="",
                                    source_image="https://example.com/bull.png")

    monkeypatch.setattr(srv, "_get_generation_provider", lambda name="meshy": _P())
    s = servers(_joined(**{REFERENCE_KEY: {"status": "compared"}}))
    captured: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    out = captured["download_generated_model"](job_id="job1", brief_id="goal-1")
    out = next(x for x in out if isinstance(x, dict)) if isinstance(out, list) else out
    assert out["success"] is True, out
    joins = [sent for tool, sent in s.asks if tool == "design_session"]
    assert len(joins) == 1 and joins[0]["verb"] == "join" and joins[0]["goal_id"] == "goal-1"
    assert out[SAVED_GOAL_KEY]["joined"] is True
    assert out[REFERENCE_KEY] == {"status": "compared"}
