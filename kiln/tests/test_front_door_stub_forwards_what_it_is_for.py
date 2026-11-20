"""A plain install's front door sends Kiln's servers what the object is for.

``design_session`` is served: on an install without kiln-pro the agent calls
a stub built from ``pro_tool_manifest.json``, and the MCP layer passes on
only the arguments that stub's schema names.  What the agent states the
object will do or live through (``in_use``) has to reach the servers whole,
or nothing it calls for is ever said.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from kiln import server


@pytest.fixture
def sent(monkeypatch, tmp_path) -> list[tuple[str, dict[str, Any]]]:
    """Kiln's servers, as the stub reaches them: every call recorded."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call(tool: str, /, **kwargs: Any) -> dict[str, Any]:
        calls.append((tool, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
        return {"status": "success", "goal": {"goal_id": "g1", "safety_notes": []}}

    monkeypatch.setattr(server, "_pro_api_call", fake_call)
    return calls


def _front_door():
    from kiln.mcp_compat import FastMCP

    door = FastMCP("plain-install")
    server._register_pro_tool_stubs(door)
    return door


def test_the_stub_publishes_it_as_a_list_of_words():
    tool = _front_door()._tool_manager._tools["design_session"]
    schema = tool.parameters["properties"]["in_use"]
    assert any(v.get("type") == "array" for v in schema.get("anyOf") or [schema]), schema
    assert "holds weight" in schema["description"]


def test_what_the_agent_states_reaches_the_servers_whole(sent):
    asyncio.run(_front_door().call_tool("design_session", {
        "verb": "start",
        "idea": "a stand for my monitor",
        "in_use": ["holds weight", "near heat"],
    }))
    assert [tool for tool, _ in sent] == ["design_session"]
    assert sent[0][1]["in_use"] == ["holds weight", "near heat"]
    assert sent[0][1]["idea"] == "a stand for my monitor"
