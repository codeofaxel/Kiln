"""An imported file with separate parts moves on a plain install.

On an install without kiln-pro (every customer's), a model from outside Kiln
that holds two or more separate parts goes to Kiln's servers as a copy,
stating nothing about how it moves; what plays it comes back in the answer
and is kept beside the file here, so the stage on this computer plays it.
The person is told once what was sent and how to stop it.  Kiln Business
and Enterprise keep their files here until they turn it on, and are told
once how.  A model in one piece is never sent for this.

Walked with kiln-pro removed from the import system and Kiln's servers stood
in at the one served door, the seam ``test_outside_model_opens_a_goal.py``
uses; the pieces are counted with scipy hidden too, since a plain install
has none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

import kiln.server as srv  # noqa: E402
from kiln import import_motion, mechanism_stage, served_makes  # noqa: E402
from kiln.account_plan import LicenseTier  # noqa: E402
from kiln.arrival import DOWNLOADED, Arrival, record  # noqa: E402
from kiln.front_door import GOAL_NOTES, SAVED_GOAL_KEY, join_goal  # noqa: E402

_TOKEN = "tok_" + "c" * 28
_WORK_IT_OUT = {"statement": None, "note": "reply"}
_NOTE = {"kind": mechanism_stage.KIND, "playable": True, "headline": "It moves 2 parts."}


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def without_scipy(monkeypatch):
    """What ``pip install kiln3d`` has: neither scipy nor networkx, which are
    developer packages only.  Both are hidden from anything that imports them
    now, and trimesh's own references, taken when it was first imported, are
    swapped for the stand-ins it keeps when they are missing."""
    from trimesh import exceptions
    from trimesh import graph as trimesh_graph

    for name in [n for n in sys.modules if n == "scipy" or n.startswith("scipy.")] + ["scipy", "networkx"]:
        monkeypatch.setitem(sys.modules, name, None)
    missing = exceptions.ExceptionWrapper(ImportError("not installed on a plain install"))
    for name in ("coo_matrix", "csgraph", "nx"):
        monkeypatch.setattr(trimesh_graph, name, missing, raising=False)


@pytest.fixture(autouse=True)
def fresh_choice():
    """Every test starts with no choice made and nothing told."""
    from kiln.persistence import get_db

    db = get_db()
    for key in (import_motion.SETTING_KEY, import_motion.TOLD_SENT_KEY, import_motion.TOLD_OFF_KEY):
        db.set_setting(key, "")
    yield


@pytest.fixture
def plan(monkeypatch):
    def hold(tier: LicenseTier) -> None:
        monkeypatch.setattr("kiln.account_plan.get_tier", lambda: tier)

    hold(LicenseTier.FREE)
    return hold


def _downloaded(folder: Path, name: str, *, pieces: int) -> str:
    """A marketplace download: *pieces* cubes apart from one another."""
    cubes = [trimesh.creation.box((10, 10, 10)).apply_translation((30 * n, 0, 5)) for n in range(pieces)]
    path = folder / name
    trimesh.util.concatenate(cubes).export(str(path))
    record(str(path), Arrival(kind=DOWNLOADED, by="MakerWorld", name="gearbox", model_id="m-1"))
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
        return dict(self.answer)

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


def _answer(*, plays: bool = True) -> dict:
    note = GOAL_NOTES["opened"].format(brief_id="goal-7", version=1)
    out = {
        "status": "success",
        SAVED_GOAL_KEY: {"brief_id": "goal-7", "joined": True, "opened": True, "version": 1, "note": note},
    }
    if plays:
        out["motion"] = {
            "plays": True, "says": "Kiln worked out how this moves from its shape. It moves 2 parts.",
            "worked_out": True, "guess": False, "note": dict(_NOTE),
        }
    return out


# ---------------------------------------------------------------------------
# Sent, played here, told once
# ---------------------------------------------------------------------------


def test_an_outside_file_with_separate_parts_plays_here_and_the_person_is_told_once(
        plain_install, servers, plan, tmp_path):
    s = servers(_answer())
    first = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": first}
    join_goal("", env, first)

    # The copy goes, stating nothing about how it moves, and asks for what
    # plays it back.  Nobody asked for this send: it is Kiln's own.
    assert s.uploads == [first]
    tool, sent = s.asks[0]
    assert tool == "design_session" and sent["verb"] == "join"
    assert sent["motion"] == _WORK_IT_OUT and sent["source_artifact_token"] == _TOKEN
    assert sent["_asked_by_user"] is False
    # What the servers worked out is said, and what plays it is kept here.
    said = env["motion"]
    assert said["plays"] is True and said["worked_out"] is True and said["guess"] is False
    assert said["on_this_computer"] is True
    assert mechanism_stage.block_for(first) == _NOTE
    assert env[import_motion.REPLY_KEY] == import_motion.SENT_LINE

    # The next file goes the same way, and nothing is said about the copy.
    second = _downloaded(tmp_path, "hinge.stl", pieces=2)
    env2 = {"success": True, "local_path": second}
    join_goal("", env2, second)
    assert s.uploads == [first, second]
    assert import_motion.REPLY_KEY not in env2
    assert mechanism_stage.block_for(second) == _NOTE


@pytest.mark.parametrize(("pieces", "separate"), [(3, True), (2, True), (1, False)])
def test_the_pieces_are_counted_without_scipy(without_scipy, tmp_path, pieces, separate):
    """A plain install has no scipy; the count still answers, and a file in
    one piece still holds none."""
    path = _downloaded(tmp_path, f"model_{pieces}.stl", pieces=pieces)
    assert import_motion.has_separate_parts(path) is separate


def test_an_outside_file_with_separate_parts_plays_here_without_scipy(
        plain_install, without_scipy, servers, plan, tmp_path):
    """The whole door, on the install a customer has: the copy goes, what the
    servers worked out plays here, and the person is told once."""
    s = servers(_answer())
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == [path]
    _tool, sent = s.asks[0]
    assert sent["motion"] == _WORK_IT_OUT and sent["source_artifact_token"] == _TOKEN
    assert env["motion"]["plays"] is True
    assert mechanism_stage.block_for(path) == _NOTE
    assert env[import_motion.REPLY_KEY] == import_motion.SENT_LINE


@pytest.mark.parametrize("tier", [LicenseTier.BUSINESS, LicenseTier.ENTERPRISE])
def test_business_is_told_how_to_turn_it_on_without_scipy(
        plain_install, without_scipy, servers, plan, tmp_path, tier):
    plan(tier)
    s = servers(_answer(plays=False))
    path = _downloaded(tmp_path, "fixture.stl", pieces=2)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == []
    assert env[import_motion.REPLY_KEY] == import_motion.OFF_LINE


def test_the_line_says_what_was_sent_where_it_goes_and_how_to_stop_it():
    line = import_motion.SENT_LINE
    assert "copy" in line and "servers" in line
    assert "isn't one of your designs" in line and "within about a month" in line
    assert f'"{import_motion.SAY_OFF}"' in line


def test_a_file_in_one_piece_is_never_sent_for_this(plain_install, servers, plan, tmp_path):
    s = servers(_answer(plays=False))
    path = _downloaded(tmp_path, "vase.stl", pieces=1)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == []
    _tool, sent = s.asks[0]
    assert "motion" not in sent and "source_artifact_token" not in sent
    assert import_motion.REPLY_KEY not in env and "motion" not in env


# ---------------------------------------------------------------------------
# The plan's default and the person's own choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [LicenseTier.BUSINESS, LicenseTier.ENTERPRISE])
def test_business_and_enterprise_keep_files_here_and_are_told_once_how_to_turn_it_on(
        plain_install, servers, plan, tmp_path, tier):
    plan(tier)
    s = servers(_answer(plays=False))
    first = _downloaded(tmp_path, "fixture.stl", pieces=2)
    env = {"success": True, "local_path": first}
    join_goal("", env, first)
    assert s.uploads == []
    _tool, sent = s.asks[0]
    assert "motion" not in sent and "source_artifact_token" not in sent
    assert env[import_motion.REPLY_KEY] == import_motion.OFF_LINE
    assert f'"{import_motion.SAY_ON}"' in import_motion.OFF_LINE

    second = _downloaded(tmp_path, "jig.stl", pieces=2)
    env2 = {"success": True, "local_path": second}
    join_goal("", env2, second)
    assert s.uploads == [] and import_motion.REPLY_KEY not in env2


def test_a_person_who_turns_it_off_keeps_files_here_and_hears_nothing_more(
        plain_install, servers, plan, tmp_path):
    # Servers asked with no file have nothing to work out.
    s = servers(_answer(plays=False))
    import_motion.set_sending(False)
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == []
    assert import_motion.REPLY_KEY not in env and "motion" not in env


def test_a_business_person_who_turns_it_on_sends_and_is_told_what_was_sent(
        plain_install, servers, plan, tmp_path):
    plan(LicenseTier.BUSINESS)
    s = servers(_answer())
    import_motion.set_sending(True)
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == [path]
    assert env[import_motion.REPLY_KEY] == import_motion.SENT_LINE


def test_a_signed_out_install_sends_nothing_and_says_nothing_about_a_copy(
        plain_install, servers, plan, tmp_path):
    s = servers(_answer(), bearer=(None, None))
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert s.uploads == [] and s.asks == []
    assert import_motion.REPLY_KEY not in env and "motion" not in env


def test_when_the_servers_do_not_answer_nothing_is_said_about_a_copy(plain_install, servers, plan, tmp_path):
    s = servers(ConnectionError("unreachable"))
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("", env, path)
    assert import_motion.REPLY_KEY not in env and "motion" not in env
    assert mechanism_stage.block_for(path) is None
    del s


def test_a_goal_the_person_named_sends_the_file_as_before_and_asks_for_its_motion(
        plain_install, servers, plan, tmp_path):
    s = servers(_answer())
    path = _downloaded(tmp_path, "gearbox.stl", pieces=3)
    env = {"success": True, "local_path": path}
    join_goal("goal-7", env, path)
    assert s.uploads == [path]
    _tool, sent = s.asks[0]
    assert sent["goal_id"] == "goal-7" and sent["motion"] == _WORK_IT_OUT
    # Their own ask sent the file: nothing to tell about a copy.
    assert import_motion.REPLY_KEY not in env
    assert mechanism_stage.block_for(path) == _NOTE


# ---------------------------------------------------------------------------
# The person's door
# ---------------------------------------------------------------------------


def _tool():
    from kiln.plugins.import_motion_tools import plugin

    registered = {}

    class _MCP:
        def tool(self, **_kwargs):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_MCP())
    return registered["motion_for_imports"]


def test_the_person_can_read_and_change_the_choice_in_their_own_words(plan):
    motion_for_imports = _tool()
    now = motion_for_imports()
    assert now["success"] is True and now["sending"] is True and now["chosen_here"] is False
    assert f'"{import_motion.SAY_OFF}"' in now["says"]

    off = motion_for_imports(on=False)
    assert off["sending"] is False and off["chosen_here"] is True
    assert f'"{import_motion.SAY_ON}"' in off["says"]
    assert import_motion.sending() is False

    plan(LicenseTier.ENTERPRISE)
    assert motion_for_imports(on=True)["sending"] is True


def test_a_choice_that_cannot_be_kept_is_said_and_changes_nothing(monkeypatch):
    motion_for_imports = _tool()

    def refuse(_on):
        raise OSError("read-only disk")

    monkeypatch.setattr(import_motion, "set_sending", refuse)
    answer = motion_for_imports(on=False)
    assert answer["success"] is False and answer["code"] == "NOT_KEPT"
    assert "Nothing changed" in answer["error"]
