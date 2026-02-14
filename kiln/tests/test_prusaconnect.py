"""Tests for kiln.printers.prusaconnect — Prusa Link adapter."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from kiln.printers.prusaconnect import (
    PrusaConnectAdapter,
    _safe_get,
)
from kiln.printers.base import (
    PrinterError,
    PrinterStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    content: bytes = b"",
    ok: bool = True,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.content = content
    resp.text = json.dumps(json_data) if json_data else ""
    resp.json.return_value = json_data or {}
    return resp


def _adapter(**kwargs) -> PrusaConnectAdapter:
    """Create an adapter with sensible defaults."""
    defaults = {"host": "http://prusa.local", "api_key": "test-key", "retries": 1}
    defaults.update(kwargs)
    return PrusaConnectAdapter(**defaults)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_empty_host_raises(self):
        with pytest.raises(ValueError, match="host must not be empty"):
            PrusaConnectAdapter(host="")

    def test_strips_trailing_slash(self):
        a = PrusaConnectAdapter(host="http://prusa.local/")
        assert a._host == "http://prusa.local"

    def test_api_key_set_in_session(self):
        a = PrusaConnectAdapter(host="http://prusa.local", api_key="abc123")
        assert a._session.headers.get("X-Api-Key") == "abc123"

    def test_no_api_key(self):
        a = PrusaConnectAdapter(host="http://prusa.local")
        assert "X-Api-Key" not in a._session.headers


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _adapter().name == "prusaconnect"

    def test_capabilities(self):
        caps = _adapter().capabilities
        assert caps.can_upload is True
        assert caps.can_set_temp is False
        assert caps.can_send_gcode is False
        assert caps.can_pause is True
        assert ".bgcode" in caps.supported_extensions


# ---------------------------------------------------------------------------
# request errors
# ---------------------------------------------------------------------------


class TestRequestErrors:
    def test_403_file_endpoint_includes_path_hint(self):
        a = _adapter()
        forbidden = _mock_response(status_code=403, ok=False)
        forbidden.text = "Forbidden"

        with patch.object(a._session, "request", return_value=forbidden):
            with pytest.raises(PrinterError) as exc_info:
                a._request("POST", "/api/v1/files/usb/WHISTL~1.GCO")

        message = str(exc_info.value)
        assert "HTTP 403" in message
        assert "/api/v1/files/usb/WHISTL~1.GCO" in message
        assert "8.3" in message


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


class TestGetState:
    def test_idle_state(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {
                "state": "IDLE",
                "temp_nozzle": 25.0,
                "target_nozzle": 0.0,
                "temp_bed": 22.0,
                "target_bed": 0.0,
            },
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            state = a.get_state()

        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual == 25.0
        assert state.bed_temp_actual == 22.0

    def test_printing_state(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "PRINTING", "temp_nozzle": 215.0, "target_nozzle": 215.0,
                        "temp_bed": 60.0, "target_bed": 60.0},
            "job": {"id": 42, "progress": 45.2, "time_printing": 1234, "time_remaining": 1800},
        })
        with patch.object(a._session, "request", return_value=resp):
            state = a.get_state()

        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_target == 215.0

    def test_offline_on_connection_error(self):
        a = _adapter()
        from requests.exceptions import ConnectionError as CE
        with patch.object(a._session, "request", side_effect=CE("refused")):
            state = a.get_state()

        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    def test_error_state(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "ERROR"},
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            state = a.get_state()

        assert state.state == PrinterStatus.ERROR

    def test_attention_maps_to_error(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "ATTENTION"},
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            state = a.get_state()

        assert state.state == PrinterStatus.ERROR

    def test_unknown_state(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "SOMETHING_NEW"},
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            state = a.get_state()

        assert state.state == PrinterStatus.UNKNOWN


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_active_job(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "PRINTING"},
            "job": {"id": 42, "progress": 45.2, "time_printing": 1234, "time_remaining": 1800},
        })
        with patch.object(a._session, "request", return_value=resp):
            job = a.get_job()

        assert job.completion == 45.2
        assert job.print_time_seconds == 1234
        assert job.print_time_left_seconds == 1800

    def test_no_job(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "IDLE"},
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            job = a.get_job()

        assert job.completion is None


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_list_files(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "children": [
                {"display_name": "benchy.gcode", "type": "PRINT_FILE", "size": 123456,
                 "m_timestamp": 1700000000},
                {"display_name": "test.gcode", "type": "PRINT_FILE", "size": 789},
            ],
        })
        with patch.object(a._session, "request", return_value=resp):
            files = a.list_files()

        assert len(files) == 2
        assert files[0].name == "benchy.gcode"
        assert files[0].size_bytes == 123456
        assert files[1].name == "test.gcode"
        assert files[0].path == "benchy.gcode"

    def test_list_files_with_folders(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "children": [
                {"display_name": "models", "type": "FOLDER", "children": [
                    {"display_name": "inner.gcode", "type": "PRINT_FILE", "size": 100},
                ]},
                {"display_name": "top.gcode", "type": "PRINT_FILE", "size": 200},
            ],
        })
        with patch.object(a._session, "request", return_value=resp):
            files = a.list_files()

        assert len(files) == 2
        paths = {f.path for f in files}
        assert "models/inner.gcode" in paths
        assert "top.gcode" in paths

    def test_list_files_maps_display_name_to_api_path(self):
        a = _adapter()
        usb_resp = _mock_response(json_data={
            "children": [
                {
                    "display_name": "Whistle_200um_MINI_PLA_32m.gcode",
                    "name": "WHISTL~1.GCO",
                    "type": "PRINT_FILE",
                    "size": 12345,
                },
            ],
        })
        local_404 = _mock_response(status_code=404, ok=False)
        local_404.text = "Not Found"

        with patch.object(a._session, "request", side_effect=[usb_resp, local_404]):
            files = a.list_files()

        assert len(files) == 1
        assert files[0].name == "Whistle_200um_MINI_PLA_32m.gcode"
        assert files[0].path == "WHISTL~1.GCO"

    def test_list_files_falls_back_to_local_when_usb_unavailable(self):
        a = _adapter()
        usb_404 = _mock_response(status_code=404, ok=False)
        usb_404.text = "Not Found"
        local_resp = _mock_response(json_data={
            "children": [
                {"display_name": "test.gcode", "name": "TEST~1.GCO", "type": "PRINT_FILE", "size": 789},
            ],
        })

        with patch.object(a._session, "request", side_effect=[usb_404, local_resp]) as mock_request:
            files = a.list_files()

        assert len(files) == 1
        assert files[0].path == "TEST~1.GCO"
        assert "/api/v1/files/usb" in mock_request.call_args_list[0].args[1]
        assert "/api/v1/files/local" in mock_request.call_args_list[1].args[1]

    def test_list_files_raises_when_no_supported_storage_endpoint(self):
        a = _adapter()
        usb_404 = _mock_response(status_code=404, ok=False)
        usb_404.text = "Not Found"
        local_403 = _mock_response(status_code=403, ok=False)
        local_403.text = "Forbidden"

        with patch.object(a._session, "request", side_effect=[usb_404, local_403]):
            with pytest.raises(PrinterError, match="Unable to list files from Prusa Link storage roots"):
                a.list_files()

    def test_list_files_empty(self):
        a = _adapter()
        usb_resp = _mock_response(json_data={"children": []})
        local_404 = _mock_response(status_code=404, ok=False)
        local_404.text = "Not Found"
        with patch.object(a._session, "request", side_effect=[usb_resp, local_404]):
            files = a.list_files()

        assert files == []


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


class TestUploadFile:
    def test_upload_success(self, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("; test gcode")

        a = _adapter()
        resp = _mock_response(status_code=201)
        with patch.object(a._session, "request", return_value=resp):
            result = a.upload_file(str(gcode))

        assert result.success is True
        assert result.file_name == "model.gcode"

    def test_upload_falls_back_to_local_when_usb_forbidden(self, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("; test gcode")

        a = _adapter()
        usb_403 = _mock_response(status_code=403, ok=False)
        usb_403.text = "Forbidden"
        local_201 = _mock_response(status_code=201)
        with patch.object(a._session, "request", side_effect=[usb_403, local_201]) as mock_request:
            result = a.upload_file(str(gcode))

        assert result.success is True
        assert "/api/v1/files/usb/model.gcode" in mock_request.call_args_list[0].args[1]
        assert "/api/v1/files/local/model.gcode" in mock_request.call_args_list[1].args[1]

    def test_upload_file_not_found(self):
        a = _adapter()
        with pytest.raises(FileNotFoundError, match="not found"):
            a.upload_file("/nonexistent/model.gcode")


# ---------------------------------------------------------------------------
# Print control
# ---------------------------------------------------------------------------


class TestPrintControl:
    def test_start_print(self):
        a = _adapter()
        usb_list = _mock_response(json_data={
            "children": [
                {"display_name": "benchy.gcode", "name": "BENCHY.GCO", "type": "PRINT_FILE", "size": 123},
            ],
        })
        local_404 = _mock_response(status_code=404, ok=False)
        local_404.text = "Not Found"
        start_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[usb_list, local_404, start_resp]) as mock_request:
            result = a.start_print("benchy.gcode")

        assert result.success is True
        assert "/api/v1/files/usb/BENCHY.GCO" in mock_request.call_args_list[2].args[1]

    def test_start_print_resolves_display_name_to_short_name(self):
        a = _adapter()
        usb_list = _mock_response(json_data={
            "children": [
                {
                    "display_name": "Whistle_200um_MINI_PLA_32m.gcode",
                    "name": "WHISTL~1.GCO",
                    "type": "PRINT_FILE",
                    "size": 12345,
                },
            ],
        })
        local_404 = _mock_response(status_code=404, ok=False)
        local_404.text = "Not Found"
        start_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[usb_list, local_404, start_resp]) as mock_request:
            result = a.start_print("Whistle_200um_MINI_PLA_32m.gcode")

        assert result.success is True
        assert "/api/v1/files/usb/WHISTL~1.GCO" in mock_request.call_args_list[2].args[1]

    def test_start_print_falls_back_to_local_when_usb_forbidden(self):
        a = _adapter()
        usb_404 = _mock_response(status_code=404, ok=False)
        usb_404.text = "Not Found"
        local_list = _mock_response(json_data={
            "children": [
                {"display_name": "benchy.gcode", "name": "BENCHY.GCO", "type": "PRINT_FILE", "size": 100},
            ],
        })
        usb_403 = _mock_response(status_code=403, ok=False)
        usb_403.text = "Forbidden"
        local_start = _mock_response(status_code=204)

        with patch.object(
            a._session,
            "request",
            side_effect=[usb_404, local_list, usb_403, local_start],
        ) as mock_request:
            result = a.start_print("benchy.gcode")

        assert result.success is True
        assert "/api/v1/files/usb/BENCHY.GCO" in mock_request.call_args_list[2].args[1]
        assert "/api/v1/files/local/BENCHY.GCO" in mock_request.call_args_list[3].args[1]

    def test_cancel_print(self):
        a = _adapter()
        status_resp = _mock_response(json_data={
            "printer": {"state": "PRINTING"},
            "job": {"id": 42, "progress": 50.0},
        })
        cancel_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[status_resp, cancel_resp]):
            result = a.cancel_print()

        assert result.success is True

    def test_cancel_no_job_raises(self):
        a = _adapter()
        resp = _mock_response(json_data={
            "printer": {"state": "IDLE"},
            "job": {},
        })
        with patch.object(a._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="No active job"):
                a.cancel_print()

    def test_pause_print(self):
        a = _adapter()
        status_resp = _mock_response(json_data={
            "printer": {"state": "PRINTING"},
            "job": {"id": 42, "progress": 30.0},
        })
        pause_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[status_resp, pause_resp]):
            result = a.pause_print()

        assert result.success is True

    def test_resume_print(self):
        a = _adapter()
        status_resp = _mock_response(json_data={
            "printer": {"state": "PAUSED"},
            "job": {"id": 42, "progress": 30.0},
        })
        resume_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[status_resp, resume_resp]):
            result = a.resume_print()

        assert result.success is True

    def test_emergency_stop_delegates_to_cancel(self):
        a = _adapter()
        status_resp = _mock_response(json_data={
            "printer": {"state": "PRINTING"},
            "job": {"id": 42, "progress": 50.0},
        })
        cancel_resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", side_effect=[status_resp, cancel_resp]):
            result = a.emergency_stop()

        assert result.success is True


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupportedOps:
    def test_set_tool_temp_raises(self):
        a = _adapter()
        with pytest.raises(PrinterError, match="does not support direct temperature"):
            a.set_tool_temp(200)

    def test_set_bed_temp_raises(self):
        a = _adapter()
        with pytest.raises(PrinterError, match="does not support direct temperature"):
            a.set_bed_temp(60)

    def test_send_gcode_raises(self):
        a = _adapter()
        with pytest.raises(PrinterError, match="does not support sending raw G-code"):
            a.send_gcode(["G28"])


# ---------------------------------------------------------------------------
# File deletion
# ---------------------------------------------------------------------------


class TestDeleteFile:
    def test_delete_file(self):
        a = _adapter()
        resp = _mock_response(status_code=204)
        with patch.object(a._session, "request", return_value=resp):
            result = a.delete_file("old_model.gcode")

        assert result is True


# ---------------------------------------------------------------------------
# Webcam snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_success(self):
        a = _adapter()
        resp = _mock_response(content=b"\x89PNG\r\n\x1a\n")
        with patch.object(a._session, "request", return_value=resp):
            data = a.get_snapshot()

        assert data == b"\x89PNG\r\n\x1a\n"

    def test_snapshot_no_camera(self):
        a = _adapter()
        resp = _mock_response(status_code=204, ok=False)
        with patch.object(a._session, "request", side_effect=Exception("no camera")):
            data = a.get_snapshot()

        assert data is None


# ---------------------------------------------------------------------------
# HTTP retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    def test_retries_on_502(self):
        a = PrusaConnectAdapter(host="http://prusa.local", retries=2)
        bad_resp = _mock_response(status_code=502, ok=False)
        good_resp = _mock_response(json_data={"printer": {"state": "IDLE"}, "job": {}})

        with patch.object(a._session, "request", side_effect=[bad_resp, good_resp]):
            with patch("kiln.printers.prusaconnect.time.sleep"):
                state = a.get_state()

        assert state.state == PrinterStatus.IDLE

    def test_raises_on_404(self):
        a = _adapter()
        resp = _mock_response(status_code=404, ok=False)
        resp.text = "Not Found"
        with patch.object(a._session, "request", return_value=resp):
            with pytest.raises(PrinterError, match="HTTP 404"):
                a._get_json("/api/v1/nonexistent")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_safe_get(self):
        data = {"a": {"b": {"c": 42}}}
        assert _safe_get(data, "a", "b", "c") == 42
        assert _safe_get(data, "a", "x", default="miss") == "miss"
        assert _safe_get(None, "a", default="none") == "none"

    def test_repr(self):
        a = _adapter()
        assert "PrusaConnectAdapter" in repr(a)
        assert "prusa.local" in repr(a)
