"""Tests for new features: snapshot, wait, history, batch print, material preflight, slice CLI."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_adapter(**overrides):
    """Build a MagicMock adapter with sane defaults."""
    adapter = MagicMock()
    adapter.name = "mock"
    adapter.capabilities = PrinterCapabilities()

    adapter.get_state.return_value = overrides.get(
        "state",
        PrinterState(connected=True, state=PrinterStatus.IDLE,
                     tool_temp_actual=22.0, tool_temp_target=0.0,
                     bed_temp_actual=22.0, bed_temp_target=0.0),
    )
    adapter.get_job.return_value = overrides.get(
        "job",
        JobProgress(file_name=None, completion=None),
    )
    adapter.upload_file.return_value = overrides.get(
        "upload",
        UploadResult(success=True, file_name="test.gcode", message="Uploaded."),
    )
    adapter.start_print.return_value = overrides.get(
        "print",
        PrintResult(success=True, message="Started."),
    )
    adapter.get_snapshot.return_value = overrides.get("snapshot", None)
    return adapter


def _run_cli(args, adapter=None):
    """Run a CLI command with a mocked adapter."""
    runner = CliRunner()
    if adapter is None:
        adapter = _mock_adapter()

    with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
        result = runner.invoke(cli, args, catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Snapshot command
# ---------------------------------------------------------------------------


class TestSnapshotCommand:
    """Tests for kiln snapshot."""

    def test_snapshot_no_webcam(self):
        adapter = _mock_adapter(snapshot=None)
        result = _run_cli(["snapshot", "--json"], adapter=adapter)
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"]["code"] == "NO_WEBCAM"

    def test_snapshot_saves_to_file(self, tmp_path):
        image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG
        adapter = _mock_adapter(snapshot=image_data)
        out_file = str(tmp_path / "snap.jpg")
        result = _run_cli(["snapshot", "--output", out_file, "--json"], adapter=adapter)
        assert result.exit_code == 0
        assert os.path.isfile(out_file)
        with open(out_file, "rb") as f:
            assert f.read() == image_data

    def test_snapshot_json_base64(self):
        import base64
        image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        adapter = _mock_adapter(snapshot=image_data)
        result = _run_cli(["snapshot", "--json"], adapter=adapter)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["image_base64"] == base64.b64encode(image_data).decode()

    def test_snapshot_default_save(self):
        image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        adapter = _mock_adapter(snapshot=image_data)

        with patch("builtins.open", MagicMock()):
            result = _run_cli(["snapshot"], adapter=adapter)
        assert result.exit_code == 0
        assert "Snapshot saved" in result.output


# ---------------------------------------------------------------------------
# Wait command
# ---------------------------------------------------------------------------


class TestWaitCommand:
    """Tests for kiln wait."""

    def test_wait_already_idle(self):
        adapter = _mock_adapter()
        result = _run_cli(["wait", "--json"], adapter=adapter)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["final_state"] == "idle"

    def test_wait_error_state(self):
        adapter = _mock_adapter(
            state=PrinterState(connected=True, state=PrinterStatus.ERROR)
        )
        result = _run_cli(["wait", "--json"], adapter=adapter)
        assert result.exit_code == 1

    def test_wait_timeout(self):
        # Always return PRINTING so it never exits
        adapter = _mock_adapter(
            state=PrinterState(connected=True, state=PrinterStatus.PRINTING),
            job=JobProgress(file_name="test.gcode", completion=50.0),
        )

        runner = CliRunner()
        call_count = [0]
        base_time = 1000000.0

        def mock_time():
            call_count[0] += 1
            return base_time + call_count[0] * 10

        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            with patch("time.sleep", side_effect=lambda _: None):
                with patch("time.time", side_effect=mock_time):
                    result = runner.invoke(
                        cli, ["wait", "--timeout", "5", "--json"],
                        catch_exceptions=False,
                    )

        assert result.exit_code == 1

    def test_wait_transitions_to_idle(self):
        adapter = _mock_adapter()
        states = [
            PrinterState(connected=True, state=PrinterStatus.PRINTING),
            PrinterState(connected=True, state=PrinterStatus.PRINTING),
            PrinterState(connected=True, state=PrinterStatus.IDLE),
        ]
        adapter.get_state.side_effect = states
        adapter.get_job.return_value = JobProgress(
            file_name="test.gcode", completion=50.0
        )

        runner = CliRunner()
        with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            with patch("time.sleep", side_effect=lambda _: None):
                result = runner.invoke(
                    cli, ["wait", "--json", "--interval", "0.01"],
                    catch_exceptions=False,
                )

        assert result.exit_code == 0
        # Parse the JSON output — format_response returns indented JSON
        data = json.loads(result.output.strip())
        assert data["data"]["final_state"] == "idle"


# ---------------------------------------------------------------------------
# History command
# ---------------------------------------------------------------------------


class TestHistoryCommand:
    """Tests for kiln history."""

    def test_history_empty(self):
        with patch("kiln.persistence.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.list_jobs.return_value = []
            mock_get_db.return_value = mock_db

            runner = CliRunner()
            result = runner.invoke(cli, ["history", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 0

    def test_history_with_records(self):
        jobs = [
            {
                "id": "abc123",
                "file_name": "benchy.gcode",
                "printer_name": "voron",
                "status": "completed",
                "priority": 0,
                "submitted_by": "cli",
                "submitted_at": 1700000000.0,
                "started_at": 1700000010.0,
                "completed_at": 1700003600.0,
                "error_message": None,
            },
        ]

        with patch("kiln.persistence.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.list_jobs.return_value = jobs
            mock_get_db.return_value = mock_db

            runner = CliRunner()
            result = runner.invoke(cli, ["history", "--json", "--limit", "5"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 1
        assert data["data"]["jobs"][0]["file_name"] == "benchy.gcode"

    def test_history_status_filter(self):
        with patch("kiln.persistence.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.list_jobs.return_value = []
            mock_get_db.return_value = mock_db

            runner = CliRunner()
            result = runner.invoke(
                cli, ["history", "--status", "failed", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        mock_db.list_jobs.assert_called_once_with(status="failed", limit=20)


# ---------------------------------------------------------------------------
# Material tracking in preflight
# ---------------------------------------------------------------------------


class TestPreflightMaterial:
    """Tests for kiln preflight --material."""

    def test_material_pla_temps_ok(self):
        adapter = _mock_adapter(
            state=PrinterState(
                connected=True, state=PrinterStatus.IDLE,
                tool_temp_actual=22.0, tool_temp_target=200.0,
                bed_temp_actual=22.0, bed_temp_target=60.0,
            )
        )
        result = _run_cli(["preflight", "--material", "PLA", "--json"], adapter=adapter)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is True

    def test_material_pla_tool_too_hot(self):
        adapter = _mock_adapter(
            state=PrinterState(
                connected=True, state=PrinterStatus.IDLE,
                tool_temp_actual=22.0, tool_temp_target=250.0,
                bed_temp_actual=22.0, bed_temp_target=60.0,
            )
        )
        result = _run_cli(["preflight", "--material", "PLA", "--json"], adapter=adapter)
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["data"]["ready"] is False
        # Find material_match check
        mat_check = [c for c in data["data"]["checks"] if c["name"] == "material_match"]
        assert len(mat_check) == 1
        assert mat_check[0]["passed"] is False

    def test_material_petg_ok(self):
        adapter = _mock_adapter(
            state=PrinterState(
                connected=True, state=PrinterStatus.IDLE,
                tool_temp_actual=22.0, tool_temp_target=240.0,
                bed_temp_actual=22.0, bed_temp_target=80.0,
            )
        )
        result = _run_cli(["preflight", "--material", "PETG", "--json"], adapter=adapter)
        assert result.exit_code == 0

    def test_material_no_target_temps_is_ok(self):
        """If no target temps are set yet, material check passes."""
        adapter = _mock_adapter(
            state=PrinterState(
                connected=True, state=PrinterStatus.IDLE,
                tool_temp_actual=22.0, tool_temp_target=0.0,
                bed_temp_actual=22.0, bed_temp_target=0.0,
            )
        )
        result = _run_cli(["preflight", "--material", "ABS", "--json"], adapter=adapter)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Batch printing
# ---------------------------------------------------------------------------


class TestBatchPrint:
    """Tests for kiln print with multiple files."""

    def test_single_file_print(self):
        adapter = _mock_adapter()
        result = _run_cli(["print", "test.gcode", "--json"], adapter=adapter)
        assert result.exit_code == 0
        adapter.start_print.assert_called_once_with("test.gcode")

    def test_print_status_no_file(self):
        adapter = _mock_adapter()
        result = _run_cli(["print", "--json"], adapter=adapter)
        assert result.exit_code == 0
        adapter.get_state.assert_called()

    def test_batch_without_queue_warns(self):
        adapter = _mock_adapter()
        result = _run_cli(["print", "a.gcode", "b.gcode"], adapter=adapter)
        assert result.exit_code == 0
        # Should start first file and warn about rest
        adapter.start_print.assert_called_once_with("a.gcode")
        assert "Remaining" in result.output or "b.gcode" in result.output

    def test_batch_with_queue(self, tmp_path):
        adapter = _mock_adapter()

        with patch("kiln.persistence.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            result = _run_cli(
                ["print", "a.gcode", "b.gcode", "c.gcode", "--queue", "--json"],
                adapter=adapter,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 3


# ---------------------------------------------------------------------------
# Slice command (CLI)
# ---------------------------------------------------------------------------


class TestSliceCommand:
    """Tests for kiln slice."""

    def test_slice_success(self, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        runner = CliRunner()
        with patch("kiln.slicer.slice_file") as mock_slice:
            from kiln.slicer import SliceResult
            mock_slice.return_value = SliceResult(
                success=True,
                output_path=str(tmp_path / "model.gcode"),
                slicer="prusa-slicer",
                message="Sliced model.stl -> model.gcode",
            )

            result = runner.invoke(
                cli, ["slice", str(stl), "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["success"] is True

    def test_slice_slicer_not_found(self, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        runner = CliRunner()
        with patch("kiln.slicer.slice_file") as mock_slice:
            from kiln.slicer import SlicerNotFoundError
            mock_slice.side_effect = SlicerNotFoundError("No slicer found")

            result = runner.invoke(
                cli, ["slice", str(stl), "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"]["code"] == "SLICER_NOT_FOUND"

    def test_slice_and_print(self, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid test\nendsolid test\n")

        gcode_path = str(tmp_path / "model.gcode")
        (tmp_path / "model.gcode").write_text("; gcode")

        adapter = _mock_adapter()

        runner = CliRunner()
        with patch("kiln.slicer.slice_file") as mock_slice:
            from kiln.slicer import SliceResult
            mock_slice.return_value = SliceResult(
                success=True,
                output_path=gcode_path,
                slicer="prusa-slicer",
                message="Sliced",
            )
            with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
                result = runner.invoke(
                    cli, ["slice", str(stl), "--print-after", "--json"],
                    catch_exceptions=False,
                )

        assert result.exit_code == 0
        adapter.upload_file.assert_called_once_with(gcode_path)
        adapter.start_print.assert_called_once()


# ---------------------------------------------------------------------------
# Webcam in adapter base
# ---------------------------------------------------------------------------


class TestAdapterSnapshot:
    """Tests for the get_snapshot method on adapters."""

    def test_base_returns_none(self):
        """Default get_snapshot on base class returns None."""
        from kiln.printers.base import PrinterAdapter

        # Create a concrete subclass for testing
        class TestAdapter(PrinterAdapter):
            @property
            def name(self): return "test"
            @property
            def capabilities(self): return PrinterCapabilities()
            def get_state(self): pass
            def get_job(self): pass
            def list_files(self): pass
            def upload_file(self, path): pass
            def start_print(self, name): pass
            def cancel_print(self): pass
            def pause_print(self): pass
            def resume_print(self): pass
            def set_tool_temp(self, t): pass
            def set_bed_temp(self, t): pass
            def send_gcode(self, cmds): pass
            def delete_file(self, path): pass
            def emergency_stop(self): pass

        adapter = TestAdapter()
        assert adapter.get_snapshot() is None

    def test_octoprint_snapshot_success(self):
        """OctoPrint adapter fetches from /webcam/?action=snapshot."""
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter(host="http://test.local", api_key="TESTKEY")

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.content = b"\xff\xd8\xff\xe0jpeg"

        adapter._session.get = MagicMock(return_value=mock_response)
        result = adapter.get_snapshot()

        assert result == b"\xff\xd8\xff\xe0jpeg"
        adapter._session.get.assert_called_once()

    def test_octoprint_snapshot_failure(self):
        """OctoPrint adapter returns None on webcam error."""
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter(host="http://test.local", api_key="TESTKEY")
        adapter._session.get = MagicMock(side_effect=Exception("connection failed"))

        result = adapter.get_snapshot()
        assert result is None

    def test_moonraker_snapshot_success(self):
        """Moonraker adapter discovers webcam and fetches snapshot."""
        from kiln.printers.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(host="http://klipper.local")

        # Mock the webcam list response
        webcam_response = MagicMock()
        webcam_response.ok = True
        webcam_response.json.return_value = {
            "result": {
                "webcams": [
                    {"snapshot_url": "/webcam/snapshot", "name": "default"}
                ]
            }
        }

        # Mock the actual snapshot response
        snapshot_response = MagicMock()
        snapshot_response.ok = True
        snapshot_response.content = b"\xff\xd8\xff\xe0jpeg"

        # First _get_json call is the webcams list
        original_get_json = adapter._get_json
        adapter._get_json = MagicMock(return_value=webcam_response.json())
        adapter._session.get = MagicMock(return_value=snapshot_response)

        result = adapter.get_snapshot()
        assert result == b"\xff\xd8\xff\xe0jpeg"

    def test_moonraker_no_webcam(self):
        """Moonraker adapter returns None when no webcams configured."""
        from kiln.printers.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(host="http://klipper.local")

        adapter._get_json = MagicMock(return_value={
            "result": {"webcams": []}
        })

        result = adapter.get_snapshot()
        assert result is None


# ---------------------------------------------------------------------------
# Output formatting: format_history
# ---------------------------------------------------------------------------


class TestFormatHistory:
    """Tests for format_history output formatter."""

    def test_json_mode(self):
        from kiln.cli.output import format_history

        jobs = [
            {"file_name": "test.gcode", "status": "completed",
             "printer_name": "voron", "submitted_at": 1700000000.0,
             "started_at": 1700000010.0, "completed_at": 1700003600.0},
        ]
        result = format_history(jobs, json_mode=True)
        data = json.loads(result)
        assert data["data"]["count"] == 1

    def test_empty_json(self):
        from kiln.cli.output import format_history

        result = format_history([], json_mode=True)
        data = json.loads(result)
        assert data["data"]["count"] == 0

    def test_human_readable(self):
        from kiln.cli.output import format_history

        jobs = [
            {"file_name": "test.gcode", "status": "completed",
             "printer_name": "voron", "submitted_at": 1700000000.0,
             "started_at": 1700000010.0, "completed_at": 1700003600.0},
        ]
        result = format_history(jobs, json_mode=False)
        assert "test.gcode" in result

    def test_empty_human_readable(self):
        from kiln.cli.output import format_history

        result = format_history([], json_mode=False)
        assert "No print history" in result
