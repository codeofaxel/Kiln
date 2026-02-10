"""Tests for octoprint_cli.client.OctoPrintClient."""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch

import pytest
import responses
from requests.exceptions import ConnectionError, Timeout

from octoprint_cli.client import OctoPrintClient

from .conftest import TEST_API_KEY, TEST_HOST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url(path: str) -> str:
    return f"{TEST_HOST}{path}"


# ===================================================================
# get_printer_state
# ===================================================================


class TestGetPrinterState:
    """Tests for OctoPrintClient.get_printer_state()."""

    @responses.activate
    def test_success(
        self,
        client: OctoPrintClient,
        printer_state_operational: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json=printer_state_operational,
            status=200,
        )
        result = client.get_printer_state()
        assert result["success"] is True
        assert result["error"] is None
        assert result["data"]["state"]["text"] == "Operational"
        assert result["data"]["temperature"]["tool0"]["actual"] == 24.3

    @responses.activate
    def test_auth_error_403(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Invalid API key"},
            status=403,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
        assert result["error"]["http_status"] == 403

    @responses.activate
    def test_not_found_404(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Not found"},
            status=404,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert result["error"]["http_status"] == 404

    @responses.activate
    def test_conflict_409(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Printer is not connected"},
            status=409,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "CONFLICT"
        assert result["error"]["http_status"] == 409

    @responses.activate
    def test_server_error_500(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Internal server error"},
            status=500,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_ERROR"
        assert result["error"]["http_status"] == 500

    @responses.activate
    def test_unsupported_file_type_415(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Unsupported"},
            status=415,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
        assert result["error"]["http_status"] == 415

    @responses.activate
    def test_generic_http_error(self, client: OctoPrintClient) -> None:
        """A 418 status (or other unmapped code) should produce HTTP_ERROR."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body="I'm a teapot",
            status=418,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "HTTP_ERROR"
        assert result["error"]["http_status"] == 418


# ===================================================================
# get_job
# ===================================================================


class TestGetJob:
    """Tests for OctoPrintClient.get_job()."""

    @responses.activate
    def test_success_with_active_job(
        self,
        client: OctoPrintClient,
        job_data_printing: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/job"),
            json=job_data_printing,
            status=200,
        )
        result = client.get_job()
        assert result["success"] is True
        data = result["data"]
        assert data["state"] == "Printing"
        assert data["progress"]["completion"] == 42.5
        assert data["job"]["file"]["name"] == "test_model.gcode"

    @responses.activate
    def test_success_idle(
        self,
        client: OctoPrintClient,
        job_data_idle: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/job"),
            json=job_data_idle,
            status=200,
        )
        result = client.get_job()
        assert result["success"] is True
        assert result["data"]["progress"]["completion"] is None


# ===================================================================
# list_files
# ===================================================================


class TestListFiles:
    """Tests for OctoPrintClient.list_files()."""

    @responses.activate
    def test_success(
        self,
        client: OctoPrintClient,
        files_list_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/files/local"),
            json=files_list_response,
            status=200,
        )
        result = client.list_files(location="local", recursive=True)
        assert result["success"] is True
        assert len(result["data"]["files"]) == 3
        # Verify the recursive query param was passed
        assert "recursive=true" in responses.calls[0].request.url

    @responses.activate
    def test_non_recursive(
        self,
        client: OctoPrintClient,
        files_list_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/files/local"),
            json=files_list_response,
            status=200,
        )
        result = client.list_files(location="local", recursive=False)
        assert result["success"] is True
        assert "recursive" not in responses.calls[0].request.url

    @responses.activate
    def test_sdcard_location(
        self,
        client: OctoPrintClient,
        files_list_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/files/sdcard"),
            json=files_list_response,
            status=200,
        )
        result = client.list_files(location="sdcard")
        assert result["success"] is True
        assert "/api/files/sdcard" in responses.calls[0].request.url


# ===================================================================
# upload_file
# ===================================================================


class TestUploadFile:
    """Tests for OctoPrintClient.upload_file()."""

    @responses.activate
    def test_success(
        self,
        client: OctoPrintClient,
        gcode_file,
        upload_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.POST,
            _url("/api/files/local"),
            json=upload_response,
            status=201,
        )
        result = client.upload_file(str(gcode_file))
        assert result["success"] is True
        assert result["data"]["done"] is True

    @responses.activate
    def test_with_select_and_print(
        self,
        client: OctoPrintClient,
        gcode_file,
        upload_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.POST,
            _url("/api/files/local"),
            json=upload_response,
            status=201,
        )
        result = client.upload_file(
            str(gcode_file),
            select=True,
            print_after=True,
        )
        assert result["success"] is True
        body = responses.calls[0].request.body
        # The body is multipart; check that select and print fields are present
        assert b"select" in body
        assert b"print" in body

    def test_file_not_found(self, client: OctoPrintClient) -> None:
        result = client.upload_file("/nonexistent/path/model.gcode")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "not found" in result["error"]["message"].lower()

    @responses.activate
    def test_sdcard_upload(
        self,
        client: OctoPrintClient,
        gcode_file,
        upload_response: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.POST,
            _url("/api/files/sdcard"),
            json=upload_response,
            status=201,
        )
        result = client.upload_file(str(gcode_file), location="sdcard")
        assert result["success"] is True
        assert "/api/files/sdcard" in responses.calls[0].request.url


# ===================================================================
# Job commands: start, cancel, pause
# ===================================================================


class TestJobCommands:
    """Tests for start_job, cancel_job, and pause_job."""

    @responses.activate
    def test_start_job(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.start_job()
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "start"}

    @responses.activate
    def test_cancel_job(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.cancel_job()
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "cancel"}

    @responses.activate
    def test_pause_job_toggle(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.pause_job()
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "pause", "action": "toggle"}

    @responses.activate
    def test_pause_job_explicit_pause(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.pause_job(action="pause")
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "pause", "action": "pause"}

    @responses.activate
    def test_pause_job_resume(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.pause_job(action="resume")
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "pause", "action": "resume"}

    @responses.activate
    def test_start_job_conflict(self, client: OctoPrintClient) -> None:
        """Starting a job when one is already running returns a CONFLICT."""
        responses.add(
            responses.POST,
            _url("/api/job"),
            json={"error": "Printer already printing"},
            status=409,
        )
        result = client.start_job()
        assert result["success"] is False
        assert result["error"]["code"] == "CONFLICT"


# ===================================================================
# Temperature commands
# ===================================================================


class TestTemperatureCommands:
    """Tests for set_tool_temp and set_bed_temp."""

    @responses.activate
    def test_set_tool_temp(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/printer/tool"),
            status=204,
        )
        result = client.set_tool_temp({"tool0": 210})
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "target", "targets": {"tool0": 210}}

    @responses.activate
    def test_set_bed_temp(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/printer/bed"),
            status=204,
        )
        result = client.set_bed_temp(60)
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "target", "target": 60}

    @responses.activate
    def test_set_bed_temp_off(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/printer/bed"),
            status=204,
        )
        result = client.set_bed_temp(0)
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body["target"] == 0


# ===================================================================
# G-code commands
# ===================================================================


class TestSendGcode:
    """Tests for send_gcode."""

    @responses.activate
    def test_single_command(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/printer/command"),
            status=204,
        )
        result = client.send_gcode("G28")
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"commands": ["G28"]}

    @responses.activate
    def test_multiple_commands(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/printer/command"),
            status=204,
        )
        result = client.send_gcode(["G28", "M104 S200", "G1 X10 Y10"])
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"commands": ["G28", "M104 S200", "G1 X10 Y10"]}


# ===================================================================
# Retry logic
# ===================================================================


class TestRetryLogic:
    """Test retry behavior on transient failures (502, 503, 504)."""

    @responses.activate
    def test_retry_on_502_then_success(self, retry_client: OctoPrintClient) -> None:
        """First request returns 502, second succeeds."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Bad Gateway"},
            status=502,
        )
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"state": {"text": "Operational"}},
            status=200,
        )
        with patch("octoprint_cli.client.time.sleep"):
            result = retry_client.get_printer_state()
        assert result["success"] is True
        assert len(responses.calls) == 2

    @responses.activate
    def test_retry_on_503_exhausted(self, retry_client: OctoPrintClient) -> None:
        """All 3 attempts return 503 -- final error is returned."""
        for _ in range(3):
            responses.add(
                responses.GET,
                _url("/api/printer"),
                json={"error": "Service Unavailable"},
                status=503,
            )
        with patch("octoprint_cli.client.time.sleep"):
            result = retry_client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_ERROR"
        assert len(responses.calls) == 3

    @responses.activate
    def test_retry_on_504(self, retry_client: OctoPrintClient) -> None:
        """504 Gateway Timeout is retryable."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Gateway Timeout"},
            status=504,
        )
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"state": {"text": "Operational"}},
            status=200,
        )
        with patch("octoprint_cli.client.time.sleep"):
            result = retry_client.get_printer_state()
        assert result["success"] is True
        assert len(responses.calls) == 2

    @responses.activate
    def test_non_retryable_error_no_retry(self, retry_client: OctoPrintClient) -> None:
        """A 403 should NOT be retried."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"error": "Forbidden"},
            status=403,
        )
        result = retry_client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
        assert len(responses.calls) == 1


# ===================================================================
# Connection error handling
# ===================================================================


class TestConnectionErrors:
    """Test handling of network-level errors."""

    @responses.activate
    def test_connection_error(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body=ConnectionError("Connection refused"),
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "CONNECTION_ERROR"

    @responses.activate
    def test_timeout_error(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body=Timeout("Read timed out"),
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "TIMEOUT"

    @responses.activate
    def test_connection_error_retry_then_success(
        self, retry_client: OctoPrintClient
    ) -> None:
        """Connection error on first attempt, success on second."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body=ConnectionError("Connection refused"),
        )
        responses.add(
            responses.GET,
            _url("/api/printer"),
            json={"state": {"text": "Operational"}},
            status=200,
        )
        with patch("octoprint_cli.client.time.sleep"):
            result = retry_client.get_printer_state()
        assert result["success"] is True
        assert len(responses.calls) == 2

    @responses.activate
    def test_timeout_retry_exhausted(self, retry_client: OctoPrintClient) -> None:
        """All attempts time out."""
        for _ in range(3):
            responses.add(
                responses.GET,
                _url("/api/printer"),
                body=Timeout("Read timed out"),
            )
        with patch("octoprint_cli.client.time.sleep"):
            result = retry_client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "TIMEOUT"
        assert len(responses.calls) == 3


# ===================================================================
# Client construction
# ===================================================================


class TestClientConstruction:
    """Test OctoPrintClient init and internal helpers."""

    def test_trailing_slash_stripped(self) -> None:
        c = OctoPrintClient("http://octopi.local/", "KEY")
        assert c.host == "http://octopi.local"

    def test_api_key_in_headers(self) -> None:
        c = OctoPrintClient("http://octopi.local", "MYKEY123")
        assert c._session.headers["X-Api-Key"] == "MYKEY123"

    def test_url_builder(self) -> None:
        c = OctoPrintClient("http://octopi.local", "KEY")
        assert c._url("/api/printer") == "http://octopi.local/api/printer"

    def test_success_helper(self) -> None:
        result = OctoPrintClient._success({"key": "value"})
        assert result == {"success": True, "data": {"key": "value"}, "error": None}

    def test_success_helper_no_data(self) -> None:
        result = OctoPrintClient._success(None)
        assert result == {"success": True, "data": None, "error": None}

    def test_error_helper(self) -> None:
        result = OctoPrintClient._error("TEST_ERROR", "Something broke", 500)
        assert result["success"] is False
        assert result["data"] is None
        assert result["error"]["code"] == "TEST_ERROR"
        assert result["error"]["message"] == "Something broke"
        assert result["error"]["http_status"] == 500

    def test_error_helper_no_status(self) -> None:
        result = OctoPrintClient._error("TEST_ERROR", "Something broke")
        assert result["error"]["http_status"] is None


# ===================================================================
# Edge cases in response parsing
# ===================================================================


class TestResponseParsing:
    """Test edge cases in _request response parsing."""

    @responses.activate
    def test_204_no_content(self, client: OctoPrintClient) -> None:
        """204 No Content should return success with None data."""
        responses.add(
            responses.POST,
            _url("/api/job"),
            status=204,
        )
        result = client.start_job()
        assert result["success"] is True
        assert result["data"] is None

    @responses.activate
    def test_200_empty_body(self, client: OctoPrintClient) -> None:
        """200 with empty body should return success with None data."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body="",
            status=200,
        )
        result = client.get_printer_state()
        assert result["success"] is True
        assert result["data"] is None

    @responses.activate
    def test_200_non_json_body(self, client: OctoPrintClient) -> None:
        """200 with non-JSON body should return success with None data."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body="OK",
            status=200,
        )
        result = client.get_printer_state()
        assert result["success"] is True
        assert result["data"] is None

    @responses.activate
    def test_error_response_non_json_body(self, client: OctoPrintClient) -> None:
        """Error response with non-JSON body should still produce an error dict."""
        responses.add(
            responses.GET,
            _url("/api/printer"),
            body="Bad Gateway",
            status=403,
        )
        result = client.get_printer_state()
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ===================================================================
# get_connection
# ===================================================================


class TestGetConnection:
    """Tests for OctoPrintClient.get_connection()."""

    @responses.activate
    def test_success(
        self,
        client: OctoPrintClient,
        connection_state_operational: Dict[str, Any],
    ) -> None:
        responses.add(
            responses.GET,
            _url("/api/connection"),
            json=connection_state_operational,
            status=200,
        )
        result = client.get_connection()
        assert result["success"] is True
        assert result["data"]["current"]["state"] == "Operational"


# ===================================================================
# get_file_info and select_file
# ===================================================================


class TestFileOperations:
    """Tests for get_file_info and select_file."""

    @responses.activate
    def test_get_file_info(self, client: OctoPrintClient) -> None:
        file_data = {
            "name": "benchy.gcode",
            "size": 2048000,
            "gcodeAnalysis": {
                "estimatedPrintTime": 3600,
                "filament": {"tool0": {"length": 5000.0, "volume": 12.5}},
            },
        }
        responses.add(
            responses.GET,
            _url("/api/files/local/benchy.gcode"),
            json=file_data,
            status=200,
        )
        result = client.get_file_info("local", "benchy.gcode")
        assert result["success"] is True
        assert result["data"]["name"] == "benchy.gcode"

    @responses.activate
    def test_select_file(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/files/local/benchy.gcode"),
            status=204,
        )
        result = client.select_file("local", "benchy.gcode", print_after=False)
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body == {"command": "select", "print": False}

    @responses.activate
    def test_select_file_and_print(self, client: OctoPrintClient) -> None:
        responses.add(
            responses.POST,
            _url("/api/files/local/benchy.gcode"),
            status=204,
        )
        result = client.select_file("local", "benchy.gcode", print_after=True)
        assert result["success"] is True
        body = json.loads(responses.calls[0].request.body)
        assert body["print"] is True
