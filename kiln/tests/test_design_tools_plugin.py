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

    def test_registers_parametric_tools(self, registered_tools) -> None:
        assert "build_parametric_prompt" in registered_tools
        assert "parse_scad_parameters" in registered_tools
        assert "update_scad_parameter" in registered_tools
        assert "validate_scad_parameters" in registered_tools

    def test_build_generation_prompt_accepts_provider(self, registered_tools) -> None:
        """Verify the updated tool signature accepts provider param."""
        import inspect

        sig = inspect.signature(registered_tools["build_generation_prompt"])
        assert "provider" in sig.parameters

    def test_registers_component_tools(self, registered_tools) -> None:
        assert "list_design_components" in registered_tools
        assert "match_design_components" in registered_tools

    def test_registers_parametric_tweaking_tools(self, registered_tools) -> None:
        assert "compile_scad" in registered_tools
        assert "tweak_and_compile_scad" in registered_tools

    def test_registers_point_and_talk_tools(self, registered_tools) -> None:
        assert "analyze_scad_code" in registered_tools
        assert "modify_scad_module" in registered_tools
        assert "insert_into_scad" in registered_tools

    def test_registers_design_dna_tools(self, registered_tools) -> None:
        assert "cache_design_with_source" in registered_tools
        assert "get_design_source" in registered_tools

    def test_cache_design_with_source_success(self, registered_tools, tmp_path, monkeypatch) -> None:
        """Handler-level test: cache_design_with_source returns success."""
        stl = tmp_path / "test.stl"
        stl.write_bytes(b"\x00" * 84 + b"\x01\x00\x00\x00" + b"\x00" * 50)

        fake_design = SimpleNamespace(
            id="d-abc123",
            file_hash="abc123hash",
            scad_source="cube([10,10,10]);",
            generation_prompt="a cube",
            provider="openscad",
            to_dict=lambda: {
                "id": "d-abc123",
                "scad_source": "cube([10,10,10]);",
                "generation_prompt": "a cube",
                "provider": "openscad",
            },
        )
        monkeypatch.setattr(
            "kiln.design_cache.get_design_cache",
            lambda: SimpleNamespace(add=lambda *a, **kw: fake_design),
        )

        result = registered_tools["cache_design_with_source"](
            file_path=str(stl),
            scad_source="cube([10,10,10]);",
            generation_prompt="a cube",
            provider="openscad",
        )
        assert result["status"] == "success"
        assert result["design_id"] == "d-abc123"
        assert result["has_source"] is True

    def test_cache_design_with_source_missing_file(self, registered_tools) -> None:
        """Handler-level test: missing file returns error."""
        result = registered_tools["cache_design_with_source"](
            file_path="/nonexistent/model.stl",
            scad_source="cube([10,10,10]);",
        )
        assert result["status"] == "error"

    def test_get_design_source_found(self, registered_tools, monkeypatch) -> None:
        """Handler-level test: get_design_source returns source when present."""
        monkeypatch.setattr(
            "kiln.design_cache.get_design_cache",
            lambda: SimpleNamespace(
                get_source=lambda did: {
                    "scad_source": "cube([5,5,5]);",
                    "generation_prompt": "small cube",
                    "provider": "openscad",
                }
            ),
        )

        result = registered_tools["get_design_source"](design_id="d-xyz")
        assert result["status"] == "success"
        assert result["scad_source"] == "cube([5,5,5]);"

    def test_get_design_source_not_found(self, registered_tools, monkeypatch) -> None:
        """Handler-level test: missing design returns error."""
        monkeypatch.setattr(
            "kiln.design_cache.get_design_cache",
            lambda: SimpleNamespace(get_source=lambda did: None),
        )

        result = registered_tools["get_design_source"](design_id="d-missing")
        assert result["status"] == "error"

    def test_get_design_source_no_source_attached(self, registered_tools, monkeypatch) -> None:
        """Handler-level test: design exists but has no source."""
        monkeypatch.setattr(
            "kiln.design_cache.get_design_cache",
            lambda: SimpleNamespace(
                get_source=lambda did: {
                    "scad_source": None,
                    "generation_prompt": None,
                    "provider": None,
                }
            ),
        )

        result = registered_tools["get_design_source"](design_id="d-nosrc")
        assert result["status"] == "success"
        assert result["has_source"] is False
