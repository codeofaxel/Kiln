"""Tests for kiln.fulfillment — external manufacturing service adapters.

Covers:
- Dataclass serialization (Material, Quote, OrderRequest, OrderResult)
- CraftcloudProvider constructor and env var handling
- Properties (name, display_name, supported_technologies, repr)
- Material catalog parsing (materialStructure → Material list)
- v5 model upload + polling (POST /v5/model, GET /v5/model/{id})
- v5 price request + polling (POST /v5/price, GET /v5/price/{id})
- v5 cart creation (POST /v5/cart)
- v5 order placement (POST /v5/order)
- v5 order status (GET /v5/order/{id}/status)
- v5 cancel order (PATCH /v5/order/{id}/status)
- Shipping address mapping (flat dict → nested camelCase user object)
- HTTP error handling (timeout, connection error, 4xx/5xx)
- Abstract base class verification
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

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
    json_data: Any | None = None,
    ok: bool = True,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = ok
    resp.text = json.dumps(json_data) if json_data is not None else ""
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _provider(**kwargs) -> CraftcloudProvider:
    """Create a CraftcloudProvider with sensible defaults."""
    defaults: dict[str, Any] = {
        "api_key": "test-key",
        "poll_interval": 0.0,
        "max_poll_attempts": 3,
    }
    defaults.update(kwargs)
    return CraftcloudProvider(**defaults)


# Sample material catalog payload (matches real API structure).
_CATALOG_PAYLOAD: dict[str, Any] = {
    "materialStructure": [
        {
            "id": "group-1",
            "name": "Nylon",
            "materials": [
                {
                    "id": "mat-1",
                    "name": "PA 12",
                    "technology": "SLS",
                    "finishGroups": [
                        {
                            "id": "fg-1",
                            "name": "Standard",
                            "materialConfigs": [
                                {
                                    "id": "6c633df0-aca1-5b95-aaab-5c19b4e0d24f",
                                    "name": "SLS Nylon PA12 Standard (Solid White)",
                                    "color": "White",
                                    "colorCode": "#FFFFFF",
                                },
                                {
                                    "id": "a97a2a21-0e71-51d1-b642-93b168660053",
                                    "name": "SLS Nylon PA12 Standard (Black)",
                                    "color": "Black",
                                    "colorCode": "#000000",
                                },
                            ],
                        },
                        {
                            "id": "fg-2",
                            "name": "Polished",
                            "materialConfigs": [
                                {
                                    "id": "bbbb0000-1111-2222-3333-444444444444",
                                    "name": "SLS Nylon PA12 Polished (White)",
                                    "color": "White",
                                },
                            ],
                        },
                    ],
                },
            ],
            "printingMethods": [
                {"name": "SLS", "minWallThickness": 0.8, "minDetails": 0.5},
            ],
        },
        {
            "id": "group-2",
            "name": "Resin",
            "materials": [
                {
                    "id": "mat-2",
                    "name": "Standard Resin",
                    "technology": "SLA",
                    "finishGroups": [
                        {
                            "id": "fg-3",
                            "name": "Raw",
                            "materialConfigs": [
                                {
                                    "id": "cccc0000-1111-2222-3333-444444444444",
                                    "name": "SLA Standard Resin Raw (Grey)",
                                    "color": "Grey",
                                },
                            ],
                        },
                    ],
                },
            ],
            "printingMethods": [
                {"name": "SLA", "minWallThickness": 0.5},
            ],
        },
    ],
}

# Sample v5 price response.
_PRICE_RESPONSE: dict[str, Any] = {
    "expiresAt": 1658972807453,
    "allComplete": True,
    "quotes": [
        {
            "quoteId": "bf2b604ae33685f",
            "vendorId": "wenext",
            "modelId": "81dee7e88f5d780",
            "materialConfigId": "a97a2a21-0e71-51d1-b642-93b168660053",
            "quantity": 1,
            "price": 72.03,
            "priceInclVat": 85.72,
            "currency": "USD",
            "productionTimeFast": 9,
            "productionTimeSlow": 10,
            "scale": 1,
        },
        {
            "quoteId": "aaa111222333444",
            "vendorId": "shapeways",
            "modelId": "81dee7e88f5d780",
            "materialConfigId": "a97a2a21-0e71-51d1-b642-93b168660053",
            "quantity": 1,
            "price": 95.00,
            "priceInclVat": 113.05,
            "currency": "USD",
            "productionTimeFast": 5,
            "productionTimeSlow": 7,
            "scale": 1,
        },
    ],
    "shippings": [
        {
            "shippingId": "dcf5a4d5-f639-4d0a-9d2c-829b7ec9f0fc",
            "vendorId": "wenext",
            "name": "UPS Ground",
            "deliveryTime": "3-5",
            "price": 15.9,
            "priceInclVat": 18.9,
            "currency": "USD",
            "type": "standard",
            "carrier": "UPS",
        },
        {
            "shippingId": "eeee0000-1111-2222-3333-444444444444",
            "vendorId": "wenext",
            "name": "UPS Express",
            "deliveryTime": "1-2",
            "price": 29.99,
            "priceInclVat": 35.69,
            "currency": "USD",
            "type": "express",
            "carrier": "UPS",
        },
        {
            "shippingId": "ffff0000-1111-2222-3333-444444444444",
            "vendorId": "shapeways",
            "name": "FedEx",
            "deliveryTime": "5",
            "price": 12.50,
            "currency": "USD",
            "type": "standard",
            "carrier": "FedEx",
        },
    ],
}


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
    def test_no_api_key_works(self):
        p = CraftcloudProvider(api_key="")
        assert p._api_key == ""
        assert "X-API-Key" not in p._session.headers

    def test_no_api_key_no_env(self):
        p = CraftcloudProvider()
        assert p._api_key == ""

    def test_env_var_api_key(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "env-key")
        p = CraftcloudProvider()
        assert p._api_key == "env-key"

    def test_custom_base_url(self):
        p = _provider(base_url="https://api-stg.craftcloud3d.com/")
        assert p._base_url == "https://api-stg.craftcloud3d.com"

    def test_api_key_in_session_when_provided(self):
        p = _provider()
        assert p._session.headers.get("X-API-Key") == "test-key"

    def test_no_api_key_header_when_empty(self):
        p = CraftcloudProvider(api_key="")
        assert "X-API-Key" not in p._session.headers

    def test_poll_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_POLL_INTERVAL", "5.0")
        p = CraftcloudProvider()
        assert p._poll_interval == 5.0

    def test_max_poll_attempts_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_MAX_POLL_ATTEMPTS", "100")
        p = CraftcloudProvider()
        assert p._max_poll_attempts == 100

    def test_poll_interval_kwarg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_POLL_INTERVAL", "10")
        p = _provider(poll_interval=0.5)
        assert p._poll_interval == 0.5

    def test_use_websocket_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_USE_WEBSOCKET", "1")
        p = CraftcloudProvider()
        assert p._use_websocket is True

    def test_use_websocket_default_false(self):
        p = CraftcloudProvider()
        assert p._use_websocket is False

    def test_use_websocket_kwarg(self):
        p = _provider(use_websocket=True)
        assert p._use_websocket is True

    def test_payment_mode_default(self):
        p = CraftcloudProvider()
        assert p._payment_mode == "craftcloud"

    def test_payment_mode_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_PAYMENT_MODE", "partner")
        p = CraftcloudProvider()
        assert p._payment_mode == "partner"

    def test_payment_mode_kwarg(self):
        p = _provider(payment_mode="partner")
        assert p._payment_mode == "partner"


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
# Material catalog parsing
# ---------------------------------------------------------------------------


class TestParseMaterialCatalog:
    def test_parses_material_structure(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        assert len(materials) == 4  # 2 SLS Standard + 1 SLS Polished + 1 SLA Raw

    def test_material_config_ids(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        ids = {m.id for m in materials}
        assert "6c633df0-aca1-5b95-aaab-5c19b4e0d24f" in ids
        assert "a97a2a21-0e71-51d1-b642-93b168660053" in ids

    def test_technology_from_parent(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        sls = [m for m in materials if "PA12" in m.name]
        assert all(m.technology == "SLS" for m in sls)
        sla = [m for m in materials if "Resin" in m.name]
        assert all(m.technology == "SLA" for m in sla)

    def test_finish_from_finish_group(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        polished = [m for m in materials if m.id == "bbbb0000-1111-2222-3333-444444444444"]
        assert len(polished) == 1
        assert polished[0].finish == "Polished"

    def test_min_wall_from_printing_methods(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        sls = [m for m in materials if m.technology == "SLS"]
        assert all(m.min_wall_mm == 0.8 for m in sls)
        sla = [m for m in materials if m.technology == "SLA"]
        assert all(m.min_wall_mm == 0.5 for m in sla)

    def test_color_from_config(self):
        materials = CraftcloudProvider._parse_material_catalog(_CATALOG_PAYLOAD)
        by_id = {m.id: m for m in materials}
        assert by_id["6c633df0-aca1-5b95-aaab-5c19b4e0d24f"].color == "White"
        assert by_id["a97a2a21-0e71-51d1-b642-93b168660053"].color == "Black"

    def test_empty_payload(self):
        assert CraftcloudProvider._parse_material_catalog({}) == []

    def test_non_dict_payload(self):
        assert CraftcloudProvider._parse_material_catalog("invalid") == []

    def test_empty_material_structure(self):
        assert CraftcloudProvider._parse_material_catalog({"materialStructure": []}) == []

    def test_missing_config_id_skipped(self):
        payload = {
            "materialStructure": [{
                "materials": [{
                    "technology": "FDM",
                    "finishGroups": [{
                        "name": "Raw",
                        "materialConfigs": [{"name": "No ID Config"}],
                    }],
                }],
                "printingMethods": [],
            }],
        }
        assert CraftcloudProvider._parse_material_catalog(payload) == []


# ---------------------------------------------------------------------------
# list_materials
# ---------------------------------------------------------------------------


class TestListMaterials:
    def test_list_materials_from_catalog(self):
        p = _provider()
        resp = _mock_response(json_data=_CATALOG_PAYLOAD)
        with patch.object(p._session, "request", return_value=resp):
            materials = p.list_materials()

        assert len(materials) == 4
        assert materials[0].id == "6c633df0-aca1-5b95-aaab-5c19b4e0d24f"

    def test_list_materials_catalog_empty_raises(self):
        p = _provider()
        resp = _mock_response(json_data={"materialStructure": []})
        with patch.object(p._session, "request", return_value=resp):
            with pytest.raises(FulfillmentError, match="no materials"):
                p.list_materials()

    def test_list_materials_connection_error(self):
        p = _provider()
        from requests.exceptions import ConnectionError as CE
        with patch.object(p._session, "request", side_effect=CE("refused")):
            with pytest.raises(FulfillmentError, match="Could not connect"):
                p.list_materials()


# ---------------------------------------------------------------------------
# _upload_model
# ---------------------------------------------------------------------------


class TestUploadModel:
    def test_upload_returns_model_id(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[
            {"modelId": "abc123", "fileName": "model.stl"},
        ])
        poll_resp = _mock_response(status_code=200, json_data={"modelId": "abc123"})
        with patch.object(p._session, "request", return_value=upload_resp):
            with patch.object(p._session, "get", return_value=poll_resp):
                model_id = p._upload_model(str(model))

        assert model_id == "abc123"

    def test_upload_polls_until_ready(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider(max_poll_attempts=5)
        upload_resp = _mock_response(json_data=[{"modelId": "abc123"}])
        parsing_resp = MagicMock(spec=requests.Response)
        parsing_resp.status_code = 206
        ready_resp = MagicMock(spec=requests.Response)
        ready_resp.status_code = 200
        with patch.object(p._session, "request", return_value=upload_resp):
            with patch.object(p._session, "get", side_effect=[
                parsing_resp, parsing_resp, ready_resp,
            ]):
                model_id = p._upload_model(str(model))

        assert model_id == "abc123"

    def test_upload_timeout(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider(max_poll_attempts=2)
        upload_resp = _mock_response(json_data=[{"modelId": "abc123"}])
        parsing_resp = MagicMock(spec=requests.Response)
        parsing_resp.status_code = 206
        with patch.object(p._session, "request", return_value=upload_resp):
            with patch.object(p._session, "get", return_value=parsing_resp):
                with pytest.raises(FulfillmentError, match="did not finish parsing"):
                    p._upload_model(str(model))

    def test_upload_empty_response(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[])
        with patch.object(p._session, "request", return_value=upload_resp):
            with pytest.raises(FulfillmentError, match="empty model list"):
                p._upload_model(str(model))

    def test_upload_missing_model_id(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"fileName": "model.stl"}])
        with patch.object(p._session, "request", return_value=upload_resp):
            with pytest.raises(FulfillmentError, match="missing modelId"):
                p._upload_model(str(model))

    def test_upload_dict_response(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        # Some responses come as a dict with a models key.
        upload_resp = _mock_response(json_data={
            "models": [{"modelId": "dict-id"}],
        })
        poll_resp = MagicMock(spec=requests.Response)
        poll_resp.status_code = 200
        with patch.object(p._session, "request", return_value=upload_resp):
            with patch.object(p._session, "get", return_value=poll_resp):
                model_id = p._upload_model(str(model))

        assert model_id == "dict-id"


# ---------------------------------------------------------------------------
# _request_prices
# ---------------------------------------------------------------------------


class TestRequestPrices:
    def test_prices_complete_immediately(self):
        p = _provider()
        post_resp = _mock_response(json_data={"priceId": "price-123"})
        get_resp = _mock_response(json_data=_PRICE_RESPONSE)
        with patch.object(p._session, "request", side_effect=[post_resp, get_resp]):
            data = p._request_prices("model-1", quantity=1)

        assert data["allComplete"] is True
        assert len(data["quotes"]) == 2

    def test_prices_poll_until_complete(self):
        p = _provider(max_poll_attempts=5)
        post_resp = _mock_response(json_data={"priceId": "price-123"})
        incomplete = _mock_response(json_data={"allComplete": False, "quotes": []})
        complete = _mock_response(json_data=_PRICE_RESPONSE)
        with patch.object(p._session, "request", side_effect=[
            post_resp, incomplete, incomplete, complete,
        ]):
            data = p._request_prices("model-1")

        assert data["allComplete"] is True

    def test_prices_timeout(self):
        p = _provider(max_poll_attempts=2)
        post_resp = _mock_response(json_data={"priceId": "price-123"})
        incomplete = _mock_response(json_data={"allComplete": False})
        with patch.object(p._session, "request", side_effect=[
            post_resp, incomplete, incomplete,
        ]), pytest.raises(FulfillmentError, match="did not complete"):
            p._request_prices("model-1")

    def test_prices_no_price_id(self):
        p = _provider()
        post_resp = _mock_response(json_data={})
        with patch.object(p._session, "request", return_value=post_resp):
            with pytest.raises(FulfillmentError, match="priceId"):
                p._request_prices("model-1")

    def test_prices_with_material_filter(self):
        p = _provider()
        post_resp = _mock_response(json_data={"priceId": "p-1"})
        get_resp = _mock_response(json_data=_PRICE_RESPONSE)
        with patch.object(p._session, "request", side_effect=[post_resp, get_resp]) as mock_req:
            p._request_prices(
                "model-1",
                material_config_ids=["a97a2a21-0e71-51d1-b642-93b168660053"],
            )

        post_call = mock_req.call_args_list[0]
        payload = post_call.kwargs.get("json")
        assert "materialConfigIds" in payload
        assert payload["materialConfigIds"] == ["a97a2a21-0e71-51d1-b642-93b168660053"]


# ---------------------------------------------------------------------------
# get_quote (full flow)
# ---------------------------------------------------------------------------


class TestGetQuote:
    def test_get_quote_full_flow(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data=_PRICE_RESPONSE)

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="a97a2a21-0e71-51d1-b642-93b168660053",
            ))

        assert quote.quote_id == "bf2b604ae33685f"  # Cheapest quote
        assert quote.unit_price == 72.03
        assert quote.total_price == 72.03
        assert quote.lead_time_days == 10
        assert quote.currency == "USD"

    def test_get_quote_selects_cheapest(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data=_PRICE_RESPONSE)

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="a97a2a21-0e71-51d1-b642-93b168660053",
            ))

        # wenext ($72.03) is cheaper than shapeways ($95.00).
        assert quote.quote_id == "bf2b604ae33685f"

    def test_get_quote_shipping_for_selected_vendor(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data=_PRICE_RESPONSE)

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="a97a2a21-0e71-51d1-b642-93b168660053",
            ))

        # Only wenext shipping (2 options), not shapeways.
        assert len(quote.shipping_options) == 2
        assert quote.shipping_options[0].id == "dcf5a4d5-f639-4d0a-9d2c-829b7ec9f0fc"
        assert quote.shipping_options[0].name == "UPS Ground"
        assert quote.shipping_options[0].price == 15.9
        assert quote.shipping_options[0].estimated_days == 5  # "3-5" → 5

    def test_get_quote_expires_at_converted_to_seconds(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data=_PRICE_RESPONSE)

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            quote = p.get_quote(QuoteRequest(
                file_path=str(model),
                material_id="a97a2a21-0e71-51d1-b642-93b168660053",
            ))

        # 1658972807453ms → 1658972807.453s
        assert quote.expires_at == pytest.approx(1658972807.453, rel=1e-3)

    def test_get_quote_file_not_found(self):
        p = _provider()
        with pytest.raises(FileNotFoundError, match="not found"):
            p.get_quote(QuoteRequest(
                file_path="/nonexistent/model.stl",
                material_id="pla",
            ))

    def test_get_quote_no_quotes_returned(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data={
            "allComplete": True, "quotes": [], "shippings": [],
        })

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            with pytest.raises(FulfillmentError, match="no quotes"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="nonexistent-material",
                ))

    def test_get_quote_no_matching_material(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider()
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-1"})
        price_get = _mock_response(json_data=_PRICE_RESPONSE)

        with patch.object(p._session, "request", side_effect=[
            upload_resp, price_post, price_get,
        ]), patch.object(p._session, "get", return_value=model_ready):
            with pytest.raises(FulfillmentError, match="No valid priced quotes"):
                p.get_quote(QuoteRequest(
                    file_path=str(model),
                    material_id="wrong-material-id",
                ))


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_place_order_full_flow(self):
        p = _provider()
        cart_resp = _mock_response(json_data={"cartId": "cart-1"})
        order_resp = _mock_response(json_data={
            "orderId": "o-456",
            "orderNumber": "CC-12345",
            "amounts": {
                "total": {
                    "totalNetPrice": 87.93,
                    "totalGrossPrice": 104.64,
                    "currency": "USD",
                },
            },
        })
        with patch.object(p._session, "request", side_effect=[cart_resp, order_resp]) as mock_req:
            result = p.place_order(OrderRequest(
                quote_id="bf2b604ae33685f",
                shipping_option_id="dcf5a4d5-f639-4d0a-9d2c-829b7ec9f0fc",
            ))

        assert result.success is True
        assert result.order_id == "o-456"
        assert result.status == OrderStatus.SUBMITTED
        assert result.total_price == 104.64
        assert result.currency == "USD"

        # Verify cart was created with correct quote + shipping IDs.
        cart_call = mock_req.call_args_list[0]
        cart_payload = cart_call.kwargs.get("json")
        assert cart_payload["quotes"] == ["bf2b604ae33685f"]
        assert cart_payload["shippingIds"] == ["dcf5a4d5-f639-4d0a-9d2c-829b7ec9f0fc"]

    def test_place_order_with_shipping_address(self):
        p = _provider()
        cart_resp = _mock_response(json_data={"cartId": "cart-1"})
        order_resp = _mock_response(json_data={
            "orderId": "o-789",
            "amounts": {"total": {"totalNetPrice": 50.0, "currency": "USD"}},
        })
        with patch.object(p._session, "request", side_effect=[cart_resp, order_resp]) as mock_req:
            p.place_order(OrderRequest(
                quote_id="q-123",
                shipping_option_id="s-1",
                shipping_address={
                    "first_name": "John",
                    "last_name": "Doe",
                    "street": "123 Main St",
                    "city": "Austin",
                    "state": "TX",
                    "postal_code": "78701",
                    "country": "US",
                    "phone": "5125551234",
                    "email": "john@example.com",
                },
            ))

        order_call = mock_req.call_args_list[1]
        order_payload = order_call.kwargs.get("json")
        user = order_payload["user"]
        assert user["emailAddress"] == "john@example.com"
        assert user["shipping"]["firstName"] == "John"
        assert user["shipping"]["lastName"] == "Doe"
        assert user["shipping"]["address"] == "123 Main St"
        assert user["shipping"]["city"] == "Austin"
        assert user["shipping"]["stateCode"] == "TX"
        assert user["shipping"]["zipCode"] == "78701"
        assert user["shipping"]["countryCode"] == "US"
        assert user["shipping"]["phoneNumber"] == "5125551234"
        assert user["billing"]["firstName"] == "John"
        assert user["billing"]["isCompany"] is False

    def test_place_order_missing_quote_id(self):
        p = _provider()
        with pytest.raises(FulfillmentError, match="quote_id is required"):
            p.place_order(OrderRequest(quote_id=""))

    def test_place_order_missing_order_id_in_response(self):
        p = _provider()
        cart_resp = _mock_response(json_data={"cartId": "cart-1"})
        order_resp = _mock_response(json_data={"orderNumber": "CC-999"})
        with patch.object(p._session, "request", side_effect=[cart_resp, order_resp]):
            with pytest.raises(FulfillmentError, match="missing orderId"):
                p.place_order(OrderRequest(quote_id="q-123"))


# ---------------------------------------------------------------------------
# _build_user_payload
# ---------------------------------------------------------------------------


class TestBuildUserPayload:
    def test_snake_case_input(self):
        user = CraftcloudProvider._build_user_payload({
            "first_name": "Jane",
            "last_name": "Smith",
            "street": "456 Oak Ave",
            "city": "Portland",
            "state": "OR",
            "postal_code": "97201",
            "country": "US",
            "phone": "5035551234",
            "email": "jane@example.com",
        })
        assert user["emailAddress"] == "jane@example.com"
        assert user["shipping"]["firstName"] == "Jane"
        assert user["shipping"]["zipCode"] == "97201"
        assert user["shipping"]["countryCode"] == "US"
        assert user["billing"]["isCompany"] is False

    def test_camel_case_input(self):
        user = CraftcloudProvider._build_user_payload({
            "firstName": "Max",
            "lastName": "Mustermann",
            "address": "Musterstrasse 1",
            "city": "Berlin",
            "zipCode": "10115",
            "countryCode": "DE",
            "phoneNumber": "01234567",
            "emailAddress": "max@example.de",
        })
        assert user["emailAddress"] == "max@example.de"
        assert user["shipping"]["firstName"] == "Max"
        assert user["shipping"]["address"] == "Musterstrasse 1"
        assert user["shipping"]["countryCode"] == "DE"

    def test_company_address(self):
        user = CraftcloudProvider._build_user_payload({
            "first_name": "Bob",
            "last_name": "Corp",
            "street": "789 Business Blvd",
            "city": "NYC",
            "postal_code": "10001",
            "country": "US",
            "company": "Acme Inc",
            "vat_id": "US123456",
            "phone": "2125551234",
        })
        assert user["shipping"]["companyName"] == "Acme Inc"
        assert user["billing"]["isCompany"] is True
        assert user["billing"]["vatId"] == "US123456"

    def test_optional_fields_none(self):
        user = CraftcloudProvider._build_user_payload({
            "first_name": "Solo",
            "last_name": "Person",
            "street": "1 Lane",
            "city": "Town",
            "postal_code": "00000",
            "country": "US",
        })
        assert user["shipping"]["addressLine2"] is None
        assert user["shipping"]["stateCode"] is None
        assert user["shipping"]["companyName"] is None


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    def test_order_status_shipped(self):
        p = _provider()
        resp = _mock_response(json_data={
            "orderNumber": "CC-12345",
            "status": [
                {
                    "vendorId": "wenext",
                    "cancelled": False,
                    "orderStatus": [
                        {"type": "ordered", "date": "2026-02-10"},
                        {"type": "in_production", "date": "2026-02-11"},
                        {"type": "shipped", "date": "2026-02-13"},
                    ],
                    "trackingUrl": "https://track.ups.com/123",
                    "trackingNumber": "1Z999",
                },
            ],
            "estDeliveryTime": "2026-02-18",
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.SHIPPING
        assert result.tracking_url == "https://track.ups.com/123"
        assert result.tracking_number == "1Z999"
        assert result.estimated_delivery == "2026-02-18"

    def test_order_status_cancelled(self):
        p = _provider()
        resp = _mock_response(json_data={
            "status": [{"vendorId": "v1", "cancelled": True, "orderStatus": []}],
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.CANCELLED

    def test_order_status_in_production(self):
        p = _provider()
        resp = _mock_response(json_data={
            "status": [
                {
                    "vendorId": "v1",
                    "cancelled": False,
                    "orderStatus": [
                        {"type": "ordered", "date": "2026-02-10"},
                        {"type": "in_production", "date": "2026-02-11"},
                    ],
                },
            ],
        })
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.PRINTING

    def test_order_status_empty_defaults_to_submitted(self):
        p = _provider()
        resp = _mock_response(json_data={"status": []})
        with patch.object(p._session, "request", return_value=resp):
            result = p.get_order_status("o-456")

        assert result.status == OrderStatus.SUBMITTED


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_order(self):
        p = _provider()
        status_resp = _mock_response(json_data={
            "status": [
                {"vendorId": "wenext", "cancelled": False, "orderStatus": []},
            ],
        })
        cancel_resp = _mock_response(json_data={"status": "ok"})
        with patch.object(p._session, "request", side_effect=[status_resp, cancel_resp]) as mock_req:
            result = p.cancel_order("o-456")

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED

        # Verify PATCH was sent with correct vendor updates.
        cancel_call = mock_req.call_args_list[1]
        assert cancel_call.args[0] == "PATCH"
        cancel_payload = cancel_call.kwargs.get("json")
        assert cancel_payload == [{"vendorId": "wenext", "status": "cancelled"}]

    def test_cancel_order_no_vendors(self):
        p = _provider()
        status_resp = _mock_response(json_data={"status": []})
        with patch.object(p._session, "request", return_value=status_resp):
            with pytest.raises(FulfillmentError, match="no vendor entries"):
                p.cancel_order("o-456")


# ---------------------------------------------------------------------------
# WebSocket price polling
# ---------------------------------------------------------------------------


class TestWebSocketPricePolling:
    def test_ws_not_available_raises(self):
        p = _provider(use_websocket=True)
        with patch("kiln.fulfillment.craftcloud._WS_AVAILABLE", False):
            with pytest.raises(FulfillmentError, match="websockets.*msgpack"):
                p._poll_prices_websocket("price-123")

    def test_ws_poll_complete(self):
        import kiln.fulfillment.craftcloud as cc_mod

        p = _provider(use_websocket=True)
        complete_data = {
            "allComplete": True,
            "quotes": [{"quoteId": "q1", "price": 50.0}],
        }
        mock_conn = MagicMock()
        mock_conn.recv.return_value = b"msgpack-data"
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_ws_sync = MagicMock()
        mock_ws_sync.connect.return_value = mock_conn
        mock_msgpack = MagicMock()
        mock_msgpack.unpackb.return_value = complete_data

        original_ws = getattr(cc_mod, "ws_sync", None)
        original_mp = getattr(cc_mod, "msgpack", None)
        try:
            cc_mod.ws_sync = mock_ws_sync
            cc_mod.msgpack = mock_msgpack
            with patch.object(cc_mod, "_WS_AVAILABLE", True):
                result = p._poll_prices_websocket("price-123")
        finally:
            if original_ws is not None:
                cc_mod.ws_sync = original_ws
            elif hasattr(cc_mod, "ws_sync"):
                delattr(cc_mod, "ws_sync")
            if original_mp is not None:
                cc_mod.msgpack = original_mp
            elif hasattr(cc_mod, "msgpack"):
                delattr(cc_mod, "msgpack")

        assert result["allComplete"] is True
        assert result["quotes"][0]["quoteId"] == "q1"

    def test_ws_poll_text_frame(self):
        import kiln.fulfillment.craftcloud as cc_mod

        p = _provider(use_websocket=True)
        complete_data = {"allComplete": True, "quotes": []}
        mock_conn = MagicMock()
        mock_conn.recv.return_value = json.dumps(complete_data)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_ws_sync = MagicMock()
        mock_ws_sync.connect.return_value = mock_conn

        original_ws = getattr(cc_mod, "ws_sync", None)
        try:
            cc_mod.ws_sync = mock_ws_sync
            with patch.object(cc_mod, "_WS_AVAILABLE", True):
                result = p._poll_prices_websocket("price-123")
        finally:
            if original_ws is not None:
                cc_mod.ws_sync = original_ws
            elif hasattr(cc_mod, "ws_sync"):
                delattr(cc_mod, "ws_sync")

        assert result["allComplete"] is True

    def test_ws_connection_error(self):
        import kiln.fulfillment.craftcloud as cc_mod

        p = _provider(use_websocket=True)
        mock_ws_sync = MagicMock()
        mock_ws_sync.connect.side_effect = ConnectionRefusedError("refused")

        original_ws = getattr(cc_mod, "ws_sync", None)
        try:
            cc_mod.ws_sync = mock_ws_sync
            with patch.object(cc_mod, "_WS_AVAILABLE", True):
                with pytest.raises(FulfillmentError, match="WebSocket error"):
                    p._poll_prices_websocket("price-123")
        finally:
            if original_ws is not None:
                cc_mod.ws_sync = original_ws
            elif hasattr(cc_mod, "ws_sync"):
                delattr(cc_mod, "ws_sync")

    def test_get_quote_uses_websocket_when_enabled(self, tmp_path):
        model = tmp_path / "model.stl"
        model.write_text("solid model")

        p = _provider(use_websocket=True)
        upload_resp = _mock_response(json_data=[{"modelId": "m-1"}])
        model_ready = MagicMock(spec=requests.Response)
        model_ready.status_code = 200
        price_post = _mock_response(json_data={"priceId": "p-ws"})

        ws_result = _PRICE_RESPONSE.copy()

        with patch.object(p._session, "request", side_effect=[upload_resp, price_post]):
            with patch.object(p._session, "get", return_value=model_ready):
                with patch.object(p, "_poll_prices_websocket", return_value=ws_result) as ws_mock:
                    quote = p.get_quote(QuoteRequest(
                        file_path=str(model),
                        material_id="a97a2a21-0e71-51d1-b642-93b168660053",
                    ))

        ws_mock.assert_called_once_with("p-ws")
        assert quote.quote_id == "bf2b604ae33685f"


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
