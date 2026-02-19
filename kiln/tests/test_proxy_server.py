"""Tests for kiln.fulfillment.proxy_server — server-side proxy orchestrator.

Covers:
- License validation (valid key, invalid key, empty key)
- Material listing delegation and error propagation
- Quote handling with fee calculation and server-side quote caching
- Order handling (quote token validation, free tier limits, fee charging,
  auto-refund on failure, ownership validation)
- Order cancellation delegation
- Order status delegation
- User registration (free tier key generation)
- Singleton orchestrator pattern
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.billing import BillingLedger, FeeCalculation
from kiln.fulfillment.base import (
    FulfillmentError,
    Material,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    QuoteRequest,
)
from kiln.fulfillment.proxy_server import ProxyOrchestrator, get_orchestrator
from kiln.licensing import LicenseInfo, LicenseTier
from kiln.payments.base import PaymentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fee_calc(
    job_cost: float = 100.0,
    fee_amount: float = 5.0,
    total_cost: float = 105.0,
) -> FeeCalculation:
    """Create a FeeCalculation with sensible defaults."""
    return FeeCalculation(
        job_cost=job_cost,
        fee_amount=fee_amount,
        fee_percent=5.0,
        total_cost=total_cost,
        currency="USD",
    )


def _quote(
    quote_id: str = "q-123",
    total_price: float = 100.0,
    currency: str = "USD",
) -> Quote:
    """Create a Quote with sensible defaults."""
    return Quote(
        quote_id=quote_id,
        provider="craftcloud",
        material="PLA",
        quantity=1,
        unit_price=total_price,
        total_price=total_price,
        currency=currency,
    )


def _order_result(
    order_id: str = "o-456",
    success: bool = True,
    status: OrderStatus = OrderStatus.SUBMITTED,
) -> OrderResult:
    """Create an OrderResult with sensible defaults."""
    return OrderResult(
        success=success,
        order_id=order_id,
        status=status,
        provider="craftcloud",
    )


def _seed_quote_cache(
    orch: ProxyOrchestrator,
    token: str = "test-token",
    *,
    total_price: float = 100.0,
    currency: str = "USD",
    provider: str = "craftcloud",
    user_email: str = "user@test.com",
    ttl: float = 3600,
) -> str:
    """Insert a fake quote into the orchestrator's server-side cache.

    Returns the token for convenience.
    """
    orch._quote_cache[token] = {
        "total_price": total_price,
        "currency": currency,
        "provider": provider,
        "user_email": user_email,
        "quote_id": "q-123",
        "expires_at": time.time() + ttl,
    }
    return token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db() -> MagicMock:
    """Mock KilnDB instance."""
    db = MagicMock()
    db._conn = MagicMock()
    return db


@pytest.fixture()
def mock_provider() -> MagicMock:
    """Mock fulfillment provider."""
    provider = MagicMock()
    provider.name = "craftcloud"
    return provider


@pytest.fixture()
def mock_payment_mgr() -> MagicMock:
    """Mock PaymentManager."""
    mgr = MagicMock()
    mgr.available_rails = ["stripe"]
    return mgr


@pytest.fixture()
def orch(mock_db: MagicMock) -> ProxyOrchestrator:
    """Create a ProxyOrchestrator with a mocked DB."""
    return ProxyOrchestrator(db=mock_db)


# ---------------------------------------------------------------------------
# TestLicenseValidation
# ---------------------------------------------------------------------------


class TestLicenseValidation:
    def test_valid_key_returns_correct_tier(self, orch: ProxyOrchestrator):
        mock_info = LicenseInfo(tier=LicenseTier.PRO)
        with patch("kiln.fulfillment.proxy_server.LicenseManager") as MockMgr:
            instance = MockMgr.return_value
            instance.get_tier.return_value = LicenseTier.PRO
            instance.get_info.return_value = mock_info

            result = orch.validate_license("kiln_pro_abc123")

        assert result["tier"] == "pro"
        assert result["valid"] is True
        assert "info" in result

    def test_invalid_key_returns_valid_false(self, orch: ProxyOrchestrator):
        with patch("kiln.fulfillment.proxy_server.LicenseManager") as MockMgr:
            MockMgr.side_effect = ValueError("Invalid key format")

            result = orch.validate_license("bad-key")

        assert result["valid"] is False
        assert result["tier"] == "free"
        assert "error" in result

    def test_empty_key_returns_valid_false(self, orch: ProxyOrchestrator):
        result = orch.validate_license("")
        assert result["valid"] is False
        assert result["tier"] == "free"
        assert result["error"] == "No license key provided"

    def test_whitespace_only_key_returns_valid_false(self, orch: ProxyOrchestrator):
        result = orch.validate_license("   ")
        assert result["valid"] is False
        assert result["tier"] == "free"

    def test_expired_key_returns_valid_false(self, orch: ProxyOrchestrator):
        mock_info = MagicMock(spec=LicenseInfo)
        mock_info.is_valid = False
        mock_info.to_dict.return_value = {"tier": "pro", "is_valid": False}

        with patch("kiln.fulfillment.proxy_server.LicenseManager") as MockMgr:
            instance = MockMgr.return_value
            instance.get_tier.return_value = LicenseTier.PRO
            instance.get_info.return_value = mock_info

            result = orch.validate_license("kiln_pro_expired")

        assert result["valid"] is False
        assert result["tier"] == "pro"


# ---------------------------------------------------------------------------
# TestHandleMaterials
# ---------------------------------------------------------------------------


class TestHandleMaterials:
    def test_delegates_to_provider(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        materials = [
            Material(id="pla-white", name="PLA White", technology="FDM"),
            Material(id="abs-black", name="ABS Black", technology="FDM"),
        ]
        mock_provider.list_materials.return_value = materials

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            result = orch.handle_materials("craftcloud")

        assert len(result) == 2
        assert result[0]["id"] == "pla-white"
        assert result[1]["id"] == "abs-black"

    def test_error_propagation(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        mock_provider.list_materials.side_effect = FulfillmentError("API down")

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with pytest.raises(FulfillmentError, match="API down"):
                orch.handle_materials("craftcloud")


# ---------------------------------------------------------------------------
# TestHandleQuote
# ---------------------------------------------------------------------------


class TestHandleQuote:
    def test_calls_provider_and_calculates_fee(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        quote = _quote(total_price=120.0, currency="USD")
        mock_provider.get_quote.return_value = quote

        fee = _fee_calc(job_cost=120.0, fee_amount=6.0, total_cost=126.0)

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with patch.object(orch._ledger, "calculate_fee", return_value=fee):
                result = orch.handle_quote(
                    "craftcloud",
                    "/tmp/model.stl",
                    QuoteRequest(file_path="/tmp/model.stl", material_id="pla"),
                    user_email="user@test.com",
                )

        assert result["quote"]["quote_id"] == "q-123"
        assert result["kiln_fee"]["fee_amount"] == 6.0
        assert result["total_with_fee"] == 126.0
        assert "quote_token" in result
        assert len(result["quote_token"]) == 32  # uuid4 hex

    def test_quote_cached_server_side(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        quote = _quote(total_price=99.0, currency="EUR")
        mock_provider.get_quote.return_value = quote
        fee = _fee_calc()

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with patch.object(orch._ledger, "calculate_fee", return_value=fee):
                result = orch.handle_quote(
                    "craftcloud",
                    "/tmp/model.stl",
                    QuoteRequest(file_path="/tmp/model.stl", material_id="pla"),
                    user_email="user@test.com",
                )

        token = result["quote_token"]
        assert token in orch._quote_cache
        cached = orch._quote_cache[token]
        assert cached["total_price"] == 99.0
        assert cached["currency"] == "EUR"
        assert cached["provider"] == "craftcloud"
        assert cached["user_email"] == "user@test.com"

    def test_provider_error_propagates(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        mock_provider.get_quote.side_effect = FulfillmentError("no quotes available")

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with pytest.raises(FulfillmentError, match="no quotes"):
                orch.handle_quote(
                    "craftcloud",
                    "/tmp/model.stl",
                    QuoteRequest(file_path="/tmp/model.stl", material_id="pla"),
                    user_email="user@test.com",
                )


# ---------------------------------------------------------------------------
# TestHandleOrder
# ---------------------------------------------------------------------------


class TestHandleOrder:
    def test_missing_quote_token_raises(self, orch: ProxyOrchestrator):
        with pytest.raises(FulfillmentError, match="Quote not found"):
            orch.handle_order(
                "craftcloud",
                OrderRequest(quote_id="q-123"),
                user_email="user@test.com",
                user_tier=LicenseTier.FREE,
                quote_token="nonexistent-token",
            )

    def test_expired_quote_raises(self, orch: ProxyOrchestrator):
        _seed_quote_cache(orch, "expired-token", ttl=-1)
        with pytest.raises(FulfillmentError, match="expired"):
            orch.handle_order(
                "craftcloud",
                OrderRequest(quote_id="q-123"),
                user_email="user@test.com",
                user_tier=LicenseTier.FREE,
                quote_token="expired-token",
            )

    def test_provider_mismatch_raises(self, orch: ProxyOrchestrator):
        _seed_quote_cache(orch, "token-1", provider="craftcloud")
        with pytest.raises(FulfillmentError, match="Provider mismatch"):
            orch.handle_order(
                "sculpteo",
                OrderRequest(quote_id="q-123"),
                user_email="user@test.com",
                user_tier=LicenseTier.FREE,
                quote_token="token-1",
            )

    def test_ownership_mismatch_raises(self, orch: ProxyOrchestrator):
        _seed_quote_cache(orch, "token-1", user_email="alice@test.com")
        with pytest.raises(FulfillmentError, match="different user"):
            orch.handle_order(
                "craftcloud",
                OrderRequest(quote_id="q-123"),
                user_email="bob@test.com",
                user_tier=LicenseTier.FREE,
                quote_token="token-1",
            )

    def test_quote_token_consumed_on_use(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        token = _seed_quote_cache(orch, "one-time-token")
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc()

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch.object(
                orch._ledger,
                "calculate_and_record_fee",
                return_value=(fee, "charge-1"),
            ):
                with patch.object(orch, "_tag_charge_with_user"):
                    with patch(
                        "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                        return_value=mock_provider,
                    ):
                        orch.handle_order(
                            "craftcloud",
                            OrderRequest(quote_id="q-123"),
                            user_email="user@test.com",
                            user_tier=LicenseTier.BUSINESS,
                            quote_token=token,
                        )

        # Second use should fail — token is consumed
        with pytest.raises(FulfillmentError, match="Quote not found"):
            orch.handle_order(
                "craftcloud",
                OrderRequest(quote_id="q-123"),
                user_email="user@test.com",
                user_tier=LicenseTier.BUSINESS,
                quote_token=token,
            )

    def test_free_tier_limit_enforcement(self, orch: ProxyOrchestrator):
        token = _seed_quote_cache(orch, "token-free")
        with patch.object(orch._ledger, "network_jobs_this_month_for_user", return_value=3):
            with pytest.raises(FulfillmentError, match="Free tier limit reached"):
                orch.handle_order(
                    "craftcloud",
                    OrderRequest(quote_id="q-123"),
                    user_email="user@test.com",
                    user_tier=LicenseTier.FREE,
                    quote_token=token,
                )

    def test_free_tier_allows_under_limit(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-under")
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc()

        with patch.object(orch._ledger, "network_jobs_this_month_for_user", return_value=2):
            with patch.object(orch._ledger, "calculate_fee", return_value=fee):
                with patch.object(
                    orch._ledger,
                    "calculate_and_record_fee",
                    return_value=(fee, "charge-1"),
                ):
                    with patch.object(orch, "_tag_charge_with_user"):
                        with patch(
                            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                            return_value=mock_provider,
                        ):
                            result = orch.handle_order(
                                "craftcloud",
                                OrderRequest(quote_id="q-123"),
                                user_email="user@test.com",
                                user_tier=LicenseTier.FREE,
                                quote_token=token,
                            )

        assert result["order"]["order_id"] == "o-456"

    def test_business_tier_bypasses_limit(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-biz")
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc()

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch.object(
                orch._ledger,
                "calculate_and_record_fee",
                return_value=(fee, "charge-1"),
            ):
                with patch.object(orch, "_tag_charge_with_user"):
                    with patch(
                        "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                        return_value=mock_provider,
                    ):
                        result = orch.handle_order(
                            "craftcloud",
                            OrderRequest(quote_id="q-123"),
                            user_email="user@test.com",
                            user_tier=LicenseTier.BUSINESS,
                            quote_token=token,
                        )

        assert result["order"]["success"] is True

    def test_fee_charge_with_payment_manager(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
        mock_payment_mgr: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-pay")
        orch._payment_mgr = mock_payment_mgr
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc()

        payment_result = MagicMock()
        payment_result.payment_id = "pay-789"
        mock_payment_mgr.charge_fee.return_value = payment_result

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch(
                "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                return_value=mock_provider,
            ):
                result = orch.handle_order(
                    "craftcloud",
                    OrderRequest(quote_id="q-123"),
                    user_email="user@test.com",
                    user_tier=LicenseTier.BUSINESS,
                    quote_token=token,
                )

        mock_payment_mgr.charge_fee.assert_called_once_with("q-123", fee)
        assert result["order"]["order_id"] == "o-456"
        assert result["kiln_fee"]["fee_amount"] == 5.0

    def test_payment_error_propagates(
        self,
        orch: ProxyOrchestrator,
        mock_payment_mgr: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-pay-err")
        orch._payment_mgr = mock_payment_mgr
        fee = _fee_calc()
        mock_payment_mgr.charge_fee.side_effect = PaymentError("card declined")

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with pytest.raises(PaymentError, match="card declined"):
                orch.handle_order(
                    "craftcloud",
                    OrderRequest(quote_id="q-123"),
                    user_email="user@test.com",
                    user_tier=LicenseTier.BUSINESS,
                    quote_token=token,
                )

    def test_auto_refund_on_order_failure(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
        mock_payment_mgr: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-refund")
        orch._payment_mgr = mock_payment_mgr
        fee = _fee_calc()

        payment_result = MagicMock()
        payment_result.payment_id = "pay-789"
        mock_payment_mgr.charge_fee.return_value = payment_result

        mock_provider.place_order.side_effect = FulfillmentError("vendor unavailable")

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch(
                "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                return_value=mock_provider,
            ):
                with pytest.raises(FulfillmentError, match="vendor unavailable"):
                    orch.handle_order(
                        "craftcloud",
                        OrderRequest(quote_id="q-123"),
                        user_email="user@test.com",
                        user_tier=LicenseTier.BUSINESS,
                        quote_token=token,
                    )

        mock_payment_mgr.cancel_fee.assert_called_once_with("pay-789")

    def test_refund_failure_logged_not_raised(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
        mock_payment_mgr: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-refund-fail")
        orch._payment_mgr = mock_payment_mgr
        fee = _fee_calc()

        payment_result = MagicMock()
        payment_result.payment_id = "pay-789"
        mock_payment_mgr.charge_fee.return_value = payment_result
        mock_payment_mgr.cancel_fee.side_effect = Exception("refund API down")

        mock_provider.place_order.side_effect = FulfillmentError("vendor unavailable")

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch(
                "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                return_value=mock_provider,
            ):
                with pytest.raises(FulfillmentError, match="vendor unavailable"):
                    orch.handle_order(
                        "craftcloud",
                        OrderRequest(quote_id="q-123"),
                        user_email="user@test.com",
                        user_tier=LicenseTier.BUSINESS,
                        quote_token=token,
                    )

    def test_no_payment_manager_records_fee(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-no-pay")
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc()

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch.object(
                orch._ledger,
                "calculate_and_record_fee",
                return_value=(fee, "charge-abc"),
            ) as mock_record:
                with patch.object(orch, "_tag_charge_with_user") as mock_tag:
                    with patch(
                        "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                        return_value=mock_provider,
                    ):
                        result = orch.handle_order(
                            "craftcloud",
                            OrderRequest(quote_id="q-123"),
                            user_email="user@test.com",
                            user_tier=LicenseTier.BUSINESS,
                            quote_token=token,
                        )

        mock_record.assert_called_once_with("q-123", 100.0, currency="USD")
        mock_tag.assert_called_once_with("charge-abc", "user@test.com")
        assert result["order"]["order_id"] == "o-456"

    def test_no_refund_when_no_payment_id(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        token = _seed_quote_cache(orch, "token-no-refund")
        mock_provider.place_order.side_effect = FulfillmentError("order failed")
        fee = _fee_calc()

        with patch.object(orch._ledger, "calculate_fee", return_value=fee):
            with patch.object(
                orch._ledger,
                "calculate_and_record_fee",
                return_value=(fee, "charge-abc"),
            ):
                with patch.object(orch, "_tag_charge_with_user"):
                    with patch(
                        "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                        return_value=mock_provider,
                    ):
                        with pytest.raises(FulfillmentError, match="order failed"):
                            orch.handle_order(
                                "craftcloud",
                                OrderRequest(quote_id="q-123"),
                                user_email="user@test.com",
                                user_tier=LicenseTier.BUSINESS,
                                quote_token=token,
                            )

    def test_pro_tier_subject_to_limit(self, orch: ProxyOrchestrator):
        token = _seed_quote_cache(orch, "token-pro")
        with patch.object(orch._ledger, "network_jobs_this_month_for_user", return_value=3):
            with pytest.raises(FulfillmentError, match="Free tier limit reached") as exc_info:
                orch.handle_order(
                    "craftcloud",
                    OrderRequest(quote_id="q-123"),
                    user_email="user@test.com",
                    user_tier=LicenseTier.PRO,
                    quote_token=token,
                )
            assert exc_info.value.code == "FREE_TIER_LIMIT"

    def test_uses_cached_price_not_client_supplied(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        """Verify the fee is calculated from the cached price, not client input."""
        token = _seed_quote_cache(orch, "token-price", total_price=200.0, currency="EUR")
        mock_provider.place_order.return_value = _order_result()
        fee = _fee_calc(job_cost=200.0, fee_amount=10.0, total_cost=210.0)

        with patch.object(orch._ledger, "calculate_fee", return_value=fee) as mock_calc:
            with patch.object(
                orch._ledger,
                "calculate_and_record_fee",
                return_value=(fee, "charge-1"),
            ):
                with patch.object(orch, "_tag_charge_with_user"):
                    with patch(
                        "kiln.fulfillment.proxy_server.get_fulfillment_provider",
                        return_value=mock_provider,
                    ):
                        orch.handle_order(
                            "craftcloud",
                            OrderRequest(quote_id="q-123"),
                            user_email="user@test.com",
                            user_tier=LicenseTier.BUSINESS,
                            quote_token=token,
                        )

        # Fee must be calculated from the cached 200.0, not any client value
        mock_calc.assert_called_once_with(200.0, currency="EUR")


# ---------------------------------------------------------------------------
# TestHandleCancel
# ---------------------------------------------------------------------------


class TestHandleCancel:
    def test_delegates_to_provider(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        cancel_result = _order_result(status=OrderStatus.CANCELLED)
        mock_provider.cancel_order.return_value = cancel_result

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            result = orch.handle_cancel(
                "craftcloud",
                "o-456",
                user_tier=LicenseTier.FREE,
            )

        mock_provider.cancel_order.assert_called_once_with("o-456")
        assert result["status"] == "cancelled"

    def test_error_propagation(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        mock_provider.cancel_order.side_effect = FulfillmentError("cannot cancel shipped order")

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with pytest.raises(FulfillmentError, match="cannot cancel shipped"):
                orch.handle_cancel(
                    "craftcloud",
                    "o-456",
                    user_tier=LicenseTier.FREE,
                )


# ---------------------------------------------------------------------------
# TestHandleStatus
# ---------------------------------------------------------------------------


class TestHandleStatus:
    def test_delegates_to_provider(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        status_result = _order_result(status=OrderStatus.PRINTING)
        mock_provider.get_order_status.return_value = status_result

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            result = orch.handle_status("craftcloud", "o-456")

        mock_provider.get_order_status.assert_called_once_with("o-456")
        assert result["status"] == "printing"

    def test_error_propagation(
        self,
        orch: ProxyOrchestrator,
        mock_provider: MagicMock,
    ):
        mock_provider.get_order_status.side_effect = FulfillmentError("order not found")

        with patch(
            "kiln.fulfillment.proxy_server.get_fulfillment_provider",
            return_value=mock_provider,
        ):
            with pytest.raises(FulfillmentError, match="order not found"):
                orch.handle_status("craftcloud", "o-456")


# ---------------------------------------------------------------------------
# TestRegisterUser
# ---------------------------------------------------------------------------


class TestRegisterUser:
    def test_generates_free_tier_key(self, orch: ProxyOrchestrator):
        with patch(
            "kiln.fulfillment.proxy_server.generate_license_key",
            return_value="kiln_free_abc123",
        ) as mock_gen:
            result = orch.register_user("user@test.com")

        mock_gen.assert_called_once_with(LicenseTier.FREE, "user@test.com")
        assert result["license_key"] == "kiln_free_abc123"
        assert result["tier"] == "free"
        assert result["email"] == "user@test.com"

    def test_signing_key_error_propagates(self, orch: ProxyOrchestrator):
        with patch(
            "kiln.fulfillment.proxy_server.generate_license_key",
            side_effect=ValueError("No signing key configured"),
        ):
            with pytest.raises(ValueError, match="signing key"):
                orch.register_user("user@test.com")


# ---------------------------------------------------------------------------
# TestOrchestrator
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_singleton_returns_same_instance(self):
        import kiln.fulfillment.proxy_server as mod

        mock_db = MagicMock()
        original = mod._orchestrator

        try:
            mod._orchestrator = None

            with patch("kiln.persistence.get_db", return_value=mock_db):
                first = get_orchestrator()
                second = get_orchestrator()

            assert first is second
        finally:
            mod._orchestrator = original

    def test_singleton_creates_with_db(self):
        import kiln.fulfillment.proxy_server as mod

        mock_db = MagicMock()
        original = mod._orchestrator

        try:
            mod._orchestrator = None

            with patch("kiln.persistence.get_db", return_value=mock_db) as mock_get_db:
                instance = get_orchestrator()

            mock_get_db.assert_called_once()
            assert instance._db is mock_db
        finally:
            mod._orchestrator = original

    def test_constructor_initializes_ledger(self, mock_db: MagicMock):
        orch = ProxyOrchestrator(db=mock_db)
        assert isinstance(orch._ledger, BillingLedger)
        assert orch._payment_mgr is None

    def test_constructor_accepts_event_bus(self, mock_db: MagicMock):
        bus = MagicMock()
        orch = ProxyOrchestrator(db=mock_db, event_bus=bus)
        assert orch._event_bus is bus


# ---------------------------------------------------------------------------
# TestTagChargeWithUser
# ---------------------------------------------------------------------------


class TestTagChargeWithUser:
    def test_tags_charge_via_sql(self, orch: ProxyOrchestrator, mock_db: MagicMock):
        orch._tag_charge_with_user("charge-1", "user@test.com")

        mock_db._conn.execute.assert_called_once_with(
            "UPDATE billing_charges SET user_email = ? WHERE id = ?",
            ("user@test.com", "charge-1"),
        )
        mock_db._conn.commit.assert_called_once()

    def test_empty_email_skips(self, orch: ProxyOrchestrator, mock_db: MagicMock):
        orch._tag_charge_with_user("charge-1", "")
        mock_db._conn.execute.assert_not_called()

    def test_sql_error_logged_not_raised(self, orch: ProxyOrchestrator, mock_db: MagicMock):
        mock_db._conn.execute.side_effect = Exception("SQL error")
        # Should not raise
        orch._tag_charge_with_user("charge-1", "user@test.com")
