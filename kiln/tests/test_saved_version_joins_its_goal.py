"""A design version saved on an install without kiln-pro joins its goal.

``save_design_version`` keeps the version on this computer.  When it names a
goal, or its design's earlier version did, and it was saved from a model
file, the version is sent to that goal in the person's Kiln account through
the same step a make that names its goal takes.  The reply carries what the
servers recorded and the goal's safety notes; for a model with separate
parts, what plays it comes back and is kept beside the copy the design's
history keeps.  A miss leaves the version saved as asked and says so once.
With kiln-pro on this computer the save does that work itself and sends
nothing.

Walked through the registered tool with kiln-pro removed from the import
system and Kiln's servers stood in at the one served door
(``kiln.server._pro_api_call``).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import types
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

import kiln.server as srv  # noqa: E402
from kiln import import_motion, make_words, mechanism_stage, served_makes, served_public  # noqa: E402
from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY  # noqa: E402
from kiln.reference_compare import NOT_COMPARED, REFERENCE_KEY  # noqa: E402

_TOKEN = "tok_" + "s" * 28
_WORK_IT_OUT = {"statement": None, "note": "reply"}
_NOTE = {"kind": mechanism_stage.KIND, "playable": True, "headline": "It moves 2 parts."}
_FLAME = "Keep the flame in a metal or glass cup and never leave it burning unattended."


@pytest.fixture(autouse=True)
def _a_fresh_session():
    served_public._reset_for_tests()
    make_words._reset_for_tests()
    yield
    served_public._reset_for_tests()
    make_words._reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_choice():
    from kiln.persistence import get_db

    db = get_db()
    for key in (import_motion.SETTING_KEY, import_motion.TOLD_SENT_KEY, import_motion.TOLD_OFF_KEY):
        db.set_setting(key, "")
    yield


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def library(monkeypatch, tmp_path) -> types.SimpleNamespace:
    """The design library in a folder of the test's own, and the history
    step after a save recorded rather than run (it is not what is tested
    here): each entry says whether a motion note sat beside the kept copy
    when it was called."""
    root = tmp_path / "designs"
    monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(root))
    history: list[dict] = []

    def after_local_save(design_id, **_kw):
        kept = sorted(str(p) for p in (root / design_id).glob("mesh.*.stl"))
        history.append(
            {
                "design_id": design_id,
                "note_beside_kept": any(os.path.isfile(k + mechanism_stage.NOTE_SUFFIX) for k in kept),
            }
        )
        return {}

    monkeypatch.setattr("kiln.history_courier.after_local_save", after_local_save)
    return types.SimpleNamespace(root=root, history=history)


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
    def install(answer, *, signed_in=True, expired=False):
        s = _Servers(answer)
        bearer = ("bearer", None) if signed_in else ("", "Your session ran out." if expired else None)
        monkeypatch.setattr(srv, "_served_bearer", lambda: bearer)
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        monkeypatch.setattr(served_makes, "_upload", s.upload)
        return s

    return install


def _joined(*, plays: bool = True, note: dict | None = None) -> dict:
    out = {
        "status": "success",
        "message": "ok",
        SAVED_GOAL_KEY: {
            "brief_id": "goal-1",
            "joined": True,
            "version": 2,
            "version_id": "lantern-v2",
            "note": GOAL_NOTES["joined"].format(brief_id="goal-1"),
        },
        "safety_notes": [_FLAME],
    }
    if plays:
        out["motion"] = {
            "plays": True,
            "says": "Kiln worked out how this moves from its shape. It moves 2 parts.",
            "worked_out": True,
            "guess": False,
            "note": dict(note or _NOTE),
        }
    return out


def _model(folder: Path, name: str, *, pieces: int) -> str:
    """*pieces* cubes apart from one another, as a file on this computer."""
    cubes = [trimesh.creation.box((10, 10, 10)).apply_translation((30 * n, 0, 5)) for n in range(pieces)]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    trimesh.util.concatenate(cubes).export(str(path))
    return str(path)


def _save(**arguments) -> dict:
    """``save_design_version`` as an agent calls it: through the registry."""
    out = asyncio.run(
        srv.mcp._tool_manager.call_tool("save_design_version", arguments, context=None, convert_result=False)
    )
    return next(x for x in out if isinstance(x, dict)) if isinstance(out, list) else out


def _joins(s: _Servers) -> list[dict]:
    return [sent for tool, sent in s.asks if tool == "design_session" and sent.get("verb") == "join"]


# ---------------------------------------------------------------------------
# Signed in: the version joins its goal, and what plays it is kept here
# ---------------------------------------------------------------------------


def test_a_saved_version_joins_its_goal_and_plays_beside_the_kept_copy(plain_install, library, servers, tmp_path):
    s = servers(_joined())
    handed = _model(tmp_path / "work", "lantern.stl", pieces=2)
    out = _save(design_id="lantern", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")

    assert out["ok"] is True, out
    kept = out["version"]["stl_path"]
    assert kept != handed and Path(kept).parent == library.root / "lantern"

    # The copy the history keeps went to the goal, as the person's own ask,
    # with the ask that what plays it comes back.
    joins = _joins(s)
    assert len(joins) == 1, s.asks
    sent = joins[0]
    assert sent["goal_id"] == "goal-1" and sent["_asked_by_user"] is True
    assert s.uploads == [kept]
    assert sent["source_artifact_token"] == _TOKEN and sent["motion"] == _WORK_IT_OUT
    assert sent["made_with"] == {"file_name": Path(kept).name}
    assert str(tmp_path) not in json.dumps(sent["made_with"])

    # What the servers recorded and the goal's notes are in the reply.
    block = out[SAVED_GOAL_KEY]
    assert block["joined"] is True and block["brief_id"] == "goal-1"
    assert block["joined_on"] == "kiln_servers" and block["version"] == 2
    assert out["safety_notes"] == [_FLAME]

    # What plays it is kept beside the history's copy, not the person's file.
    assert out["motion"]["plays"] is True and out["motion"]["on_this_computer"] is True
    assert mechanism_stage.block_for(kept) == _NOTE
    assert mechanism_stage.block_for(handed) is None
    # And it was there before the history step ran, so a push carries it.
    assert library.history == [{"design_id": "lantern", "note_beside_kept": True}]


def test_a_later_save_joins_the_goal_its_design_already_named(plain_install, library, servers, tmp_path):
    s = servers(_joined(plays=False))
    first = _model(tmp_path / "work", "v1.stl", pieces=1)
    _save(design_id="lantern", scad_source="cube(10);", stl_path=first, goal_id="goal-1")
    second = _model(tmp_path / "work", "v2.stl", pieces=1)
    out = _save(design_id="lantern", scad_source="cube(12);", stl_path=second)

    assert out["ok"] is True and out["version"]["brief_id"] == "goal-1"
    joins = _joins(s)
    assert [j["goal_id"] for j in joins] == ["goal-1", "goal-1"]
    assert s.uploads[-1] == out["version"]["stl_path"]
    assert out[SAVED_GOAL_KEY]["joined"] is True


def test_a_one_piece_model_goes_without_a_motion_ask(plain_install, library, servers, tmp_path):
    s = servers(_joined(plays=False))
    handed = _model(tmp_path / "work", "vase.stl", pieces=1)
    out = _save(design_id="vase", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")
    assert "motion" not in _joins(s)[0]
    assert "motion" not in out and import_motion.REPLY_KEY not in out


def test_a_save_that_names_no_goal_sends_nothing(plain_install, library, servers, tmp_path):
    s = servers(_joined())
    handed = _model(tmp_path / "work", "lantern.stl", pieces=2)
    out = _save(design_id="lantern", scad_source="cube(10);", stl_path=handed)
    assert out["ok"] is True
    assert s.asks == [] and s.uploads == []
    assert SAVED_GOAL_KEY not in out and "motion" not in out


def test_a_save_with_no_model_file_sends_nothing(plain_install, library, servers):
    s = servers(_joined())
    out = _save(design_id="lantern", scad_source="cube(10);", goal_id="goal-1")
    assert out["ok"] is True and out["version"]["brief_id"] == "goal-1"
    assert s.asks == [] and s.uploads == []
    assert SAVED_GOAL_KEY not in out


def test_a_file_that_already_plays_is_not_worked_out_again(plain_install, library, servers, tmp_path):
    """A note bound to the file's bytes (what its make stated, or an earlier
    reading) travels into the kept copy and is never written over."""
    stated = {**_NOTE, "headline": "The lid turns on its hinge."}
    s = servers(_joined(note={**_NOTE, "headline": "Kiln's own reading."}))
    handed = _model(tmp_path / "work", "box.stl", pieces=2)
    assert mechanism_stage.record(handed, stated) is not None
    out = _save(design_id="box", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")

    assert "motion" not in _joins(s)[0]
    kept = out["version"]["stl_path"]
    assert mechanism_stage.block_for(kept) == stated
    assert "motion" not in out
    assert out[SAVED_GOAL_KEY]["joined"] is True


def test_a_goal_not_in_this_account_is_said_for_a_version(plain_install, library, servers, tmp_path):
    servers({"status": "error", "code": "NOT_FOUND", "error": "I don't have that goal."})
    handed = _model(tmp_path / "work", "lantern.stl", pieces=1)
    out = _save(design_id="lantern", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")
    assert out["ok"] is True
    assert out[SAVED_GOAL_KEY]["joined"] is False
    assert out[SAVED_GOAL_KEY]["note"] == GOAL_NOTES["version_not_in_account"].format(brief_id="goal-1")


# ---------------------------------------------------------------------------
# The four misses: the version stands, never read as joined or checked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "why", "says"),
    [
        ({"signed_in": False, "answer": None}, "signed_out", "Kiln is signed out"),
        ({"answer": OSError(errno.ENETUNREACH, "Network is unreachable")}, "offline", "this computer is offline"),
        (
            {"answer": {"status": "error", "code": "SERVER_UNREACHABLE", "error": "down", "retryable": True}},
            "unanswered",
            "Kiln's servers didn't answer",
        ),
        (
            {"answer": {"status": "error", "code": "QUOTA", "error": "No more today."}},
            "refused",
            "Kiln's servers said no",
        ),
    ],
)
def test_every_miss_says_the_version_stands_unjoined_and_unchecked(
    plain_install, library, servers, tmp_path, monkeypatch, setup, why, says
):
    monkeypatch.setattr("kiln.served_answer._route_to", lambda host: False)
    servers(setup.get("answer"), signed_in=setup.get("signed_in", True))
    handed = _model(tmp_path / "work", "lantern.stl", pieces=2)
    out = _save(design_id="lantern", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")

    assert out["ok"] is True and out["version"]["version"] == 1
    block = out[SAVED_GOAL_KEY]
    assert block["joined"] is False and block["why"] == why
    note = block["note"]
    assert says in note
    assert "this version" in note and "did not add it to the goal" in note
    assert "is saved on this computer as asked" in note and "make it again" not in note
    if why == "refused":
        # The servers' own words carry the fix.
        assert "No more today." in note
    else:
        assert "save it again with the same goal" in note
    # Nothing a miss could be read as: no safety note, no motion, no check.
    assert "safety_notes" not in out and "motion" not in out
    assert out[REFERENCE_KEY]["status"] == NOT_COMPARED
    assert mechanism_stage.block_for(out["version"]["stl_path"]) is None
    # Said once: the sentence is not repeated as a warning.
    assert note not in (out.get("warnings") or [])


def test_signed_out_is_said_once_a_session_with_what_signing_in_adds(plain_install, library, servers, tmp_path):
    s = servers(None, signed_in=False)
    first = _save(
        design_id="lantern",
        scad_source="cube(10);",
        stl_path=_model(tmp_path / "work", "a.stl", pieces=1),
        goal_id="goal-1",
    )
    block = first[SAVED_GOAL_KEY]
    assert block["why"] == "signed_out" and block["joined"] is False
    assert block["agent_hint"] and block["setup_hint"]

    later = _save(design_id="lantern", scad_source="cube(12);", stl_path=_model(tmp_path / "work", "b.stl", pieces=1))
    assert later["ok"] is True
    assert SAVED_GOAL_KEY not in later and REFERENCE_KEY not in later
    assert s.asks == [] and s.uploads == []


# ---------------------------------------------------------------------------
# With kiln-pro on this computer the save does this work itself
# ---------------------------------------------------------------------------


def test_with_kiln_pro_installed_a_save_sends_nothing(library, servers, tmp_path, monkeypatch):
    pro = types.ModuleType("kiln_pro")
    bridge = types.ModuleType("kiln_pro.bridge")
    bridge.pro_features = types.SimpleNamespace(enrich_version=lambda version, **_kw: version)
    pro.bridge = bridge
    monkeypatch.setitem(sys.modules, "kiln_pro", pro)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)
    for name in [n for n in sys.modules if n.startswith("kiln_pro.") and n != "kiln_pro.bridge"]:
        monkeypatch.setitem(sys.modules, name, None)
    s = servers(_joined())
    handed = _model(tmp_path / "work", "lantern.stl", pieces=2)
    out = _save(design_id="lantern", scad_source="cube(10);", stl_path=handed, goal_id="goal-1")

    assert out["ok"] is True, out
    assert s.asks == [] and s.uploads == []
    assert SAVED_GOAL_KEY not in out and library.history == []
