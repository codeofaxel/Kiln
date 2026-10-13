"""``design_session`` is the front door for making an object -- and every make
door says so, on this install, in words an agent reads, and can JOIN the goal
it names.

Two anchors, both the live registry, never a hand list:

* every tool that LOOKS like a make door (a stage tool that takes no file in,
  or a make-shaped name) is in ``MAKE_DOOR_TOOLS`` or recorded in
  ``NOT_A_MAKE_DOOR`` with a reason -- a new make tool with no decision fails
  here at conception;
* every make door takes ``brief_id`` (the goal's id) or says in
  ``GOAL_NAMED_ELSEWHERE`` where the goal is named -- a make door that cannot
  join a goal fails here at conception.  "An imported part joins the goal
  too" is a property of the roster, not a sentence.
"""

from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from kiln import front_door, server
from kiln.front_door import (
    ENGINE_CLAUSE,
    ENGINE_CLAUSE_HEAD,
    FALLBACK_CLAUSE,
    FALLBACK_LOOP,
    FRONT_DOOR,
    FRONT_DOOR_FALLBACK_LINE,
    FRONT_DOOR_LINE,
    GOAL_NAMED_ELSEWHERE,
    GOAL_NOTES,
    GOAL_PARAM,
    MAKE_DOOR_MARKER,
    MAKE_DOOR_TOOLS,
    NOT_A_MAKE_DOOR,
    SAVED_GOAL_KEY,
    front_door_line,
    front_door_present,
    join_goal,
    make_door_candidates,
    record_inferred_goal,
    make_door_clause,
    registry_params,
    takes_a_file,
    with_make_door_clause,
)
from kiln.mcp_compat import FastMCP

_MANIFEST = Path(server.__file__).parent / "pro_tool_manifest.json"


def _manifest_names() -> set[str]:
    raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    tools = raw["tools"] if isinstance(raw, dict) else raw
    return {t["name"] for t in tools}


# ---------------------------------------------------------------------------
# Anchor 1: every make-shaped tool in the live registry has a decision.
# ---------------------------------------------------------------------------


class TestEveryMakeDoorIsDecided:
    def test_candidates_are_derived_from_the_registry(self):
        params = registry_params(server.mcp)
        assert len(params) > 100, "the live registry did not load"
        candidates = make_door_candidates(params)
        # The incident's door and the fast paths are candidates by derivation.
        assert FALLBACK_LOOP in candidates
        assert "generate_from_template" in candidates
        assert "import_step_file" in candidates
        assert FRONT_DOOR not in candidates

    def test_every_candidate_is_a_make_door_or_says_why_not(self):
        candidates = make_door_candidates(registry_params(server.mcp))
        undecided = sorted(candidates - MAKE_DOOR_TOOLS - set(NOT_A_MAKE_DOOR))
        assert not undecided, (
            f"make-shaped tools with no front-door decision: {undecided} -- add each "
            "to kiln.front_door.MAKE_DOOR_TOOLS (it births geometry; the clause is "
            "stamped) or to NOT_A_MAKE_DOOR with the reason it is not a make"
        )

    def test_no_tool_is_both_a_make_door_and_not_one(self):
        both = sorted(MAKE_DOOR_TOOLS & set(NOT_A_MAKE_DOOR))
        assert not both, both
        assert FRONT_DOOR not in MAKE_DOOR_TOOLS
        assert FRONT_DOOR not in NOT_A_MAKE_DOOR

    def test_every_row_names_a_tool_that_exists(self):
        """A row for a tool that is gone is a decision about nothing.  Judged
        against this registry plus the kiln-pro manifest, because the roster
        names both repos' tools and a plain install registers only the
        served kiln-pro ones as stubs."""
        known = set(registry_params(server.mcp)) | _manifest_names()
        rows = MAKE_DOOR_TOOLS | set(NOT_A_MAKE_DOOR) | set(GOAL_NAMED_ELSEWHERE)
        stale = sorted(rows - known)
        assert not stale, f"front-door rows naming no registered tool: {stale}"

    def test_every_reason_is_written(self):
        empty = sorted(n for n, why in NOT_A_MAKE_DOOR.items() if not str(why).strip())
        assert not empty, empty

    def test_the_gate_can_fail(self, monkeypatch):
        """A make door struck from the roster is reported, not forgiven."""
        params = registry_params(server.mcp)
        monkeypatch.setattr(front_door, "MAKE_DOOR_TOOLS", MAKE_DOOR_TOOLS - {FALLBACK_LOOP})
        candidates = make_door_candidates(params)
        undecided = candidates - front_door.MAKE_DOOR_TOOLS - set(NOT_A_MAKE_DOOR)
        assert undecided == {FALLBACK_LOOP}


# ---------------------------------------------------------------------------
# Anchor 2: every make door can join the goal, or says where it is named.
# ---------------------------------------------------------------------------


class TestEveryMakeDoorJoinsTheGoal:
    def test_every_registered_make_door_takes_the_goal_or_names_it_elsewhere(self):
        params = registry_params(server.mcp)
        doors = sorted(n for n in MAKE_DOOR_TOOLS if n in params)
        assert len(doors) >= 30, doors
        goalless = [
            n for n in doors if GOAL_PARAM not in params[n] and n not in GOAL_NAMED_ELSEWHERE
        ]
        assert not goalless, (
            f"make doors that cannot join a saved goal: {goalless} -- add a "
            f"`{GOAL_PARAM}` parameter that calls kiln.front_door.join_goal on success, "
            "or record in GOAL_NAMED_ELSEWHERE where the goal is named instead"
        )

    def test_a_door_named_elsewhere_really_lacks_the_parameter(self):
        """A row that says 'named elsewhere' for a door that DOES take the goal
        is stale: the door would carry the wrong tail."""
        params = registry_params(server.mcp)
        wrong = [n for n in GOAL_NAMED_ELSEWHERE if n in params and GOAL_PARAM in params[n]]
        assert not wrong, wrong
        not_make = sorted(set(GOAL_NAMED_ELSEWHERE) - MAKE_DOOR_TOOLS)
        assert not not_make, not_make

    def test_the_incidents_door_and_the_imports_take_the_goal(self):
        params = registry_params(server.mcp)
        for name in ("compile_scad", "import_step_file", "download_model", "generate_from_template"):
            assert GOAL_PARAM in params[name], name

    def test_the_gate_can_fail(self, monkeypatch):
        params = dict(registry_params(server.mcp))
        params["compile_scad"] = [p for p in params["compile_scad"] if p != GOAL_PARAM]
        goalless = [
            n for n in MAKE_DOOR_TOOLS
            if n in params and GOAL_PARAM not in params[n] and n not in GOAL_NAMED_ELSEWHERE
        ]
        assert goalless == ["compile_scad"]


class TestDerivation:
    def test_an_input_path_counts_and_an_output_path_does_not(self):
        assert takes_a_file(["file_path"])
        assert takes_a_file(["mesh_path", "design_id"])
        assert takes_a_file(["file_paths"])
        assert not takes_a_file(["output_path", "output_dir", "dest_dir"])
        assert not takes_a_file(["scad_code", "timeout"])
        # Source is what a make is born from, not a file it works on.
        assert not takes_a_file(["scad_code", "scad_path", "timeout", "fastener"])

    def test_stage_tools_without_a_file_and_make_shaped_names(self):
        params = {
            "compile_scad": ["scad_code"],
            "repair_mesh": ["file_path"],
            "generate_thing": ["file_path"],
            "import_thing": ["file_path"],
            "estimate_cost": [],
            FRONT_DOOR: ["verb"],
        }
        got = make_door_candidates(params, stage_tools={"compile_scad", "repair_mesh", FRONT_DOOR})
        assert got == {"compile_scad", "generate_thing", "import_thing"}


# ---------------------------------------------------------------------------
# The stamp: the clause lands on make doors, with this install's wording.
# ---------------------------------------------------------------------------


def _registry(*, front_door_module: str | None):
    mcp = FastMCP("t")

    @mcp.tool(name="compile_scad")
    def compile_scad(scad_code: str = "", brief_id: str = "") -> dict:
        """Compile OpenSCAD code into an STL file."""
        return {"success": True}

    @mcp.tool(name="repair_mesh")
    def repair_mesh(file_path: str) -> dict:
        """Repair a mesh."""
        return {"success": True}

    @mcp.tool(name="generate_model")
    def generate_model(prompt: str) -> dict:
        """Start a cloud generation job."""
        return {"success": True}

    if front_door_module is not None:

        @mcp.tool(name=FRONT_DOOR)
        def design_session(verb: str = "start") -> dict:
            """Conversational design generator."""
            return {"success": True}

        design_session.__module__ = front_door_module
        mcp._tool_manager._tools[FRONT_DOOR].fn.__module__ = front_door_module
    return mcp


def _desc(mcp, name: str) -> str:
    return mcp._tool_manager._tools[name].description or ""


class TestStamp:
    def test_real_front_door_present_stamps_the_engine_clause(self):
        mcp = _registry(front_door_module="a_real_plugin")
        assert front_door_present(mcp)
        out = front_door.install(mcp)
        assert out == {"front_door_present": True, "stamped": 2}
        assert _desc(mcp, "compile_scad").endswith(ENGINE_CLAUSE)
        assert _desc(mcp, "compile_scad").startswith("Compile OpenSCAD code")
        assert MAKE_DOOR_MARKER not in _desc(mcp, "repair_mesh")
        assert MAKE_DOOR_MARKER not in _desc(mcp, FRONT_DOOR)

    def test_a_door_that_names_the_goal_elsewhere_gets_its_own_tail(self):
        mcp = _registry(front_door_module="a_real_plugin")
        front_door.install(mcp)
        desc = _desc(mcp, "generate_model")
        assert desc.endswith(make_door_clause(True, "generate_model"))
        assert ENGINE_CLAUSE_HEAD in desc
        assert "download_generated_model" in desc
        assert "Then make here with" not in desc  # its goal is named on another door

    def test_stamp_is_idempotent(self):
        mcp = _registry(front_door_module="a_real_plugin")
        front_door.install(mcp)
        once = _desc(mcp, "compile_scad")
        assert front_door.install(mcp)["stamped"] == 0
        assert _desc(mcp, "compile_scad") == once
        assert once.count(MAKE_DOOR_MARKER) == 1

    def test_no_front_door_means_compile_scad_is_the_loop(self):
        mcp = _registry(front_door_module=None)
        assert not front_door_present(mcp)
        front_door.install(mcp)
        desc = _desc(mcp, "compile_scad")
        assert desc.endswith(FALLBACK_CLAUSE)
        assert f"`{FALLBACK_LOOP}` IS the make" in desc
        assert "not installed here" in desc

    def test_a_stub_is_not_the_front_door(self):
        """A plain install registers design_session as a stub that runs on
        Kiln's servers for a signed-in person; the install speaks the
        fallback, and a stub description that arrived carrying the hosted
        wording is brought into the same voice."""
        mcp = _registry(front_door_module="kiln.pro_stubs")

        @mcp.tool(name="generate_coaster")
        def generate_coaster(top: str = "", brief_id: str = "") -> dict:
            return {"success": True}

        mcp._tool_manager._tools["generate_coaster"].fn.__module__ = "kiln.pro_stubs"
        mcp._tool_manager._tools["generate_coaster"].description = (
            f"Generate a coaster. {ENGINE_CLAUSE}\n\nFree tier: 3 coasters/month."
        )
        hosted_wording = _desc(mcp, "generate_coaster")
        assert not front_door_present(mcp)
        out = front_door.install(mcp)
        assert out["stamped"] == 3
        coaster = _desc(mcp, "generate_coaster")
        assert ENGINE_CLAUSE not in coaster
        assert FALLBACK_CLAUSE in coaster
        assert coaster.count(MAKE_DOOR_MARKER) == 1
        assert coaster.endswith("Free tier: 3 coasters/month.")
        # Only the clause moved: what stood before it, the space included, stays.
        assert coaster == hosted_wording.replace(ENGINE_CLAUSE, FALLBACK_CLAUSE)
        assert _desc(mcp, "compile_scad").endswith(FALLBACK_CLAUSE)

    def test_non_make_doors_are_left_alone(self):
        assert with_make_door_clause("repair_mesh", "Repair a mesh.") == "Repair a mesh."
        assert with_make_door_clause("compile_scad", "") == ENGINE_CLAUSE

    def test_install_never_raises(self):
        assert front_door.install(object()) == {"front_door_present": False, "stamped": 0}


# ---------------------------------------------------------------------------
# Joining a goal: the one helper, honest in every posture.
# ---------------------------------------------------------------------------


def _fake_attach(monkeypatch, outcome):
    """Stand in kiln-pro's join: ``outcome`` is what attach returns, or an
    exception to raise."""
    mod = types.ModuleType("kiln_pro.design_brief.explicit_attach")
    calls: list[tuple[str, dict]] = []

    def attach_brief_id_to_result(brief_id, result):
        calls.append((brief_id, dict(result)))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    mod.attach_brief_id_to_result = attach_brief_id_to_result
    pkg = types.ModuleType("kiln_pro.design_brief")
    pkg.explicit_attach = mod
    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief.explicit_attach", mod)
    return calls


class _NoKilnPro:
    def find_spec(self, name, path=None, target=None):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError("kiln_pro is not installed")
        return None


class TestJoinGoal:
    def test_no_goal_on_a_plain_install_leaves_the_envelope_untouched(self, monkeypatch):
        """Without the real front door there is nothing to point at; the
        fallback clause already says compile_scad is the loop here."""
        for key in [k for k in sys.modules if k == "kiln_pro" or k.startswith("kiln_pro.")]:
            monkeypatch.delitem(sys.modules, key)
        monkeypatch.setattr(sys, "meta_path", [_NoKilnPro(), *sys.meta_path])
        env = {"success": True, "stl_path": "/x.stl"}
        assert join_goal("", env, "/x.stl") is env
        assert SAVED_GOAL_KEY not in env

    def test_an_inferred_goal_replaces_the_nudge(self, monkeypatch):
        """Kiln's hosted service infers and keeps a goal for a make that named
        none; the reply must then say which goal, not that none was kept."""
        _fake_attach(monkeypatch, True)
        env = {"success": True}
        join_goal("", env, "/x.stl")
        assert env[SAVED_GOAL_KEY]["note"] == GOAL_NOTES["none_named"]
        record_inferred_goal(env, "g-inferred")
        assert env[SAVED_GOAL_KEY] == {
            "brief_id": "g-inferred",
            "joined": True,
            "inferred": True,
            "note": GOAL_NOTES["inferred"].format(brief_id="g-inferred"),
        }
        assert 'goal_id="g-inferred"' in env[SAVED_GOAL_KEY]["note"]
        assert record_inferred_goal({"success": True}, "") == {"success": True}

    def test_no_goal_where_the_front_door_is_installed_says_what_was_skipped(self, monkeypatch):
        """The incident's shape: a make with no goal named.  One line, at the
        moment it was skipped, never a refusal."""
        calls = _fake_attach(monkeypatch, True)
        env = {"success": True, "stl_path": "/x.stl"}
        join_goal("", env, "/x.stl")
        assert env[SAVED_GOAL_KEY] == {
            "brief_id": "", "joined": False, "note": GOAL_NOTES["none_named"],
        }
        assert FRONT_DOOR in env[SAVED_GOAL_KEY]["note"]
        assert GOAL_PARAM in env[SAVED_GOAL_KEY]["note"]
        assert calls == []  # nothing to join

    def test_the_nudge_never_rides_a_failure_or_a_second_time(self, monkeypatch):
        _fake_attach(monkeypatch, True)
        failed = {"success": False, "error": "no"}
        assert SAVED_GOAL_KEY not in join_goal("", failed)
        said = {"success": True, SAVED_GOAL_KEY: {"brief_id": "g", "joined": True, "note": "x"}}
        join_goal("", said)
        assert said[SAVED_GOAL_KEY]["brief_id"] == "g"

    def test_joined(self, monkeypatch, tmp_path):
        calls = _fake_attach(monkeypatch, True)
        env = {"success": True, "path": str(tmp_path / "a.stl")}
        join_goal("g1", env, env["path"])
        assert env[SAVED_GOAL_KEY] == {
            "brief_id": "g1", "joined": True, "note": GOAL_NOTES["joined"].format(brief_id="g1"),
        }
        # The file is handed to the join under the key it reads, whatever
        # key the door kept it under.
        assert calls[0][1]["local_path"] == env["path"]

    def test_not_found(self, monkeypatch, tmp_path):
        _fake_attach(monkeypatch, False)
        env = {"success": True}
        join_goal("g1", env, str(tmp_path / "a.stl"))
        assert env[SAVED_GOAL_KEY]["joined"] is False
        assert "stands on its own" in env[SAVED_GOAL_KEY]["note"]

    def test_an_already_recorded_make_reads_as_joined(self, monkeypatch, tmp_path):
        """The join is idempotent: a second call for the same file returns
        False, and a goal sidecar beside the file is the proof it was done."""
        _fake_attach(monkeypatch, False)
        mesh = tmp_path / "a.stl"
        mesh.write_bytes(b"solid\nendsolid\n")
        (tmp_path / "a.stl.intent.json").write_text("{}")
        env = {"success": True}
        join_goal("g1", env, str(mesh))
        assert env[SAVED_GOAL_KEY]["joined"] is True

    def test_a_failing_join_never_costs_the_make(self, monkeypatch, tmp_path):
        _fake_attach(monkeypatch, RuntimeError("store down"))
        env = {"success": True}
        assert join_goal("g1", env, str(tmp_path / "a.stl")) is env
        assert env[SAVED_GOAL_KEY]["joined"] is False

    def test_a_plain_install_says_goals_live_with_the_front_door(self, monkeypatch, tmp_path):
        for key in [k for k in sys.modules if k == "kiln_pro" or k.startswith("kiln_pro.")]:
            monkeypatch.delitem(sys.modules, key)
        monkeypatch.setattr(sys, "meta_path", [_NoKilnPro(), *sys.meta_path])
        env = {"success": True}
        join_goal("g1", env, str(tmp_path / "a.stl"))
        assert env[SAVED_GOAL_KEY]["joined"] is False
        assert "not installed here" in env[SAVED_GOAL_KEY]["note"]
        assert FRONT_DOOR in env[SAVED_GOAL_KEY]["note"]


@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_the_incidents_door_carries_the_goal_through_the_real_tool(monkeypatch, tmp_path):
    """``compile_scad(brief_id=...)`` through the registered tool: the make
    is real and the envelope says what became of the goal id."""
    calls = _fake_attach(monkeypatch, True)
    tool = server.mcp._tool_manager._tools["compile_scad"]
    res = tool.fn(scad_code="cube(20);", brief_id="goal-abc")
    assert res.get("success") is True, res
    assert res[SAVED_GOAL_KEY]["joined"] is True and res[SAVED_GOAL_KEY]["brief_id"] == "goal-abc"
    assert calls and calls[0][0] == "goal-abc"
    assert calls[0][1]["local_path"] == res["stl_path"]


# ---------------------------------------------------------------------------
# The words: one home, true at every tier.
# ---------------------------------------------------------------------------


class TestWords:
    def test_both_clauses_name_the_front_door_and_open_with_the_marker(self):
        for clause in (ENGINE_CLAUSE, FALLBACK_CLAUSE):
            assert clause.startswith(MAKE_DOOR_MARKER)
            assert FRONT_DOOR in clause
        assert FALLBACK_LOOP in FALLBACK_CLAUSE
        assert GOAL_PARAM in ENGINE_CLAUSE and "imported" in ENGINE_CLAUSE
        # Both spellings, so the agent is carried from design_session's goal_id
        # to the make door's brief_id.
        assert "goal_id" in ENGINE_CLAUSE and "goal_id" in FRONT_DOOR_LINE
        assert "goal_id" in GOAL_NOTES["none_named"]

    def test_the_paid_check_is_said_as_paid_and_the_goal_as_free(self):
        """Checking a result against the goal is a Kiln Pro feature;
        keeping the goal is not.  The words say which is which."""
        assert "on Kiln Pro checks the result against that goal" in ENGINE_CLAUSE_HEAD
        assert "on Kiln Pro checks the result against that goal" in FRONT_DOOR_LINE
        assert "version history" not in ENGINE_CLAUSE_HEAD  # a paid feature, not promised here

    def test_every_door_named_elsewhere_has_its_own_tail(self):
        for name, tail in GOAL_NAMED_ELSEWHERE.items():
            clause = make_door_clause(True, name)
            assert clause == f"{ENGINE_CLAUSE_HEAD} {tail}"
            assert GOAL_PARAM in tail, name

    def test_instruction_line_says_the_fallback_only_when_the_door_is_absent(self):
        assert front_door_line(True) == FRONT_DOOR_LINE
        assert front_door_line(False) == f"{FRONT_DOOR_LINE} {FRONT_DOOR_FALLBACK_LINE}"
        assert f"`{FRONT_DOOR}`" in FRONT_DOOR_LINE
        assert FALLBACK_LOOP in FRONT_DOOR_FALLBACK_LINE


# ---------------------------------------------------------------------------
# The instructions: the make story opens with the front door.
# ---------------------------------------------------------------------------


class TestInstructions:
    def test_built_instructions_carry_the_line_for_this_install(self):
        text = server._build_instructions()
        assert front_door_line(front_door_present(server.mcp)) in text
        assert text.count("MAKE: a make starts with") == 1

    def test_the_line_precedes_the_first_make_tool_named(self):
        text = server._build_instructions()
        assert text.index(FRONT_DOOR_LINE) < text.index("`generate_from_template(")

    def test_static_fallback_instructions_carry_the_line(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        assert '+ "\\n\\n" + _FRONT_DOOR_LINE' in src

    @pytest.mark.parametrize("present", [True, False])
    def test_line_is_one_wording_for_every_surface(self, present):
        # The hosted connector imports the same function; a reword here is a
        # reword there.
        assert front_door_line(present).startswith("MAKE: a make starts with `design_session`")
