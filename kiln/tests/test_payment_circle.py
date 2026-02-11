"""Tests for kiln.payments.circle_provider — Circle USDC payment adapter."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)
from kiln.payments.circle_provider import CircleProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    ok: bool = True,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.text = json.dumps(json_data) if json_data else ""
    resp.json.return_value = json_data or {}
    return resp


def _provider(**kwargs) -> CircleProvider:
    """Create a CircleProvider with sensible defaults."""
    defaults: Dict[str, Any] = {"api_key": "test-circle-key"}
    defaults.update(kwargs)
    return CircleProvider(**defaults)


def _payment_request(**kwargs) -> PaymentRequest:
    """Create a PaymentRequest with sensible defaults."""
    defaults: Dict[str, Any] = {
        "amount": 25.00,
        "currency": Currency.USDC,
        "rail": PaymentRail.SOLANA,
        "job_id": "job-001",
        "description": "Print job payment",
        "metadata": {"destination_address": "So1anaAddr3ss..."},
    }
    defaults.update(kwargs)
    return PaymentRequest(**defaults)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_api_key_raises(self):
        with pytest.raises(ValueError, match="API key required"):
            CircleProvider(api_key="")

    def test_no_api_key_no_env_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_CIRCLE_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key required"):
            CircleProvider()

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_CIRCLE_API_KEY", "env-circle-key")
        p = CircleProvider()
        assert p._api_key == "env-circle-key"

    def test_custom_base_url(self):
        p = _provider(base_url="https://sandbox.circle.com/v1/")
        assert p._base_url == "https://sandbox.circle.com/v1"

    def test_session_auth_header(self):
        p = _provider()
        assert "Bearer test-circle-key" in p._session.headers.get("Authorization", "")

    def test_default_network(self):
        p = _provider()
        assert p._default_network == "solana"

    def test_custom_default_network(self):
        p = _provider(default_network="base")
        assert p._default_network == "base"


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _provider().name == "circle"

    def test_supported_currencies(self):
        currencies = _provider().supported_currencies
        assert currencies == [Currency.USDC]

    def test_rail(self):
        assert _provider().rail == PaymentRail.SOLANA

    def test_repr(self):
        p = _provider()
        r = repr(p)
        assert "CircleProvider" in r
        assert "api.circle.com" in r


# ---------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------


class TestCreatePayment:
    @patch("kiln.payments.circle_provider.time.sleep")
    def test_success_on_solana(self, mock_sleep):
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-sol-001", "status": "pending"},
        })
        status_resp = _mock_response(json_data={
            "data": {
                "id": "tx-sol-001",
                "status": "complete",
                "amount": {"amount": "25.00", "currency": "USD"},
                "transactionHash": "5KtP...solhash",
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, status_resp]):
            result = p.create_payment(_payment_request())

        assert result.success is True
        assert result.payment_id == "tx-sol-001"
        assert result.status == PaymentStatus.COMPLETED
        assert result.tx_hash == "5KtP...solhash"
        mock_sleep.assert_called()

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_success_on_base(self, mock_sleep):
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-base-001", "status": "pending"},
        })
        status_resp = _mock_response(json_data={
            "data": {
                "id": "tx-base-001",
                "status": "complete",
                "amount": {"amount": "10.50", "currency": "USD"},
                "transactionHash": "0xBaseTxHash...",
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, status_resp]):
            result = p.create_payment(_payment_request(rail=PaymentRail.BASE))

        assert result.success is True
        assert result.payment_id == "tx-base-001"
        assert result.tx_hash == "0xBaseTxHash..."

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_network_routing_solana_chain(self, mock_sleep):
        """Verify the POST body uses chain=SOL for Solana rail."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-100", "status": "pending"},
        })
        status_resp = _mock_response(json_data={
            "data": {
                "id": "tx-100",
                "status": "complete",
                "amount": {"amount": "5.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, status_resp]) as mock_req:
            p.create_payment(_payment_request(rail=PaymentRail.SOLANA))

        # First call is the POST /v1/transfers
        call_args = mock_req.call_args_list[0]
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["destination"]["chain"] == "SOL"

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_network_routing_base_chain(self, mock_sleep):
        """Verify the POST body uses chain=ETH-BASE for Base rail."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-200", "status": "pending"},
        })
        status_resp = _mock_response(json_data={
            "data": {
                "id": "tx-200",
                "status": "complete",
                "amount": {"amount": "5.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, status_resp]) as mock_req:
            p.create_payment(_payment_request(rail=PaymentRail.BASE))

        call_args = mock_req.call_args_list[0]
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["destination"]["chain"] == "ETH-BASE"

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_polling_for_finality(self, mock_sleep):
        """Transfer goes pending -> pending -> complete across 3 polls."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-poll", "status": "pending"},
        })
        pending_resp = _mock_response(json_data={
            "data": {
                "id": "tx-poll",
                "status": "pending",
                "amount": {"amount": "25.00", "currency": "USD"},
            },
        })
        complete_resp = _mock_response(json_data={
            "data": {
                "id": "tx-poll",
                "status": "complete",
                "amount": {"amount": "25.00", "currency": "USD"},
                "transactionHash": "final-hash",
            },
        })
        with patch.object(
            p._session, "request",
            side_effect=[create_resp, pending_resp, pending_resp, complete_resp],
        ):
            result = p.create_payment(_payment_request())

        assert result.status == PaymentStatus.COMPLETED
        assert result.tx_hash == "final-hash"
        assert mock_sleep.call_count == 3

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_polling_failure_detected(self, mock_sleep):
        """Transfer fails during polling."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-fail", "status": "pending"},
        })
        failed_resp = _mock_response(json_data={
            "data": {
                "id": "tx-fail",
                "status": "failed",
                "amount": {"amount": "25.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, failed_resp]):
            result = p.create_payment(_payment_request())

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_timeout_returns_processing(self, mock_sleep):
        """After 10 polls still pending, returns PROCESSING."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-slow", "status": "pending"},
        })
        pending_resp = _mock_response(json_data={
            "data": {
                "id": "tx-slow",
                "status": "pending",
                "amount": {"amount": "25.00", "currency": "USD"},
            },
        })
        # 1 create + 10 pending polls
        with patch.object(
            p._session, "request",
            side_effect=[create_resp] + [pending_resp] * 10,
        ):
            result = p.create_payment(_payment_request())

        assert result.status == PaymentStatus.PROCESSING
        assert result.success is False
        assert result.payment_id == "tx-slow"
        assert mock_sleep.call_count == 10

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_missing_transfer_id_raises(self, mock_sleep):
        p = _provider()
        create_resp = _mock_response(json_data={"data": {}})
        with patch.object(p._session, "request", return_value=create_resp):
            with pytest.raises(PaymentError, match="transfer ID"):
                p.create_payment(_payment_request())

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_description_in_payload(self, mock_sleep):
        """Verify description is included in the transfer payload."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-desc", "status": "pending"},
        })
        complete_resp = _mock_response(json_data={
            "data": {
                "id": "tx-desc",
                "status": "complete",
                "amount": {"amount": "25.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, complete_resp]) as mock_req:
            p.create_payment(_payment_request(description="Test print"))

        call_args = mock_req.call_args_list[0]
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["description"] == "Test print"

    @patch("kiln.payments.circle_provider.time.sleep")
    def test_amount_formatted_two_decimals(self, mock_sleep):
        """Amount is always formatted to 2 decimal places."""
        p = _provider()
        create_resp = _mock_response(json_data={
            "data": {"id": "tx-fmt", "status": "pending"},
        })
        complete_resp = _mock_response(json_data={
            "data": {
                "id": "tx-fmt",
                "status": "complete",
                "amount": {"amount": "5.10", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[create_resp, complete_resp]) as mock_req:
            p.create_payment(_payment_request(amount=5.1))

        call_args = mock_req.call_args_list[0]
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["amount"]["amount"] == "5.10"


# ---------------------------------------------------------------------------
# get_payment_status
# ---------------------------------------------------------------------------


class TestGetPaymentStatus:
    def test_complete_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-001",
                "status": "complete",
                "amount": {"amount": "25.00", "currency": "USD"},
                "transactionHash": "hash-abc",
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-001")

        assert result.status == PaymentStatus.COMPLETED
        assert result.success is True
        assert result.payment_id == "tx-001"
        assert result.amount == 25.00

    def test_pending_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-002",
                "status": "pending",
                "amount": {"amount": "10.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-002")

        assert result.status == PaymentStatus.PROCESSING
        assert result.success is False

    def test_failed_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-003",
                "status": "failed",
                "amount": {"amount": "50.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-003")

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    def test_with_tx_hash(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-004",
                "status": "complete",
                "amount": {"amount": "15.00", "currency": "USD"},
                "transactionHash": "0xDeadBeef...",
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-004")

        assert result.tx_hash == "0xDeadBeef..."

    def test_without_tx_hash(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-005",
                "status": "pending",
                "amount": {"amount": "15.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-005")

        assert result.tx_hash is None

    def test_unknown_status_maps_to_pending(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-006",
                "status": "some_new_status",
                "amount": {"amount": "20.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-006")

        assert result.status == PaymentStatus.PENDING

    def test_currency_is_usdc(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "id": "tx-007",
                "status": "complete",
                "amount": {"amount": "1.00", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-007")

        assert result.currency == Currency.USDC


# ---------------------------------------------------------------------------
# refund_payment
# ---------------------------------------------------------------------------


class TestRefundPayment:
    def test_refund_success(self):
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "id": "tx-orig",
                "status": "complete",
                "amount": {"amount": "30.00", "currency": "USD"},
                "source": {"type": "wallet", "id": "master"},
                "destination": {"type": "blockchain", "id": "addr-123"},
            },
        })
        refund_resp = _mock_response(json_data={
            "data": {
                "id": "tx-refund-001",
                "status": "complete",
                "transactionHash": "refund-hash-abc",
            },
        })
        with patch.object(p._session, "request", side_effect=[original_resp, refund_resp]):
            result = p.refund_payment("tx-orig")

        assert result.status == PaymentStatus.REFUNDED
        assert result.payment_id == "tx-refund-001"
        assert result.amount == 30.00
        assert result.tx_hash == "refund-hash-abc"
        assert result.success is True

    def test_refund_failed_transfer(self):
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "id": "tx-orig-2",
                "status": "complete",
                "amount": {"amount": "20.00", "currency": "USD"},
                "source": {"type": "wallet", "id": "master"},
                "destination": {"type": "blockchain", "id": "addr-456"},
            },
        })
        refund_resp = _mock_response(json_data={
            "data": {
                "id": "tx-refund-002",
                "status": "failed",
            },
        })
        with patch.object(p._session, "request", side_effect=[original_resp, refund_resp]):
            result = p.refund_payment("tx-orig-2")

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    def test_refund_api_error(self):
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "id": "tx-orig-3",
                "status": "complete",
                "amount": {"amount": "10.00", "currency": "USD"},
                "source": {"type": "wallet", "id": "master"},
                "destination": {"type": "blockchain", "id": "addr-789"},
            },
        })
        error_resp = _mock_response(status_code=500, ok=False, json_data={"error": "internal"})
        with patch.object(p._session, "request", side_effect=[original_resp, error_resp]):
            with pytest.raises(PaymentError, match="HTTP 500"):
                p.refund_payment("tx-orig-3")


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHTTPErrors:
    def test_timeout(self):
        p = _provider()
        with patch.object(p._session, "request", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(PaymentError, match="timeout") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "TIMEOUT"

    def test_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(PaymentError, match="Cannot reach") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "CONNECTION_ERROR"

    def test_401_unauthorized(self):
        p = _provider()
        resp = _mock_response(status_code=401, ok=False, json_data={"error": "Unauthorized"})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="HTTP 401") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "HTTP_401"

    def test_500_server_error(self):
        p = _provider()
        resp = _mock_response(status_code=500, ok=False, json_data={"error": "Internal"})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="HTTP 500") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "HTTP_500"

    def test_403_forbidden(self):
        p = _provider()
        resp = _mock_response(status_code=403, ok=False, json_data={"error": "Forbidden"})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="HTTP 403"):
                p.get_payment_status("tx-000")

    def test_payment_error_has_code(self):
        e = PaymentError("test error", code="TEST_CODE")
        assert e.code == "TEST_CODE"
        assert str(e) == "test error"

    def test_payment_error_default_code_none(self):
        e = PaymentError("simple error")
        assert e.code is None


# ---------------------------------------------------------------------------
# Network mapping
# ---------------------------------------------------------------------------


class TestNetworkMapping:
    def test_solana_maps_to_sol(self):
        p = _provider()
        assert p._resolve_chain(PaymentRail.SOLANA) == "SOL"

    def test_base_maps_to_eth_base(self):
        p = _provider()
        assert p._resolve_chain(PaymentRail.BASE) == "ETH-BASE"

    def test_circle_rail_falls_back_to_default_solana(self):
        p = _provider(default_network="solana")
        assert p._resolve_chain(PaymentRail.CIRCLE) == "SOL"

    def test_circle_rail_falls_back_to_default_base(self):
        p = _provider(default_network="base")
        assert p._resolve_chain(PaymentRail.CIRCLE) == "ETH-BASE"

    def test_stripe_rail_falls_back_to_default(self):
        p = _provider(default_network="solana")
        assert p._resolve_chain(PaymentRail.STRIPE) == "SOL"


# ---------------------------------------------------------------------------
# Abstract base verification
# ---------------------------------------------------------------------------


class TestAbstractBase:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            PaymentProvider()

    def test_circle_is_payment_provider(self):
        p = _provider()
        assert isinstance(p, PaymentProvider)
