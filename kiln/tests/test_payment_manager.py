"""Tests for kiln.payments.manager.PaymentManager."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from kiln.billing import BillingLedger, FeeCalculation, FeePolicy, SpendLimits
from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)
from kiln.payments.manager import PaymentManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeProvider(PaymentProvider):
    """Minimal provider for testing."""

    def __init__(
        self,
        *,
        name: str = "fake",
        rail: PaymentRail = PaymentRail.STRIPE,
        currencies: Optional[list] = None,
        result: Optional[PaymentResult] = None,
        error: Optional[PaymentError] = None,
    ):
        self._name = name
        self._rail = rail
        self._currencies = currencies or [Currency.USD]
        self._result = result
        self._error = error
        self.calls: List[PaymentRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_currencies(self) -> list:
        return self._currencies

    @property
    def rail(self) -> PaymentRail:
        return self._rail

    def create_payment(self, request: PaymentRequest) -> PaymentResult:
        self.calls.append(request)
        if self._error:
            raise self._error
        return self._result or PaymentResult(
            success=True,
            payment_id="pay_123",
            status=PaymentStatus.COMPLETED,
            amount=request.amount,
            currency=request.currency,
            rail=self._rail,
        )

    def get_payment_status(self, payment_id: str) -> PaymentResult:
        return PaymentResult(
            success=True,
            payment_id=payment_id,
            status=PaymentStatus.COMPLETED,
            amount=0,
            currency=Currency.USD,
            rail=self._rail,
        )

    def refund_payment(self, payment_id: str) -> PaymentResult:
        return PaymentResult(
            success=True,
            payment_id=payment_id,
            status=PaymentStatus.REFUNDED,
            amount=0,
            currency=Currency.USD,
            rail=self._rail,
        )


class FakeDB:
    """Minimal KilnDB stand-in."""

    def __init__(self):
        self.payments: List[Dict] = []
        self.methods: List[Dict] = []

    def save_payment(self, payment: Dict[str, Any]) -> None:
        self.payments.append(payment)

    def update_payment_status(self, payment_id, status, tx_hash=None):
        pass

    def list_payment_methods(self, user_id: str) -> list:
        return [m for m in self.methods if m["user_id"] == user_id]

    def get_default_payment_method(self, user_id: str) -> Optional[dict]:
        for m in self.methods:
            if m["user_id"] == user_id and m.get("is_default"):
                return m
        return None

    def save_billing_charge(self, charge):
        pass

    def get_billing_charge(self, charge_id):
        return None

    def list_billing_charges(self, limit=50, month=None, year=None):
        return []

    def monthly_billing_summary(self, year=None, month=None):
        return {"total_fees": 0.0, "job_count": 0, "waived_count": 0}

    def billing_charges_this_month(self):
        return 0

    def monthly_fee_total(self):
        return 0.0


def _fee(amount=100.0, fee=5.0) -> FeeCalculation:
    return FeeCalculation(
        job_cost=amount,
        fee_amount=fee,
        fee_percent=5.0,
        total_cost=amount + fee,
        currency="USD",
        waived=False,
    )


def _waived_fee() -> FeeCalculation:
    return FeeCalculation(
        job_cost=50.0,
        fee_amount=0.0,
        fee_percent=0.0,
        total_cost=50.0,
        currency="USD",
        waived=True,
        waiver_reason="Free tier: job 1 of 5 free this month",
    )


@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def mgr(db, provider):
    m = PaymentManager(db=db, config={"default_rail": "fake"})
    m.register_provider(provider)
    return m


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_get(self, db):
        mgr = PaymentManager(db=db)
        p = FakeProvider(name="test")
        mgr.register_provider(p)
        assert mgr.get_provider("test") is p

    def test_available_rails(self, mgr, provider):
        assert "fake" in mgr.available_rails

    def test_get_nonexistent(self, db):
        mgr = PaymentManager(db=db)
        assert mgr.get_provider("nope") is None


# ---------------------------------------------------------------------------
# Rail resolution
# ---------------------------------------------------------------------------


class TestRailResolution:
    def test_configured_default(self, db, provider):
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        assert mgr.get_active_rail() == "fake"

    def test_falls_back_to_first(self, db, provider):
        mgr = PaymentManager(db=db, config={})
        mgr.register_provider(provider)
        assert mgr.get_active_rail() == "fake"

    def test_no_providers_raises(self, db):
        mgr = PaymentManager(db=db)
        with pytest.raises(PaymentError, match="No payment providers"):
            mgr.get_active_rail()

    def test_crypto_mapped_to_circle(self, db):
        circle = FakeProvider(name="circle", rail=PaymentRail.SOLANA)
        mgr = PaymentManager(db=db, config={"default_rail": "crypto"})
        mgr.register_provider(circle)
        assert mgr.get_active_rail() == "circle"


# ---------------------------------------------------------------------------
# Spend limits
# ---------------------------------------------------------------------------


class TestSpendLimits:
    def test_within_limits(self, mgr):
        ok, reason = mgr.check_spend_limits(5.0)
        assert ok is True
        assert reason is None

    def test_exceeds_per_order(self, db, provider):
        mgr = PaymentManager(
            db=db,
            config={"spend_limits": {"max_per_order_usd": 10.0}},
        )
        mgr.register_provider(provider)
        ok, reason = mgr.check_spend_limits(15.0)
        assert ok is False
        assert "per-order" in reason


# ---------------------------------------------------------------------------
# charge_fee
# ---------------------------------------------------------------------------


class TestChargeFee:
    def test_successful_charge(self, mgr, provider, db):
        result = mgr.charge_fee("job-1", _fee())
        assert result.success is True
        assert result.payment_id == "pay_123"
        assert len(provider.calls) == 1
        assert provider.calls[0].amount == 5.0
        # Payment persisted to DB
        assert len(db.payments) == 1
        assert db.payments[0]["status"] == "completed"

    def test_waived_fee_skips_payment(self, mgr, provider, db):
        result = mgr.charge_fee("job-2", _waived_fee())
        assert result.success is True
        assert len(provider.calls) == 0
        # No payment record since it was waived
        assert len(db.payments) == 0

    def test_zero_fee_skips_payment(self, mgr, provider, db):
        fee = FeeCalculation(
            job_cost=10.0, fee_amount=0.0, fee_percent=0.0,
            total_cost=10.0, currency="USD", waived=False,
        )
        result = mgr.charge_fee("job-3", fee)
        assert result.success is True
        assert len(provider.calls) == 0

    def test_payment_failure_raises(self, db):
        err_provider = FakeProvider(
            error=PaymentError("card declined", code="DECLINED"),
        )
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(err_provider)
        with pytest.raises(PaymentError, match="card declined"):
            mgr.charge_fee("job-fail", _fee())
        # Failed payment still persisted
        assert len(db.payments) == 1
        assert db.payments[0]["status"] == "failed"

    def test_spend_limit_blocks_charge(self, db, provider):
        mgr = PaymentManager(
            db=db,
            config={
                "default_rail": "fake",
                "spend_limits": {"max_per_order_usd": 1.0},
            },
        )
        mgr.register_provider(provider)
        with pytest.raises(PaymentError, match="Spend limit"):
            mgr.charge_fee("job-x", _fee())

    def test_explicit_rail_override(self, db):
        stripe = FakeProvider(name="stripe", rail=PaymentRail.STRIPE)
        circle = FakeProvider(name="circle", rail=PaymentRail.SOLANA,
                              currencies=[Currency.USDC])
        mgr = PaymentManager(db=db, config={"default_rail": "stripe"})
        mgr.register_provider(stripe)
        mgr.register_provider(circle)
        mgr.charge_fee("job-circ", _fee(), rail="circle")
        assert len(circle.calls) == 1
        assert len(stripe.calls) == 0

    def test_no_provider_registered(self, db):
        mgr = PaymentManager(db=db)
        with pytest.raises(PaymentError, match="No payment providers"):
            mgr.charge_fee("job-none", _fee())

    def test_unknown_provider_name(self, db, provider):
        mgr = PaymentManager(db=db)
        mgr.register_provider(provider)
        with pytest.raises(PaymentError, match="not registered"):
            mgr.charge_fee("job-bad", _fee(), rail="nonexistent")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_emits_payment_completed(self, db, provider):
        bus = MagicMock()
        mgr = PaymentManager(db=db, config={"default_rail": "fake"}, event_bus=bus)
        mgr.register_provider(provider)
        mgr.charge_fee("job-evt", _fee())
        # Should have emitted PAYMENT_INITIATED and PAYMENT_COMPLETED
        assert bus.publish.call_count >= 2

    def test_emits_payment_failed(self, db):
        bus = MagicMock()
        err_provider = FakeProvider(
            error=PaymentError("declined"),
        )
        mgr = PaymentManager(db=db, config={"default_rail": "fake"}, event_bus=bus)
        mgr.register_provider(err_provider)
        with pytest.raises(PaymentError):
            mgr.charge_fee("job-fail-evt", _fee())
        # Should have emitted PAYMENT_INITIATED and PAYMENT_FAILED
        assert bus.publish.call_count >= 2

    def test_emits_spend_limit_reached(self, db, provider):
        bus = MagicMock()
        mgr = PaymentManager(
            db=db,
            config={
                "default_rail": "fake",
                "spend_limits": {"max_per_order_usd": 1.0},
            },
            event_bus=bus,
        )
        mgr.register_provider(provider)
        with pytest.raises(PaymentError):
            mgr.charge_fee("job-limit", _fee())
        # Should have emitted SPEND_LIMIT_REACHED
        assert bus.publish.call_count >= 1

    def test_no_event_bus_no_error(self, db, provider):
        """Charging without an event bus should not raise."""
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        result = mgr.charge_fee("job-no-bus", _fee())
        assert result.success is True


# ---------------------------------------------------------------------------
# Setup URL
# ---------------------------------------------------------------------------


class TestSetupURL:
    def test_no_provider_raises(self, db):
        mgr = PaymentManager(db=db)
        with pytest.raises(PaymentError, match="not registered"):
            mgr.get_setup_url("stripe")

    def test_provider_without_setup_url(self, db, provider):
        mgr = PaymentManager(db=db)
        mgr.register_provider(provider)
        with pytest.raises(PaymentError, match="does not support"):
            mgr.get_setup_url("fake")

    def test_provider_with_setup_url(self, db):
        class SetupProvider(FakeProvider):
            def create_setup_url(self):
                return "https://example.com/setup"

        p = SetupProvider(name="setupable")
        mgr = PaymentManager(db=db)
        mgr.register_provider(p)
        url = mgr.get_setup_url("setupable")
        assert url == "https://example.com/setup"


# ---------------------------------------------------------------------------
# Billing status
# ---------------------------------------------------------------------------


class TestBillingStatus:
    def test_returns_expected_keys(self, mgr):
        data = mgr.get_billing_status("user-1")
        assert "user_id" in data
        assert "month_revenue" in data
        assert "fee_policy" in data
        assert "payment_methods" in data
        assert "available_rails" in data
        assert "spend_limits" in data

    def test_shows_default_method(self, mgr, db):
        db.methods.append({
            "id": "pm_1",
            "user_id": "user-1",
            "rail": "stripe",
            "provider_ref": "cus_123",
            "label": "Visa *4242",
            "is_default": True,
            "created_at": time.time(),
        })
        data = mgr.get_billing_status("user-1")
        assert data["default_payment_method"] is not None
        assert data["default_payment_method"]["label"] == "Visa *4242"

    def test_no_methods(self, mgr):
        data = mgr.get_billing_status("user-none")
        assert data["default_payment_method"] is None
        assert data["payment_methods"] == []


# ---------------------------------------------------------------------------
# Billing history
# ---------------------------------------------------------------------------


class TestBillingHistory:
    def test_returns_list(self, mgr):
        charges = mgr.get_billing_history(limit=10)
        assert isinstance(charges, list)

    def test_uses_limit(self, mgr):
        # With empty DB should return empty list
        charges = mgr.get_billing_history(limit=5)
        assert len(charges) <= 5


# ---------------------------------------------------------------------------
# Auth-and-capture flow
# ---------------------------------------------------------------------------


class AuthCaptureProvider(FakeProvider):
    """Provider that supports authorize/capture/cancel."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.auth_calls: List[PaymentRequest] = []
        self.capture_calls: List[str] = []
        self.cancel_calls: List[str] = []

    def authorize_payment(self, request: PaymentRequest) -> PaymentResult:
        self.auth_calls.append(request)
        return PaymentResult(
            success=True,
            payment_id="pi_hold_123",
            status=PaymentStatus.AUTHORIZED,
            amount=request.amount,
            currency=request.currency,
            rail=self._rail,
        )

    def capture_payment(self, payment_id: str) -> PaymentResult:
        self.capture_calls.append(payment_id)
        return PaymentResult(
            success=True,
            payment_id=payment_id,
            status=PaymentStatus.COMPLETED,
            amount=5.0,
            currency=Currency.USD,
            rail=self._rail,
        )

    def cancel_payment(self, payment_id: str) -> PaymentResult:
        self.cancel_calls.append(payment_id)
        return PaymentResult(
            success=True,
            payment_id=payment_id,
            status=PaymentStatus.CANCELLED,
            amount=5.0,
            currency=Currency.USD,
            rail=self._rail,
        )


class TestAuthorizeFee:
    def test_authorize_places_hold(self, db):
        p = AuthCaptureProvider(name="auth")
        mgr = PaymentManager(db=db, config={"default_rail": "auth"})
        mgr.register_provider(p)
        result = mgr.authorize_fee("quote-1", _fee())
        assert result.status == PaymentStatus.AUTHORIZED
        assert result.payment_id == "pi_hold_123"
        assert len(p.auth_calls) == 1

    def test_authorize_waived_skips_hold(self, db):
        p = AuthCaptureProvider(name="auth")
        mgr = PaymentManager(db=db, config={"default_rail": "auth"})
        mgr.register_provider(p)
        result = mgr.authorize_fee("quote-2", _waived_fee())
        assert result.success is True
        assert len(p.auth_calls) == 0

    def test_authorize_fallback_for_unsupported(self, db, provider):
        """FakeProvider doesn't have authorize_payment — should not raise."""
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        result = mgr.authorize_fee("quote-3", _fee())
        assert result.status == PaymentStatus.AUTHORIZED
        assert result.payment_id == ""  # synthetic, no real hold

    def test_authorize_spend_limit(self, db):
        p = AuthCaptureProvider(name="auth")
        mgr = PaymentManager(
            db=db,
            config={"default_rail": "auth", "spend_limits": {"max_per_order_usd": 1.0}},
        )
        mgr.register_provider(p)
        with pytest.raises(PaymentError, match="Spend limit"):
            mgr.authorize_fee("quote-limit", _fee())


class TestCaptureFee:
    def test_capture_existing_hold(self, db):
        p = AuthCaptureProvider(name="auth")
        mgr = PaymentManager(db=db, config={"default_rail": "auth"})
        mgr.register_provider(p)
        result = mgr.capture_fee("pi_hold_123", "order-1", _fee())
        assert result.status == PaymentStatus.COMPLETED
        assert len(p.capture_calls) == 1
        assert p.capture_calls[0] == "pi_hold_123"

    def test_capture_without_hold_falls_back_to_charge(self, db, provider):
        """Empty payment_id means no hold — should do a normal charge."""
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        result = mgr.capture_fee("", "order-2", _fee())
        assert result.success is True
        assert len(provider.calls) == 1  # fell back to create_payment

    def test_capture_no_provider_raises(self, db):
        mgr = PaymentManager(db=db)
        with pytest.raises(PaymentError, match="No payment providers"):
            mgr.capture_fee("pi_123", "order-x", _fee())


class TestCancelFee:
    def test_cancel_releases_hold(self, db):
        p = AuthCaptureProvider(name="auth")
        mgr = PaymentManager(db=db, config={"default_rail": "auth"})
        mgr.register_provider(p)
        result = mgr.cancel_fee("pi_hold_123")
        assert result.status == PaymentStatus.CANCELLED
        assert len(p.cancel_calls) == 1

    def test_cancel_empty_id_is_noop(self, db, provider):
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        result = mgr.cancel_fee("")
        assert result.status == PaymentStatus.CANCELLED
        assert result.success is True

    def test_cancel_fallback_for_unsupported(self, db, provider):
        """FakeProvider doesn't have cancel_payment — should not raise."""
        mgr = PaymentManager(db=db, config={"default_rail": "fake"})
        mgr.register_provider(provider)
        result = mgr.cancel_fee("pi_fake")
        assert result.status == PaymentStatus.CANCELLED
