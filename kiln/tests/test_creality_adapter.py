"""Tests for the Creality brand adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from kiln.printers.base import PrinterError
from kiln.printers.creality import (
    CrealityAdapter,
    _candidate_moonraker_urls,
    diagnose_creality_moonraker,
)


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = payload
    return response


class TestCandidateMoonrakerUrls:
    def test_bare_host_tries_creality_moonraker_ports(self) -> None:
        assert _candidate_moonraker_urls("k1-max.local") == [
            "http://k1-max.local:7125",
            "http://k1-max.local:80",
            "http://k1-max.local:4408",
        ]

    def test_explicit_port_stays_exact(self) -> None:
        assert _candidate_moonraker_urls("http://192.168.1.55:7125") == [
            "http://192.168.1.55:7125"
        ]

    def test_scheme_without_port_adds_candidates(self) -> None:
        assert _candidate_moonraker_urls("http://192.168.1.55") == [
            "http://192.168.1.55",
            "http://192.168.1.55:7125",
            "http://192.168.1.55:80",
            "http://192.168.1.55:4408",
        ]


class TestCrealityAdapter:
    def test_resolves_bare_host_to_moonraker_port(self) -> None:
        with patch("kiln.printers.creality.requests.get") as mock_get:
            mock_get.return_value = _ok_response({"result": {"klippy_state": "ready"}})

            adapter = CrealityAdapter("k1-max.local", timeout=5, retries=1)

        assert adapter.name == "creality"
        assert adapter.moonraker_url == "http://k1-max.local:7125"
        mock_get.assert_called_once()

    def test_falls_through_to_7125_when_http_root_is_not_moonraker(self) -> None:
        bad_root = _ok_response({})
        good_moonraker = _ok_response({"result": {"klippy_state": "ready"}})
        with patch("kiln.printers.creality.requests.get", side_effect=[bad_root, good_moonraker]):
            adapter = CrealityAdapter("http://192.168.1.55", timeout=5, retries=1)

        assert adapter.moonraker_url == "http://192.168.1.55:7125"

    def test_connection_failure_raises_actionable_error(self) -> None:
        with patch(
            "kiln.printers.creality.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ), pytest.raises(PrinterError, match="http://<ip>:7125"):
            CrealityAdapter("k1-max.local", timeout=5, retries=1)

    def test_serial_path_gets_clear_error(self) -> None:
        with pytest.raises(PrinterError, match="type 'serial' or 'octoprint'"):
            CrealityAdapter("/dev/ttyUSB0", timeout=5, retries=1)

    def test_set_safety_profile_updates_delegate(self) -> None:
        with patch("kiln.printers.creality.requests.get") as mock_get:
            mock_get.return_value = _ok_response({"result": {"klippy_state": "ready"}})
            adapter = CrealityAdapter("k1-max.local", timeout=5, retries=1)

        adapter.set_safety_profile("k1_max")

        assert adapter._safety_profile_id == "k1_max"
        assert adapter._backend._safety_profile_id == "k1_max"

    def test_get_cfs_status_discovers_objects_and_slots(self) -> None:
        with patch("kiln.printers.creality.requests.get") as mock_get:
            mock_get.return_value = _ok_response({"result": {"klippy_state": "ready"}})
            adapter = CrealityAdapter("k1-max.local", timeout=5, retries=1)

        with patch(
            "kiln.printers.moonraker.MoonrakerAdapter._get_json",
            side_effect=[
                {"result": {"objects": ["print_stats", "cfs", "filament_switch_sensor runout"]}},
                {
                    "result": {
                        "status": {
                            "cfs": {
                                "boxsInfo": [
                                    {
                                        "boxId": 0,
                                        "materialId": "PLA",
                                        "color": "#FFFFFF",
                                        "remain": 82,
                                    }
                                ]
                            }
                        }
                    }
                },
                {"result": {"CFS_LOAD": "load filament", "G28": "home axes"}},
            ],
        ):
            status = adapter.get_cfs_status()

        assert status["detected"] is True
        assert status["hardware_unverified"] is True
        assert status["active_slot_control_supported"] is False
        assert status["candidate_objects"] == ["cfs"]
        assert status["candidate_commands"] == ["CFS_LOAD"]
        assert status["slots"][0]["material"] == "PLA"


class TestCrealityDiagnostics:
    def test_diagnostic_reports_resolved_port(self) -> None:
        with patch("kiln.printers.creality.requests.get") as mock_get:
            mock_get.return_value = _ok_response(
                {"result": {"moonraker_version": "0.9.3", "klippy_state": "ready"}}
            )

            diag = diagnose_creality_moonraker("k1-max.local")

        assert diag.ok is True
        assert diag.resolved_url == "http://k1-max.local:7125"
        assert diag.browser_test_url == "http://k1-max.local:7125/server/info"
        assert diag.klippy_state == "ready"
        mock_get.assert_called_once()

    def test_diagnostic_reports_auth_required(self) -> None:
        response = MagicMock()
        response.ok = False
        response.status_code = 401
        with patch("kiln.printers.creality.requests.get", return_value=response):
            diag = diagnose_creality_moonraker("http://192.168.1.55:7125")

        assert diag.ok is False
        assert diag.auth_required is True
        assert diag.likely_cause == "moonraker_auth_required"
        assert diag.user_message is not None
        assert "api key" in diag.user_message.lower()
        assert diag.checks[0].auth_required is True
        assert "api-key" in " ".join(diag.next_steps).lower()

    def test_diagnostic_connection_failure_explains_lan_ip_and_ports(self) -> None:
        with patch(
            "kiln.printers.creality.requests.get",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            diag = diagnose_creality_moonraker("k1-max.local")

        assert diag.ok is False
        assert diag.likely_cause == "network_or_port_unreachable"
        assert diag.firmware_lockdown_possible is False
        guidance = " ".join(diag.connection_checklist + diag.next_steps).lower()
        assert "same wi-fi/lan" in guidance
        assert "ip address" in guidance
        assert ":7125/server/info" in guidance
        assert "stock firmware" in guidance

    def test_diagnostic_non_moonraker_http_marks_firmware_lockdown_possible(self) -> None:
        response = MagicMock()
        response.ok = False
        response.status_code = 404
        with patch("kiln.printers.creality.requests.get", return_value=response):
            diag = diagnose_creality_moonraker("http://192.168.1.55:7125")

        assert diag.ok is False
        assert diag.likely_cause == "firmware_locked_or_wrong_port"
        assert diag.firmware_lockdown_possible is True
        assert diag.checks[0].failure_kind == "moonraker_not_exposed"
        assert diag.user_message is not None
        assert "stock firmware" in diag.user_message.lower()
