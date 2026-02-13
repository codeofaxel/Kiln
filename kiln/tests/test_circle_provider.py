"""Tests for kiln.circle_provider -- Circle USDC payment provider.

Covers:
- CircleProvider construction and validation
- Address validation (Ethereum/Base 42-char, Solana base58)
- create_payment: happy path, invalid address, zero amount, missing transfer ID
- check_payment_status: complete, pending, failed, empty ID
- refund_payment: outbound payout, inbound transfer, wallet-to-wallet, zero amount
- list_payments: pagination, empty results
- HTTP error handling: timeout, connection error, 4xx/5xx, non-JSON response
- Dataclass to_dict methods
- Status mapping for all Circle statuses
"""

from __future__ import annotations

import pytest
import responses

from kiln.circle_provider import (
    BlockchainNetwork,
    CirclePaymentError,
    CirclePaymentStatus,
    CircleProvider,
    PaymentResult,
    PaymentSummary,
    RefundResult,
    validate_blockchain_address,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SANDBOX_BASE = "https://api-sandbox.circle.com/v1"
_VALID_ETH_ADDRESS = "0x" + "a1" * 20  # 42 chars
_VALID_SOL_ADDRESS = "So11111111111111111111111111111112"  # 32+ chars base58


def _make_provider(
    *,
    api_key: str = "test-key-123",
    environment: str = "sandbox",
    default_network: str = "solana",
) -> CircleProvider:
    """Helper to construct a CircleProvider with test defaults."""
    return CircleProvider(
        api_key=api_key,
        environment=environment,
        default_network=default_network,
    )


def _transfer_response(
    transfer_id: str = "txn-001",
    status: str = "pending",
    amount: str = "25.00",
    chain: str = "SOL",
    address: str = _VALID_SOL_ADDRESS,
    tx_hash: str | None = None,
) -> dict:
    """Build a Circle transfer API response body."""
    data: dict = {
        "id": transfer_id,
        "status": status,
        "amount": {"amount": amount, "currency": "USD"},
        "source": {"type": "wallet", "id": "master"},
        "destination": {
            "type": "blockchain",
            "chain": chain,
            "address": address,
        },
    }
    if tx_hash:
        data["transactionHash"] = tx_hash
    return {"data": data}


# ---------------------------------------------------------------------------
# CircleProvider construction
# ---------------------------------------------------------------------------

class TestCircleProviderConstructor:
    """Tests for CircleProvider.__init__ validation."""

    def test_missing_api_key_raises(self):
        with pytest.raises(ValueError, match="Circle API key required"):
            CircleProvider(api_key="", environment="sandbox")

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_CIRCLE_API_KEY", "env-key")
        provider = CircleProvider(environment="sandbox")
        assert provider.environment == "sandbox"

    def test_invalid_environment_raises(self):
        with pytest.raises(ValueError, match="Invalid Circle environment"):
            CircleProvider(api_key="key", environment="staging")

    def test_invalid_default_network_raises(self):
        with pytest.raises(ValueError, match="Invalid default network"):
            CircleProvider(
                api_key="key", environment="sandbox", default_network="polygon",
            )

    def test_sandbox_environment(self):
        provider = _make_provider(environment="sandbox")
        assert provider.environment == "sandbox"
        assert provider.default_network == BlockchainNetwork.SOLANA

    def test_production_environment(self):
        provider = _make_provider(environment="production")
        assert provider.environment == "production"

    def test_base_default_network(self):
        provider = _make_provider(default_network="base")
        assert provider.default_network == BlockchainNetwork.BASE

    def test_repr(self):
        provider = _make_provider()
        r = repr(provider)
        assert "CircleProvider" in r
        assert "sandbox" in r
        assert "solana" in r


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

class TestAddressValidation:
    """Tests for validate_blockchain_address."""

    def test_valid_ethereum_address(self):
        assert validate_blockchain_address(_VALID_ETH_ADDRESS, BlockchainNetwork.BASE) is None

    def test_valid_solana_address(self):
        assert validate_blockchain_address(_VALID_SOL_ADDRESS, BlockchainNetwork.SOLANA) is None

    def test_empty_address(self):
        err = validate_blockchain_address("", BlockchainNetwork.SOLANA)
        assert err is not None
        assert "required" in err.lower()

    def test_invalid_ethereum_too_short(self):
        err = validate_blockchain_address("0xabc", BlockchainNetwork.BASE)
        assert err is not None
        assert "42-character" in err

    def test_invalid_ethereum_missing_prefix(self):
        err = validate_blockchain_address("a1" * 20, BlockchainNetwork.BASE)
        assert err is not None

    def test_invalid_solana_bad_chars(self):
        # 'O' and '0' are not in base58 alphabet (0 is excluded, O is excluded)
        err = validate_blockchain_address("O" * 32, BlockchainNetwork.SOLANA)
        assert err is not None
        assert "base58" in err

    def test_invalid_solana_too_short(self):
        err = validate_blockchain_address("ABC", BlockchainNetwork.SOLANA)
        assert err is not None

    def test_valid_solana_44_chars(self):
        # Maximum length Solana address (44 chars base58).
        addr = "1" * 44
        assert validate_blockchain_address(addr, BlockchainNetwork.SOLANA) is None

    def test_solana_45_chars_invalid(self):
        addr = "1" * 45
        err = validate_blockchain_address(addr, BlockchainNetwork.SOLANA)
        assert err is not None


# ---------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------

class TestCreatePayment:
    """Tests for CircleProvider.create_payment."""

    @responses.activate
    def test_happy_path_solana(self):
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(transfer_id="txn-sol-1", status="pending"),
            status=200,
        )

        provider = _make_provider()
        result = provider.create_payment(
            amount=25.00,
            destination_address=_VALID_SOL_ADDRESS,
            job_id="job-42",
        )

        assert isinstance(result, PaymentResult)
        assert result.payment_id == "txn-sol-1"
        assert result.status == CirclePaymentStatus.PROCESSING
        assert result.amount == 25.00
        assert result.network == BlockchainNetwork.SOLANA
        assert result.success is False  # not yet completed
        assert result.error is None

    @responses.activate
    def test_happy_path_base(self):
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(
                transfer_id="txn-base-1",
                status="pending",
                chain="ETH-BASE",
                address=_VALID_ETH_ADDRESS,
            ),
            status=200,
        )

        provider = _make_provider(default_network="base")
        result = provider.create_payment(
            amount=50.00,
            destination_address=_VALID_ETH_ADDRESS,
            network="base",
        )

        assert result.payment_id == "txn-base-1"
        assert result.network == BlockchainNetwork.BASE

    @responses.activate
    def test_completed_status(self):
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(transfer_id="txn-fast", status="complete"),
            status=200,
        )

        provider = _make_provider()
        result = provider.create_payment(
            amount=10.00,
            destination_address=_VALID_SOL_ADDRESS,
        )

        assert result.success is True
        assert result.status == CirclePaymentStatus.COMPLETED

    def test_zero_amount_returns_failed(self):
        provider = _make_provider()
        result = provider.create_payment(
            amount=0.0,
            destination_address=_VALID_SOL_ADDRESS,
        )

        assert result.success is False
        assert result.status == CirclePaymentStatus.FAILED
        assert "positive" in result.error

    def test_negative_amount_returns_failed(self):
        provider = _make_provider()
        result = provider.create_payment(
            amount=-5.0,
            destination_address=_VALID_SOL_ADDRESS,
        )

        assert result.success is False
        assert result.status == CirclePaymentStatus.FAILED

    def test_invalid_address_returns_failed(self):
        provider = _make_provider()
        result = provider.create_payment(
            amount=10.00,
            destination_address="bad-address",
        )

        assert result.success is False
        assert result.status == CirclePaymentStatus.FAILED
        assert result.error is not None

    def test_invalid_network_raises(self):
        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="Unsupported network"):
            provider.create_payment(
                amount=10.00,
                destination_address=_VALID_SOL_ADDRESS,
                network="polygon",
            )

    @responses.activate
    def test_missing_transfer_id_raises(self):
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": {"status": "pending"}},
            status=200,
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="did not return a transfer ID"):
            provider.create_payment(
                amount=10.00,
                destination_address=_VALID_SOL_ADDRESS,
            )

    @responses.activate
    def test_description_and_job_id_in_payload(self):
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(),
            status=200,
        )

        provider = _make_provider()
        provider.create_payment(
            amount=5.00,
            destination_address=_VALID_SOL_ADDRESS,
            job_id="job-99",
            description="Test payment",
        )

        body = responses.calls[0].request.body
        assert b"job-99" in body
        assert b"Test payment" in body


# ---------------------------------------------------------------------------
# check_payment_status
# ---------------------------------------------------------------------------

class TestCheckPaymentStatus:
    """Tests for CircleProvider.check_payment_status."""

    @responses.activate
    def test_completed_transfer(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-001",
            json=_transfer_response(
                transfer_id="txn-001",
                status="complete",
                amount="100.00",
                tx_hash="abc123hash",
            ),
            status=200,
        )

        provider = _make_provider()
        result = provider.check_payment_status("txn-001")

        assert result.success is True
        assert result.status == CirclePaymentStatus.COMPLETED
        assert result.amount == 100.00
        assert result.tx_hash == "abc123hash"

    @responses.activate
    def test_pending_transfer(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-002",
            json=_transfer_response(transfer_id="txn-002", status="pending"),
            status=200,
        )

        provider = _make_provider()
        result = provider.check_payment_status("txn-002")

        assert result.success is False
        assert result.status == CirclePaymentStatus.PROCESSING

    @responses.activate
    def test_failed_transfer(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-003",
            json=_transfer_response(transfer_id="txn-003", status="failed"),
            status=200,
        )

        provider = _make_provider()
        result = provider.check_payment_status("txn-003")

        assert result.success is False
        assert result.status == CirclePaymentStatus.FAILED

    @responses.activate
    def test_unknown_status_maps_to_pending(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-004",
            json=_transfer_response(transfer_id="txn-004", status="unknown_state"),
            status=200,
        )

        provider = _make_provider()
        result = provider.check_payment_status("txn-004")

        assert result.status == CirclePaymentStatus.PENDING

    def test_empty_payment_id_raises(self):
        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="payment_id is required"):
            provider.check_payment_status("")


# ---------------------------------------------------------------------------
# refund_payment
# ---------------------------------------------------------------------------

class TestRefundPayment:
    """Tests for CircleProvider.refund_payment."""

    @responses.activate
    def test_outbound_payout_refund(self):
        # GET original transfer (outbound: wallet -> blockchain).
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-orig",
            json=_transfer_response(
                transfer_id="txn-orig",
                status="complete",
                amount="50.00",
            ),
            status=200,
        )
        # POST refund transfer.
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(
                transfer_id="txn-refund-1",
                status="pending",
                amount="50.00",
            ),
            status=200,
        )

        provider = _make_provider()
        result = provider.refund_payment("txn-orig")

        assert isinstance(result, RefundResult)
        assert result.success is True
        assert result.refund_id == "txn-refund-1"
        assert result.original_payment_id == "txn-orig"
        assert result.status == CirclePaymentStatus.REFUNDED
        assert result.amount == 50.00

    @responses.activate
    def test_inbound_transfer_refund(self):
        # Original transfer: blockchain -> wallet (inbound).
        original_resp = {
            "data": {
                "id": "txn-inbound",
                "status": "complete",
                "amount": {"amount": "30.00", "currency": "USD"},
                "source": {
                    "type": "blockchain",
                    "chain": "SOL",
                    "address": _VALID_SOL_ADDRESS,
                },
                "destination": {"type": "wallet", "id": "master"},
            },
        }
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-inbound",
            json=original_resp,
            status=200,
        )
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(
                transfer_id="txn-refund-2",
                status="pending",
                amount="30.00",
            ),
            status=200,
        )

        provider = _make_provider()
        result = provider.refund_payment("txn-inbound")

        assert result.success is True
        assert result.refund_id == "txn-refund-2"
        assert result.amount == 30.00

    @responses.activate
    def test_wallet_to_wallet_refund(self):
        original_resp = {
            "data": {
                "id": "txn-w2w",
                "status": "complete",
                "amount": {"amount": "15.00", "currency": "USD"},
                "source": {"type": "wallet", "id": "wallet-A"},
                "destination": {"type": "wallet", "id": "wallet-B"},
            },
        }
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-w2w",
            json=original_resp,
            status=200,
        )
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={
                "data": {
                    "id": "txn-refund-3",
                    "status": "pending",
                    "amount": {"amount": "15.00", "currency": "USD"},
                },
            },
            status=200,
        )

        provider = _make_provider()
        result = provider.refund_payment("txn-w2w")

        assert result.success is True
        assert result.refund_id == "txn-refund-3"

    @responses.activate
    def test_refund_missing_id_raises(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-bad",
            json=_transfer_response(transfer_id="txn-bad", amount="10.00"),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": {"status": "pending"}},
            status=200,
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="missing transfer ID"):
            provider.refund_payment("txn-bad")

    @responses.activate
    def test_refund_zero_amount_raises(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-zero",
            json=_transfer_response(transfer_id="txn-zero", amount="0.00"),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(
                transfer_id="txn-refund-zero",
                amount="0.00",
            ),
            status=200,
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="zero amount"):
            provider.refund_payment("txn-zero")

    @responses.activate
    def test_refund_failed_status(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/txn-fail",
            json=_transfer_response(
                transfer_id="txn-fail",
                status="complete",
                amount="20.00",
            ),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{_SANDBOX_BASE}/v1/transfers",
            json=_transfer_response(
                transfer_id="txn-refund-fail",
                status="failed",
                amount="20.00",
            ),
            status=200,
        )

        provider = _make_provider()
        result = provider.refund_payment("txn-fail")

        assert result.success is False
        assert result.status == CirclePaymentStatus.FAILED

    def test_refund_empty_payment_id_raises(self):
        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="payment_id is required"):
            provider.refund_payment("")


# ---------------------------------------------------------------------------
# list_payments
# ---------------------------------------------------------------------------

class TestListPayments:
    """Tests for CircleProvider.list_payments."""

    @responses.activate
    def test_list_returns_summaries(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={
                "data": [
                    {
                        "id": "txn-a",
                        "status": "complete",
                        "amount": {"amount": "10.00", "currency": "USD"},
                        "destination": {"type": "blockchain", "chain": "SOL"},
                        "createDate": "2025-01-01T00:00:00Z",
                    },
                    {
                        "id": "txn-b",
                        "status": "pending",
                        "amount": {"amount": "20.00", "currency": "USD"},
                        "destination": {"type": "blockchain", "chain": "ETH-BASE"},
                        "createDate": "2025-01-02T00:00:00Z",
                    },
                ],
            },
            status=200,
        )

        provider = _make_provider()
        payments = provider.list_payments(limit=10)

        assert len(payments) == 2
        assert isinstance(payments[0], PaymentSummary)
        assert payments[0].payment_id == "txn-a"
        assert payments[0].status == CirclePaymentStatus.COMPLETED
        assert payments[0].amount == 10.00
        assert payments[0].network == BlockchainNetwork.SOLANA
        assert payments[1].network == BlockchainNetwork.BASE

    @responses.activate
    def test_list_empty_results(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": []},
            status=200,
        )

        provider = _make_provider()
        payments = provider.list_payments()

        assert payments == []

    @responses.activate
    def test_list_non_array_data_returns_empty(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": {"unexpected": "object"}},
            status=200,
        )

        provider = _make_provider()
        payments = provider.list_payments()

        assert payments == []

    @responses.activate
    def test_list_pagination_params(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": []},
            status=200,
        )

        provider = _make_provider()
        provider.list_payments(limit=5, page_after="cursor-abc")

        assert "pageSize=5" in responses.calls[0].request.url
        assert "pageAfter=cursor-abc" in responses.calls[0].request.url

    @responses.activate
    def test_list_limit_clamping(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers",
            json={"data": []},
            status=200,
        )

        provider = _make_provider()
        provider.list_payments(limit=999)

        assert "pageSize=100" in responses.calls[0].request.url


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------

class TestHttpErrorHandling:
    """Tests for HTTP failure modes in CircleProvider._request."""

    @responses.activate
    def test_http_400_raises(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/bad",
            json={"error": "not found"},
            status=400,
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="HTTP 400") as exc_info:
            provider.check_payment_status("bad")

        assert exc_info.value.code == "HTTP_400"

    @responses.activate
    def test_http_500_raises(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/err",
            json={"error": "internal"},
            status=500,
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="HTTP 500") as exc_info:
            provider.check_payment_status("err")

        assert exc_info.value.code == "HTTP_500"

    @responses.activate
    def test_timeout_raises(self):
        import requests as req_lib

        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/slow",
            body=req_lib.exceptions.Timeout("timed out"),
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="timeout") as exc_info:
            provider.check_payment_status("slow")

        assert exc_info.value.code == "TIMEOUT"

    @responses.activate
    def test_connection_error_raises(self):
        import requests as req_lib

        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/offline",
            body=req_lib.exceptions.ConnectionError("refused"),
        )

        provider = _make_provider()
        with pytest.raises(CirclePaymentError, match="Cannot reach") as exc_info:
            provider.check_payment_status("offline")

        assert exc_info.value.code == "CONNECTION_ERROR"

    @responses.activate
    def test_non_json_success_response(self):
        responses.add(
            responses.GET,
            f"{_SANDBOX_BASE}/v1/transfers/no-json",
            body="OK",
            status=200,
            content_type="text/plain",
        )

        provider = _make_provider()
        # Should not raise -- falls back to {"status": "ok"}.
        result = provider.check_payment_status("no-json")
        assert result.status == CirclePaymentStatus.PENDING


# ---------------------------------------------------------------------------
# Dataclass to_dict methods
# ---------------------------------------------------------------------------

class TestDataclassToDict:
    """Tests for dataclass .to_dict() serialisation."""

    def test_payment_result_to_dict(self):
        result = PaymentResult(
            success=True,
            payment_id="txn-1",
            status=CirclePaymentStatus.COMPLETED,
            amount=42.50,
            network=BlockchainNetwork.SOLANA,
            tx_hash="0xabc",
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["payment_id"] == "txn-1"
        assert d["status"] == "completed"
        assert d["amount"] == 42.50
        assert d["network"] == "solana"
        assert d["tx_hash"] == "0xabc"

    def test_payment_result_to_dict_no_optionals(self):
        result = PaymentResult(
            success=False,
            payment_id="",
            status=CirclePaymentStatus.FAILED,
            amount=0,
        )
        d = result.to_dict()
        assert "tx_hash" not in d
        assert "error" not in d
        assert d["network"] is None

    def test_payment_result_to_dict_with_error(self):
        result = PaymentResult(
            success=False,
            payment_id="",
            status=CirclePaymentStatus.FAILED,
            amount=0,
            error="Something broke",
        )
        d = result.to_dict()
        assert d["error"] == "Something broke"

    def test_refund_result_to_dict(self):
        result = RefundResult(
            success=True,
            refund_id="ref-1",
            original_payment_id="txn-1",
            status=CirclePaymentStatus.REFUNDED,
            amount=25.00,
            network=BlockchainNetwork.BASE,
            tx_hash="0xdef",
        )
        d = result.to_dict()
        assert d["refund_id"] == "ref-1"
        assert d["original_payment_id"] == "txn-1"
        assert d["status"] == "refunded"
        assert d["network"] == "base"
        assert d["tx_hash"] == "0xdef"

    def test_refund_result_to_dict_no_optionals(self):
        result = RefundResult(
            success=True,
            refund_id="ref-2",
            original_payment_id="txn-2",
            status=CirclePaymentStatus.REFUNDED,
            amount=10.00,
        )
        d = result.to_dict()
        assert "tx_hash" not in d
        assert "error" not in d

    def test_payment_summary_to_dict(self):
        s = PaymentSummary(
            payment_id="txn-s1",
            status=CirclePaymentStatus.COMPLETED,
            amount=99.99,
            network=BlockchainNetwork.SOLANA,
            created_at="2025-06-01T00:00:00Z",
        )
        d = s.to_dict()
        assert d["payment_id"] == "txn-s1"
        assert d["status"] == "completed"
        assert d["network"] == "solana"
        assert d["created_at"] == "2025-06-01T00:00:00Z"


# ---------------------------------------------------------------------------
# CirclePaymentError
# ---------------------------------------------------------------------------

class TestCirclePaymentError:
    """Tests for the CirclePaymentError exception."""

    def test_message_and_code(self):
        exc = CirclePaymentError("bad request", code="HTTP_400")
        assert str(exc) == "bad request"
        assert exc.code == "HTTP_400"

    def test_default_code_is_none(self):
        exc = CirclePaymentError("oops")
        assert exc.code is None


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------

class TestEnumValues:
    """Verify enum string values for JSON serialisation."""

    def test_payment_status_values(self):
        assert CirclePaymentStatus.PENDING.value == "pending"
        assert CirclePaymentStatus.PROCESSING.value == "processing"
        assert CirclePaymentStatus.COMPLETED.value == "completed"
        assert CirclePaymentStatus.FAILED.value == "failed"
        assert CirclePaymentStatus.REFUNDED.value == "refunded"

    def test_blockchain_network_values(self):
        assert BlockchainNetwork.SOLANA.value == "solana"
        assert BlockchainNetwork.BASE.value == "base"
