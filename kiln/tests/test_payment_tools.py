"""Integration tests for payment-related MCP tools in server.py.

Covers:
- check_payment_status: happy path, not found, auth rejection, unexpected error
- billing_check_setup: success, pending, no provider, unsupported, unexpected error
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiln.payments.base import (
    Currency,
    PaymentRail,
    PaymentResult,
    PaymentStatus,
)


# ---------------------------------------------------------------------------
# check_payment_status
# ---------------------------------------------------------------------------


class TestCheckPaymentStatus:
    """Integration tests for the check_payment_status MCP tool."""

    def test_happy_path_returns_status(self):
        from kiln.server import check_payment_status

        fake_result = PaymentResult(
            success=True,
            payment_id="pay_abc",
            status=PaymentStatus.COMPLETED,
            amount=5.0,
            currency=Currency.USD,
            rail=PaymentRail.STRIPE,
            tx_hash="tx_123",
        )
        mock_provider = MagicMock()
        mock_provider.get_payment_status.return_value = fake_result

        mock_mgr = MagicMock()
        mock_mgr.available_rails = ["stripe"]
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = check_payment_status(payment_id="pay_abc")

        assert result["success"] is True
        assert result["payment_id"] == "pay_abc"
        assert result["status"] == "completed"
        assert result["amount"] == 5.0
        assert result["currency"] == "USD"
        assert result["rail"] == "stripe"
        assert result["tx_hash"] == "tx_123"
        assert result["provider"] == "stripe"

    def test_not_found_on_any_provider(self):
        from kiln.server import check_payment_status

        mock_provider = MagicMock()
        mock_provider.get_payment_status.side_effect = Exception("unknown id")

        mock_mgr = MagicMock()
        mock_mgr.available_rails = ["stripe", "circle"]
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = check_payment_status(payment_id="pay_nonexistent")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "pay_nonexistent" in result["error"]["message"]

    def test_provider_returns_none_is_skipped(self):
        """When get_provider returns None for a rail, that rail is skipped."""
        from kiln.server import check_payment_status

        mock_mgr = MagicMock()
        mock_mgr.available_rails = ["stripe"]
        mock_mgr.get_provider.return_value = None

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = check_payment_status(payment_id="pay_xyz")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    def test_first_provider_fails_second_succeeds(self):
        """Iterates providers until one recognises the payment ID."""
        from kiln.server import check_payment_status

        fake_result = PaymentResult(
            success=True,
            payment_id="pay_multi",
            status=PaymentStatus.PROCESSING,
            amount=10.0,
            currency=Currency.USDC,
            rail=PaymentRail.SOLANA,
        )

        provider_a = MagicMock()
        provider_a.get_payment_status.side_effect = Exception("nope")
        provider_b = MagicMock()
        provider_b.get_payment_status.return_value = fake_result

        mock_mgr = MagicMock()
        mock_mgr.available_rails = ["stripe", "circle"]
        mock_mgr.get_provider.side_effect = lambda name: (
            provider_a if name == "stripe" else provider_b
        )

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = check_payment_status(payment_id="pay_multi")

        assert result["success"] is True
        assert result["payment_id"] == "pay_multi"
        assert result["status"] == "processing"
        assert result["provider"] == "circle"

    def test_auth_rejection(self):
        from kiln.server import check_payment_status

        auth_err = {"success": False, "error": {"code": "AUTH", "message": "denied"}}
        with patch("kiln.server._check_auth", return_value=auth_err):
            result = check_payment_status(payment_id="pay_1")

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH"

    def test_unexpected_error(self):
        from kiln.server import check_payment_status

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", side_effect=RuntimeError("boom")):
            result = check_payment_status(payment_id="pay_err")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert "boom" in result["error"]["message"]

    def test_result_without_rail_uses_provider_name(self):
        """When PaymentResult.rail is None, the provider name is used."""
        from kiln.server import check_payment_status

        fake_result = PaymentResult(
            success=True,
            payment_id="pay_norail",
            status=PaymentStatus.COMPLETED,
            amount=2.0,
            currency=Currency.USD,
            rail=PaymentRail.STRIPE,
        )
        # Override rail to None after construction to simulate missing rail
        object.__setattr__(fake_result, "rail", None)

        mock_provider = MagicMock()
        mock_provider.get_payment_status.return_value = fake_result

        mock_mgr = MagicMock()
        mock_mgr.available_rails = ["custom_provider"]
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = check_payment_status(payment_id="pay_norail")

        assert result["success"] is True
        assert result["rail"] == "custom_provider"


# ---------------------------------------------------------------------------
# billing_check_setup
# ---------------------------------------------------------------------------


class TestBillingCheckSetup:
    """Integration tests for the billing_check_setup MCP tool."""

    def test_success_activates_payment_method(self):
        from kiln.server import billing_check_setup

        mock_provider = MagicMock()
        mock_provider.poll_setup_intent.return_value = "pm_card_123"
        mock_provider._customer_id = "cus_abc"

        mock_mgr = MagicMock()
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr), \
             patch("kiln.cli.config.save_billing_config") as mock_save:
            result = billing_check_setup()

        assert result["success"] is True
        assert result["status"] == "active"
        assert result["payment_method_id"] == "pm_card_123"
        mock_provider.set_payment_method.assert_called_once_with("pm_card_123")
        mock_save.assert_called_once()
        saved_config = mock_save.call_args[0][0]
        assert saved_config["stripe_payment_method_id"] == "pm_card_123"
        assert saved_config["stripe_customer_id"] == "cus_abc"

    def test_pending_returns_not_complete(self):
        from kiln.server import billing_check_setup

        mock_provider = MagicMock()
        mock_provider.poll_setup_intent.return_value = None

        mock_mgr = MagicMock()
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = billing_check_setup()

        assert result["success"] is False
        assert result["status"] == "pending"
        assert "not yet complete" in result["message"].lower()

    def test_no_stripe_provider(self):
        from kiln.server import billing_check_setup

        mock_mgr = MagicMock()
        mock_mgr.get_provider.return_value = None

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = billing_check_setup()

        assert result["success"] is False
        assert result["error"]["code"] == "NO_PROVIDER"

    def test_provider_without_poll_setup_intent(self):
        from kiln.server import billing_check_setup

        mock_provider = MagicMock(spec=[])  # no methods at all

        mock_mgr = MagicMock()
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = billing_check_setup()

        assert result["success"] is False
        assert result["error"]["code"] == "UNSUPPORTED"

    def test_get_payment_mgr_failure(self):
        """When _get_payment_mgr raises, the tool returns INTERNAL_ERROR."""
        from kiln.server import billing_check_setup

        with patch("kiln.server._get_payment_mgr", side_effect=RuntimeError("config missing")):
            result = billing_check_setup()

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"

    def test_unexpected_error_in_poll(self):
        from kiln.server import billing_check_setup

        mock_provider = MagicMock()
        mock_provider.poll_setup_intent.side_effect = RuntimeError("stripe down")

        mock_mgr = MagicMock()
        mock_mgr.get_provider.return_value = mock_provider

        with patch("kiln.server._check_auth", return_value=None), \
             patch("kiln.server._get_payment_mgr", return_value=mock_mgr):
            result = billing_check_setup()

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
