"""Tests for kiln.cli.main — CLI commands using Click's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import (
    JobProgress,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_adapter():
    """Return a mock PrinterAdapter with sensible defaults."""
    adapter = MagicMock()
    adapter.name = "mock"
    adapter.get_state.return_value = PrinterState(
        state=PrinterStatus.IDLE,
        connected=True,
        tool_temp_actual=22.0,
        tool_temp_target=0.0,
        bed_temp_actual=21.0,
        bed_temp_target=0.0,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=None,
        completion=None,
        print_time_seconds=None,
        print_time_left_seconds=None,
    )
    adapter.list_files.return_value = [
        PrinterFile(name="test.gcode", path="/test.gcode", size_bytes=1024, date=None),
    ]
    adapter.upload_file.return_value = UploadResult(
        success=True,
        message="Uploaded test.gcode",
        file_name="test.gcode",
    )
    adapter.start_print.return_value = PrintResult(
        success=True,
        message="Print started: test.gcode",
    )
    adapter.cancel_print.return_value = PrintResult(
        success=True,
        message="Print cancelled.",
    )
    adapter.pause_print.return_value = PrintResult(
        success=True,
        message="Print paused.",
    )
    adapter.resume_print.return_value = PrintResult(
        success=True,
        message="Print resumed.",
    )
    adapter.set_tool_temp.return_value = True
    adapter.set_bed_temp.return_value = True
    adapter.send_gcode.return_value = None
    adapter.capabilities = MagicMock(can_send_gcode=True)
    return adapter


@pytest.fixture
def config_file(tmp_path):
    """Create a temporary config file with one printer."""
    import yaml
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "active_printer": "test-printer",
        "printers": {
            "test-printer": {
                "type": "moonraker",
                "host": "http://test.local:7125",
            },
        },
        "settings": {"timeout": 30, "retries": 3},
    }))
    return cfg_path


def _patch_adapter(mock_adapter, config_file):
    """Return patch context managers for adapter and config."""
    return (
        patch("kiln.cli.main._make_adapter", return_value=mock_adapter),
        patch("kiln.cli.main.load_printer_config", return_value={
            "type": "moonraker",
            "host": "http://test.local:7125",
            "timeout": 30,
            "retries": 3,
        }),
        patch("kiln.cli.main.validate_printer_config", return_value=(True, None)),
    )


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Kiln" in result.output

    def test_subcommand_help(self, runner):
        for cmd in ["discover", "auth", "status", "files", "upload", "print",
                     "cancel", "pause", "resume", "temp", "gcode", "printers",
                     "use", "serve"]:
            result = runner.invoke(cli, [cmd, "--help"])
            assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_human(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "idle" in result.output.lower()

    def test_status_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "printer" in data["data"]


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


class TestFiles:
    def test_files_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["files", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["count"] == 1
        assert data["data"]["files"][0]["name"] == "test.gcode"


# ---------------------------------------------------------------------------
# print
# ---------------------------------------------------------------------------


class TestPrint:
    def test_print_status(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", "--status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_print_start(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", "test.gcode", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        mock_adapter.start_print.assert_called_once_with("test.gcode")

    def test_print_no_args_shows_status(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "printer" in data["data"]


# ---------------------------------------------------------------------------
# cancel / pause / resume
# ---------------------------------------------------------------------------


class TestJobControl:
    def test_cancel_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["cancel", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        mock_adapter.cancel_print.assert_called_once()

    def test_pause_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["pause", "--json"])
        assert result.exit_code == 0
        mock_adapter.pause_print.assert_called_once()

    def test_resume_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["resume", "--json"])
        assert result.exit_code == 0
        mock_adapter.resume_print.assert_called_once()


# ---------------------------------------------------------------------------
# temp
# ---------------------------------------------------------------------------


class TestTemp:
    def test_get_temps(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["temp", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["tool_actual"] == 22.0

    def test_set_tool_temp(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["temp", "--tool", "200", "--json"])
        assert result.exit_code == 0
        mock_adapter.set_tool_temp.assert_called_once_with(200.0)

    def test_set_bed_temp(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["temp", "--bed", "60", "--json"])
        assert result.exit_code == 0
        mock_adapter.set_bed_temp.assert_called_once_with(60.0)


# ---------------------------------------------------------------------------
# gcode
# ---------------------------------------------------------------------------


class TestGcode:
    def test_send_gcode(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["gcode", "G28", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        mock_adapter.send_gcode.assert_called_once()

    def test_blocked_gcode(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["gcode", "M112", "--json"])
        assert result.exit_code != 0
        assert "blocked" in result.output.lower() or "GCODE_BLOCKED" in result.output


# ---------------------------------------------------------------------------
# printers / use / remove
# ---------------------------------------------------------------------------


class TestPrinterManagement:
    def test_printers_json(self, runner, tmp_path):
        import yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "active_printer": "p1",
            "printers": {"p1": {"type": "moonraker", "host": "http://p1"}},
        }))
        with patch("kiln.cli.main._list_printers") as mock_list:
            mock_list.return_value = [
                {"name": "p1", "type": "moonraker", "host": "http://p1", "active": True},
            ]
            result = runner.invoke(cli, ["printers", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 1

    def test_use(self, runner, tmp_path):
        import yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "active_printer": "a",
            "printers": {"a": {"type": "moonraker", "host": "http://a"},
                         "b": {"type": "moonraker", "host": "http://b"}},
        }))
        with patch("kiln.cli.main.set_active_printer") as mock_set:
            result = runner.invoke(cli, ["use", "b"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("b")

    def test_use_not_found(self, runner):
        with patch("kiln.cli.main.set_active_printer", side_effect=ValueError("not found")):
            result = runner.invoke(cli, ["use", "nope"])
        assert result.exit_code != 0

    def test_remove(self, runner):
        with patch("kiln.cli.main.remove_printer") as mock_rm:
            result = runner.invoke(cli, ["remove", "old"])
        assert result.exit_code == 0
        mock_rm.assert_called_once_with("old")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_auth_octoprint(self, runner):
        with patch("kiln.cli.main.save_printer", return_value=Path("/tmp/config.yaml")):
            result = runner.invoke(cli, [
                "auth",
                "--name", "ender",
                "--host", "http://octopi.local",
                "--type", "octoprint",
                "--api-key", "abc123",
                "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["name"] == "ender"

    def test_auth_bambu(self, runner):
        with patch("kiln.cli.main.save_printer", return_value=Path("/tmp/config.yaml")):
            result = runner.invoke(cli, [
                "auth",
                "--name", "x1c",
                "--host", "192.168.1.100",
                "--type", "bambu",
                "--access-code", "12345678",
                "--serial", "01P00A000000001",
                "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["type"] == "bambu"


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discover_no_printers(self, runner):
        with patch("kiln.cli.discovery.discover_printers", return_value=[]):
            result = runner.invoke(cli, ["discover", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 0

    def test_discover_found(self, runner):
        from kiln.cli.discovery import DiscoveredPrinter
        found = [DiscoveredPrinter(name="Voron", printer_type="moonraker",
                                   host="http://192.168.1.50:7125", port=7125)]
        with patch("kiln.cli.discovery.discover_printers", return_value=found):
            result = runner.invoke(cli, ["discover", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 1
        assert data["data"]["printers"][0]["name"] == "Voron"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_config_error(self, runner):
        with patch("kiln.cli.main.load_printer_config", side_effect=ValueError("No printers")):
            result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code != 0

    def test_adapter_error(self, runner, mock_adapter, config_file):
        mock_adapter.get_state.side_effect = Exception("Connection refused")
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code != 0
        assert "Connection refused" in result.output
