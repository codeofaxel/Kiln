"""Tests for kiln.fulfillment.sculpteo — Sculpteo fulfillment adapter."""

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
from kiln.fulfillment.sculpteo import SculpteoProvider


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


def _provider(**kwargs) -> SculpteoProvider:
    """Create a SculpteoProvider with sensible defaults."""
    defaults = {"api_key": "test-key"}
    defaults.update(kwargs)
    return SculpteoProvider(**defaults)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_api_key_raises(self):
        with pytest.raises(ValueError, match="API key required"):
            SculpteoProvider(api_key="")

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_SCULPTEO_API_KEY", "env-key")
        p = SculpteoProvider()
        assert p._api_key == "env-key"

    def test_custom_base_url(self):
        p = _provider(base_url="https://custom.sculpteo.com/api/")
        assert p._base_url == "https://custom.sculpteo.com/api"

    def test_api_key_in_session(self):
        p = _provider()
        assert "Bearer test-key" in p._session.headers.get("Authorization", "")


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _provider().name == "sculpteo"

    def test_display_name(self):
        assert _provider().display_name == "Sculpteo"

    def test_supported_technologies(self):
        techs = _provider().supported_technologies
        assert "FDM" in techs
        assert "SLA" in techs
        assert "SLS" in techs
        assert "CNC" in techs

    def test_repr(self):
        p = _provider()
        assert "SculpteoProvider" in repr(p)


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestListMaterials:
    def test_list_materials(self):
        p = _provider()
        resp = _mock_response(json_data={
            "materials": [
                {"id": "white-plastic", "name": "White Plastic", "technology": "SLS",
                 "color": "white", "price_per_cm3": 0.21, "currency": "EUR"},
                {"id": "alumide", "name": "Alumide", "technology": "SLS",
                 "color": "grey"},
            ],
        })
        with patch.object(p._session, "request", return_value=resp):
            materials = p.list_materials()

        assert len(materials) == 2
        assert materials[0].id == "white-plastic"
        assert materials[0].technology == "SLS"
        assert materials[0].currency == "EUR"
        assert materials[1].name == "Alumide"

    def test_list_materials_empty(self):
        p = _provider()
        resp = _mock_response(json_data={"materials": []})
        with patch.object(p._session, "request", return_value=resp):
            materials = p.list_materials()
        assert materials == []

    def test_list_materials_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(FulfillmentError, match="Could not connect"):
                p.list_materials()

    def test_list_materials_uuid_key(self):
        """Sculpteo may use 'uuid' instead of 'id'."""
        p = _provider()
        resp = _mock_response(json_data={
            "materials": [
                {"uuid": "abc-123", "title": "Resin HD", "technology": "SLA"},
            ],
        })
        with patch.object(p._session, "request", return_value=resp):
            materials = p.list_materials()
        assert materials[0].id == "abc-123"
        assert materials[0].name == "Resin HD"


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------


class TestGetQuote:
    def test_get_quote_success(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={"uuid": "d-uuid-123"})
        price_resp = _mock_response(json_data={
            "unit_price": 12.50,
            "total_price": 25.00,
            "material_name": "White Plastic",
            "currency": "EUR",
            "lead_time_days": 7,
            "shipping_options": [
                {"id": "std", "name": "Standard", "price": 6.99, "currency": "EUR", "estimated_days": 10},
            ],
        })
        with patch.object(p._session, "request", side_effect=[upload_resp, price_resp]):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="white-plastic",
                quantity=2,
            ))

        assert quote.quote_id == "sc-d-uuid-123-white-plastic"
        assert quote.unit_price == 12.50
        assert quote.total_price == 25.00
        assert quote.currency == "EUR"
        assert quote.lead_time_days == 7
        assert len(quote.shipping_options) == 1
        assert quote.shipping_options[0].name == "Standard"

    def test_get_quote_file_not_found(self):
        p = _provider()
        with pytest.raises(FileNotFoundError, match="not found"):
            p.get_quote(QuoteRequest(
                file_path="/nonexistent/model.stl",
                material_id="pla",
            ))

    def test_get_quote_no_uuid(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={})
        with patch.object(p._session, "request", return_value=upload_resp):
            with pytest.raises(FulfillmentError, match="design UUID"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="pla",
                ))

    def test_get_quote_api_error(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        resp = _mock_response(status_code=400, ok=False, json_data={"error": "bad"})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(FulfillmentError, match="HTTP 400"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="pla",
                ))

    def test_get_quote_price_fallback(self, tmp_path):
        """When 'unit_price' not present, falls back to 'price'."""
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={"uuid": "d-uuid-456"})
        price_resp = _mock_response(json_data={
            "price": 8.00,
            "currency": "EUR",
        })
        with patch.object(p._session, "request", side_effect=[upload_resp, price_resp]):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="resin",
                quantity=3,
            ))
        assert quote.unit_price == 8.00
        assert quote.total_price == 24.00  # fallback: unit * quantity


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_place_order_success(self):
        p = _provider()
        resp = _mock_response(json_data={
            "order_id": "SCO-789",
            "status": "confirmed",
            "total_price": 31.99,
            "currency": "EUR",
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.place_order(OrderRequest(
                quote_id="sc-d-uuid-123-white-plastic",
            ))

        assert result.success is True
        assert result.order_id == "SCO-789"
        assert result.status == OrderStatus.PROCESSING
        assert result.currency == "EUR"

    def test_place_order_invalid_quote_id(self):
        p = _provider()
        with pytest.raises(FulfillmentError, match="Invalid Sculpteo quote ID"):
            p.place_order(OrderRequest(quote_id="invalid"))

    def test_place_order_with_address(self):
        p = _provider()
        resp = _mock_response(json_data={
            "order_ref": "SCO-456",
            "status": "submitted",
        })
        with patch.object(p._session, "request", return_value=resp) as mock_req:
            p.place_order(OrderRequest(
                quote_id="sc-uuid1-material1",
                shipping_option_id="express",
                shipping_address={"street": "456 Rue", "city": "Paris"},
            ))

        call_kwargs = mock_req.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["shipping_option"] == "express"
        assert payload["shipping_address"]["city"] == "Paris"

    def test_place_order_uses_order_ref(self):
        """Sculpteo may return 'order_ref' instead of 'order_id'."""
        p = _provider()
        resp = _mock_response(json_data={
            "order_ref": "REF-999",
            "status": "pending",
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.place_order(OrderRequest(
                quote_id="sc-uuid1-mat1",
            ))
        assert result.order_id == "REF-999"


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    def test_get_order_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "status": "shipped",
            "tracking_number": "LP123456789FR",
            "tracking_url": "https://laposte.fr/track/LP123",
            "estimated_delivery": "2026-02-25",
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("SCO-789")

        assert result.status == OrderStatus.SHIPPING
        assert result.tracking_number == "LP123456789FR"

    def test_unknown_status_defaults(self):
        p = _provider()
        resp = _mock_response(json_data={"status": "something_new"})
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("SCO-789")
        assert result.status == OrderStatus.SUBMITTED

    @pytest.mark.parametrize("api_status,expected", [
        ("production", OrderStatus.PRINTING),
        ("in_production", OrderStatus.PRINTING),
        ("shipped", OrderStatus.SHIPPING),
        ("delivered", OrderStatus.DELIVERED),
        ("cancelled", OrderStatus.CANCELLED),
        ("canceled", OrderStatus.CANCELLED),
    ])
    def test_status_mapping(self, api_status, expected):
        p = _provider()
        resp = _mock_response(json_data={"status": api_status})
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("SCO-789")
        assert result.status == expected


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_order(self):
        p = _provider()
        resp = _mock_response(json_data={"status": "cancelled"})
        with patch.object(p._session, "request", return_value=resp):
            result = p.cancel_order("SCO-789")

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHTTPErrors:
    def test_timeout(self):
        p = _provider()
        with patch.object(p._session, "request", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(FulfillmentError, match="timed out"):
                p.list_materials()

    def test_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(FulfillmentError, match="Could not connect"):
                p.list_materials()

    def test_http_error_code(self):
        p = _provider()
        resp = _mock_response(status_code=403, ok=False)
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(FulfillmentError, match="HTTP 403"):
                p.list_materials()
