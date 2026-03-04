"""Tests for material inventory tools plugin — MCP tool registration and wiring.

Covers:
    - Plugin metadata (name, description)
    - register() registers all expected tools
    - get_fleet_material_summary tool wiring
    - get_material_consumption_history tool wiring
    - forecast_material_consumption tool wiring
    - check_material_sufficiency tool wiring
    - get_restock_suggestions tool wiring
    - find_printers_with_material tool wiring
    - optimize_fleet_assignment tool wiring
    - suggest_spool_swaps tool wiring
"""

from __future__ import annotations

import pytest

from kiln.persistence import KilnDB

# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture()
def db(tmp_path):
    """Temporary KilnDB for test isolation."""
    db_path = str(tmp_path / "test.db")
    return KilnDB(db_path)


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
def registered_tools(mock_mcp, db, monkeypatch):
    """Register the plugin and return the captured tools dict."""
    mcp, tools = mock_mcp
    monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
    from kiln.plugins.material_inventory_tools import plugin
    plugin.register(mcp)
    return tools


def _add_spool(db, spool_id="spool-1", material_type="PLA", *, color=None,
               remaining_grams=800.0):
    """Helper to add a spool to the DB."""
    db.save_spool({
        "id": spool_id,
        "material_type": material_type,
        "color": color,
        "weight_grams": 1000.0,
        "remaining_grams": remaining_grams,
    })


def _add_material(db, printer_name="printer-1", material_type="PLA", *,
                  color=None, remaining_grams=500.0, spool_id=None, tool_index=0):
    """Helper to add loaded material to a printer."""
    db.save_material(
        printer_name,
        tool_index,
        material_type,
        color=color,
        spool_id=spool_id,
        remaining_grams=remaining_grams,
    )


# ===================================================================
# Plugin metadata
# ===================================================================


class TestMaterialInventoryPluginMeta:
    """Tests for plugin identity and registration."""

    def test_plugin_name(self) -> None:
        from kiln.plugins.material_inventory_tools import plugin
        assert plugin.name == "material_inventory_tools"

    def test_plugin_description(self) -> None:
        from kiln.plugins.material_inventory_tools import plugin
        assert "inventory" in plugin.description.lower()

    def test_registers_all_tools(self, registered_tools) -> None:
        expected = {
            "get_fleet_material_summary",
            "get_material_consumption_history",
            "forecast_material_consumption",
            "check_material_sufficiency",
            "get_restock_suggestions",
            "find_printers_with_material",
            "optimize_fleet_assignment",
            "suggest_spool_swaps",
        }
        assert expected == set(registered_tools.keys())


# ===================================================================
# Tool wiring
# ===================================================================


class TestFleetMaterialSummaryTool:
    """Tests for get_fleet_material_summary tool."""

    def test_empty_fleet(self, registered_tools) -> None:
        result = registered_tools["get_fleet_material_summary"]()
        assert result["success"] is True
        assert result["summary"] == []

    def test_with_spool(self, registered_tools, db) -> None:
        _add_spool(db, spool_id="s1", material_type="PLA", remaining_grams=500.0)
        result = registered_tools["get_fleet_material_summary"]()
        assert result["success"] is True
        assert result["material_types"] >= 1


class TestConsumptionHistoryTool:
    """Tests for get_material_consumption_history tool."""

    def test_no_history(self, registered_tools) -> None:
        result = registered_tools["get_material_consumption_history"]()
        assert result["success"] is True
        assert result["history"] == []
        assert result["period_days"] == 30

    def test_custom_days(self, registered_tools) -> None:
        result = registered_tools["get_material_consumption_history"](days=7)
        assert result["success"] is True
        assert result["period_days"] == 7


class TestForecastConsumptionTool:
    """Tests for forecast_material_consumption tool."""

    def test_no_stock(self, registered_tools) -> None:
        result = registered_tools["forecast_material_consumption"]("PLA")
        assert result["success"] is True
        assert result["forecast"]["material_type"] == "PLA"

    def test_with_stock(self, registered_tools, db) -> None:
        _add_spool(db, material_type="PETG", remaining_grams=1000.0)
        result = registered_tools["forecast_material_consumption"]("PETG")
        assert result["success"] is True
        assert result["forecast"]["current_stock_grams"] > 0


class TestCheckMaterialSufficiencyTool:
    """Tests for check_material_sufficiency tool."""

    def test_no_material_loaded(self, registered_tools) -> None:
        result = registered_tools["check_material_sufficiency"](
            printer_name="printer-1",
            required_grams=100.0,
        )
        assert result["success"] is True
        assert result["check"]["sufficient"] is False

    def test_sufficient_material(self, registered_tools, db) -> None:
        _add_material(db, printer_name="printer-1", material_type="PLA", remaining_grams=500.0)
        result = registered_tools["check_material_sufficiency"](
            printer_name="printer-1",
            required_grams=100.0,
        )
        assert result["success"] is True
        assert result["check"]["sufficient"] is True


class TestRestockSuggestionsTool:
    """Tests for get_restock_suggestions tool."""

    def test_empty_inventory(self, registered_tools) -> None:
        result = registered_tools["get_restock_suggestions"]()
        assert result["success"] is True
        assert result["suggestions"] == []


class TestFindPrintersWithMaterialTool:
    """Tests for find_printers_with_material tool."""

    def test_no_printers(self, registered_tools) -> None:
        result = registered_tools["find_printers_with_material"](material_type="PLA")
        assert result["success"] is True
        assert result["printers"] == []

    def test_matching_printer(self, registered_tools, db) -> None:
        _add_material(db, printer_name="p1", material_type="PLA", remaining_grams=400.0)
        result = registered_tools["find_printers_with_material"](material_type="PLA")
        assert result["success"] is True
        assert result["count"] >= 1


class TestOptimizeFleetAssignmentTool:
    """Tests for optimize_fleet_assignment tool."""

    def test_empty_jobs(self, registered_tools) -> None:
        result = registered_tools["optimize_fleet_assignment"](jobs=[])
        assert result["success"] is True
        assert result["assignments"] == []

    def test_single_job(self, registered_tools, db) -> None:
        _add_material(db, printer_name="p1", material_type="PLA", remaining_grams=500.0)
        jobs = [{"file_name": "test.gcode", "material_type": "PLA", "required_grams": 100.0}]
        result = registered_tools["optimize_fleet_assignment"](jobs=jobs)
        assert result["success"] is True
        assert result["count"] == 1


class TestSuggestSpoolSwapsTool:
    """Tests for suggest_spool_swaps tool."""

    def test_empty_jobs(self, registered_tools) -> None:
        result = registered_tools["suggest_spool_swaps"](jobs=[])
        assert result["success"] is True
        assert result["swap_suggestions"] == []
