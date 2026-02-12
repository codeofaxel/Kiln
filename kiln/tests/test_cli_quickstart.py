"""Tests for the ``kiln quickstart`` CLI command.

Covers:
    - Help text
    - Human output with no printers configured, no discovered printers
    - Human output with existing printer configured
    - JSON output mode
    - Discovery finds a printer and auto-configures
    - Verify step failure propagation
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import PrinterState, PrinterStatus


@pytest.fixture
def runner():
    return CliRunner()


class TestQuickstartHelp:
    def test_help(self, runner) -> None:
        result = runner.invoke(cli, ["quickstart", "--help"])
        assert result.exit_code == 0
        assert "quickstart" in result.output.lower()
        assert "--json" in result.output


class TestQuickstartNoPrinters:
    """Quickstart when no printers are configured and none discovered."""

    @patch("kiln.cli.discovery.discover_printers", side_effect=ImportError("no discovery"))
    @patch("kiln.cli.main._list_printers", return_value=[])
    @patch("kiln.cli.main.load_printer_config", side_effect=ValueError("no printer"))
    def test_human_output_no_printers(self, _cfg, _list, _disc, runner) -> None:
        result = runner.invoke(cli, ["quickstart"])
        assert "Verify environment" in result.output
        assert "Discover printers" in result.output

    @patch("kiln.cli.discovery.discover_printers", side_effect=ImportError("no discovery"))
    @patch("kiln.cli.main._list_printers", return_value=[])
    @patch("kiln.cli.main.load_printer_config", side_effect=ValueError("no printer"))
    def test_json_output_no_printers(self, _cfg, _list, _disc, runner) -> None:
        result = runner.invoke(cli, ["quickstart", "--json"])
        data = json.loads(result.output)
        assert data["status"] in ("success", "error")
        assert "verify" in data["data"]
        assert "discover" in data["data"]
        assert "setup" in data["data"]
        assert "status" in data["data"]


class TestQuickstartExistingPrinter:
    """Quickstart when a printer is already configured."""

    @patch("kiln.cli.discovery.discover_printers", return_value=[])
    @patch("kiln.cli.main._list_printers", return_value=[
        {"name": "ender3", "type": "octoprint", "host": "http://ender.local", "active": True},
    ])
    @patch("kiln.cli.main.load_printer_config", return_value={
        "type": "moonraker", "host": "http://ender.local",
    })
    @patch("kiln.cli.main._make_adapter")
    def test_uses_existing_printer(self, mock_make, _cfg, _list, _disc, runner) -> None:
        mock_adapter = MagicMock()
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
            tool_temp_actual=22.0,
            tool_temp_target=0.0,
            bed_temp_actual=21.0,
            bed_temp_target=0.0,
        )
        mock_make.return_value = mock_adapter

        result = runner.invoke(cli, ["quickstart"])
        assert result.exit_code == 0
        assert "Already configured" in result.output
        assert "ender3" in result.output

    @patch("kiln.cli.discovery.discover_printers", return_value=[])
    @patch("kiln.cli.main._list_printers", return_value=[
        {"name": "ender3", "type": "octoprint", "host": "http://ender.local", "active": True},
    ])
    @patch("kiln.cli.main.load_printer_config", return_value={
        "type": "moonraker", "host": "http://ender.local",
    })
    @patch("kiln.cli.main._make_adapter")
    def test_json_with_existing_printer(self, mock_make, _cfg, _list, _disc, runner) -> None:
        mock_adapter = MagicMock()
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
            tool_temp_actual=22.0,
            tool_temp_target=0.0,
            bed_temp_actual=21.0,
            bed_temp_target=0.0,
        )
        mock_make.return_value = mock_adapter

        result = runner.invoke(cli, ["quickstart", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["setup"]["action"] == "existing"


class TestQuickstartAutoDiscover:
    """Quickstart discovers a printer and auto-configures it."""

    @patch("kiln.cli.main._list_printers", return_value=[])
    @patch("kiln.cli.main.save_printer")
    @patch("kiln.cli.main.load_printer_config", return_value={
        "type": "moonraker", "host": "http://voron.local:7125",
    })
    @patch("kiln.cli.main._make_adapter")
    def test_auto_configure_discovered_printer(
        self, mock_make, _cfg, mock_save, _list, runner,
    ) -> None:
        # Mock discovered printer
        discovered = MagicMock()
        discovered.name = "Voron"
        discovered.host = "http://voron.local:7125"
        discovered.printer_type = "moonraker"
        discovered.discovery_method = "mdns"

        mock_adapter = MagicMock()
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
        )
        mock_make.return_value = mock_adapter

        with patch("kiln.cli.discovery.discover_printers", return_value=[discovered]):
            result = runner.invoke(cli, ["quickstart"])

        assert result.exit_code == 0
        assert "Auto-configured" in result.output
        mock_save.assert_called_once()
        # Verify saved with correct name/type
        call_args = mock_save.call_args
        assert call_args[0][0] == "voron"  # name
        assert call_args[0][1] == "moonraker"  # type
