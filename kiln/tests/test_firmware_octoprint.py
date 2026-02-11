"""Tests for OctoPrint adapter firmware update methods.

Covers:
- get_firmware_status — parsing Software Update plugin response
- update_firmware — triggering update via POST /plugin/softwareupdate/update
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest import mock

import pytest
import requests

from kiln.printers.base import (
    FirmwareComponent,
    FirmwareStatus,
    FirmwareUpdateResult,
    PrinterError,
    PrinterStatus,
)
from kiln.printers.octoprint import OctoPrintAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOST = "http://octopi.local"
API_KEY = "test-api-key"


def _adapter(**kwargs: Any) -> OctoPrintAdapter:
    defaults: Dict[str, Any] = {
        "host": HOST,
        "api_key": API_KEY,
        "timeout": 5,
        "retries": 1,
    }
    defaults.update(kwargs)
    return OctoPrintAdapter(**defaults)


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    text: str = "",
    ok: Optional[bool] = None,
) -> mock.MagicMock:
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
# get_firmware_status
# ---------------------------------------------------------------------------


class TestOctoPrintFirmwareStatus:
    """Tests for get_firmware_status()."""

    def test_returns_components_from_information(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "busy": False,
            "information": {
                "octoprint": {
                    "displayName": "OctoPrint",
                    "information": {
                        "local": {"value": "1.9.3"},
                        "remote": {"value": "1.10.0"},
                    },
                    "updateAvailable": True,
                },
                "pi_support": {
                    "displayName": "Pi Support Plugin",
                    "information": {
                        "local": {"value": "2023.5.1"},
                        "remote": {"value": "2023.5.1"},
                    },
                    "updateAvailable": False,
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.busy is False
        assert len(status.components) == 2
        assert status.updates_available == 1

        octo = next(c for c in status.components if c.name == "OctoPrint")
        assert octo.current_version == "1.9.3"
        assert octo.remote_version == "1.10.0"
        assert octo.update_available is True
        assert octo.component_type == "octoprint_plugin"

        pi = next(c for c in status.components if c.name == "Pi Support Plugin")
        assert pi.update_available is False

    def test_busy_flag_propagated(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "busy": True,
            "information": {},
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.busy is True

    def test_non_dict_entries_skipped(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "busy": False,
            "information": {
                "octoprint": {
                    "displayName": "OctoPrint",
                    "information": {"local": {"value": "1.9.3"}},
                    "updateAvailable": False,
                },
                "bad_entry": "not a dict",
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert len(status.components) == 1

    def test_returns_none_on_http_failure(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session, "request",
            side_effect=requests.exceptions.ConnectionError("offline"),
        ):
            status = adapter.get_firmware_status()

        assert status is None

    def test_empty_information(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"busy": False, "information": {}})
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.components == []
        assert status.updates_available == 0

    def test_missing_version_fields_default_empty(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "busy": False,
            "information": {
                "plugin_x": {
                    "displayName": "Plugin X",
                    "information": {},
                    "updateAvailable": False,
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        comp = status.components[0]
        assert comp.current_version == ""
        assert comp.remote_version is None


# ---------------------------------------------------------------------------
# update_firmware
# ---------------------------------------------------------------------------


class TestOctoPrintUpdateFirmware:
    """Tests for update_firmware()."""

    def test_update_specific_component(self) -> None:
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": False, "operational": True}},
        })
        update_resp = _mock_response(json_data={"ok": True})

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[state_resp, update_resp],
        ):
            result = adapter.update_firmware(component="octoprint")

        assert result.success is True
        assert result.component == "octoprint"

    def test_update_all_auto_discovers_targets(self) -> None:
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": False, "operational": True}},
        })
        check_resp = _mock_response(json_data={
            "busy": False,
            "information": {
                "octoprint": {
                    "displayName": "OctoPrint",
                    "information": {
                        "local": {"value": "1.9.3"},
                        "remote": {"value": "1.10.0"},
                    },
                    "updateAvailable": True,
                },
            },
        })
        update_resp = _mock_response(json_data={"ok": True})

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[state_resp, check_resp, update_resp],
        ):
            result = adapter.update_firmware()

        assert result.success is True
        assert "all components" in result.message

    def test_update_all_no_updates_available(self) -> None:
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": False, "operational": True}},
        })
        check_resp = _mock_response(json_data={
            "busy": False,
            "information": {
                "octoprint": {
                    "displayName": "OctoPrint",
                    "information": {"local": {"value": "1.10.0"}},
                    "updateAvailable": False,
                },
            },
        })

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[state_resp, check_resp],
        ):
            result = adapter.update_firmware()

        assert result.success is True
        assert "up to date" in result.message

    def test_refuses_while_printing(self) -> None:
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": True, "operational": True}},
        })

        with mock.patch.object(adapter._session, "request", return_value=state_resp):
            with pytest.raises(PrinterError, match="Cannot update firmware while printing"):
                adapter.update_firmware()

    def test_http_failure_raises_printer_error(self) -> None:
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": False, "operational": True}},
        })

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[state_resp, requests.exceptions.ConnectionError("fail")],
        ):
            with pytest.raises(PrinterError):
                adapter.update_firmware(component="octoprint")

    def test_plugin_unavailable_raises(self) -> None:
        """When get_firmware_status returns None, update_firmware raises."""
        adapter = _adapter()
        state_resp = _mock_response(json_data={
            "state": {"flags": {"printing": False, "operational": True}},
        })

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[state_resp, requests.exceptions.ConnectionError("no plugin")],
        ):
            with pytest.raises(PrinterError):
                adapter.update_firmware()
