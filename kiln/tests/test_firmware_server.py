"""Tests for firmware MCP tools in kiln.server.

Covers:
- firmware_status — query firmware versions
- update_firmware — trigger firmware upgrade
- rollback_firmware — roll back to previous version
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    PrinterCapabilities,
    PrinterError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_caps(can_update: bool = True) -> PrinterCapabilities:
    return PrinterCapabilities(can_update_firmware=can_update)


def _make_firmware_status(
    busy: bool = False,
    updates: int = 1,
) -> FirmwareStatus:
    return FirmwareStatus(
        busy=busy,
        updates_available=updates,
        components=[
            FirmwareComponent(
                name="klipper",
                current_version="v0.12.0",
                remote_version="v0.12.1",
                update_available=True,
                component_type="git_repo",
                channel="stable",
            ),
        ],
    )


def _make_update_result(
    success: bool = True,
    component: str | None = None,
) -> FirmwareUpdateResult:
    return FirmwareUpdateResult(
        success=success,
        message="Update started." if success else "Failed.",
        component=component,
    )


# ---------------------------------------------------------------------------
# firmware_status
# ---------------------------------------------------------------------------


class TestFirmwareStatusTool:
    """Tests for the firmware_status MCP tool."""

    def test_returns_component_info(self) -> None:
        from kiln.server import firmware_status

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.get_firmware_status.return_value = _make_firmware_status()

        with patch("kiln.server._get_adapter", return_value=mock_adapter):
            result = firmware_status()

        assert result["success"] is True
        assert result["updates_available"] == 1
        assert len(result["components"]) == 1
        assert result["components"][0]["name"] == "klipper"

    def test_unsupported_printer(self) -> None:
        from kiln.server import firmware_status

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=False)

        with patch("kiln.server._get_adapter", return_value=mock_adapter):
            result = firmware_status()

        assert result["success"] is False
        assert "UNSUPPORTED" in result["error"]["code"]

    def test_status_unavailable(self) -> None:
        from kiln.server import firmware_status

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.get_firmware_status.return_value = None

        with patch("kiln.server._get_adapter", return_value=mock_adapter):
            result = firmware_status()

        assert result["success"] is False
        assert "UNAVAILABLE" in result["error"]["code"]

    def test_printer_error_handled(self) -> None:
        from kiln.server import firmware_status

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.get_firmware_status.side_effect = PrinterError("offline")

        with patch("kiln.server._get_adapter", return_value=mock_adapter):
            result = firmware_status()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# update_firmware
# ---------------------------------------------------------------------------


class TestUpdateFirmwareTool:
    """Tests for the update_firmware MCP tool."""

    def test_update_success(self) -> None:
        from kiln.server import update_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.update_firmware.return_value = _make_update_result()

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = update_firmware()

        assert result["success"] is True
        mock_adapter.update_firmware.assert_called_once_with(component=None)

    def test_update_specific_component(self) -> None:
        from kiln.server import update_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.update_firmware.return_value = _make_update_result(
            component="klipper",
        )

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = update_firmware(component="klipper")

        assert result["success"] is True
        assert result["component"] == "klipper"

    def test_unsupported_printer(self) -> None:
        from kiln.server import update_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=False)

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = update_firmware()

        assert result["success"] is False
        assert "UNSUPPORTED" in result["error"]["code"]

    def test_auth_rejected(self) -> None:
        from kiln.server import update_firmware

        with patch("kiln.server._check_auth", return_value={"success": False, "error": {"code": "AUTH", "message": "denied"}}):
            result = update_firmware()

        assert result["success"] is False

    def test_printer_error_handled(self) -> None:
        from kiln.server import update_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.update_firmware.side_effect = PrinterError("busy")

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = update_firmware()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# rollback_firmware
# ---------------------------------------------------------------------------


class TestRollbackFirmwareTool:
    """Tests for the rollback_firmware MCP tool."""

    def test_rollback_success(self) -> None:
        from kiln.server import rollback_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.rollback_firmware.return_value = FirmwareUpdateResult(
            success=True,
            message="Rollback started for klipper.",
            component="klipper",
        )

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = rollback_firmware(component="klipper")

        assert result["success"] is True
        assert result["component"] == "klipper"

    def test_unsupported_printer(self) -> None:
        from kiln.server import rollback_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=False)

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = rollback_firmware(component="klipper")

        assert result["success"] is False
        assert "UNSUPPORTED" in result["error"]["code"]

    def test_printer_error_handled(self) -> None:
        from kiln.server import rollback_firmware

        mock_adapter = MagicMock()
        mock_adapter.capabilities = _make_caps(can_update=True)
        mock_adapter.rollback_firmware.side_effect = PrinterError("no rollback")

        with patch("kiln.server._get_adapter", return_value=mock_adapter), \
             patch("kiln.server._check_auth", return_value=None):
            result = rollback_firmware(component="klipper")

        assert result["success"] is False
