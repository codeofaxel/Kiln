"""Tests for print_status_lite, list_snapshots, and watch_print save_to_disk.

Covers:
- print_status_lite (printing state, idle state, printer not found)
- list_snapshots (filter passthrough, empty results, DB errors)
- watch_print save_to_disk parameter
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.base import PrinterStatus
from kiln.registry import PrinterNotFoundError


class TestPrintStatusLite:
    """print_status_lite tool tests."""

    @patch("kiln.server._get_adapter")
    def test_returns_minimal_state(self, mock_get_adapter):
        from kiln.server import print_status_lite

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        mock_state.hotend_temp = 210.0
        mock_state.bed_temp = 60.0
        mock_adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = 45.2
        mock_job.file_name = "benchy.gcode"
        mock_job.time_left = 1800
        mock_job.time_elapsed = 600
        mock_adapter.get_job.return_value = mock_job

        mock_get_adapter.return_value = mock_adapter

        result = print_status_lite()
        assert result["state"] == "printing"
        assert result["completion_pct"] == 45.2
        assert result["file_name"] == "benchy.gcode"
        assert result["eta_seconds"] == 1800
        assert result["elapsed_seconds"] == 600
        assert result["hotend_temp"] == 210.0
        assert result["bed_temp"] == 60.0

    @patch("kiln.server._get_adapter")
    def test_idle_no_temps(self, mock_get_adapter):
        from kiln.server import print_status_lite

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.IDLE
        mock_adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = None
        mock_job.file_name = None
        mock_job.time_left = None
        mock_job.time_elapsed = None
        mock_adapter.get_job.return_value = mock_job

        mock_get_adapter.return_value = mock_adapter

        result = print_status_lite()
        assert result["state"] == "idle"
        assert "hotend_temp" not in result
        assert "bed_temp" not in result

    @patch("kiln.server._get_adapter")
    def test_idle_no_eta_or_elapsed(self, mock_get_adapter):
        from kiln.server import print_status_lite

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.IDLE
        mock_adapter.get_state.return_value = mock_state

        mock_job = MagicMock()
        mock_job.completion = None
        mock_job.file_name = None
        mock_job.time_left = None
        mock_job.time_elapsed = None
        mock_adapter.get_job.return_value = mock_job

        mock_get_adapter.return_value = mock_adapter

        result = print_status_lite()
        assert "eta_seconds" not in result
        assert "elapsed_seconds" not in result

    @patch("kiln.server._get_adapter")
    def test_printer_not_found_via_registry(self, mock_get_adapter):
        from kiln.server import print_status_lite

        with patch("kiln.server._registry") as mock_registry:
            mock_registry.get.side_effect = PrinterNotFoundError("nope")
            result = print_status_lite(printer_name="ghost")
            assert result["state"] == "not_found"


class TestListSnapshots:
    """list_snapshots tool tests."""

    @patch("kiln.server.get_db")
    def test_returns_snapshots(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = [
            {"id": 1, "printer_name": "voron", "phase": "timelapse"},
        ]
        mock_get_db.return_value = mock_db

        result = list_snapshots(printer_name="voron")
        assert result["success"] is True
        assert result["count"] == 1
        mock_db.get_snapshots.assert_called_once_with(
            job_id=None, printer_name="voron", phase=None, limit=20,
        )

    @patch("kiln.server.get_db")
    def test_empty_result(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = []
        mock_get_db.return_value = mock_db

        result = list_snapshots()
        assert result["success"] is True
        assert result["count"] == 0

    @patch("kiln.server.get_db")
    def test_passes_all_filters(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = []
        mock_get_db.return_value = mock_db

        list_snapshots(printer_name="v", job_id="j1", phase="timelapse", limit=5)
        mock_db.get_snapshots.assert_called_once_with(
            job_id="j1", printer_name="v", phase="timelapse", limit=5,
        )

    @patch("kiln.server.get_db")
    def test_db_error_returns_error_dict(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_get_db.side_effect = RuntimeError("db exploded")

        result = list_snapshots()
        assert result["success"] is False
        assert "error" in result


class TestWatchPrintSaveToDisk:
    """watch_print save_to_disk parameter tests."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._get_adapter")
    def test_save_to_disk_returns_save_dir(self, mock_get_adapter, mock_auth):
        from kiln.server import watch_print, _watchers

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        mock_adapter.get_state.return_value = mock_state
        mock_job = MagicMock()
        mock_job.completion = 10.0
        mock_adapter.get_job.return_value = mock_job
        mock_get_adapter.return_value = mock_adapter

        result = watch_print(save_to_disk=True, max_snapshots=1, timeout=5)
        assert result["success"] is True
        assert result["save_to_disk"] is True
        assert "save_dir" in result
        assert "timelapses" in result["save_dir"]

        # Clean up
        from kiln.server import stop_watch_print
        stop_watch_print(result["watch_id"])

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._get_adapter")
    def test_default_no_save_dir(self, mock_get_adapter, mock_auth):
        from kiln.server import watch_print

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        mock_adapter.get_state.return_value = mock_state
        mock_job = MagicMock()
        mock_job.completion = 10.0
        mock_adapter.get_job.return_value = mock_job
        mock_get_adapter.return_value = mock_adapter

        result = watch_print(max_snapshots=1, timeout=5)
        assert result["success"] is True
        assert result["save_to_disk"] is False
        assert "save_dir" not in result

        # Clean up
        from kiln.server import stop_watch_print
        stop_watch_print(result["watch_id"])
