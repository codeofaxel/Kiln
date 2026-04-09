"""Tests for service_tools.py plugin MCP tools.

Covers:
- create_print_service_order — happy path, auth failure, validation error, unexpected error
- print_service_quote — happy path, auth failure, validation error
- print_service_status — found, not found, unexpected error
- cancel_print_service_order — happy path, auth failure, not found
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_service_tools() -> dict:
    """Register the plugin on a mock MCP and return captured tool functions."""
    from kiln.plugins.service_tools import plugin

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
def service_tools():
    return _register_service_tools()


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
# TestCreatePrintServiceOrder
# ---------------------------------------------------------------------------


class TestCreatePrintServiceOrder:
    """Tests for create_print_service_order."""

    @patch("kiln.print_service.create_print_order")
    @patch("kiln.print_service.PrintServiceRequest")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_req_cls, mock_create, service_tools):
        quote = MagicMock()
        quote.order_id = "ord-1"
        quote.recommended = "local"
        quote.total_cost_usd = 5.99
        quote.estimated_time_hours = 2.0
        quote.to_dict.return_value = {"order_id": "ord-1", "status": "quoted"}
        mock_create.return_value = quote

        result = service_tools["create_print_service_order"](
            model_path="/tmp/model.stl",
        )

        assert result["success"] is True
        assert "ord-1" in result["message"]

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, service_tools):
        result = service_tools["create_print_service_order"](
            model_path="/tmp/model.stl",
        )

        assert result["success"] is False

    @patch("kiln.print_service.create_print_order")
    @patch("kiln.print_service.PrintServiceRequest")
    @patch("kiln.server._check_auth", return_value=None)
    def test_validation_error(self, _auth, mock_req_cls, mock_create, service_tools):
        mock_create.side_effect = ValueError("Must provide model_path, model_url, or prompt")

        result = service_tools["create_print_service_order"]()

        assert result["success"] is False

    @patch("kiln.print_service.create_print_order")
    @patch("kiln.print_service.PrintServiceRequest")
    @patch("kiln.server._check_auth", return_value=None)
    def test_unexpected_error(self, _auth, mock_req_cls, mock_create, service_tools):
        mock_create.side_effect = RuntimeError("boom")

        result = service_tools["create_print_service_order"](
            model_path="/tmp/model.stl",
        )

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestPrintServiceQuote
# ---------------------------------------------------------------------------


class TestPrintServiceQuote:
    """Tests for print_service_quote."""

    @patch("kiln.print_service.confirm_print_order")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_confirm, service_tools):
        order = MagicMock()
        order.status = "confirmed"
        order.to_dict.return_value = {"order_id": "ord-1", "status": "confirmed"}
        mock_confirm.return_value = order

        result = service_tools["print_service_quote"](order_id="ord-1")

        assert result["success"] is True
        assert "confirmed" in result["message"]

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, service_tools):
        result = service_tools["print_service_quote"](order_id="ord-1")

        assert result["success"] is False

    @patch("kiln.print_service.confirm_print_order")
    @patch("kiln.server._check_auth", return_value=None)
    def test_validation_error(self, _auth, mock_confirm, service_tools):
        mock_confirm.side_effect = ValueError("Order not found")

        result = service_tools["print_service_quote"](order_id="bad")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestPrintServiceStatus
# ---------------------------------------------------------------------------


class TestPrintServiceStatus:
    """Tests for print_service_status."""

    @patch("kiln.print_service.get_order_status")
    @patch("kiln.server._check_auth", return_value=None)
    def test_found(self, _auth, mock_status, service_tools):
        order = MagicMock()
        order.status = "printing"
        order.current_step = "slicing"
        order.to_dict.return_value = {"order_id": "ord-1", "status": "printing"}
        mock_status.return_value = order

        result = service_tools["print_service_status"](order_id="ord-1")

        assert result["success"] is True
        assert "printing" in result["message"]

    @patch("kiln.print_service.get_order_status")
    @patch("kiln.server._check_auth", return_value=None)
    def test_not_found(self, _auth, mock_status, service_tools):
        mock_status.side_effect = ValueError("Order not found")

        result = service_tools["print_service_status"](order_id="bad")

        assert result["success"] is False

    @patch("kiln.print_service.get_order_status")
    @patch("kiln.server._check_auth", return_value=None)
    def test_unexpected_error(self, _auth, mock_status, service_tools):
        mock_status.side_effect = RuntimeError("boom")

        result = service_tools["print_service_status"](order_id="ord-1")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestCancelPrintServiceOrder
# ---------------------------------------------------------------------------


class TestCancelPrintServiceOrder:
    """Tests for cancel_print_service_order."""

    @patch("kiln.print_service.cancel_order")
    @patch("kiln.server._check_auth", return_value=None)
    def test_happy_path(self, _auth, mock_cancel, service_tools):
        mock_cancel.return_value = {"success": True, "message": "Cancelled"}

        result = service_tools["cancel_print_service_order"](order_id="ord-1")

        assert result["success"] is True

    @patch("kiln.server._check_auth", return_value=_auth_error())
    def test_auth_failure(self, _auth, service_tools):
        result = service_tools["cancel_print_service_order"](order_id="ord-1")

        assert result["success"] is False

    @patch("kiln.print_service.cancel_order")
    @patch("kiln.server._check_auth", return_value=None)
    def test_not_found(self, _auth, mock_cancel, service_tools):
        mock_cancel.side_effect = ValueError("Order not found")

        result = service_tools["cancel_print_service_order"](order_id="bad")

        assert result["success"] is False
