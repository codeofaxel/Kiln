"""Tests for kiln.plugins.generation_tools registration and wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


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
    from kiln.plugins.generation_tools import plugin

    plugin.register(mcp)
    return tools


class TestGenerationToolsPlugin:
    def test_registers_generate_original_design(self, registered_tools) -> None:
        assert "generate_original_design" in registered_tools

    def test_generate_original_design_wires_to_core_loop(self, registered_tools, monkeypatch) -> None:
        monkeypatch.setattr("kiln.server._check_auth", lambda scope: None)

        session = SimpleNamespace(
            summary="Best attempt scored 94/100 (A) via gemini. The design is ready for print.",
            to_dict=lambda: {
                "provider_used": "gemini",
                "best_readiness_score": 94,
                "best_readiness_grade": "A",
                "ready_for_print": True,
                "attempts_made": 1,
            },
        )
        monkeypatch.setattr(
            "kiln.original_design.generate_original_design",
            lambda *args, **kwargs: session,
        )

        result = registered_tools["generate_original_design"](
            "phone stand with cable slot",
            provider="auto",
            printer_model="bambu_a1",
            max_attempts=2,
        )

        assert result["status"] == "success"
        assert result["message"] == session.summary
        assert result["provider_used"] == "gemini"
        assert result["best_readiness_score"] == 94
