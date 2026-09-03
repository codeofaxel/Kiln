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

from unittest.mock import MagicMock, patch

import responses

from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.moonraker import MoonrakerAdapter
from kiln.printers.octoprint import OctoPrintAdapter
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
            # Filament handling is part of the adapter contract; these stubs
            # never move filament, so the hooks refuse.
            def _load_filament_impl(self, plan):
                raise NotImplementedError
            def _unload_filament_impl(self, plan):
                raise NotImplementedError
            def _purge_filament_impl(self, plan):
                raise NotImplementedError

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

            def _start_print_impl(self, file_name):
                pass

            def cancel_print(self):
                pass

            def pause_print(self):
                pass

            def _resume_print_impl(self):
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
    """Fixtures mirror payloads captured from a real Klipper instance.

    Klipper registers every runout sensor under its full config section name
    (``"<type> <name>"``) -- both sensor modules are ``load_config_prefix``-only,
    so a bare ``[filament_switch_sensor]`` section cannot exist.  Fixtures that
    use a bare, un-namespaced key describe a printer that cannot exist, which is
    how a broken query passed its tests while never matching real hardware.
    """

    @staticmethod
    def _mock(sensors: dict[str, dict], *, extra_objects: list[str] | None = None) -> None:
        """Register objects/list + objects/query the way Moonraker answers them."""
        objects = ["gcode", "toolhead", "extruder", *(extra_objects or []), *sensors]
        responses.add(
            responses.GET,
            f"{MOONRAKER_HOST}/printer/objects/list",
            json={"result": {"objects": objects}},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{MOONRAKER_HOST}/printer/objects/query",
            json={"result": {"eventtime": 1234.5, "status": dict(sensors)},
                  },
            status=200,
        )

    @responses.activate
    def test_named_switch_sensor_detected(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({"filament_switch_sensor runout": {"filament_detected": True, "enabled": True}})
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is True
        assert result["sensor_enabled"] is True
        assert result["sensor_name"] == "filament_switch_sensor runout"
        assert result["source"] == "klipper_filament_switch_sensor"

    @responses.activate
    def test_named_motion_sensor_detected(self):
        """Regression: encoder sensors were invisible -- only switches were queried.

        Payload captured verbatim from a live Klipper instance.
        """
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({"filament_motion_sensor runout_sensor": {"filament_detected": True, "enabled": True}})
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is True
        assert result["sensor_enabled"] is True
        assert result["sensor_name"] == "filament_motion_sensor runout_sensor"
        assert result["source"] == "klipper_filament_motion_sensor"

    @responses.activate
    def test_filament_not_detected(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({"filament_switch_sensor runout": {"filament_detected": False, "enabled": True}})
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is False
        assert result["sensor_enabled"] is True

    @responses.activate
    def test_any_armed_sensor_reporting_runout_wins(self):
        """Two sensors, one out of filament -- must report runout, not average it away."""
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock(
            {
                "filament_switch_sensor left": {"filament_detected": True, "enabled": True},
                "filament_motion_sensor right": {"filament_detected": False, "enabled": True},
            }
        )
        result = adapter.get_filament_status()
        assert result is not None
        assert result["detected"] is False
        assert result["sensor_name"] == "filament_motion_sensor right"
        assert len(result["sensors"]) == 2

    @responses.activate
    def test_disabled_sensor_still_reports_its_reading(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({"filament_switch_sensor runout": {"filament_detected": True, "enabled": False}})
        result = adapter.get_filament_status()
        assert result is not None
        assert result["sensor_enabled"] is False
        assert result["detected"] is True

    @responses.activate
    def test_no_sensor_configured_returns_none(self):
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({})
        result = adapter.get_filament_status()
        assert result is None

    @responses.activate
    def test_unrelated_filament_objects_are_not_sensors(self):
        """``filament_switch_sensor`` must match as a section prefix, not a substring."""
        adapter = MoonrakerAdapter(host=MOONRAKER_HOST, retries=1)
        self._mock({}, extra_objects=["filament_switch_sensor_helper", "tmc2209 extruder"])
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
