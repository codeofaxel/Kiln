"""Tests for kiln.payments.circle_provider — Circle W3S Programmable Wallets adapter."""

from __future__ import annotations

import json
import uuid
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
    """Create a CircleProvider with sensible defaults for the W3S API."""
    defaults: Dict[str, Any] = {
        "api_key": "test-circle-key",
        "entity_secret": "a" * 64,
        "wallet_id": "test-wallet-uuid",
    }
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
        "metadata": {"destination_address": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"},
    }
    defaults.update(kwargs)
    return PaymentRequest(**defaults)


def _transfer_response(tx_id: str = "tx-001", state: str = "INITIATED"):
    """W3S transfer response — uses transactionIds list."""
    return _mock_response(json_data={
        "data": {"id": tx_id, "state": state},
    })


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_api_key_raises(self):
        with pytest.raises(ValueError, match="API key required"):
            CircleProvider(api_key="", entity_secret="a" * 64, wallet_id="w")

    def test_no_api_key_no_env_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_CIRCLE_API_KEY", raising=False)
        monkeypatch.delenv("KILN_CIRCLE_ENTITY_SECRET", raising=False)
        monkeypatch.delenv("KILN_CIRCLE_WALLET_ID", raising=False)
        with pytest.raises(ValueError, match="API key required"):
            CircleProvider()

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_CIRCLE_API_KEY", "env-circle-key")
        monkeypatch.setenv("KILN_CIRCLE_ENTITY_SECRET", "b" * 64)
        monkeypatch.setenv("KILN_CIRCLE_WALLET_ID", "wallet-from-env")
        p = CircleProvider()
        assert p._api_key == "env-circle-key"

    def test_env_var_entity_secret(self, monkeypatch):
        monkeypatch.setenv("KILN_CIRCLE_API_KEY", "key-123")
        monkeypatch.setenv("KILN_CIRCLE_ENTITY_SECRET", "b" * 64)
        monkeypatch.setenv("KILN_CIRCLE_WALLET_ID", "wallet-from-env")
        p = CircleProvider()
        assert p._entity_secret == "b" * 64

    def test_env_var_wallet_id(self, monkeypatch):
        monkeypatch.setenv("KILN_CIRCLE_API_KEY", "key-123")
        monkeypatch.setenv("KILN_CIRCLE_ENTITY_SECRET", "b" * 64)
        monkeypatch.setenv("KILN_CIRCLE_WALLET_ID", "wallet-from-env")
        p = CircleProvider()
        assert p._wallet_id == "wallet-from-env"

    def test_custom_base_url(self):
        p = _provider(base_url="https://sandbox.circle.com/")
        assert p._base_url == "https://sandbox.circle.com"

    def test_session_auth_header(self):
        p = _provider()
        assert "Bearer test-circle-key" in p._session.headers.get("Authorization", "")

    def test_default_network(self):
        p = _provider()
        assert p._default_network == "solana"

    def test_custom_default_network(self):
        p = _provider(default_network="base")
        assert p._default_network == "base"

    def test_entity_secret_optional_at_construction(self):
        """entity_secret can be empty — only needed for mutating calls."""
        p = _provider(entity_secret="")
        assert p._entity_secret == ""

    def test_wallet_id_optional_at_construction(self):
        """wallet_id can be empty — only needed for transfers."""
        p = _provider(wallet_id="")
        assert p._wallet_id == ""


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

    def test_rail_base(self):
        assert _provider(default_network="base").rail == PaymentRail.BASE

    def test_repr(self):
        p = _provider()
        r = repr(p)
        assert "CircleProvider" in r
        assert "api.circle.com" in r


# ---------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------


class TestCreatePayment:
    def test_success_on_solana(self):
        p = _provider()
        create_resp = _transfer_response("tx-sol-001")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp),
        ):
            result = p.create_payment(_payment_request())

        assert result.payment_id == "tx-sol-001"
        assert result.status == PaymentStatus.PROCESSING

    def test_success_on_base(self):
        p = _provider()
        create_resp = _transfer_response("tx-base-001")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp),
        ):
            result = p.create_payment(_payment_request(rail=PaymentRail.BASE))

        assert result.payment_id == "tx-base-001"
        assert result.status == PaymentStatus.PROCESSING

    def test_blockchain_in_payload(self):
        """Verify the POST body includes the blockchain field for Solana."""
        p = _provider()
        create_resp = _transfer_response("tx-100")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request(rail=PaymentRail.SOLANA))

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["blockchain"] == "SOL"

    def test_blockchain_base_in_payload(self):
        """Verify the POST body uses the correct blockchain for Base rail."""
        p = _provider()
        create_resp = _transfer_response("tx-200")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request(rail=PaymentRail.BASE))

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["blockchain"] == "BASE"

    def test_wallet_id_in_payload(self):
        """Verify walletId from constructor is included in POST body."""
        p = _provider(wallet_id="my-custom-wallet")
        create_resp = _transfer_response("tx-300")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request())

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["walletId"] == "my-custom-wallet"

    def test_token_address_in_payload(self):
        """Verify USDC token address for the chain is included in POST body."""
        p = _provider()
        create_resp = _transfer_response("tx-400")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request())

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "tokenAddress" in payload
        assert isinstance(payload["tokenAddress"], str)
        assert len(payload["tokenAddress"]) > 0

    def test_idempotency_key_is_uuid(self):
        """Verify payload has a valid UUID idempotencyKey."""
        p = _provider()
        create_resp = _transfer_response("tx-500")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request())

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "idempotencyKey" in payload
        parsed = uuid.UUID(payload["idempotencyKey"], version=4)
        assert str(parsed) == payload["idempotencyKey"]

    def test_entity_secret_ciphertext_in_payload(self):
        """Verify payload has entitySecretCiphertext."""
        p = _provider()
        create_resp = _transfer_response("tx-600")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext") as mock_cipher,
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request())

        mock_cipher.assert_called_once()
        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["entitySecretCiphertext"] == "fake-ciphertext"

    def test_create_payment_returns_processing(self):
        """create_payment returns immediately with PROCESSING — no polling."""
        p = _provider()
        create_resp = _transfer_response("tx-immediate")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            result = p.create_payment(_payment_request())

        # Only the POST call — no GET polling calls
        assert mock_req.call_count == 1
        assert result.payment_id == "tx-immediate"
        assert result.status == PaymentStatus.PROCESSING
        assert result.amount == 25.00

    def test_always_returns_processing(self):
        """create_payment always returns PROCESSING regardless of API state.

        The W3S API is async — callers use get_payment_status to poll.
        """
        p = _provider()
        # Even if API says COMPLETE, create_payment should return PROCESSING
        create_resp = _transfer_response("tx-fast", state="COMPLETE")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp),
        ):
            result = p.create_payment(_payment_request())

        assert result.status == PaymentStatus.PROCESSING
        assert result.success is False  # Not yet confirmed via polling

    def test_missing_transaction_id_raises(self):
        p = _provider()
        create_resp = _mock_response(json_data={"data": {}})
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp),
        ):
            with pytest.raises(PaymentError, match="transaction ID"):
                p.create_payment(_payment_request())

    def test_destination_address_required(self):
        """Empty metadata means no destination_address -> fail."""
        p = _provider()
        result = p.create_payment(_payment_request(metadata={}))

        assert result.status == PaymentStatus.FAILED
        assert result.success is False
        assert "destination_address" in (result.error or "").lower()

    def test_invalid_destination_address(self):
        """Bad address format -> fail."""
        p = _provider()
        result = p.create_payment(
            _payment_request(metadata={"destination_address": "not-valid!"})
        )

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    def test_amounts_formatted(self):
        """Verify amounts is a string array like ['25.00']."""
        p = _provider()
        create_resp = _transfer_response("tx-fmt")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request(amount=25.00))

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["amounts"] == ["25.00"]

    def test_fee_level_medium(self):
        """Verify feeLevel is 'MEDIUM' in the POST payload."""
        p = _provider()
        create_resp = _transfer_response("tx-fee")
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=create_resp) as mock_req,
        ):
            p.create_payment(_payment_request())

        call_args = mock_req.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["feeLevel"] == "MEDIUM"

    def test_missing_wallet_id_raises(self):
        """create_payment raises if wallet_id is not set."""
        p = _provider(wallet_id="")
        with pytest.raises(PaymentError, match="Wallet ID required"):
            p.create_payment(_payment_request())

    def test_missing_entity_secret_raises(self):
        """create_payment raises if entity_secret is not set."""
        p = _provider(entity_secret="")
        # Mock _get_public_key so we get to the entity_secret check
        with patch.object(p, "_get_public_key", return_value="fake-pem"):
            with pytest.raises(PaymentError, match="Entity secret required"):
                p.create_payment(_payment_request())


# ---------------------------------------------------------------------------
# get_payment_status
# ---------------------------------------------------------------------------


class TestGetPaymentStatus:
    def test_complete_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-001",
                    "state": "COMPLETE",
                    "amounts": ["25.00"],
                    "txHash": "hash-abc",
                    "sourceAddress": "0xSource...",
                    "destinationAddress": "0xDest...",
                    "blockchain": "SOL",
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-001")

        assert result.status == PaymentStatus.COMPLETED
        assert result.success is True
        assert result.payment_id == "tx-001"
        assert result.amount == 25.00

    def test_initiated_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-002",
                    "state": "INITIATED",
                    "amounts": ["10.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-002")

        assert result.status == PaymentStatus.PROCESSING
        assert result.success is False

    def test_sent_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-sent",
                    "state": "SENT",
                    "amounts": ["10.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-sent")

        assert result.status == PaymentStatus.PROCESSING

    def test_failed_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-003",
                    "state": "FAILED",
                    "amounts": ["50.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-003")

        assert result.status == PaymentStatus.FAILED
        assert result.success is False

    def test_cancelled_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-cancel",
                    "state": "CANCELLED",
                    "amounts": ["20.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-cancel")

        assert result.status == PaymentStatus.CANCELLED

    def test_with_tx_hash(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-004",
                    "state": "COMPLETE",
                    "amounts": ["15.00"],
                    "txHash": "0xDeadBeef...",
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-004")

        assert result.tx_hash == "0xDeadBeef..."

    def test_without_tx_hash(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-005",
                    "state": "INITIATED",
                    "amounts": ["15.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-005")

        assert result.tx_hash is None

    def test_currency_is_usdc(self):
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-007",
                    "state": "COMPLETE",
                    "amounts": ["1.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-007")

        assert result.currency == Currency.USDC

    def test_amount_from_amounts_array(self):
        """amounts: ['25.00'] -> result.amount = 25.0"""
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-amt",
                    "state": "COMPLETE",
                    "amounts": ["25.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-amt")

        assert result.amount == 25.0

    def test_empty_amounts_returns_zero(self):
        """Missing amounts array -> amount = 0.0"""
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-no-amt",
                    "state": "INITIATED",
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-no-amt")

        assert result.amount == 0.0

    def test_denied_maps_to_failed(self):
        """DENIED state maps to FAILED."""
        p = _provider()
        resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-denied",
                    "state": "DENIED",
                    "amounts": ["10.00"],
                },
            },
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_payment_status("tx-denied")

        assert result.status == PaymentStatus.FAILED


# ---------------------------------------------------------------------------
# refund_payment
# ---------------------------------------------------------------------------


class TestRefundPayment:
    def test_refund_success(self):
        """GET original tx, POST new transfer back to sourceAddress."""
        p = _provider()
        # First call: GET to fetch original transaction
        original_resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-orig",
                    "state": "COMPLETE",
                    "amounts": ["30.00"],
                    "sourceAddress": "0xSourceWallet",
                    "destinationAddress": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                    "blockchain": "SOL",
                },
            },
        })
        # Second call: POST to create refund transfer
        refund_resp = _mock_response(json_data={
            "data": {
                "id": "tx-refund-001",
                "state": "INITIATED",
            },
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(
                p._session, "request", side_effect=[original_resp, refund_resp]
            ),
        ):
            result = p.refund_payment("tx-orig")

        assert result.status == PaymentStatus.REFUNDED
        assert result.payment_id == "tx-refund-001"
        assert result.amount == 30.00
        # Refund is still processing (async), success indicates it was accepted
        assert result.success is False

    def test_refund_sends_to_source_address(self):
        """Verify refund transfer is sent to the original sourceAddress."""
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-orig",
                    "state": "COMPLETE",
                    "amounts": ["10.00"],
                    "sourceAddress": "0xRefundHere",
                    "blockchain": "SOL",
                },
            },
        })
        refund_resp = _mock_response(json_data={
            "data": {"id": "tx-refund-check", "state": "INITIATED"},
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(
                p._session, "request", side_effect=[original_resp, refund_resp]
            ) as mock_req,
        ):
            p.refund_payment("tx-orig")

        # Second call is the POST (refund transfer)
        refund_call = mock_req.call_args_list[1]
        payload = refund_call.kwargs.get("json") or refund_call[1].get("json")
        assert payload["destinationAddress"] == "0xRefundHere"

    def test_refund_non_complete_raises(self):
        """Cannot refund a transaction that isn't COMPLETE or CONFIRMED."""
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-pending",
                    "state": "INITIATED",
                    "amounts": ["10.00"],
                    "sourceAddress": "0xSource",
                    "blockchain": "SOL",
                },
            },
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=original_resp),
        ):
            with pytest.raises(PaymentError, match="Cannot refund"):
                p.refund_payment("tx-pending")

    def test_refund_api_error(self):
        """HTTP 500 on refund POST -> PaymentError."""
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-orig-3",
                    "state": "COMPLETE",
                    "amounts": ["10.00"],
                    "sourceAddress": "0xSourceWallet",
                    "destinationAddress": "addr-789",
                    "blockchain": "SOL",
                },
            },
        })
        error_resp = _mock_response(
            status_code=500, ok=False, json_data={"error": "internal"}
        )
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(
                p._session, "request", side_effect=[original_resp, error_resp]
            ),
        ):
            with pytest.raises(PaymentError, match="HTTP 500"):
                p.refund_payment("tx-orig-3")

    def test_refund_missing_source_address_raises(self):
        """Cannot refund if original tx has no source address."""
        p = _provider()
        original_resp = _mock_response(json_data={
            "data": {
                "transaction": {
                    "id": "tx-no-source",
                    "state": "COMPLETE",
                    "amounts": ["10.00"],
                    "blockchain": "SOL",
                },
            },
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=original_resp),
        ):
            with pytest.raises(PaymentError, match="source address"):
                p.refund_payment("tx-no-source")


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHTTPErrors:
    def test_timeout(self):
        p = _provider()
        with patch.object(
            p._session, "request", side_effect=requests.exceptions.Timeout()
        ):
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
        resp = _mock_response(
            status_code=401, ok=False, json_data={"error": "Unauthorized"}
        )
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="HTTP 401") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "HTTP_401"

    def test_500_server_error(self):
        p = _provider()
        resp = _mock_response(
            status_code=500, ok=False, json_data={"error": "Internal"}
        )
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="HTTP 500") as exc_info:
                p.get_payment_status("tx-000")
        assert exc_info.value.code == "HTTP_500"

    def test_403_forbidden(self):
        p = _provider()
        resp = _mock_response(
            status_code=403, ok=False, json_data={"error": "Forbidden"}
        )
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
# Network / chain mapping
# ---------------------------------------------------------------------------


class TestNetworkMapping:
    def test_solana_maps_to_sol(self):
        p = _provider()
        assert p._resolve_chain(PaymentRail.SOLANA) == "SOL"

    def test_base_maps_to_base(self):
        p = _provider()
        assert p._resolve_chain(PaymentRail.BASE) == "BASE"

    def test_fallback_to_default(self):
        """Unmapped rail falls back to the default network mapping."""
        p = _provider(default_network="solana")
        result = p._resolve_chain(PaymentRail.CIRCLE)
        assert result == "SOL"

    def test_fallback_to_default_base(self):
        p = _provider(default_network="base")
        result = p._resolve_chain(PaymentRail.CIRCLE)
        assert result == "BASE"

    def test_stripe_rail_falls_back_to_default(self):
        p = _provider(default_network="solana")
        result = p._resolve_chain(PaymentRail.STRIPE)
        assert result == "SOL"

    def test_usdc_address_sol(self):
        p = _provider()
        addr = p._get_usdc_token_address("SOL")
        assert addr == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def test_usdc_address_base(self):
        p = _provider()
        addr = p._get_usdc_token_address("BASE")
        assert addr == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def test_usdc_address_unsupported_raises(self):
        p = _provider()
        with pytest.raises(PaymentError, match="No USDC token address"):
            p._get_usdc_token_address("UNSUPPORTED-CHAIN")


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


# ---------------------------------------------------------------------------
# Setup methods (W3S-specific)
# ---------------------------------------------------------------------------


class TestSetupMethods:
    def test_setup_entity_secret(self):
        """Mocks POST to register entity secret, verifies returns dict with hex."""
        p = _provider()
        # setup_entity_secret calls _get_public_key then _encrypt_entity_secret
        # then POSTs to /v1/w3s/config/entity/entitySecret
        public_key_resp = _mock_response(json_data={
            "data": {"publicKey": "fake-pem"},
        })
        register_resp = _mock_response(json_data={
            "data": {"recoveryFile": "base64-recovery-data"},
        })
        with (
            patch.object(
                p._session, "request", side_effect=[public_key_resp, register_resp]
            ),
            patch(
                "kiln.payments.circle_provider._encrypt_entity_secret",
                return_value="fake-ciphertext",
            ),
        ):
            result = p.setup_entity_secret()

        assert isinstance(result, dict)
        assert "entity_secret" in result
        assert len(result["entity_secret"]) == 64  # 32 bytes hex
        assert "recovery_file" in result

    def test_setup_wallet(self):
        """Mocks wallet set + wallet creation, verifies returns dict."""
        p = _provider()
        wallet_set_resp = _mock_response(json_data={
            "data": {"walletSet": {"id": "ws-001"}},
        })
        wallet_resp = _mock_response(json_data={
            "data": {"wallets": [{"id": "wallet-new-001", "address": "0xNew..."}]},
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(
                p._session,
                "request",
                side_effect=[wallet_set_resp, wallet_resp],
            ),
        ):
            result = p.setup_wallet()

        assert isinstance(result, dict)
        assert result["wallet_id"] == "wallet-new-001"
        assert result["wallet_set_id"] == "ws-001"
        assert result["address"] == "0xNew..."

    def test_setup_wallet_reuses_existing_wallet_set(self):
        """If wallet_set_id already set, skip wallet set creation."""
        p = _provider()
        p._wallet_set_id = "existing-ws"
        wallet_resp = _mock_response(json_data={
            "data": {"wallets": [{"id": "wallet-002", "address": "0xAddr2"}]},
        })
        with (
            patch.object(p, "_get_entity_secret_ciphertext", return_value="fake-ciphertext"),
            patch.object(p._session, "request", return_value=wallet_resp) as mock_req,
        ):
            result = p.setup_wallet()

        # Only 1 call (wallet creation), no wallet set creation
        assert mock_req.call_count == 1
        assert result["wallet_id"] == "wallet-002"

    def test_get_wallet_balance(self):
        """Mocks GET balances, verifies returns dict with balances."""
        p = _provider()
        balance_resp = _mock_response(json_data={
            "data": {
                "tokenBalances": [
                    {
                        "token": {"id": "tok-1", "symbol": "USDC", "blockchain": "SOL"},
                        "amount": "150.50",
                    },
                ],
            },
        })
        with patch.object(p._session, "request", return_value=balance_resp):
            result = p.get_wallet_balance()

        assert isinstance(result, dict)
        assert result["wallet_id"] == "test-wallet-uuid"
        assert len(result["balances"]) == 1
        assert result["balances"][0]["symbol"] == "USDC"
        assert result["balances"][0]["amount"] == "150.50"

    def test_get_wallet_balance_no_wallet_id_raises(self):
        """get_wallet_balance raises if no wallet_id configured."""
        p = _provider(wallet_id="")
        with pytest.raises(PaymentError, match="Wallet ID required"):
            p.get_wallet_balance()

    def test_get_wallet_balance_empty(self):
        """Empty token balances returns empty list."""
        p = _provider()
        balance_resp = _mock_response(json_data={
            "data": {"tokenBalances": []},
        })
        with patch.object(p._session, "request", return_value=balance_resp):
            result = p.get_wallet_balance()

        assert result["balances"] == []


# ---------------------------------------------------------------------------
# Entity secret ciphertext
# ---------------------------------------------------------------------------


class TestEntitySecretCiphertext:
    def test_missing_entity_secret_raises(self):
        """_get_entity_secret_ciphertext raises if entity_secret is empty."""
        p = _provider(entity_secret="")
        with pytest.raises(PaymentError, match="Entity secret required"):
            p._get_entity_secret_ciphertext()

    def test_caches_public_key(self):
        """_get_public_key fetches once, then caches."""
        p = _provider()
        pk_resp = _mock_response(json_data={
            "data": {"publicKey": "-----BEGIN RSA PUBLIC KEY-----\nfake\n-----END RSA PUBLIC KEY-----"},
        })
        with patch.object(p._session, "request", return_value=pk_resp) as mock_req:
            pem1 = p._get_public_key()
            pem2 = p._get_public_key()

        assert mock_req.call_count == 1  # Only fetched once
        assert pem1 == pem2

    def test_missing_public_key_raises(self):
        """_get_public_key raises if API returns empty key."""
        p = _provider()
        resp = _mock_response(json_data={"data": {"publicKey": ""}})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(PaymentError, match="public key"):
                p._get_public_key()
