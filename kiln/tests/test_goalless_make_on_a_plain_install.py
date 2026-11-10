"""A make that names no goal, on an install without kiln-pro.

Signed in, the make is told to Kiln's servers in words -- the tool, what the
call named it, the made file's name -- and the reply carries the session the
person's account opened for it and the safety notes the object calls for.
Signed out, the first make of a session says what signing in adds and later
ones say nothing.  A miss leaves the make as it was.

These tests walk that path with kiln-pro removed from the import system and
Kiln's servers stood in at the one served door (``kiln.server._pro_api_call``).
What is asserted about the wire is what was SENT: no model, no source code,
no folder.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

import kiln.server as srv
from kiln import _pro_make_session_bridge as bridge
from kiln import served_answer, served_makes, served_public, tool_context
from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY, join_goal

_TOKEN = "tok_" + "a" * 28
_LAMP_NOTE = "Use LED bulbs only, and keep the shade from touching the bulb."


@pytest.fixture(autouse=True)
def _a_fresh_session():
    served_public._reset_for_tests()
    bridge._reset_for_tests()
    yield
    served_public._reset_for_tests()
    bridge._reset_for_tests()


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def made(tmp_path) -> str:
    folder = tmp_path / "private_folder_name"
    folder.mkdir()
    path = folder / "pendant_lamp_shade.stl"
    path.write_text("solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
                    "vertex 0 1 0\nendloop\nendfacet\nendsolid s\n")
    return str(path)


class _Servers:
    """Kiln's servers at the served door: records each ask, answers with *answer*."""

    def __init__(self, answer):
        self.answer = answer
        self.asks: list[tuple[str, dict]] = []
        self.timeouts: list[float | None] = []
        self.uploads: list[str] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs, _asked_by_user=_asked_by_user)))
        self.timeouts.append(_timeout)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer) if isinstance(self.answer, dict) else self.answer

    def upload(self, path, local, form, key, on_miss=None):
        self.uploads.append(str(local))
        return _TOKEN, ""


@pytest.fixture
def servers(monkeypatch):
    def install(answer, *, signed_in=True, expired=False):
        s = _Servers(answer)
        bearer = ("bearer", None) if signed_in else ("", "Your session ran out." if expired else None)
        monkeypatch.setattr(srv, "_served_bearer", lambda: bearer)
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        monkeypatch.setattr(served_makes, "_upload", s.upload)
        return s

    return install


def _opened(**extra):
    block = {
        "brief_id": "session-1", "joined": True, "opened": True, "version": 1,
        "version_id": "pendant_lamp_shade",
        "note": GOAL_NOTES["opened"].format(brief_id="session-1", version=1),
    }
    return {"status": "success", "message": "ok", SAVED_GOAL_KEY: block, **extra}


def _make(door: str, arguments: dict, envelope: dict, mesh: str | None, brief_id: str = ""):
    """``join_goal`` as a make door calls it, inside that door's dispatch."""
    token = tool_context.enter_tool(door, arguments)
    try:
        return join_goal(brief_id, envelope, mesh)
    finally:
        tool_context.leave_tool(token)


# ---------------------------------------------------------------------------
# Signed in: exactly the words go, and the answer lands in the reply
# ---------------------------------------------------------------------------


def test_a_signed_in_make_sends_its_words_and_nothing_else(plain_install, servers, made):
    s = servers(_opened())
    env = {"success": True, "local_path": made}
    _make(
        "download_model",
        {"file_id": "f-1", "model_id": "m-9", "source": "thingiverse",
         "dest_dir": str(Path(made).parent), "file_name": "pendant_lamp_shade.stl"},
        env, made,
    )
    assert len(s.asks) == 1
    tool, sent = s.asks[0]
    assert tool == "design_session"
    assert sent == {
        "verb": "join",
        "made_with": {
            "door": "download_model",
            "version_id": "pendant_lamp_shade",
            "file_name": "pendant_lamp_shade.stl",
        },
        "_asked_by_user": False,
    }
    # No model went up, and nothing says where the file sits on this computer.
    assert s.uploads == []
    assert "private_folder_name" not in repr(sent)
    assert s.timeouts == [bridge.CONSULT_TIMEOUT_S] and bridge.CONSULT_TIMEOUT_S <= 5


def test_the_naming_words_go_and_source_and_folders_do_not(plain_install, servers, made, tmp_path):
    s = servers(_opened())
    secret = tmp_path / "private_folder_name" / "notes.txt"
    secret.write_text("x")
    _make(
        "generate_and_print",
        {
            "prompt": "a pendant lamp shade for the kitchen",
            "description": "hangs over the table",
            "title": str(secret),
            "notes": "module shade() { difference() { sphere(40); sphere(38); } } shade();",
            "scad_code": "cube([10, 10, 10]);",
            "scad_path": str(tmp_path / "private_folder_name" / "shade.scad"),
            "output_path": made,
            "material": "PETG",
            "printer_name": "garage-a1",
            "timeout": 30,
        },
        {"success": True, "version_id": "v3"}, made,
    )
    sent = s.asks[0][1]
    assert sent["made_with"] == {
        "door": "generate_and_print",
        "version_id": "v3",
        "title": "notes.txt",
        "description": "hangs over the table",
        "prompt": "a pendant lamp shade for the kitchen",
        "file_name": "pendant_lamp_shade.stl",
    }
    wire = repr(sent)
    for never in ("cube(", "sphere(", "module ", "private_folder_name", str(tmp_path), "garage-a1", "PETG"):
        assert never not in wire, never
    assert s.uploads == []


def test_the_servers_session_and_safety_notes_land_in_the_makes_reply(plain_install, servers, made):
    asked = {"question": "Will a real flame burn in or next to this?", "goal_id": "session-1"}
    servers(_opened(safety_notes=[_LAMP_NOTE], owner_safety_notes=["Keep away from curtains"], open_question=asked))
    env = {"success": True, "local_path": made, "safety_notes": ["Wash by hand."]}
    _make("download_model", {"file_name": "pendant_lamp_shade.stl"}, env, made)
    assert env[SAVED_GOAL_KEY] == {
        "brief_id": "session-1", "joined": True, "opened": True, "version": 1,
        "version_id": "pendant_lamp_shade",
        "note": GOAL_NOTES["opened"].format(brief_id="session-1", version=1),
        "joined_on": "kiln_servers",
    }
    assert env["safety_notes"] == ["Wash by hand.", _LAMP_NOTE]
    assert env["owner_safety_notes"] == ["Keep away from curtains"]
    assert env["open_question"] == asked


def test_a_session_the_account_did_not_take_is_not_claimed_and_its_notes_still_land(plain_install, servers, made):
    block = {"brief_id": "", "joined": False, "saved": False, "note": GOAL_NOTES["not_saved"]}
    servers({"status": "success", SAVED_GOAL_KEY: block, "safety_notes": [_LAMP_NOTE]})
    env = {"success": True}
    _make("download_model", {}, env, made)
    assert env[SAVED_GOAL_KEY]["saved"] is False and env[SAVED_GOAL_KEY]["joined"] is False
    assert env["safety_notes"] == [_LAMP_NOTE]


# ---------------------------------------------------------------------------
# Signed out: one line, once per session, and nothing is sent
# ---------------------------------------------------------------------------


def test_signed_out_says_once_what_signing_in_adds(plain_install, servers, made):
    s = servers(_opened(), signed_in=False)
    first = {"success": True, "local_path": made}
    _make("download_model", {"file_name": "pendant_lamp_shade.stl"}, first, made)
    block = first[SAVED_GOAL_KEY]
    assert block["joined"] is False and block["brief_id"] == "" and block["why"] == "signed_out"
    assert block["note"] == bridge.SIGNED_OUT_LINE
    assert block["note"].startswith("Signed in to a free Kiln account, a make that names no goal is kept in a goal")
    assert "any safety note the object calls for" in block["note"]
    assert "no safety note here means none was looked for" in block["note"]
    assert block["agent_hint"] and block["setup_hint"]
    assert first["success"] is True and "safety_notes" not in first
    # Never again in this session, and never a refusal.
    second = {"success": True, "local_path": made}
    _make("download_model", {"file_name": "pendant_lamp_shade.stl"}, second, made)
    assert second == {"success": True, "local_path": made}
    assert s.asks == [] and s.uploads == []


def test_a_session_that_ran_out_is_told_to_sign_back_in(plain_install, servers, made):
    servers(_opened(), signed_in=False, expired=True)
    env = {"success": True}
    _make("download_model", {}, env, made)
    assert env[SAVED_GOAL_KEY]["note"] == bridge.SESSION_EXPIRED_LINE
    assert env[SAVED_GOAL_KEY]["note"].startswith("Your Kiln session has expired. Signed back in, ")


def test_a_note_another_tool_said_does_not_use_up_the_signed_out_line(plain_install, servers, made):
    served_public.say_once("analyze_printability", {}, served_answer.Miss("signed_out", served_answer.SESSION_EXPIRED_CODE))
    servers(_opened(), signed_in=False)
    env = {"success": True}
    _make("download_model", {}, env, made)
    assert env[SAVED_GOAL_KEY]["why"] == "signed_out"


# ---------------------------------------------------------------------------
# Offline, unanswered, refused: the make stands, said once per cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (OSError(51, "Network is unreachable"), "offline"),
        ({"status": "error", "code": "SERVER_UNREACHABLE", "error": "down", "why": "unanswered"}, "unanswered"),
        ({"status": "error", "code": "QUOTA", "error": "No more today."}, "refused"),
        ({"status": "success", "message": "an answer with no session in it"}, "unanswered"),
        (None, "unanswered"),
    ],
)
def test_every_miss_leaves_the_make_as_it_was_and_says_why_once(plain_install, servers, made, answer, why):
    servers(answer)
    first = {"success": True, "local_path": made}
    assert _make("download_model", {}, first, made) is first
    block = first.pop(SAVED_GOAL_KEY)
    assert first == {"success": True, "local_path": made}
    assert block["joined"] is False and block["brief_id"] == "" and block["why"] == why
    # A miss is never "no safety note": the line says none was looked for.
    assert "looked for no safety note" in block["note"]
    assert "no safety note in this reply means none was looked for" in block["note"]
    bridge._reset_for_tests()  # the servers are asked again; the cause is not said again
    second = {"success": True, "local_path": made}
    _make("download_model", {}, second, made)
    assert second == {"success": True, "local_path": made}


def test_a_cause_a_public_tool_already_said_is_not_said_again(plain_install, servers, made):
    served_public.say_once("analyze_printability", {}, served_answer.Miss("offline", "SERVER_UNREACHABLE"))
    servers(OSError(51, "Network is unreachable"))
    env = {"success": True, "local_path": made}
    _make("download_model", {}, env, made)
    assert env == {"success": True, "local_path": made}


def test_servers_that_did_not_answer_are_left_alone_for_a_while(plain_install, servers, made):
    s = servers({"status": "error", "code": "SERVER_UNREACHABLE", "error": "down", "why": "unanswered"})
    _make("download_model", {}, {"success": True}, made)
    later = {"success": True}
    _make("download_model", {}, later, made)
    assert len(s.asks) == 1 and later == {"success": True}


def test_a_make_the_dispatcher_already_sent_is_not_sent_again(plain_install, servers, made):
    """A public tool Kiln's servers did not answer runs on this computer; the
    dispatcher says why, and the make inside it asks nothing more."""
    s = servers(_opened())
    env = {"success": True, "local_path": made}
    with served_public.here_after(served_answer.Miss("unanswered", "SERVER_UNREACHABLE")):
        _make("compile_scad", {"scad_code": "cube(5);"}, env, made)
    assert s.asks == [] and env == {"success": True, "local_path": made}


# ---------------------------------------------------------------------------
# What is not a goal-less make
# ---------------------------------------------------------------------------


def test_a_make_that_names_its_goal_is_unchanged(plain_install, servers, made):
    joined = {
        "status": "success",
        SAVED_GOAL_KEY: {"brief_id": "goal-1", "joined": True, "version": 2,
                         "note": GOAL_NOTES["joined"].format(brief_id="goal-1")},
    }
    s = servers(joined)
    env = {"success": True, "local_path": made}
    _make("download_model", {"file_name": "pendant_lamp_shade.stl", "brief_id": "goal-1"}, env, made, brief_id="goal-1")
    tool, sent = s.asks[0]
    assert len(s.asks) == 1 and sent["goal_id"] == "goal-1" and sent["_asked_by_user"] is True
    assert sent["source_artifact_token"] == _TOKEN and s.uploads == [made]
    assert "door" not in sent["made_with"]
    assert env[SAVED_GOAL_KEY]["joined"] is True and env[SAVED_GOAL_KEY]["version"] == 2


@pytest.mark.parametrize(
    ("door", "envelope", "has_file"),
    [
        ("download_model", {"success": False, "error": "no"}, True),      # the make failed
        ("download_model", {"success": True}, False),                      # nothing was made
        ("analyze_printability", {"success": True}, True),                 # not a make door
        (None, {"success": True}, True),                                   # outside a tool call
        ("download_model", {"success": True, SAVED_GOAL_KEY: {"brief_id": "g", "joined": True, "note": "x"}}, True),
    ],
)
def test_nothing_is_sent_or_said_for_what_is_not_a_goalless_make(plain_install, servers, made, tmp_path, door, envelope, has_file):
    s = servers(_opened())
    before = dict(envelope)
    mesh = made if has_file else str(tmp_path / "gone.stl")
    if door is None:
        join_goal("", envelope, mesh)
    else:
        _make(door, {}, envelope, mesh)
    assert s.asks == [] and envelope == before


def test_where_kiln_pro_is_installed_the_servers_are_not_asked(servers, made, monkeypatch):
    """The install that holds the front door opens the session itself."""
    import types

    opened: list[dict] = []
    mod = types.ModuleType("kiln_pro.design_brief.explicit_attach")
    mod.attach_brief_id_to_result = lambda *a, **k: False
    mod.open_session_for_make = lambda result, **kw: opened.append(kw) or {
        "brief_id": "local-1", "opened": True, "version": 1,
    }
    pkg = types.ModuleType("kiln_pro.design_brief")
    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief.explicit_attach", mod)
    s = servers(_opened())
    env = {"success": True}
    _make("download_model", {}, env, made)
    assert s.asks == [] and env[SAVED_GOAL_KEY]["brief_id"] == "local-1" and len(opened) == 1


# ---------------------------------------------------------------------------
# The real doors
# ---------------------------------------------------------------------------


def test_the_download_door_tells_the_servers_its_words(plain_install, servers, made, monkeypatch):
    """The image-to-3D download, as its registered function, no goal named."""
    from kiln.generation.base import GenerationResult
    from kiln.plugins.generation_ai_tools import plugin

    class _P:
        name = "fake"
        display_name = "Fake"

        def download_result(self, job_id, output_dir=None):
            return GenerationResult(job_id=job_id, provider="fake", local_path=made, format="stl",
                                    file_size_bytes=Path(made).stat().st_size, prompt="",
                                    source_image="https://example.com/lamp.png")

    monkeypatch.setattr(srv, "_get_generation_provider", lambda name="meshy": _P())
    s = servers(_opened(safety_notes=[_LAMP_NOTE]))
    captured: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    token = tool_context.enter_tool("download_generated_model", {"job_id": "job1"})
    try:
        out = captured["download_generated_model"](job_id="job1")
    finally:
        tool_context.leave_tool(token)
    out = next(x for x in out if isinstance(x, dict)) if isinstance(out, list) else out
    assert out["success"] is True, out
    sessions = [sent for tool, sent in s.asks if tool == "design_session"]
    assert len(sessions) == 1 and sessions[0]["verb"] == "join" and "goal_id" not in sessions[0]
    assert sessions[0]["made_with"]["door"] == "download_generated_model"
    assert sessions[0]["made_with"]["file_name"] == "pendant_lamp_shade.stl"
    assert sessions[0]["_asked_by_user"] is False and s.uploads == []
    assert out[SAVED_GOAL_KEY]["brief_id"] == "session-1"
    assert out["safety_notes"] == [_LAMP_NOTE]


@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_a_compile_through_the_mcp_dispatch_sends_no_source(plain_install, servers):
    """The whole path a person's agent takes: the dispatch names the door,
    the compile makes the file, and the ask carries neither the code nor
    where the file was written."""
    s = servers(_opened())
    code = "// pendant lamp shade\nmodule shade() { cylinder(h = 30, d1 = 40, d2 = 60); }\nshade();"
    out = asyncio.run(
        srv.mcp._tool_manager.call_tool("compile_scad", {"scad_code": code}, context=None, convert_result=False)
    )
    out = next(x for x in out if isinstance(x, dict)) if isinstance(out, list) else out
    assert out.get("success") is True, out
    sessions = [sent for tool, sent in s.asks if tool == "design_session"]
    assert len(sessions) == 1, s.asks
    made_with = sessions[0]["made_with"]
    assert set(made_with) == {"door", "version_id", "file_name"} and made_with["door"] == "compile_scad"
    assert made_with["file_name"] == Path(out["stl_path"]).name
    wire = repr(sessions[0])
    assert "cylinder" not in wire and "pendant" not in wire and str(Path(out["stl_path"]).parent) not in wire
    assert out[SAVED_GOAL_KEY]["brief_id"] == "session-1"


# ---------------------------------------------------------------------------
# The served door itself: an ask nobody made, and scrubbed
# ---------------------------------------------------------------------------


def test_the_ask_is_scrubbed_at_the_served_door_and_bounded(plain_install, made, monkeypatch):
    """Through the real served door, to the bytes: the body is the words,
    with nothing that locates a person or a machine in them."""
    import json
    import urllib.request

    bodies: list[dict] = []
    waits: list[float] = []

    class _Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(_opened()).encode()

    def fake_urlopen(req, timeout=None):
        bodies.append({"url": req.full_url, "body": json.loads(req.data)})
        waits.append(timeout)
        return _Reply()

    monkeypatch.setattr(srv, "_served_bearer", lambda: ("bearer", None))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    env = {"success": True}
    _make(
        "generate_and_print",
        {"prompt": "a pendant lamp shade, ask me at someone@example.com or 192.168.1.20"},
        env, made,
    )
    assert len(bodies) == 1 and bodies[0]["url"].endswith("/api/tools/design_session")
    body = bodies[0]["body"]
    assert set(body) == {"verb", "made_with"} and body["verb"] == "join"
    assert body["made_with"]["door"] == "generate_and_print"
    assert "someone@example.com" not in json.dumps(body) and "192.168.1.20" not in json.dumps(body)
    assert "a pendant lamp shade" in body["made_with"]["prompt"]
    assert waits == [bridge.CONSULT_TIMEOUT_S]
    assert env[SAVED_GOAL_KEY]["brief_id"] == "session-1"


def test_every_cause_has_its_own_line_and_no_code_in_it():
    seen = set()
    for miss in (
        served_answer.Miss("offline", "SERVER_UNREACHABLE", "no route"),
        served_answer.Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED", "wall"),
        served_answer.Miss("signed_out", served_answer.SESSION_EXPIRED_CODE, ""),
        served_answer.Miss("unanswered", "SERVER_UNREACHABLE", "timed out"),
        served_answer.Miss("refused", "QUOTA", "No more today."),
        served_answer.Miss("refused", "WEIRD_CODE", ""),
    ):
        text = bridge.line_for(miss)
        assert text == " ".join(text.split()) and text.endswith(".")
        if miss.cause != "signed_out":
            # The voice every unanswered ask of Kiln's servers is worded in.
            for anchor in ("Kiln can't", "right now because", ", so it"):
                assert anchor in text
        assert "none was looked for" in text
        assert "kiln-pro" not in text.lower() and "QUOTA" not in text and "WEIRD_CODE" not in text
        assert "KILN_" not in text and "requires" not in text.lower() and "upgrade" not in text.lower()
        seen.add(text)
    assert len(seen) == 6
