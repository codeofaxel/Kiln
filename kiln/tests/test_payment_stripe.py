"""Tests for kiln.payments.stripe_provider — Stripe payment adapter."""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_mock_stripe() -> MagicMock:
    """Return a mock ``stripe`` module with the expected sub-objects."""
    mock = MagicMock()

    # stripe.error namespace — real exception classes so except clauses work
    StripeError = type("StripeError", (Exception,), {})
    CardError = type("CardError", (StripeError,), {})
    mock.error.StripeError = StripeError
    mock.error.CardError = CardError

    return mock


def _make_provider(
    secret_key: str = "sk_test_abc123",
    customer_id: Optional[str] = "cus_test",
    payment_method_id: Optional[str] = "pm_test",
) -> Any:
    """Create a StripeProvider without hitting real Stripe."""
    from kiln.payments.stripe_provider import StripeProvider

    return StripeProvider(
        secret_key=secret_key,
        customer_id=customer_id,
        payment_method_id=payment_method_id,
    )


def _payment_request(**overrides: Any) -> PaymentRequest:
    """Build a PaymentRequest with sensible defaults."""
    defaults: Dict[str, Any] = {
        "amount": 25.50,
        "currency": Currency.USD,
        "rail": PaymentRail.STRIPE,
        "job_id": "job-001",
        "description": "Test print job",
        "metadata": {"model": "benchy.stl"},
    }
    defaults.update(overrides)
    return PaymentRequest(**defaults)


def _mock_intent(
    id: str = "pi_test123",
    status: str = "succeeded",
    amount: int = 2550,
    currency: str = "usd",
) -> MagicMock:
    """Create a mock Stripe PaymentIntent object."""
    intent = MagicMock()
    intent.id = id
    intent.status = status
    intent.amount = amount
    intent.currency = currency
    return intent


def _mock_refund(
    id: str = "re_test123",
    status: str = "succeeded",
) -> MagicMock:
    """Create a mock Stripe Refund object."""
    refund = MagicMock()
    refund.id = id
    refund.status = status
    return refund


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_key_raises(self):
        from kiln.payments.stripe_provider import StripeProvider

        with pytest.raises(PaymentError, match="secret key required"):
            StripeProvider(secret_key="")

    def test_no_key_no_env_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_STRIPE_SECRET_KEY", raising=False)
        from kiln.payments.stripe_provider import StripeProvider

        with pytest.raises(PaymentError, match="secret key required"):
            StripeProvider()

    def test_env_var_key(self, monkeypatch):
        monkeypatch.setenv("KILN_STRIPE_SECRET_KEY", "sk_test_env")
        from kiln.payments.stripe_provider import StripeProvider

        p = StripeProvider()
        assert p._secret_key == "sk_test_env"

    def test_custom_key(self):
        p = _make_provider(secret_key="sk_test_custom")
        assert p._secret_key == "sk_test_custom"

    def test_customer_and_payment_method_stored(self):
        p = _make_provider(customer_id="cus_abc", payment_method_id="pm_xyz")
        assert p._customer_id == "cus_abc"
        assert p._payment_method_id == "pm_xyz"

    def test_defaults_none_for_customer_and_pm(self):
        p = _make_provider(customer_id=None, payment_method_id=None)
        assert p._customer_id is None
        assert p._payment_method_id is None

    def test_error_code_is_missing_key(self):
        from kiln.payments.stripe_provider import StripeProvider

        with pytest.raises(PaymentError) as exc_info:
            StripeProvider(secret_key="")
        assert exc_info.value.code == "MISSING_KEY"


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _make_provider().name == "stripe"

    def test_supported_currencies(self):
        currencies = _make_provider().supported_currencies
        assert Currency.USD in currencies
        assert Currency.EUR in currencies
        assert len(currencies) == 2

    def test_rail(self):
        assert _make_provider().rail == PaymentRail.STRIPE

    def test_repr_contains_key_hint(self):
        r = repr(_make_provider())
        assert "StripeProvider" in r
        assert "sk_test" in r

    def test_repr_contains_customer(self):
        r = repr(_make_provider(customer_id="cus_abc"))
        assert "cus_abc" in r

    def test_is_payment_provider_subclass(self):
        assert isinstance(_make_provider(), PaymentProvider)


# ---------------------------------------------------------------------------
# _import_stripe
# ---------------------------------------------------------------------------


class TestImportStripe:
    def test_import_error_gives_clear_message(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError, match="stripe package not installed"):
                p._import_stripe()

    def test_import_error_has_code(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError) as exc_info:
                p._import_stripe()
            assert exc_info.value.code == "MISSING_DEPENDENCY"

    def test_successful_import_sets_api_key(self):
        mock_stripe = _build_mock_stripe()
        p = _make_provider(secret_key="sk_test_xyz")
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p._import_stripe()
        assert result.api_key == "sk_test_xyz"


# ---------------------------------------------------------------------------
# create_setup_url
# ---------------------------------------------------------------------------


class TestCreateSetupUrl:
    def test_creates_customer_when_none(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_new")
        mock_stripe.SetupIntent.create.return_value = MagicMock(
            id="seti_123", client_secret="seti_123_secret_abc"
        )

        p = _make_provider(customer_id=None)
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            url = p.create_setup_url()

        mock_stripe.Customer.create.assert_called_once()
        assert p._customer_id == "cus_new"
        assert "seti_123_secret_abc" in url

    def test_reuses_existing_customer(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.create.return_value = MagicMock(
            id="seti_456", client_secret="seti_456_secret_def"
        )

        p = _make_provider(customer_id="cus_existing")
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            url = p.create_setup_url()

        mock_stripe.Customer.create.assert_not_called()
        assert p._customer_id == "cus_existing"

    def test_setup_intent_has_off_session_usage(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.create.return_value = MagicMock(
            id="seti_789", client_secret="secret"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_setup_url()

        call_kwargs = mock_stripe.SetupIntent.create.call_args.kwargs
        assert call_kwargs["usage"] == "off_session"

    def test_custom_return_url(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.create.return_value = MagicMock(
            id="seti_abc", client_secret="sec"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            url = p.create_setup_url(return_url="https://example.com/done")

        assert "return_url=https://example.com/done" in url

    def test_stripe_error_raises_payment_error(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.SetupIntent.create.side_effect = mock_stripe.error.StripeError(
            "API down"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Failed to create setup URL"):
                p.create_setup_url()


# ---------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------


class TestCreatePayment:
    def test_success(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent(
            id="pi_ok", status="succeeded", amount=2550
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.create_payment(_payment_request())

        assert result.success is True
        assert result.payment_id == "pi_ok"
        assert result.status == PaymentStatus.COMPLETED
        assert result.amount == 25.50
        assert result.currency == Currency.USD
        assert result.rail == PaymentRail.STRIPE

    def test_amount_converted_to_cents(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_payment(_payment_request(amount=99.99))

        call_kwargs = mock_stripe.PaymentIntent.create.call_args.kwargs
        assert call_kwargs["amount"] == 9999

    def test_small_amount_cents_rounding(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_payment(_payment_request(amount=0.01))

        call_kwargs = mock_stripe.PaymentIntent.create.call_args.kwargs
        assert call_kwargs["amount"] == 1

    def test_processing_status(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent(
            status="processing"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.create_payment(_payment_request())

        assert result.success is False
        assert result.status == PaymentStatus.PROCESSING

    def test_card_declined(self):
        mock_stripe = _build_mock_stripe()
        card_err = mock_stripe.error.CardError("Your card was declined.")
        card_err.payment_intent = {"id": "pi_declined"}
        mock_stripe.PaymentIntent.create.side_effect = card_err

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.create_payment(_payment_request())

        assert result.success is False
        assert result.status == PaymentStatus.FAILED
        assert "declined" in result.error.lower()
        assert result.payment_id == "pi_declined"

    def test_card_error_no_intent_id(self):
        mock_stripe = _build_mock_stripe()
        card_err = mock_stripe.error.CardError("Insufficient funds.")
        card_err.payment_intent = None
        mock_stripe.PaymentIntent.create.side_effect = card_err

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.create_payment(_payment_request())

        assert result.success is False
        assert result.payment_id == ""

    def test_stripe_error_raises_payment_error(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.side_effect = (
            mock_stripe.error.StripeError("Network issue")
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Stripe error creating payment"):
                p.create_payment(_payment_request())

    def test_no_customer_raises(self):
        p = _make_provider(customer_id=None, payment_method_id="pm_test")
        mock_stripe = _build_mock_stripe()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Customer and payment method"):
                p.create_payment(_payment_request())

    def test_no_payment_method_raises(self):
        p = _make_provider(customer_id="cus_test", payment_method_id=None)
        mock_stripe = _build_mock_stripe()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Customer and payment method"):
                p.create_payment(_payment_request())

    def test_off_session_and_confirm_flags(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_payment(_payment_request())

        call_kwargs = mock_stripe.PaymentIntent.create.call_args.kwargs
        assert call_kwargs["off_session"] is True
        assert call_kwargs["confirm"] is True

    def test_metadata_includes_job_id(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_payment(_payment_request(job_id="job-xyz"))

        call_kwargs = mock_stripe.PaymentIntent.create.call_args.kwargs
        assert call_kwargs["metadata"]["job_id"] == "job-xyz"

    def test_eur_currency(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.create.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.create_payment(_payment_request(currency=Currency.EUR))

        call_kwargs = mock_stripe.PaymentIntent.create.call_args.kwargs
        assert call_kwargs["currency"] == "eur"


# ---------------------------------------------------------------------------
# get_payment_status
# ---------------------------------------------------------------------------


class TestGetPaymentStatus:
    def test_succeeded(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            id="pi_done", status="succeeded", amount=5000, currency="usd"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_done")

        assert result.success is True
        assert result.status == PaymentStatus.COMPLETED
        assert result.amount == 50.00
        assert result.currency == Currency.USD

    def test_processing(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            status="processing"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.status == PaymentStatus.PROCESSING
        assert result.success is False

    def test_failed_requires_payment_method(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            status="requires_payment_method"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    def test_failed_requires_action(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            status="requires_action"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.status == PaymentStatus.FAILED

    def test_canceled(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            status="canceled"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.status == PaymentStatus.FAILED

    def test_unknown_status_defaults_to_pending(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            status="requires_confirmation"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.status == PaymentStatus.PENDING

    def test_stripe_error_raises_payment_error(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.side_effect = (
            mock_stripe.error.StripeError("Not found")
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Failed to retrieve payment"):
                p.get_payment_status("pi_nonexistent")

    def test_eur_currency_mapping(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            amount=1200, currency="eur"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.get_payment_status("pi_test")

        assert result.currency == Currency.EUR
        assert result.amount == 12.00


# ---------------------------------------------------------------------------
# refund_payment
# ---------------------------------------------------------------------------


class TestRefundPayment:
    def test_success(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Refund.create.return_value = _mock_refund(
            id="re_ok", status="succeeded"
        )
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            id="pi_refund", amount=2550, currency="usd"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.refund_payment("pi_refund")

        assert result.success is True
        assert result.status == PaymentStatus.REFUNDED
        assert result.amount == 25.50
        assert result.payment_id == "pi_refund"

    def test_pending_refund(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Refund.create.return_value = _mock_refund(status="pending")
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            amount=1000, currency="usd"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.refund_payment("pi_test")

        assert result.success is True
        assert result.status == PaymentStatus.PROCESSING

    def test_failed_refund(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Refund.create.return_value = _mock_refund(status="failed")
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent(
            amount=1000, currency="usd"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            result = p.refund_payment("pi_test")

        assert result.success is False
        assert result.status == PaymentStatus.FAILED

    def test_stripe_error_raises_payment_error(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Refund.create.side_effect = mock_stripe.error.StripeError(
            "Already refunded"
        )

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            with pytest.raises(PaymentError, match="Failed to refund"):
                p.refund_payment("pi_test")

    def test_refund_passes_payment_intent_id(self):
        mock_stripe = _build_mock_stripe()
        mock_stripe.Refund.create.return_value = _mock_refund()
        mock_stripe.PaymentIntent.retrieve.return_value = _mock_intent()

        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": mock_stripe}):
            p.refund_payment("pi_specific")

        mock_stripe.Refund.create.assert_called_once_with(
            payment_intent="pi_specific"
        )


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    """Verify all documented Stripe statuses map correctly."""

    def test_succeeded_maps_to_completed(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP["succeeded"] == PaymentStatus.COMPLETED

    def test_processing_maps_to_processing(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP["processing"] == PaymentStatus.PROCESSING

    def test_requires_payment_method_maps_to_failed(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP["requires_payment_method"] == PaymentStatus.FAILED

    def test_requires_action_maps_to_failed(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP["requires_action"] == PaymentStatus.FAILED

    def test_canceled_maps_to_failed(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP["canceled"] == PaymentStatus.FAILED

    def test_unknown_status_defaults_to_pending(self):
        from kiln.payments.stripe_provider import _STATUS_MAP

        assert _STATUS_MAP.get("some_future_status", PaymentStatus.PENDING) == PaymentStatus.PENDING


# ---------------------------------------------------------------------------
# Import error integration
# ---------------------------------------------------------------------------


class TestImportError:
    """Verify clear errors when the stripe package is missing."""

    def test_create_payment_without_stripe(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError, match="stripe package not installed"):
                p.create_payment(_payment_request())

    def test_get_payment_status_without_stripe(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError, match="stripe package not installed"):
                p.get_payment_status("pi_test")

    def test_refund_payment_without_stripe(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError, match="stripe package not installed"):
                p.refund_payment("pi_test")

    def test_create_setup_url_without_stripe(self):
        p = _make_provider()
        with patch.dict(sys.modules, {"stripe": None}):
            with pytest.raises(PaymentError, match="stripe package not installed"):
                p.create_setup_url()


# ---------------------------------------------------------------------------
# Abstract base verification
# ---------------------------------------------------------------------------


class TestAbstractBase:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            PaymentProvider()

    def test_payment_error_has_code(self):
        e = PaymentError("test error", code="TEST_CODE")
        assert e.code == "TEST_CODE"
        assert str(e) == "test error"

    def test_payment_error_code_defaults_to_none(self):
        e = PaymentError("simple error")
        assert e.code is None
