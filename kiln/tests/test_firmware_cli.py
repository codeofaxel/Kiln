"""Tests for firmware CLI commands in kiln.cli.main.

Covers:
- kiln firmware status
- kiln firmware update
- kiln firmware rollback
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    PrinterCapabilities,
    PrinterError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


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
            FirmwareComponent(
                name="moonraker",
                current_version="v0.9.0",
                remote_version="v0.9.0",
                update_available=False,
                component_type="git_repo",
                channel="stable",
            ),
        ],
    )


def _make_adapter(can_update: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.capabilities = _make_caps(can_update=can_update)
    return adapter


# ---------------------------------------------------------------------------
# firmware status
# ---------------------------------------------------------------------------


class TestFirmwareStatusCLI:
    """Tests for 'kiln firmware status'."""

    def test_status_json_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.get_firmware_status.return_value = _make_firmware_status()

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["updates_available"] == 1
        assert len(data["data"]["components"]) == 2

    def test_status_text_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.get_firmware_status.return_value = _make_firmware_status()

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "status"])

        assert result.exit_code == 0
        assert "klipper" in result.output
        assert "v0.12.0" in result.output
        assert "v0.12.1" in result.output

    def test_status_unsupported_printer(self, runner) -> None:
        adapter = _make_adapter(can_update=False)

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "status", "--json"])

        assert result.exit_code != 0

    def test_status_unavailable(self, runner) -> None:
        adapter = _make_adapter()
        adapter.get_firmware_status.return_value = None

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "status", "--json"])

        assert result.exit_code != 0

    def test_status_busy_indicator(self, runner) -> None:
        adapter = _make_adapter()
        adapter.get_firmware_status.return_value = FirmwareStatus(
            busy=True, updates_available=0, components=[],
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "status"])

        assert result.exit_code == 0
        assert "progress" in result.output.lower()


# ---------------------------------------------------------------------------
# firmware update
# ---------------------------------------------------------------------------


class TestFirmwareUpdateCLI:
    """Tests for 'kiln firmware update'."""

    def test_update_json_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.update_firmware.return_value = FirmwareUpdateResult(
            success=True, message="Update started for all components.", component=None,
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "update", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["success"] is True

    def test_update_specific_component(self, runner) -> None:
        adapter = _make_adapter()
        adapter.update_firmware.return_value = FirmwareUpdateResult(
            success=True, message="Update started for klipper.", component="klipper",
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, [
                "firmware", "update", "--component", "klipper", "--json",
            ])

        assert result.exit_code == 0
        adapter.update_firmware.assert_called_once_with(component="klipper")

    def test_update_text_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.update_firmware.return_value = FirmwareUpdateResult(
            success=True, message="Update started for all components.", component=None,
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "update"])

        assert result.exit_code == 0
        assert "Update started" in result.output

    def test_update_unsupported(self, runner) -> None:
        adapter = _make_adapter(can_update=False)

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "update", "--json"])

        assert result.exit_code != 0

    def test_update_failure_exits_nonzero(self, runner) -> None:
        adapter = _make_adapter()
        adapter.update_firmware.return_value = FirmwareUpdateResult(
            success=False, message="Update failed: busy.", component=None,
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "update"])

        assert result.exit_code != 0

    def test_update_exception(self, runner) -> None:
        adapter = _make_adapter()
        adapter.update_firmware.side_effect = PrinterError("connection lost")

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "update", "--json"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# firmware rollback
# ---------------------------------------------------------------------------


class TestFirmwareRollbackCLI:
    """Tests for 'kiln firmware rollback'."""

    def test_rollback_json_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.rollback_firmware.return_value = FirmwareUpdateResult(
            success=True, message="Rollback started for klipper.", component="klipper",
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, [
                "firmware", "rollback", "klipper", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["component"] == "klipper"

    def test_rollback_text_output(self, runner) -> None:
        adapter = _make_adapter()
        adapter.rollback_firmware.return_value = FirmwareUpdateResult(
            success=True, message="Rollback started for klipper.", component="klipper",
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "rollback", "klipper"])

        assert result.exit_code == 0
        assert "Rollback started" in result.output

    def test_rollback_unsupported(self, runner) -> None:
        adapter = _make_adapter(can_update=False)

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, [
                "firmware", "rollback", "klipper", "--json",
            ])

        assert result.exit_code != 0

    def test_rollback_missing_component_arg(self, runner) -> None:
        """Rollback requires a COMPONENT argument."""
        result = runner.invoke(cli, ["firmware", "rollback", "--json"])
        assert result.exit_code != 0

    def test_rollback_failure_exits_nonzero(self, runner) -> None:
        adapter = _make_adapter()
        adapter.rollback_firmware.return_value = FirmwareUpdateResult(
            success=False, message="No rollback available.", component="klipper",
        )

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            result = runner.invoke(cli, ["firmware", "rollback", "klipper"])

        assert result.exit_code != 0
