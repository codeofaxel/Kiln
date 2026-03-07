"""Tests for kiln.plugins.design_tools discovery and registration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiln.plugin_loader import discover_plugins


@pytest.fixture()
def mock_mcp():
    """Create a mock MCP server that captures registered tools."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    return MockMCP(), tools


@pytest.fixture()
def registered_tools(mock_mcp):
    mcp, tools = mock_mcp
    from kiln.plugins.design_tools import plugin

    plugin.register(mcp)
    return tools


class TestDesignToolsPlugin:
    def test_design_tools_is_auto_discoverable(self) -> None:
        names = {plugin.name for plugin in discover_plugins("kiln.plugins")}
        assert "design_tools" in names

    def test_registers_original_design_tools(self, registered_tools) -> None:
        assert "get_design_brief" in registered_tools
        assert "build_generation_prompt" in registered_tools
        assert "audit_original_design" in registered_tools

    def test_audit_original_design_wires_to_core_loop(self, registered_tools, monkeypatch) -> None:
        session = SimpleNamespace(
            readiness_score=93,
            readiness_grade="A",
            ready_for_print=True,
            to_dict=lambda: {
                "readiness_score": 93,
                "readiness_grade": "A",
                "ready_for_print": True,
            },
        )
        monkeypatch.setattr(
            "kiln.original_design.audit_original_design",
            lambda *args, **kwargs: session,
        )

        result = registered_tools["audit_original_design"](
            "/tmp/model.stl",
            "phone stand with cable slot",
            printer_model="bambu_a1",
        )

        assert result["status"] == "success"
        assert result["readiness_score"] == 93
        assert result["ready_for_print"] is True
