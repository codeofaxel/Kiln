"""Circle USDC payment provider for Kiln billing.

Handles USDC stablecoin payments on Solana and Base networks via the
`Circle API <https://developers.circle.com>`_.  Designed to integrate
with :mod:`kiln.billing` -- the :class:`CircleProvider` creates, checks,
and refunds USDC transfers that can be recorded in the
:class:`~kiln.billing.BillingLedger`.

Environment variables
---------------------
``KILN_CIRCLE_API_KEY``
    API key for authenticating with the Circle API.
``KILN_CIRCLE_ENVIRONMENT``
    ``"sandbox"`` (default) or ``"production"``.  Controls the base URL.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URLS: Dict[str, str] = {
    "sandbox": "https://api-sandbox.circle.com/v1",
    "production": "https://api.circle.com/v1",
}

_ETHEREUM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CirclePaymentStatus(str, Enum):
    """Payment lifecycle states for Circle transfers."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class BlockchainNetwork(str, Enum):
    """Supported blockchain networks for USDC transfers."""

    SOLANA = "solana"
    BASE = "base"


# Map Circle API status strings to our enum.
_STATUS_MAP: Dict[str, CirclePaymentStatus] = {
    "complete": CirclePaymentStatus.COMPLETED,
    "pending": CirclePaymentStatus.PROCESSING,
    "failed": CirclePaymentStatus.FAILED,
}

# Map our network enum to Circle's chain identifiers.
_NETWORK_TO_CHAIN: Dict[BlockchainNetwork, str] = {
    BlockchainNetwork.SOLANA: "SOL",
    BlockchainNetwork.BASE: "ETH-BASE",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CirclePaymentError(Exception):
    """Exception raised for Circle payment failures.

    :param message: Human-readable error description.
    :param code: Machine-readable error code (e.g. ``"TIMEOUT"``).
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PaymentResult:
    """Outcome of a Circle payment operation.

    :param success: ``True`` if the payment completed successfully.
    :param payment_id: Circle transfer ID.
    :param status: Current payment lifecycle status.
    :param amount: Payment amount in USDC.
    :param network: Blockchain network used.
    :param tx_hash: On-chain transaction hash (if available).
    :param error: Human-readable error message (if failed).
    """

    success: bool
    payment_id: str
    status: CirclePaymentStatus
    amount: float
    network: Optional[BlockchainNetwork] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        d: Dict[str, Any] = {
            "success": self.success,
            "payment_id": self.payment_id,
            "status": self.status.value,
            "amount": self.amount,
            "network": self.network.value if self.network else None,
        }
        if self.tx_hash is not None:
            d["tx_hash"] = self.tx_hash
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class RefundResult:
    """Outcome of a Circle refund operation.

    :param success: ``True`` if the refund was accepted (may still be processing).
    :param refund_id: Circle transfer ID for the refund.
    :param original_payment_id: The payment ID that was refunded.
    :param status: Current refund lifecycle status.
    :param amount: Refund amount in USDC.
    :param network: Blockchain network used.
    :param tx_hash: On-chain transaction hash (if available).
    :param error: Human-readable error message (if failed).
    """

    success: bool
    refund_id: str
    original_payment_id: str
    status: CirclePaymentStatus
    amount: float
    network: Optional[BlockchainNetwork] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        d: Dict[str, Any] = {
            "success": self.success,
            "refund_id": self.refund_id,
            "original_payment_id": self.original_payment_id,
            "status": self.status.value,
            "amount": self.amount,
            "network": self.network.value if self.network else None,
        }
        if self.tx_hash is not None:
            d["tx_hash"] = self.tx_hash
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class PaymentSummary:
    """Lightweight summary of a payment for list results.

    :param payment_id: Circle transfer ID.
    :param status: Current lifecycle status.
    :param amount: Amount in USDC.
    :param network: Blockchain network.
    :param created_at: ISO-8601 creation timestamp from Circle.
    """

    payment_id: str
    status: CirclePaymentStatus
    amount: float
    network: Optional[BlockchainNetwork] = None
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "payment_id": self.payment_id,
            "status": self.status.value,
            "amount": self.amount,
            "network": self.network.value if self.network else None,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

def validate_blockchain_address(
    address: str,
    network: BlockchainNetwork,
) -> Optional[str]:
    """Validate a blockchain address format.

    :param address: The address string to validate.
    :param network: Target blockchain network.
    :returns: ``None`` if valid, or a human-readable error string.
    """
    if not address:
        return "Address is required"

    if network == BlockchainNetwork.BASE:
        if not _ETHEREUM_ADDRESS_RE.match(address):
            return (
                f"Invalid Ethereum/Base address: expected 42-character "
                f"hex string starting with 0x, got {len(address)} chars"
            )
    elif network == BlockchainNetwork.SOLANA:
        if not _SOLANA_ADDRESS_RE.match(address):
            return (
                f"Invalid Solana address: expected 32-44 character "
                f"base58 string, got {len(address)} chars"
            )
    return None


# ---------------------------------------------------------------------------
# Circle provider
# ---------------------------------------------------------------------------

class CircleProvider:
    """USDC payment provider backed by the Circle Transfers API.

    Routes USDC payments to Solana or Base depending on the requested
    network.

    :param api_key: Circle API key.  Falls back to ``KILN_CIRCLE_API_KEY``.
    :param environment: ``"sandbox"`` or ``"production"``.  Falls back to
        ``KILN_CIRCLE_ENVIRONMENT`` (default ``"sandbox"``).
    :param default_network: Default blockchain when the caller does not
        specify one (``"solana"`` or ``"base"``).
    :raises ValueError: If no API key is available or environment is invalid.

    Example::

        provider = CircleProvider()
        result = provider.create_payment(
            amount=25.00,
            destination_address="So1ana...",
            job_id="job-42",
        )
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        environment: Optional[str] = None,
        default_network: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CIRCLE_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "Circle API key required. "
                "Set KILN_CIRCLE_API_KEY or pass api_key=."
            )

        env = environment or os.environ.get("KILN_CIRCLE_ENVIRONMENT", "sandbox")
        if env not in _BASE_URLS:
            raise ValueError(
                f"Invalid Circle environment {env!r}. "
                f"Must be one of: {', '.join(sorted(_BASE_URLS))}."
            )
        self._environment = env
        self._base_url = _BASE_URLS[env]

        net = default_network or os.environ.get("KILN_CIRCLE_DEFAULT_NETWORK", "solana")
        try:
            self._default_network = BlockchainNetwork(net)
        except ValueError:
            raise ValueError(
                f"Invalid default network {net!r}. "
                f"Must be one of: {', '.join(n.value for n in BlockchainNetwork)}."
            )

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # -- Properties -----------------------------------------------------------

    @property
    def environment(self) -> str:
        """Current Circle environment (``"sandbox"`` or ``"production"``)."""
        return self._environment

    @property
    def default_network(self) -> BlockchainNetwork:
        """Default blockchain network for payments."""
        return self._default_network

    # -- Internal HTTP helpers ------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute an authenticated HTTP request to the Circle API.

        :param method: HTTP method (``GET``, ``POST``, etc.).
        :param path: API path (e.g. ``/v1/transfers``).
        :param kwargs: Extra keyword arguments forwarded to
            :meth:`requests.Session.request`.
        :returns: Parsed JSON response body.
        :raises CirclePaymentError: On timeout, connection failure, or HTTP error.
        """
        url = self._url(path)
        try:
            response = self._session.request(method, url, timeout=30, **kwargs)

            if not response.ok:
                raise CirclePaymentError(
                    f"Circle API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )

            try:
                return response.json()
            except ValueError:
                return {"status": "ok"}

        except Timeout as exc:
            raise CirclePaymentError(
                "Circle API timeout",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise CirclePaymentError(
                "Cannot reach Circle API",
                code="CONNECTION_ERROR",
            ) from exc
        except CirclePaymentError:
            raise
        except RequestException as exc:
            raise CirclePaymentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    def _resolve_chain(self, network: Optional[BlockchainNetwork] = None) -> str:
        """Map a :class:`BlockchainNetwork` to a Circle chain identifier."""
        net = network or self._default_network
        return _NETWORK_TO_CHAIN.get(net, "SOL")

    def _resolve_network(self, network: Optional[str] = None) -> BlockchainNetwork:
        """Resolve a network string or fall back to the default."""
        if network is None:
            return self._default_network
        try:
            return BlockchainNetwork(network)
        except ValueError:
            raise CirclePaymentError(
                f"Unsupported network {network!r}. "
                f"Must be one of: {', '.join(n.value for n in BlockchainNetwork)}.",
                code="INVALID_NETWORK",
            )

    # -- Public API -----------------------------------------------------------

    def create_payment(
        self,
        amount: float,
        destination_address: str,
        *,
        job_id: Optional[str] = None,
        network: Optional[str] = None,
        description: Optional[str] = None,
    ) -> PaymentResult:
        """Create a USDC transfer via the Circle API.

        Sends a ``POST /v1/transfers`` request and returns immediately
        with the initial transfer status (typically ``PROCESSING``).
        Use :meth:`check_payment_status` to poll for finality.

        :param amount: USDC amount to transfer.
        :param destination_address: Blockchain wallet address.
        :param job_id: Optional Kiln job ID for metadata.
        :param network: Blockchain network (``"solana"`` or ``"base"``).
            Defaults to :attr:`default_network`.
        :param description: Optional human-readable description.
        :returns: Transfer outcome with ``PROCESSING`` status on success.
        :raises CirclePaymentError: If the transfer cannot be initiated.
        """
        if amount <= 0:
            return PaymentResult(
                success=False,
                payment_id="",
                status=CirclePaymentStatus.FAILED,
                amount=amount,
                error="Amount must be positive.",
            )

        resolved_network = self._resolve_network(network)
        chain = self._resolve_chain(resolved_network)

        # Validate destination address format.
        addr_error = validate_blockchain_address(destination_address, resolved_network)
        if addr_error:
            return PaymentResult(
                success=False,
                payment_id="",
                status=CirclePaymentStatus.FAILED,
                amount=amount,
                network=resolved_network,
                error=addr_error,
            )

        payload: Dict[str, Any] = {
            "source": {
                "type": "wallet",
                "id": "master",
            },
            "destination": {
                "type": "blockchain",
                "chain": chain,
                "address": destination_address,
            },
            "amount": {
                "amount": f"{amount:.2f}",
                "currency": "USD",
            },
        }

        if description:
            payload["description"] = description
        if job_id:
            payload["metadata"] = {"job_id": job_id}

        logger.info(
            "Creating Circle transfer: %.2f USDC on %s for job %s",
            amount,
            chain,
            job_id,
        )

        data = self._request("POST", "/v1/transfers", json=payload)

        transfer = data.get("data", data)
        transfer_id = str(transfer.get("id", ""))

        if not transfer_id:
            raise CirclePaymentError(
                "Circle API did not return a transfer ID.",
                code="MISSING_ID",
            )

        initial_status_str = transfer.get("status", "pending")
        mapped_status = _STATUS_MAP.get(
            initial_status_str, CirclePaymentStatus.PROCESSING,
        )

        logger.info(
            "Circle transfer %s created with initial status: %s",
            transfer_id,
            initial_status_str,
        )

        return PaymentResult(
            success=mapped_status == CirclePaymentStatus.COMPLETED,
            payment_id=transfer_id,
            status=mapped_status,
            amount=amount,
            network=resolved_network,
        )

    def check_payment_status(self, payment_id: str) -> PaymentResult:
        """Check status of an existing Circle transfer.

        :param payment_id: The Circle transfer ID.
        :returns: Current transfer state.
        :raises CirclePaymentError: If the transfer cannot be queried.
        """
        if not payment_id:
            raise CirclePaymentError(
                "payment_id is required.",
                code="MISSING_PAYMENT_ID",
            )

        data = self._request("GET", f"/v1/transfers/{payment_id}")

        transfer = data.get("data", data)
        status_str = transfer.get("status", "pending")
        mapped_status = _STATUS_MAP.get(status_str, CirclePaymentStatus.PENDING)

        amount_info = transfer.get("amount", {})
        amount_val = float(amount_info.get("amount", 0))

        tx_hash = transfer.get("transactionHash")

        return PaymentResult(
            success=mapped_status == CirclePaymentStatus.COMPLETED,
            payment_id=payment_id,
            status=mapped_status,
            amount=amount_val,
            network=self._default_network,
            tx_hash=tx_hash,
        )

    def refund_payment(self, payment_id: str) -> RefundResult:
        """Refund a completed Circle transfer.

        Retrieves the original transfer to determine amount and
        destination, then creates a reverse transfer via the Circle API.

        :param payment_id: The original transfer ID to refund.
        :returns: Refund outcome.
        :raises CirclePaymentError: If the refund cannot be processed.
        """
        if not payment_id:
            raise CirclePaymentError(
                "payment_id is required.",
                code="MISSING_PAYMENT_ID",
            )

        # Retrieve the original transfer.
        original_data = self._request("GET", f"/v1/transfers/{payment_id}")
        original = original_data.get("data", original_data)

        amount_info = original.get("amount", {})
        source = original.get("source", {})
        destination = original.get("destination", {})

        # Source is always the master wallet for refunds.
        refund_source: Dict[str, Any] = {"type": "wallet", "id": "master"}

        # Determine refund destination based on original transfer direction.
        source_type = source.get("type", "")
        dest_type = destination.get("type", "")

        if source_type == "blockchain":
            # Inbound blockchain transfer -> refund to original sender.
            refund_destination: Dict[str, Any] = {
                "type": "blockchain",
                "chain": source.get("chain", self._resolve_chain()),
                "address": source.get("address", ""),
            }
        elif source_type == "wallet" and dest_type == "blockchain":
            # Outbound payout -> refund back to same blockchain address.
            logger.warning(
                "Refunding outbound payout %s: sending back to same "
                "destination address %s",
                payment_id,
                destination.get("address", "unknown"),
            )
            refund_destination = {
                "type": "blockchain",
                "chain": destination.get("chain", self._resolve_chain()),
                "address": destination.get("address", ""),
            }
        else:
            # Wallet-to-wallet -> reverse to original source wallet.
            refund_destination = {
                "type": source.get("type", "wallet"),
                "id": source.get("id", "master"),
            }

        refund_payload: Dict[str, Any] = {
            "source": refund_source,
            "destination": refund_destination,
            "amount": amount_info,
        }

        logger.info("Creating refund for Circle transfer %s", payment_id)

        data = self._request("POST", "/v1/transfers", json=refund_payload)

        refund = data.get("data", data)
        refund_id = str(refund.get("id", ""))
        if not refund_id:
            raise CirclePaymentError(
                "Circle refund response missing transfer ID.",
                code="MISSING_REFUND_ID",
            )

        refund_amount = float(amount_info.get("amount", 0))
        if refund_amount <= 0:
            raise CirclePaymentError(
                f"Circle refund returned zero amount for payment {payment_id}. "
                "Refund may not have been processed.",
                code="ZERO_REFUND_AMOUNT",
            )

        refund_status_str = refund.get("status", "pending")
        refund_status = _STATUS_MAP.get(
            refund_status_str, CirclePaymentStatus.PROCESSING,
        )
        if refund_status_str not in _STATUS_MAP:
            logger.warning(
                "Unknown Circle refund status %r for payment %s "
                "-- defaulting to PROCESSING",
                refund_status_str,
                payment_id,
            )

        return RefundResult(
            success=refund_status != CirclePaymentStatus.FAILED,
            refund_id=refund_id,
            original_payment_id=payment_id,
            status=(
                CirclePaymentStatus.REFUNDED
                if refund_status != CirclePaymentStatus.FAILED
                else CirclePaymentStatus.FAILED
            ),
            amount=refund_amount,
            network=self._default_network,
            tx_hash=refund.get("transactionHash"),
        )

    def list_payments(
        self,
        *,
        limit: int = 25,
        page_before: Optional[str] = None,
        page_after: Optional[str] = None,
    ) -> List[PaymentSummary]:
        """List recent USDC transfers from the Circle API.

        :param limit: Maximum number of transfers to return (1-100).
        :param page_before: Pagination cursor for previous page.
        :param page_after: Pagination cursor for next page.
        :returns: List of payment summaries.
        :raises CirclePaymentError: If the API call fails.
        """
        params: Dict[str, Any] = {"pageSize": min(max(limit, 1), 100)}
        if page_before:
            params["pageBefore"] = page_before
        if page_after:
            params["pageAfter"] = page_after

        data = self._request("GET", "/v1/transfers", params=params)

        transfers = data.get("data", [])
        if not isinstance(transfers, list):
            transfers = []

        results: List[PaymentSummary] = []
        for t in transfers:
            amount_info = t.get("amount", {})
            status_str = t.get("status", "pending")

            # Attempt to resolve network from the destination chain.
            dest = t.get("destination", {})
            chain = dest.get("chain", "")
            network = self._chain_to_network(chain)

            results.append(PaymentSummary(
                payment_id=str(t.get("id", "")),
                status=_STATUS_MAP.get(status_str, CirclePaymentStatus.PENDING),
                amount=float(amount_info.get("amount", 0)),
                network=network,
                created_at=t.get("createDate"),
            ))

        return results

    def _chain_to_network(self, chain: str) -> Optional[BlockchainNetwork]:
        """Reverse-map a Circle chain identifier to a :class:`BlockchainNetwork`."""
        for net, chain_id in _NETWORK_TO_CHAIN.items():
            if chain_id == chain:
                return net
        return None

    def __repr__(self) -> str:
        return (
            f"<CircleProvider environment={self._environment!r} "
            f"default_network={self._default_network.value!r}>"
        )
