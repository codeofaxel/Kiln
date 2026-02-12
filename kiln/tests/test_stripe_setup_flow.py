"""Tests for the Stripe setup flow — setter, polling, config loading."""

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
# set_payment_method
# ---------------------------------------------------------------------------


class TestSetPaymentMethod:
    def test_set_payment_method(self):
        p = _make_provider(payment_method_id=None)
        assert p._payment_method_id is None
        p.set_payment_method("pm_new_card")
        assert p._payment_method_id == "pm_new_card"

    def test_set_payment_method_overwrites(self):
        p = _make_provider(payment_method_id="pm_old")
        p.set_payment_method("pm_new")
        assert p._payment_method_id == "pm_new"


# ---------------------------------------------------------------------------
# poll_setup_intent
# ---------------------------------------------------------------------------


class TestPollSetupIntent:
    def test_poll_setup_intent_succeeded(self):
        mock_stripe = _build_mock_stripe()
        si = MagicMock()
        si.status = "succeeded"
        si.payment_method = "pm_from_setup"
        mock_stripe.SetupIntent.retrieve.return_value = si

        p = _make_provider()
        p._pending_setup_intent_id = "seti_abc"
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.poll_setup_intent()

        assert result == "pm_from_setup"
        mock_stripe.SetupIntent.retrieve.assert_called_once_with("seti_abc")

    def test_poll_setup_intent_not_ready(self):
        mock_stripe = _build_mock_stripe()
        si = MagicMock()
        si.status = "requires_payment_method"
        si.payment_method = None
        mock_stripe.SetupIntent.retrieve.return_value = si

        p = _make_provider()
        p._pending_setup_intent_id = "seti_pending"
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.poll_setup_intent()

        assert result is None

    def test_poll_setup_intent_no_id(self):
        p = _make_provider()
        assert p._pending_setup_intent_id is None
        result = p.poll_setup_intent()
        assert result is None

    def test_poll_setup_intent_explicit_id(self):
        mock_stripe = _build_mock_stripe()
        si = MagicMock()
        si.status = "succeeded"
        si.payment_method = "pm_explicit"
        mock_stripe.SetupIntent.retrieve.return_value = si

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.poll_setup_intent(setup_intent_id="seti_explicit")

        assert result == "pm_explicit"
        mock_stripe.SetupIntent.retrieve.assert_called_once_with("seti_explicit")

    def test_poll_setup_intent_exception_returns_none(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.retrieve.side_effect = Exception("API error")

        p = _make_provider()
        p._pending_setup_intent_id = "seti_err"
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.poll_setup_intent()

        assert result is None


# ---------------------------------------------------------------------------
# create_setup_url stores pending ID
# ---------------------------------------------------------------------------


class TestCreateSetupUrlStoresPendingId:
    def test_stores_setup_intent_id(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.create.return_value = MagicMock(
            id="seti_stored", client_secret="secret_val"
        )

        p = _make_provider()
        assert p._pending_setup_intent_id is None
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_setup_url()

        assert p._pending_setup_intent_id == "seti_stored"


# ---------------------------------------------------------------------------
# _get_payment_mgr loads payment_method_id from config
# ---------------------------------------------------------------------------


class TestPaymentMethodLoadedFromConfig:
    def test_payment_method_loaded_from_config(self, monkeypatch):
        """Verify _get_payment_mgr passes stripe_payment_method_id from config."""
        monkeypatch.setenv("KILN_STRIPE_SECRET_KEY", "sk_test_from_config")

        mock_config = {
            "stripe_customer_id": "cus_cfg",
            "stripe_payment_method_id": "pm_cfg_loaded",
            "user_id": "user-1",
        }

        # We need to patch the config loader and other deps.
        with patch("kiln.server.get_db") as mock_db, \
             patch("kiln.server._event_bus", new=MagicMock()), \
             patch("kiln.server._billing", new=MagicMock()):

            mock_db.return_value = MagicMock()

            # Reset the cached manager.
            import kiln.server as srv
            old_mgr = srv._payment_mgr
            srv._payment_mgr = None

            try:
                with patch("kiln.cli.config.get_billing_config", return_value=mock_config):
                    mgr = srv._get_payment_mgr()

                provider = mgr.get_provider("stripe")
                assert provider is not None
                assert provider._payment_method_id == "pm_cfg_loaded"
                assert provider._customer_id == "cus_cfg"
            finally:
                srv._payment_mgr = old_mgr
