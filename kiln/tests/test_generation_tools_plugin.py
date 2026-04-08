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


_EXPECTED_TOOLS = {
    "generate_original_design",
    "preview_generated_model",
    "validate_and_prepare_mesh",
    "generate_texture",
}


class TestGenerationToolsRegistration:
    """Verify exactly the intended tools are registered — no more, no less."""

    def test_registers_exactly_four_tools(self, registered_tools) -> None:
        assert set(registered_tools.keys()) == _EXPECTED_TOOLS

    def test_no_server_duplicates(self, registered_tools) -> None:
        """Tools owned by server.py must NOT be re-registered by the plugin."""
        server_owned = {
            "list_generation_providers",
            "generate_model",
            "generate_model_from_image",
            "generation_status",
            "download_generated_model",
            "await_generation",
            "generate_and_print",
            "validate_generated_mesh",
        }
        for name in server_owned:
            assert name not in registered_tools, f"{name} should not be in plugin"

    def test_preview_generated_model_registered(self, registered_tools) -> None:
        assert "preview_generated_model" in registered_tools

    def test_validate_and_prepare_mesh_registered(self, registered_tools) -> None:
        assert "validate_and_prepare_mesh" in registered_tools

    def test_generate_texture_registered(self, registered_tools) -> None:
        assert "generate_texture" in registered_tools


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

        assert result["success"] is True
        assert result["message"] == session.summary
        assert result["provider_used"] == "gemini"
        assert result["best_readiness_score"] == 94
