"""Tests for kiln.registry -- printer registry management.

Covers:
- Register/unregister printers
- get / list_names / list_all
- PrinterNotFoundError
- get_fleet_status (with mock adapters)
- get_idle_printers
- get_printers_by_status
- Thread safety
- __contains__
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln.printers.base import PrinterAdapter, PrinterCapabilities, PrinterState, PrinterStatus
from kiln.registry import PrinterNotFoundError, PrinterRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_adapter(
    name: str = "mock-printer",
    connected: bool = True,
    status: PrinterStatus = PrinterStatus.IDLE,
    tool_temp_actual: float | None = 24.0,
    tool_temp_target: float | None = 0.0,
    bed_temp_actual: float | None = 22.0,
    bed_temp_target: float | None = 0.0,
) -> MagicMock:
    """Create a MagicMock that behaves like a PrinterAdapter."""
    adapter = MagicMock(spec=PrinterAdapter)
    type(adapter).name = PropertyMock(return_value=name)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.get_state.return_value = PrinterState(
        connected=connected,
        state=status,
        tool_temp_actual=tool_temp_actual,
        tool_temp_target=tool_temp_target,
        bed_temp_actual=bed_temp_actual,
        bed_temp_target=bed_temp_target,
    )
    return adapter


# ---------------------------------------------------------------------------
# PrinterNotFoundError
# ---------------------------------------------------------------------------

class TestPrinterNotFoundError:
    """Tests for the PrinterNotFoundError exception."""

    def test_is_key_error(self):
        exc = PrinterNotFoundError("voron")
        assert isinstance(exc, KeyError)

    def test_stores_printer_name(self):
        exc = PrinterNotFoundError("ender-3")
        assert exc.printer_name == "ender-3"

    def test_message_contains_name(self):
        exc = PrinterNotFoundError("prusa-mk4")
        assert "prusa-mk4" in str(exc)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(PrinterNotFoundError):
            raise PrinterNotFoundError("missing")


# ---------------------------------------------------------------------------
# Register / unregister
# ---------------------------------------------------------------------------

class TestPrinterRegistryRegistration:
    """Tests for register and unregister."""

    def test_register_adds_printer(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter(name="voron")
        registry.register("voron", adapter)
        assert registry.count == 1
        assert "voron" in registry

    def test_register_replaces_existing(self):
        registry = PrinterRegistry()
        adapter1 = _make_mock_adapter(name="old")
        adapter2 = _make_mock_adapter(name="new")
        registry.register("voron", adapter1)
        registry.register("voron", adapter2)
        assert registry.count == 1
        assert registry.get("voron").name == "new"

    def test_unregister_removes_printer(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter()
        registry.register("voron", adapter)
        registry.unregister("voron")
        assert registry.count == 0
        assert "voron" not in registry

    def test_unregister_not_found_raises(self):
        registry = PrinterRegistry()
        with pytest.raises(PrinterNotFoundError):
            registry.unregister("nonexistent")

    def test_register_multiple_printers(self):
        registry = PrinterRegistry()
        for name in ["voron", "ender", "prusa"]:
            registry.register(name, _make_mock_adapter(name=name))
        assert registry.count == 3


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

class TestPrinterRegistryLookup:
    """Tests for get, list_names, list_all, count, __contains__."""

    def test_get_returns_adapter(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter(name="voron")
        registry.register("voron", adapter)
        result = registry.get("voron")
        assert result is adapter

    def test_get_not_found_raises(self):
        registry = PrinterRegistry()
        with pytest.raises(PrinterNotFoundError):
            registry.get("missing")

    def test_list_names_sorted(self):
        registry = PrinterRegistry()
        for name in ["ender", "voron", "bambu", "prusa"]:
            registry.register(name, _make_mock_adapter(name=name))
        assert registry.list_names() == ["bambu", "ender", "prusa", "voron"]

    def test_list_names_empty(self):
        registry = PrinterRegistry()
        assert registry.list_names() == []

    def test_list_all_returns_copy(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter(name="voron")
        registry.register("voron", adapter)

        all_printers = registry.list_all()
        assert "voron" in all_printers
        assert all_printers["voron"] is adapter

        # Ensure it is a copy (modifying the result does not affect registry)
        all_printers["hacked"] = _make_mock_adapter()
        assert "hacked" not in registry

    def test_count_empty(self):
        registry = PrinterRegistry()
        assert registry.count == 0

    def test_count_after_operations(self):
        registry = PrinterRegistry()
        registry.register("a", _make_mock_adapter())
        registry.register("b", _make_mock_adapter())
        assert registry.count == 2
        registry.unregister("a")
        assert registry.count == 1

    def test_contains_true(self):
        registry = PrinterRegistry()
        registry.register("voron", _make_mock_adapter())
        assert "voron" in registry

    def test_contains_false(self):
        registry = PrinterRegistry()
        assert "missing" not in registry

    def test_contains_after_unregister(self):
        registry = PrinterRegistry()
        registry.register("voron", _make_mock_adapter())
        registry.unregister("voron")
        assert "voron" not in registry


# ---------------------------------------------------------------------------
# Fleet queries
# ---------------------------------------------------------------------------

class TestGetFleetStatus:
    """Tests for get_fleet_status."""

    def test_single_printer_idle(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter(
            name="octoprint",
            connected=True,
            status=PrinterStatus.IDLE,
            tool_temp_actual=24.5,
            tool_temp_target=0.0,
            bed_temp_actual=23.0,
            bed_temp_target=0.0,
        )
        registry.register("voron", adapter)

        fleet = registry.get_fleet_status()
        assert len(fleet) == 1
        entry = fleet[0]
        assert entry["name"] == "voron"
        assert entry["backend"] == "octoprint"
        assert entry["connected"] is True
        assert entry["state"] == "idle"
        assert entry["tool_temp_actual"] == 24.5
        assert entry["bed_temp_actual"] == 23.0

    def test_multiple_printers(self):
        registry = PrinterRegistry()
        registry.register("idle-printer", _make_mock_adapter(
            name="backend-a", status=PrinterStatus.IDLE,
        ))
        registry.register("printing-printer", _make_mock_adapter(
            name="backend-b", status=PrinterStatus.PRINTING, connected=True,
        ))

        fleet = registry.get_fleet_status()
        assert len(fleet) == 2

        names = {entry["name"] for entry in fleet}
        assert names == {"idle-printer", "printing-printer"}

    def test_printer_query_failure_reports_offline(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter(name="flaky")
        adapter.get_state.side_effect = Exception("connection refused")
        registry.register("flaky-printer", adapter)

        fleet = registry.get_fleet_status()
        assert len(fleet) == 1
        entry = fleet[0]
        assert entry["name"] == "flaky-printer"
        assert entry["connected"] is False
        assert entry["state"] == "offline"
        assert entry["tool_temp_actual"] is None
        assert entry["bed_temp_actual"] is None

    def test_empty_registry(self):
        registry = PrinterRegistry()
        assert registry.get_fleet_status() == []


class TestGetIdlePrinters:
    """Tests for get_idle_printers."""

    def test_returns_idle_printers(self):
        registry = PrinterRegistry()
        registry.register("idle1", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("idle2", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("printing", _make_mock_adapter(status=PrinterStatus.PRINTING))

        idle = registry.get_idle_printers()
        assert sorted(idle) == ["idle1", "idle2"]

    def test_excludes_disconnected(self):
        registry = PrinterRegistry()
        registry.register("disconnected", _make_mock_adapter(
            status=PrinterStatus.IDLE, connected=False,
        ))
        registry.register("connected", _make_mock_adapter(
            status=PrinterStatus.IDLE, connected=True,
        ))

        idle = registry.get_idle_printers()
        assert idle == ["connected"]

    def test_empty_when_none_idle(self):
        registry = PrinterRegistry()
        registry.register("printing", _make_mock_adapter(status=PrinterStatus.PRINTING))
        assert registry.get_idle_printers() == []

    def test_excludes_erroring_printers(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter()
        adapter.get_state.side_effect = Exception("timeout")
        registry.register("broken", adapter)

        assert registry.get_idle_printers() == []

    def test_returns_sorted(self):
        registry = PrinterRegistry()
        registry.register("zebra", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("alpha", _make_mock_adapter(status=PrinterStatus.IDLE))

        assert registry.get_idle_printers() == ["alpha", "zebra"]


class TestGetPrintersByStatus:
    """Tests for get_printers_by_status."""

    def test_filter_by_printing(self):
        registry = PrinterRegistry()
        registry.register("idle", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("printing", _make_mock_adapter(status=PrinterStatus.PRINTING))

        result = registry.get_printers_by_status(PrinterStatus.PRINTING)
        assert result == ["printing"]

    def test_filter_by_idle(self):
        registry = PrinterRegistry()
        registry.register("a", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("b", _make_mock_adapter(status=PrinterStatus.IDLE))
        registry.register("c", _make_mock_adapter(status=PrinterStatus.ERROR))

        result = registry.get_printers_by_status(PrinterStatus.IDLE)
        assert sorted(result) == ["a", "b"]

    def test_offline_includes_erroring_adapters(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter()
        adapter.get_state.side_effect = Exception("network down")
        registry.register("dead", adapter)

        result = registry.get_printers_by_status(PrinterStatus.OFFLINE)
        assert result == ["dead"]

    def test_non_offline_excludes_erroring_adapters(self):
        registry = PrinterRegistry()
        adapter = _make_mock_adapter()
        adapter.get_state.side_effect = Exception("network down")
        registry.register("dead", adapter)

        result = registry.get_printers_by_status(PrinterStatus.IDLE)
        assert result == []

    def test_returns_sorted(self):
        registry = PrinterRegistry()
        registry.register("z-printer", _make_mock_adapter(status=PrinterStatus.PAUSED))
        registry.register("a-printer", _make_mock_adapter(status=PrinterStatus.PAUSED))

        result = registry.get_printers_by_status(PrinterStatus.PAUSED)
        assert result == ["a-printer", "z-printer"]

    def test_empty_registry(self):
        registry = PrinterRegistry()
        result = registry.get_printers_by_status(PrinterStatus.IDLE)
        assert result == []


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestPrinterRegistryThreadSafety:
    """Tests for thread-safe concurrent operations."""

    def test_concurrent_register(self):
        registry = PrinterRegistry()

        def register_printers(prefix: str, count: int) -> None:
            for i in range(count):
                registry.register(f"{prefix}-{i}", _make_mock_adapter(name=f"{prefix}-{i}"))

        threads = [
            threading.Thread(target=register_printers, args=(f"thread{t}", 20))
            for t in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert registry.count == 100

    def test_concurrent_register_and_lookup(self):
        registry = PrinterRegistry()
        errors: list[Exception] = []

        def register_batch() -> None:
            for i in range(20):
                registry.register(f"printer-{i}", _make_mock_adapter(name=f"printer-{i}"))

        def lookup_batch() -> None:
            for _ in range(50):
                try:
                    registry.list_names()
                    registry.list_all()
                    if "printer-0" in registry:
                        registry.get("printer-0")
                except PrinterNotFoundError:
                    pass
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=register_batch)
        t2 = threading.Thread(target=lookup_batch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0
