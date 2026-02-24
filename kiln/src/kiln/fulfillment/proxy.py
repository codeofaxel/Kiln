"""Kiln Cloud proxy fulfillment adapter.

Implements :class:`~kiln.fulfillment.base.FulfillmentProvider` by forwarding
all calls to the hosted Kiln proxy at ``api.kiln3d.com``.  The proxy acts as
a unified gateway to multiple fulfillment providers (Craftcloud, etc.) with
centralized billing and license key authentication.

The adapter routes requests to the proxy's REST API:

1. ``GET /api/fulfillment/materials?provider={provider}`` — list materials
2. ``POST /api/fulfillment/quote`` — upload model + get quote
3. ``POST /api/fulfillment/order`` — place order from quote
4. ``GET /api/fulfillment/order/{orderId}/status`` — check order status
5. ``POST /api/fulfillment/order/{orderId}/cancel`` — cancel order

Authentication
--------------
Requires a valid Kiln license key.  The key is read from:

1. ``KILN_LICENSE_KEY`` environment variable
2. ``~/.kiln/license`` file

Run ``kiln register`` to obtain a license key.

Environment variables
---------------------
``KILN_LICENSE_KEY``
    License key for authentication.  Overrides the file-based key.
``KILN_PROXY_URL``
    Base URL of the Kiln proxy (defaults to ``https://api.kiln3d.com``).
"""

from __future__ import annotations

import logging
import os
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.fulfillment.base import (
    FulfillmentError,
    FulfillmentProvider,
    Material,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    QuoteRequest,
    ShippingOption,
)

logger = logging.getLogger(__name__)

_DEFAULT_PROXY_URL = "https://api.kiln3d.com"


def _read_license_key() -> str:
    """Read license key from environment or file.

    Returns:
        License key string, or empty string if not found.
    """
    key = os.environ.get("KILN_LICENSE_KEY", "").strip()
    if key:
        return key
    license_path = Path.home() / ".kiln" / "license"
    if license_path.exists():
        return license_path.read_text().strip()
    return ""


def _client_version() -> str:
    """Best-effort package version for request metadata."""
    try:
        return metadata.version("kiln3d")
    except Exception:
        return ""


def _device_fingerprint() -> str:
    """Return a stable local device fingerprint (random UUID persisted on disk)."""
    explicit = os.environ.get("KILN_DEVICE_FINGERPRINT", "").strip()
    if explicit:
        return explicit

    path = Path.home() / ".kiln" / "device_fingerprint"
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = f"kiln-device-{uuid.uuid4().hex}"
        path.write_text(value, encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return value
    except Exception:
        # Last-resort ephemeral fallback.
        return f"kiln-device-{uuid.uuid4().hex}"


class ProxyProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Kiln Cloud proxy.

    Args:
        proxy_url: Base URL of the Kiln proxy.
        license_key: License key for authentication.  If not provided,
            reads from ``KILN_LICENSE_KEY`` env var or ``~/.kiln/license``.
        provider: Which backend provider to request from the proxy
            (e.g. ``"craftcloud"``).  Defaults to ``"craftcloud"``.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        proxy_url: str | None = None,
        license_key: str | None = None,
        *,
        provider: str = "craftcloud",
        timeout: int = 60,
    ) -> None:
        self._proxy_url = (proxy_url or os.environ.get("KILN_PROXY_URL", _DEFAULT_PROXY_URL)).rstrip("/")
        self._license_key = license_key or _read_license_key()
        self._provider = provider
        self._timeout = timeout

        if not self._license_key:
            raise FulfillmentError(
                "License key required. Set KILN_LICENSE_KEY or run 'kiln register'.",
                code="MISSING_LICENSE_KEY",
            )

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._license_key}",
                "X-Kiln-Device-Fingerprint": _device_fingerprint(),
                "X-Kiln-Client-Version": _client_version(),
            }
        )

    # -- FulfillmentProvider identity ----------------------------------------

    @property
    def name(self) -> str:
        return "proxy"

    @property
    def display_name(self) -> str:
        return "Kiln Cloud"

    @property
    def supported_technologies(self) -> list[str]:
        return ["FDM", "SLA", "SLS", "MJF", "DMLS"]

    # -- HTTP layer ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: Any | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an HTTP request to the Kiln proxy API.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g. ``/api/fulfillment/materials``).
            json: JSON payload for POST/PUT.
            params: Query parameters.
            data: Form data.
            files: Multipart file upload.

        Returns:
            Parsed JSON response.

        Raises:
            FulfillmentError: If the request fails.
        """
        url = f"{self._proxy_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                params=params,
                data=data,
                files=files,
                timeout=self._timeout,
            )

            # Handle special status codes.
            if response.status_code == 401:
                raise FulfillmentError(
                    "License key invalid or expired. Run 'kiln register' to get a new key.",
                    code="AUTH_ERROR",
                )
            if response.status_code == 402:
                raise FulfillmentError(
                    "Payment required. Update your payment method with 'kiln billing setup'.",
                    code="PAYMENT_REQUIRED",
                )
            if response.status_code == 429:
                raise FulfillmentError(
                    "Rate limit exceeded. Please try again shortly.",
                    code="RATE_LIMITED",
                )

            if response.ok:
                try:
                    return response.json()
                except ValueError:
                    return {"status": "ok"}

            raise FulfillmentError(
                f"Kiln proxy returned HTTP {response.status_code} for {method} {path}: {response.text[:500]}",
                code=f"HTTP_{response.status_code}",
            )

        except FulfillmentError:
            raise
        except Timeout as exc:
            raise FulfillmentError(
                f"Request to Kiln proxy timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to Kiln proxy at {self._proxy_url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> list[Material]:
        """Return available materials from the proxy.

        Returns:
            List of materials with pricing and specifications.

        Raises:
            FulfillmentError: If the materials cannot be retrieved.
        """
        data = self._request(
            "GET",
            "/api/fulfillment/materials",
            params={"provider": self._provider},
        )

        if not isinstance(data, dict):
            raise FulfillmentError(
                "Proxy returned invalid response for materials.",
                code="INVALID_RESPONSE",
            )

        materials_raw = data.get("materials", [])
        if not isinstance(materials_raw, list):
            raise FulfillmentError(
                "Proxy materials response missing 'materials' list.",
                code="INVALID_RESPONSE",
            )

        materials: list[Material] = []
        for m in materials_raw:
            if not isinstance(m, dict):
                continue
            materials.append(
                Material(
                    id=m.get("id", ""),
                    name=m.get("name", ""),
                    technology=m.get("technology", ""),
                    color=m.get("color", ""),
                    finish=m.get("finish", ""),
                    min_wall_mm=m.get("min_wall_mm"),
                    price_per_cm3=m.get("price_per_cm3"),
                    currency=m.get("currency", "USD"),
                )
            )

        return materials

    def get_quote(self, request: QuoteRequest) -> Quote:
        """Request a price quote for manufacturing a part.

        Args:
            request: Quote parameters including file, material, and quantity.

        Returns:
            A quote with pricing, lead time, and shipping options.

        Raises:
            FulfillmentError: If the quote cannot be generated.
            FileNotFoundError: If the model file does not exist.
        """
        abs_path = os.path.abspath(request.file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Model file not found: {abs_path}")

        filename = os.path.basename(abs_path)

        # Upload file as multipart + JSON params.
        try:
            with open(abs_path, "rb") as f:
                data = self._request(
                    "POST",
                    "/api/fulfillment/quote",
                    files={"file": (filename, f, "application/octet-stream")},
                    data={
                        "material_id": request.material_id,
                        "quantity": str(request.quantity),
                        "shipping_country": request.shipping_country,
                        "provider": self._provider,
                    },
                )
        except PermissionError as exc:
            raise FulfillmentError(
                f"Permission denied reading file: {abs_path}",
                code="PERMISSION_ERROR",
            ) from exc

        if not isinstance(data, dict):
            raise FulfillmentError(
                "Proxy returned invalid response for quote.",
                code="INVALID_RESPONSE",
            )

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            raise FulfillmentError(
                f"Quote failed: {error}",
                code="QUOTE_ERROR",
            )

        quote_raw = data.get("quote", {})
        if not isinstance(quote_raw, dict):
            raise FulfillmentError(
                "Proxy quote response missing 'quote' object.",
                code="INVALID_RESPONSE",
            )

        # Parse shipping options.
        shipping_options: list[ShippingOption] = []
        shipping_raw = quote_raw.get("shipping_options", [])
        if isinstance(shipping_raw, list):
            for s in shipping_raw:
                if not isinstance(s, dict):
                    continue
                shipping_options.append(
                    ShippingOption(
                        id=s.get("id", ""),
                        name=s.get("name", ""),
                        price=s.get("price", 0.0),
                        currency=s.get("currency", "USD"),
                        estimated_days=s.get("estimated_days"),
                    )
                )

        return Quote(
            quote_id=quote_raw.get("quote_id", ""),
            provider=quote_raw.get("provider", self._provider),
            material=quote_raw.get("material", ""),
            quantity=quote_raw.get("quantity", request.quantity),
            unit_price=quote_raw.get("unit_price", 0.0),
            total_price=quote_raw.get("total_price", 0.0),
            currency=quote_raw.get("currency", "USD"),
            lead_time_days=quote_raw.get("lead_time_days"),
            shipping_options=shipping_options,
            expires_at=quote_raw.get("expires_at"),
            raw=data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a manufacturing order based on a previous quote.

        Args:
            request: Order parameters referencing a quote ID.

        Returns:
            Order result with ID, status, and tracking info.

        Raises:
            FulfillmentError: If the order cannot be placed.
        """
        if not request.quote_id:
            raise FulfillmentError(
                "quote_id is required to place an order.",
                code="MISSING_QUOTE_ID",
            )

        payload = {
            "quote_id": request.quote_id,
            "shipping_option_id": request.shipping_option_id,
            "shipping_address": request.shipping_address,
            "notes": request.notes,
        }

        data = self._request("POST", "/api/fulfillment/order", json=payload)

        if not isinstance(data, dict):
            raise FulfillmentError(
                "Proxy returned invalid response for order.",
                code="INVALID_RESPONSE",
            )

        order_raw = data.get("order", {})
        if not isinstance(order_raw, dict):
            raise FulfillmentError(
                "Proxy order response missing 'order' object.",
                code="INVALID_RESPONSE",
            )

        # Map status string to enum.
        status_str = order_raw.get("status", "submitted")
        try:
            status = OrderStatus(status_str)
        except ValueError:
            status = OrderStatus.SUBMITTED

        return OrderResult(
            success=order_raw.get("success", True),
            order_id=order_raw.get("order_id", ""),
            status=status,
            provider=order_raw.get("provider", self._provider),
            tracking_url=order_raw.get("tracking_url"),
            tracking_number=order_raw.get("tracking_number"),
            estimated_delivery=order_raw.get("estimated_delivery"),
            total_price=order_raw.get("total_price"),
            currency=order_raw.get("currency", "USD"),
            error=order_raw.get("error"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the current status of an existing order.

        Args:
            order_id: The order ID returned by :meth:`place_order`.

        Returns:
            Current order state.

        Raises:
            FulfillmentError: If the order cannot be found or queried.
        """
        data = self._request("GET", f"/api/fulfillment/order/{order_id}/status")

        if not isinstance(data, dict):
            raise FulfillmentError(
                "Proxy returned invalid response for order status.",
                code="INVALID_RESPONSE",
            )

        order_raw = data.get("order", {})
        if not isinstance(order_raw, dict):
            raise FulfillmentError(
                "Proxy order status response missing 'order' object.",
                code="INVALID_RESPONSE",
            )

        # Map status string to enum.
        status_str = order_raw.get("status", "submitted")
        try:
            status = OrderStatus(status_str)
        except ValueError:
            status = OrderStatus.SUBMITTED

        return OrderResult(
            success=order_raw.get("success", True),
            order_id=order_raw.get("order_id", order_id),
            status=status,
            provider=order_raw.get("provider", self._provider),
            tracking_url=order_raw.get("tracking_url"),
            tracking_number=order_raw.get("tracking_number"),
            estimated_delivery=order_raw.get("estimated_delivery"),
            total_price=order_raw.get("total_price"),
            currency=order_raw.get("currency", "USD"),
            error=order_raw.get("error"),
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order (if still cancellable).

        Args:
            order_id: The order ID to cancel.

        Returns:
            Updated order state.

        Raises:
            FulfillmentError: If the order cannot be cancelled.
        """
        data = self._request("POST", f"/api/fulfillment/order/{order_id}/cancel")

        if not isinstance(data, dict):
            raise FulfillmentError(
                "Proxy returned invalid response for cancel order.",
                code="INVALID_RESPONSE",
            )

        order_raw = data.get("order", {})
        if not isinstance(order_raw, dict):
            raise FulfillmentError(
                "Proxy cancel order response missing 'order' object.",
                code="INVALID_RESPONSE",
            )

        # Map status string to enum.
        status_str = order_raw.get("status", "cancelled")
        try:
            status = OrderStatus(status_str)
        except ValueError:
            status = OrderStatus.CANCELLED

        return OrderResult(
            success=order_raw.get("success", True),
            order_id=order_raw.get("order_id", order_id),
            status=status,
            provider=order_raw.get("provider", self._provider),
            tracking_url=order_raw.get("tracking_url"),
            tracking_number=order_raw.get("tracking_number"),
            estimated_delivery=order_raw.get("estimated_delivery"),
            total_price=order_raw.get("total_price"),
            currency=order_raw.get("currency", "USD"),
            error=order_raw.get("error"),
        )

    def __repr__(self) -> str:
        return f"<ProxyProvider proxy_url={self._proxy_url!r} provider={self._provider!r}>"
