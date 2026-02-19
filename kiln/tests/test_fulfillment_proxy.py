"""Tests for kiln.fulfillment.proxy — Kiln Cloud proxy fulfillment adapter.

Covers:
- ProxyProvider constructor (default URL, custom URL, env vars, license key sources)
- list_materials (success, empty, invalid response, HTTP errors)
- get_quote (file upload, file not found, permission error, invalid response)
- place_order (success, missing quote_id, HTTP 402)
- get_order_status (success, HTTP 404)
- cancel_order (success, HTTP error)
- HTTP layer (timeout, connection error, generic request error)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import responses

from kiln.fulfillment.base import (
    FulfillmentError,
    Material,
    OrderRequest,
    OrderStatus,
    QuoteRequest,
)
from kiln.fulfillment.proxy import ProxyProvider, _DEFAULT_PROXY_URL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = _DEFAULT_PROXY_URL


def _provider(**kwargs: Any) -> ProxyProvider:
    """Create a ProxyProvider with sensible test defaults."""
    defaults: dict[str, Any] = {
        "license_key": "test-license-key",
    }
    defaults.update(kwargs)
    return ProxyProvider(**defaults)


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

_MATERIALS_RESPONSE: dict[str, Any] = {
    "materials": [
        {
            "id": "pla-white",
            "name": "PLA White",
            "technology": "FDM",
            "color": "White",
            "finish": "raw",
            "min_wall_mm": 0.8,
            "price_per_cm3": 0.05,
            "currency": "USD",
        },
        {
            "id": "nylon-pa12",
            "name": "Nylon PA12",
            "technology": "SLS",
            "color": "White",
            "finish": "standard",
            "min_wall_mm": 0.7,
            "price_per_cm3": 0.12,
            "currency": "EUR",
        },
    ],
}

_QUOTE_RESPONSE: dict[str, Any] = {
    "success": True,
    "quote": {
        "quote_id": "q-abc123",
        "provider": "craftcloud",
        "material": "PLA White",
        "quantity": 2,
        "unit_price": 15.50,
        "total_price": 31.00,
        "currency": "USD",
        "lead_time_days": 7,
        "expires_at": "2026-03-01T00:00:00Z",
        "shipping_options": [
            {
                "id": "ship-std",
                "name": "Standard",
                "price": 5.99,
                "currency": "USD",
                "estimated_days": 5,
            },
            {
                "id": "ship-exp",
                "name": "Express",
                "price": 14.99,
                "currency": "USD",
                "estimated_days": 2,
            },
        ],
    },
}

_ORDER_RESPONSE: dict[str, Any] = {
    "order": {
        "success": True,
        "order_id": "o-xyz789",
        "status": "processing",
        "provider": "craftcloud",
        "tracking_url": "https://track.example.com/o-xyz789",
        "tracking_number": "1Z999",
        "estimated_delivery": "2026-03-10",
        "total_price": 36.99,
        "currency": "USD",
    },
}

_ORDER_STATUS_RESPONSE: dict[str, Any] = {
    "order": {
        "success": True,
        "order_id": "o-xyz789",
        "status": "shipping",
        "provider": "craftcloud",
        "tracking_url": "https://track.example.com/o-xyz789",
        "tracking_number": "1Z999",
        "estimated_delivery": "2026-03-10",
        "total_price": 36.99,
        "currency": "USD",
    },
}

_CANCEL_RESPONSE: dict[str, Any] = {
    "order": {
        "success": True,
        "order_id": "o-xyz789",
        "status": "cancelled",
        "provider": "craftcloud",
    },
}


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestProxyProviderConstructor:
    def test_default_url(self):
        p = _provider()
        assert p._proxy_url == _DEFAULT_PROXY_URL

    def test_custom_url_via_param(self):
        p = _provider(proxy_url="https://staging.kiln3d.com/")
        assert p._proxy_url == "https://staging.kiln3d.com"

    def test_custom_url_via_env(self, monkeypatch):
        monkeypatch.setenv("KILN_PROXY_URL", "https://dev.kiln3d.com")
        p = _provider(proxy_url=None)
        assert p._proxy_url == "https://dev.kiln3d.com"

    def test_param_url_overrides_env(self, monkeypatch):
        monkeypatch.setenv("KILN_PROXY_URL", "https://env.kiln3d.com")
        p = _provider(proxy_url="https://param.kiln3d.com")
        assert p._proxy_url == "https://param.kiln3d.com"

    def test_trailing_slash_stripped(self):
        p = _provider(proxy_url="https://api.kiln3d.com///")
        assert p._proxy_url == "https://api.kiln3d.com"

    def test_missing_license_key_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        # Point home to a temp dir with no .kiln/license file.
        with patch("kiln.fulfillment.proxy.Path.home", return_value=tmp_path):
            with pytest.raises(FulfillmentError, match="License key required"):
                ProxyProvider(license_key="")

    def test_license_key_from_param(self):
        p = _provider(license_key="my-key")
        assert p._license_key == "my-key"

    def test_license_key_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_LICENSE_KEY", "env-key")
        p = ProxyProvider()
        assert p._license_key == "env-key"

    def test_license_key_from_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        license_dir = tmp_path / ".kiln"
        license_dir.mkdir()
        license_file = license_dir / "license"
        license_file.write_text("  file-key  \n")
        with patch("kiln.fulfillment.proxy.Path.home", return_value=tmp_path):
            p = ProxyProvider()
        assert p._license_key == "file-key"

    def test_param_license_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("KILN_LICENSE_KEY", "env-key")
        p = _provider(license_key="param-key")
        assert p._license_key == "param-key"

    def test_session_auth_header(self):
        p = _provider(license_key="bearer-test")
        assert p._session.headers.get("Authorization") == "Bearer bearer-test"

    def test_session_accept_header(self):
        p = _provider()
        assert p._session.headers.get("Accept") == "application/json"

    def test_default_provider(self):
        p = _provider()
        assert p._provider == "craftcloud"

    def test_custom_provider(self):
        p = _provider(provider="sculpteo")
        assert p._provider == "sculpteo"

    def test_default_timeout(self):
        p = _provider()
        assert p._timeout == 60

    def test_custom_timeout(self):
        p = _provider(timeout=30)
        assert p._timeout == 30


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProxyProviderProperties:
    def test_name(self):
        assert _provider().name == "proxy"

    def test_display_name(self):
        assert _provider().display_name == "Kiln Cloud"

    def test_supported_technologies(self):
        techs = _provider().supported_technologies
        assert "FDM" in techs
        assert "SLA" in techs
        assert "SLS" in techs
        assert "MJF" in techs
        assert "DMLS" in techs

    def test_repr(self):
        p = _provider()
        r = repr(p)
        assert "ProxyProvider" in r
        assert _DEFAULT_PROXY_URL in r
        assert "craftcloud" in r


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestProxyListMaterials:
    @responses.activate
    def test_success(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json=_MATERIALS_RESPONSE,
            status=200,
        )
        p = _provider()
        materials = p.list_materials()

        assert len(materials) == 2
        assert isinstance(materials[0], Material)
        assert materials[0].id == "pla-white"
        assert materials[0].technology == "FDM"
        assert materials[1].id == "nylon-pa12"
        assert materials[1].currency == "EUR"

    @responses.activate
    def test_empty_materials_list(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"materials": []},
            status=200,
        )
        p = _provider()
        materials = p.list_materials()
        assert materials == []

    @responses.activate
    def test_non_dict_items_skipped(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"materials": ["not-a-dict", {"id": "valid", "name": "Valid"}]},
            status=200,
        )
        p = _provider()
        materials = p.list_materials()
        assert len(materials) == 1
        assert materials[0].id == "valid"

    @responses.activate
    def test_invalid_response_not_dict(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json=["not", "a", "dict"],
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="invalid response"):
            p.list_materials()

    @responses.activate
    def test_invalid_response_materials_not_list(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"materials": "not-a-list"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="missing 'materials' list"):
            p.list_materials()

    @responses.activate
    def test_http_401_auth_error(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"error": "unauthorized"},
            status=401,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="License key invalid") as exc_info:
            p.list_materials()
        assert exc_info.value.code == "AUTH_ERROR"

    @responses.activate
    def test_http_402_payment_required(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"error": "payment required"},
            status=402,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="Payment required") as exc_info:
            p.list_materials()
        assert exc_info.value.code == "PAYMENT_REQUIRED"

    @responses.activate
    def test_http_429_rate_limited(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"error": "too many requests"},
            status=429,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="Rate limit") as exc_info:
            p.list_materials()
        assert exc_info.value.code == "RATE_LIMITED"

    @responses.activate
    def test_http_500_server_error(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            body="Internal Server Error",
            status=500,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="HTTP 500") as exc_info:
            p.list_materials()
        assert exc_info.value.code == "HTTP_500"

    @responses.activate
    def test_query_param_provider(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"materials": []},
            status=200,
        )
        p = _provider(provider="sculpteo")
        p.list_materials()

        assert responses.calls[0].request.params["provider"] == "sculpteo"

    @responses.activate
    def test_material_defaults_for_missing_fields(self):
        responses.get(
            f"{_BASE}/api/fulfillment/materials",
            json={"materials": [{"id": "bare", "name": "Bare Material"}]},
            status=200,
        )
        p = _provider()
        materials = p.list_materials()
        assert len(materials) == 1
        assert materials[0].technology == ""
        assert materials[0].color == ""
        assert materials[0].finish == ""
        assert materials[0].min_wall_mm is None
        assert materials[0].price_per_cm3 is None
        assert materials[0].currency == "USD"


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------


class TestProxyGetQuote:
    @responses.activate
    def test_success(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json=_QUOTE_RESPONSE,
            status=200,
        )
        p = _provider()
        quote = p.get_quote(QuoteRequest(
            file_path=str(model),
            material_id="pla-white",
            quantity=2,
        ))

        assert quote.quote_id == "q-abc123"
        assert quote.provider == "craftcloud"
        assert quote.material == "PLA White"
        assert quote.quantity == 2
        assert quote.unit_price == 15.50
        assert quote.total_price == 31.00
        assert quote.currency == "USD"
        assert quote.lead_time_days == 7
        assert len(quote.shipping_options) == 2
        assert quote.shipping_options[0].id == "ship-std"
        assert quote.shipping_options[0].price == 5.99
        assert quote.shipping_options[1].id == "ship-exp"
        assert quote.shipping_options[1].estimated_days == 2
        assert quote.raw == _QUOTE_RESPONSE

    @responses.activate
    def test_file_upload_multipart(self, tmp_path):
        model = tmp_path / "benchy.stl"
        model.write_bytes(b"\x00binary-stl-content")

        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json=_QUOTE_RESPONSE,
            status=200,
        )
        p = _provider()
        p.get_quote(QuoteRequest(
            file_path=str(model),
            material_id="pla-white",
        ))

        req = responses.calls[0].request
        assert "multipart/form-data" in req.headers.get("Content-Type", "")

    def test_file_not_found(self):
        p = _provider()
        with pytest.raises(FileNotFoundError, match="not found"):
            p.get_quote(QuoteRequest(
                file_path="/nonexistent/path/model.stl",
                material_id="pla-white",
            ))

    def test_permission_error(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            with pytest.raises(FulfillmentError, match="Permission denied") as exc_info:
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="pla-white",
                ))
        assert exc_info.value.code == "PERMISSION_ERROR"

    @responses.activate
    def test_invalid_response_not_dict(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json=["not", "a", "dict"],
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="invalid response"):
            p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="pla-white",
            ))

    @responses.activate
    def test_quote_not_successful(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json={"success": False, "error": "Model too small"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="Model too small") as exc_info:
            p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="pla-white",
            ))
        assert exc_info.value.code == "QUOTE_ERROR"

    @responses.activate
    def test_missing_quote_object(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json={"success": True, "quote": "not-a-dict"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="missing 'quote' object"):
            p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="pla-white",
            ))

    @responses.activate
    def test_shipping_options_non_list_ignored(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        resp = {
            "success": True,
            "quote": {
                "quote_id": "q-1",
                "provider": "craftcloud",
                "material": "PLA",
                "quantity": 1,
                "unit_price": 10.0,
                "total_price": 10.0,
                "shipping_options": "not-a-list",
            },
        }
        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json=resp,
            status=200,
        )
        p = _provider()
        quote = p.get_quote(QuoteRequest(
            file_path=str(model),
            material_id="pla-white",
        ))
        assert quote.shipping_options == []

    @responses.activate
    def test_shipping_options_non_dict_items_skipped(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        resp = {
            "success": True,
            "quote": {
                "quote_id": "q-1",
                "provider": "craftcloud",
                "material": "PLA",
                "quantity": 1,
                "unit_price": 10.0,
                "total_price": 10.0,
                "shipping_options": [
                    "bad-entry",
                    {"id": "s-1", "name": "Good", "price": 5.0},
                ],
            },
        }
        responses.post(
            f"{_BASE}/api/fulfillment/quote",
            json=resp,
            status=200,
        )
        p = _provider()
        quote = p.get_quote(QuoteRequest(
            file_path=str(model),
            material_id="pla-white",
        ))
        assert len(quote.shipping_options) == 1
        assert quote.shipping_options[0].id == "s-1"


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestProxyPlaceOrder:
    @responses.activate
    def test_success(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json=_ORDER_RESPONSE,
            status=200,
        )
        p = _provider()
        result = p.place_order(OrderRequest(
            quote_id="q-abc123",
            shipping_option_id="ship-std",
        ))

        assert result.success is True
        assert result.order_id == "o-xyz789"
        assert result.status == OrderStatus.PROCESSING
        assert result.provider == "craftcloud"
        assert result.tracking_url == "https://track.example.com/o-xyz789"
        assert result.tracking_number == "1Z999"
        assert result.estimated_delivery == "2026-03-10"
        assert result.total_price == 36.99
        assert result.currency == "USD"

    @responses.activate
    def test_json_payload_sent(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json=_ORDER_RESPONSE,
            status=200,
        )
        p = _provider()
        p.place_order(OrderRequest(
            quote_id="q-abc123",
            shipping_option_id="ship-std",
            shipping_address={"city": "Austin"},
            notes="Fragile",
        ))

        import json
        body = json.loads(responses.calls[0].request.body)
        assert body["quote_id"] == "q-abc123"
        assert body["shipping_option_id"] == "ship-std"
        assert body["shipping_address"] == {"city": "Austin"}
        assert body["notes"] == "Fragile"

    def test_missing_quote_id(self):
        p = _provider()
        with pytest.raises(FulfillmentError, match="quote_id is required") as exc_info:
            p.place_order(OrderRequest(quote_id=""))
        assert exc_info.value.code == "MISSING_QUOTE_ID"

    @responses.activate
    def test_http_402_payment_required(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json={"error": "payment required"},
            status=402,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="Payment required") as exc_info:
            p.place_order(OrderRequest(quote_id="q-123"))
        assert exc_info.value.code == "PAYMENT_REQUIRED"

    @responses.activate
    def test_invalid_response_not_dict(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json=["bad"],
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="invalid response"):
            p.place_order(OrderRequest(quote_id="q-123"))

    @responses.activate
    def test_missing_order_object(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json={"order": "not-a-dict"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="missing 'order' object"):
            p.place_order(OrderRequest(quote_id="q-123"))

    @responses.activate
    def test_unknown_status_defaults_to_submitted(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order",
            json={
                "order": {
                    "order_id": "o-1",
                    "status": "some_unknown_status",
                    "provider": "craftcloud",
                },
            },
            status=200,
        )
        p = _provider()
        result = p.place_order(OrderRequest(quote_id="q-123"))
        assert result.status == OrderStatus.SUBMITTED


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestProxyOrderStatus:
    @responses.activate
    def test_success(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-xyz789/status",
            json=_ORDER_STATUS_RESPONSE,
            status=200,
        )
        p = _provider()
        result = p.get_order_status("o-xyz789")

        assert result.success is True
        assert result.order_id == "o-xyz789"
        assert result.status == OrderStatus.SHIPPING
        assert result.tracking_url == "https://track.example.com/o-xyz789"
        assert result.tracking_number == "1Z999"

    @responses.activate
    def test_http_404_not_found(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-missing/status",
            body="Not Found",
            status=404,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="HTTP 404") as exc_info:
            p.get_order_status("o-missing")
        assert exc_info.value.code == "HTTP_404"

    @responses.activate
    def test_order_id_defaults_to_param(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-param/status",
            json={"order": {"status": "submitted", "provider": "craftcloud"}},
            status=200,
        )
        p = _provider()
        result = p.get_order_status("o-param")
        assert result.order_id == "o-param"

    @responses.activate
    def test_unknown_status_defaults_to_submitted(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-1/status",
            json={
                "order": {
                    "order_id": "o-1",
                    "status": "fabricating_widgets",
                    "provider": "craftcloud",
                },
            },
            status=200,
        )
        p = _provider()
        result = p.get_order_status("o-1")
        assert result.status == OrderStatus.SUBMITTED

    @responses.activate
    def test_invalid_response_not_dict(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-1/status",
            json=["bad"],
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="invalid response"):
            p.get_order_status("o-1")

    @responses.activate
    def test_missing_order_object(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-1/status",
            json={"order": "not-a-dict"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="missing 'order' object"):
            p.get_order_status("o-1")


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestProxyCancelOrder:
    @responses.activate
    def test_success(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-xyz789/cancel",
            json=_CANCEL_RESPONSE,
            status=200,
        )
        p = _provider()
        result = p.cancel_order("o-xyz789")

        assert result.success is True
        assert result.order_id == "o-xyz789"
        assert result.status == OrderStatus.CANCELLED
        assert result.provider == "craftcloud"

    @responses.activate
    def test_http_error(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-locked/cancel",
            body="Conflict",
            status=409,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="HTTP 409") as exc_info:
            p.cancel_order("o-locked")
        assert exc_info.value.code == "HTTP_409"

    @responses.activate
    def test_invalid_response_not_dict(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-1/cancel",
            json=["bad"],
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="invalid response"):
            p.cancel_order("o-1")

    @responses.activate
    def test_missing_order_object(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-1/cancel",
            json={"order": "not-a-dict"},
            status=200,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="missing 'order' object"):
            p.cancel_order("o-1")

    @responses.activate
    def test_unknown_status_defaults_to_cancelled(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-1/cancel",
            json={
                "order": {
                    "order_id": "o-1",
                    "status": "mystery_status",
                    "provider": "craftcloud",
                },
            },
            status=200,
        )
        p = _provider()
        result = p.cancel_order("o-1")
        assert result.status == OrderStatus.CANCELLED

    @responses.activate
    def test_order_id_defaults_to_param(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-param/cancel",
            json={"order": {"status": "cancelled", "provider": "craftcloud"}},
            status=200,
        )
        p = _provider()
        result = p.cancel_order("o-param")
        assert result.order_id == "o-param"


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class TestProxyHTTPLayer:
    def test_timeout(self):
        p = _provider()
        from requests.exceptions import Timeout

        with patch.object(p._session, "request", side_effect=Timeout()):
            with pytest.raises(FulfillmentError, match="timed out") as exc_info:
                p.list_materials()
        assert exc_info.value.code == "TIMEOUT"

    def test_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE

        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(FulfillmentError, match="Could not connect") as exc_info:
                p.list_materials()
        assert exc_info.value.code == "CONNECTION_ERROR"

    def test_generic_request_error(self):
        p = _provider()
        from requests.exceptions import RequestException

        with patch.object(p._session, "request", side_effect=RequestException("oops")):
            with pytest.raises(FulfillmentError, match="Request error") as exc_info:
                p.list_materials()
        assert exc_info.value.code == "REQUEST_ERROR"

    @responses.activate
    def test_ok_response_with_invalid_json_falls_back(self):
        responses.post(
            f"{_BASE}/api/fulfillment/order/o-1/cancel",
            body="not json",
            status=200,
        )
        p = _provider()
        # _request returns {"status": "ok"} when response.json() raises ValueError.
        # cancel_order then gets order_raw={} (empty dict) from data.get("order", {})
        # and builds an OrderResult with defaults.
        result = p.cancel_order("o-1")
        assert result.order_id == "o-1"
        assert result.status == OrderStatus.CANCELLED

    def test_fulfillment_error_reraise(self):
        p = _provider()
        # FulfillmentError raised inside _request should propagate directly.
        with patch.object(
            p._session,
            "request",
            side_effect=FulfillmentError("custom error", code="CUSTOM"),
        ):
            with pytest.raises(FulfillmentError, match="custom error") as exc_info:
                p.list_materials()
        assert exc_info.value.code == "CUSTOM"

    @responses.activate
    def test_http_401_auth_error(self):
        responses.get(
            f"{_BASE}/api/fulfillment/order/o-1/status",
            json={"error": "unauthorized"},
            status=401,
        )
        p = _provider()
        with pytest.raises(FulfillmentError, match="License key invalid") as exc_info:
            p.get_order_status("o-1")
        assert exc_info.value.code == "AUTH_ERROR"
