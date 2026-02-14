"""Tests for kiln.cli.main — CLI commands using Click's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
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
        for cmd in ["discover", "auth", "status", "files", "upload", "preflight",
                     "print", "cancel", "pause", "resume", "temp", "gcode",
                     "printers", "use", "serve"]:
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

    def test_print_auto_uploads_local_file(self, runner, mock_adapter, config_file, tmp_path):
        # Create a real local .gcode file
        gcode_file = tmp_path / "model.gcode"
        gcode_file.write_text("G28\nG1 X10\n")
        mock_adapter.upload_file.return_value = UploadResult(
            success=True, message="Uploaded model.gcode", file_name="model.gcode",
        )
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode_file), "--json"])
        assert result.exit_code == 0, result.output
        # upload_file should have been called with the local path
        mock_adapter.upload_file.assert_called_once_with(str(gcode_file))
        # start_print should use the printer filename, not the local path
        mock_adapter.start_print.assert_called_once_with("model.gcode")

    def test_print_auto_upload_failure(self, runner, mock_adapter, config_file, tmp_path):
        gcode_file = tmp_path / "bad.gcode"
        gcode_file.write_text("G28\n")
        mock_adapter.upload_file.return_value = UploadResult(
            success=False, message="Upload rejected", file_name=None,
        )
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode_file), "--json"])
        assert result.exit_code != 0
        mock_adapter.start_print.assert_not_called()

    def test_print_allows_warm_hotend_when_idle(self, runner, mock_adapter, config_file):
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
            tool_temp_actual=170.0,
            tool_temp_target=170.0,
            bed_temp_actual=22.0,
            bed_temp_target=0.0,
        )
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["print", "test.gcode", "--json"])
        assert result.exit_code == 0
        mock_adapter.start_print.assert_called_once_with("test.gcode")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_preflight_pass_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is True
        assert len(data["data"]["checks"]) >= 3

    def test_preflight_fail_not_idle(self, runner, mock_adapter, config_file):
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.PRINTING,
            connected=True,
            tool_temp_actual=210.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is False

    def test_preflight_human_output(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight"])
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "Ready to print" in result.output

    def test_preflight_with_file(self, runner, mock_adapter, config_file, tmp_path):
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--file", str(gcode), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is True

    def test_preflight_with_missing_file(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--file", "/nonexistent.gcode", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is False


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

    def test_auth_prusa_runs_diagnostics_and_persists_model(self, runner):
        diag = {
            "ok": True,
            "profile_id": "prusa_mini",
            "file_count": 16,
            "checks": [{"name": "storage_usb", "ok": True}],
        }
        with patch("kiln.cli.main.save_printer", return_value=Path("/tmp/config.yaml")) as mock_save, \
             patch("kiln.cli.main.load_printer_config", return_value={
                 "type": "prusaconnect",
                 "host": "http://192.168.0.44",
                 "api_key": "abc123",
             }), \
             patch("kiln.cli.main._run_prusa_diagnostics", return_value=diag):
            result = runner.invoke(cli, [
                "auth",
                "--name", "prusa-mini",
                "--host", "http://192.168.0.44",
                "--type", "prusaconnect",
                "--api-key", "abc123",
                "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["diagnostics"]["profile_id"] == "prusa_mini"
        assert mock_save.call_count == 2
        assert mock_save.call_args_list[1].kwargs["printer_model"] == "prusa_mini"

    def test_auth_prusa_returns_error_when_diagnostics_fail(self, runner):
        diag = {
            "ok": False,
            "checks": [{"name": "api_status", "ok": False}],
        }
        with patch("kiln.cli.main.save_printer", return_value=Path("/tmp/config.yaml")) as mock_save, \
             patch("kiln.cli.main.load_printer_config", return_value={
                 "type": "prusaconnect",
                 "host": "http://192.168.0.44",
                 "api_key": "abc123",
             }), \
             patch("kiln.cli.main._run_prusa_diagnostics", return_value=diag):
            result = runner.invoke(cli, [
                "auth",
                "--name", "prusa-mini",
                "--host", "http://192.168.0.44",
                "--type", "prusaconnect",
                "--api-key", "abc123",
                "--json",
            ])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "PRUSA_DIAGNOSTICS_FAILED"
        assert data["data"]["config_path"] == "/tmp/config.yaml"
        assert mock_save.call_count == 1


class TestPrusaProfileDetection:
    def test_map_printer_hint_does_not_assume_generic_mini(self):
        from kiln.cli.main import _map_printer_hint_to_profile_id

        assert _map_printer_hint_to_profile_id("mini") is None
        assert _map_printer_hint_to_profile_id("PrusaMINI") == "prusa_mini"

    def test_autodetect_profile_falls_back_to_api_version(self):
        from kiln.cli.main import _autodetect_printer_profile_id

        adapter = MagicMock()
        adapter._get_json.side_effect = [
            Exception("no /api/v1/info"),
            {"hostname": "PrusaMINI"},
        ]
        ctx = click.Context(cli)
        ctx.obj = {"printer": "prusa-mini"}

        with patch("kiln.cli.main.load_printer_config", return_value={
            "type": "prusaconnect",
            "host": "http://192.168.0.44",
            "api_key": "abc123",
        }), patch("kiln.cli.main._make_adapter", return_value=adapter):
            profile = _autodetect_printer_profile_id(ctx)

        assert profile == "prusa_mini"


class TestDoctorPrusa:
    def test_doctor_prusa_json_success(self, runner):
        with patch("kiln.cli.main.load_printer_config", return_value={
            "type": "prusaconnect",
            "host": "http://192.168.0.44",
            "api_key": "abc123",
        }), patch("kiln.cli.main._run_prusa_diagnostics", return_value={
            "ok": True,
            "checks": [{"name": "api_status", "ok": True, "detail": "ok"}],
            "profile_id": "prusa_mini",
            "file_count": 12,
        }):
            result = runner.invoke(cli, ["doctor-prusa", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["profile_id"] == "prusa_mini"

    def test_doctor_prusa_wrong_backend(self, runner):
        with patch("kiln.cli.main.load_printer_config", return_value={
            "type": "moonraker",
            "host": "http://test.local",
        }):
            result = runner.invoke(cli, ["doctor-prusa", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"


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


# ---------------------------------------------------------------------------
# License commands (kiln upgrade, kiln license-info)
# ---------------------------------------------------------------------------


class TestLicenseCommands:
    """Tests for kiln upgrade and kiln license-info CLI commands."""

    def test_license_info_shows_free_tier(self, runner, tmp_path):
        """kiln license-info shows FREE tier when no license is set."""
        from kiln.licensing import LicenseManager

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(cli, ["license-info"])
        assert result.exit_code == 0
        assert "Free" in result.output

    def test_license_info_json_mode(self, runner, tmp_path):
        """kiln license-info --json returns valid JSON with tier field."""
        from kiln.licensing import LicenseManager

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(cli, ["license-info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["tier"] == "free"

    def test_upgrade_shows_info_without_key(self, runner, tmp_path):
        """kiln upgrade without --key shows current tier and upgrade URL."""
        from kiln.licensing import LicenseManager

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(cli, ["upgrade"])
        assert result.exit_code == 0
        assert "Free" in result.output
        assert "kiln3d.com/pro" in result.output

    def test_upgrade_activates_pro_key(self, runner, tmp_path):
        """kiln upgrade --key activates a Pro license."""
        from kiln.licensing import LicenseManager, _KEY_PREFIX_PRO

        license_file = tmp_path / "license"
        mgr = LicenseManager(
            license_path=license_file,
            cache_path=tmp_path / "cache.json",
        )
        key = f"{_KEY_PREFIX_PRO}test_activate_abcdef"
        with patch("kiln.licensing._manager", mgr):
            result = runner.invoke(cli, ["upgrade", "--key", key])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert license_file.exists()

    def test_upgrade_activates_key_json_mode(self, runner, tmp_path):
        """kiln upgrade --key --json returns valid JSON."""
        from kiln.licensing import LicenseManager, _KEY_PREFIX_PRO

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        key = f"{_KEY_PREFIX_PRO}json_test_abcdef"
        with patch("kiln.licensing._manager", mgr):
            result = runner.invoke(cli, ["upgrade", "--key", key, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["tier"] == "pro"

    def test_upgrade_shows_active_for_pro_user(self, runner, tmp_path):
        """kiln upgrade for existing Pro user shows active status."""
        from kiln.licensing import LicenseManager, _KEY_PREFIX_PRO

        license_file = tmp_path / "license"
        key = f"{_KEY_PREFIX_PRO}existing_pro_key"
        license_file.write_text(key, encoding="utf-8")

        mgr = LicenseManager(
            license_path=license_file,
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {"KILN_LICENSE_OFFLINE": "1"}, clear=True):
            result = runner.invoke(cli, ["upgrade"])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert "Active" in result.output or "valid" in result.output.lower()


# ---------------------------------------------------------------------------
# Fleet commands (kiln fleet status, kiln fleet register)
# ---------------------------------------------------------------------------


class TestFleetCLI:
    """Tests for kiln fleet CLI commands."""

    def test_fleet_status_success(self, runner):
        """kiln fleet status shows fleet printers."""
        mock_result = {
            "success": True,
            "printers": [
                {
                    "name": "voron-350",
                    "type": "moonraker",
                    "state": "idle",
                    "tool_temp_actual": 22.0,
                    "tool_temp_target": 0.0,
                    "bed_temp_actual": 21.0,
                    "bed_temp_target": 0.0,
                    "file_name": None,
                },
            ],
            "count": 1,
            "idle_printers": ["voron-350"],
        }
        with patch("kiln.cli.main._fleet_status", return_value=mock_result, create=True), \
             patch("kiln.server.fleet_status", return_value=mock_result):
            result = runner.invoke(cli, ["fleet", "status"])
        assert result.exit_code == 0
        assert "voron-350" in result.output

    def test_fleet_status_json(self, runner):
        """kiln fleet status --json returns valid JSON."""
        mock_result = {
            "success": True,
            "printers": [],
            "count": 0,
        }
        with patch("kiln.server.fleet_status", return_value=mock_result):
            result = runner.invoke(cli, ["fleet", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["count"] == 0

    def test_fleet_status_empty(self, runner):
        """kiln fleet status with no printers shows helpful message."""
        mock_result = {
            "success": True,
            "printers": [],
            "count": 0,
            "message": "No printers registered.",
        }
        with patch("kiln.server.fleet_status", return_value=mock_result):
            result = runner.invoke(cli, ["fleet", "status"])
        assert result.exit_code == 0
        assert "No printers" in result.output or "fleet" in result.output.lower()

    def test_fleet_status_error(self, runner):
        """kiln fleet status handles server errors."""
        mock_result = {
            "success": False,
            "error": "Internal error",
            "code": "INTERNAL_ERROR",
        }
        with patch("kiln.server.fleet_status", return_value=mock_result):
            result = runner.invoke(cli, ["fleet", "status"])
        assert result.exit_code != 0
        assert "Internal error" in result.output

    def test_fleet_register_success(self, runner):
        """kiln fleet register succeeds with valid args."""
        mock_result = {
            "success": True,
            "message": "Registered printer 'test-printer' (octoprint @ http://10.0.0.5).",
            "name": "test-printer",
        }
        with patch("kiln.server.register_printer", return_value=mock_result):
            result = runner.invoke(cli, [
                "fleet", "register", "test-printer", "octoprint",
                "http://10.0.0.5", "--api-key", "TESTKEY",
            ])
        assert result.exit_code == 0
        assert "success" in result.output.lower() or "Registered" in result.output

    def test_fleet_register_missing_api_key(self, runner):
        """kiln fleet register returns error when OctoPrint needs api_key."""
        mock_result = {
            "success": False,
            "error": "api_key is required for OctoPrint printers.",
            "code": "INVALID_ARGS",
        }
        with patch("kiln.server.register_printer", return_value=mock_result):
            result = runner.invoke(cli, [
                "fleet", "register", "my-printer", "octoprint", "http://10.0.0.5",
            ])
        assert result.exit_code != 0
        assert "api_key" in result.output

    def test_fleet_register_json(self, runner):
        """kiln fleet register --json returns valid JSON."""
        mock_result = {
            "success": True,
            "message": "Registered printer 'voron' (moonraker @ http://10.0.0.6).",
            "name": "voron",
        }
        with patch("kiln.server.register_printer", return_value=mock_result):
            result = runner.invoke(cli, [
                "fleet", "register", "voron", "moonraker",
                "http://10.0.0.6", "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_fleet_requires_license(self, runner, tmp_path):
        """kiln fleet status requires Pro license."""
        from kiln.licensing import LicenseManager

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(cli, ["fleet", "status"])
        assert result.exit_code != 0
        assert "Free tier" in result.output or "upgrade" in result.output.lower()


# ---------------------------------------------------------------------------
# Queue commands (kiln queue submit, status, list, cancel)
# ---------------------------------------------------------------------------


class TestQueueCLI:
    """Tests for kiln queue CLI commands."""

    def test_queue_submit_success(self, runner):
        """kiln queue submit dispatches a job."""
        mock_result = {
            "success": True,
            "job_id": "job-abc-123",
            "message": "Job job-abc-123 submitted to queue.",
        }
        with patch("kiln.plugins.queue_tools.submit_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "submit", "benchy.gcode"])
        assert result.exit_code == 0
        assert "job-abc-123" in result.output

    def test_queue_submit_with_printer(self, runner):
        """kiln queue submit --printer targets a specific printer."""
        mock_result = {
            "success": True,
            "job_id": "job-xyz-789",
            "message": "Job submitted.",
        }
        with patch("kiln.plugins.queue_tools.submit_job", return_value=mock_result):
            result = runner.invoke(cli, [
                "queue", "submit", "cube.gcode",
                "--printer", "voron-350", "--priority", "5",
            ])
        assert result.exit_code == 0
        assert "job-xyz-789" in result.output

    def test_queue_submit_json(self, runner):
        """kiln queue submit --json returns valid JSON."""
        mock_result = {
            "success": True,
            "job_id": "job-json-test",
            "message": "Job submitted.",
        }
        with patch("kiln.plugins.queue_tools.submit_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "submit", "test.gcode", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_queue_status_success(self, runner):
        """kiln queue status shows job detail."""
        mock_result = {
            "success": True,
            "job": {
                "id": "job-abc-123",
                "file_name": "benchy.gcode",
                "status": "printing",
                "priority": 0,
                "printer_name": "voron-350",
                "submitted_by": "cli",
                "submitted_at": 1700000000,
                "started_at": 1700000060,
                "completed_at": None,
                "error": None,
            },
        }
        with patch("kiln.plugins.queue_tools.job_status", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "status", "job-abc-123"])
        assert result.exit_code == 0
        assert "benchy.gcode" in result.output

    def test_queue_status_not_found(self, runner):
        """kiln queue status returns error for unknown job."""
        mock_result = {
            "success": False,
            "error": "Job not found: 'nonexistent'",
            "code": "NOT_FOUND",
        }
        with patch("kiln.plugins.queue_tools.job_status", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "status", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_queue_status_json(self, runner):
        """kiln queue status --json returns valid JSON."""
        mock_result = {
            "success": True,
            "job": {
                "id": "job-json-stat",
                "file_name": "test.gcode",
                "status": "queued",
                "priority": 0,
            },
        }
        with patch("kiln.plugins.queue_tools.job_status", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "status", "job-json-stat", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["job"]["status"] == "queued"

    def test_queue_list_summary(self, runner):
        """kiln queue list shows queue summary by default."""
        mock_result = {
            "success": True,
            "counts": {"queued": 3, "printing": 1, "completed": 10},
            "pending": 3,
            "active": 1,
            "total": 14,
            "next_job": {"id": "job-next", "file_name": "next.gcode"},
            "recent_jobs": [],
        }
        with patch("kiln.plugins.queue_tools.queue_summary", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "list"])
        assert result.exit_code == 0
        assert "14" in result.output or "total" in result.output.lower()

    def test_queue_list_with_filter(self, runner):
        """kiln queue list --status filters to that status."""
        mock_result = {
            "success": True,
            "jobs": [
                {
                    "file_name": "failed_print.gcode",
                    "status": "failed",
                    "printer_name": "voron",
                    "submitted_at": 1700000000,
                    "started_at": 1700000060,
                    "completed_at": 1700000120,
                },
            ],
            "count": 1,
        }
        with patch("kiln.plugins.queue_tools.job_history", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "list", "--status", "failed"])
        assert result.exit_code == 0
        assert "failed_print.gcode" in result.output

    def test_queue_list_json(self, runner):
        """kiln queue list --json returns valid JSON."""
        mock_result = {
            "success": True,
            "counts": {"queued": 0},
            "pending": 0,
            "active": 0,
            "total": 0,
            "next_job": None,
            "recent_jobs": [],
        }
        with patch("kiln.plugins.queue_tools.queue_summary", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_queue_cancel_success(self, runner):
        """kiln queue cancel cancels a job."""
        mock_result = {
            "success": True,
            "job": {"id": "job-cancel-me", "status": "cancelled"},
            "message": "Job job-cancel-me cancelled.",
        }
        with patch("kiln.plugins.queue_tools.cancel_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "cancel", "job-cancel-me"])
        assert result.exit_code == 0
        assert "cancel" in result.output.lower() or "success" in result.output.lower()

    def test_queue_cancel_not_found(self, runner):
        """kiln queue cancel returns error for unknown job."""
        mock_result = {
            "success": False,
            "error": "Job not found: 'ghost'",
            "code": "NOT_FOUND",
        }
        with patch("kiln.plugins.queue_tools.cancel_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "cancel", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_queue_cancel_json(self, runner):
        """kiln queue cancel --json returns valid JSON."""
        mock_result = {
            "success": True,
            "job": {"id": "job-c", "status": "cancelled"},
            "message": "Job cancelled.",
        }
        with patch("kiln.plugins.queue_tools.cancel_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "cancel", "job-c", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_queue_submit_available_on_free_tier(self, runner, tmp_path):
        """kiln queue submit is available on Free tier (subject to queue cap)."""
        from kiln.licensing import LicenseManager

        mgr = LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(cli, ["queue", "submit", "test.gcode"])
        assert result.exit_code == 0
