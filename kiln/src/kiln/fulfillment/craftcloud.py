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
``KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL``
    URL for Craftcloud's material catalog endpoint (materialConfigIds).
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

_DEFAULT_BASE_URL = "https://api.craftcloud3d.com"
_DEFAULT_MATERIAL_CATALOG_URL = "http://customer-api.craftcloud3d.com/material-catalog"

_STATUS_MAP: Dict[str, OrderStatus] = {
    "pending": OrderStatus.SUBMITTED,
    "submitted": OrderStatus.SUBMITTED,
    "confirmed": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "printing": OrderStatus.PRINTING,
    "shipped": OrderStatus.SHIPPING,
    "delivered": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "failed": OrderStatus.FAILED,
}


class CraftcloudProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Craftcloud API.

    Args:
        api_key: Craftcloud API key.  If not provided, reads from
            ``KILN_CRAFTCLOUD_API_KEY``.
        base_url: Base URL of the Craftcloud API.
        material_catalog_url: URL for material catalog retrieval
            (materialConfigIds).
        timeout: Per-request timeout in seconds.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        material_catalog_url: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
        configured_base_url = (
            base_url
            or os.environ.get("KILN_CRAFTCLOUD_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self._base_urls = self._build_base_url_candidates(configured_base_url)
        self._base_url = self._base_urls[0]
        self._material_catalog_url = (
            material_catalog_url
            or os.environ.get(
                "KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL",
                _DEFAULT_MATERIAL_CATALOG_URL,
            )
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
            "X-API-Key": self._api_key,
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

    @staticmethod
    def _build_base_url_candidates(base_url: str) -> List[str]:
        """Return base URLs to try (host + host/v1 or vice versa)."""
        candidates = [base_url]
        if base_url.endswith("/v1"):
            alt = base_url[:-3].rstrip("/")
        else:
            alt = f"{base_url}/v1"
        if alt and alt not in candidates:
            candidates.append(alt)
        return candidates

    @staticmethod
    def _to_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("name", "label", "displayName", "display_name", "value", "id"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    @classmethod
    def _coalesce_text(cls, *values: Any) -> str:
        for value in values:
            text = cls._to_text(value)
            if text:
                return text
        return ""

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _url(self, path: str, *, base_url: Optional[str] = None) -> str:
        return f"{base_url or self._base_url}{path}"

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
        for idx, base_url in enumerate(self._base_urls):
            url = self._url(path, base_url=base_url)
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
                if response.ok:
                    # Persist the first working base URL for subsequent requests.
                    if idx != 0:
                        self._base_urls = [base_url] + [
                            candidate for candidate in self._base_urls if candidate != base_url
                        ]
                        self._base_url = self._base_urls[0]
                    try:
                        return response.json()
                    except ValueError:
                        return {"status": "ok"}

                # Some Craftcloud deployments expose endpoints at /v1 and others
                # at the host root; retry 404s with the alternate base URL.
                if response.status_code == 404 and idx < len(self._base_urls) - 1:
                    logger.debug(
                        "Craftcloud returned 404 for %s %s; retrying with %s",
                        method,
                        url,
                        self._base_urls[idx + 1],
                    )
                    continue
                raise FulfillmentError(
                    f"Craftcloud API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )

            except Timeout as exc:
                raise FulfillmentError(
                    f"Request to Craftcloud timed out after {self._timeout}s",
                    code="TIMEOUT",
                ) from exc
            except ReqConnectionError as exc:
                raise FulfillmentError(
                    f"Could not connect to Craftcloud API at {base_url}",
                    code="CONNECTION_ERROR",
                ) from exc
            except RequestException as exc:
                raise FulfillmentError(
                    f"Request error for {method} {path}: {exc}",
                    code="REQUEST_ERROR",
                ) from exc

        raise FulfillmentError(
            f"Could not resolve Craftcloud endpoint for {method} {path}",
            code="ENDPOINT_NOT_FOUND",
        )

    def _request_material_catalog(self) -> Any:
        """Retrieve Craftcloud material catalog payload."""
        try:
            response = self._session.request(
                "GET",
                self._material_catalog_url,
                timeout=self._timeout,
            )
            if not response.ok:
                raise FulfillmentError(
                    f"Craftcloud material catalog returned HTTP {response.status_code}: "
                    f"{response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )
            return response.json()
        except ValueError as exc:
            raise FulfillmentError(
                "Craftcloud material catalog returned invalid JSON.",
                code="INVALID_RESPONSE",
            ) from exc
        except Timeout as exc:
            raise FulfillmentError(
                f"Material catalog request timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to Craftcloud material catalog at "
                f"{self._material_catalog_url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for material catalog: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    @classmethod
    def _extract_material_records(cls, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []

        candidate_keys = (
            "materials",
            "materialCatalog",
            "material_catalog",
            "data",
            "results",
            "items",
        )
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = cls._extract_material_records(value)
                if nested:
                    return nested

        if any(
            key in payload
            for key in ("materialConfigId", "material_config_id", "materialId", "id")
        ):
            return [payload]

        for value in payload.values():
            if isinstance(value, list):
                rows = [item for item in value if isinstance(item, dict)]
                if rows:
                    return rows
        return []

    @classmethod
    def _material_from_catalog_record(cls, record: Dict[str, Any]) -> Optional[Material]:
        material_block = record.get("material")
        material_data = material_block if isinstance(material_block, dict) else {}

        material_id = cls._coalesce_text(
            record.get("materialConfigId"),
            record.get("material_config_id"),
            record.get("materialId"),
            record.get("material_id"),
            record.get("id"),
        )
        if not material_id:
            return None

        name = cls._coalesce_text(
            record.get("displayName"),
            record.get("display_name"),
            record.get("name"),
            record.get("materialName"),
            record.get("material_name"),
            material_data.get("name"),
            material_data.get("displayName"),
            material_data.get("label"),
            material_id,
        )

        technology = cls._coalesce_text(
            record.get("technology"),
            record.get("technologyName"),
            record.get("technology_name"),
            material_data.get("technology"),
            material_data.get("technologyName"),
        )
        color = cls._coalesce_text(
            record.get("color"),
            record.get("colorName"),
            record.get("color_name"),
            material_data.get("color"),
        )
        finish = cls._coalesce_text(
            record.get("finish"),
            record.get("finishName"),
            record.get("finish_name"),
            record.get("finishing"),
            record.get("finishingName"),
            record.get("finishing_name"),
            material_data.get("finish"),
            material_data.get("finishing"),
        )
        min_wall_mm = cls._to_float(
            record.get("minWallThickness")
            or record.get("min_wall_thickness")
            or material_data.get("minWallThickness")
            or material_data.get("min_wall_thickness")
        )
        price_per_cm3 = cls._to_float(
            record.get("pricePerCm3")
            or record.get("price_per_cm3")
            or record.get("pricePerCubicCm")
            or record.get("price_per_cubic_cm")
            or material_data.get("pricePerCm3")
            or material_data.get("price_per_cm3")
        )
        currency = cls._coalesce_text(
            record.get("currency"),
            material_data.get("currency"),
            "USD",
        )

        return Material(
            id=material_id,
            name=name or material_id,
            technology=technology,
            color=color,
            finish=finish,
            min_wall_mm=min_wall_mm,
            price_per_cm3=price_per_cm3,
            currency=currency,
        )

    def _list_materials_legacy(self) -> List[Material]:
        data = self._request("GET", "/materials")
        materials_raw = data.get("materials", data.get("data", []))
        if not isinstance(materials_raw, list):
            return []

        results: List[Material] = []
        for row in materials_raw:
            if not isinstance(row, dict):
                continue
            results.append(Material(
                id=str(row.get("id", "")),
                name=row.get("name", ""),
                technology=row.get("technology", ""),
                color=row.get("color", ""),
                finish=row.get("finish", ""),
                min_wall_mm=row.get("min_wall_thickness"),
                price_per_cm3=row.get("price_per_cm3"),
                currency=row.get("currency", "USD"),
            ))
        return results

    def _request_with_payload_fallback(
        self,
        path: str,
        *,
        primary_payload: Dict[str, Any],
        fallback_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            return self._request("POST", path, json=primary_payload)
        except FulfillmentError as exc:
            if primary_payload == fallback_payload:
                raise
            if not exc.code or not exc.code.startswith("HTTP_4"):
                raise
            logger.info(
                "Retrying Craftcloud %s with legacy payload schema after %s",
                path,
                exc.code,
            )
            return self._request("POST", path, json=fallback_payload)

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> List[Material]:
        """Return available materials from Craftcloud.

        Fetches the material catalog endpoint for materialConfigIds.
        Falls back to legacy ``GET /materials`` if needed.
        """
        try:
            catalog_payload = self._request_material_catalog()
            records = self._extract_material_records(catalog_payload)
            materials: List[Material] = []
            for record in records:
                material = self._material_from_catalog_record(record)
                if material is not None:
                    materials.append(material)
            if materials:
                return materials
            logger.warning(
                "Craftcloud material catalog returned no parseable records; "
                "falling back to legacy /materials endpoint."
            )
        except FulfillmentError as exc:
            logger.warning(
                "Failed to read Craftcloud material catalog (%s); "
                "falling back to legacy /materials endpoint.",
                exc,
            )

        try:
            return self._list_materials_legacy()
        except FulfillmentError:
            raise
        except Exception as exc:
            raise FulfillmentError(f"Failed to list materials: {exc}") from exc

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

        upload_id = self._coalesce_text(
            upload_data.get("uploadId"),
            upload_data.get("upload_id"),
            upload_data.get("id"),
        )
        if not upload_id:
            raise FulfillmentError(
                "Craftcloud did not return an upload ID.",
                code="UPLOAD_ERROR",
            )

        quote_payload = {
            "uploadId": upload_id,
            "materialConfigId": request.material_id,
            "quantity": request.quantity,
            "shippingCountry": request.shipping_country,
        }
        legacy_quote_payload = {
            "upload_id": upload_id,
            "material_id": request.material_id,
            "quantity": request.quantity,
            "shipping_country": request.shipping_country,
        }
        if request.notes:
            quote_payload["notes"] = request.notes
            legacy_quote_payload["notes"] = request.notes

        data = self._request_with_payload_fallback(
            "/quotes",
            primary_payload=quote_payload,
            fallback_payload=legacy_quote_payload,
        )

        # Parse shipping options
        shipping_raw = data.get("shipping_options", data.get("shippingOptions", []))
        shipping_rows: List[Dict[str, Any]]
        if isinstance(shipping_raw, list):
            shipping_rows = [item for item in shipping_raw if isinstance(item, dict)]
        elif isinstance(shipping_raw, dict):
            shipping_rows = [
                item for item in shipping_raw.values() if isinstance(item, dict)
            ]
        else:
            shipping_rows = []

        shipping: List[ShippingOption] = []
        for row in shipping_rows:
            ship_price = self._to_float(
                row.get("price")
                or row.get("shipping_price")
                or row.get("shippingPrice")
            )
            if ship_price is None:
                logger.warning(
                    "Craftcloud shipping option %r missing price field — skipping",
                    self._coalesce_text(
                        row.get("name"),
                        row.get("label"),
                        row.get("service"),
                        "unknown",
                    ),
                )
                continue
            shipping.append(ShippingOption(
                id=self._coalesce_text(
                    row.get("id"),
                    row.get("shippingOptionId"),
                    row.get("shipping_option_id"),
                ),
                name=self._coalesce_text(
                    row.get("name"),
                    row.get("label"),
                    row.get("service"),
                ),
                price=ship_price,
                currency=self._coalesce_text(
                    row.get("currency"),
                    data.get("currency"),
                    "USD",
                ),
                estimated_days=self._to_int(
                    row.get("estimated_days") or row.get("estimatedDays")
                ),
            ))

        quote_id = self._coalesce_text(
            data.get("quote_id"),
            data.get("quoteId"),
            data.get("id"),
        )
        if not quote_id:
            raise FulfillmentError(
                "Craftcloud quote response missing quote ID. "
                f"Response keys: {list(data.keys())}",
                code="MISSING_QUOTE_ID",
            )

        quantity = self._to_int(data.get("quantity")) or request.quantity
        unit_price = self._to_float(
            data.get("unit_price")
            or data.get("unitPrice")
            or data.get("price_per_unit")
            or data.get("pricePerUnit")
        ) or 0.0
        total_price = self._to_float(
            data.get("total_price")
            or data.get("totalPrice")
            or data.get("price")
        ) or 0.0
        if unit_price <= 0 and total_price > 0 and quantity > 0:
            unit_price = total_price / quantity
        if total_price <= 0 and unit_price > 0:
            total_price = unit_price * quantity

        if unit_price <= 0 and total_price <= 0:
            logger.warning(
                "Craftcloud returned $0 pricing — API field names may have changed. "
                "Response keys: %s",
                list(data.keys()),
            )
            raise FulfillmentError(
                "Craftcloud returned zero pricing. This likely means the API "
                "response format has changed. Contact support or check API docs. "
                f"Response keys: {list(data.keys())}",
                code="ZERO_PRICE",
            )

        return Quote(
            quote_id=quote_id,
            provider=self.name,
            material=self._coalesce_text(
                data.get("material"),
                data.get("materialName"),
                data.get("material_name"),
                request.material_id,
            ),
            quantity=quantity,
            unit_price=unit_price,
            total_price=total_price,
            currency=self._coalesce_text(data.get("currency"), "USD"),
            lead_time_days=self._to_int(
                data.get("lead_time_days")
                or data.get("leadTimeDays")
                or data.get("lead_time")
                or data.get("leadTime")
            ),
            shipping_options=shipping,
            expires_at=self._to_float(data.get("expires_at") or data.get("expiresAt")),
            raw=data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order based on a previously obtained quote.

        Calls ``POST /orders`` with the quote ID and shipping details.
        """
        payload: Dict[str, Any] = {
            "quoteId": request.quote_id,
        }
        legacy_payload: Dict[str, Any] = {
            "quote_id": request.quote_id,
        }
        if request.shipping_option_id:
            payload["shippingOptionId"] = request.shipping_option_id
            legacy_payload["shipping_option_id"] = request.shipping_option_id
        if request.shipping_address:
            payload["shippingAddress"] = request.shipping_address
            legacy_payload["shipping_address"] = request.shipping_address
        if request.notes:
            payload["notes"] = request.notes
            legacy_payload["notes"] = request.notes

        data = self._request_with_payload_fallback(
            "/orders",
            primary_payload=payload,
            fallback_payload=legacy_payload,
        )

        status_str = self._coalesce_text(data.get("status"), "submitted").lower()
        mapped_status = _STATUS_MAP.get(status_str)
        if mapped_status is None:
            logger.warning(
                "Unknown Craftcloud order status %r — defaulting to SUBMITTED. "
                "The API may have added new statuses.",
                status_str,
            )
            mapped_status = OrderStatus.SUBMITTED

        order_id = self._coalesce_text(
            data.get("order_id"),
            data.get("orderId"),
            data.get("id"),
        )
        if not order_id:
            raise FulfillmentError(
                "Craftcloud order response missing order ID. "
                f"Response keys: {list(data.keys())}",
                code="MISSING_ORDER_ID",
            )

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=self._coalesce_text(
                data.get("tracking_url"),
                data.get("trackingUrl"),
            ) or None,
            tracking_number=self._coalesce_text(
                data.get("tracking_number"),
                data.get("trackingNumber"),
            ) or None,
            estimated_delivery=self._coalesce_text(
                data.get("estimated_delivery"),
                data.get("estimatedDelivery"),
            ) or None,
            total_price=self._to_float(data.get("total_price") or data.get("totalPrice")),
            currency=self._coalesce_text(data.get("currency"), "USD"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the status of an existing order.

        Calls ``GET /orders/<order_id>``.
        """
        data = self._request("GET", f"/orders/{url_quote(order_id, safe='')}")

        status_str = self._coalesce_text(data.get("status"), "submitted").lower()
        mapped_status = _STATUS_MAP.get(status_str)
        if mapped_status is None:
            logger.warning(
                "Unknown Craftcloud order status %r for order %s — defaulting to SUBMITTED",
                status_str, order_id,
            )
            mapped_status = OrderStatus.SUBMITTED

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=self._coalesce_text(
                data.get("tracking_url"),
                data.get("trackingUrl"),
            ) or None,
            tracking_number=self._coalesce_text(
                data.get("tracking_number"),
                data.get("trackingNumber"),
            ) or None,
            estimated_delivery=self._coalesce_text(
                data.get("estimated_delivery"),
                data.get("estimatedDelivery"),
            ) or None,
            total_price=self._to_float(data.get("total_price") or data.get("totalPrice")),
            currency=self._coalesce_text(data.get("currency"), "USD"),
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
            total_price=self._to_float(data.get("total_price") or data.get("totalPrice")),
            currency=self._coalesce_text(data.get("currency"), "USD"),
        )

    def __repr__(self) -> str:
        return (
            "<CraftcloudProvider "
            f"base_url={self._base_url!r} "
            f"material_catalog_url={self._material_catalog_url!r}>"
        )
