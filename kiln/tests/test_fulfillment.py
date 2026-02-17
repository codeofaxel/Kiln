"""Tests for kiln.fulfillment — external manufacturing service adapters."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, mock_open

import pytest
import requests

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
from kiln.fulfillment.craftcloud import CraftcloudProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: Optional[Any] = None,
    ok: bool = True,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.text = json.dumps(json_data) if json_data else ""
    resp.json.return_value = json_data or {}
    return resp


def _provider(**kwargs) -> CraftcloudProvider:
    """Create a CraftcloudProvider with sensible defaults."""
    defaults = {"api_key": "test-key"}
    defaults.update(kwargs)
    return CraftcloudProvider(**defaults)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_material_to_dict(self):
        m = Material(id="pla-white", name="PLA White", technology="FDM", color="white")
        d = m.to_dict()
        assert d["id"] == "pla-white"
        assert d["technology"] == "FDM"

    def test_shipping_option_to_dict(self):
        s = ShippingOption(id="std", name="Standard", price=5.99, estimated_days=7)
        d = s.to_dict()
        assert d["price"] == 5.99
        assert d["estimated_days"] == 7

    def test_quote_to_dict_excludes_raw(self):
        q = Quote(
            quote_id="q-123",
            provider="craftcloud",
            material="PLA",
            quantity=1,
            unit_price=10.0,
            total_price=10.0,
            raw={"internal": "data"},
        )
        d = q.to_dict()
        assert "raw" not in d
        assert d["quote_id"] == "q-123"

    def test_quote_request_to_dict(self):
        qr = QuoteRequest(file_path="/tmp/model.stl", material_id="pla-white")
        d = qr.to_dict()
        assert d["file_path"] == "/tmp/model.stl"
        assert d["quantity"] == 1

    def test_order_request_to_dict(self):
        o = OrderRequest(quote_id="q-123", shipping_option_id="std")
        d = o.to_dict()
        assert d["quote_id"] == "q-123"

    def test_order_result_to_dict(self):
        o = OrderResult(
            success=True,
            order_id="o-456",
            status=OrderStatus.PROCESSING,
            provider="craftcloud",
        )
        d = o.to_dict()
        assert d["status"] == "processing"
        assert d["order_id"] == "o-456"

    def test_order_status_values(self):
        assert OrderStatus.QUOTING.value == "quoting"
        assert OrderStatus.DELIVERED.value == "delivered"
        assert OrderStatus.CANCELLED.value == "cancelled"


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_no_api_key_raises(self):
        with pytest.raises(ValueError, match="API key required"):
            CraftcloudProvider(api_key="")

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "env-key")
        p = CraftcloudProvider()
        assert p._api_key == "env-key"

    def test_custom_base_url(self):
        p = _provider(base_url="https://custom.api.com/v2/")
        assert p._base_url == "https://custom.api.com/v2"

    def test_api_key_in_session(self):
        p = _provider()
        assert "Bearer test-key" in p._session.headers.get("Authorization", "")


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_name(self):
        assert _provider().name == "craftcloud"

    def test_display_name(self):
        assert _provider().display_name == "Craftcloud by All3DP"

    def test_supported_technologies(self):
        techs = _provider().supported_technologies
        assert "FDM" in techs
        assert "SLA" in techs
        assert "SLS" in techs

    def test_repr(self):
        p = _provider()
        assert "CraftcloudProvider" in repr(p)


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestListMaterials:
    def test_list_materials_material_catalog_material_config_id(self):
        p = _provider()
        resp = _mock_response(json_data=[
            {
                "materialConfigId": "mc-pla-white",
                "displayName": "PLA White",
                "technology": "FDM",
                "color": "white",
                "pricePerCm3": 0.15,
            },
        ])
        with patch.object(p._session, "request", return_value=resp) as mock_req:
            materials = p.list_materials()

        assert len(materials) == 1
        assert materials[0].id == "mc-pla-white"
        assert materials[0].name == "PLA White"
        assert materials[0].technology == "FDM"
        first_url = mock_req.call_args_list[0].args[1]
        assert "material-catalog" in first_url

    def test_list_materials(self):
        p = _provider()
        resp = _mock_response(json_data={
            "materials": [
                {"id": "pla-white", "name": "PLA White", "technology": "FDM",
                 "color": "white", "price_per_cm3": 0.15},
                {"id": "resin-grey", "name": "Resin Grey", "technology": "SLA",
                 "color": "grey"},
            ],
        })
        with patch.object(p._session, "request", return_value=resp):
            materials = p.list_materials()

        assert len(materials) == 2
        assert materials[0].id == "pla-white"
        assert materials[0].technology == "FDM"
        assert materials[1].name == "Resin Grey"

    def test_list_materials_fallback_legacy_when_catalog_fails(self):
        p = _provider()
        catalog_error = _mock_response(status_code=500, ok=False, json_data={"error": "down"})
        legacy_resp = _mock_response(json_data={
            "materials": [
                {"id": "legacy-pla", "name": "Legacy PLA", "technology": "FDM"},
            ],
        })
        with patch.object(p._session, "request", side_effect=[catalog_error, legacy_resp]):
            materials = p.list_materials()

        assert len(materials) == 1
        assert materials[0].id == "legacy-pla"

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


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------


class TestGetQuote:
    def test_get_quote_uses_material_config_id_payload(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={"uploadId": "u-789"})
        quote_resp = _mock_response(json_data={
            "quoteId": "q-123",
            "materialName": "PLA White",
            "quantity": 2,
            "unitPrice": 5.50,
            "totalPrice": 11.00,
        })
        with patch.object(p._session, "request", side_effect=[upload_resp, quote_resp]) as mock_req:
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="mc-pla-white",
                quantity=2,
            ))

        quote_call = mock_req.call_args_list[1]
        payload = quote_call.kwargs.get("json") or quote_call[1].get("json")
        assert payload["materialConfigId"] == "mc-pla-white"
        assert payload["shippingCountry"] == "US"
        assert quote.quote_id == "q-123"

    def test_get_quote_fallback_to_legacy_payload(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={"upload_id": "u-789"})
        bad_resp = _mock_response(status_code=400, ok=False, json_data={"error": "bad payload"})
        good_resp = _mock_response(json_data={
            "quote_id": "q-legacy",
            "material": "PLA White",
            "quantity": 1,
            "unit_price": 6.0,
            "total_price": 6.0,
        })
        with patch.object(
            p._session,
            "request",
            side_effect=[upload_resp, bad_resp, good_resp],
        ) as mock_req:
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="mc-pla-white",
            ))

        primary_payload = mock_req.call_args_list[1].kwargs.get("json")
        fallback_payload = mock_req.call_args_list[2].kwargs.get("json")
        assert "materialConfigId" in primary_payload
        assert "material_id" in fallback_payload
        assert quote.quote_id == "q-legacy"

    def test_get_quote_success(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={"upload_id": "u-789"})
        quote_resp = _mock_response(json_data={
            "quote_id": "q-123",
            "material": "PLA White",
            "quantity": 2,
            "unit_price": 5.50,
            "total_price": 11.00,
            "lead_time_days": 5,
            "shipping_options": [
                {"id": "std", "name": "Standard", "price": 4.99, "estimated_days": 7},
                {"id": "exp", "name": "Express", "price": 12.99, "estimated_days": 2},
            ],
        })
        with patch.object(p._session, "request", side_effect=[upload_resp, quote_resp]):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="pla-white",
                quantity=2,
            ))

        assert quote.quote_id == "q-123"
        assert quote.total_price == 11.00
        assert quote.lead_time_days == 5
        assert len(quote.shipping_options) == 2
        assert quote.shipping_options[0].name == "Standard"

    def test_get_quote_file_not_found(self):
        p = _provider()
        with pytest.raises(FileNotFoundError, match="not found"):
            p.get_quote(QuoteRequest(
                file_path="/nonexistent/model.stl",
                material_id="pla",
            ))

    def test_get_quote_upload_no_id(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data={})
        with patch.object(p._session, "request", return_value=upload_resp):
            with pytest.raises(FulfillmentError, match="upload ID"):
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


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_place_order_success(self):
        p = _provider()
        resp = _mock_response(json_data={
            "order_id": "o-456",
            "status": "confirmed",
            "tracking_url": "https://track.me/o-456",
            "total_price": 15.99,
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.place_order(OrderRequest(
                quote_id="q-123",
                shipping_option_id="std",
            ))

        assert result.success is True
        assert result.order_id == "o-456"
        assert result.status == OrderStatus.PROCESSING
        assert result.tracking_url == "https://track.me/o-456"

    def test_place_order_with_address(self):
        p = _provider()
        resp = _mock_response(json_data={
            "order_id": "o-789",
            "status": "submitted",
        })
        with patch.object(p._session, "request", return_value=resp) as mock_req:
            result = p.place_order(OrderRequest(
                quote_id="q-123",
                shipping_option_id="exp",
                shipping_address={"street": "123 Main St", "city": "NYC"},
            ))

        assert result.success is True
        # Verify the payload included shipping_address
        call_kwargs = mock_req.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        shipping = payload.get("shippingAddress") or payload.get("shipping_address")
        assert shipping["city"] == "NYC"


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    def test_get_order_status(self):
        p = _provider()
        resp = _mock_response(json_data={
            "status": "shipped",
            "tracking_number": "1Z999AA10123456784",
            "tracking_url": "https://ups.com/track/1Z999",
            "estimated_delivery": "2026-02-15",
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.SHIPPING
        assert result.tracking_number == "1Z999AA10123456784"
        assert result.estimated_delivery == "2026-02-15"

    def test_unknown_status_defaults(self):
        p = _provider()
        resp = _mock_response(json_data={"status": "something_new"})
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.SUBMITTED


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_order(self):
        p = _provider()
        resp = _mock_response(json_data={"status": "cancelled"})
        with patch.object(p._session, "request", return_value=resp):
            result = p.cancel_order("o-456")

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

    def test_fulfillment_error_has_code(self):
        e = FulfillmentError("test error", code="TEST_CODE")
        assert e.code == "TEST_CODE"
        assert str(e) == "test error"


# ---------------------------------------------------------------------------
# Abstract base verification
# ---------------------------------------------------------------------------


class TestAbstractBase:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            FulfillmentProvider()
