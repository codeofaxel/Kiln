"""Tests for the Stripe setup flow — setter, polling, config loading."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

try:
    from kiln_pro.payments.stripe_provider import StripeProvider as _check  # noqa: F401
except ImportError:
    pytest.skip("kiln-pro payments module not available", allow_module_level=True)

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
    customer_id: str | None = "cus_test",
    payment_method_id: str | None = None,
) -> Any:
    """Create a StripeProvider without hitting real Stripe."""
    from kiln_pro.payments.stripe_provider import StripeProvider

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
    """The card form is a Stripe-hosted Checkout Session, not a raw SetupIntent.

    ``create_setup_url`` mints a Checkout Session in ``mode="setup"`` so the
    card number never transits or rests on a Kiln surface.  The session
    carries the SetupIntent id, which is what ``poll_setup_intent`` later
    confirms against -- so the mock has to be a session, not an intent.
    """

    def test_stores_setup_intent_id(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_test_123",
            setup_intent="seti_stored",
            url="https://checkout.stripe.com/c/pay/cs_test_123",
        )

        p = _make_provider()
        assert p._pending_setup_intent_id is None
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            url = p.create_setup_url()

        assert p._pending_setup_intent_id == "seti_stored"
        assert p._pending_setup_session_id == "cs_test_123"
        assert url == "https://checkout.stripe.com/c/pay/cs_test_123"
        # mode="setup" is what attaches the card for off_session reuse --
        # fulfillment fees charge without the user present.
        assert mock_stripe.checkout.Session.create.call_args.kwargs["mode"] == "setup"

    def test_recovers_intent_id_from_session_when_absent(self):
        """A session may not expose its SetupIntent at creation time.

        When it doesn't, the id is recovered from the session on first
        poll rather than the setup being silently unconfirmable.
        """
        mock_stripe = _build_mock_stripe()
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_test_456",
            setup_intent=None,
            url="https://checkout.stripe.com/c/pay/cs_test_456",
        )
        mock_stripe.checkout.Session.retrieve.return_value = MagicMock(
            setup_intent="seti_recovered"
        )
        si = MagicMock()
        si.status = "succeeded"
        si.payment_method = "pm_recovered"
        mock_stripe.SetupIntent.retrieve.return_value = si

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_setup_url()
            assert p._pending_setup_intent_id is None
            result = p.poll_setup_intent()

        assert result == "pm_recovered"
        assert p._pending_setup_intent_id == "seti_recovered"
        mock_stripe.checkout.Session.retrieve.assert_called_once_with("cs_test_456")


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
