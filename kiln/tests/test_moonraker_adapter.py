"""Tests for the Moonraker / Klipper printer adapter.

Every public method of :class:`MoonrakerAdapter` is exercised with mocked
HTTP responses so the test suite runs without a real Moonraker instance.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional
from unittest import mock

import pytest
import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import Timeout

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.moonraker import (
    MoonrakerAdapter,
    _map_moonraker_state,
    _safe_get,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

HOST = "http://klipper.local:7125"


def _adapter(**kwargs: Any) -> MoonrakerAdapter:
    """Create a :class:`MoonrakerAdapter` with sensible test defaults."""
    defaults: Dict[str, Any] = {
        "host": HOST,
        "timeout": 5,
        "retries": 1,
    }
    defaults.update(kwargs)
    return MoonrakerAdapter(**defaults)


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    text: str = "",
    ok: Optional[bool] = None,
) -> mock.MagicMock:
    """Build a fake :class:`requests.Response`."""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok if ok is not None else (200 <= status_code < 300)
    resp.text = text or json.dumps(json_data or {})
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------

class TestMoonrakerAdapterInit:
    """Tests for adapter construction and identity properties."""

    def test_empty_host_raises(self) -> None:
        with pytest.raises(ValueError, match="host must not be empty"):
            MoonrakerAdapter(host="")

    def test_trailing_slash_stripped(self) -> None:
        adapter = _adapter(host="http://klipper.local:7125/")
        assert adapter._host == "http://klipper.local:7125"

    def test_name_property(self) -> None:
        adapter = _adapter()
        assert adapter.name == "moonraker"

    def test_capabilities(self) -> None:
        caps = _adapter().capabilities
        assert isinstance(caps, PrinterCapabilities)
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True
        assert ".gcode" in caps.supported_extensions

    def test_repr(self) -> None:
        adapter = _adapter()
        assert "MoonrakerAdapter" in repr(adapter)
        assert HOST in repr(adapter)

    def test_no_api_key_by_default(self) -> None:
        adapter = _adapter()
        assert "X-Api-Key" not in adapter._session.headers

    def test_api_key_set_when_provided(self) -> None:
        adapter = _adapter(api_key="my-secret-key")
        assert adapter._session.headers["X-Api-Key"] == "my-secret-key"

    def test_empty_string_api_key_treated_as_none(self) -> None:
        adapter = _adapter(api_key="")
        assert "X-Api-Key" not in adapter._session.headers

    def test_retries_at_least_one(self) -> None:
        adapter = _adapter(retries=0)
        assert adapter._retries == 1


# ---------------------------------------------------------------------------
# _safe_get helper
# ---------------------------------------------------------------------------

class TestSafeGet:

    def test_simple_nested(self) -> None:
        data = {"a": {"b": {"c": 42}}}
        assert _safe_get(data, "a", "b", "c") == 42

    def test_missing_key(self) -> None:
        data = {"a": {"b": 1}}
        assert _safe_get(data, "a", "x") is None

    def test_custom_default(self) -> None:
        assert _safe_get({}, "missing", default="fallback") == "fallback"

    def test_non_dict_intermediate(self) -> None:
        data = {"a": "not-a-dict"}
        assert _safe_get(data, "a", "b") is None

    def test_empty_keys(self) -> None:
        data = {"x": 1}
        assert _safe_get(data) == data


# ---------------------------------------------------------------------------
# _map_moonraker_state helper
# ---------------------------------------------------------------------------

class TestMapMoonrakerState:

    @pytest.mark.parametrize(
        "state_string, expected",
        [
            ("ready", PrinterStatus.IDLE),
            ("printing", PrinterStatus.PRINTING),
            ("paused", PrinterStatus.PAUSED),
            ("error", PrinterStatus.ERROR),
            ("shutdown", PrinterStatus.OFFLINE),
            ("startup", PrinterStatus.BUSY),
            ("standby", PrinterStatus.IDLE),
            ("complete", PrinterStatus.IDLE),
            ("cancelled", PrinterStatus.IDLE),
        ],
    )
    def test_basic_states(self, state_string: str, expected: PrinterStatus) -> None:
        assert _map_moonraker_state(state_string) == expected

    def test_unknown_state(self) -> None:
        assert _map_moonraker_state("garbage") == PrinterStatus.UNKNOWN

    def test_ready_with_printing_print_state(self) -> None:
        assert _map_moonraker_state("ready", "printing") == PrinterStatus.PRINTING

    def test_ready_with_paused_print_state(self) -> None:
        assert _map_moonraker_state("ready", "paused") == PrinterStatus.PAUSED

    def test_ready_with_standby_print_state(self) -> None:
        assert _map_moonraker_state("ready", "standby") == PrinterStatus.IDLE

    def test_ready_with_unknown_print_state_falls_back(self) -> None:
        # If print_state is not in _STATE_MAP, fall back to klippy state.
        assert _map_moonraker_state("ready", "never-heard-of-this") == PrinterStatus.IDLE

    def test_non_ready_ignores_print_state(self) -> None:
        # If klippy is not "ready", we don't check print_state.
        assert _map_moonraker_state("error", "printing") == PrinterStatus.ERROR


# ---------------------------------------------------------------------------
# HTTP layer tests
# ---------------------------------------------------------------------------

class TestHTTPLayer:
    """Tests for _request, _get_json, _post retry / error handling."""

    def test_successful_get(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})
        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter._get_json("/printer/info")
        assert result == {"result": "ok"}

    def test_non_retryable_http_error(self) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=404, text="Not Found", ok=False)
        with mock.patch.object(adapter._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="HTTP 404"):
                adapter._get_json("/printer/info")

    def test_retryable_http_error_exhausts_retries(self) -> None:
        adapter = _adapter(retries=2)
        resp = _mock_response(status_code=503, text="Unavailable", ok=False)
        with mock.patch.object(adapter._session, "request", return_value=resp):
            with mock.patch("kiln.printers.moonraker.time.sleep"):
                with pytest.raises(PrinterError, match="HTTP 503"):
                    adapter._get_json("/printer/info")

    def test_timeout_raises_printer_error(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session, "request", side_effect=Timeout("timed out")
        ):
            with pytest.raises(PrinterError, match="timed out"):
                adapter._get_json("/printer/info")

    def test_connection_error_raises_printer_error(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session, "request", side_effect=ReqConnectionError("refused")
        ):
            with pytest.raises(PrinterError, match="Could not connect"):
                adapter._get_json("/printer/info")

    def test_invalid_json_raises_printer_error(self) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=200)
        resp.json.side_effect = ValueError("bad json")
        with mock.patch.object(adapter._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="Invalid JSON"):
                adapter._get_json("/printer/info")

    def test_post_delegates_to_request(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"ok": True})
        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            adapter._post("/printer/print/cancel")
        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "POST"

    def test_retryable_succeeds_on_second_attempt(self) -> None:
        adapter = _adapter(retries=3)
        fail_resp = _mock_response(status_code=502, ok=False, text="Bad Gateway")
        ok_resp = _mock_response(json_data={"result": "ok"})
        with mock.patch.object(
            adapter._session, "request", side_effect=[fail_resp, ok_resp]
        ):
            with mock.patch("kiln.printers.moonraker.time.sleep"):
                result = adapter._get_json("/printer/info")
        assert result == {"result": "ok"}

    def test_non_transient_request_exception_raises_immediately(self) -> None:
        adapter = _adapter(retries=3)
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=requests.exceptions.InvalidURL("bad url"),
        ):
            with pytest.raises(PrinterError, match="Request error"):
                adapter._get_json("/printer/info")


# ---------------------------------------------------------------------------
# get_state tests
# ---------------------------------------------------------------------------

class TestGetState:

    def _printer_info_response(self, state: str = "ready") -> Dict[str, Any]:
        return {"result": {"state": state, "state_message": ""}}

    def _objects_response(
        self,
        extruder_temp: float = 25.0,
        extruder_target: float = 0.0,
        bed_temp: float = 22.0,
        bed_target: float = 0.0,
        print_state: str = "standby",
    ) -> Dict[str, Any]:
        return {
            "result": {
                "status": {
                    "extruder": {
                        "temperature": extruder_temp,
                        "target": extruder_target,
                    },
                    "heater_bed": {
                        "temperature": bed_temp,
                        "target": bed_target,
                    },
                    "print_stats": {
                        "state": print_state,
                        "filename": "",
                        "print_duration": 0,
                        "total_duration": 0,
                    },
                }
            }
        }

    def test_idle_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("ready"))
        obj_resp = _mock_response(json_data=self._objects_response(print_state="standby"))

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp, obj_resp]
        ):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual == 25.0
        assert state.tool_temp_target == 0.0
        assert state.bed_temp_actual == 22.0
        assert state.bed_temp_target == 0.0

    def test_printing_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("ready"))
        obj_resp = _mock_response(
            json_data=self._objects_response(
                extruder_temp=205.0,
                extruder_target=210.0,
                bed_temp=60.0,
                bed_target=60.0,
                print_state="printing",
            )
        )

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp, obj_resp]
        ):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_actual == 205.0
        assert state.bed_temp_target == 60.0

    def test_paused_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("ready"))
        obj_resp = _mock_response(json_data=self._objects_response(print_state="paused"))

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp, obj_resp]
        ):
            state = adapter.get_state()

        assert state.state == PrinterStatus.PAUSED

    def test_connection_error_returns_offline(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=ReqConnectionError("refused"),
        ):
            state = adapter.get_state()

        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    def test_timeout_returns_offline(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=Timeout("timed out"),
        ):
            state = adapter.get_state()

        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    def test_klippy_not_ready_returns_early(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("startup"))

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp]
        ) as mock_req:
            state = adapter.get_state()

        # Only one request should have been made (no objects query).
        assert mock_req.call_count == 1
        assert state.connected is True
        assert state.state == PrinterStatus.BUSY

    def test_shutdown_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("shutdown"))

        with mock.patch.object(adapter._session, "request", side_effect=[info_resp]):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.OFFLINE

    def test_error_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("error"))

        with mock.patch.object(adapter._session, "request", side_effect=[info_resp]):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.ERROR

    def test_objects_query_failure_still_returns_state(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data=self._printer_info_response("ready"))
        obj_resp = _mock_response(status_code=500, text="Internal Error", ok=False)

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp, obj_resp]
        ):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual is None

    def test_non_connection_error_re_raises(self) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=401, text="Unauthorized", ok=False)
        with mock.patch.object(adapter._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="HTTP 401"):
                adapter.get_state()

    def test_non_string_klippy_state_handled(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data={"result": {"state": 12345}})

        with mock.patch.object(adapter._session, "request", side_effect=[info_resp]):
            state = adapter.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.UNKNOWN


# ---------------------------------------------------------------------------
# get_job tests
# ---------------------------------------------------------------------------

class TestGetJob:

    def _job_response(
        self,
        filename: str = "test.gcode",
        progress: float = 0.5,
        print_duration: float = 600.0,
        total_duration: float = 620.0,
    ) -> Dict[str, Any]:
        return {
            "result": {
                "status": {
                    "print_stats": {
                        "filename": filename,
                        "print_duration": print_duration,
                        "total_duration": total_duration,
                        "state": "printing",
                    },
                    "virtual_sdcard": {
                        "progress": progress,
                        "is_active": True,
                        "file_position": 1000,
                    },
                }
            }
        }

    def test_active_job(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data=self._job_response())

        with mock.patch.object(adapter._session, "request", return_value=resp):
            job = adapter.get_job()

        assert isinstance(job, JobProgress)
        assert job.file_name == "test.gcode"
        assert job.completion == 50.0
        assert job.print_time_seconds == 600
        # time_left = (600 / 0.5) - 600 = 600
        assert job.print_time_left_seconds == 600

    def test_no_active_job(self) -> None:
        adapter = _adapter()
        resp = _mock_response(
            json_data={
                "result": {
                    "status": {
                        "print_stats": {
                            "filename": "",
                            "print_duration": 0,
                            "total_duration": 0,
                            "state": "standby",
                        },
                        "virtual_sdcard": {
                            "progress": 0.0,
                            "is_active": False,
                        },
                    }
                }
            }
        )

        with mock.patch.object(adapter._session, "request", return_value=resp):
            job = adapter.get_job()

        assert job.file_name is None
        assert job.completion == 0.0

    def test_completed_job(self) -> None:
        adapter = _adapter()
        resp = _mock_response(
            json_data=self._job_response(progress=1.0, print_duration=3600.0)
        )

        with mock.patch.object(adapter._session, "request", return_value=resp):
            job = adapter.get_job()

        assert job.completion == 100.0
        assert job.print_time_seconds == 3600
        assert job.print_time_left_seconds == 0

    def test_progress_at_zero_no_time_left(self) -> None:
        adapter = _adapter()
        resp = _mock_response(
            json_data=self._job_response(progress=0.0, print_duration=0.0)
        )

        with mock.patch.object(adapter._session, "request", return_value=resp):
            job = adapter.get_job()

        assert job.completion == 0.0
        assert job.print_time_left_seconds is None

    def test_connection_error_raises(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=ReqConnectionError("down"),
        ):
            with pytest.raises(PrinterError):
                adapter.get_job()


# ---------------------------------------------------------------------------
# list_files tests
# ---------------------------------------------------------------------------

class TestListFiles:

    def _files_response(self) -> Dict[str, Any]:
        return {
            "result": [
                {
                    "path": "test_print.gcode",
                    "modified": 1700000000.0,
                    "size": 12345,
                    "permissions": "rw",
                },
                {
                    "path": "subdir/nested.gcode",
                    "modified": 1700001000.0,
                    "size": 67890,
                    "permissions": "rw",
                },
            ]
        }

    def test_lists_files(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data=self._files_response())

        with mock.patch.object(adapter._session, "request", return_value=resp):
            files = adapter.list_files()

        assert len(files) == 2
        assert all(isinstance(f, PrinterFile) for f in files)

        assert files[0].name == "test_print.gcode"
        assert files[0].path == "test_print.gcode"
        assert files[0].size_bytes == 12345
        assert files[0].date == 1700000000

        assert files[1].name == "nested.gcode"
        assert files[1].path == "subdir/nested.gcode"

    def test_empty_file_list(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": []})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            files = adapter.list_files()

        assert files == []

    def test_non_list_result_handled(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "not-a-list"})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            files = adapter.list_files()

        assert files == []

    def test_non_dict_entries_skipped(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": ["not-a-dict", 123]})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            files = adapter.list_files()

        assert files == []

    def test_file_without_modified_field(self) -> None:
        adapter = _adapter()
        resp = _mock_response(
            json_data={
                "result": [
                    {"path": "nodate.gcode", "size": 100},
                ]
            }
        )

        with mock.patch.object(adapter._session, "request", return_value=resp):
            files = adapter.list_files()

        assert len(files) == 1
        assert files[0].date is None

    def test_passes_root_gcodes_param(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": []})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            adapter.list_files()

        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"root": "gcodes"}


# ---------------------------------------------------------------------------
# upload_file tests
# ---------------------------------------------------------------------------

class TestUploadFile:

    def test_successful_upload(self, tmp_path: Any) -> None:
        gcode_file = tmp_path / "test.gcode"
        gcode_file.write_text("G28\nG1 X10 Y10 Z10\n")

        adapter = _adapter()
        resp = _mock_response(
            json_data={
                "result": {
                    "item": {"path": "test.gcode", "root": "gcodes"},
                    "action": "create_file",
                }
            }
        )

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.upload_file(str(gcode_file))

        assert isinstance(result, UploadResult)
        assert result.success is True
        assert result.file_name == "test.gcode"
        assert "Uploaded" in result.message

    def test_file_not_found_raises(self) -> None:
        adapter = _adapter()
        with pytest.raises(FileNotFoundError, match="Local file not found"):
            adapter.upload_file("/nonexistent/path/file.gcode")

    def test_permission_error_raises_printer_error(self, tmp_path: Any) -> None:
        gcode_file = tmp_path / "locked.gcode"
        gcode_file.write_text("G28\n")

        adapter = _adapter()
        with mock.patch("builtins.open", side_effect=PermissionError("no read")):
            with pytest.raises(PrinterError, match="Permission denied"):
                adapter.upload_file(str(gcode_file))

    def test_upload_with_no_json_response(self, tmp_path: Any) -> None:
        gcode_file = tmp_path / "test.gcode"
        gcode_file.write_text("G28\n")

        adapter = _adapter()
        resp = _mock_response(status_code=200)
        resp.json.side_effect = ValueError("no json")

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.upload_file(str(gcode_file))

        assert result.success is True
        assert result.file_name == "test.gcode"


# ---------------------------------------------------------------------------
# start_print tests
# ---------------------------------------------------------------------------

class TestStartPrint:

    def test_start_print(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": {}})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            result = adapter.start_print("test.gcode")

        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "test.gcode" in result.message

        # Verify the filename was passed as a query param.
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"filename": "test.gcode"}

    def test_start_print_failure(self) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=400, text="No file", ok=False)

        with mock.patch.object(adapter._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="HTTP 400"):
                adapter.start_print("missing.gcode")


# ---------------------------------------------------------------------------
# cancel_print tests
# ---------------------------------------------------------------------------

class TestCancelPrint:

    def test_cancel_print(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": {}})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.cancel_print()

        assert result.success is True
        assert "cancelled" in result.message.lower()

    def test_cancel_when_not_printing(self) -> None:
        adapter = _adapter()
        resp = _mock_response(status_code=400, text="Not printing", ok=False)

        with mock.patch.object(adapter._session, "request", return_value=resp):
            with pytest.raises(PrinterError):
                adapter.cancel_print()


# ---------------------------------------------------------------------------
# pause_print tests
# ---------------------------------------------------------------------------

class TestPausePrint:

    def test_pause_print(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": {}})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.pause_print()

        assert result.success is True
        assert "paused" in result.message.lower()


# ---------------------------------------------------------------------------
# resume_print tests
# ---------------------------------------------------------------------------

class TestResumePrint:

    def test_resume_print(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": {}})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.resume_print()

        assert result.success is True
        assert "resumed" in result.message.lower()


# ---------------------------------------------------------------------------
# set_tool_temp tests
# ---------------------------------------------------------------------------

class TestSetToolTemp:

    def test_set_tool_temp(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            ok = adapter.set_tool_temp(210.0)

        assert ok is True
        # Verify M104 G-code was sent.
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "M104 S210"}

    def test_turn_off_tool(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            ok = adapter.set_tool_temp(0)

        assert ok is True
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "M104 S0"}

    def test_set_tool_temp_error(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session,
            "request",
            side_effect=ReqConnectionError("down"),
        ):
            with pytest.raises(PrinterError):
                adapter.set_tool_temp(200)


# ---------------------------------------------------------------------------
# set_bed_temp tests
# ---------------------------------------------------------------------------

class TestSetBedTemp:

    def test_set_bed_temp(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            ok = adapter.set_bed_temp(60.0)

        assert ok is True
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "M140 S60"}

    def test_turn_off_bed(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            ok = adapter.set_bed_temp(0)

        assert ok is True
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "M140 S0"}


# ---------------------------------------------------------------------------
# _send_gcode tests
# ---------------------------------------------------------------------------

class TestSendGcode:

    def test_single_command(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            adapter._send_gcode("G28")

        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "G28"}

    def test_multi_line_script(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp) as mock_req:
            adapter._send_gcode("G28\nG1 X10 Y10 Z10 F300")

        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("params") == {"script": "G28\nG1 X10 Y10 Z10 F300"}


# ---------------------------------------------------------------------------
# PrinterState serialisation
# ---------------------------------------------------------------------------

class TestPrinterStateSerialization:

    def test_to_dict_roundtrip(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(
            json_data={"result": {"state": "ready", "state_message": ""}}
        )
        obj_resp = _mock_response(
            json_data={
                "result": {
                    "status": {
                        "extruder": {"temperature": 200.0, "target": 210.0},
                        "heater_bed": {"temperature": 58.5, "target": 60.0},
                        "print_stats": {"state": "printing"},
                    }
                }
            }
        )

        with mock.patch.object(
            adapter._session, "request", side_effect=[info_resp, obj_resp]
        ):
            state = adapter.get_state()

        d = state.to_dict()
        assert d["connected"] is True
        assert d["state"] == "printing"
        assert d["tool_temp_actual"] == 200.0
        assert d["bed_temp_target"] == 60.0


# ---------------------------------------------------------------------------
# Integration-style: verify the adapter is a valid PrinterAdapter
# ---------------------------------------------------------------------------

class TestAdapterInterface:
    """Verify that MoonrakerAdapter correctly implements the abstract interface."""

    def test_is_printer_adapter_subclass(self) -> None:
        from kiln.printers.base import PrinterAdapter

        assert issubclass(MoonrakerAdapter, PrinterAdapter)

    def test_instance_check(self) -> None:
        from kiln.printers.base import PrinterAdapter

        adapter = _adapter()
        assert isinstance(adapter, PrinterAdapter)

    def test_importable_from_package(self) -> None:
        from kiln.printers import MoonrakerAdapter as Imported

        assert Imported is MoonrakerAdapter
