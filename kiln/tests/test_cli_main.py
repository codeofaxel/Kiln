"""Tests for kiln.cli.main — CLI commands using Click's CliRunner."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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


@pytest.fixture(autouse=True)
def _reset_emergency_state(monkeypatch):
    """Keep CLI tests isolated from any local persisted E-stop state."""
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as _emergency_mod

    _emergency_mod._coordinator = None
    yield
    _emergency_mod._coordinator = None


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
# ingest / local-first / fleet route
# ---------------------------------------------------------------------------


class TestIngestWatch:
    def test_detect_only_once_json(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        file_path = watch_dir / "part.gcode"
        file_path.write_text("G28\nM104 S200\n", encoding="utf-8")

        result = runner.invoke(
            cli,
            ["ingest", "watch", "--dir", str(watch_dir), "--once", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "success"
        assert payload["data"]["mode"] == "detect_only"
        assert str(file_path) in payload["data"]["detected"]

    def test_detect_once_with_state_file_persists_progress(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        file_path = watch_dir / "part.gcode"
        file_path.write_text("G28\nM104 S200\n", encoding="utf-8")
        state_path = tmp_path / "watch_state.json"

        first = runner.invoke(
            cli,
            [
                "ingest",
                "watch",
                "--dir",
                str(watch_dir),
                "--once",
                "--state-file",
                str(state_path),
                "--min-stable-seconds",
                "0",
                "--json",
            ],
        )
        assert first.exit_code == 0, first.output
        first_payload = json.loads(first.output)
        assert str(file_path) in first_payload["data"]["detected"]
        assert state_path.exists()

        second = runner.invoke(
            cli,
            [
                "ingest",
                "watch",
                "--dir",
                str(watch_dir),
                "--once",
                "--state-file",
                str(state_path),
                "--min-stable-seconds",
                "0",
                "--json",
            ],
        )
        assert second.exit_code == 0, second.output
        second_payload = json.loads(second.output)
        assert second_payload["data"]["detected"] == []

    def test_detect_once_respects_stability_window(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        file_path = watch_dir / "fresh.gcode"
        file_path.write_text("G28\nM104 S200\n", encoding="utf-8")

        deferred = runner.invoke(
            cli,
            [
                "ingest",
                "watch",
                "--dir",
                str(watch_dir),
                "--once",
                "--min-stable-seconds",
                "10",
                "--json",
            ],
        )
        assert deferred.exit_code == 0, deferred.output
        deferred_payload = json.loads(deferred.output)
        assert deferred_payload["data"]["detected"] == []

        old_ts = time.time() - 20
        os.utime(file_path, (old_ts, old_ts))

        ready = runner.invoke(
            cli,
            [
                "ingest",
                "watch",
                "--dir",
                str(watch_dir),
                "--once",
                "--min-stable-seconds",
                "10",
                "--json",
            ],
        )
        assert ready.exit_code == 0, ready.output
        ready_payload = json.loads(ready.output)
        assert str(file_path) in ready_payload["data"]["detected"]

    def test_auto_queue_once_dispatches_when_idle(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        file_path = watch_dir / "widget.gcode"
        file_path.write_text("G28\nM109 S205\n", encoding="utf-8")

        adapter = MagicMock()
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE,
            connected=True,
        )
        adapter.upload_file.return_value = UploadResult(
            success=True,
            message="Uploaded widget.gcode",
            file_name="widget.gcode",
        )
        adapter.start_print.return_value = PrintResult(
            success=True,
            message="Print started",
        )

        with (
            patch("kiln.cli.main._load_fleet_adapters", return_value=({"lab-printer": adapter}, [])),
            patch("kiln.cli.main._collect_routing_candidates", return_value=[{"printer_id": "lab-printer"}]),
            patch(
                "kiln.cli.main._route_printer_for_job",
                return_value=("lab-printer", {"recommended_printer": {"score": 92.0}}, None),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "ingest",
                    "watch",
                    "--dir",
                    str(watch_dir),
                    "--once",
                    "--auto-queue",
                    "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "success"
        assert payload["data"]["mode"] == "auto_queue"
        assert payload["data"]["queued"][0]["printer"] == "lab-printer"
        assert payload["data"]["dispatched"][0]["printer"] == "lab-printer"
        adapter.upload_file.assert_called_once_with(str(file_path))
        adapter.start_print.assert_called_once_with("widget.gcode")


class TestIngestService:
    def test_service_install_and_status(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        config_path = tmp_path / "service.json"

        install = runner.invoke(
            cli,
            [
                "ingest",
                "service",
                "install",
                "--dir",
                str(watch_dir),
                "--config-path",
                str(config_path),
                "--json",
            ],
        )
        assert install.exit_code == 0, install.output
        install_payload = json.loads(install.output)
        assert install_payload["status"] == "success"
        assert config_path.exists()

        status = runner.invoke(
            cli,
            ["ingest", "service", "status", "--config-path", str(config_path), "--json"],
        )
        assert status.exit_code == 0, status.output
        status_payload = json.loads(status.output)
        assert status_payload["status"] == "success"
        assert status_payload["data"]["installed"] is True
        assert status_payload["data"]["running"] is False

    def test_service_start_writes_pid(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        config_path = tmp_path / "service.json"

        install = runner.invoke(
            cli,
            [
                "ingest",
                "service",
                "install",
                "--dir",
                str(watch_dir),
                "--config-path",
                str(config_path),
                "--json",
            ],
        )
        assert install.exit_code == 0, install.output

        proc = MagicMock()
        proc.pid = 43210
        proc.poll.return_value = None
        with patch("kiln.cli.main.subprocess.Popen", return_value=proc):
            start = runner.invoke(
                cli,
                ["ingest", "service", "start", "--config-path", str(config_path), "--json"],
            )
        assert start.exit_code == 0, start.output
        start_payload = json.loads(start.output)
        assert start_payload["data"]["running"] is True
        assert start_payload["data"]["pid"] == 43210

        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        pid_path = Path(cfg["pid_file"])
        assert pid_path.exists()
        assert pid_path.read_text(encoding="utf-8").strip() == "43210"

    def test_service_stop_not_running(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        config_path = tmp_path / "service.json"
        install = runner.invoke(
            cli,
            [
                "ingest",
                "service",
                "install",
                "--dir",
                str(watch_dir),
                "--config-path",
                str(config_path),
                "--json",
            ],
        )
        assert install.exit_code == 0, install.output

        stop = runner.invoke(
            cli,
            ["ingest", "service", "stop", "--config-path", str(config_path), "--json"],
        )
        assert stop.exit_code == 0, stop.output
        stop_payload = json.loads(stop.output)
        assert stop_payload["data"]["running"] is False
        assert stop_payload["data"]["reason"] == "not_running"

    def test_service_stop_running_process(self, runner, tmp_path):
        watch_dir = tmp_path / "incoming"
        watch_dir.mkdir()
        config_path = tmp_path / "service.json"
        install = runner.invoke(
            cli,
            [
                "ingest",
                "service",
                "install",
                "--dir",
                str(watch_dir),
                "--config-path",
                str(config_path),
                "--json",
            ],
        )
        assert install.exit_code == 0, install.output

        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        pid_path = Path(cfg["pid_file"])
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("5001\n", encoding="utf-8")

        checks = {"count": 0}

        def _fake_is_running(_: int) -> bool:
            checks["count"] += 1
            return checks["count"] == 1

        with (
            patch("kiln.cli.main.os.kill"),
            patch("kiln.cli.main._is_pid_running", side_effect=_fake_is_running),
        ):
            stop = runner.invoke(
                cli,
                ["ingest", "service", "stop", "--config-path", str(config_path), "--json"],
            )
        assert stop.exit_code == 0, stop.output
        stop_payload = json.loads(stop.output)
        assert stop_payload["data"]["stopped"] is True
        assert stop_payload["data"]["forced"] is False


class TestFleetRoute:
    @pytest.mark.skipif(
        "fleet" not in getattr(__import__("kiln.cli.main", fromlist=["cli"]).cli, "commands", {}),
        reason="fleet commands require kiln-pro",
    )
    def test_fleet_route_json(self, runner):
        adapter = MagicMock()
        with (
            patch("kiln.cli.main._load_fleet_adapters", return_value=({"p1": adapter}, [])),
            patch("kiln.cli.main._collect_routing_candidates", return_value=[{"printer_id": "p1"}]),
            patch(
                "kiln.cli.main._route_printer_for_job",
                return_value=("p1", {"recommended_printer": {"score": 88.5}}, None),
            ),
        ):
            result = runner.invoke(cli, ["fleet", "route", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "success"
        assert payload["data"]["recommended_printer"] == "p1"


class TestLocalFirst:
    def test_local_first_apply_updates_cloud_sync_setting(self, runner):
        fake_db = MagicMock()
        fake_db.get_setting.return_value = '{"provider":"x"}'
        with patch("kiln.persistence.get_db", return_value=fake_db):
            result = runner.invoke(cli, ["local-first", "--apply", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "success"
        assert fake_db.set_setting.call_args_list == [
            call("cloud_sync_config_backup", '{"provider":"x"}'),
            call("cloud_sync_config", ""),
        ]


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
        # start_print should use the printer filename, not the local path.
        # local_file_path is passed for 3MF auto-detection.
        mock_adapter.start_print.assert_called_once()
        call_args = mock_adapter.start_print.call_args
        assert call_args[0][0] == "model.gcode"

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

    def test_resume_blocked_when_latched(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch("kiln.cli.main._emergency_latch_status", return_value={"latched": True}):
            result = runner.invoke(cli, ["resume", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "E_STOP_LATCHED"


class TestEmergencyCommands:
    def test_emergency_status_json(self, runner):
        fake_coord = MagicMock()
        fake_coord.get_latch_status.return_value = {"printer_id": "default", "latched": False}
        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            result = runner.invoke(cli, ["emergency-status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["printer"] == "default"

    def test_emergency_stop_json(self, runner):
        fake_record = MagicMock()
        fake_record.to_dict.return_value = {"printer_id": "default", "success": True}
        fake_coord = MagicMock()
        fake_coord.emergency_stop.return_value = fake_record
        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            result = runner.invoke(cli, ["emergency-stop", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["emergency_stop"]["success"] is True

    def test_emergency_clear_json(self, runner):
        fake_coord = MagicMock()
        fake_coord.clear_stop_with_ack.return_value = {
            "success": True,
            "status": {"printer_id": "default", "latched": False},
        }
        with patch("kiln.emergency.get_emergency_coordinator", return_value=fake_coord):
            result = runner.invoke(cli, ["emergency-clear", "--ack-note", "operator checked", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["cleared"] is True


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

    def test_ceiling_comes_from_the_printer_not_a_hardcoded_300(
        self, runner, mock_adapter, config_file, monkeypatch
    ):
        """A PTFE-lined Ender 3 must refuse 300°C, which the old CLI allowed.

        The limit used to be a literal `300` typed into this command while
        set_temperature resolved the machine's real ceiling over MCP.  For an
        Ender 3 that ceiling is 250, clamped to 240 for a PTFE-lined hotend —
        so the CLI would drive it 60°C past safe.  Pinning the REFUSAL rather
        than the number: a future profile change should move the message, not
        reopen the hazard.
        """
        import kiln.server as _srv

        monkeypatch.setattr(_srv, "_get_temp_limits", lambda *a, **k: (240.0, 110.0))
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["temp", "--tool", "300", "--json"])
        assert result.exit_code != 0
        assert "240" in result.output
        mock_adapter.set_tool_temp.assert_not_called()

    def test_ceiling_allows_what_a_high_temp_machine_is_rated_for(
        self, runner, mock_adapter, config_file, monkeypatch
    ):
        """The same copy refused 350°C on machines rated to 500.

        The false-refusal direction is cheaper than the false-permit, but it
        was still wrong, and it is the half a factory-class printer meets.
        """
        import kiln.server as _srv

        monkeypatch.setattr(_srv, "_get_temp_limits", lambda *a, **k: (500.0, 300.0))
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["temp", "--tool", "350", "--json"])
        assert result.exit_code == 0
        mock_adapter.set_tool_temp.assert_called_once_with(350.0)


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
                 "type": "prusalink",
                 "host": "http://192.168.0.44",
                 "api_key": "abc123",
             }), \
             patch("kiln.cli.main._run_prusa_diagnostics", return_value=diag):
            result = runner.invoke(cli, [
                "auth",
                "--name", "prusa-mini",
                "--host", "http://192.168.0.44",
                "--type", "prusalink",
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
        # ``save_printer`` returns a ``Path``; the CLI stringifies it
        # into the JSON payload.  Compare against the same Path
        # stringified so the expectation matches on Windows too (where
        # the separator is a backslash).
        config_path = Path("/tmp/config.yaml")
        with patch("kiln.cli.main.save_printer", return_value=config_path) as mock_save, \
             patch("kiln.cli.main.load_printer_config", return_value={
                 "type": "prusalink",
                 "host": "http://192.168.0.44",
                 "api_key": "abc123",
             }), \
             patch("kiln.cli.main._run_prusa_diagnostics", return_value=diag):
            result = runner.invoke(cli, [
                "auth",
                "--name", "prusa-mini",
                "--host", "http://192.168.0.44",
                "--type", "prusalink",
                "--api-key", "abc123",
                "--json",
            ])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "PRUSA_DIAGNOSTICS_FAILED"
        assert data["data"]["config_path"] == str(config_path)
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
            "type": "prusalink",
            "host": "http://192.168.0.44",
            "api_key": "abc123",
        }), patch("kiln.cli.main._make_adapter", return_value=adapter):
            profile = _autodetect_printer_profile_id(ctx)

        assert profile == "prusa_mini"

    def test_map_creality_k1_max_hint(self):
        from kiln.cli.main import _map_printer_hint_to_profile_id

        assert _map_printer_hint_to_profile_id("Creality K1 Max 2025") == "k1_max"
        assert _map_printer_hint_to_profile_id("K2 Plus") == "k2_plus"
        assert _map_printer_hint_to_profile_id("Ender-3 V3 KE") == "ender3_v3_ke"


class TestDoctorPrusa:
    def test_doctor_prusa_json_success(self, runner):
        with patch("kiln.cli.main.load_printer_config", return_value={
            "type": "prusalink",
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


class TestDoctorCreality:
    def test_doctor_creality_json_success_with_host(self, runner):
        with patch("kiln.cli.main._run_creality_diagnostics", return_value={
            "ok": True,
            "checks": [{"name": "moonraker_probe", "ok": True, "detail": "ok"}],
            "resolved_url": "http://192.168.1.55:7125",
            "browser_test_url": "http://192.168.1.55:7125/server/info",
            "cfs_status": {
                "detected": True,
                "hardware_unverified": True,
                "slot_count": 4,
            },
        }):
            result = runner.invoke(
                cli,
                ["doctor-creality", "--host", "192.168.1.55", "--model", "k1_max", "--json"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["resolved_url"] == "http://192.168.1.55:7125"

    def test_doctor_creality_wrong_backend(self, runner):
        with patch("kiln.cli.main.load_printer_config", return_value={
            "type": "moonraker",
            "host": "http://test.local",
        }):
            result = runner.invoke(cli, ["doctor-creality", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_doctor_creality_human_failure_prints_reachability_guidance(self, runner):
        with patch("kiln.cli.main._run_creality_diagnostics", return_value={
            "ok": False,
            "checks": [{"name": "moonraker_probe", "ok": False, "detail": "HTTP 404"}],
            "likely_cause": "firmware_locked_or_wrong_port",
            "user_message": "Something answered, but /server/info was not Moonraker.",
            "firmware_lockdown_possible": True,
            "connection_checklist": [
                "Keep the printer and this computer on the same Wi-Fi/LAN.",
                "Confirm the printer IP address.",
                "Check http://<printer-ip>:7125/server/info.",
            ],
            "next_steps": ["Check Creality firmware settings for local Moonraker access."],
        }):
            result = runner.invoke(cli, ["doctor-creality", "--host", "192.168.1.55"])

        assert result.exit_code != 0
        assert "Likely cause: firmware_locked_or_wrong_port" in result.output
        assert "same Wi-Fi/LAN" in result.output
        assert "printer IP address" in result.output
        assert "local Moonraker disabled" in result.output


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


@pytest.mark.skipif(
    not importlib.util.find_spec("kiln.licensing"),
    reason="kiln.licensing extracted to kiln-pro",
)
class TestLicenseCommands:
    """Tests for kiln upgrade and kiln license-info CLI commands."""

    @pytest.fixture(autouse=True)
    def _isolate_oauth_session(self, monkeypatch, tmp_path_factory):
        """Redirect ``$KILN_AUTH_HOME`` at an empty tmp dir so the user's real
        ``~/.kiln/auth_tokens.json`` doesn't bleed an OAuth-resolved tier into
        tests that assert FREE / a specific legacy-key tier.

        OAuth resolution runs *before* the legacy-key path in
        ``LicenseManager.get_tier()``, so a developer running this suite on a
        signed-in machine would otherwise see ``Tier: Enterprise`` /
        ``Source: oauth`` instead of the expected ``Free`` / ``Pro``.
        """
        oauth_home = tmp_path_factory.mktemp("oauth_isolate")
        monkeypatch.setenv("KILN_AUTH_HOME", str(oauth_home))

    def test_license_info_shows_free_tier(self, runner, tmp_path):
        """kiln license-info shows FREE tier when no license is set."""
        from kiln.licensing import LicenseManager

        _license_env = {
            "KILN_LICENSE_KEY": "",
            "KILN_LICENSE_PUBLIC_KEY": "",
            "KILN_LICENSE_SIGNING_SECRET": "",
        }
        with patch.dict("os.environ", _license_env):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            # Force tier resolution so get_info() has _resolved set
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr):
                result = runner.invoke(cli, ["license-info"])
        assert result.exit_code == 0
        assert "Free" in result.output

    def test_license_info_json_mode(self, runner, tmp_path):
        """kiln license-info --json returns valid JSON with tier field."""
        from kiln.licensing import LicenseManager

        _license_env = {
            "KILN_LICENSE_KEY": "",
            "KILN_LICENSE_PUBLIC_KEY": "",
            "KILN_LICENSE_SIGNING_SECRET": "",
        }
        with patch.dict("os.environ", _license_env):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr):
                result = runner.invoke(cli, ["license-info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["tier"] == "free"

    def test_upgrade_shows_info_without_key(self, runner, tmp_path):
        """kiln upgrade without --key shows current tier and upgrade URL."""
        from kiln.licensing import LicenseManager

        _license_env = {
            "KILN_LICENSE_KEY": "",
            "KILN_LICENSE_PUBLIC_KEY": "",
            "KILN_LICENSE_SIGNING_SECRET": "",
        }
        with patch.dict("os.environ", _license_env):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr):
                result = runner.invoke(cli, ["upgrade"])
        assert result.exit_code == 0
        assert "Free" in result.output
        assert "kiln3d.com/pricing" in result.output

    def test_upgrade_activates_pro_key(self, runner, tmp_path):
        """kiln upgrade --key activates a Pro license."""
        from kiln.licensing import LicenseManager, LicenseTier, generate_license_key

        _secret = "test-cli-signing-secret"
        license_file = tmp_path / "license"
        key = generate_license_key(LicenseTier.PRO, "test@example.com", signing_key=_secret)
        _env = {"KILN_LICENSE_SIGNING_SECRET": _secret, "KILN_LICENSE_KEY": ""}
        with patch.dict("os.environ", _env, clear=False):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr):
                result = runner.invoke(cli, ["upgrade", "--key", key])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert license_file.exists()

    def test_upgrade_activates_key_json_mode(self, runner, tmp_path):
        """kiln upgrade --key --json returns valid JSON."""
        from kiln.licensing import LicenseManager, LicenseTier, generate_license_key

        _secret = "test-cli-signing-secret"
        key = generate_license_key(LicenseTier.PRO, "test@example.com", signing_key=_secret)
        _env = {"KILN_LICENSE_SIGNING_SECRET": _secret, "KILN_LICENSE_KEY": ""}
        with patch.dict("os.environ", _env, clear=False):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr):
                result = runner.invoke(cli, ["upgrade", "--key", key, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["tier"] == "pro"

    def test_upgrade_shows_active_for_pro_user(self, runner, tmp_path):
        """kiln upgrade for existing Pro user shows active status."""
        from kiln.licensing import LicenseManager, LicenseTier, generate_license_key

        _secret = "test-cli-signing-secret"
        license_file = tmp_path / "license"
        key = generate_license_key(LicenseTier.PRO, "test@example.com", signing_key=_secret)
        license_file.write_text(key, encoding="utf-8")

        _env = {
            "KILN_LICENSE_SIGNING_SECRET": _secret,
            "KILN_LICENSE_KEY": "",  # prevent a real env license key from leaking in
        }
        with patch.dict("os.environ", _env, clear=False):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing.get_license_manager", return_value=mgr), \
                 patch("kiln.licensing._manager", mgr):
                result = runner.invoke(cli, ["upgrade"])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert "Active" in result.output or "valid" in result.output.lower()


# ---------------------------------------------------------------------------
# Fleet commands (kiln fleet status, kiln fleet register)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not importlib.util.find_spec("kiln.licensing"),
    reason="kiln.licensing extracted to kiln-pro",
)
class TestFleetCLI:
    """Tests for kiln fleet CLI commands."""

    def _license_pass(self):
        """Context manager that bypasses the PRO tier check for fleet commands."""
        return patch("kiln.licensing.check_tier", return_value=(True, None))

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
        with self._license_pass(), \
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
        with self._license_pass(), \
             patch("kiln.server.fleet_status", return_value=mock_result):
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
        with self._license_pass(), \
             patch("kiln.server.fleet_status", return_value=mock_result):
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
        with self._license_pass(), \
             patch("kiln.server.fleet_status", return_value=mock_result):
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
        with patch("kiln.licensing.check_tier", return_value=(False, (
            "This feature requires Kiln Pro. "
            "You're on the Free tier. "
            "Already subscribed? Run `kiln login` to sync this machine. "
            "Otherwise: https://kiln3d.com/pricing"
        ))):
            result = runner.invoke(cli, ["fleet", "status"])
        assert result.exit_code != 0
        assert "LICENSE_REQUIRED" in result.output
        assert "kiln3d.com/pricing" in result.output


# ---------------------------------------------------------------------------
# Queue commands (kiln queue submit, status, list, cancel)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not importlib.util.find_spec("kiln.licensing"),
    reason="kiln.licensing extracted to kiln-pro",
)
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

    def test_queue_submit_passes_idempotency_key(self, runner):
        """kiln queue submit --idempotency-key reaches the tool door.

        The retry-safety guard lives at PrintQueue.submit; every tool
        door carries the key, and the CLI is a door too — a script
        wrapping the CLI must be able to retry a lost reply without
        queueing a duplicate print.
        """
        mock_result = {
            "success": True,
            "job_id": "job-idem-1",
            "message": "Job submitted.",
        }
        with patch(
            "kiln.plugins.queue_tools.submit_job", return_value=mock_result
        ) as mock_submit:
            result = runner.invoke(cli, [
                "queue", "submit", "cube.gcode",
                "--idempotency-key", "retry-key-7",
            ])
        assert result.exit_code == 0
        assert mock_submit.call_args.kwargs["idempotency_key"] == "retry-key-7"

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
        with patch("kiln.plugins.queue_tools.cancel_queued_job", return_value=mock_result):
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
        with patch("kiln.plugins.queue_tools.cancel_queued_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "cancel", "ghost"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_queue_clear_all(self, runner):
        """kiln queue clear cancels every queued job and reports the count."""
        mock_result = {
            "success": True,
            "dry_run": False,
            "count": 3,
            "cancelled": ["job-a", "job-b", "job-c"],
            "skipped": [],
            "message": "Cancelled 3 queued job(s).",
        }
        with patch(
            "kiln.plugins.queue_tools.cancel_queued_jobs", return_value=mock_result
        ) as mock_clear:
            result = runner.invoke(cli, ["queue", "clear"])
        assert result.exit_code == 0
        # No --printer => sweep every queued job; not a dry run.
        mock_clear.assert_called_once_with(printer_name=None, dry_run=False)
        assert "3 queued job(s) cancelled" in result.output

    def test_queue_clear_dry_run_changes_nothing(self, runner):
        """kiln queue clear --dry-run previews and cancels nothing."""
        mock_result = {
            "success": True,
            "dry_run": True,
            "count": 2,
            "cancelled": ["job-a", "job-b"],
            "skipped": [],
            "message": "2 queued job(s) would be cancelled — dry run, nothing changed.",
        }
        with patch(
            "kiln.plugins.queue_tools.cancel_queued_jobs", return_value=mock_result
        ) as mock_clear:
            result = runner.invoke(cli, ["queue", "clear", "--dry-run"])
        assert result.exit_code == 0
        mock_clear.assert_called_once_with(printer_name=None, dry_run=True)
        assert "would be cancelled" in result.output

    def test_queue_clear_scopes_to_printer(self, runner):
        """kiln queue clear --printer scopes the sweep to one printer."""
        mock_result = {
            "success": True,
            "dry_run": False,
            "count": 1,
            "cancelled": ["job-on-voron"],
            "skipped": [],
            "message": "Cancelled 1 queued job(s) on voron-350.",
        }
        with patch(
            "kiln.plugins.queue_tools.cancel_queued_jobs", return_value=mock_result
        ) as mock_clear:
            result = runner.invoke(cli, ["queue", "clear", "--printer", "voron-350"])
        assert result.exit_code == 0
        mock_clear.assert_called_once_with(printer_name="voron-350", dry_run=False)
        assert "voron-350" in result.output

    def test_queue_clear_json(self, runner):
        """kiln queue clear --json emits the raw result under the data envelope."""
        mock_result = {
            "success": True,
            "dry_run": False,
            "count": 2,
            "cancelled": ["job-a", "job-b"],
            "skipped": [{"job_id": "job-c", "reason": "no longer queued (status: PRINTING)"}],
            "message": "Cancelled 2 queued job(s). 1 skipped (already started or no longer queued).",
        }
        with patch("kiln.plugins.queue_tools.cancel_queued_jobs", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "clear", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["count"] == 2
        assert data["data"]["cancelled"] == ["job-a", "job-b"]
        assert data["data"]["skipped"][0]["job_id"] == "job-c"

    def test_queue_cancel_json(self, runner):
        """kiln queue cancel --json returns valid JSON."""
        mock_result = {
            "success": True,
            "job": {"id": "job-c", "status": "cancelled"},
            "message": "Job cancelled.",
        }
        with patch("kiln.plugins.queue_tools.cancel_queued_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "cancel", "job-c", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_queue_submit_available_on_free_tier(self, runner, tmp_path):
        """kiln queue submit is available on Free tier (subject to queue cap)."""
        from kiln.licensing import LicenseManager

        mock_result = {"success": True, "job_id": "test-job-123", "position": 1}
        _license_env = {
            "KILN_LICENSE_KEY": "",
            "KILN_LICENSE_PUBLIC_KEY": "",
            "KILN_LICENSE_SIGNING_SECRET": "",
        }
        with patch.dict("os.environ", _license_env):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            mgr.get_tier()
            with patch("kiln.licensing._manager", mgr), \
                 patch("kiln.licensing.get_license_manager", return_value=mgr), \
                 patch("kiln.plugins.queue_tools.submit_job", return_value=mock_result):
                result = runner.invoke(cli, ["queue", "submit", "test.gcode"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# monitor — CLI redirect onto PrintHealthMonitor
# ---------------------------------------------------------------------------


class TestMonitor:
    """The kiln monitor CLI must drive the canonical PrintHealthMonitor.

    Pre-fold (commit 7db5ab6 era), the CLI ran a separate
    PrintSafetyMonitor with its own loop, so the operator's experience
    diverged from the MCP tool's predictive+detective stack.  These
    tests pin that the click flags map onto MonitorPolicy / start_monitoring
    correctly so the CLI now runs through the same engine.
    """

    def _stub_monitor_class(self, mock_adapter):
        """Build a MagicMock class whose .start_monitoring captures kwargs.

        Returns the (MockClass, captured_calls) pair.  The mock's
        get_session yields a COMPLETED session immediately so the CLI's
        post-iter teardown returns clean.
        """
        from kiln.print_health_monitor import (
            HealthSeverity,
            MonitorSession,
            MonitorStatus,
            PrinterHealthReport,
        )

        captured: dict = {}

        # A single-report iterator so the CLI's Rich loop has something
        # to consume before exiting.  print_progress is reported at
        # 99.5% so the CLI's "end-of-print" branch terminates the loop.
        def _make_one_report():
            return PrinterHealthReport(
                printer_name="test-printer",
                metrics=[],
                overall_status=HealthSeverity.OK,
                checked_at=time.time(),
            )

        instance = MagicMock()

        def _start(printer, *, interval_seconds=30, policy=None,
                   output_stream=None, enable_report_queue=False, **kw):
            captured["printer"] = printer
            captured["interval_seconds"] = interval_seconds
            captured["policy"] = policy
            captured["output_stream"] = output_stream
            captured["enable_report_queue"] = enable_report_queue
            captured["t_start"] = time.time()
            return "session-id-stub"

        instance.start_monitoring.side_effect = _start

        def _iter_reports(session_id, *, timeout=None):
            # Yield one report whose progress >= 99% so the CLI breaks
            # out of the loop without touching real adapters.
            report = _make_one_report()
            from kiln.print_health_monitor import HealthMetric
            report.metrics.append(
                HealthMetric(
                    metric_name="print_progress",
                    current_value=99.5,
                    expected_value=100.0,
                    deviation=0.5,
                    is_warning=False,
                    timestamp=time.time(),
                    severity=HealthSeverity.OK,
                    unit="%",
                )
            )
            yield report

        instance.iter_reports.side_effect = _iter_reports

        # Final session has COMPLETED status and no auto-cancel issues
        # so the CLI's post-loop check picks exit code 0.
        completed_session = MonitorSession(
            session_id="session-id-stub",
            printer_name="test-printer",
            job_id="job-x",
            policy=None,  # type: ignore[arg-type]
            status=MonitorStatus.COMPLETED,
            ended_at=time.time(),
        )
        instance.get_session.return_value = completed_session

        # JSON-mode path polls get_session; same response.

        return instance, captured

    def test_monitor_command_invokes_print_health_monitor(
        self, runner, mock_adapter, config_file
    ):
        """kiln monitor must construct and invoke PrintHealthMonitor."""
        mock_instance, captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            # Use --json so the CLI takes the polling path which
            # terminates as soon as get_session returns COMPLETED.
            # --timeout 1 caps the safety-net wait in case anything
            # blocks.
            result = runner.invoke(
                cli, ["--printer", "test-printer", "monitor", "--json", "--timeout", "1"],
            )

        assert mock_instance.start_monitoring.called, result.output
        # The CLI resolves the printer name from --printer (set above).
        assert captured.get("printer") == "test-printer"
        # Exit cleanly when the session ends in COMPLETED status.
        assert result.exit_code == 0, result.output

    def test_monitor_command_says_what_is_watching_before_it_starts(
        self, runner, mock_adapter, config_file
    ):
        """The header carries the coverage line when kiln-pro can compose
        one, and nothing extra when it cannot."""
        mock_instance, _captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        line = "What is watching this print — watched: spaghetti. Kiln is watching: a heater fault."
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ), patch("kiln.server._coverage_line_for", return_value=line):
            result = runner.invoke(
                cli, ["--printer", "test-printer", "monitor", "--timeout", "1"],
            )
        assert line in result.output, result.output
        assert result.output.index("Monitoring printer") < result.output.index("What is watching")

        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ), patch("kiln.server._coverage_line_for", return_value=None):
            result = runner.invoke(
                cli, ["--printer", "test-printer", "monitor", "--timeout", "1"],
            )
        assert "What is watching" not in result.output, result.output

    def test_monitor_command_passes_interval_flag(
        self, runner, mock_adapter, config_file
    ):
        """--interval flag must reach start_monitoring's interval_seconds."""
        mock_instance, captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            result = runner.invoke(
                cli, ["monitor", "--interval", "5", "--json", "--timeout", "1"],
            )

        assert mock_instance.start_monitoring.called, result.output
        assert captured.get("interval_seconds") == 5.0
        # Policy should also have check_interval_seconds=5 so the
        # underlying loop respects the same cadence.
        policy = captured.get("policy")
        assert policy is not None
        assert policy.check_interval_seconds == 5

    def test_monitor_command_auto_cancel_flag(
        self, runner, mock_adapter, config_file
    ):
        """--auto-cancel flag must set policy.auto_cancel_on_emergency=True."""
        mock_instance, captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            result = runner.invoke(
                cli, ["monitor", "--auto-cancel", "--json", "--timeout", "1"],
            )

        assert mock_instance.start_monitoring.called, result.output
        policy = captured.get("policy")
        assert policy is not None
        assert policy.auto_cancel_on_emergency is True

    def test_monitor_command_timeout_flag_caps_session(
        self, runner, mock_adapter, config_file
    ):
        """--timeout flag must cap the wall-clock session duration."""
        mock_instance, captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            t0 = time.time()
            result = runner.invoke(
                cli, ["monitor", "--timeout", "30", "--json"],
            )
            elapsed = time.time() - t0

        assert mock_instance.start_monitoring.called, result.output
        # Session must end at or before 30 seconds (well below — the
        # stub completes immediately, but the policy must carry the
        # cap so the underlying loop honours it in real use).
        policy = captured.get("policy")
        assert policy is not None
        assert policy.session_timeout_seconds == 30.0
        # The CLI should not have blocked anywhere near 30s in this
        # stubbed path; sanity-check it returned promptly.
        assert elapsed < 30.0

    # ------------------------------------------------------------------
    # Tier-1 smart-monitoring fields — JSON + Rich symmetry with MCP
    # ------------------------------------------------------------------

    def test_monitor_drops_legacy_snapshot_flags(
        self, runner, mock_adapter, config_file
    ):
        """--snapshot-interval and --snapshot-dir were soft no-ops; gone."""
        mock_instance, _captured = self._stub_monitor_class(mock_adapter)
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            result = runner.invoke(
                cli,
                [
                    "monitor",
                    "--snapshot-interval", "5",
                    "--snapshot-dir", "/tmp/x",
                    "--json",
                ],
            )

        # Click reports the unknown option with exit code 2 and a
        # "No such option" message.  Both arms should mention the
        # flag name we deleted.
        assert result.exit_code == 2, result.output
        assert "snapshot" in result.output.lower(), result.output

    def test_monitor_json_includes_signals_block(
        self, runner, mock_adapter, config_file, tmp_path
    ):
        """JSON-Lines envelope must carry the Tier-1 signals block."""
        from kiln.print_health_monitor import (
            HealthSeverity,
            PrinterHealthReport,
            PrintHealthMonitor,
        )

        # Build an in-process monitor (so _build_jsonl_envelope runs)
        # and hand the CLI a stub PrintHealthMonitor whose
        # start_monitoring writes one envelope to the supplied stream
        # using the real builder.  This pins the contract end-to-end:
        # CLI -> monitor -> envelope -> stream.
        captured: dict = {}
        real_monitor = PrintHealthMonitor()

        def _start(printer, *, interval_seconds=30, policy=None,
                   output_stream=None, enable_report_queue=False, **kw):
            captured["output_stream"] = output_stream
            # Synthesize one envelope so the JSON line shows up in
            # stdout the way it would in a real session.
            sample_signals = {
                "monitoring_active": True,
                "session_id": "abc12345-fake",
                "session_started_at": time.time(),
                "issue_count": 3,
                "report_count": 12,
                "risk": {
                    "score": 0.55,
                    "severity": "amber",
                    "kinds": ["thermal_drift"],
                },
                "predictive": None,
                "detective": None,
                "auto_pause": None,
                "as_of": time.time(),
            }
            with patch.object(
                real_monitor,
                "get_latest_signals",
                return_value=sample_signals,
            ):
                report = PrinterHealthReport(
                    printer_name=printer,
                    metrics=[],
                    overall_status=HealthSeverity.OK,
                    checked_at=time.time(),
                )
                report.session_id = "abc12345-fake"
                envelope = real_monitor._build_jsonl_envelope(printer, report)
                if output_stream is not None:
                    import json as _json
                    output_stream.write(_json.dumps(envelope, default=str) + "\n")
                    output_stream.flush()
            return "abc12345-fake"

        instance = MagicMock()
        instance.start_monitoring.side_effect = _start
        from kiln.print_health_monitor import MonitorSession, MonitorStatus
        instance.get_session.return_value = MonitorSession(
            session_id="abc12345-fake",
            printer_name="test-printer",
            job_id="job-x",
            policy=None,  # type: ignore[arg-type]
            status=MonitorStatus.COMPLETED,
            ended_at=time.time(),
        )

        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=instance,
        ):
            result = runner.invoke(
                cli,
                [
                    "--printer", "test-printer",
                    "monitor", "--json",
                    "--interval", "0.1",
                    "--timeout", "1",
                ],
            )

        assert result.exit_code == 0, result.output
        # First non-blank line should parse as JSON and carry signals.
        first_line = next(
            (ln for ln in result.output.splitlines() if ln.strip().startswith("{")),
            None,
        )
        assert first_line is not None, result.output
        envelope = json.loads(first_line)
        assert "signals" in envelope, envelope
        assert envelope["signals"].get("monitoring_active") is True
        assert envelope["signals"]["risk"]["severity"] == "amber"
        # Stable schema: auto_recover and reroute keys exist (None on
        # free tier).
        assert "auto_recover" in envelope
        assert "reroute" in envelope

    def test_monitor_json_omits_auto_recover_when_kiln_pro_unavailable(self):
        """Free tier (no kiln_pro) -> auto_recover/reroute fields are None."""
        import sys

        from kiln.print_health_monitor import (
            HealthSeverity,
            PrinterHealthReport,
            PrintHealthMonitor,
        )

        # Force the import to fail by stubbing the kiln_pro module out
        # of sys.modules — the envelope builder should swallow ImportError
        # and leave both fields null.
        saved = {
            k: v for k, v in sys.modules.items()
            if k.startswith("kiln_pro")
        }
        for k in list(saved):
            sys.modules.pop(k, None)
        try:
            with patch.dict(
                sys.modules,
                {"kiln_pro": None, "kiln_pro.recovery": None,
                 "kiln_pro.recovery.auto_recover_engine": None},
            ):
                monitor = PrintHealthMonitor()
                with patch.object(
                    monitor,
                    "get_latest_signals",
                    return_value={
                        "monitoring_active": True,
                        "session_id": "s-1",
                        "session_started_at": time.time(),
                        "issue_count": 0,
                        "report_count": 1,
                        "risk": None,
                        "predictive": None,
                        "detective": None,
                        "auto_pause": None,
                        "as_of": time.time(),
                    },
                ):
                    report = PrinterHealthReport(
                        printer_name="voron",
                        metrics=[],
                        overall_status=HealthSeverity.OK,
                        checked_at=time.time(),
                    )
                    envelope = monitor._build_jsonl_envelope("voron", report)
        finally:
            sys.modules.update(saved)

        # The fields must exist (stable schema) but be None.
        assert "auto_recover" in envelope
        assert envelope["auto_recover"] is None
        assert "reroute" in envelope
        assert envelope["reroute"] is None

    def test_monitor_rich_renders_smart_panel_when_active(
        self, runner, mock_adapter, config_file
    ):
        """Rich-mode monitor must render the Smart Monitoring panel."""
        mock_instance, captured = self._stub_monitor_class(mock_adapter)

        # Override get_latest_signals to return an active session with
        # a populated risk block — that's what the panel keys off.
        active_signals = {
            "monitoring_active": True,
            "session_id": "abc12345-deadbeef",
            "session_started_at": time.time(),
            "issue_count": 2,
            "report_count": 7,
            "risk": {
                "score": 0.55,
                "severity": "amber",
                "kinds": ["thermal_drift"],
            },
            "predictive": None,
            "detective": None,
            "auto_pause": None,
            "as_of": time.time(),
        }
        mock_instance.get_latest_signals.return_value = active_signals

        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch(
            "kiln.print_health_monitor.PrintHealthMonitor", return_value=mock_instance,
        ):
            result = runner.invoke(
                cli,
                ["--printer", "test-printer", "monitor", "--timeout", "1"],
            )

        # Either the Rich panel title shows up, or (if Rich isn't
        # available in this env) the plain-text fallback marker does.
        # Either way the risk score string must appear.
        out = result.output
        assert (
            "Smart Monitoring" in out
            or "[Smart Monitoring]" in out
        ), out
        assert "0.55" in out and "amber" in out, out


# ---------------------------------------------------------------------------
# `kiln step check` — the door a user knocks on to ask "is my FreeCAD found?"
# ---------------------------------------------------------------------------


def test_step_check_reports_a_missing_backend_as_missing(runner):
    """It read the backend's DICT for truthiness, which is never falsy.

    So every backend printed "✓ available" on every machine, including ones
    that had just been searched for and not found — the report that was
    supposed to answer whether FreeCAD had been located said yes regardless.
    """
    support = {
        "any_available": True,
        "backends": {
            "freecad": {"available": False, "executable": None, "priority": 1},
            "ocp": {"available": True, "executable": None, "priority": 3},
        },
    }
    with patch("kiln.step_import.check_step_support", return_value=support):
        result = runner.invoke(cli, ["step", "check"])

    assert result.exit_code == 0, result.output
    freecad_line = next(ln for ln in result.output.splitlines() if "freecad" in ln)
    ocp_line = next(ln for ln in result.output.splitlines() if "ocp" in ln)
    assert "not found" in freecad_line, freecad_line
    assert "available" in ocp_line and "not found" not in ocp_line, ocp_line


def test_step_check_shows_where_the_backend_was_found(runner):
    """A found backend names the command, so "which FreeCAD?" is answerable."""
    support = {
        "any_available": True,
        "backends": {
            "freecad": {
                "available": True,
                "executable": "/Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c",
                "priority": 1,
            },
        },
    }
    with patch("kiln.step_import.check_step_support", return_value=support):
        result = runner.invoke(cli, ["step", "check"])

    assert result.exit_code == 0, result.output
    assert "/Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c" in result.output
