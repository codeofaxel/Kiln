"""Tests for kiln.printers.octoprint -- OctoPrintAdapter with mocked HTTP.

Uses the ``responses`` library to intercept all outgoing HTTP requests so
that tests are fast, deterministic, and do not require a real OctoPrint
instance.

Covers:
- Constructor validation
- get_state() with every flag combination (idle, printing, paused, error,
  cancelling, offline via connection error)
- get_job() parsing
- list_files() with flat and nested/folder responses
- upload_file() success and FileNotFoundError
- start_print(), cancel_print(), pause_print(), resume_print()
- set_tool_temp(), set_bed_temp()
- Retry logic on HTTP 502/503/504
- Immediate failure on HTTP 401
- Connection error maps to OFFLINE state
- Helper functions (_safe_get, _map_flags_to_status, _flatten_files)
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest
import responses
from requests.exceptions import ConnectionError as ReqConnectionError

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
from kiln.printers.octoprint import (
    OctoPrintAdapter,
    _flatten_files,
    _map_flags_to_status,
    _safe_get,
)

OCTOPRINT_HOST = "http://octopi.local"
OCTOPRINT_API_KEY = "TESTAPIKEY123"


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------

class TestOctoPrintAdapterConstructor:
    """Tests for OctoPrintAdapter.__init__."""

    def test_valid_construction(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        assert a.name == "octoprint"
        assert a._host == OCTOPRINT_HOST
        assert a._api_key == OCTOPRINT_API_KEY

    def test_host_trailing_slash_stripped(self):
        a = OctoPrintAdapter(host="http://octopi.local/", api_key=OCTOPRINT_API_KEY)
        assert a._host == "http://octopi.local"

    def test_empty_host_raises(self):
        with pytest.raises(ValueError, match="host must not be empty"):
            OctoPrintAdapter(host="", api_key=OCTOPRINT_API_KEY)

    def test_empty_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key must not be empty"):
            OctoPrintAdapter(host=OCTOPRINT_HOST, api_key="")

    def test_retries_minimum_is_one(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, retries=0)
        assert a._retries == 1

    def test_default_timeout_and_retries(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        assert a._timeout == 30
        assert a._retries == 3

    def test_custom_timeout_and_retries(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY, timeout=10, retries=5)
        assert a._timeout == 10
        assert a._retries == 5

    def test_session_has_api_key_header(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        assert a._session.headers.get("X-Api-Key") == OCTOPRINT_API_KEY

    def test_capabilities(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        caps = a.capabilities
        assert isinstance(caps, PrinterCapabilities)
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True

    def test_repr(self):
        a = OctoPrintAdapter(host=OCTOPRINT_HOST, api_key=OCTOPRINT_API_KEY)
        assert "OctoPrintAdapter" in repr(a)
        assert OCTOPRINT_HOST in repr(a)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestSafeGet:
    """Tests for the _safe_get helper."""

    def test_single_key(self):
        assert _safe_get({"a": 1}, "a") == 1

    def test_nested_keys(self):
        data = {"a": {"b": {"c": 42}}}
        assert _safe_get(data, "a", "b", "c") == 42

    def test_missing_key_returns_default(self):
        assert _safe_get({"a": 1}, "b") is None
        assert _safe_get({"a": 1}, "b", default="fallback") == "fallback"

    def test_non_dict_intermediate(self):
        assert _safe_get({"a": "not_a_dict"}, "a", "b") is None

    def test_none_data(self):
        assert _safe_get(None, "a") is None

    def test_empty_data(self):
        assert _safe_get({}, "a") is None

    def test_no_keys(self):
        data = {"a": 1}
        assert _safe_get(data) == data


class TestMapFlagsToStatus:
    """Tests for the _map_flags_to_status helper."""

    def test_cancelling(self):
        assert _map_flags_to_status({"cancelling": True, "printing": True}) == PrinterStatus.CANCELLING

    def test_printing(self):
        assert _map_flags_to_status({"printing": True, "operational": True}) == PrinterStatus.PRINTING

    def test_paused(self):
        assert _map_flags_to_status({"paused": True, "operational": True}) == PrinterStatus.PAUSED

    def test_pausing(self):
        assert _map_flags_to_status({"pausing": True, "operational": True}) == PrinterStatus.PAUSED

    def test_error(self):
        assert _map_flags_to_status({"error": True}) == PrinterStatus.ERROR

    def test_closed_or_error(self):
        assert _map_flags_to_status({"closedOrError": True}) == PrinterStatus.ERROR

    def test_idle(self):
        assert _map_flags_to_status({"ready": True, "operational": True}) == PrinterStatus.IDLE

    def test_busy_operational_not_ready(self):
        assert _map_flags_to_status({"operational": True, "ready": False}) == PrinterStatus.BUSY

    def test_unknown_empty_flags(self):
        assert _map_flags_to_status({}) == PrinterStatus.UNKNOWN

    def test_all_false(self):
        flags = {
            "operational": False, "paused": False, "printing": False,
            "cancelling": False, "pausing": False, "error": False,
            "ready": False, "closedOrError": False,
        }
        assert _map_flags_to_status(flags) == PrinterStatus.UNKNOWN


class TestFlattenFiles:
    """Tests for the _flatten_files helper."""

    def test_empty_list(self):
        assert _flatten_files([]) == []

    def test_flat_files_only(self):
        entries = [
            {"name": "a.gcode", "type": "machinecode"},
            {"name": "b.gcode", "type": "machinecode"},
        ]
        result = _flatten_files(entries)
        assert len(result) == 2

    def test_nested_folders(self):
        entries = [
            {"name": "top.gcode", "type": "machinecode"},
            {
                "name": "folder",
                "type": "folder",
                "children": [
                    {"name": "child.gcode", "type": "machinecode"},
                ],
            },
        ]
        result = _flatten_files(entries)
        assert len(result) == 2
        names = [e["name"] for e in result]
        assert "top.gcode" in names
        assert "child.gcode" in names

    def test_deeply_nested(self):
        entries = [
            {
                "name": "l1",
                "type": "folder",
                "children": [
                    {
                        "name": "l2",
                        "type": "folder",
                        "children": [
                            {"name": "deep.gcode", "type": "machinecode"},
                        ],
                    },
                ],
            },
        ]
        result = _flatten_files(entries)
        assert len(result) == 1
        assert result[0]["name"] == "deep.gcode"

    def test_folder_with_no_children(self):
        entries = [{"name": "empty_folder", "type": "folder"}]
        result = _flatten_files(entries)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# get_state() tests
# ---------------------------------------------------------------------------

class TestGetState:
    """Tests for OctoPrintAdapter.get_state()."""

    @responses.activate
    def test_idle_state(self, adapter, printer_state_idle):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_idle,
            status=200,
        )
        state = adapter.get_state()
        assert isinstance(state, PrinterState)
        assert state.connected is True
        assert state.state == PrinterStatus.IDLE
        assert state.tool_temp_actual == 24.5
        assert state.tool_temp_target == 0.0
        assert state.bed_temp_actual == 23.1
        assert state.bed_temp_target == 0.0

    @responses.activate
    def test_printing_state(self, adapter, printer_state_printing):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_printing,
            status=200,
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.PRINTING
        assert state.tool_temp_actual == 205.0
        assert state.bed_temp_target == 60.0

    @responses.activate
    def test_paused_state(self, adapter, printer_state_paused):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_paused,
            status=200,
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.PAUSED

    @responses.activate
    def test_error_state(self, adapter, printer_state_error):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_error,
            status=200,
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.ERROR

    @responses.activate
    def test_cancelling_state(self, adapter, printer_state_cancelling):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_cancelling,
            status=200,
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.CANCELLING

    @responses.activate
    def test_connection_error_returns_offline(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            body=ReqConnectionError("Connection refused"),
        )
        state = adapter.get_state()
        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE

    @responses.activate
    def test_missing_temperature_data(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={
                "state": {
                    "flags": {"operational": True, "ready": True},
                },
            },
            status=200,
        )
        state = adapter.get_state()
        assert state.connected is True
        assert state.tool_temp_actual is None
        assert state.bed_temp_actual is None

    @responses.activate
    def test_missing_flags_returns_unknown(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"temperature": {}, "state": {}},
            status=200,
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.UNKNOWN

    @responses.activate
    def test_auth_error_raises_immediately(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"error": "Unauthorized"},
            status=401,
        )
        with pytest.raises(PrinterError, match="401"):
            adapter.get_state()


# ---------------------------------------------------------------------------
# get_job() tests
# ---------------------------------------------------------------------------

class TestGetJob:
    """Tests for OctoPrintAdapter.get_job()."""

    @responses.activate
    def test_active_job(self, adapter, job_response_printing):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/job",
            json=job_response_printing,
            status=200,
        )
        job = adapter.get_job()
        assert isinstance(job, JobProgress)
        assert job.file_name == "benchy.gcode"
        assert job.completion == 45.68
        assert job.print_time_seconds == 1620
        assert job.print_time_left_seconds == 1980

    @responses.activate
    def test_idle_job(self, adapter, job_response_idle):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/job",
            json=job_response_idle,
            status=200,
        )
        job = adapter.get_job()
        assert job.file_name is None
        assert job.completion is None
        assert job.print_time_seconds is None
        assert job.print_time_left_seconds is None

    @responses.activate
    def test_missing_progress_keys(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/job",
            json={"job": {"file": {}}, "progress": {}},
            status=200,
        )
        job = adapter.get_job()
        assert job.file_name is None
        assert job.completion is None


# ---------------------------------------------------------------------------
# list_files() tests
# ---------------------------------------------------------------------------

class TestListFiles:
    """Tests for OctoPrintAdapter.list_files()."""

    @responses.activate
    def test_flat_files(self, adapter, files_response_flat):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/files/local",
            json=files_response_flat,
            status=200,
        )
        files = adapter.list_files()
        assert len(files) == 2
        assert all(isinstance(f, PrinterFile) for f in files)
        assert files[0].name == "benchy.gcode"
        assert files[1].name == "cube.gcode"
        assert files[0].size_bytes == 1234567

    @responses.activate
    def test_nested_files(self, adapter, files_response_nested):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/files/local",
            json=files_response_nested,
            status=200,
        )
        files = adapter.list_files()
        assert len(files) == 3
        names = [f.name for f in files]
        assert "benchy.gcode" in names
        assert "first_layer.gcode" in names
        assert "deep_file.gcode" in names

    @responses.activate
    def test_empty_file_list(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/files/local",
            json={"files": []},
            status=200,
        )
        files = adapter.list_files()
        assert files == []

    @responses.activate
    def test_invalid_files_key_type(self, adapter):
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/files/local",
            json={"files": "not_a_list"},
            status=200,
        )
        files = adapter.list_files()
        assert files == []


# ---------------------------------------------------------------------------
# upload_file() tests
# ---------------------------------------------------------------------------

class TestUploadFile:
    """Tests for OctoPrintAdapter.upload_file()."""

    @responses.activate
    def test_successful_upload(self, adapter, upload_response_success):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/files/local",
            json=upload_response_success,
            status=201,
        )
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\nG1 X10 Y10\n")
            tmp_path = f.name

        try:
            result = adapter.upload_file(tmp_path)
            assert isinstance(result, UploadResult)
            assert result.success is True
            assert result.file_name == "test_print.gcode"
            assert "Uploaded" in result.message
        finally:
            os.unlink(tmp_path)

    def test_file_not_found(self, adapter):
        with pytest.raises(FileNotFoundError, match="not found"):
            adapter.upload_file("/nonexistent/path/file.gcode")

    @responses.activate
    def test_upload_with_no_json_body(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/files/local",
            body="OK",
            status=201,
        )
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(b"G28\n")
            tmp_path = f.name

        try:
            result = adapter.upload_file(tmp_path)
            assert result.success is True
            # Falls back to local filename
            assert os.path.basename(tmp_path) in result.file_name
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Print control tests
# ---------------------------------------------------------------------------

class TestStartPrint:
    """Tests for OctoPrintAdapter.start_print()."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/files/local/benchy.gcode",
            status=204,
        )
        result = adapter.start_print("benchy.gcode")
        assert isinstance(result, PrintResult)
        assert result.success is True
        assert "benchy.gcode" in result.message

    @responses.activate
    def test_file_name_with_special_chars(self, adapter):
        encoded_name = "my%20file.gcode"
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/files/local/{encoded_name}",
            status=204,
        )
        result = adapter.start_print("my file.gcode")
        assert result.success is True

    @responses.activate
    def test_http_error(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/files/local/missing.gcode",
            json={"error": "not found"},
            status=404,
        )
        with pytest.raises(PrinterError, match="404"):
            adapter.start_print("missing.gcode")


class TestCancelPrint:
    """Tests for OctoPrintAdapter.cancel_print()."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/job",
            status=204,
        )
        result = adapter.cancel_print()
        assert result.success is True
        assert "cancelled" in result.message.lower()

    @responses.activate
    def test_conflict_error(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/job",
            json={"error": "No job running"},
            status=409,
        )
        with pytest.raises(PrinterError, match="409"):
            adapter.cancel_print()


class TestEmergencyStop:
    """Tests for OctoPrintAdapter.emergency_stop()."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/command",
            status=204,
        )
        result = adapter.emergency_stop()
        assert result.success is True
        assert "m112" in result.message.lower() or "emergency" in result.message.lower()

        body = json.loads(responses.calls[0].request.body)
        assert body["commands"] == ["M112"]

    @responses.activate
    def test_server_error(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/command",
            json={"error": "Internal error"},
            status=500,
        )
        with pytest.raises(PrinterError):
            adapter.emergency_stop()


class TestPausePrint:
    """Tests for OctoPrintAdapter.pause_print()."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/job",
            status=204,
        )
        result = adapter.pause_print()
        assert result.success is True
        assert "paused" in result.message.lower()


class TestResumePrint:
    """Tests for OctoPrintAdapter.resume_print()."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/job",
            status=204,
        )
        result = adapter.resume_print()
        assert result.success is True
        assert "resumed" in result.message.lower()


# ---------------------------------------------------------------------------
# Temperature control tests
# ---------------------------------------------------------------------------

class TestSetToolTemp:
    """Tests for OctoPrintAdapter.set_tool_temp()."""

    @responses.activate
    def test_set_temp(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/tool",
            status=204,
        )
        ok = adapter.set_tool_temp(210.0)
        assert ok is True

        body = json.loads(responses.calls[0].request.body)
        assert body["command"] == "target"
        assert body["targets"]["tool0"] == 210

    @responses.activate
    def test_turn_off(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/tool",
            status=204,
        )
        ok = adapter.set_tool_temp(0)
        assert ok is True

        body = json.loads(responses.calls[0].request.body)
        assert body["targets"]["tool0"] == 0


class TestSetBedTemp:
    """Tests for OctoPrintAdapter.set_bed_temp()."""

    @responses.activate
    def test_set_temp(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/bed",
            status=204,
        )
        ok = adapter.set_bed_temp(60.0)
        assert ok is True

        body = json.loads(responses.calls[0].request.body)
        assert body["command"] == "target"
        assert body["target"] == 60

    @responses.activate
    def test_turn_off(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/bed",
            status=204,
        )
        ok = adapter.set_bed_temp(0)
        assert ok is True

        body = json.loads(responses.calls[0].request.body)
        assert body["target"] == 0


# ---------------------------------------------------------------------------
# Retry logic tests
# ---------------------------------------------------------------------------

class TestRetryLogic:
    """Tests for HTTP retry behaviour on transient errors."""

    @responses.activate
    def test_retry_on_502(self, adapter_with_retries, printer_state_idle):
        """502 is retried and eventually succeeds."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"error": "Bad Gateway"},
            status=502,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_idle,
            status=200,
        )
        with patch("kiln.printers.octoprint.time.sleep"):
            state = adapter_with_retries.get_state()
        assert state.state == PrinterStatus.IDLE
        assert len(responses.calls) == 2

    @responses.activate
    def test_retry_on_503(self, adapter_with_retries, printer_state_idle):
        """503 is retried and eventually succeeds."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"error": "Service Unavailable"},
            status=503,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_idle,
            status=200,
        )
        with patch("kiln.printers.octoprint.time.sleep"):
            state = adapter_with_retries.get_state()
        assert state.state == PrinterStatus.IDLE

    @responses.activate
    def test_retry_on_504(self, adapter_with_retries, printer_state_idle):
        """504 is retried and eventually succeeds."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"error": "Gateway Timeout"},
            status=504,
        )
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json=printer_state_idle,
            status=200,
        )
        with patch("kiln.printers.octoprint.time.sleep"):
            state = adapter_with_retries.get_state()
        assert state.state == PrinterStatus.IDLE

    @responses.activate
    def test_all_retries_exhausted(self, adapter_with_retries):
        """If all retries return 502, the adapter raises PrinterError."""
        for _ in range(3):
            responses.add(
                responses.GET,
                f"{OCTOPRINT_HOST}/api/printer",
                json={"error": "Bad Gateway"},
                status=502,
            )
        with patch("kiln.printers.octoprint.time.sleep"):
            with pytest.raises(PrinterError, match="502"):
                adapter_with_retries.get_state()
        assert len(responses.calls) == 3

    @responses.activate
    def test_401_no_retry(self, adapter_with_retries):
        """401 is not retryable -- raises immediately on first attempt."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"error": "Unauthorized"},
            status=401,
        )
        with pytest.raises(PrinterError, match="401"):
            adapter_with_retries.get_state()
        assert len(responses.calls) == 1

    @responses.activate
    def test_connection_error_single_retry_adapter(self, adapter):
        """Connection error on adapter with retries=1 returns OFFLINE."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            body=ReqConnectionError("refused"),
        )
        state = adapter.get_state()
        assert state.state == PrinterStatus.OFFLINE

    @responses.activate
    def test_connection_error_with_multiple_retries(self, adapter_with_retries):
        """Connection errors exhaust retries then get_state returns OFFLINE."""
        for _ in range(3):
            responses.add(
                responses.GET,
                f"{OCTOPRINT_HOST}/api/printer",
                body=ReqConnectionError("refused"),
            )
        with patch("kiln.printers.octoprint.time.sleep"):
            state = adapter_with_retries.get_state()
        assert state.connected is False
        assert state.state == PrinterStatus.OFFLINE


# ---------------------------------------------------------------------------
# send_gcode tests
# ---------------------------------------------------------------------------

class TestOctoPrintSendGcode:
    """Tests for the public send_gcode interface."""

    @responses.activate
    def test_single_command(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/command",
            json={"ok": True},
            status=200,
        )
        ok = adapter.send_gcode(["G28"])
        assert ok is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"commands": ["G28"]}

    @responses.activate
    def test_multiple_commands(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/command",
            json={"ok": True},
            status=200,
        )
        ok = adapter.send_gcode(["G28", "M104 S200", "G1 X10 F300"])
        assert ok is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"commands": ["G28", "M104 S200", "G1 X10 F300"]}

    @responses.activate
    def test_error_raises(self, adapter):
        responses.add(
            responses.POST,
            f"{OCTOPRINT_HOST}/api/printer/command",
            json={"error": "Printer is not operational"},
            status=409,
        )
        with pytest.raises(PrinterError):
            adapter.send_gcode(["G28"])


# ---------------------------------------------------------------------------
# delete_file tests
# ---------------------------------------------------------------------------

class TestOctoPrintDeleteFile:
    """Tests for the delete_file method."""

    @responses.activate
    def test_success(self, adapter):
        responses.add(
            responses.DELETE,
            f"{OCTOPRINT_HOST}/api/files/local/benchy.gcode",
            status=204,
        )
        ok = adapter.delete_file("benchy.gcode")
        assert ok is True

    @responses.activate
    def test_not_found_raises(self, adapter):
        responses.add(
            responses.DELETE,
            f"{OCTOPRINT_HOST}/api/files/local/missing.gcode",
            json={"error": "File not found"},
            status=404,
        )
        with pytest.raises(PrinterError):
            adapter.delete_file("missing.gcode")


# ---------------------------------------------------------------------------
# _request method edge cases
# ---------------------------------------------------------------------------

class TestRequestMethod:
    """Additional edge case tests for _request."""

    @responses.activate
    def test_invalid_json_response(self, adapter):
        """_get_json raises PrinterError on invalid JSON."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/job",
            body="not json at all",
            status=200,
            content_type="text/plain",
        )
        with pytest.raises(PrinterError, match="Invalid JSON"):
            adapter.get_job()

    @responses.activate
    def test_url_construction(self, adapter):
        """_url correctly joins host and path."""
        responses.add(
            responses.GET,
            f"{OCTOPRINT_HOST}/api/printer",
            json={"state": {"flags": {}}, "temperature": {}},
            status=200,
        )
        adapter.get_state()
        assert responses.calls[0].request.url.startswith(OCTOPRINT_HOST)
