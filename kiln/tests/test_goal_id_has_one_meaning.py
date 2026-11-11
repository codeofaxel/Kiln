"""A saved goal's id has one argument name: ``goal_id``.

``design_session`` returns it and every tool that takes a saved goal
declares it under that name.  ``brief_id`` is the earlier spelling: a call
that still sends it is read as ``goal_id`` and the reply says so.  An
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

    def test_the_make_doors_declare_goal_id(self):
        tools = server.mcp._tool_manager._tools
        assert GOAL_PARAM == "goal_id"
        for name in ("compile_scad", "generate_from_template", "download_model", "monitor_print"):
            assert "goal_id" in tools[name].parameters["properties"], name

    def test_the_earlier_spelling_still_reaches_a_real_make_door(self):
        from kiln.goal_argument import as_declared, declared_by

        tool = server.mcp._tool_manager._tools["compile_scad"]
        arguments, renamed = as_declared(declared_by(tool), {"scad_code": "cube(1);", "brief_id": "g-9"})
        assert arguments == {"scad_code": "cube(1);", "goal_id": "g-9"}
        assert renamed == ("brief_id", "goal_id")


class TestWhatIsStillRefused:
    def test_two_different_goals_in_one_call(self):
        with pytest.raises(RuntimeError):
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


class TestTheInstructionUsesOneName:
    def test_the_instruction_uses_the_one_name(self):
        assert "goal_id" in FRONT_DOOR_LINE
        assert "brief_id" not in FRONT_DOOR_LINE

    def test_only_tool_names_are_quoted_in_the_instruction(self):
        import re

        assert set(re.findall(r"`([a-z_]+)`", FRONT_DOOR_LINE)) == {FRONT_DOOR}


class TestTwoDifferentGoalsAreRefusedInWords:
    """A call naming two different goals is told so: both ids, and the one
    argument the tool takes."""

    def test_the_refusal_names_both_ids_and_the_one_argument_to_keep(self):
        with pytest.raises(RuntimeError) as refused:
            _call(_GOAL_TOOL, {"goal_id": "g-1", "brief_id": "g-2"})
        message = str(refused.value)
        assert "g-1" in message and "g-2" in message
        assert "one saved goal" in message and "goal_id" in message
        assert "does not accept" not in message

    def test_the_mirror_on_a_make_tool(self):
        with pytest.raises(RuntimeError) as refused:
            _call(_BRIEF_TOOL, {"goal_id": "g-1", "brief_id": "g-2"})
        assert "takes it as brief_id" in str(refused.value)


class TestTheBridgeDoor:
    """A call relayed to this computer follows the dispatcher's rule."""

    def test_a_relayed_call_reads_the_other_name_too(self):
        from kiln import bridge_client

        call = bridge_client._default_tool_caller()
        assert call(_BRIEF_TOOL, {"goal_id": "g-123"})["goal"] == "g-123"
        assert call(_GOAL_TOOL, {"brief_id": "g-123"})["goal"] == "g-123"

    def test_a_relayed_unknown_argument_still_fails(self):
        from kiln import bridge_client

        call = bridge_client._default_tool_caller()
        with pytest.raises(TypeError):
            call(_BRIEF_TOOL, {"goal_idd": "g-123"})


class TestNoToolDeclaresTheEarlierSpelling:
    """Every registered tool that takes a saved goal declares ``goal_id``."""

    def test_no_registered_tool_declares_brief_id(self):
        declaring = sorted(
            name
            for name, tool in server.mcp._tool_manager._tools.items()
            if "brief_id" in (tool.parameters or {}).get("properties", {})
        )
        assert declaring == [_BRIEF_TOOL], declaring

    def test_no_tool_description_teaches_the_earlier_spelling(self):
        teaching = sorted(
            name
            for name, tool in server.mcp._tool_manager._tools.items()
            if name != _BRIEF_TOOL and "brief_id" in (tool.description or "")
        )
        assert teaching == [], teaching
