"""``design_session`` is the front door for making an object -- and every make
door says so, on this install, in words an agent reads.

The anchor is the live registry, not a hand list: every tool that LOOKS like
a make door (a stage tool that takes no file in, or a make-shaped name) must
be in ``MAKE_DOOR_TOOLS`` or recorded in ``NOT_A_MAKE_DOOR`` with a reason,
so a new make tool with no decision fails here at conception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import front_door, server
from kiln.front_door import (
    ENGINE_CLAUSE,
    FALLBACK_CLAUSE,
    FALLBACK_LOOP,
    FRONT_DOOR,
    FRONT_DOOR_FALLBACK_LINE,
    FRONT_DOOR_LINE,
    MAKE_DOOR_MARKER,
    MAKE_DOOR_TOOLS,
    NOT_A_MAKE_DOOR,
    front_door_line,
    front_door_present,
    make_door_candidates,
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
# The anchor: every make-shaped tool in the live registry has a decision.
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
        stale = sorted((MAKE_DOOR_TOOLS | set(NOT_A_MAKE_DOOR)) - known)
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
    def compile_scad(scad_code: str = "") -> dict:
        """Compile OpenSCAD code into an STL file."""
        return {"success": True}

    @mcp.tool(name="repair_mesh")
    def repair_mesh(file_path: str) -> dict:
        """Repair a mesh."""
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
        assert out == {"front_door_present": True, "stamped": 1}
        assert _desc(mcp, "compile_scad").endswith(ENGINE_CLAUSE)
        assert _desc(mcp, "compile_scad").startswith("Compile OpenSCAD code")
        assert MAKE_DOOR_MARKER not in _desc(mcp, "repair_mesh")
        assert MAKE_DOOR_MARKER not in _desc(mcp, FRONT_DOOR)

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
        assert f"`{FALLBACK_LOOP}` IS the make -> preview -> iterate loop" in desc
        assert "not installed here" in desc

    def test_a_stub_is_not_the_front_door(self):
        """A plain install registers design_session as a stub that runs on
        Kiln's servers for a signed-in person; the install speaks the
        fallback, and a stub description that arrived carrying the hosted
        wording is brought into the same voice."""
        mcp = _registry(front_door_module="kiln.pro_stubs")

        @mcp.tool(name="generate_coaster")
        def generate_coaster(top: str = "") -> dict:
            return {"success": True}

        mcp._tool_manager._tools["generate_coaster"].fn.__module__ = "kiln.pro_stubs"
        mcp._tool_manager._tools["generate_coaster"].description = (
            f"Generate a coaster. {ENGINE_CLAUSE}\n\nFree tier: 3 coasters/month."
        )
        assert not front_door_present(mcp)
        out = front_door.install(mcp)
        assert out["stamped"] == 2
        coaster = _desc(mcp, "generate_coaster")
        assert ENGINE_CLAUSE not in coaster
        assert FALLBACK_CLAUSE in coaster
        assert coaster.count(MAKE_DOOR_MARKER) == 1
        assert coaster.endswith("Free tier: 3 coasters/month.")
        assert _desc(mcp, "compile_scad").endswith(FALLBACK_CLAUSE)

    def test_non_make_doors_are_left_alone(self):
        assert with_make_door_clause("repair_mesh", "Repair a mesh.") == "Repair a mesh."
        assert with_make_door_clause("compile_scad", "") == ENGINE_CLAUSE

    def test_install_never_raises(self):
        assert front_door.install(object()) == {"front_door_present": False, "stamped": 0}


# ---------------------------------------------------------------------------
# The words: one home, both wordings say the same thing about the door.
# ---------------------------------------------------------------------------


class TestWords:
    def test_both_clauses_name_the_front_door_and_open_with_the_marker(self):
        for clause in (ENGINE_CLAUSE, FALLBACK_CLAUSE):
            assert clause.startswith(MAKE_DOOR_MARKER)
            assert FRONT_DOOR in clause
            assert "imported" in clause
        assert FALLBACK_LOOP in FALLBACK_CLAUSE

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
