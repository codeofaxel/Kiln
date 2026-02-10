"""Shared fixtures for octoprint-cli test suite."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest
import responses

from octoprint_cli.client import OctoPrintClient


# ---------------------------------------------------------------------------
# Constants reused across tests
# ---------------------------------------------------------------------------

TEST_HOST = "http://octopi.local"
TEST_API_KEY = "TESTAPIKEY123456"
TEST_TIMEOUT = 5
TEST_RETRIES = 1  # keep tests fast; retry tests override this


# ---------------------------------------------------------------------------
# Client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> OctoPrintClient:
    """Return an OctoPrintClient pre-configured for testing (1 retry, short timeout)."""
    return OctoPrintClient(
        host=TEST_HOST,
        api_key=TEST_API_KEY,
        timeout=TEST_TIMEOUT,
        retries=TEST_RETRIES,
    )


@pytest.fixture()
def retry_client() -> OctoPrintClient:
    """Return an OctoPrintClient configured with 3 retries for retry-logic tests."""
    return OctoPrintClient(
        host=TEST_HOST,
        api_key=TEST_API_KEY,
        timeout=TEST_TIMEOUT,
        retries=3,
    )


# ---------------------------------------------------------------------------
# Mock API response payloads
# ---------------------------------------------------------------------------


@pytest.fixture()
def printer_state_operational() -> Dict[str, Any]:
    """OctoPrint /api/printer response when the printer is operational and idle."""
    return {
        "state": {
            "text": "Operational",
            "flags": {
                "operational": True,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "paused": False,
                "error": False,
                "ready": True,
                "closedOrError": False,
            },
        },
        "temperature": {
            "tool0": {"actual": 24.3, "target": 0.0, "offset": 0},
            "bed": {"actual": 23.1, "target": 0.0, "offset": 0},
        },
    }


@pytest.fixture()
def printer_state_printing() -> Dict[str, Any]:
    """OctoPrint /api/printer response when the printer is actively printing."""
    return {
        "state": {
            "text": "Printing",
            "flags": {
                "operational": True,
                "printing": True,
                "cancelling": False,
                "pausing": False,
                "paused": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
        "temperature": {
            "tool0": {"actual": 210.0, "target": 210.0, "offset": 0},
            "bed": {"actual": 60.0, "target": 60.0, "offset": 0},
        },
    }


@pytest.fixture()
def printer_state_error() -> Dict[str, Any]:
    """OctoPrint /api/printer response when the printer is in an error state."""
    return {
        "state": {
            "text": "Error",
            "flags": {
                "operational": False,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "paused": False,
                "error": True,
                "ready": False,
                "closedOrError": True,
            },
        },
        "temperature": {
            "tool0": {"actual": 0.0, "target": 0.0, "offset": 0},
            "bed": {"actual": 0.0, "target": 0.0, "offset": 0},
        },
    }


@pytest.fixture()
def connection_state_operational() -> Dict[str, Any]:
    """OctoPrint /api/connection response when the printer is connected."""
    return {
        "current": {
            "state": "Operational",
            "port": "/dev/ttyUSB0",
            "baudrate": 115200,
            "printerProfile": "_default",
        },
        "options": {
            "ports": ["/dev/ttyUSB0"],
            "baudrates": [115200, 250000],
            "printerProfiles": [{"id": "_default", "name": "Default"}],
        },
    }


@pytest.fixture()
def connection_state_closed() -> Dict[str, Any]:
    """OctoPrint /api/connection response when the printer is disconnected."""
    return {
        "current": {
            "state": "Closed",
            "port": None,
            "baudrate": None,
            "printerProfile": "_default",
        },
        "options": {
            "ports": ["/dev/ttyUSB0"],
            "baudrates": [115200, 250000],
            "printerProfiles": [{"id": "_default", "name": "Default"}],
        },
    }


@pytest.fixture()
def job_data_printing() -> Dict[str, Any]:
    """OctoPrint /api/job response when a print is active."""
    return {
        "job": {
            "file": {
                "name": "test_model.gcode",
                "origin": "local",
                "size": 1048576,
                "date": 1700000000,
            },
            "estimatedPrintTime": 3600,
            "filament": {"tool0": {"length": 5000.0, "volume": 12.5}},
        },
        "progress": {
            "completion": 42.5,
            "filepos": 446464,
            "printTime": 1530,
            "printTimeLeft": 2070,
            "printTimeLeftOrigin": "linear",
        },
        "state": "Printing",
    }


@pytest.fixture()
def job_data_idle() -> Dict[str, Any]:
    """OctoPrint /api/job response when no job is active."""
    return {
        "job": {
            "file": {"name": None, "origin": None, "size": None, "date": None},
            "estimatedPrintTime": None,
            "filament": None,
        },
        "progress": {
            "completion": None,
            "filepos": None,
            "printTime": None,
            "printTimeLeft": None,
            "printTimeLeftOrigin": None,
        },
        "state": "Operational",
    }


@pytest.fixture()
def files_list_response() -> Dict[str, Any]:
    """OctoPrint /api/files/local response with a mix of files and folders."""
    return {
        "files": [
            {
                "name": "benchy.gcode",
                "display": "benchy.gcode",
                "path": "benchy.gcode",
                "type": "machinecode",
                "origin": "local",
                "size": 2048000,
                "date": 1700000000,
            },
            {
                "name": "calibration",
                "display": "calibration",
                "path": "calibration",
                "type": "folder",
                "origin": "local",
                "children": [
                    {
                        "name": "cube.gcode",
                        "display": "cube.gcode",
                        "path": "calibration/cube.gcode",
                        "type": "machinecode",
                        "origin": "local",
                        "size": 512000,
                        "date": 1699000000,
                    },
                ],
            },
            {
                "name": "vase.gcode",
                "display": "vase.gcode",
                "path": "vase.gcode",
                "type": "machinecode",
                "origin": "local",
                "size": 4096000,
                "date": 1701000000,
            },
        ],
        "free": 10000000000,
        "total": 32000000000,
    }


@pytest.fixture()
def upload_response() -> Dict[str, Any]:
    """OctoPrint upload response payload."""
    return {
        "files": {
            "local": {
                "name": "test_model.gcode",
                "origin": "local",
                "path": "test_model.gcode",
                "refs": {
                    "resource": f"{TEST_HOST}/api/files/local/test_model.gcode",
                    "download": f"{TEST_HOST}/downloads/files/local/test_model.gcode",
                },
            },
        },
        "done": True,
    }


# ---------------------------------------------------------------------------
# Temp directory fixtures for file operations
# ---------------------------------------------------------------------------


@pytest.fixture()
def gcode_file(tmp_path: Path) -> Path:
    """Create a small valid .gcode file for upload/validation tests."""
    p = tmp_path / "test_print.gcode"
    p.write_text("; G-code test file\nG28\nG1 X10 Y10 Z0.2\n")
    return p


@pytest.fixture()
def large_gcode_file(tmp_path: Path) -> Path:
    """Create a gcode file above the 500 MB warning threshold (fake via small content)."""
    p = tmp_path / "big_print.gcode"
    p.write_text("G28\n")
    return p


@pytest.fixture()
def empty_file(tmp_path: Path) -> Path:
    """Create an empty file."""
    p = tmp_path / "empty.gcode"
    p.write_text("")
    return p


@pytest.fixture()
def non_gcode_file(tmp_path: Path) -> Path:
    """Create a file with a non-gcode extension."""
    p = tmp_path / "readme.txt"
    p.write_text("This is not gcode.")
    return p


@pytest.fixture()
def sample_config_file(tmp_path: Path) -> Path:
    """Create a sample YAML config file."""
    import yaml

    config = {
        "host": "http://myprinter.local",
        "api_key": "FILEAPIKEY789",
        "timeout": 15,
        "retries": 2,
    }
    p = tmp_path / "config.yaml"
    with p.open("w") as fh:
        yaml.safe_dump(config, fh)
    return p


@pytest.fixture()
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure OctoPrint env vars are not set."""
    monkeypatch.delenv("OCTOPRINT_HOST", raising=False)
    monkeypatch.delenv("OCTOPRINT_API_KEY", raising=False)
