"""End-to-end integration test covering the full agent workflow.

Exercises the complete pipeline with a mock printer backend:
    discover → configure → slice → upload → print → wait → history

Each piece works in isolation (unit-tested elsewhere), but this test
verifies the modules compose correctly via the CLI layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def tmp_config(tmp_path):
    """A temporary config directory with no printers configured."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "printers": {},
        "settings": {"timeout": 30, "retries": 3},
    }))
    return cfg_path


@pytest.fixture
def mock_adapter():
    """A mock PrinterAdapter that simulates a working printer."""
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
        PrinterFile(name="benchy.gcode", path="/benchy.gcode", size_bytes=50000, date=None),
    ]
    adapter.upload_file.return_value = UploadResult(
        success=True, message="Uploaded model.gcode", file_name="model.gcode",
    )
    adapter.start_print.return_value = PrintResult(
        success=True, message="Print started: model.gcode",
    )
    adapter.cancel_print.return_value = PrintResult(
        success=True, message="Print cancelled.",
    )
    adapter.pause_print.return_value = PrintResult(
        success=True, message="Print paused.",
    )
    adapter.resume_print.return_value = PrintResult(
        success=True, message="Print resumed.",
    )
    adapter.set_tool_temp.return_value = True
    adapter.set_bed_temp.return_value = True
    adapter.send_gcode.return_value = None
    adapter.capabilities = MagicMock(can_send_gcode=True)
    adapter.get_snapshot.return_value = b"\x89PNG\x00fake_image_data"
    adapter.get_stream_url.return_value = None
    return adapter


def _make_patches(mock_adapter, config_file):
    """Return context managers patching adapter creation and config loading."""
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
# Integration test: full pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Simulate the agent workflow: discover → auth → preflight → upload → print → wait."""

    def test_discover_then_auth(self, runner, tmp_path):
        """Step 1-2: Discover a printer, then authenticate with it."""
        from kiln.cli.discovery import DiscoveredPrinter

        # Step 1: Discover
        found = [DiscoveredPrinter(
            name="Voron", printer_type="moonraker",
            host="http://192.168.1.50:7125", port=7125,
        )]
        with patch("kiln.cli.discovery.discover_printers", return_value=found):
            result = runner.invoke(cli, ["discover", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 1
        printer = data["data"]["printers"][0]
        assert printer["name"] == "Voron"
        assert printer["printer_type"] == "moonraker"

        # Step 2: Auth with discovered printer
        cfg_path = tmp_path / "config.yaml"
        with patch("kiln.cli.main.save_printer", return_value=cfg_path):
            result = runner.invoke(cli, [
                "auth",
                "--name", printer["name"],
                "--host", printer["host"],
                "--type", printer["printer_type"],
                "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["name"] == "Voron"

    def test_preflight_then_upload_then_print(self, runner, mock_adapter, tmp_path):
        """Step 3-5: Preflight check, upload a file, start printing."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            # Step 3: Preflight
            result = runner.invoke(cli, ["preflight", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["data"]["ready"] is True

            # Step 4: Upload a file
            gcode = tmp_path / "model.gcode"
            gcode.write_text(";TYPE:External perimeter\nG28\nG1 X10 Y10 Z0.2 F3000\n")
            result = runner.invoke(cli, ["upload", str(gcode), "--json"])
            assert result.exit_code == 0
            mock_adapter.upload_file.assert_called_once_with(str(gcode))

            # Step 5: Start the print
            result = runner.invoke(cli, ["print", "model.gcode", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            mock_adapter.start_print.assert_called_once_with("model.gcode")

    def test_wait_completes_when_idle(self, runner, mock_adapter, tmp_path):
        """Step 6: Wait for print to complete (printer reports IDLE)."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        # Simulate: first poll → printing, second poll → idle (print done)
        mock_adapter.get_state.side_effect = [
            PrinterState(
                state=PrinterStatus.PRINTING, connected=True,
                tool_temp_actual=210.0, tool_temp_target=210.0,
                bed_temp_actual=60.0, bed_temp_target=60.0,
            ),
            PrinterState(
                state=PrinterStatus.IDLE, connected=True,
                tool_temp_actual=35.0, tool_temp_target=0.0,
                bed_temp_actual=30.0, bed_temp_target=0.0,
            ),
        ]
        mock_adapter.get_job.side_effect = [
            JobProgress(file_name="model.gcode", completion=42.5,
                        print_time_seconds=600, print_time_left_seconds=800),
            JobProgress(file_name=None, completion=None,
                        print_time_seconds=None, print_time_left_seconds=None),
        ]

        with p1, p2, p3, patch("time.sleep"):
            result = runner.invoke(cli, ["wait", "--interval", "0.01", "--json"])

        assert result.exit_code == 0
        assert '"idle"' in result.output
        # The output is multi-line JSON from format_response; parse it
        data = json.loads(result.output)
        assert data["data"]["final_state"] == "idle"

    def test_wait_exits_on_error(self, runner, mock_adapter, tmp_path):
        """Wait exits with code 1 if printer enters error state."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )
        mock_adapter.get_job.return_value = JobProgress(
            file_name="model.gcode", completion=15.0,
            print_time_seconds=200, print_time_left_seconds=None,
        )

        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])

        assert result.exit_code != 0

    def test_history_shows_past_jobs(self, runner):
        """Step 7: View print history."""
        mock_db = MagicMock()
        mock_db.list_jobs.return_value = [
            {"id": "j1", "file_name": "model.gcode", "status": "completed",
             "started_at": 1700000000, "completed_at": 1700003600,
             "printer_name": "voron"},
        ]
        with patch("kiln.persistence.get_db", return_value=mock_db):
            result = runner.invoke(cli, ["history", "--json"])
        assert result.exit_code == 0

    def test_complete_pipeline_slice_upload_print(self, runner, mock_adapter, tmp_path):
        """Full pipeline: slice → upload → print in one shot via --print-after."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        # Create a fake STL file (slice_file will be mocked)
        stl = tmp_path / "model.stl"
        stl.write_text("solid model\nendsolid model\n")

        mock_slice_result = MagicMock()
        mock_slice_result.message = "Sliced model.stl → model.gcode"
        mock_slice_result.output_path = str(tmp_path / "model.gcode")
        mock_slice_result.to_dict.return_value = {
            "input_file": str(stl),
            "output_path": str(tmp_path / "model.gcode"),
            "message": "Sliced model.stl → model.gcode",
        }
        # Create the output file so upload can reference it
        Path(mock_slice_result.output_path).write_text("G28\n")

        with p1, p2, p3, \
             patch("kiln.slicer.slice_file", return_value=mock_slice_result):
            result = runner.invoke(cli, [
                "slice", str(stl), "--print-after", "--json",
            ])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "slice" in data["data"]
        assert "upload" in data["data"]
        assert "print" in data["data"]
        mock_adapter.upload_file.assert_called_once()
        mock_adapter.start_print.assert_called_once()

    def test_snapshot_after_print(self, runner, mock_adapter, tmp_path):
        """After printing, capture a webcam snapshot."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "image_base64" in data["data"]

    def test_auto_upload_and_print_local_file(self, runner, mock_adapter, tmp_path):
        """Print command auto-uploads local .gcode files before starting."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        gcode_file = tmp_path / "test_print.gcode"
        gcode_file.write_text("G28\nG1 X10\n")

        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode_file), "--json"])

        assert result.exit_code == 0
        # Should have uploaded then started
        mock_adapter.upload_file.assert_called_once_with(str(gcode_file))
        mock_adapter.start_print.assert_called_once_with("model.gcode")


# ---------------------------------------------------------------------------
# Edge cases: error propagation through pipeline
# ---------------------------------------------------------------------------


class TestPipelineErrorPropagation:
    """Verify errors at each pipeline stage propagate correctly."""

    def test_preflight_blocks_on_printer_error(self, runner, mock_adapter, tmp_path):
        """If printer is in error state, preflight should fail."""
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is False

    def test_upload_failure_prevents_print(self, runner, mock_adapter, tmp_path):
        """If upload fails, print should not start."""
        mock_adapter.upload_file.return_value = UploadResult(
            success=False, message="Disk full", file_name=None,
        )
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\n")

        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode), "--json"])

        assert result.exit_code != 0
        mock_adapter.start_print.assert_not_called()

    def test_adapter_connection_failure(self, runner, mock_adapter, tmp_path):
        """If printer is unreachable, status should report error."""
        mock_adapter.get_state.side_effect = Exception("Connection refused")
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code != 0
        assert "Connection refused" in result.output

    def test_printer_offline_during_wait(self, runner, mock_adapter, tmp_path):
        """Wait should fail if printer goes offline."""
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.OFFLINE, connected=False,
        )
        mock_adapter.get_job.return_value = JobProgress(
            file_name=None, completion=None,
            print_time_seconds=None, print_time_left_seconds=None,
        )

        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])

        assert result.exit_code != 0

    def test_no_webcam_returns_error(self, runner, mock_adapter, tmp_path):
        """Snapshot should fail gracefully if webcam is not available."""
        mock_adapter.get_snapshot.return_value = None
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])

        assert result.exit_code != 0
        assert "NO_WEBCAM" in result.output or "not available" in result.output.lower()
