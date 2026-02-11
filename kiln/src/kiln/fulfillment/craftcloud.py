"""Craftcloud by All3DP fulfillment adapter.

Implements :class:`~kiln.fulfillment.base.FulfillmentProvider` using the
`Craftcloud API <https://craftcloud3d.com>`_.  Craftcloud is a 3D printing
price comparison service that aggregates quotes from 150+ print services.

The adapter uploads a model file, retrieves quotes across multiple materials
and vendors, and can place orders with shipping.

Environment variables
---------------------
``KILN_CRAFTCLOUD_API_KEY``
    API key for authenticating with the Craftcloud API.
``KILN_CRAFTCLOUD_BASE_URL``
    Base URL of the Craftcloud API (defaults to production).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
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

_DEFAULT_BASE_URL = "https://api.craftcloud3d.com/v1"

_STATUS_MAP: Dict[str, OrderStatus] = {
    "pending": OrderStatus.SUBMITTED,
    "submitted": OrderStatus.SUBMITTED,
    "confirmed": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "printing": OrderStatus.PRINTING,
    "shipped": OrderStatus.SHIPPING,
    "delivered": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "failed": OrderStatus.FAILED,
}


class CraftcloudProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Craftcloud API.

    Args:
        api_key: Craftcloud API key.  If not provided, reads from
            ``KILN_CRAFTCLOUD_API_KEY``.
        base_url: Base URL of the Craftcloud API.
        timeout: Per-request timeout in seconds.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
        self._base_url = (
            base_url
            or os.environ.get("KILN_CRAFTCLOUD_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self._timeout = timeout

        if not self._api_key:
            raise ValueError(
                "Craftcloud API key required. "
                "Set KILN_CRAFTCLOUD_API_KEY or pass api_key."
            )

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        })

    # -- FulfillmentProvider identity ----------------------------------------

    @property
    def name(self) -> str:
        return "craftcloud"

    @property
    def display_name(self) -> str:
        return "Craftcloud by All3DP"

    @property
    def supported_technologies(self) -> List[str]:
        return ["FDM", "SLA", "SLS", "MJF", "DMLS", "PolyJet"]

    # -- Internal HTTP helpers -----------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an authenticated HTTP request to the Craftcloud API."""
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
                    f"Craftcloud API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )
            try:
                return response.json()
            except ValueError:
                return {"status": "ok"}

        except Timeout as exc:
            raise FulfillmentError(
                f"Request to Craftcloud timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to Craftcloud API at {self._base_url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> List[Material]:
        """Return available materials from Craftcloud.

        Calls ``GET /materials`` for the full catalog.
        """
        try:
            data = self._request("GET", "/materials")
        except FulfillmentError:
            raise
        except Exception as exc:
            raise FulfillmentError(f"Failed to list materials: {exc}") from exc

        materials_raw = data.get("materials", data.get("data", []))
        if not isinstance(materials_raw, list):
            return []

        results: List[Material] = []
        for m in materials_raw:
            if not isinstance(m, dict):
                continue
            results.append(Material(
                id=str(m.get("id", "")),
                name=m.get("name", ""),
                technology=m.get("technology", ""),
                color=m.get("color", ""),
                finish=m.get("finish", ""),
                min_wall_mm=m.get("min_wall_thickness"),
                price_per_cm3=m.get("price_per_cm3"),
                currency=m.get("currency", "USD"),
            ))
        return results

    def get_quote(self, request: QuoteRequest) -> Quote:
        """Upload a model file and get a manufacturing quote.

        Steps:
        1. Upload the file via ``POST /uploads``
        2. Request a quote via ``POST /quotes`` referencing the upload
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
                    "/uploads",
                    files={"file": (filename, fh, "application/octet-stream")},
                )
        except PermissionError as exc:
            raise FulfillmentError(
                f"Permission denied reading file: {abs_path}",
                code="PERMISSION_ERROR",
            ) from exc

        upload_id = upload_data.get("upload_id") or upload_data.get("id", "")
        if not upload_id:
            raise FulfillmentError(
                "Craftcloud did not return an upload ID.",
                code="UPLOAD_ERROR",
            )

        # Step 2: Request quote
        quote_payload = {
            "upload_id": upload_id,
            "material_id": request.material_id,
            "quantity": request.quantity,
            "shipping_country": request.shipping_country,
        }
        if request.notes:
            quote_payload["notes"] = request.notes

        data = self._request("POST", "/quotes", json=quote_payload)

        # Parse shipping options
        shipping_raw = data.get("shipping_options", [])
        shipping: List[ShippingOption] = []
        for s in shipping_raw:
            if not isinstance(s, dict):
                continue
            shipping.append(ShippingOption(
                id=str(s.get("id", "")),
                name=s.get("name", ""),
                price=float(s.get("price", 0)),
                currency=s.get("currency", "USD"),
                estimated_days=s.get("estimated_days"),
            ))

        return Quote(
            quote_id=str(data.get("quote_id") or data.get("id", "")),
            provider=self.name,
            material=data.get("material", request.material_id),
            quantity=data.get("quantity", request.quantity),
            unit_price=float(data.get("unit_price", 0)),
            total_price=float(data.get("total_price", 0)),
            currency=data.get("currency", "USD"),
            lead_time_days=data.get("lead_time_days"),
            shipping_options=shipping,
            expires_at=data.get("expires_at"),
            raw=data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order based on a previously obtained quote.

        Calls ``POST /orders`` with the quote ID and shipping details.
        """
        payload: Dict[str, Any] = {
            "quote_id": request.quote_id,
        }
        if request.shipping_option_id:
            payload["shipping_option_id"] = request.shipping_option_id
        if request.shipping_address:
            payload["shipping_address"] = request.shipping_address
        if request.notes:
            payload["notes"] = request.notes

        data = self._request("POST", "/orders", json=payload)

        status_str = data.get("status", "submitted")
        mapped_status = _STATUS_MAP.get(status_str, OrderStatus.SUBMITTED)

        return OrderResult(
            success=True,
            order_id=str(data.get("order_id") or data.get("id", "")),
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("tracking_url"),
            tracking_number=data.get("tracking_number"),
            estimated_delivery=data.get("estimated_delivery"),
            total_price=data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the status of an existing order.

        Calls ``GET /orders/<order_id>``.
        """
        data = self._request("GET", f"/orders/{url_quote(order_id, safe='')}")

        status_str = data.get("status", "submitted")
        mapped_status = _STATUS_MAP.get(status_str, OrderStatus.SUBMITTED)

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("tracking_url"),
            tracking_number=data.get("tracking_number"),
            estimated_delivery=data.get("estimated_delivery"),
            total_price=data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order.

        Calls ``POST /orders/<order_id>/cancel``.
        """
        data = self._request(
            "POST", f"/orders/{url_quote(order_id, safe='')}/cancel"
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            provider=self.name,
            total_price=data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def __repr__(self) -> str:
        return f"<CraftcloudProvider base_url={self._base_url!r}>"
