"""Tests for Moonraker adapter firmware update methods.

Covers:
- get_firmware_status — parsing Moonraker update manager response
- update_firmware — triggering update via POST /machine/update/upgrade
- rollback_firmware — triggering rollback via POST /machine/update/rollback
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
from kiln.printers.moonraker import MoonrakerAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOST = "http://klipper.local:7125"


def _adapter(**kwargs: Any) -> MoonrakerAdapter:
    defaults: Dict[str, Any] = {"host": HOST, "timeout": 5, "retries": 1}
    defaults.update(kwargs)
    return MoonrakerAdapter(**defaults)


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


class TestMoonrakerFirmwareStatus:
    """Tests for get_firmware_status()."""

    def test_returns_components_from_version_info(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": False,
                "version_info": {
                    "klipper": {
                        "version": "v0.12.0",
                        "remote_version": "v0.12.1",
                        "configured_type": "git_repo",
                        "channel": "stable",
                    },
                    "moonraker": {
                        "version": "v0.9.0",
                        "remote_version": "v0.9.0",
                        "configured_type": "git_repo",
                        "channel": "stable",
                    },
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.busy is False
        assert len(status.components) == 2
        assert status.updates_available == 1

        klipper = next(c for c in status.components if c.name == "klipper")
        assert klipper.current_version == "v0.12.0"
        assert klipper.remote_version == "v0.12.1"
        assert klipper.update_available is True
        assert klipper.channel == "stable"

        moonraker = next(c for c in status.components if c.name == "moonraker")
        assert moonraker.update_available is False

    def test_busy_flag_propagated(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": True,
                "version_info": {},
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.busy is True

    def test_system_package_count_detection(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": False,
                "version_info": {
                    "system": {
                        "version": "",
                        "package_count": 5,
                    },
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        sys = status.components[0]
        assert sys.name == "system"
        assert sys.update_available is True

    def test_commits_behind_detection(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": False,
                "version_info": {
                    "klipper": {
                        "version": "v0.12.0",
                        "remote_version": "v0.12.0",
                        "commits_behind_count": 3,
                    },
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.components[0].update_available is True
        assert status.updates_available == 1

    def test_rollback_version_captured(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": False,
                "version_info": {
                    "klipper": {
                        "version": "v0.12.1",
                        "remote_version": "v0.12.1",
                        "rollback_version": "v0.12.0",
                    },
                },
            },
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status.components[0].rollback_version == "v0.12.0"

    def test_non_dict_entries_skipped(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {
                "busy": False,
                "version_info": {
                    "klipper": {"version": "v0.12.0"},
                    "bad_entry": "not a dict",
                },
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

    def test_empty_version_info(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={
            "result": {"busy": False, "version_info": {}},
        })
        with mock.patch.object(adapter._session, "request", return_value=resp):
            status = adapter.get_firmware_status()

        assert status is not None
        assert status.components == []
        assert status.updates_available == 0


# ---------------------------------------------------------------------------
# update_firmware
# ---------------------------------------------------------------------------


class TestMoonrakerUpdateFirmware:
    """Tests for update_firmware()."""

    def test_update_all_components(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data={
            "result": {"state": "ready", "state_message": ""},
        })
        obj_resp = _mock_response(json_data={
            "result": {"status": {"print_stats": {"state": "standby"}}},
        })
        update_resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[info_resp, obj_resp, update_resp],
        ):
            result = adapter.update_firmware()

        assert result.success is True
        assert result.component is None
        assert "all components" in result.message

    def test_update_specific_component(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data={
            "result": {"state": "ready", "state_message": ""},
        })
        obj_resp = _mock_response(json_data={
            "result": {"status": {"print_stats": {"state": "standby"}}},
        })
        update_resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[info_resp, obj_resp, update_resp],
        ):
            result = adapter.update_firmware(component="klipper")

        assert result.success is True
        assert result.component == "klipper"
        assert "klipper" in result.message

    def test_refuses_while_printing(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data={
            "result": {"state": "ready", "state_message": ""},
        })
        obj_resp = _mock_response(json_data={
            "result": {"status": {"print_stats": {"state": "printing"}}},
        })

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[info_resp, obj_resp],
        ):
            with pytest.raises(PrinterError, match="Cannot update firmware while printing"):
                adapter.update_firmware()

    def test_http_failure_raises_printer_error(self) -> None:
        adapter = _adapter()
        info_resp = _mock_response(json_data={
            "result": {"state": "ready", "state_message": ""},
        })
        obj_resp = _mock_response(json_data={
            "result": {"status": {"print_stats": {"state": "standby"}}},
        })

        with mock.patch.object(
            adapter._session, "request",
            side_effect=[info_resp, obj_resp, requests.exceptions.ConnectionError("fail")],
        ):
            with pytest.raises(PrinterError):
                adapter.update_firmware()


# ---------------------------------------------------------------------------
# rollback_firmware
# ---------------------------------------------------------------------------


class TestMoonrakerRollbackFirmware:
    """Tests for rollback_firmware()."""

    def test_rollback_success(self) -> None:
        adapter = _adapter()
        resp = _mock_response(json_data={"result": "ok"})

        with mock.patch.object(adapter._session, "request", return_value=resp):
            result = adapter.rollback_firmware("klipper")

        assert result.success is True
        assert result.component == "klipper"
        assert "klipper" in result.message

    def test_empty_component_raises(self) -> None:
        adapter = _adapter()
        with pytest.raises(PrinterError, match="Component name is required"):
            adapter.rollback_firmware("")

    def test_http_failure_raises_printer_error(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter._session, "request",
            side_effect=requests.exceptions.ConnectionError("fail"),
        ):
            with pytest.raises(PrinterError):
                adapter.rollback_firmware("klipper")
