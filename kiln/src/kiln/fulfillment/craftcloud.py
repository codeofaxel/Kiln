"""Craftcloud by All3DP fulfillment adapter.

Implements :class:`~kiln.fulfillment.base.FulfillmentProvider` using the
`Craftcloud v5 API <https://api.craftcloud3d.com/docs>`_.  Craftcloud is a 3D
printing price comparison service that aggregates quotes from 150+ print
services.

The adapter follows the v5 flow:

1. Upload model → ``POST /v5/model``
2. Poll until parsed → ``GET /v5/model/{modelId}`` (200 = ready, 206 = parsing)
3. Request prices → ``POST /v5/price`` (async)
4. Poll prices → ``GET /v5/price/{priceId}`` until ``allComplete`` is true
5. Create cart → ``POST /v5/cart`` (select quote + shipping)
6. Place order → ``POST /v5/order`` (cart + shipping/billing address)
7. Track order → ``GET /v5/order/{orderId}/status``

Environment variables
---------------------
``KILN_CRAFTCLOUD_API_KEY``
    API key for authenticating with the Craftcloud API (partner endpoints).
``KILN_CRAFTCLOUD_BASE_URL``
    Base URL of the Craftcloud API (defaults to production).
``KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL``
    URL for Craftcloud's material catalog endpoint (materialConfigIds).
``KILN_CRAFTCLOUD_POLL_INTERVAL``
    Seconds between price polling requests (default 2).
``KILN_CRAFTCLOUD_MAX_POLL_ATTEMPTS``
    Maximum polling attempts before timeout (default 60).
"""

from __future__ import annotations

import logging
import os
import time
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

_DEFAULT_BASE_URL = "https://api.craftcloud3d.com"
_DEFAULT_MATERIAL_CATALOG_URL = "http://customer-api.craftcloud3d.com/material-catalog"
_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_MAX_POLL_ATTEMPTS = 60

# Craftcloud v5 order status types → internal OrderStatus.
_STATUS_MAP: dict[str, OrderStatus] = {
    "ordered": OrderStatus.SUBMITTED,
    "in_production": OrderStatus.PRINTING,
    "shipped": OrderStatus.SHIPPING,
    "received": OrderStatus.DELIVERED,
    "blocked": OrderStatus.PROCESSING,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
}


class CraftcloudProvider(FulfillmentProvider):
    """Concrete :class:`FulfillmentProvider` backed by the Craftcloud v5 API.

    Args:
        api_key: Craftcloud API key.  If not provided, reads from
            ``KILN_CRAFTCLOUD_API_KEY``.
        base_url: Base URL of the Craftcloud API.
        material_catalog_url: URL for material catalog retrieval
            (materialConfigIds).
        timeout: Per-request timeout in seconds.
        poll_interval: Seconds between async price polling requests.
        max_poll_attempts: Maximum polling iterations before giving up.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        material_catalog_url: str | None = None,
        timeout: int = 60,
        *,
        poll_interval: float | None = None,
        max_poll_attempts: int | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
        self._base_url = (base_url or os.environ.get("KILN_CRAFTCLOUD_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")
        self._material_catalog_url = (
            material_catalog_url
            or os.environ.get(
                "KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL",
                _DEFAULT_MATERIAL_CATALOG_URL,
            )
        ).rstrip("/")
        self._timeout = timeout
        self._poll_interval = (
            poll_interval
            if poll_interval is not None
            else float(os.environ.get("KILN_CRAFTCLOUD_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL))
        )
        self._max_poll_attempts = (
            max_poll_attempts
            if max_poll_attempts is not None
            else int(os.environ.get("KILN_CRAFTCLOUD_MAX_POLL_ATTEMPTS", _DEFAULT_MAX_POLL_ATTEMPTS))
        )

        if not self._api_key:
            raise ValueError("Craftcloud API key required. Set KILN_CRAFTCLOUD_API_KEY or pass api_key.")

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "X-API-Key": self._api_key,
            }
        )

    # -- FulfillmentProvider identity ----------------------------------------

    @property
    def name(self) -> str:
        return "craftcloud"

    @property
    def display_name(self) -> str:
        return "Craftcloud by All3DP"

    @property
    def supported_technologies(self) -> list[str]:
        return ["FDM", "SLA", "SLS", "MJF", "DMLS", "PolyJet"]

    # -- Internal helpers ----------------------------------------------------

    @staticmethod
    def _to_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("name", "label", "displayName", "value", "id"):
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
    def _to_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
        """Execute an HTTP request to the Craftcloud v5 API."""
        url = f"{self._base_url}{path}"
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
                try:
                    return response.json()
                except ValueError:
                    return {"status": "ok"}

            raise FulfillmentError(
                f"Craftcloud API returned HTTP {response.status_code} for {method} {path}: {response.text[:500]}",
                code=f"HTTP_{response.status_code}",
            )

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

    def _request_external(self, method: str, url: str) -> Any:
        """HTTP request to a non-v5 URL (e.g. material catalog)."""
        try:
            response = self._session.request(method, url, timeout=self._timeout)
            if not response.ok:
                raise FulfillmentError(
                    f"Craftcloud returned HTTP {response.status_code} for {method} {url}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )
            return response.json()
        except ValueError as exc:
            raise FulfillmentError(
                f"Invalid JSON from {url}",
                code="INVALID_RESPONSE",
            ) from exc
        except Timeout as exc:
            raise FulfillmentError(
                f"Request to {url} timed out after {self._timeout}s",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise FulfillmentError(
                f"Could not connect to {url}",
                code="CONNECTION_ERROR",
            ) from exc
        except RequestException as exc:
            raise FulfillmentError(
                f"Request error for {url}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- v5 upload + model polling -------------------------------------------

    def _upload_model(self, file_path: str) -> str:
        """Upload a model file and return the modelId.

        Calls ``POST /v5/model`` then polls ``GET /v5/model/{modelId}``
        until parsing is complete (HTTP 200; 206 means still parsing).
        """
        filename = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as fh:
                result = self._request(
                    "POST",
                    "/v5/model",
                    files={"file": (filename, fh, "application/octet-stream")},
                )
        except PermissionError as exc:
            raise FulfillmentError(
                f"Permission denied reading file: {file_path}",
                code="PERMISSION_ERROR",
            ) from exc

        # Response is an array of model objects.
        models: list[dict[str, Any]]
        if isinstance(result, list):
            models = result
        elif isinstance(result, dict):
            models = result.get("models", result.get("data", [result]))
            if not isinstance(models, list):
                models = [result]
        else:
            raise FulfillmentError(
                "Craftcloud model upload returned unexpected response type.",
                code="UPLOAD_ERROR",
            )

        if not models:
            raise FulfillmentError(
                "Craftcloud model upload returned empty model list.",
                code="UPLOAD_ERROR",
            )

        model_id = models[0].get("modelId") or models[0].get("id", "")
        if not model_id:
            raise FulfillmentError(
                f"Craftcloud model upload response missing modelId. Keys: {list(models[0].keys())}",
                code="UPLOAD_ERROR",
            )

        # Poll until model parsing is complete (200 = done, 206 = parsing).
        for attempt in range(self._max_poll_attempts):
            url = f"{self._base_url}/v5/model/{url_quote(str(model_id), safe='')}"
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except RequestException as exc:
                raise FulfillmentError(
                    f"Error polling model {model_id}: {exc}",
                    code="MODEL_POLL_ERROR",
                ) from exc

            if resp.status_code == 200:
                return str(model_id)
            if resp.status_code == 206:
                logger.debug(
                    "Model %s still parsing (attempt %d/%d)",
                    model_id,
                    attempt + 1,
                    self._max_poll_attempts,
                )
                time.sleep(self._poll_interval)
                continue

            raise FulfillmentError(
                f"Unexpected status {resp.status_code} polling model {model_id}: {resp.text[:300]}",
                code=f"HTTP_{resp.status_code}",
            )

        raise FulfillmentError(
            f"Model {model_id} did not finish parsing after "
            f"{self._max_poll_attempts} attempts "
            f"({self._max_poll_attempts * self._poll_interval:.0f}s).",
            code="MODEL_PARSE_TIMEOUT",
        )

    # -- v5 pricing ----------------------------------------------------------

    def _request_prices(
        self,
        model_id: str,
        *,
        quantity: int = 1,
        currency: str = "USD",
        country_code: str = "US",
        material_config_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Request prices and poll until all vendors respond.

        Calls ``POST /v5/price`` then polls ``GET /v5/price/{priceId}``
        until ``allComplete`` is true.
        """
        payload: dict[str, Any] = {
            "currency": currency,
            "countryCode": country_code,
            "models": [
                {
                    "modelId": model_id,
                    "quantity": quantity,
                    "scale": 1,
                },
            ],
        }
        if material_config_ids:
            payload["materialConfigIds"] = material_config_ids

        price_response = self._request("POST", "/v5/price", json=payload)
        price_id = ""
        if isinstance(price_response, dict):
            price_id = price_response.get("priceId", "")
        if not price_id:
            raise FulfillmentError(
                f"Craftcloud price request did not return a priceId. Response: {price_response}",
                code="PRICE_REQUEST_ERROR",
            )

        # Poll for results.
        for attempt in range(self._max_poll_attempts):
            data = self._request(
                "GET",
                f"/v5/price/{url_quote(str(price_id), safe='')}",
            )
            if not isinstance(data, dict):
                raise FulfillmentError(
                    f"Unexpected price poll response type: {type(data).__name__}",
                    code="INVALID_RESPONSE",
                )

            if data.get("allComplete", False):
                return data

            logger.debug(
                "Prices not complete for %s (attempt %d/%d)",
                price_id,
                attempt + 1,
                self._max_poll_attempts,
            )
            time.sleep(self._poll_interval)

        raise FulfillmentError(
            f"Price request {price_id} did not complete after "
            f"{self._max_poll_attempts} attempts "
            f"({self._max_poll_attempts * self._poll_interval:.0f}s).",
            code="PRICE_POLL_TIMEOUT",
        )

    # -- v5 cart + order -----------------------------------------------------

    def _create_cart(
        self,
        quote_ids: list[str],
        shipping_ids: list[str],
        *,
        currency: str = "USD",
    ) -> str:
        """Create a cart from selected quotes and shipping options."""
        payload: dict[str, Any] = {
            "quotes": quote_ids,
            "shippingIds": shipping_ids,
            "currency": currency,
        }
        result = self._request("POST", "/v5/cart", json=payload)
        if not isinstance(result, dict):
            raise FulfillmentError(
                "Craftcloud cart creation returned unexpected response.",
                code="CART_ERROR",
            )

        cart_id = result.get("cartId") or result.get("id", "")
        if not cart_id:
            raise FulfillmentError(
                f"Craftcloud cart response missing cartId. Keys: {list(result.keys())}",
                code="CART_ERROR",
            )
        return str(cart_id)

    @staticmethod
    def _build_user_payload(shipping_address: dict[str, str]) -> dict[str, Any]:
        """Build the ``user`` object for ``POST /v5/order``.

        Maps from Kiln's flat address dict to Craftcloud's nested
        ``user.shipping`` / ``user.billing`` schema with camelCase fields.
        """
        shipping = {
            "firstName": shipping_address.get("first_name", shipping_address.get("firstName", "")),
            "lastName": shipping_address.get("last_name", shipping_address.get("lastName", "")),
            "address": shipping_address.get("street", shipping_address.get("address", "")),
            "addressLine2": shipping_address.get("street2", shipping_address.get("addressLine2")) or None,
            "city": shipping_address.get("city", ""),
            "zipCode": shipping_address.get(
                "postal_code", shipping_address.get("zipCode", shipping_address.get("zip", ""))
            ),
            "stateCode": shipping_address.get("state", shipping_address.get("stateCode")) or None,
            "countryCode": shipping_address.get("country", shipping_address.get("countryCode", "US")),
            "companyName": shipping_address.get("company", shipping_address.get("companyName")) or None,
            "phoneNumber": shipping_address.get("phone", shipping_address.get("phoneNumber", "")),
        }

        billing = {
            **shipping,
            "isCompany": bool(shipping.get("companyName")),
            "vatId": shipping_address.get("vat_id", shipping_address.get("vatId")) or None,
        }

        email_addr = shipping_address.get("email", "") or shipping_address.get("emailAddress", "")

        return {
            "emailAddress": email_addr,
            "shipping": shipping,
            "billing": billing,
        }

    # -- Material catalog parsing --------------------------------------------

    @classmethod
    def _parse_material_catalog(cls, payload: Any) -> list[Material]:
        """Parse the customer-api material catalog response.

        The catalog has the structure::

            {
                "materialStructure": [
                    {
                        "name": "Nylon",
                        "materials": [
                            {
                                "technology": "SLS",
                                "finishGroups": [
                                    {
                                        "name": "Standard",
                                        "materialConfigs": [
                                            {
                                                "id": "<materialConfigId>",
                                                "name": "SLS Nylon PA12 ...",
                                                "color": "White",
                                            }
                                        ]
                                    }
                                ]
                            }
                        ],
                        "printingMethods": [
                            {"minWallThickness": 0.8}
                        ]
                    }
                ]
            }
        """
        if not isinstance(payload, dict):
            return []

        material_groups = payload.get("materialStructure", [])
        if not isinstance(material_groups, list):
            return []

        results: list[Material] = []
        for group in material_groups:
            if not isinstance(group, dict):
                continue

            # Extract min wall thickness from printing methods.
            printing_methods = group.get("printingMethods", [])
            min_wall_mm: float | None = None
            if isinstance(printing_methods, list):
                for pm in printing_methods:
                    if isinstance(pm, dict):
                        wall = cls._to_float(pm.get("minWallThickness"))
                        if wall is not None and (min_wall_mm is None or wall < min_wall_mm):
                            min_wall_mm = wall

            materials_list = group.get("materials", [])
            if not isinstance(materials_list, list):
                continue

            for material in materials_list:
                if not isinstance(material, dict):
                    continue

                technology = material.get("technology", "")
                finish_groups = material.get("finishGroups", [])
                if not isinstance(finish_groups, list):
                    continue

                for fg in finish_groups:
                    if not isinstance(fg, dict):
                        continue

                    finish_name = fg.get("name", "")
                    configs = fg.get("materialConfigs", [])
                    if not isinstance(configs, list):
                        continue

                    for config in configs:
                        if not isinstance(config, dict):
                            continue

                        config_id = config.get("id", "")
                        if not config_id:
                            continue

                        results.append(
                            Material(
                                id=str(config_id),
                                name=config.get("name", "") or str(config_id),
                                technology=str(technology),
                                color=config.get("color", ""),
                                finish=str(finish_name),
                                min_wall_mm=min_wall_mm,
                                currency="USD",
                            )
                        )

        return results

    # -- FulfillmentProvider methods -----------------------------------------

    def list_materials(self) -> list[Material]:
        """Return available materials from the Craftcloud catalog.

        Fetches the material catalog at
        ``customer-api.craftcloud3d.com/material-catalog`` and extracts
        all materialConfigId entries.
        """
        try:
            catalog_payload = self._request_external("GET", self._material_catalog_url)
            materials = self._parse_material_catalog(catalog_payload)
            if materials:
                return materials
            logger.warning("Craftcloud material catalog returned no parseable materials.")
            raise FulfillmentError(
                "Craftcloud material catalog returned no materials. The catalog format may have changed.",
                code="EMPTY_CATALOG",
            )
        except FulfillmentError:
            raise
        except Exception as exc:
            raise FulfillmentError(
                f"Failed to list materials: {exc}",
                code="CATALOG_ERROR",
            ) from exc

    def get_quote(self, request: QuoteRequest) -> Quote:
        """Upload a model file and get manufacturing quotes.

        Follows the Craftcloud v5 flow:

        1. Upload file → ``POST /v5/model``
        2. Poll model parsing → ``GET /v5/model/{modelId}``
        3. Request prices → ``POST /v5/price``
        4. Poll prices → ``GET /v5/price/{priceId}``
        5. Return the cheapest matching quote with shipping options.
        """
        abs_path = os.path.abspath(request.file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Model file not found: {abs_path}")

        # Step 1-2: Upload and wait for model parsing.
        model_id = self._upload_model(abs_path)

        # Step 3-4: Request and poll prices.
        material_filter = [request.material_id] if request.material_id else None
        price_data = self._request_prices(
            model_id,
            quantity=request.quantity,
            currency="USD",
            country_code=request.shipping_country,
            material_config_ids=material_filter,
        )

        # Parse quotes — find the cheapest for the requested material.
        quotes_raw = price_data.get("quotes", [])
        if not isinstance(quotes_raw, list) or not quotes_raw:
            raise FulfillmentError(
                "Craftcloud returned no quotes for this model/material. "
                "Try a different material or check that the model is printable. "
                f"Response keys: {list(price_data.keys())}",
                code="NO_QUOTES",
            )

        best_quote: dict[str, Any] | None = None
        for q in quotes_raw:
            if not isinstance(q, dict):
                continue
            if request.material_id and q.get("materialConfigId") != request.material_id:
                continue
            price = self._to_float(q.get("price"))
            if price is None or price <= 0:
                continue
            if best_quote is None or (price < (self._to_float(best_quote.get("price")) or float("inf"))):
                best_quote = q

        if best_quote is None:
            raise FulfillmentError(
                "No valid priced quotes returned. The material may not be "
                f"available for this model. Quotes checked: {len(quotes_raw)}",
                code="NO_VALID_QUOTES",
            )

        # Parse shipping options for this quote's vendor.
        vendor_id = best_quote.get("vendorId", "")
        shippings_raw = price_data.get("shippings", [])
        shipping_options: list[ShippingOption] = []
        if isinstance(shippings_raw, list):
            for s in shippings_raw:
                if not isinstance(s, dict):
                    continue
                if s.get("vendorId") != vendor_id:
                    continue
                ship_price = self._to_float(s.get("price"))
                if ship_price is None:
                    continue
                # deliveryTime format: "3-5" or "3" — take the high end.
                delivery_time = s.get("deliveryTime", "")
                estimated_days: int | None = None
                if isinstance(delivery_time, str) and delivery_time:
                    parts = delivery_time.split("-")
                    estimated_days = self._to_int(parts[-1])

                shipping_options.append(
                    ShippingOption(
                        id=s.get("shippingId", s.get("id", "")),
                        name=self._coalesce_text(
                            s.get("name"),
                            s.get("carrier"),
                            s.get("type", ""),
                        ),
                        price=ship_price,
                        currency=s.get("currency", "USD"),
                        estimated_days=estimated_days,
                    )
                )

        quote_id = best_quote.get("quoteId", "")
        if not quote_id:
            raise FulfillmentError(
                f"Craftcloud quote missing quoteId. Quote keys: {list(best_quote.keys())}",
                code="MISSING_QUOTE_ID",
            )

        unit_price = self._to_float(best_quote.get("price")) or 0.0
        quantity = self._to_int(best_quote.get("quantity")) or request.quantity
        total_price = unit_price * quantity
        lead_time = self._to_int(best_quote.get("productionTimeSlow"))

        expires_at = self._to_float(price_data.get("expiresAt"))
        # Craftcloud sends milliseconds; convert to seconds.
        if expires_at and expires_at > 1e12:
            expires_at = expires_at / 1000.0

        return Quote(
            quote_id=quote_id,
            provider=self.name,
            material=self._coalesce_text(
                best_quote.get("materialConfigId"),
                request.material_id,
            ),
            quantity=quantity,
            unit_price=unit_price,
            total_price=total_price,
            currency=best_quote.get("currency", "USD"),
            lead_time_days=lead_time,
            shipping_options=shipping_options,
            expires_at=expires_at,
            raw=price_data,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order based on a previously obtained quote.

        Follows the Craftcloud v5 flow:

        1. Create cart → ``POST /v5/cart`` with quoteId + shippingId
        2. Place order → ``POST /v5/order`` with cartId + user info
        """
        if not request.quote_id:
            raise FulfillmentError(
                "quote_id is required to place an order.",
                code="MISSING_QUOTE_ID",
            )

        shipping_ids: list[str] = []
        if request.shipping_option_id:
            shipping_ids = [request.shipping_option_id]

        # Step 1: Create cart.
        cart_id = self._create_cart(
            quote_ids=[request.quote_id],
            shipping_ids=shipping_ids,
        )

        # Step 2: Place order.
        order_payload: dict[str, Any] = {"cartId": cart_id}
        if request.shipping_address:
            order_payload["user"] = self._build_user_payload(
                request.shipping_address,
            )

        data = self._request("POST", "/v5/order", json=order_payload)
        if not isinstance(data, dict):
            raise FulfillmentError(
                "Craftcloud order response was not a JSON object.",
                code="INVALID_RESPONSE",
            )

        order_id = data.get("orderId", data.get("id", ""))
        if not order_id:
            raise FulfillmentError(
                f"Craftcloud order response missing orderId. Keys: {list(data.keys())}",
                code="MISSING_ORDER_ID",
            )

        # Extract total from amounts if available.
        amounts = data.get("amounts", {})
        total_data = amounts.get("total", {}) if isinstance(amounts, dict) else {}
        total_price = (
            self._to_float(total_data.get("totalGrossPrice") or total_data.get("totalNetPrice"))
            if isinstance(total_data, dict)
            else None
        )
        currency = total_data.get("currency", "USD") if isinstance(total_data, dict) else "USD"

        return OrderResult(
            success=True,
            order_id=str(order_id),
            status=OrderStatus.SUBMITTED,
            provider=self.name,
            total_price=total_price,
            currency=currency,
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the status of an existing order.

        Calls ``GET /v5/order/{orderId}/status``.
        """
        safe_id = url_quote(order_id, safe="")
        data = self._request("GET", f"/v5/order/{safe_id}/status")
        if not isinstance(data, dict):
            raise FulfillmentError(
                "Craftcloud order status response was not a JSON object.",
                code="INVALID_RESPONSE",
            )

        # Response: {orderNumber, status: [{vendorId, cancelled,
        #   orderStatus: [{type, date}]}], estDeliveryTime}
        mapped_status = OrderStatus.SUBMITTED
        tracking_url: str | None = None
        tracking_number: str | None = None

        status_entries = data.get("status", [])
        if isinstance(status_entries, list):
            for entry in status_entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("cancelled"):
                    mapped_status = OrderStatus.CANCELLED
                    break
                order_statuses = entry.get("orderStatus", [])
                if isinstance(order_statuses, list) and order_statuses:
                    latest = order_statuses[-1]
                    if isinstance(latest, dict):
                        status_type = latest.get("type", "")
                        if status_type in _STATUS_MAP:
                            mapped_status = _STATUS_MAP[status_type]
                tracking_url = tracking_url or entry.get("trackingUrl") or None
                tracking_number = tracking_number or entry.get("trackingNumber") or None

        estimated_delivery = data.get("estDeliveryTime")
        if estimated_delivery is not None:
            estimated_delivery = str(estimated_delivery)

        return OrderResult(
            success=True,
            order_id=order_id,
            status=mapped_status,
            provider=self.name,
            tracking_url=tracking_url,
            tracking_number=tracking_number,
            estimated_delivery=estimated_delivery,
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order.

        Calls ``PATCH /v5/order/{orderId}/status`` with cancelled status
        for each vendor.  May fail if already in production.
        """
        safe_id = url_quote(order_id, safe="")

        # Get current status to find vendor IDs.
        status_data = self._request("GET", f"/v5/order/{safe_id}/status")
        if not isinstance(status_data, dict):
            raise FulfillmentError(
                "Cannot cancel: failed to read order status.",
                code="CANCEL_ERROR",
            )

        vendor_updates: list[dict[str, Any]] = []
        status_entries = status_data.get("status", [])
        if isinstance(status_entries, list):
            for entry in status_entries:
                if isinstance(entry, dict) and entry.get("vendorId"):
                    vendor_updates.append(
                        {
                            "vendorId": entry["vendorId"],
                            "status": "cancelled",
                        }
                    )

        if not vendor_updates:
            raise FulfillmentError(
                "Cannot cancel: no vendor entries found in order status.",
                code="CANCEL_ERROR",
            )

        self._request(
            "PATCH",
            f"/v5/order/{safe_id}/status",
            json=vendor_updates,
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            provider=self.name,
        )

    def __repr__(self) -> str:
        return f"<CraftcloudProvider base_url={self._base_url!r} material_catalog_url={self._material_catalog_url!r}>"
