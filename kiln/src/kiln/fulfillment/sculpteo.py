"""Sculpteo fulfillment adapter.

Implements :class:`~kiln.fulfillment.base.FulfillmentProvider` using the
`Sculpteo API <https://www.sculpteo.com/en/services/api-services/>`_.
Sculpteo is a professional 3D printing service supporting 75+ materials
across FDM, SLA, SLS, and metal technologies.

The adapter uploads model files via the Web2web API, retrieves pricing by
UUID, and places orders via the store API.

Environment variables
---------------------
``KILN_SCULPTEO_API_KEY``
    API key for authenticating with Sculpteo (partner account).
``KILN_SCULPTEO_BASE_URL``
    Base URL of the Sculpteo API (defaults to production).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote as url_quote

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

_DEFAULT_BASE_URL = "https://www.sculpteo.com/en/api"

_STATUS_MAP: dict[str, OrderStatus] = {
    "pending": OrderStatus.SUBMITTED,
    "submitted": OrderStatus.SUBMITTED,
    "confirmed": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "production": OrderStatus.PRINTING,
    "printing": OrderStatus.PRINTING,
    "in_production": OrderStatus.PRINTING,
    "shipped": OrderStatus.SHIPPING,
    "delivered": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "failed": OrderStatus.FAILED,
}


class SculpteoProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Sculpteo API.

    Uses a partner API key for authentication.

    Args:
        api_key: Sculpteo partner API key.  Falls back to
            ``KILN_SCULPTEO_API_KEY``.
        base_url: Base URL of the Sculpteo API.
        timeout: Per-request timeout in seconds.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 60,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_SCULPTEO_API_KEY", "")
        self._base_url = (base_url or os.environ.get("KILN_SCULPTEO_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")
        self._timeout = timeout

        if not self._api_key:
            raise ValueError("Sculpteo API key required. Set KILN_SCULPTEO_API_KEY or pass api_key.")

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            }
        )

    # -- FulfillmentProvider identity ----------------------------------------

    @property
    def name(self) -> str:
        return "sculpteo"

    @property
    def display_name(self) -> str:
        return "Sculpteo"

    @property
    def supported_technologies(self) -> list[str]:
        return ["FDM", "SLA", "SLS", "MJF", "DMLS", "CNC"]

    # -- Internal HTTP helpers -----------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: Any | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request to the Sculpteo API."""
        url = self._url(path)
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
            if not response.ok:
                raise FulfillmentError(
                    f"Sculpteo API returned HTTP {response.status_code} for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )
            try:
                return response.json()
            except ValueError:
                return {"status": "ok"}

        except FulfillmentError:
            raise
        except Timeout as exc:
            raise FulfillmentError(
                f"Request to Sculpteo timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to Sculpteo API at {self._base_url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> list[Material]:
        """Return available materials from Sculpteo.

        Calls ``GET /materials/3D/`` for the material catalog.
        """
        try:
            data = self._request("GET", "/materials/3D/")
        except FulfillmentError:
            raise
        except Exception as exc:
            raise FulfillmentError(f"Failed to list materials: {exc}") from exc

        materials_raw = data.get("materials", data.get("data", []))
        if not isinstance(materials_raw, list):
            return []

        results: list[Material] = []
        for m in materials_raw:
            if not isinstance(m, dict):
                continue
            results.append(
                Material(
                    id=str(m.get("id", m.get("uuid", ""))),
                    name=m.get("name", m.get("title", "")),
                    technology=m.get("technology", ""),
                    color=m.get("color", ""),
                    finish=m.get("finish", m.get("finishing", "")),
                    min_wall_mm=m.get("min_wall_thickness", m.get("minimumWallThickness")),
                    price_per_cm3=m.get("price_per_cm3"),
                    currency=m.get("currency", "EUR"),
                )
            )
        return results

    def get_quote(self, request: QuoteRequest) -> Quote:
        """Upload a model file and get a manufacturing quote from Sculpteo.

        Steps:
        1. Upload the file via ``POST /design/3D/upload/``
        2. Get pricing via ``GET /design/3D/price_by_uuid/``
        """
        abs_path = os.path.abspath(request.file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Model file not found: {abs_path}")

        # Step 1: Upload file
        filename = os.path.basename(abs_path)
        try:
            with open(abs_path, "rb") as fh:
                upload_data = self._request(
                    "POST",
                    "/design/3D/upload/",
                    files={"file": (filename, fh, "application/octet-stream")},
                )
        except PermissionError as exc:
            raise FulfillmentError(
                f"Permission denied reading file: {abs_path}",
                code="PERMISSION_ERROR",
            ) from exc

        design_uuid = upload_data.get("uuid") or upload_data.get("design_uuid") or upload_data.get("id", "")
        if not design_uuid:
            raise FulfillmentError(
                "Sculpteo did not return a design UUID.",
                code="UPLOAD_ERROR",
            )

        # Step 2: Get price
        price_data = self._request(
            "GET",
            "/design/3D/price_by_uuid/",
            params={
                "uuid": design_uuid,
                "material": request.material_id,
                "quantity": request.quantity,
            },
        )

        unit_price = float(price_data.get("unit_price", price_data.get("price", 0)))
        total_price = float(price_data.get("total_price", unit_price * request.quantity))

        if unit_price <= 0 and total_price <= 0:
            logger.warning(
                "Sculpteo returned $0 pricing — API field names may have changed. Response keys: %s",
                list(price_data.keys()),
            )
            raise FulfillmentError(
                "Sculpteo returned zero pricing. This likely means the API "
                "response format has changed. Contact support or check API docs. "
                f"Response keys: {list(price_data.keys())}",
                code="ZERO_PRICE",
            )

        # Parse shipping options
        shipping: list[ShippingOption] = []
        shipping_raw = price_data.get("shipping_options", [])
        for s in shipping_raw:
            if not isinstance(s, dict):
                continue
            ship_price = float(s.get("price", -1))
            if ship_price < 0:
                logger.warning(
                    "Sculpteo shipping option %r missing price field — skipping",
                    s.get("name", "unknown"),
                )
                continue
            shipping.append(
                ShippingOption(
                    id=str(s.get("id", "")),
                    name=s.get("name", ""),
                    price=ship_price,
                    currency=s.get("currency", "EUR"),
                    estimated_days=s.get("estimated_days"),
                )
            )

        quote_id = f"sc-{design_uuid}-{request.material_id}"

        return Quote(
            quote_id=quote_id,
            provider=self.name,
            material=price_data.get("material_name", request.material_id),
            quantity=request.quantity,
            unit_price=unit_price,
            total_price=total_price,
            currency=price_data.get("currency", "EUR"),
            lead_time_days=price_data.get("lead_time_days"),
            shipping_options=shipping,
            expires_at=price_data.get("expires_at"),
            raw=price_data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Sculpteo.

        Calls ``POST /store/3D/order/`` with the design UUID and material
        from the quote ID (format: ``sc-<uuid>-<material_id>``).
        """
        parts = request.quote_id.split("-", 2)
        if len(parts) < 3 or parts[0] != "sc":
            raise FulfillmentError(
                f"Invalid Sculpteo quote ID format: {request.quote_id}",
                code="INVALID_QUOTE",
            )
        design_uuid = parts[1]
        material_id = parts[2]

        payload: dict[str, Any] = {
            "uuid": design_uuid,
            "material": material_id,
            "quantity": 1,
        }
        if request.shipping_option_id:
            payload["shipping_option"] = request.shipping_option_id
        if request.shipping_address:
            payload["shipping_address"] = request.shipping_address
        if request.notes:
            payload["notes"] = request.notes

        data = self._request("POST", "/store/3D/order/", json=payload)

        status_str = str(data.get("status", "submitted")).lower()
        mapped_status = _STATUS_MAP.get(status_str)
        if mapped_status is None:
            logger.warning(
                "Unknown Sculpteo order status %r — defaulting to SUBMITTED. The API may have added new statuses.",
                status_str,
            )
            mapped_status = OrderStatus.SUBMITTED

        order_id = str(data.get("order_id") or data.get("order_ref") or data.get("id", ""))
        if not order_id:
            raise FulfillmentError(
                f"Sculpteo order response missing order ID. Response keys: {list(data.keys())}",
                code="MISSING_ORDER_ID",
            )

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("tracking_url"),
            tracking_number=data.get("tracking_number"),
            estimated_delivery=data.get("estimated_delivery"),
            total_price=data.get("total_price"),
            currency=data.get("currency", "EUR"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the status of an existing Sculpteo order.

        Calls ``GET /store/3D/order/<order_id>/``.
        """
        safe_id = url_quote(order_id, safe="")
        data = self._request("GET", f"/store/3D/order/{safe_id}/")

        status_str = str(data.get("status", "submitted")).lower()
        mapped_status = _STATUS_MAP.get(status_str)
        if mapped_status is None:
            logger.warning(
                "Unknown Sculpteo order status %r for order %s — defaulting to SUBMITTED",
                status_str,
                order_id,
            )
            mapped_status = OrderStatus.SUBMITTED

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("tracking_url"),
            tracking_number=data.get("tracking_number"),
            estimated_delivery=data.get("estimated_delivery"),
            total_price=data.get("total_price"),
            currency=data.get("currency", "EUR"),
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a Sculpteo order.

        Calls ``POST /store/3D/order/<order_id>/cancel/``.
        """
        safe_id = url_quote(order_id, safe="")
        data = self._request("POST", f"/store/3D/order/{safe_id}/cancel/")

        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            provider=self.name,
            total_price=data.get("total_price"),
            currency=data.get("currency", "EUR"),
        )

    def __repr__(self) -> str:
        return f"<SculpteoProvider base_url={self._base_url!r}>"
