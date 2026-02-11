"""Shapeways fulfillment adapter.

Implements :class:`~kiln.fulfillment.base.FulfillmentProvider` using the
`Shapeways API <https://developers.shapeways.com>`_.  Shapeways is an
on-demand 3D printing marketplace with 90+ materials across multiple
manufacturing technologies.

The adapter uploads model files, retrieves per-material pricing, and can
place orders with shipping.

Environment variables
---------------------
``KILN_SHAPEWAYS_CLIENT_ID``
    OAuth2 client ID for the Shapeways API.
``KILN_SHAPEWAYS_CLIENT_SECRET``
    OAuth2 client secret for the Shapeways API.
``KILN_SHAPEWAYS_BASE_URL``
    Base URL of the Shapeways API (defaults to production).
"""

from __future__ import annotations

import base64
import logging
import os
import time
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

_DEFAULT_BASE_URL = "https://api.shapeways.com"

_STATUS_MAP: Dict[str, OrderStatus] = {
    "placed": OrderStatus.SUBMITTED,
    "pending": OrderStatus.SUBMITTED,
    "in_production": OrderStatus.PRINTING,
    "production": OrderStatus.PRINTING,
    "printing": OrderStatus.PRINTING,
    "shipped": OrderStatus.SHIPPING,
    "delivered": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "failed": OrderStatus.FAILED,
}


class ShapewaysProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Shapeways API.

    Uses OAuth2 client-credentials flow for authentication.

    Args:
        client_id: OAuth2 client ID.  Falls back to
            ``KILN_SHAPEWAYS_CLIENT_ID``.
        client_secret: OAuth2 client secret.  Falls back to
            ``KILN_SHAPEWAYS_CLIENT_SECRET``.
        base_url: Base URL of the Shapeways API.
        timeout: Per-request timeout in seconds.

    Raises:
        ValueError: If credentials are not available.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self._client_id = (
            client_id or os.environ.get("KILN_SHAPEWAYS_CLIENT_ID", "")
        )
        self._client_secret = (
            client_secret or os.environ.get("KILN_SHAPEWAYS_CLIENT_SECRET", "")
        )
        self._base_url = (
            base_url
            or os.environ.get("KILN_SHAPEWAYS_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self._timeout = timeout

        if not self._client_id or not self._client_secret:
            raise ValueError(
                "Shapeways credentials required. "
                "Set KILN_SHAPEWAYS_CLIENT_ID and KILN_SHAPEWAYS_CLIENT_SECRET "
                "or pass client_id and client_secret."
            )

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Token state — lazily obtained on first request.
        self._access_token: Optional[str] = None
        self._token_expires: float = 0.0

    # -- FulfillmentProvider identity ----------------------------------------

    @property
    def name(self) -> str:
        return "shapeways"

    @property
    def display_name(self) -> str:
        return "Shapeways"

    @property
    def supported_technologies(self) -> List[str]:
        return ["FDM", "SLA", "SLS", "MJF", "DMLS", "PolyJet", "Wax", "Metal"]

    # -- Internal HTTP helpers -----------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _ensure_token(self) -> None:
        """Obtain or refresh the OAuth2 access token."""
        if self._access_token and time.time() < self._token_expires:
            return

        try:
            resp = self._session.post(
                self._url("/oauth2/token"),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=self._timeout,
            )
            if not resp.ok:
                raise FulfillmentError(
                    f"Shapeways OAuth2 token request failed: HTTP {resp.status_code}",
                    code=f"AUTH_{resp.status_code}",
                )
            token_data = resp.json()
            self._access_token = token_data.get("access_token", "")
            # Default to 1 hour minus buffer if expires_in not provided.
            expires_in = int(token_data.get("expires_in", 3600))
            self._token_expires = time.time() + expires_in - 60

            if not self._access_token:
                raise FulfillmentError(
                    "Shapeways OAuth2 response missing access_token.",
                    code="AUTH_ERROR",
                )
        except (Timeout, ReqConnectionError, RequestException) as exc:
            raise FulfillmentError(
                f"Failed to obtain Shapeways OAuth2 token: {exc}",
                code="AUTH_ERROR",
            ) from exc

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
        """Execute an authenticated HTTP request to the Shapeways API."""
        self._ensure_token()

        url = self._url(path)
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=self._timeout,
            )
            if not response.ok:
                raise FulfillmentError(
                    f"Shapeways API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
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
                f"Request to Shapeways timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to Shapeways API at {self._base_url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> List[Material]:
        """Return available materials from Shapeways.

        Calls ``GET /materials/v1`` for the full catalog.
        """
        try:
            data = self._request("GET", "/materials/v1")
        except FulfillmentError:
            raise
        except Exception as exc:
            raise FulfillmentError(f"Failed to list materials: {exc}") from exc

        # Shapeways returns {"materials": {"<id>": {...}, ...}}
        materials_raw = data.get("materials", {})
        if isinstance(materials_raw, list):
            # Normalize list format as well
            items = materials_raw
        elif isinstance(materials_raw, dict):
            items = list(materials_raw.values())
        else:
            return []

        results: List[Material] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            results.append(Material(
                id=str(m.get("materialId", m.get("id", ""))),
                name=m.get("title", m.get("name", "")),
                technology=m.get("printerId", m.get("technology", "")),
                color=m.get("color", ""),
                finish=m.get("finishId", m.get("finish", "")),
                min_wall_mm=m.get("minimumWallThickness"),
                price_per_cm3=m.get("pricePerCm3", m.get("price_per_cm3")),
                currency=m.get("currency", "USD"),
            ))
        return results

    def get_quote(self, request: QuoteRequest) -> Quote:
        """Upload a model and get pricing from Shapeways.

        Steps:
        1. Upload the file via ``POST /models/v1`` with base64-encoded data.
        2. Parse per-material pricing from the upload response or a subsequent
           ``GET /models/<id>/v1`` call.
        """
        abs_path = os.path.abspath(request.file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Model file not found: {abs_path}")

        filename = os.path.basename(abs_path)
        try:
            with open(abs_path, "rb") as fh:
                file_data = base64.b64encode(fh.read()).decode("ascii")
        except PermissionError as exc:
            raise FulfillmentError(
                f"Permission denied reading file: {abs_path}",
                code="PERMISSION_ERROR",
            ) from exc

        # Step 1: Upload model
        upload_payload = {
            "fileName": filename,
            "file": file_data,
            "hasRightsToModel": 1,
            "acceptTermsAndConditions": 1,
        }
        upload_data = self._request("POST", "/models/v1", json=upload_payload)

        model_id = str(
            upload_data.get("modelId")
            or upload_data.get("model_id")
            or upload_data.get("id", "")
        )
        if not model_id:
            raise FulfillmentError(
                "Shapeways did not return a model ID.",
                code="UPLOAD_ERROR",
            )

        # Step 2: Extract pricing for the requested material
        # Shapeways embeds per-material pricing in the model resource.
        materials_data = upload_data.get("materials", {})
        if isinstance(materials_data, dict):
            mat_info = materials_data.get(request.material_id, {})
        else:
            mat_info = {}

        unit_price = float(mat_info.get("price", 0))
        total_price = unit_price * request.quantity

        # Shipping options (from cart endpoint when available)
        shipping: List[ShippingOption] = []
        shipping_raw = upload_data.get("shipping_options", [])
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

        # Use model_id + material_id as the quote_id since Shapeways doesn't
        # have a separate quote concept.
        quote_id = f"sw-{model_id}-{request.material_id}"

        return Quote(
            quote_id=quote_id,
            provider=self.name,
            material=mat_info.get("title", request.material_id),
            quantity=request.quantity,
            unit_price=unit_price,
            total_price=total_price,
            currency=mat_info.get("currency", "USD"),
            lead_time_days=mat_info.get("lead_time_days"),
            shipping_options=shipping,
            expires_at=None,
            raw=upload_data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Shapeways.

        Calls ``POST /orders/v1`` with the model and material from the
        quote ID (format: ``sw-<model_id>-<material_id>``).
        """
        # Parse the composite quote_id
        parts = request.quote_id.split("-", 2)
        if len(parts) < 3 or parts[0] != "sw":
            raise FulfillmentError(
                f"Invalid Shapeways quote ID format: {request.quote_id}",
                code="INVALID_QUOTE",
            )
        model_id = parts[1]
        material_id = parts[2]

        order_item: Dict[str, Any] = {
            "modelId": int(model_id) if model_id.isdigit() else model_id,
            "materialId": int(material_id) if material_id.isdigit() else material_id,
            "quantity": 1,
        }
        payload: Dict[str, Any] = {"items": [order_item]}

        if request.shipping_option_id:
            payload["shippingOption"] = request.shipping_option_id
        if request.shipping_address:
            payload["shippingAddress"] = request.shipping_address
        if request.notes:
            payload["message"] = request.notes

        data = self._request("POST", "/orders/v1", json=payload)

        status_str = str(data.get("status", "placed")).lower()
        mapped_status = _STATUS_MAP.get(status_str, OrderStatus.SUBMITTED)

        return OrderResult(
            success=True,
            order_id=str(data.get("orderId") or data.get("order_id") or data.get("id", "")),
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("trackingUrl") or data.get("tracking_url"),
            tracking_number=data.get("trackingNumber") or data.get("tracking_number"),
            estimated_delivery=data.get("estimatedDelivery") or data.get("estimated_delivery"),
            total_price=data.get("totalPrice") or data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the status of an existing Shapeways order.

        Calls ``GET /orders/<order_id>/v1``.
        """
        safe_id = url_quote(order_id, safe="")
        data = self._request("GET", f"/orders/{safe_id}/v1")

        status_str = str(data.get("status", "placed")).lower()
        mapped_status = _STATUS_MAP.get(status_str, OrderStatus.SUBMITTED)

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=data.get("trackingUrl") or data.get("tracking_url"),
            tracking_number=data.get("trackingNumber") or data.get("tracking_number"),
            estimated_delivery=data.get("estimatedDelivery") or data.get("estimated_delivery"),
            total_price=data.get("totalPrice") or data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a Shapeways order.

        Calls ``POST /orders/<order_id>/cancel/v1``.  Shapeways may not
        support cancellation for orders already in production.
        """
        safe_id = url_quote(order_id, safe="")
        data = self._request("POST", f"/orders/{safe_id}/cancel/v1")

        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            provider=self.name,
            total_price=data.get("totalPrice") or data.get("total_price"),
            currency=data.get("currency", "USD"),
        )

    def __repr__(self) -> str:
        return f"<ShapewaysProvider base_url={self._base_url!r}>"
