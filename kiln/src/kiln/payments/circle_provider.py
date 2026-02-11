"""Circle USDC payment provider.

Implements :class:`~kiln.payments.base.PaymentProvider` using the
`Circle API <https://developers.circle.com>`_ for USDC stablecoin
payments on Solana and Base networks.

Circle is a regulated financial infrastructure company that issues USDC,
the second-largest stablecoin by market cap.  This adapter uses Circle's
transfer API to send USDC payouts on-chain.

Environment variables
---------------------
``KILN_CIRCLE_API_KEY``
    API key for authenticating with the Circle API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.circle.com/v1"

_STATUS_MAP: Dict[str, PaymentStatus] = {
    "complete": PaymentStatus.COMPLETED,
    "pending": PaymentStatus.PROCESSING,
    "failed": PaymentStatus.FAILED,
}

_RAIL_TO_CHAIN: Dict[PaymentRail, str] = {
    PaymentRail.SOLANA: "SOL",
    PaymentRail.BASE: "ETH-BASE",
}


class CircleProvider(PaymentProvider):
    """Concrete :class:`PaymentProvider` backed by the Circle Transfers API.

    Routes USDC payments to Solana or Base depending on the requested
    :class:`PaymentRail`.

    Args:
        api_key: Circle API key.  If not provided, reads from
            ``KILN_CIRCLE_API_KEY``.
        default_network: Default blockchain network when the payment
            request does not specify a rail (``"solana"`` or ``"base"``).
        base_url: Base URL for the Circle API.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_network: str = "solana",
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CIRCLE_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._default_network = default_network

        if not self._api_key:
            raise ValueError(
                "Circle API key required. "
                "Set KILN_CIRCLE_API_KEY or pass api_key."
            )

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # -- PaymentProvider identity ---------------------------------------------

    @property
    def name(self) -> str:
        return "circle"

    @property
    def supported_currencies(self) -> list[Currency]:
        return [Currency.USDC]

    @property
    def rail(self) -> PaymentRail:
        return PaymentRail.SOLANA

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

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g. ``/v1/transfers``).
            **kwargs: Extra keyword arguments forwarded to
                :meth:`requests.Session.request`.

        Returns:
            Parsed JSON response body.

        Raises:
            PaymentError: On timeout, connection failure, or HTTP error.
        """
        url = self._url(path)
        try:
            response = self._session.request(method, url, timeout=30, **kwargs)

            if not response.ok:
                raise PaymentError(
                    f"Circle API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )

            try:
                return response.json()
            except ValueError:
                return {"status": "ok"}

        except Timeout as exc:
            raise PaymentError(
                "Circle API timeout",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise PaymentError(
                "Cannot reach Circle API",
                code="CONNECTION_ERROR",
            ) from exc
        except PaymentError:
            raise
        except RequestException as exc:
            raise PaymentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    def _resolve_chain(self, rail: PaymentRail) -> str:
        """Map a :class:`PaymentRail` to a Circle chain identifier.

        Falls back to *default_network* when the rail is not explicitly
        mapped (e.g. ``PaymentRail.CIRCLE``).
        """
        if rail in _RAIL_TO_CHAIN:
            return _RAIL_TO_CHAIN[rail]
        # Fall back to default network
        default_rail = (
            PaymentRail.SOLANA
            if self._default_network == "solana"
            else PaymentRail.BASE
        )
        return _RAIL_TO_CHAIN.get(default_rail, "SOL")

    # -- PaymentProvider methods ----------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Create a USDC transfer via the Circle API.

        Sends a ``POST /v1/transfers`` request, then polls for finality
        with exponential backoff (up to ~60 s).  If the transfer has not
        settled after the polling window, a result with status
        :attr:`PaymentStatus.PROCESSING` is returned.

        Args:
            request: Payment parameters including amount, rail, and job ID.

        Returns:
            Transfer outcome with on-chain transaction hash when available.

        Raises:
            PaymentError: If the transfer cannot be initiated.
        """
        chain = self._resolve_chain(request.rail)

        payload = {
            "source": {
                "type": "wallet",
                "id": "master",
            },
            "destination": {
                "type": "blockchain",
                "chain": chain,
                "address": request.metadata.get("destination_address", ""),
            },
            "amount": {
                "amount": f"{request.amount:.2f}",
                "currency": "USD",
            },
        }

        if request.description:
            payload["description"] = request.description
        if request.job_id:
            payload["metadata"] = {"job_id": request.job_id}

        logger.info(
            "Creating Circle transfer: %.2f USDC on %s for job %s",
            request.amount,
            chain,
            request.job_id,
        )

        data = self._request("POST", "/v1/transfers", json=payload)

        transfer = data.get("data", data)
        transfer_id = str(transfer.get("id", ""))

        if not transfer_id:
            raise PaymentError(
                "Circle API did not return a transfer ID.",
                code="MISSING_ID",
            )

        # Poll for finality (up to ~60 seconds)
        for attempt in range(10):
            time.sleep(min(2 ** attempt, 15))
            result = self.get_payment_status(transfer_id)
            if result.status in (PaymentStatus.COMPLETED, PaymentStatus.FAILED):
                return result

        # Still pending after timeout — return current state
        logger.warning(
            "Circle transfer %s still processing after polling timeout",
            transfer_id,
        )
        return PaymentResult(
            success=False,
            payment_id=transfer_id,
            status=PaymentStatus.PROCESSING,
            amount=request.amount,
            currency=request.currency,
            rail=request.rail,
        )

    def get_payment_status(self, payment_id: str) -> PaymentResult:
        """Check status of an existing Circle transfer.

        Calls ``GET /v1/transfers/{payment_id}`` and maps the Circle
        status string to :class:`PaymentStatus`.

        Args:
            payment_id: The Circle transfer ID.

        Returns:
            Current transfer state.

        Raises:
            PaymentError: If the transfer cannot be queried.
        """
        data = self._request("GET", f"/v1/transfers/{payment_id}")

        transfer = data.get("data", data)
        status_str = transfer.get("status", "pending")
        mapped_status = _STATUS_MAP.get(status_str, PaymentStatus.PENDING)

        amount_info = transfer.get("amount", {})
        amount_val = float(amount_info.get("amount", 0))

        tx_hash = transfer.get("transactionHash")

        return PaymentResult(
            success=mapped_status == PaymentStatus.COMPLETED,
            payment_id=payment_id,
            status=mapped_status,
            amount=amount_val,
            currency=Currency.USDC,
            rail=self.rail,
            tx_hash=tx_hash,
        )

    def refund_payment(self, payment_id: str) -> PaymentResult:
        """Refund a completed Circle transfer.

        Creates a return transfer via the Circle API.  Retrieves the
        original transfer first to determine amount and destination, then
        posts a new reverse transfer.

        Args:
            payment_id: The original transfer ID to refund.

        Returns:
            Refund outcome.

        Raises:
            PaymentError: If the refund cannot be processed.
        """
        # Retrieve the original transfer
        original_data = self._request("GET", f"/v1/transfers/{payment_id}")
        original = original_data.get("data", original_data)

        amount_info = original.get("amount", {})
        source = original.get("source", {})
        destination = original.get("destination", {})

        # Create a reverse transfer (swap source and destination)
        refund_payload = {
            "source": {
                "type": destination.get("type", "blockchain"),
                "id": destination.get("id", "master"),
            },
            "destination": {
                "type": source.get("type", "wallet"),
                "id": source.get("id", "master"),
            },
            "amount": amount_info,
        }

        logger.info("Creating refund for Circle transfer %s", payment_id)

        data = self._request("POST", "/v1/transfers", json=refund_payload)

        refund = data.get("data", data)
        refund_id = str(refund.get("id", ""))
        refund_status_str = refund.get("status", "pending")
        refund_status = _STATUS_MAP.get(refund_status_str, PaymentStatus.PROCESSING)

        return PaymentResult(
            success=refund_status != PaymentStatus.FAILED,
            payment_id=refund_id or payment_id,
            status=PaymentStatus.REFUNDED if refund_status != PaymentStatus.FAILED else PaymentStatus.FAILED,
            amount=float(amount_info.get("amount", 0)),
            currency=Currency.USDC,
            rail=self.rail,
            tx_hash=refund.get("transactionHash"),
        )

    def __repr__(self) -> str:
        return (
            f"<CircleProvider base_url={self._base_url!r} "
            f"default_network={self._default_network!r}>"
        )
