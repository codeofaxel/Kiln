"""Tests for physical-world device generalization.

Covers DeviceType enum, DeviceAdapter alias, extended PrinterCapabilities,
and optional CNC/laser methods on PrinterAdapter.
"""

from __future__ import annotations
from unittest import mock
import pytest

from kiln.printers import DeviceAdapter, DeviceType
from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
    JobProgress,
    PrinterFile,
    PrintResult,
    UploadResult,
)


class TestDeviceAdapterAlias:
    def test_alias_is_printer_adapter(self):
        assert DeviceAdapter is PrinterAdapter

    def test_importable_from_package(self):
        from kiln.printers import DeviceAdapter as DA
        assert DA is PrinterAdapter

    def test_subclass_via_device_adapter(self):
        """A concrete class inheriting DeviceAdapter satisfies isinstance checks."""
        class FakeCNC(DeviceAdapter):
            @property
            def name(self): return "fake_cnc"
            @property
            def capabilities(self): return PrinterCapabilities(device_type="cnc_router")
            def get_state(self): return PrinterState(connected=True, state=PrinterStatus.IDLE)
            def get_job(self): return JobProgress()
            def list_files(self): return []
            def upload_file(self, p): return UploadResult(success=True, file_name="f", message="ok")
            def start_print(self, f): return PrintResult(success=True, message="ok")
            def cancel_print(self): return PrintResult(success=True, message="ok")
            def pause_print(self): return PrintResult(success=True, message="ok")
            def resume_print(self): return PrintResult(success=True, message="ok")
            def set_tool_temp(self, t): return True
            def set_bed_temp(self, t): return True
            def send_gcode(self, c): return True
            def delete_file(self, p): return True
            def emergency_stop(self): return PrintResult(success=True, message="ok")

        cnc = FakeCNC()
        assert isinstance(cnc, PrinterAdapter)
        assert isinstance(cnc, DeviceAdapter)
        assert cnc.capabilities.device_type == "cnc_router"


class TestDeviceType:
    def test_fdm_printer(self):
        assert DeviceType.FDM_PRINTER.value == "fdm_printer"

    def test_sla_printer(self):
        assert DeviceType.SLA_PRINTER.value == "sla_printer"

    def test_cnc_router(self):
        assert DeviceType.CNC_ROUTER.value == "cnc_router"

    def test_laser_cutter(self):
        assert DeviceType.LASER_CUTTER.value == "laser_cutter"

    def test_generic(self):
        assert DeviceType.GENERIC.value == "generic"

    def test_all_members(self):
        assert len(DeviceType) == 5


class TestExtendedCapabilities:
    def test_default_device_type(self):
        caps = PrinterCapabilities()
        assert caps.device_type == "fdm_printer"

    def test_custom_device_type(self):
        caps = PrinterCapabilities(device_type="cnc_router")
        assert caps.device_type == "cnc_router"

    def test_can_snapshot_default_false(self):
        caps = PrinterCapabilities()
        assert caps.can_snapshot is False

    def test_can_snapshot_true(self):
        caps = PrinterCapabilities(can_snapshot=True)
        assert caps.can_snapshot is True

    def test_to_dict_includes_new_fields(self):
        caps = PrinterCapabilities(device_type="laser_cutter", can_snapshot=True)
        d = caps.to_dict()
        assert d["device_type"] == "laser_cutter"
        assert d["can_snapshot"] is True

    def test_existing_defaults_unchanged(self):
        caps = PrinterCapabilities()
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True
        assert caps.can_stream is False
        assert caps.can_probe_bed is False
        assert caps.can_update_firmware is False
        assert caps.supported_extensions == (".gcode", ".gco", ".g")


class TestOptionalDeviceMethods:
    """Test that optional CNC/laser methods raise or return None on the base."""

    def _make_adapter(self):
        """Create a minimal concrete adapter for testing optional methods."""
        class Stub(PrinterAdapter):
            @property
            def name(self): return "stub"
            @property
            def capabilities(self): return PrinterCapabilities()
            def get_state(self): return PrinterState(connected=True, state=PrinterStatus.IDLE)
            def get_job(self): return JobProgress()
            def list_files(self): return []
            def upload_file(self, p): return UploadResult(success=True, file_name="f", message="ok")
            def start_print(self, f): return PrintResult(success=True, message="ok")
            def cancel_print(self): return PrintResult(success=True, message="ok")
            def pause_print(self): return PrintResult(success=True, message="ok")
            def resume_print(self): return PrintResult(success=True, message="ok")
            def set_tool_temp(self, t): return True
            def set_bed_temp(self, t): return True
            def send_gcode(self, c): return True
            def delete_file(self, p): return True
            def emergency_stop(self): return PrintResult(success=True, message="ok")
        return Stub()

    def test_set_spindle_speed_raises(self):
        adapter = self._make_adapter()
        with pytest.raises(PrinterError, match="spindle"):
            adapter.set_spindle_speed(1000)

    def test_set_laser_power_raises(self):
        adapter = self._make_adapter()
        with pytest.raises(PrinterError, match="laser"):
            adapter.set_laser_power(50.0)

    def test_get_tool_position_returns_none(self):
        adapter = self._make_adapter()
        assert adapter.get_tool_position() is None


class TestBackwardCompatibility:
    """Ensure existing adapters are not broken by the new fields."""

    def test_octoprint_instantiates(self):
        from kiln.printers.octoprint import OctoPrintAdapter
        a = OctoPrintAdapter(host="http://test", api_key="key")
        assert a.name == "octoprint"
        assert a.capabilities.device_type == "fdm_printer"

    def test_moonraker_instantiates(self):
        from kiln.printers.moonraker import MoonrakerAdapter
        a = MoonrakerAdapter(host="http://test")
        assert a.name == "moonraker"
        assert a.capabilities.device_type == "fdm_printer"

    def test_bambu_instantiates(self):
        try:
            from kiln.printers.bambu import BambuAdapter
        except ImportError:
            pytest.skip("paho-mqtt not installed")
        a = BambuAdapter(host="192.168.1.1", access_code="12345678", serial="01P00A000000001")
        assert a.name == "bambu"

    def test_prusaconnect_instantiates(self):
        from kiln.printers.prusaconnect import PrusaConnectAdapter
        a = PrusaConnectAdapter(host="http://test")
        assert a.name == "prusaconnect"
