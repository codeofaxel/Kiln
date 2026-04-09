"""Tests for network_tools.py (provider integration) plugin MCP tools.

Covers:
- connect_provider_account — happy path, ThreeDOSError, unexpected error
- sync_provider_capacity — no update, with update, error
- list_provider_capacity — happy path, error
- find_provider_capacity — happy path, error
- submit_provider_job — happy path, error
- provider_job_status — happy path, error
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_network_tools() -> dict:
    """Register the plugin on a mock MCP and return captured tool functions."""
    from kiln.plugins.network_tools import plugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    plugin.register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def network_tools():
    return _register_network_tools()


def _mock_printer(pid: str = "p1") -> MagicMock:
    p = MagicMock()
    p.to_dict.return_value = {"printer_id": pid, "name": "Test Printer"}
    return p


def _mock_job(jid: str = "j1") -> MagicMock:
    j = MagicMock()
    j.to_dict.return_value = {"job_id": jid, "status": "pending"}
    return j


# ---------------------------------------------------------------------------
# TestConnectProviderAccount
# ---------------------------------------------------------------------------


class TestConnectProviderAccount:
    """Tests for connect_provider_account."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_client, network_tools):
        listing = MagicMock()
        listing.to_dict.return_value = {"printer_id": "p1"}
        mock_client.return_value.register_printer.return_value = listing

        result = network_tools["connect_provider_account"](
            name="Prusa MK4",
            location="Austin, TX",
        )

        assert result["success"] is True
        assert result["provider_name"] == "3dos"

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_threedos_error(self, _auth, mock_client, network_tools):
        from kiln.gateway.threedos import ThreeDOSError

        mock_client.return_value.register_printer.side_effect = ThreeDOSError("API down")

        result = network_tools["connect_provider_account"](
            name="Prusa MK4",
            location="Austin, TX",
        )

        assert result["success"] is False

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_unexpected_error(self, _auth, mock_client, network_tools):
        mock_client.side_effect = RuntimeError("boom")

        result = network_tools["connect_provider_account"](
            name="Prusa MK4",
            location="Austin, TX",
        )

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSyncProviderCapacity
# ---------------------------------------------------------------------------


class TestSyncProviderCapacity:
    """Tests for sync_provider_capacity."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_no_update(self, _auth, mock_client, network_tools):
        mock_client.return_value.list_my_printers.return_value = [_mock_printer()]

        result = network_tools["sync_provider_capacity"]()

        assert result["success"] is True
        assert result["updated"] is False
        assert result["count"] == 1

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_with_update(self, _auth, mock_client, network_tools):
        mock_client.return_value.list_my_printers.return_value = [_mock_printer()]

        result = network_tools["sync_provider_capacity"](
            printer_id="p1",
            available=True,
        )

        assert result["success"] is True
        assert result["updated"] is True
        mock_client.return_value.update_printer_status.assert_called_once()

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_client, network_tools):
        mock_client.side_effect = RuntimeError("boom")

        result = network_tools["sync_provider_capacity"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestListProviderCapacity
# ---------------------------------------------------------------------------


class TestListProviderCapacity:
    """Tests for list_provider_capacity."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_client, network_tools):
        mock_client.return_value.list_my_printers.return_value = [
            _mock_printer("p1"),
            _mock_printer("p2"),
        ]

        result = network_tools["list_provider_capacity"]()

        assert result["success"] is True
        assert result["count"] == 2

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_client, network_tools):
        mock_client.side_effect = RuntimeError("boom")

        result = network_tools["list_provider_capacity"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestFindProviderCapacity
# ---------------------------------------------------------------------------


class TestFindProviderCapacity:
    """Tests for find_provider_capacity."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_client, network_tools):
        mock_client.return_value.find_printers.return_value = [_mock_printer()]

        result = network_tools["find_provider_capacity"](material="PLA")

        assert result["success"] is True
        assert result["count"] == 1

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_client, network_tools):
        mock_client.return_value.find_printers.side_effect = ValueError("bad material")

        result = network_tools["find_provider_capacity"](material="unobtainium")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSubmitProviderJob
# ---------------------------------------------------------------------------


class TestSubmitProviderJob:
    """Tests for submit_provider_job."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_client, network_tools):
        mock_client.return_value.submit_network_job.return_value = _mock_job()

        result = network_tools["submit_provider_job"](
            file_url="https://example.com/model.stl",
            material="PLA",
        )

        assert result["success"] is True
        assert result["job"]["job_id"] == "j1"

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_client, network_tools):
        mock_client.return_value.submit_network_job.side_effect = ValueError("bad url")

        result = network_tools["submit_provider_job"](
            file_url="bad",
            material="PLA",
        )

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestProviderJobStatus
# ---------------------------------------------------------------------------


class TestProviderJobStatus:
    """Tests for provider_job_status."""

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_client, network_tools):
        mock_client.return_value.get_network_job.return_value = _mock_job("j42")

        result = network_tools["provider_job_status"](job_id="j42")

        assert result["success"] is True
        assert result["job"]["job_id"] == "j42"

    @patch("kiln.server._get_threedos_client")
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_client, network_tools):
        from kiln.gateway.threedos import ThreeDOSError

        mock_client.return_value.get_network_job.side_effect = ThreeDOSError("not found")

        result = network_tools["provider_job_status"](job_id="bad")

        assert result["success"] is False
