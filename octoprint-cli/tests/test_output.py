"""Tests for octoprint_cli.output formatting helpers."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from octoprint_cli.output import (
    format_bytes,
    format_file_list,
    format_printer_status,
    format_response,
    format_temp,
    format_time,
    format_upload_result,
    format_job_action,
    progress_bar,
    _flatten_files,
)


# ===================================================================
# format_time
# ===================================================================


class TestFormatTime:
    """Tests for format_time()."""

    def test_zero_seconds(self) -> None:
        assert format_time(0) == "0s"

    def test_seconds_only(self) -> None:
        assert format_time(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert format_time(125) == "2m 5s"

    def test_hours_minutes_seconds(self) -> None:
        assert format_time(3661) == "1h 1m 1s"

    def test_hours_only(self) -> None:
        assert format_time(3600) == "1h 0s"

    def test_large_value(self) -> None:
        result = format_time(86400)  # 24 hours
        assert "24h" in result

    def test_none_returns_na(self) -> None:
        assert format_time(None) == "N/A"

    def test_negative_returns_na(self) -> None:
        assert format_time(-10) == "N/A"

    def test_float_value(self) -> None:
        result = format_time(90.7)
        assert result == "1m 30s"

    def test_exact_minute(self) -> None:
        assert format_time(60) == "1m 0s"


# ===================================================================
# format_bytes
# ===================================================================


class TestFormatBytes:
    """Tests for format_bytes()."""

    def test_zero_bytes(self) -> None:
        assert format_bytes(0) == "0 B"

    def test_bytes_range(self) -> None:
        assert format_bytes(500) == "500 B"

    def test_kilobytes(self) -> None:
        result = format_bytes(1024)
        assert "KB" in result
        assert "1.0" in result

    def test_megabytes(self) -> None:
        result = format_bytes(1048576)
        assert "MB" in result
        assert "1.0" in result

    def test_gigabytes(self) -> None:
        result = format_bytes(1073741824)
        assert "GB" in result

    def test_terabytes(self) -> None:
        result = format_bytes(1099511627776)
        assert "TB" in result

    def test_none_returns_na(self) -> None:
        assert format_bytes(None) == "N/A"

    def test_negative_returns_na(self) -> None:
        assert format_bytes(-100) == "N/A"

    def test_fractional_megabytes(self) -> None:
        result = format_bytes(1500000)
        assert "MB" in result

    def test_one_byte(self) -> None:
        assert format_bytes(1) == "1 B"


# ===================================================================
# format_temp
# ===================================================================


class TestFormatTemp:
    """Tests for format_temp()."""

    def test_both_values(self) -> None:
        result = format_temp(214.8, 220.0)
        assert "214.8" in result
        assert "220.0" in result
        assert "/" in result

    def test_zero_target(self) -> None:
        result = format_temp(24.3, 0.0)
        assert "24.3" in result
        assert "0.0" in result

    def test_actual_none(self) -> None:
        result = format_temp(None, 220.0)
        assert "N/A" in result
        assert "220.0" in result

    def test_target_none(self) -> None:
        result = format_temp(214.8, None)
        assert "214.8" in result
        assert "N/A" in result

    def test_both_none(self) -> None:
        result = format_temp(None, None)
        assert result.count("N/A") == 2

    def test_degree_symbol(self) -> None:
        result = format_temp(200.0, 210.0)
        assert "\u00b0C" in result


# ===================================================================
# progress_bar
# ===================================================================


class TestProgressBar:
    """Tests for progress_bar()."""

    def test_zero_percent(self) -> None:
        result = progress_bar(0.0)
        assert "0.0%" in result

    def test_fifty_percent(self) -> None:
        result = progress_bar(50.0)
        assert "50.0%" in result

    def test_hundred_percent(self) -> None:
        result = progress_bar(100.0)
        assert "100.0%" in result

    def test_none_treated_as_zero(self) -> None:
        result = progress_bar(None)
        assert "0.0%" in result

    def test_clamped_above_100(self) -> None:
        result = progress_bar(150.0)
        assert "100.0%" in result

    def test_clamped_below_0(self) -> None:
        result = progress_bar(-10.0)
        assert "0.0%" in result

    def test_has_brackets(self) -> None:
        result = progress_bar(42.3)
        assert result.startswith("[")
        assert "]" in result


# ===================================================================
# format_response
# ===================================================================


class TestFormatResponse:
    """Tests for format_response()."""

    def test_json_success(self) -> None:
        result = format_response(
            "success",
            data={"action": "start"},
            json_mode=True,
        )
        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["action"] == "start"
        assert parsed["error"] is None

    def test_json_error(self) -> None:
        result = format_response(
            "error",
            error={"code": "AUTH_ERROR", "message": "Bad key"},
            json_mode=True,
        )
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["error"]["code"] == "AUTH_ERROR"
        assert parsed["data"] is None

    def test_human_error(self) -> None:
        result = format_response(
            "error",
            error={"code": "AUTH_ERROR", "message": "Bad key"},
            json_mode=False,
        )
        assert "AUTH_ERROR" in result
        assert "Bad key" in result

    def test_human_success_with_data(self) -> None:
        result = format_response(
            "success",
            data={"key1": "value1", "key2": "value2"},
            json_mode=False,
        )
        assert "key1" in result
        assert "value1" in result

    def test_human_no_data(self) -> None:
        result = format_response("success", json_mode=False)
        assert "success" in result.lower()

    def test_json_output_is_valid_json(self) -> None:
        result = format_response(
            "success",
            data={"nested": {"a": 1}},
            json_mode=True,
        )
        parsed = json.loads(result)
        assert isinstance(parsed, dict)


# ===================================================================
# format_printer_status
# ===================================================================


class TestFormatPrinterStatus:
    """Tests for format_printer_status()."""

    def test_json_mode(
        self,
        printer_state_operational: Dict[str, Any],
        job_data_printing: Dict[str, Any],
    ) -> None:
        result = format_printer_status(
            printer_state_operational,
            job_data_printing,
            json_mode=True,
        )
        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["state"] == "Operational"
        assert parsed["data"]["temperature"]["tool0"]["actual"] == 24.3
        assert parsed["data"]["job"]["file"] == "test_model.gcode"
        assert parsed["data"]["job"]["completion"] == 42.5

    def test_human_mode(
        self,
        printer_state_operational: Dict[str, Any],
        job_data_printing: Dict[str, Any],
    ) -> None:
        result = format_printer_status(
            printer_state_operational,
            job_data_printing,
            json_mode=False,
        )
        assert "Operational" in result
        assert "test_model.gcode" in result

    def test_none_data(self) -> None:
        result = format_printer_status(None, None, json_mode=True)
        parsed = json.loads(result)
        assert parsed["data"]["state"] == "Unknown"

    def test_no_job_data(
        self,
        printer_state_operational: Dict[str, Any],
    ) -> None:
        result = format_printer_status(
            printer_state_operational,
            None,
            json_mode=True,
        )
        parsed = json.loads(result)
        assert parsed["data"]["job"]["file"] is None
        assert parsed["data"]["job"]["completion"] is None

    def test_idle_no_progress(
        self,
        printer_state_operational: Dict[str, Any],
        job_data_idle: Dict[str, Any],
    ) -> None:
        result = format_printer_status(
            printer_state_operational,
            job_data_idle,
            json_mode=False,
        )
        assert "Operational" in result

    def test_json_structure_keys(
        self,
        printer_state_operational: Dict[str, Any],
    ) -> None:
        result = format_printer_status(
            printer_state_operational,
            None,
            json_mode=True,
        )
        parsed = json.loads(result)
        assert "state" in parsed["data"]
        assert "temperature" in parsed["data"]
        assert "job" in parsed["data"]
        assert "tool0" in parsed["data"]["temperature"]
        assert "bed" in parsed["data"]["temperature"]


# ===================================================================
# format_file_list
# ===================================================================


class TestFormatFileList:
    """Tests for format_file_list()."""

    def test_json_mode(
        self,
        files_list_response: Dict[str, Any],
    ) -> None:
        result = format_file_list(files_list_response, json_mode=True)
        parsed = json.loads(result)
        assert parsed["status"] == "success"
        files = parsed["data"]["files"]
        assert len(files) == 3
        names = [f["name"] for f in files]
        assert "benchy.gcode" in names
        assert "calibration/cube.gcode" in names
        assert "vase.gcode" in names

    def test_human_mode(
        self,
        files_list_response: Dict[str, Any],
    ) -> None:
        result = format_file_list(files_list_response, json_mode=False)
        assert "benchy.gcode" in result
        assert "cube.gcode" in result
        assert "vase.gcode" in result

    def test_empty_file_list(self) -> None:
        result = format_file_list({"files": []}, json_mode=False)
        assert "no files" in result.lower()

    def test_none_data(self) -> None:
        result = format_file_list(None, json_mode=True)
        parsed = json.loads(result)
        assert parsed["data"]["files"] == []

    def test_json_file_entry_structure(
        self,
        files_list_response: Dict[str, Any],
    ) -> None:
        result = format_file_list(files_list_response, json_mode=True)
        parsed = json.loads(result)
        for f in parsed["data"]["files"]:
            assert "name" in f
            assert "size" in f
            assert "date" in f
            assert "type" in f


# ===================================================================
# _flatten_files
# ===================================================================


class TestFlattenFiles:
    """Tests for the internal _flatten_files helper."""

    def test_no_folders(self) -> None:
        entries = [
            {"name": "a.gcode", "type": "machinecode"},
            {"name": "b.gcode", "type": "machinecode"},
        ]
        flat = _flatten_files(entries)
        assert len(flat) == 2
        assert flat[0]["display_name"] == "a.gcode"
        assert flat[1]["display_name"] == "b.gcode"

    def test_nested_folder(self) -> None:
        entries = [
            {
                "name": "folder1",
                "type": "folder",
                "children": [
                    {"name": "inner.gcode", "type": "machinecode"},
                ],
            },
        ]
        flat = _flatten_files(entries)
        assert len(flat) == 1
        assert flat[0]["display_name"] == "folder1/inner.gcode"

    def test_deeply_nested(self) -> None:
        entries = [
            {
                "name": "a",
                "type": "folder",
                "children": [
                    {
                        "name": "b",
                        "type": "folder",
                        "children": [
                            {"name": "deep.gcode", "type": "machinecode"},
                        ],
                    },
                ],
            },
        ]
        flat = _flatten_files(entries)
        assert len(flat) == 1
        assert flat[0]["display_name"] == "a/b/deep.gcode"

    def test_empty_list(self) -> None:
        flat = _flatten_files([])
        assert flat == []

    def test_empty_folder(self) -> None:
        entries = [
            {"name": "empty_dir", "type": "folder", "children": []},
        ]
        flat = _flatten_files(entries)
        assert flat == []

    def test_mixed_files_and_folders(self) -> None:
        entries = [
            {"name": "top.gcode", "type": "machinecode"},
            {
                "name": "sub",
                "type": "folder",
                "children": [
                    {"name": "nested.gcode", "type": "machinecode"},
                ],
            },
        ]
        flat = _flatten_files(entries)
        assert len(flat) == 2
        names = [f["display_name"] for f in flat]
        assert "top.gcode" in names
        assert "sub/nested.gcode" in names


# ===================================================================
# format_upload_result
# ===================================================================


class TestFormatUploadResult:
    """Tests for format_upload_result()."""

    def test_json_mode(self, upload_response: Dict[str, Any]) -> None:
        result = format_upload_result(upload_response, json_mode=True)
        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["done"] is True

    def test_human_mode(self, upload_response: Dict[str, Any]) -> None:
        result = format_upload_result(upload_response, json_mode=False)
        assert "upload complete" in result.lower()
        assert "test_model.gcode" in result

    def test_none_data(self) -> None:
        result = format_upload_result(None, json_mode=True)
        parsed = json.loads(result)
        assert parsed["status"] == "success"

    def test_processing_not_done(self) -> None:
        data = {
            "files": {"local": {"name": "model.gcode"}},
            "done": False,
        }
        result = format_upload_result(data, json_mode=False)
        assert "processing" in result.lower()


# ===================================================================
# format_job_action
# ===================================================================


class TestFormatJobAction:
    """Tests for format_job_action()."""

    @pytest.mark.parametrize(
        "action,expected_message",
        [
            ("start", "Print job started."),
            ("cancel", "Print job cancelled."),
            ("pause", "Print job paused."),
            ("resume", "Print job resumed."),
            ("restart", "Print job restarted."),
        ],
    )
    def test_json_mode_known_actions(
        self, action: str, expected_message: str
    ) -> None:
        result = format_job_action(action, None, json_mode=True)
        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["action"] == action
        assert parsed["data"]["message"] == expected_message

    def test_unknown_action(self) -> None:
        result = format_job_action("custom_action", None, json_mode=True)
        parsed = json.loads(result)
        assert "custom_action" in parsed["data"]["message"]

    def test_human_mode_start(self) -> None:
        result = format_job_action("start", None, json_mode=False)
        assert "started" in result.lower()

    def test_human_mode_cancel(self) -> None:
        result = format_job_action("cancel", None, json_mode=False)
        assert "cancelled" in result.lower()

    def test_result_data_merged(self) -> None:
        result = format_job_action(
            "start",
            {"extra_key": "extra_val"},
            json_mode=True,
        )
        parsed = json.loads(result)
        assert parsed["data"]["extra_key"] == "extra_val"
