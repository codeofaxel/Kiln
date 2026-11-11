"""A model from outside Kiln opens a goal in the person's Kiln account.

On an install without kiln-pro (every customer's), a model that came from
outside Kiln -- a generator's result, a marketplace download: anything with
an arrival note beside it -- and names no goal: the one join helper every
make door calls asks Kiln's servers to open a goal for it, with where it
came from and what was asked of the generator, and the reply says which
goal.  A file Kiln made here still opens nothing, and an
install that was never signed in is told in one line what signing in keeps.

Walked with kiln-pro removed from the import system and Kiln's servers stood
in at the one served door, the seam ``test_goal_join_on_a_plain_install.py``
uses.
"""

from __future__ import annotations

import hashlib

import sys
from pathlib import Path

import pytest

import kiln.server as srv
from kiln import arrival, served_makes
from kiln.arrival import DOWNLOADED, GENERATED, Arrival, record
from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY, join_goal
from kiln.reference_compare import REFERENCE_KEY

_TOKEN = "tok_" + "b" * 28
_STL = (
    "solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
    "vertex 0 1 0\nendloop\nendfacet\nendsolid s\n"
)


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def generated(tmp_path) -> str:
    path = tmp_path / "fox_knight.stl"
    path.write_text(_STL)
    record(str(path), Arrival(kind=GENERATED, by="Meshy", job_id="job-77",
                              prompt="a small fox knight holding a lantern", real_size=False))
    return str(path)


@pytest.fixture
def made_here(tmp_path) -> str:
    path = tmp_path / "bracket.stl"
    path.write_text(_STL)
    return str(path)


class _Servers:
    def __init__(self, answer):
        self.answer = answer
        self.asks: list[tuple[str, dict]] = []
        self.uploads: list[str] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs, _asked_by_user=_asked_by_user)))
        if isinstance(self.answer, BaseException):
            raise self.answer
        if callable(self.answer):
            return self.answer(kwargs)
        return dict(self.answer) if isinstance(self.answer, dict) else self.answer

    def upload(self, path, local, form, key, on_miss=None):
        self.uploads.append(str(local))
        return _TOKEN, ""


@pytest.fixture
def servers(monkeypatch):
    def install(answer, *, bearer=("bearer", None)):
        s = _Servers(answer)
        monkeypatch.setattr(srv, "_served_bearer", lambda: bearer)
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        monkeypatch.setattr(served_makes, "_upload", s.upload)
        return s

    return install


def _opened(brief_id="goal-9"):
    note = GOAL_NOTES["opened"].format(brief_id=brief_id, version=1)
    block = {
        "brief_id": brief_id, "joined": True, "opened": True, "version": 1,
        "version_id": "fox_knight-1a2b3c4d", "note": note,
        "came_from": {"kind": "generated", "by": "Meshy", "job_id": "job-77"},
    }
    return {"status": "success", "message": note, SAVED_GOAL_KEY: block}


def test_an_outside_model_with_no_goal_opens_one_in_the_account(plain_install, servers, generated):
    s = servers(_opened())
    env = {"success": True, "local_path": generated}
    assert join_goal("", env, generated) is env

    # The model stays on this computer: nothing is uploaded and no file
    # rides the call, only its fingerprint.
    assert s.uploads == []
    tool, sent = s.asks[0]
    assert tool == "design_session" and sent["verb"] == "join"
    # No goal is named: the servers open one.
    assert sent["goal_id"] == ""
    assert "source_artifact_token" not in sent and "mesh_path" not in sent
    # Nobody asked for this; it is not counted as a person reaching for a feature.
    assert sent["_asked_by_user"] is False
    # Where it came from and the person's own words go; where it sits here does not.
    assert sent["made_with"] == {
        "file_name": "fox_knight.stl", "kind": "generated", "by": "Meshy",
        "job_id": "job-77", "prompt": "a small fox knight holding a lantern",
        "sha256": hashlib.sha256(Path(generated).read_bytes()).hexdigest(),
    }
    assert str(Path(generated).parent) not in repr(sent)

    block = env[SAVED_GOAL_KEY]
    assert block["opened"] is True and block["joined"] is True
    assert block["brief_id"] == "goal-9" and block["version"] == 1
    assert block["joined_on"] == "kiln_servers"
    assert "goal-9" in block["note"]
    # The make itself is as it was.
    assert env["success"] is True and env["local_path"] == generated
    assert REFERENCE_KEY not in env


def test_a_file_kiln_made_asks_nothing(plain_install, servers, made_here):
    s = servers(_opened())
    env = {"success": True, "stl_path": made_here}
    join_goal("", env, made_here)
    assert env == {"success": True, "stl_path": made_here}
    assert s.asks == [] and s.uploads == []


def test_a_failed_make_asks_nothing(plain_install, servers, generated):
    s = servers(_opened())
    env = {"success": False, "error": "no"}
    join_goal("", env, generated)
    assert SAVED_GOAL_KEY not in env and s.asks == []


def test_never_signed_in_is_one_plain_line_and_nothing_is_sent(plain_install, servers, generated):
    s = servers(_opened(), bearer=(None, None))
    env = {"success": True, "local_path": generated}
    join_goal("", env, generated)
    block = env[SAVED_GOAL_KEY]
    assert block["note"] == GOAL_NOTES["sign_in_keeps_source"]
    assert block["joined"] is False and block["opened"] is False
    assert block["why"] == "signed_out"
    # A line, not an error and not a pitch.
    assert "error" not in env and env["success"] is True
    assert "(free)" in block["note"] and "can't" not in block["note"]
    for word in ("Pro", "upgrade", "pricing", "plan"):
        assert word not in block["note"]
    assert s.asks == [] and s.uploads == []


@pytest.mark.parametrize(
    ("setup", "why", "says"),
    [
        ({"bearer": ("", "Your Kiln session has expired.")}, "signed_out", "sign back in"),
        ({"answer": OSError(51, "Network is unreachable")}, None, ""),
        (
            {"answer": {"status": "error", "code": "SERVER_UNREACHABLE", "error": "down", "retryable": True}},
            "unanswered", "",
        ),
        ({"answer": {"status": "error", "code": "QUOTA", "error": "No more today."}}, "refused", "No more today."),
        ({"answer": {"status": "success", "message": "ok"}}, "unanswered", ""),
    ],
    ids=["session_expired", "offline", "unanswered", "refused", "no_goal_in_the_answer"],
)
def test_every_miss_says_the_model_is_here_and_was_not_recorded(plain_install, servers, generated, setup, why, says):
    servers(setup.get("answer"), bearer=setup.get("bearer", ("bearer", None)))
    env = {"success": True, "local_path": generated}
    assert join_goal("", env, generated) is env
    block = env[SAVED_GOAL_KEY]
    assert block["joined"] is False and block["opened"] is False and block["brief_id"] == ""
    if why:
        assert block["why"] == why
    assert block["why"] in ("offline", "signed_out", "unanswered", "refused")
    assert "did not record it in your history" in block["note"]
    assert "The model is here as made" in block["note"]
    assert says in block["note"]
    assert env["success"] is True
    assert arrival.noted_goal(generated) == ""


def test_the_opened_goal_is_noted_so_the_next_make_adds_to_it(plain_install, servers, generated):
    servers(_opened("goal-9"))
    join_goal("", {"success": True}, generated)
    assert arrival.noted_goal(generated) == "goal-9"
    # Where it came from is still read beside the file.
    assert arrival.read(generated).prompt == "a small fox knight holding a lantern"

    joined = {
        "status": "success",
        SAVED_GOAL_KEY: {"brief_id": "goal-9", "joined": True, "version": 1,
                         "note": GOAL_NOTES["joined"].format(brief_id="goal-9")},
    }
    s = servers(joined)
    env = {"success": True}
    join_goal("", env, generated)
    assert [(sent["goal_id"], sent["_asked_by_user"]) for _, sent in s.asks] == [("goal-9", False)]
    assert "prompt" not in s.asks[0][1]["made_with"]
    assert env[SAVED_GOAL_KEY]["joined"] is True and "opened" not in env[SAVED_GOAL_KEY]


def test_a_goal_noted_under_another_account_opens_one_in_this_one(plain_install, servers, generated):
    arrival.note_goal(generated, "goal-old")

    def answer(sent):
        if sent["goal_id"]:
            return {"status": "error", "code": "NOT_FOUND", "error": "I don't have that goal."}
        return _opened("goal-new")

    s = servers(answer)
    env = {"success": True}
    join_goal("", env, generated)
    assert [sent["goal_id"] for _, sent in s.asks] == ["goal-old", ""]
    assert env[SAVED_GOAL_KEY]["brief_id"] == "goal-new" and env[SAVED_GOAL_KEY]["opened"] is True
    assert arrival.noted_goal(generated) == "goal-new"


def test_the_same_model_in_another_file_keeps_its_goal(plain_install, servers, generated, tmp_path):
    servers(_opened("goal-9"))
    join_goal("", {"success": True}, generated)
    other = tmp_path / "fox_knight_scaled.stl"
    other.write_text(_STL.replace("1 0 0", "2 0 0"))
    arrival.carry(generated, str(other))
    assert arrival.noted_goal(str(other)) == "goal-9"


def test_a_download_sends_its_listing_and_no_address(plain_install, servers, tmp_path):
    """What this install read beside the file is scrubbed on the way out
    (the ask is Kiln's own), so the facts that name the listing go and
    nothing that is an address does."""
    path = tmp_path / "skull_planter.stl"
    path.write_text(_STL)
    record(str(path), Arrival(kind=DOWNLOADED, by="MakerWorld", name="Skull planter",
                              creator="someone", license="CC BY 4.0", model_id="1",
                              url="https://makerworld.com/en/models/1"))
    s = servers(_opened())
    join_goal("", {"success": True}, str(path))
    assert s.asks[0][1]["made_with"] == {
        "file_name": "skull_planter.stl", "kind": "downloaded", "by": "MakerWorld",
        "name": "Skull planter", "creator": "someone", "license": "CC BY 4.0", "model_id": "1",
        "listing_url": "https://makerworld.com/en/models/1",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_the_download_door_opens_a_goal_through_the_servers(plain_install, servers, generated, monkeypatch):
    """The image-to-3D download, as FastMCP calls it, with no goal named."""
    from kiln.generation.base import GenerationResult
    from kiln.plugins.generation_ai_tools import plugin

    class _P:
        name = "meshy"
        display_name = "Meshy"

        def download_result(self, job_id, output_dir=None):
            return GenerationResult(job_id=job_id, provider="meshy", local_path=generated, format="stl",
                                    file_size_bytes=Path(generated).stat().st_size,
                                    prompt="a small fox knight holding a lantern")

    monkeypatch.setattr(srv, "_get_generation_provider", lambda name="meshy": _P())
    s = servers(_opened())
    captured: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    out = captured["download_generated_model"](job_id="job-77")
    out = next(x for x in out if isinstance(x, dict)) if isinstance(out, list) else out
    assert out["success"] is True, out
    opens = [sent for tool, sent in s.asks if tool == "design_session"]
    assert len(opens) == 1 and opens[0]["verb"] == "join" and opens[0]["goal_id"] == ""
    assert opens[0]["made_with"]["prompt"] == "a small fox knight holding a lantern"
    assert out[SAVED_GOAL_KEY]["opened"] is True and out[SAVED_GOAL_KEY]["brief_id"] == "goal-9"
