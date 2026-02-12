"""Tests for filament sensor preflight check and adapter methods.

Covers:
- PrinterCapabilities.can_detect_filament default value
- PrinterAdapter.get_filament_status() default returns None
- OctoPrintAdapter.get_filament_status() with plugin available
- OctoPrintAdapter.get_filament_status() with plugin not installed
- MoonrakerAdapter.get_filament_status() with sensor configured
- MoonrakerAdapter.get_filament_status() with no sensor
- preflight_check includes filament warning when sensor detects no filament
- preflight_check includes filament OK when sensor detects filament
- preflight_check skips filament when adapter lacks capability
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import responses

from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.server import preflight_check


OCTOPRINT_HOST = "http://octopi.local"
OCTOPRINT_API_KEY = "TESTAPIKEY123"
MOONRAKER_HOST = "http://klipper.local"


# ---------------------------------------------------------------------------
# PrinterCapabilities defaults
# ---------------------------------------------------------------------------


class TestFilamentCapability:

    def test_can_detect_filament_default_false(self):
        caps = PrinterCapabilities()
        assert caps.can_detect_filament is False

    def test_can_detect_filament_in_to_dict(self):
        caps = PrinterCapabilities(can_detect_filament=True)
        d = caps.to_dict()
        assert d["can_detect_filament"] is True

    def test_octoprint_capabilities_include_filament(self):
        adapter = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        assert adapter.capabilities.can_detect_filament is True

    def test_moonraker_capabilities_include_filament(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST)
        assert adapter.capabilities.can_detect_filament is True


# ---------------------------------------------------------------------------
# Base adapter default
# ---------------------------------------------------------------------------


class TestBaseFilamentStatus:

    def test_default_returns_none(self):
        """The base class default get_filament_status returns None."""
        # Use a MagicMock that inherits from PrinterAdapter to test default
        class StubAdapter(PrinterAdapter):
            @property
            def name(self):
                return "stub"

            @property
            def capabilities(self):
                return PrinterCapabilities()

            def get_state(self):
                pass

            def get_job(self):
                pass

            def list_files(self):
                pass

            def upload_file(self, file_path):
                pass

            def start_print(self, file_name):
                pass

            def cancel_print(self):
                pass

            def pause_print(self):
                pass

            def resume_print(self):
                pass

            def emergency_stop(self):
                pass

            def set_tool_temp(self, target):
                pass

            def set_bed_temp(self, target):
                pass

            def send_gcode(self, commands):
                pass

            def delete_file(self, file_path):
                pass

        stub = StubAdapter()
        assert stub.get_filament_status() is None


# ---------------------------------------------------------------------------
# OctoPrint adapter filament sensor
# ---------------------------------------------------------------------------


class TestOctoPrintFilamentStatus:

    @responses.activate
    def test_filament_detected(self):
        adapter = OctoPrintAdapter(
            host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=1,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/plugin/filamentmanager",
            json={"selections": [{"spool": {"id": 1, "name": "PLA"}}]},
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is True
        assert result["sensor_enabled"] is True
        assert result["source"] == "filamentmanager_plugin"

    @responses.activate
    def test_filament_not_detected_empty_selections(self):
        adapter = OctoPrintAdapter(
            host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=1,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/plugin/filamentmanager",
            json={"selections": []},
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is False

    @responses.activate
    def test_filament_not_detected_null_spool(self):
        adapter = OctoPrintAdapter(
            host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=1,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/plugin/filamentmanager",
            json={"selections": [{"spool": None}]},
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is False

    @responses.activate
    def test_plugin_not_installed_returns_none(self):
        adapter = OctoPrintAdapter(
            host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=1,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/plugin/filamentmanager",
            status=404,
        )
        result = adapter.get_filament_status()
        assert result is None

    @responses.activate
    def test_connection_error_returns_none(self):
        adapter = OctoPrintAdapter(
            host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=1,
        )
        # No response registered -- will raise ConnectionError
        result = adapter.get_filament_status()
        assert result is None


# ---------------------------------------------------------------------------
# Moonraker adapter filament sensor
# ---------------------------------------------------------------------------


class TestMoonrakerFilamentStatus:

    @responses.activate
    def test_filament_detected(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        responses.add(
            responses.GET,
            f"{MOONRAKER_HOST}/printer/objects/query",
            json={
                "result": {
                    "status": {
                        "filament_switch_sensor": {
                            "filament_detected": True,
                            "enabled": True,
                        }
                    }
                }
            },
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is True
        assert result["sensor_enabled"] is True
        assert result["source"] == "klipper_filament_switch_sensor"

    @responses.activate
    def test_filament_not_detected(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        responses.add(
            responses.GET,
            f"{MOONRAKER_HOST}/printer/objects/query",
            json={
                "result": {
                    "status": {
                        "filament_switch_sensor": {
                            "filament_detected": False,
                            "enabled": True,
                        }
                    }
                }
            },
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is False
        assert result["sensor_enabled"] is True

    @responses.activate
    def test_sensor_not_configured_returns_none(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        responses.add(
            responses.GET,
            f"{MOONRAKER_HOST}/printer/objects/query",
            json={"result": {"status": {}}},
            status=200,
        )
        result = adapter.get_filament_status()
        assert result is None

    @responses.activate
    def test_connection_error_returns_none(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        # No response registered -- will raise ConnectionError
        result = adapter.get_filament_status()
        assert result is None


# ---------------------------------------------------------------------------
# Preflight check filament integration
# ---------------------------------------------------------------------------


class TestPreflightFilamentCheck:

    def _idle_state(self):
        return PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            tool_temp_actual=24.5,
            tool_temp_target=0.0,
            bed_temp_actual=23.1,
            bed_temp_target=0.0,
        )

    @patch("kiln.server._get_adapter")
    def test_filament_detected_adds_pass_check(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=True)
        adapter.get_filament_status.return_value = {
            "detected": True,
            "sensor_enabled": True,
        }
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        filament_check = next(
            (c for c in result["checks"] if c["name"] == "filament_loaded"), None,
        )
        assert filament_check is not None
        assert filament_check["passed"] is True
        assert "detected" in filament_check["message"].lower()

    @patch("kiln.server._get_adapter")
    def test_filament_not_detected_adds_warning(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=True)
        adapter.get_filament_status.return_value = {
            "detected": False,
            "sensor_enabled": True,
        }
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        filament_check = next(
            (c for c in result["checks"] if c["name"] == "filament_loaded"), None,
        )
        assert filament_check is not None
        assert filament_check["passed"] is True  # Warning only, not blocking
        assert "advisory" in filament_check
        assert filament_check["advisory"] is True
        assert "WARNING" in filament_check["message"]
        # Should not block the print (ready should still be True if all else passes)
        assert result["ready"] is True

    @patch("kiln.server._get_adapter")
    def test_filament_sensor_not_available_skips_check(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=True)
        adapter.get_filament_status.return_value = None
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        filament_checks = [c for c in result["checks"] if c["name"] == "filament_loaded"]
        assert len(filament_checks) == 0

    @patch("kiln.server._get_adapter")
    def test_no_filament_capability_skips_check(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=False)
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        filament_checks = [c for c in result["checks"] if c["name"] == "filament_loaded"]
        assert len(filament_checks) == 0
        adapter.get_filament_status.assert_not_called()

    @patch("kiln.server._get_adapter")
    def test_filament_sensor_exception_skips_silently(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=True)
        adapter.get_filament_status.side_effect = Exception("sensor error")
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        assert result["ready"] is True
        filament_checks = [c for c in result["checks"] if c["name"] == "filament_loaded"]
        assert len(filament_checks) == 0

    @patch("kiln.server._get_adapter")
    def test_sensor_disabled_skips_check(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.get_state.return_value = self._idle_state()
        adapter.capabilities = PrinterCapabilities(can_detect_filament=True)
        adapter.get_filament_status.return_value = {
            "detected": False,
            "sensor_enabled": False,
        }
        mock_get_adapter.return_value = adapter

        result = preflight_check()
        assert result["success"] is True
        # Sensor disabled -- no filament check should be added
        filament_checks = [c for c in result["checks"] if c["name"] == "filament_loaded"]
        assert len(filament_checks) == 0
