"""Shared fixtures for the Kiln test suite.

Provides reusable mock data for OctoPrint API responses, pre-configured
adapter instances, and environment variable helpers used across multiple
test modules.

NOTE: The installed ``mcp`` library's ``FastMCP`` does not accept the
``description`` keyword argument used in ``kiln.server``.  We monkey-patch
``FastMCP.__init__`` at import time so that the server module can be
loaded by the test suite without modification.
"""

from __future__ import annotations

import functools

# ---------------------------------------------------------------------------
# Monkey-patch FastMCP to accept unknown kwargs (like ``description``)
# so that ``import kiln.server`` succeeds at collection time.
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

_original_fastmcp_init = FastMCP.__init__


@functools.wraps(_original_fastmcp_init)
def _patched_fastmcp_init(self, *args, **kwargs):
    # Strip out any kwargs the current FastMCP does not understand.
    import inspect
    sig = inspect.signature(_original_fastmcp_init)
    valid_params = set(sig.parameters.keys())
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_params}
    return _original_fastmcp_init(self, *args, **filtered_kwargs)


FastMCP.__init__ = _patched_fastmcp_init  # type: ignore[method-assign]

# ---------------------------------------------------------------------------
# Now safe to import everything else.
# ---------------------------------------------------------------------------

import pytest
import responses

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.printers.octoprint import OctoPrintAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OCTOPRINT_HOST = "http://octopi.local"
OCTOPRINT_API_KEY = "TESTAPIKEY123"


# ---------------------------------------------------------------------------
# OctoPrint API response payloads
# ---------------------------------------------------------------------------

@pytest.fixture()
def printer_state_idle():
    """OctoPrint /api/printer response when idle and operational."""
    return {
        "temperature": {
            "tool0": {"actual": 24.5, "target": 0.0},
            "bed": {"actual": 23.1, "target": 0.0},
        },
        "state": {
            "text": "Operational",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": True,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_printing():
    """OctoPrint /api/printer response when actively printing."""
    return {
        "temperature": {
            "tool0": {"actual": 205.0, "target": 210.0},
            "bed": {"actual": 59.8, "target": 60.0},
        },
        "state": {
            "text": "Printing",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": True,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_paused():
    """OctoPrint /api/printer response when paused."""
    return {
        "temperature": {
            "tool0": {"actual": 200.0, "target": 210.0},
            "bed": {"actual": 58.0, "target": 60.0},
        },
        "state": {
            "text": "Paused",
            "flags": {
                "operational": True,
                "paused": True,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_error():
    """OctoPrint /api/printer response when in error state."""
    return {
        "temperature": {
            "tool0": {"actual": 0.0, "target": 0.0},
            "bed": {"actual": 0.0, "target": 0.0},
        },
        "state": {
            "text": "Error",
            "flags": {
                "operational": False,
                "paused": False,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": True,
                "ready": False,
                "closedOrError": True,
            },
        },
    }


@pytest.fixture()
def printer_state_cancelling():
    """OctoPrint /api/printer response when cancelling a job."""
    return {
        "temperature": {
            "tool0": {"actual": 195.0, "target": 0.0},
            "bed": {"actual": 55.0, "target": 0.0},
        },
        "state": {
            "text": "Cancelling",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": False,
                "cancelling": True,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def job_response_printing():
    """OctoPrint /api/job response for an active print job."""
    return {
        "job": {
            "file": {
                "name": "benchy.gcode",
                "origin": "local",
                "size": 1234567,
            },
            "estimatedPrintTime": 3600,
        },
        "progress": {
            "completion": 45.6789,
            "printTime": 1620,
            "printTimeLeft": 1980,
        },
        "state": "Printing",
    }


@pytest.fixture()
def job_response_idle():
    """OctoPrint /api/job response when no active job."""
    return {
        "job": {
            "file": {"name": None, "origin": None, "size": None},
        },
        "progress": {
            "completion": None,
            "printTime": None,
            "printTimeLeft": None,
        },
        "state": "Operational",
    }


@pytest.fixture()
def files_response_flat():
    """OctoPrint /api/files/local response with flat file list."""
    return {
        "files": [
            {
                "name": "benchy.gcode",
                "path": "benchy.gcode",
                "type": "machinecode",
                "size": 1234567,
                "date": 1700000000,
            },
            {
                "name": "cube.gcode",
                "path": "cube.gcode",
                "type": "machinecode",
                "size": 456789,
                "date": 1700001000,
            },
        ],
    }


@pytest.fixture()
def files_response_nested():
    """OctoPrint /api/files/local response with nested folders."""
    return {
        "files": [
            {
                "name": "benchy.gcode",
                "path": "benchy.gcode",
                "type": "machinecode",
                "size": 1234567,
                "date": 1700000000,
            },
            {
                "name": "calibration",
                "type": "folder",
                "children": [
                    {
                        "name": "first_layer.gcode",
                        "path": "calibration/first_layer.gcode",
                        "type": "machinecode",
                        "size": 99999,
                        "date": 1700002000,
                    },
                    {
                        "name": "subfolder",
                        "type": "folder",
                        "children": [
                            {
                                "name": "deep_file.gcode",
                                "path": "calibration/subfolder/deep_file.gcode",
                                "type": "machinecode",
                                "size": 55555,
                                "date": 1700003000,
                            },
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture()
def upload_response_success():
    """OctoPrint /api/files/local upload success response."""
    return {
        "files": {
            "local": {
                "name": "test_print.gcode",
                "origin": "local",
            },
        },
        "done": True,
    }


# ---------------------------------------------------------------------------
# Pre-configured adapter
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    """Return an OctoPrintAdapter configured for testing (retries=1, timeout=5)."""
    return OctoPrintAdapter(
        host=OCTOPRINT_HOST,
        api_key=OCTOPRINT_API_KEY,
        timeout=5,
        retries=1,
    )


@pytest.fixture()
def adapter_with_retries():
    """Return an OctoPrintAdapter configured with 3 retries for retry tests."""
    return OctoPrintAdapter(
        host=OCTOPRINT_HOST,
        api_key=OCTOPRINT_API_KEY,
        timeout=5,
        retries=3,
    )


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env_configured(monkeypatch):
    """Set the environment variables required by the server module."""
    monkeypatch.setenv("KILN_PRINTER_HOST", OCTOPRINT_HOST)
    monkeypatch.setenv("KILN_PRINTER_API_KEY", OCTOPRINT_API_KEY)
    monkeypatch.setenv("KILN_PRINTER_TYPE", "octoprint")


@pytest.fixture()
def env_missing_host(monkeypatch):
    """Ensure KILN_PRINTER_HOST is unset."""
    monkeypatch.delenv("KILN_PRINTER_HOST", raising=False)
    monkeypatch.setenv("KILN_PRINTER_API_KEY", OCTOPRINT_API_KEY)


@pytest.fixture()
def env_missing_api_key(monkeypatch):
    """Ensure KILN_PRINTER_API_KEY is unset."""
    monkeypatch.setenv("KILN_PRINTER_HOST", OCTOPRINT_HOST)
    monkeypatch.delenv("KILN_PRINTER_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Mock adapter for server tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_printer_state_idle():
    """Return a PrinterState representing an idle printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=24.5,
        tool_temp_target=0.0,
        bed_temp_actual=23.1,
        bed_temp_target=0.0,
    )


@pytest.fixture()
def mock_printer_state_printing():
    """Return a PrinterState representing a printing printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.PRINTING,
        tool_temp_actual=205.0,
        tool_temp_target=210.0,
        bed_temp_actual=59.8,
        bed_temp_target=60.0,
    )


@pytest.fixture()
def mock_printer_state_offline():
    """Return a PrinterState representing an offline printer."""
    return PrinterState(
        connected=False,
        state=PrinterStatus.OFFLINE,
    )


@pytest.fixture()
def mock_printer_state_error():
    """Return a PrinterState representing an errored printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.ERROR,
        tool_temp_actual=0.0,
        tool_temp_target=0.0,
        bed_temp_actual=0.0,
        bed_temp_target=0.0,
    )


@pytest.fixture()
def mock_job_progress():
    """Return a JobProgress for an active print."""
    return JobProgress(
        file_name="benchy.gcode",
        completion=45.68,
        print_time_seconds=1620,
        print_time_left_seconds=1980,
    )


@pytest.fixture()
def mock_capabilities():
    """Return default PrinterCapabilities."""
    return PrinterCapabilities()


@pytest.fixture()
def mock_file_list():
    """Return a list of PrinterFile objects."""
    return [
        PrinterFile(name="benchy.gcode", path="benchy.gcode", size_bytes=1234567, date=1700000000),
        PrinterFile(name="cube.gcode", path="cube.gcode", size_bytes=456789, date=1700001000),
    ]


# ---------------------------------------------------------------------------
# License tier bypass for tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bypass_license_tier(monkeypatch, tmp_path):
    """Ensure all tests run with a BUSINESS-tier license by default.

    This prevents tier-gated MCP tools from returning LICENSE_REQUIRED
    errors in existing tests.  Tests that specifically test licensing
    behaviour can override this by patching ``kiln.licensing._manager``
    themselves.
    """
    from kiln.licensing import LicenseManager, _KEY_PREFIX_BUSINESS

    # Allow legacy prefix keys in tests (no HMAC signature available)
    monkeypatch.setenv("KILN_LICENSE_OFFLINE", "1")

    mgr = LicenseManager(
        license_key=f"{_KEY_PREFIX_BUSINESS}test_bypass_key",
        license_path=tmp_path / "test_license",
        cache_path=tmp_path / "test_cache.json",
    )
    monkeypatch.setattr("kiln.licensing._manager", mgr)
