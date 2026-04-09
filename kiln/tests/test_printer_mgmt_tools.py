"""Tests for printer_management_tools.py plugin MCP tools.

Covers:
- list_trusted_printers — happy path, auth failure, unexpected error
- trust_printer — happy path, validation error, auth failure
- untrust_printer — happy path, not found, auth failure
- acquire_printer_lock — happy path, lock timeout, auth failure
- release_printer_lock — happy path, error, auth failure
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_printer_mgmt_tools() -> dict:
    """Register the plugin on a mock MCP and return captured tool functions."""
    from kiln.plugins.printer_management_tools import plugin

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
def mgmt_tools():
    return _register_printer_mgmt_tools()


def _auth_error() -> dict:
    return {
        "success": False,
        "error": {
            "code": "AUTH_ERROR",
            "message": "Authentication failed.",
            "retryable": False,
        },
    }


# ---------------------------------------------------------------------------
# TestListTrustedPrinters
# ---------------------------------------------------------------------------


class TestListTrustedPrinters:
    """Tests for list_trusted_printers."""

    @patch("kiln.cli.config.get_trusted_printers")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_trusted, mgmt_tools):
        mock_trusted.return_value = ["192.168.1.10", "printer.local"]

        result = mgmt_tools["list_trusted_printers"]()

        assert result["success"] is True
        assert result["count"] == 2

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, mgmt_tools):
        result = mgmt_tools["list_trusted_printers"]()

        assert result["success"] is False

    @patch("kiln.cli.config.get_trusted_printers")
    @patch("kiln.server._check_auth", return_value=None)
    def test_unexpected_error(self, _auth, mock_trusted, mgmt_tools):
        mock_trusted.side_effect = RuntimeError("config error")

        result = mgmt_tools["list_trusted_printers"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestTrustPrinter
# ---------------------------------------------------------------------------


class TestTrustPrinter:
    """Tests for trust_printer."""

    @patch("kiln.cli.config.add_trusted_printer")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_add, mgmt_tools):
        result = mgmt_tools["trust_printer"](host="192.168.1.10")

        assert result["success"] is True
        assert result["host"] == "192.168.1.10"
        mock_add.assert_called_once_with("192.168.1.10")

    @patch("kiln.cli.config.add_trusted_printer")
    @patch("kiln.server._check_auth", return_value=None)
    def test_validation_error(self, _auth, mock_add, mgmt_tools):
        mock_add.side_effect = ValueError("already trusted")

        result = mgmt_tools["trust_printer"](host="192.168.1.10")

        assert result["success"] is False

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, mgmt_tools):
        result = mgmt_tools["trust_printer"](host="192.168.1.10")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestUntrustPrinter
# ---------------------------------------------------------------------------


class TestUntrustPrinter:
    """Tests for untrust_printer."""

    @patch("kiln.cli.config.remove_trusted_printer")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_remove, mgmt_tools):
        result = mgmt_tools["untrust_printer"](host="192.168.1.10")

        assert result["success"] is True
        assert result["host"] == "192.168.1.10"

    @patch("kiln.cli.config.remove_trusted_printer")
    @patch("kiln.server._check_auth", return_value=None)
    def test_not_found(self, _auth, mock_remove, mgmt_tools):
        mock_remove.side_effect = ValueError("not in list")

        result = mgmt_tools["untrust_printer"](host="192.168.1.10")

        assert result["success"] is False

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, mgmt_tools):
        result = mgmt_tools["untrust_printer"](host="192.168.1.10")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestAcquirePrinterLock
# ---------------------------------------------------------------------------


class TestAcquirePrinterLock:
    """Tests for acquire_printer_lock."""

    @patch("kiln.state_lock.get_state_lock_manager", create=True)
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_mgr, mgmt_tools):
        mock_mgr.return_value.acquire.return_value = True

        result = mgmt_tools["acquire_printer_lock"](printer_name="ender3")

        assert result["success"] is True
        assert result["locked"] is True

    @patch("kiln.state_lock.get_state_lock_manager", create=True)
    @patch("kiln.server._check_auth", return_value=None)
    def test_lock_timeout(self, _auth, mock_mgr, mgmt_tools):
        mock_mgr.return_value.acquire.return_value = False

        result = mgmt_tools["acquire_printer_lock"](
            printer_name="ender3",
            timeout_seconds=1.0,
        )

        assert result["success"] is False

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, mgmt_tools):
        result = mgmt_tools["acquire_printer_lock"](printer_name="ender3")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestReleasePrinterLock
# ---------------------------------------------------------------------------


class TestReleasePrinterLock:
    """Tests for release_printer_lock."""

    @patch("kiln.state_lock.get_state_lock_manager", create=True)
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_mgr, mgmt_tools):
        mock_mgr.return_value.release.return_value = True

        result = mgmt_tools["release_printer_lock"](printer_name="ender3")

        assert result["success"] is True
        assert result["released"] is True

    @patch("kiln.state_lock.get_state_lock_manager", create=True)
    @patch("kiln.server._check_auth", return_value=None)
    def test_error(self, _auth, mock_mgr, mgmt_tools):
        mock_mgr.return_value.release.side_effect = RuntimeError("boom")

        result = mgmt_tools["release_printer_lock"](printer_name="ender3")

        assert result["success"] is False

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, mgmt_tools):
        result = mgmt_tools["release_printer_lock"](printer_name="ender3")

        assert result["success"] is False
