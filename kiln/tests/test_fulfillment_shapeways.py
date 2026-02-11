"""Tests for kiln.fulfillment.shapeways — Shapeways fulfillment adapter."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests

from kiln.fulfillment.base import (
    FulfillmentError,
    OrderRequest,
    OrderResult,
    OrderStatus,
    QuoteRequest,
)
from kiln.fulfillment.shapeways import ShapewaysProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    ok: bool = True,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.text = json.dumps(json_data) if json_data else ""
    resp.json.return_value = json_data or {}
    return resp


def _token_response() -> MagicMock:
    """Create a mock OAuth2 token response."""
    return _mock_response(json_data={
        "access_token": "test-token-123",
        "expires_in": 3600,
        "token_type": "Bearer",
    })


def _provider(**kwargs) -> ShapewaysProvider:
    """Create a ShapewaysProvider with sensible defaults."""
    defaults = {
        "client_id": "test-id",
        "client_secret": "test-secret",
    }
    defaults.update(kwargs)
    return ShapewaysProvider(**defaults)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_credentials_raises(self):
        with pytest.raises(ValueError, match="credentials required"):
            ShapewaysProvider(client_id="", client_secret="")

    def test_missing_secret_raises(self):
        with pytest.raises(ValueError, match="credentials required"):
            ShapewaysProvider(client_id="id", client_secret="")

    def test_missing_id_raises(self):
        with pytest.raises(ValueError, match="credentials required"):
            ShapewaysProvider(client_id="", client_secret="secret")

    def test_env_var_credentials(self, monkeypatch):
        monkeypatch.setenv("KILN_SHAPEWAYS_CLIENT_ID", "env-id")
        monkeypatch.setenv("KILN_SHAPEWAYS_CLIENT_SECRET", "env-secret")
        p = ShapewaysProvider()
        assert p._client_id == "env-id"
        assert p._client_secret == "env-secret"

    def test_custom_base_url(self):
        p = _provider(base_url="https://custom.api.com/v2/")
        assert p._base_url == "https://custom.api.com/v2"


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _provider().name == "shapeways"

    def test_display_name(self):
        assert _provider().display_name == "Shapeways"

    def test_supported_technologies(self):
        techs = _provider().supported_technologies
        assert "FDM" in techs
        assert "SLA" in techs
        assert "SLS" in techs
        assert "Metal" in techs

    def test_repr(self):
        p = _provider()
        assert "ShapewaysProvider" in repr(p)


# ---------------------------------------------------------------------------
# OAuth2 token
# ---------------------------------------------------------------------------


class TestOAuth2:
    def test_token_obtained_on_first_request(self):
        p = _provider()
        token_resp = _token_response()
        materials_resp = _mock_response(json_data={"materials": {}})
        with patch.object(p._session, "request", side_effect=[token_resp, materials_resp]) as mock_req:
            p.list_materials()

        # First call should be token request
        first_call = mock_req.call_args_list[0]
        assert first_call[0][0] == "POST"
        assert "oauth2/token" in first_call[0][1]

    def test_token_reused_when_not_expired(self):
        p = _provider()
        token_resp = _token_response()
        resp1 = _mock_response(json_data={"materials": {}})
        resp2 = _mock_response(json_data={"materials": {}})
        with patch.object(p._session, "request", side_effect=[token_resp, resp1, resp2]) as mock_req:
            p.list_materials()
            p.list_materials()

        # Only one token request, two material requests
        assert mock_req.call_count == 3

    def test_token_failure_raises(self):
        p = _provider()
        token_resp = _mock_response(status_code=401, ok=False)
        with patch.object(p._session, "request", return_value=token_resp):
            with pytest.raises(FulfillmentError, match="OAuth2 token request failed"):
                p.list_materials()

    def test_token_missing_access_token(self):
        p = _provider()
        token_resp = _mock_response(json_data={"expires_in": 3600})
        with patch.object(p._session, "request", return_value=token_resp):
            with pytest.raises(FulfillmentError, match="missing access_token"):
                p.list_materials()


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestListMaterials:
    def test_list_materials_dict_format(self):
        p = _provider()
        token_resp = _token_response()
        materials_resp = _mock_response(json_data={
            "materials": {
                "6": {"materialId": 6, "title": "White Nylon", "printerId": "SLS",
                      "color": "white", "minimumWallThickness": 0.7},
                "25": {"materialId": 25, "title": "Stainless Steel", "printerId": "DMLS",
                       "color": "silver"},
            },
        })
        with patch.object(p._session, "request", side_effect=[token_resp, materials_resp]):
            materials = p.list_materials()

        assert len(materials) == 2
        names = {m.name for m in materials}
        assert "White Nylon" in names
        assert "Stainless Steel" in names

    def test_list_materials_empty(self):
        p = _provider()
        token_resp = _token_response()
        resp = _mock_response(json_data={"materials": {}})
        with patch.object(p._session, "request", side_effect=[token_resp, resp]):
            materials = p.list_materials()
        assert materials == []

    def test_list_materials_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(FulfillmentError, match="Failed to obtain"):
                p.list_materials()


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------


class TestGetQuote:
    def test_get_quote_success(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        token_resp = _token_response()
        upload_resp = _mock_response(json_data={
            "modelId": 12345,
            "materials": {
                "6": {"price": 25.50, "title": "White Nylon", "currency": "USD"},
            },
        })
        with patch.object(p._session, "request", side_effect=[token_resp, upload_resp]):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="6",
                quantity=2,
            ))

        assert quote.quote_id == "sw-12345-6"
        assert quote.unit_price == 25.50
        assert quote.total_price == 51.00
        assert quote.provider == "shapeways"
        assert quote.material == "White Nylon"

    def test_get_quote_file_not_found(self):
        p = _provider()
        with pytest.raises(FileNotFoundError, match="not found"):
            p.get_quote(QuoteRequest(
                file_path="/nonexistent/model.stl",
                material_id="6",
            ))

    def test_get_quote_no_model_id(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        token_resp = _token_response()
        upload_resp = _mock_response(json_data={})
        with patch.object(p._session, "request", side_effect=[token_resp, upload_resp]):
            with pytest.raises(FulfillmentError, match="model ID"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="6",
                ))

    def test_get_quote_api_error(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        token_resp = _token_response()
        err_resp = _mock_response(status_code=400, ok=False, json_data={"error": "bad"})
        with patch.object(p._session, "request", side_effect=[token_resp, err_resp]):
            with pytest.raises(FulfillmentError, match="HTTP 400"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="6",
                ))

    def test_get_quote_material_not_in_response(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        token_resp = _token_response()
        upload_resp = _mock_response(json_data={
            "modelId": 12345,
            "materials": {"99": {"price": 10.0}},
        })
        with patch.object(p._session, "request", side_effect=[token_resp, upload_resp]):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="6",
                quantity=1,
            ))
        # Material not found → price defaults to 0
        assert quote.unit_price == 0
        assert quote.total_price == 0


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_place_order_success(self):
        p = _provider()
        token_resp = _token_response()
        order_resp = _mock_response(json_data={
            "orderId": 67890,
            "status": "placed",
            "totalPrice": 25.50,
        })
        with patch.object(p._session, "request", side_effect=[token_resp, order_resp]):
            result = p.place_order(OrderRequest(
                quote_id="sw-12345-6",
            ))

        assert result.success is True
        assert result.order_id == "67890"
        assert result.status == OrderStatus.SUBMITTED
        assert result.provider == "shapeways"

    def test_place_order_invalid_quote_id(self):
        p = _provider()
        with pytest.raises(FulfillmentError, match="Invalid Shapeways quote ID"):
            p.place_order(OrderRequest(quote_id="invalid"))

    def test_place_order_with_shipping(self):
        p = _provider()
        token_resp = _token_response()
        order_resp = _mock_response(json_data={
            "orderId": 67890,
            "status": "placed",
        })
        with patch.object(p._session, "request", side_effect=[token_resp, order_resp]) as mock_req:
            p.place_order(OrderRequest(
                quote_id="sw-12345-6",
                shipping_option_id="express",
                shipping_address={"street": "123 Main", "city": "NYC"},
            ))

        order_call = mock_req.call_args_list[1]
        payload = order_call.kwargs.get("json") or order_call[1].get("json")
        assert payload["shippingOption"] == "express"
        assert payload["shippingAddress"]["city"] == "NYC"


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    def test_get_order_status(self):
        p = _provider()
        token_resp = _token_response()
        status_resp = _mock_response(json_data={
            "status": "shipped",
            "trackingNumber": "1Z999AA10123456784",
            "trackingUrl": "https://ups.com/track/1Z999",
            "estimatedDelivery": "2026-02-20",
        })
        with patch.object(p._session, "request", side_effect=[token_resp, status_resp]):
            result = p.get_order_status("67890")

        assert result.status == OrderStatus.SHIPPING
        assert result.tracking_number == "1Z999AA10123456784"
        assert result.estimated_delivery == "2026-02-20"

    def test_unknown_status_defaults(self):
        p = _provider()
        token_resp = _token_response()
        resp = _mock_response(json_data={"status": "something_new"})
        with patch.object(p._session, "request", side_effect=[token_resp, resp]):
            result = p.get_order_status("67890")
        assert result.status == OrderStatus.SUBMITTED

    @pytest.mark.parametrize("api_status,expected", [
        ("in_production", OrderStatus.PRINTING),
        ("shipped", OrderStatus.SHIPPING),
        ("delivered", OrderStatus.DELIVERED),
        ("cancelled", OrderStatus.CANCELLED),
        ("canceled", OrderStatus.CANCELLED),
    ])
    def test_status_mapping(self, api_status, expected):
        p = _provider()
        token_resp = _token_response()
        resp = _mock_response(json_data={"status": api_status})
        with patch.object(p._session, "request", side_effect=[token_resp, resp]):
            result = p.get_order_status("67890")
        assert result.status == expected


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_order(self):
        p = _provider()
        token_resp = _token_response()
        resp = _mock_response(json_data={"status": "cancelled"})
        with patch.object(p._session, "request", side_effect=[token_resp, resp]):
            result = p.cancel_order("67890")

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHTTPErrors:
    def test_timeout(self):
        p = _provider()
        # First call is token, which times out
        with patch.object(p._session, "request", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(FulfillmentError, match="Failed to obtain"):
                p.list_materials()

    def test_timeout_after_auth(self):
        p = _provider()
        token_resp = _token_response()
        with patch.object(p._session, "request", side_effect=[token_resp, requests.exceptions.Timeout()]):
            with pytest.raises(FulfillmentError, match="timed out"):
                p.list_materials()

    def test_connection_error_after_auth(self):
        p = _provider()
        token_resp = _token_response()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=[token_resp, CE("refused")]):
            with pytest.raises(FulfillmentError, match="Could not connect"):
                p.list_materials()

    def test_http_error_code(self):
        p = _provider()
        token_resp = _token_response()
        resp = _mock_response(status_code=403, ok=False)
        with patch.object(p._session, "request", side_effect=[token_resp, resp]):
            with pytest.raises(FulfillmentError, match="HTTP 403"):
                p.list_materials()
