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


# ---------------------------------------------------------------------------
# Fleet-scope tier gate — the cross-machine ANSWER is Business+
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Registry stub counting distinct MACHINES (not names)."""

    def __init__(self, machine_count: int):
        self._count = machine_count

    @property
    def count(self) -> int:
        return self._count


@pytest.fixture()
def fleet_of(monkeypatch):
    """Install a fake registry + controllable tier cap.

    ``kiln.licensing`` is an alias kiln-pro installs at import time, so it
    exists on a dev box and not in public CI.  The gate tolerates its
    absence; the test stands up a stub so the cap stays controlled either
    way (same pattern as test_printer_identity_and_fleet_gate).
    """
    import sys
    import types

    import kiln.registry as registry_mod

    def _install(machines: int, cap: int | None = 1):
        monkeypatch.setattr(
            registry_mod, "get_registry", lambda: _FakeRegistry(machines)
        )
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            lic, "max_printers_for_tier", lambda _t: cap, raising=False
        )

    return _install


# Every door whose answer spans machines, and a minimal call for each.
_FLEET_DOORS = [
    ("get_fleet_material_summary", {}),
    ("get_material_consumption_history", {}),
    ("forecast_material_consumption", {"material_type": "PLA"}),
    ("get_restock_suggestions", {}),
    ("find_printers_with_material", {"material_type": "PLA"}),
    ("optimize_fleet_assignment", {"jobs": []}),
    ("suggest_spool_swaps", {"jobs": []}),
]


class TestFleetScopeGate:
    """Fleets are Business+; single-printer installs are never gated."""

    @pytest.mark.parametrize("tool_name,kwargs", _FLEET_DOORS)
    def test_single_machine_is_free_at_every_door(
        self, registered_tools, fleet_of, tool_name, kwargs
    ) -> None:
        """One printer can never be a fleet — free/Pro keep full awareness
        of their own machine and shelf."""
        fleet_of(machines=1, cap=1)
        result = registered_tools[tool_name](**kwargs)
        assert result["success"] is True

    @pytest.mark.parametrize("tool_name,kwargs", _FLEET_DOORS)
    def test_multi_machine_blocked_below_business_at_every_door(
        self, registered_tools, fleet_of, tool_name, kwargs
    ) -> None:
        """The gate must actually fire — and at EVERY door, not just the
        one a reviewer happened to look at."""
        fleet_of(machines=4, cap=1)
        result = registered_tools[tool_name](**kwargs)
        assert result["success"] is False
        assert result["code"] == "TIER_FLEET_SCOPE"
        assert result["machines"] == 4
        assert "pricing" in result["upgrade_hint"]

    @pytest.mark.parametrize("tool_name,kwargs", _FLEET_DOORS)
    def test_business_cap_allows_the_fleet_answer(
        self, registered_tools, fleet_of, tool_name, kwargs
    ) -> None:
        fleet_of(machines=4, cap=50)
        assert registered_tools[tool_name](**kwargs)["success"] is True

    def test_enterprise_uncapped_allows_everything(
        self, registered_tools, fleet_of
    ) -> None:
        fleet_of(machines=200, cap=None)
        assert registered_tools["get_fleet_material_summary"]()["success"] is True

    def test_broken_registry_soft_passes(
        self, registered_tools, monkeypatch
    ) -> None:
        """A licensing check must never be why a user can't see their own
        materials."""
        import kiln.registry as registry_mod

        def _boom():
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(registry_mod, "get_registry", _boom)
        assert registered_tools["get_fleet_material_summary"]()["success"] is True


class TestSufficiencyStaysSingleMachine:
    """The 'does THIS printer have enough' answer is never gated."""

    def test_core_verdict_survives_below_business(
        self, registered_tools, fleet_of, db
    ) -> None:
        db.save_material("a1-left", 0, "PLA", color="white", remaining_grams=100.0)
        fleet_of(machines=4, cap=1)

        result = registered_tools["check_material_sufficiency"](
            printer_name="a1-left", required_grams=50.0
        )
        assert result["success"] is True
        assert result["check"]["sufficient"] is True
        assert result["check"]["printer_name"] == "a1-left"

    def test_other_machines_are_withheld_below_business(
        self, registered_tools, fleet_of, db
    ) -> None:
        """The shortfall is still reported; naming the OTHER machine that
        could rescue the print is the fleet answer."""
        db.save_material("a1-left", 0, "PLA", color="white", remaining_grams=10.0)
        db.save_material("a1-right", 0, "PLA", color="white", remaining_grams=900.0)
        fleet_of(machines=2, cap=1)

        result = registered_tools["check_material_sufficiency"](
            printer_name="a1-left", required_grams=500.0
        )
        check = result["check"]
        assert check["sufficient"] is False          # the honest verdict stays
        assert check["alternative_printers"] == []   # the fleet half is withheld
        assert check["fleet_alternatives_available"] == 1
        assert "Business" in check["upgrade_hint"]
        assert not any("Printer " in s for s in check["suggestions"])

    def test_business_sees_the_alternative_machine(
        self, registered_tools, fleet_of, db
    ) -> None:
        db.save_material("a1-left", 0, "PLA", color="white", remaining_grams=10.0)
        db.save_material("a1-right", 0, "PLA", color="white", remaining_grams=900.0)
        fleet_of(machines=2, cap=50)

        check = registered_tools["check_material_sufficiency"](
            printer_name="a1-left", required_grams=500.0
        )["check"]
        assert check["alternative_printers"] == ["a1-right"]
        assert "fleet_alternatives_available" not in check


class TestFleetGateCountsMachinesNotNames:
    """Identity before arithmetic — the cap counts MACHINES.

    The production incident: the server registered one printer as
    ``"default"`` AND under its config.yaml name, so a single-printer
    install read as a fleet.  A material gate that counted NAMES would
    resurrect that bug as a false paywall on a one-printer user.
    """

    def test_one_machine_under_two_names_is_not_a_fleet(
        self, registered_tools, monkeypatch
    ) -> None:
        import sys
        import types

        import kiln.registry as registry_mod
        from kiln.printers.base import PrinterCapabilities
        from kiln.registry import PrinterRegistry

        class _Adapter:
            """A real object, not a Mock: a Mock auto-creates the private
            attrs the fingerprint falls back on and would defeat the test."""

            def __init__(self, host):
                self.name = "moonraker"
                self.host = host
                self.serial = ""
                self.capabilities = PrinterCapabilities()

        reg = PrinterRegistry()
        reg.register("default", _Adapter("http://192.0.2.10:7125"))
        reg.register("my-voron", _Adapter("http://192.0.2.10:7125"))
        # Precondition: two names, one machine.
        assert reg.name_count == 2
        assert reg.count == 1

        monkeypatch.setattr(registry_mod, "get_registry", lambda: reg)
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            lic, "max_printers_for_tier", lambda _t: 1, raising=False
        )

        result = registered_tools["get_fleet_material_summary"]()
        assert result["success"] is True, "an alias must not create a paywall"
