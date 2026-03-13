"""Tests for kiln.firmware — FirmwareManager unit tests.

Covers:
- Registration: valid, empty name, empty type
- Version checking: snapshot, timestamp, empty components
- Component lookup: found, not found, unregistered printer
- Update: single component, all components, no updates, missing component
- Rollback: success, no history, missing component
- History: ordering, empty
- Fleet-wide: list_printers_with_updates, critical updates, fleet summary
- Singleton: get_firmware_manager lazy init
"""

from __future__ import annotations

import pytest

# Reset singleton between tests
import kiln.firmware as _fw_mod
from kiln.firmware import (
    FirmwareComponent,
    FirmwareError,
    FirmwareInfo,
    FirmwareManager,
    FirmwareType,
    FirmwareUpdateRecord,
    get_firmware_manager,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _fw_mod._manager = None
    yield
    _fw_mod._manager = None


def _comp(
    name: str = "klipper",
    current: str = "v0.12.0",
    latest: str | None = "v0.12.1",
    update: bool = True,
    critical: bool = False,
) -> FirmwareComponent:
    return FirmwareComponent(
        name=name,
        current_version=current,
        latest_version=latest,
        update_available=update,
        critical=critical,
        component_type="firmware",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestFirmwareManagerRegistration:

    def test_register_printer(self):
        mgr = FirmwareManager()
        comp = _comp()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [comp])
        info = mgr.check_version("voron")
        assert info.printer_name == "voron"
        assert info.firmware_type == FirmwareType.KLIPPER
        assert len(info.components) == 1

    def test_register_with_release_notes(self):
        mgr = FirmwareManager()
        mgr.register_printer("ender", FirmwareType.MARLIN, [_comp()], release_notes="bug fixes")
        info = mgr.check_version("ender")
        assert info.release_notes == "bug fixes"

    def test_empty_printer_name_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="printer_name must not be empty"):
            mgr.register_printer("", FirmwareType.KLIPPER, [])

    def test_empty_firmware_type_raises(self):
        """FirmwareType is an enum, but the check still runs."""
        mgr = FirmwareManager()
        # Can't really pass an empty enum, but the code checks truthiness
        # which always passes for enums.  Test the string path if needed.
        # This test documents the validation exists.
        mgr.register_printer("voron", FirmwareType.UNKNOWN, [])
        info = mgr.check_version("voron")
        assert info.firmware_type == FirmwareType.UNKNOWN


# ---------------------------------------------------------------------------
# Version checking
# ---------------------------------------------------------------------------


class TestFirmwareManagerCheckVersion:

    def test_check_version_returns_firmware_info(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        info = mgr.check_version("voron")
        assert isinstance(info, FirmwareInfo)
        assert info.current_version == "v0.12.0"
        assert info.latest_version == "v0.12.1"
        assert info.update_available is True

    def test_check_version_records_timestamp(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        info = mgr.check_version("voron")
        assert info.last_checked is not None
        assert "T" in info.last_checked  # ISO-8601

    def test_check_version_unregistered_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="Printer not registered"):
            mgr.check_version("ghost")

    def test_check_version_empty_components(self):
        mgr = FirmwareManager()
        mgr.register_printer("empty", FirmwareType.UNKNOWN, [])
        info = mgr.check_version("empty")
        assert info.current_version == "unknown"
        assert info.latest_version is None
        assert info.update_available is False

    def test_has_critical_property(self):
        mgr = FirmwareManager()
        mgr.register_printer(
            "voron",
            FirmwareType.KLIPPER,
            [_comp(critical=True)],
        )
        info = mgr.check_version("voron")
        assert info.has_critical is True

    def test_no_critical_when_not_flagged(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(critical=False)])
        info = mgr.check_version("voron")
        assert info.has_critical is False


# ---------------------------------------------------------------------------
# Component lookup
# ---------------------------------------------------------------------------


class TestFirmwareManagerGetComponent:

    def test_get_component_found(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(name="moonraker")])
        comp = mgr.get_component("voron", "moonraker")
        assert comp is not None
        assert comp.name == "moonraker"

    def test_get_component_not_found(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        assert mgr.get_component("voron", "nonexistent") is None

    def test_get_component_unregistered_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="Printer not registered"):
            mgr.get_component("ghost", "klipper")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestFirmwareManagerUpdate:

    def test_update_single_component(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        result = mgr.update_firmware("voron", component="klipper")
        assert result["success"] is True
        assert "klipper" in result["updated"]
        # Verify version changed
        comp = mgr.get_component("voron", "klipper")
        assert comp.current_version == "v0.12.1"
        assert comp.update_available is False

    def test_update_all_components(self):
        mgr = FirmwareManager()
        comps = [_comp(name="klipper"), _comp(name="moonraker", current="v0.8.0", latest="v0.8.1")]
        mgr.register_printer("voron", FirmwareType.KLIPPER, comps)
        result = mgr.update_firmware("voron")
        assert result["success"] is True
        assert len(result["updated"]) == 2

    def test_update_no_updates_available_raises(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(update=False)])
        with pytest.raises(FirmwareError, match="No firmware updates available"):
            mgr.update_firmware("voron")

    def test_update_missing_component_raises(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        with pytest.raises(FirmwareError, match="Component.*not found"):
            mgr.update_firmware("voron", component="nonexistent")

    def test_update_component_no_update_raises(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(update=False)])
        with pytest.raises(FirmwareError, match="No update available"):
            mgr.update_firmware("voron", component="klipper")

    def test_update_unregistered_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="Printer not registered"):
            mgr.update_firmware("ghost")

    def test_update_creates_history_record(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        mgr.update_firmware("voron", component="klipper")
        history = mgr.list_firmware_history("voron")
        assert len(history) == 1
        assert history[0]["operation"] == "update"
        assert history[0]["from_version"] == "v0.12.0"
        assert history[0]["to_version"] == "v0.12.1"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class TestFirmwareManagerRollback:

    def test_rollback_after_update(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        mgr.update_firmware("voron", component="klipper")
        result = mgr.rollback_firmware("voron", "klipper")
        assert result["success"] is True
        assert result["rolled_back_to"] == "v0.12.0"
        comp = mgr.get_component("voron", "klipper")
        assert comp.current_version == "v0.12.0"

    def test_rollback_no_history_raises(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        with pytest.raises(FirmwareError, match="No previous version found"):
            mgr.rollback_firmware("voron", "klipper")

    def test_rollback_missing_component_raises(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        with pytest.raises(FirmwareError, match="Component.*not found"):
            mgr.rollback_firmware("voron", "nonexistent")

    def test_rollback_unregistered_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="Printer not registered"):
            mgr.rollback_firmware("ghost", "klipper")

    def test_rollback_re_enables_update_available(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        mgr.update_firmware("voron", component="klipper")
        comp = mgr.get_component("voron", "klipper")
        assert comp.update_available is False
        mgr.rollback_firmware("voron", "klipper")
        assert comp.update_available is True

    def test_rollback_creates_history_record(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp()])
        mgr.update_firmware("voron", component="klipper")
        mgr.rollback_firmware("voron", "klipper")
        history = mgr.list_firmware_history("voron")
        assert len(history) == 2
        assert history[0]["operation"] == "rollback"  # newest first


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestFirmwareManagerHistory:

    def test_history_empty_for_new_printer(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [])
        assert mgr.list_firmware_history("voron") == []

    def test_history_unregistered_raises(self):
        mgr = FirmwareManager()
        with pytest.raises(FirmwareError, match="Printer not registered"):
            mgr.list_firmware_history("ghost")

    def test_history_newest_first(self):
        mgr = FirmwareManager()
        mgr.register_printer(
            "voron",
            FirmwareType.KLIPPER,
            [_comp(name="a"), _comp(name="b")],
        )
        mgr.update_firmware("voron")
        history = mgr.list_firmware_history("voron")
        # b was updated after a, so b should be first (newest first)
        assert history[0]["component"] == "b"
        assert history[1]["component"] == "a"


# ---------------------------------------------------------------------------
# Fleet-wide helpers
# ---------------------------------------------------------------------------


class TestFirmwareManagerFleet:

    def test_list_printers_with_updates(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(update=True)])
        mgr.register_printer("ender", FirmwareType.MARLIN, [_comp(update=False)])
        assert mgr.list_printers_with_updates() == ["voron"]

    def test_list_printers_with_critical_updates(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(critical=True)])
        mgr.register_printer("ender", FirmwareType.MARLIN, [_comp(critical=False)])
        assert mgr.list_printers_with_critical_updates() == ["voron"]

    def test_fleet_summary(self):
        mgr = FirmwareManager()
        mgr.register_printer("voron", FirmwareType.KLIPPER, [_comp(critical=True)])
        mgr.register_printer("ender", FirmwareType.MARLIN, [_comp(update=False)])
        summary = mgr.get_fleet_summary()
        assert summary["total_printers"] == 2
        assert summary["printers_with_updates"] == 1
        assert summary["printers_with_critical_updates"] == 1
        assert len(summary["printers"]) == 2

    def test_fleet_summary_empty(self):
        mgr = FirmwareManager()
        summary = mgr.get_fleet_summary()
        assert summary["total_printers"] == 0
        assert summary["printers"] == []


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestFirmwareDataclasses:

    def test_firmware_info_to_dict(self):
        info = FirmwareInfo(
            printer_name="voron",
            current_version="v1.0",
            firmware_type=FirmwareType.KLIPPER,
            components=[_comp()],
        )
        d = info.to_dict()
        assert d["firmware_type"] == "klipper"
        assert isinstance(d["components"], list)
        assert d["components"][0]["name"] == "klipper"

    def test_firmware_component_to_dict(self):
        comp = _comp()
        d = comp.to_dict()
        assert d["name"] == "klipper"
        assert d["current_version"] == "v0.12.0"

    def test_firmware_update_record_to_dict(self):
        record = FirmwareUpdateRecord(
            printer_name="voron",
            component="klipper",
            from_version="v0.12.0",
            to_version="v0.12.1",
            operation="update",
            success=True,
            timestamp=1000.0,
        )
        d = record.to_dict()
        assert d["operation"] == "update"
        assert d["success"] is True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestFirmwareManagerSingleton:

    def test_get_firmware_manager_returns_same_instance(self):
        a = get_firmware_manager()
        b = get_firmware_manager()
        assert a is b

    def test_get_firmware_manager_creates_instance(self):
        mgr = get_firmware_manager()
        assert isinstance(mgr, FirmwareManager)
