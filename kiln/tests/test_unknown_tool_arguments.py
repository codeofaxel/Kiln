"""An argument a tool does not declare is refused, never silently dropped.

The SDK's generated argument model ignores keys it does not know, so a
call carrying a misspelt or unsupported parameter went ahead WITHOUT it
and reported success — the caller believed the parameter had taken
effect.  The dispatch chokepoint now refuses such a call by name.
"""
from __future__ import annotations

import asyncio

import pytest

from kiln import server


@pytest.fixture(autouse=True)
def _terms_accepted(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)


def _call(name: str, arguments: dict):
    return asyncio.run(server.mcp._tool_manager.call_tool(name, arguments))


def test_an_undeclared_argument_is_refused_by_name():
    with pytest.raises(RuntimeError) as excinfo:
        _call("get_material_properties", {"material_id": "PLA", "image_style": "silhouette"})
    message = str(excinfo.value)
    assert "image_style" in message
    assert "get_material_properties" in message
    assert "material_id" in message, "the refusal must say what IS accepted"


def test_declared_arguments_still_dispatch():
    # Same tool, only declared keys: the gate must be invisible.
    result = _call("get_material_properties", {"material_id": "PLA"})
    assert isinstance(result, (dict, list))


def test_unknown_argument_helper_names_only_the_strangers():
    mgr = server.mcp._tool_manager
    assert server._unknown_tool_arguments(
        mgr, "get_material_properties", {"material_id": "PLA", "zzz": 1, "aaa": 2}
    ) == ["aaa", "zzz"]
    assert server._unknown_tool_arguments(mgr, "get_material_properties", {"material_id": "PLA"}) == []
    assert server._unknown_tool_arguments(mgr, "get_material_properties", None) == []
    # A tool this registry does not know is the SDK's error to raise, not ours.
    assert server._unknown_tool_arguments(mgr, "no_such_tool", {"a": 1}) == []
