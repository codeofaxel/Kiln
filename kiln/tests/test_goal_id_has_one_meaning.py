"""A saved goal's id is one thing under two argument names.

``design_session`` takes it as ``goal_id``; the make tools take it as
``brief_id``.  A call that hands the id over under the other tool's name is
read as the name the called tool declares, and the reply says so.  An
argument a tool does not know is still refused.
"""

from __future__ import annotations

import asyncio

import pytest

from kiln import server
from kiln.front_door import FRONT_DOOR, FRONT_DOOR_LINE, GOAL_PARAM

_GOAL_TOOL = "probe_tool_taking_goal_id"
_BRIEF_TOOL = "probe_tool_taking_brief_id"


@pytest.fixture(autouse=True)
def _terms_accepted(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def probe_tools():
    """One tool per spelling, registered the way every tool is."""

    def probe_tool_taking_goal_id(goal_id: str = "", idea: str = "") -> dict:
        return {"success": True, "goal": goal_id, "idea": idea}

    def probe_tool_taking_brief_id(brief_id: str = "") -> dict:
        return {"success": True, "goal": brief_id}

    server.mcp.tool()(probe_tool_taking_goal_id)
    server.mcp.tool()(probe_tool_taking_brief_id)
    try:
        yield
    finally:
        tools = server.mcp._tool_manager._tools
        tools.pop(_GOAL_TOOL, None)
        tools.pop(_BRIEF_TOOL, None)


def _call(name: str, arguments: dict):
    return asyncio.run(server.mcp._tool_manager.call_tool(name, arguments))


def _goal(result) -> str:
    if isinstance(result, dict):
        return result["goal"]
    import json

    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)["goal"]


class TestTheOtherNameIsReadAsTheSameGoal:
    def test_brief_id_handed_to_a_tool_that_takes_goal_id(self):
        assert _goal(_call(_GOAL_TOOL, {"brief_id": "g-123", "idea": "a hook"})) == "g-123"

    def test_goal_id_handed_to_a_tool_that_takes_brief_id(self):
        assert _goal(_call(_BRIEF_TOOL, {"goal_id": "g-123"})) == "g-123"

    def test_both_names_with_the_same_id_is_one_goal(self):
        assert _goal(_call(_GOAL_TOOL, {"goal_id": "g-123", "brief_id": "g-123"})) == "g-123"

    def test_an_empty_other_name_does_not_erase_the_goal(self):
        assert _goal(_call(_GOAL_TOOL, {"goal_id": "g-123", "brief_id": ""})) == "g-123"

    def test_the_real_front_door_declares_goal_id_and_the_make_doors_brief_id(self):
        tools = server.mcp._tool_manager._tools
        if FRONT_DOOR not in tools:
            pytest.skip("the front door is not registered on this install")
        assert "goal_id" in tools[FRONT_DOOR].parameters["properties"]
        assert GOAL_PARAM in tools["compile_scad"].parameters["properties"]


class TestWhatIsStillRefused:
    def test_two_different_goals_in_one_call(self):
        with pytest.raises(RuntimeError, match="does not accept the argument: brief_id"):
            _call(_GOAL_TOOL, {"goal_id": "g-1", "brief_id": "g-2"})

    def test_an_argument_nobody_declares(self):
        with pytest.raises(RuntimeError, match="does not accept the argument: goal_idd"):
            _call(_GOAL_TOOL, {"goal_idd": "g-1"})

    def test_a_goal_handed_to_a_tool_that_takes_none(self):
        with pytest.raises(RuntimeError, match="does not accept the argument: brief_id"):
            _call("get_material_properties", {"material_id": "PLA", "brief_id": "g-1"})
        with pytest.raises(RuntimeError, match="does not accept the argument: goal_id"):
            _call("get_material_properties", {"material_id": "PLA", "goal_id": "g-1"})


class TestTheReplySaysSo:
    @pytest.fixture(autouse=True)
    def _note_installed(self):
        server._install_goal_argument_note()  # once per process; a repeat is a no-op

    def test_the_server_installs_the_note_when_it_starts(self):
        import inspect

        assert "_install_goal_argument_note()" in inspect.getsource(server._start)

    def _structured(self, name: str, arguments: dict) -> dict:
        Client = pytest.importorskip("mcp").Client

        async def _one():
            async with Client(server.mcp) as client:
                return await client.call_tool(name, arguments)

        result = asyncio.run(_one())
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        import json

        return json.loads(result.content[0].text)

    def test_a_renamed_argument_is_named_in_the_reply(self):
        reply = self._structured(_GOAL_TOOL, {"brief_id": "g-123"})
        assert reply["goal"] == "g-123"
        note = reply["argument_note"]
        assert "brief_id" in note and "goal_id" in note and _GOAL_TOOL in note

    def test_a_call_under_the_declared_name_carries_no_note(self):
        reply = self._structured(_GOAL_TOOL, {"goal_id": "g-123"})
        assert reply["goal"] == "g-123"
        assert "argument_note" not in reply


class TestTheInstructionNamesWhichToolTakesWhich:
    def test_the_front_door_is_told_apart_from_the_make_tools(self):
        assert "as that tool's" not in FRONT_DOOR_LINE
        assert f"`{FRONT_DOOR}` itself takes it as goal_id" in FRONT_DOOR_LINE
        assert f"their {GOAL_PARAM} argument" in FRONT_DOOR_LINE

    def test_only_tool_names_are_quoted_in_the_instruction(self):
        import re

        assert set(re.findall(r"`([a-z_]+)`", FRONT_DOOR_LINE)) == {FRONT_DOOR}
