"""Tests for kiln.cli.main — CLI commands using Click's CliRunner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
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
        from kiln.licensing import LicenseManager, LicenseTier, generate_license_key

        _secret = "test-cli-signing-secret"
        license_file = tmp_path / "license"
        key = generate_license_key(LicenseTier.PRO, "test@example.com", signing_key=_secret)
        with patch.dict("os.environ", {"KILN_LICENSE_SIGNING_SECRET": _secret}, clear=False):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            with patch("kiln.licensing._manager", mgr):
                result = runner.invoke(cli, ["upgrade", "--key", key])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert license_file.exists()

    def test_upgrade_activates_key_json_mode(self, runner, tmp_path):
        """kiln upgrade --key --json returns valid JSON."""
        from kiln.licensing import LicenseManager, LicenseTier, generate_license_key

        _secret = "test-cli-signing-secret"
        key = generate_license_key(LicenseTier.PRO, "test@example.com", signing_key=_secret)
        with patch.dict("os.environ", {"KILN_LICENSE_SIGNING_SECRET": _secret}, clear=False):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            with patch("kiln.licensing._manager", mgr):
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

        with patch.dict("os.environ", {"KILN_LICENSE_SIGNING_SECRET": _secret}, clear=False):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            with patch("kiln.licensing._manager", mgr):
                result = runner.invoke(cli, ["upgrade"])
        assert result.exit_code == 0
        assert "Pro" in result.output
        assert "Active" in result.output or "valid" in result.output.lower()


# ---------------------------------------------------------------------------
# Fleet commands (kiln fleet status, kiln fleet register)
# ---------------------------------------------------------------------------


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
        mock_result = {"success": True, "job_id": "test-job-123", "position": 1}
        with patch("kiln.licensing._manager", mgr), \
             patch.dict("os.environ", {}, clear=True), \
             patch("kiln.plugins.queue_tools.submit_job", return_value=mock_result):
            result = runner.invoke(cli, ["queue", "submit", "test.gcode"])
        assert result.exit_code == 0
