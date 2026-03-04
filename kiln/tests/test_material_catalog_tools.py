"""Tests for material catalog tools plugin — MCP tool registration and wiring.

Covers:
    - Plugin metadata (name, description)
    - register() registers all expected tools
    - search_material_catalog tool returns results
    - get_material_info tool returns entry or error
    - list_material_catalog tool returns IDs
    - get_compatible_materials tool returns family members
    - get_material_purchase_urls tool returns URLs
    - find_material_match tool returns match or None
"""

from __future__ import annotations

import pytest

# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture(autouse=True)
def _reset_catalog():
    """Reset the singleton cache before each test for isolation."""
    import kiln.material_catalog as mod
    mod._catalog = None
    yield
    mod._catalog = None


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
    """Register the plugin and return the captured tools dict."""
    mcp, tools = mock_mcp
    from kiln.plugins.material_catalog_tools import plugin
    plugin.register(mcp)
    return tools


# ===================================================================
# Plugin metadata
# ===================================================================


class TestMaterialCatalogPluginMeta:
    """Tests for plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.material_catalog_tools import plugin
        assert plugin.name == "material_catalog_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.material_catalog_tools import plugin
        assert "catalog" in plugin.description.lower()

    def test_registers_all_tools(self, registered_tools) -> None:
        expected = {
            "search_material_catalog",
            "get_material_info",
            "list_material_catalog",
            "get_compatible_materials",
            "get_material_purchase_urls",
            "find_material_match",
        }
        assert expected == set(registered_tools.keys())


# ===================================================================
# Tool wiring
# ===================================================================


class TestSearchMaterialCatalog:
    """Tests for search_material_catalog tool."""

    def test_search_returns_results(self, registered_tools) -> None:
        result = registered_tools["search_material_catalog"]("Hatchbox")
        assert result["success"] is True
        assert result["count"] >= 1
        assert len(result["results"]) >= 1

    def test_search_empty_query_returns_error(self, registered_tools) -> None:
        result = registered_tools["search_material_catalog"]("")
        assert "error" in result

    def test_search_no_match(self, registered_tools) -> None:
        result = registered_tools["search_material_catalog"]("nonexistentxyz123")
        assert result["success"] is True
        assert result["count"] == 0


class TestGetMaterialInfo:
    """Tests for get_material_info tool."""

    def test_known_material(self, registered_tools) -> None:
        result = registered_tools["get_material_info"]("hatchbox_pla")
        assert result["success"] is True
        assert result["material"]["id"] == "hatchbox_pla"
        assert result["material"]["vendor"] == "Hatchbox"

    def test_unknown_material(self, registered_tools) -> None:
        result = registered_tools["get_material_info"]("nonexistent_xyz")
        assert "error" in result


class TestListMaterialCatalog:
    """Tests for list_material_catalog tool."""

    def test_returns_ids(self, registered_tools) -> None:
        result = registered_tools["list_material_catalog"]()
        assert result["success"] is True
        assert result["count"] >= 50
        assert "hatchbox_pla" in result["material_ids"]


class TestGetCompatibleMaterials:
    """Tests for get_compatible_materials tool."""

    def test_pla_family(self, registered_tools) -> None:
        result = registered_tools["get_compatible_materials"]("pla")
        assert result["success"] is True
        assert result["count"] >= 5
        assert result["family"] == "pla"

    def test_unknown_family(self, registered_tools) -> None:
        result = registered_tools["get_compatible_materials"]("unobtainium")
        assert result["success"] is True
        assert result["count"] == 0


class TestGetMaterialPurchaseUrls:
    """Tests for get_material_purchase_urls tool."""

    def test_known_material_urls(self, registered_tools) -> None:
        result = registered_tools["get_material_purchase_urls"]("hatchbox_pla")
        assert result["success"] is True
        assert "amazon" in result["urls"]
        assert "manufacturer" in result["urls"]

    def test_with_color(self, registered_tools) -> None:
        result = registered_tools["get_material_purchase_urls"]("hatchbox_pla", color="blue")
        assert result["success"] is True
        assert result["color"] == "blue"
        assert "blue" in result["urls"]["amazon"]

    def test_unknown_material(self, registered_tools) -> None:
        result = registered_tools["get_material_purchase_urls"]("nonexistent_xyz")
        assert "error" in result


class TestFindMaterialMatch:
    """Tests for find_material_match tool."""

    def test_vendor_match(self, registered_tools) -> None:
        result = registered_tools["find_material_match"](vendor="Hatchbox", material_type="PLA")
        assert result["success"] is True
        assert result["match"] is not None
        assert result["match"]["vendor"] == "Hatchbox"

    def test_no_match(self, registered_tools) -> None:
        result = registered_tools["find_material_match"](vendor="NonexistentBrandXYZ")
        assert result["success"] is True
        assert result["match"] is None
