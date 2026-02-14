"""Tests for Stripe Checkout Session creation."""

from __future__ import annotations

import sys
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from kiln.payments.base import PaymentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_mock_stripe() -> MagicMock:
    """Return a mock ``stripe`` module with the expected sub-objects."""
    mock = MagicMock()
    StripeError = type("StripeError", (Exception,), {})
    CardError = type("CardError", (StripeError,), {})
    mock.error.StripeError = StripeError
    mock.error.CardError = CardError
    return mock


def _make_provider(
    secret_key: str = "sk_test_abc123",
    customer_id: Optional[str] = "cus_test",
    payment_method_id: Optional[str] = None,
) -> Any:
    """Create a StripeProvider without hitting real Stripe."""
    from kiln.payments.stripe_provider import StripeProvider

    return StripeProvider(
        secret_key=secret_key,
        customer_id=customer_id,
        payment_method_id=payment_method_id,
    )


# ---------------------------------------------------------------------------
# create_checkout_session
# ---------------------------------------------------------------------------


class TestCreateCheckoutSession:
    def test_creates_session(self):
        mock_stripe = _build_mock_stripe()
        mock_session = MagicMock(
            id="cs_test123",
            url="https://checkout.stripe.com/c/pay/cs_test123",
        )
        mock_stripe.checkout.Session.create.return_value = mock_session

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_checkout_session(price_id="price_abc")

        mock_stripe.checkout.Session.create.assert_called_once_with(
            mode="payment",
            line_items=[{"price": "price_abc", "quantity": 1}],
            success_url="https://kiln3d.com/pro/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://kiln3d.com/pricing",
            customer_email=None,
            metadata={},
        )

    def test_returns_session_id_and_url(self):
        mock_stripe = _build_mock_stripe()
        mock_session = MagicMock(
            id="cs_test456",
            url="https://checkout.stripe.com/c/pay/cs_test456",
        )
        mock_stripe.checkout.Session.create.return_value = mock_session

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.create_checkout_session(price_id="price_abc")

        assert result["session_id"] == "cs_test456"
        assert result["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test456"

    def test_email_passed_through(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_email", url="https://checkout.stripe.com/c/pay/cs_email"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_checkout_session(
                price_id="price_abc", customer_email="test@example.com"
            )

        call_kwargs = mock_stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["customer_email"] == "test@example.com"

    def test_metadata_passed_through(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_meta", url="https://checkout.stripe.com/c/pay/cs_meta"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_checkout_session(
                price_id="price_abc", metadata={"tier": "pro"}
            )

        call_kwargs = mock_stripe.checkout.Session.create.call_args[1]
        assert call_kwargs["metadata"] == {"tier": "pro"}

    def test_stripe_error_raises_payment_error(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.checkout.Session.create.side_effect = (
            mock_stripe.error.StripeError("API connection error")
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Failed to create checkout session"):
                p.create_checkout_session(price_id="price_abc")
