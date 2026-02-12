"""Tests for the 3DOS distributed manufacturing gateway client."""

from __future__ import annotations

import json
import os

import pytest
import requests.exceptions
import responses

from kiln.gateway.threedos import (
    NetworkJob,
    PrinterListing,
    ThreeDOSClient,
    ThreeDOSError,
)

_BASE = "https://api.3dos.io/v1"


def _client(**kwargs) -> ThreeDOSClient:
    return ThreeDOSClient(api_key="test-key-123", **kwargs)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_printer_listing_to_dict(self):
        p = PrinterListing(
            id="p-1", name="Prusa MK4", location="NYC",
            capabilities={"materials": ["PLA"]}, available=True,
            price_per_gram=0.05, currency="USD",
        )
        d = p.to_dict()
        assert d["id"] == "p-1"
        assert d["name"] == "Prusa MK4"
        assert d["available"] is True
        assert d["price_per_gram"] == 0.05

    def test_printer_listing_defaults(self):
        p = PrinterListing(id="p-2", name="Test", location="LA")
        assert p.capabilities == {}
        assert p.available is True
        assert p.price_per_gram is None
        assert p.currency == "USD"

    def test_network_job_to_dict(self):
        j = NetworkJob(
            id="j-1", file_url="https://example.com/model.stl",
            material="PLA", status="printing", printer_id="p-1",
            estimated_cost=4.50,
        )
        d = j.to_dict()
        assert d["id"] == "j-1"
        assert d["status"] == "printing"
        assert d["estimated_cost"] == 4.50

    def test_network_job_defaults(self):
        j = NetworkJob(id="j-2", file_url="", material="PLA", status="submitted")
        assert j.printer_id is None
        assert j.estimated_cost is None
        assert j.currency == "USD"


# ---------------------------------------------------------------------------
# ThreeDOSError
# ---------------------------------------------------------------------------


class TestThreeDOSError:
    def test_message(self):
        err = ThreeDOSError("something failed")
        assert str(err) == "something failed"

    def test_status_code(self):
        err = ThreeDOSError("not found", status_code=404)
        assert err.status_code == 404

    def test_default_status_code(self):
        err = ThreeDOSError("oops")
        assert err.status_code is None

    def test_inherits_exception(self):
        assert issubclass(ThreeDOSError, Exception)


# ---------------------------------------------------------------------------
# Client init
# ---------------------------------------------------------------------------


class TestThreeDOSClientInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(ValueError, match="3DOS API key required"):
            ThreeDOSClient(api_key="")

    def test_env_var_key(self, monkeypatch):
        monkeypatch.setenv("KILN_3DOS_API_KEY", "env-key-abc")
        client = ThreeDOSClient()
        assert client._api_key == "env-key-abc"

    def test_explicit_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("KILN_3DOS_API_KEY", "env-key")
        client = ThreeDOSClient(api_key="explicit-key")
        assert client._api_key == "explicit-key"

    def test_default_base_url(self):
        client = _client()
        assert client._base_url == "https://api.3dos.io/v1"

    def test_custom_base_url(self):
        client = _client(base_url="https://custom.api.com/v2")
        assert client._base_url == "https://custom.api.com/v2"

    def test_trailing_slash_stripped(self):
        client = _client(base_url="https://custom.api.com/v2/")
        assert client._base_url == "https://custom.api.com/v2"

    def test_env_base_url(self, monkeypatch):
        monkeypatch.setenv("KILN_3DOS_BASE_URL", "https://staging.3dos.io/v1")
        client = _client()
        assert client._base_url == "https://staging.3dos.io/v1"

    def test_session_headers(self):
        client = _client()
        assert client._session.headers["Authorization"] == "Bearer test-key-123"
        assert client._session.headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# register_printer
# ---------------------------------------------------------------------------


class TestRegisterPrinter:
    @responses.activate
    def test_register_success(self):
        responses.add(
            responses.POST, f"{_BASE}/printers",
            json={"id": "p-new", "name": "MK4", "location": "NYC",
                  "capabilities": {"volume": "250x210x220"},
                  "available": True, "price_per_gram": 0.05},
            status=200,
        )
        client = _client()
        result = client.register_printer(name="MK4", location="NYC",
                                         capabilities={"volume": "250x210x220"},
                                         price_per_gram=0.05)
        assert isinstance(result, PrinterListing)
        assert result.id == "p-new"
        assert result.name == "MK4"

    @responses.activate
    def test_register_minimal_fields(self):
        responses.add(
            responses.POST, f"{_BASE}/printers",
            json={"id": "p-min"},
            status=200,
        )
        client = _client()
        result = client.register_printer(name="Test", location="Nowhere")
        assert result.id == "p-min"
        assert result.name == "Test"
        assert result.location == "Nowhere"

    @responses.activate
    def test_register_server_error(self):
        responses.add(
            responses.POST, f"{_BASE}/printers",
            json={"error": "internal server error"}, status=500,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 500"):
            client.register_printer(name="Test", location="Nowhere")

    @responses.activate
    def test_register_payload_sent(self):
        responses.add(
            responses.POST, f"{_BASE}/printers",
            json={"id": "p-check"}, status=200,
        )
        client = _client()
        client.register_printer(name="MK4", location="Berlin",
                                capabilities={"volume": "250x210x220"},
                                price_per_gram=0.08)
        body = json.loads(responses.calls[0].request.body)
        assert body["name"] == "MK4"
        assert body["location"] == "Berlin"
        assert body["capabilities"]["volume"] == "250x210x220"
        assert body["price_per_gram"] == 0.08


# ---------------------------------------------------------------------------
# update_printer_status
# ---------------------------------------------------------------------------


class TestUpdatePrinterStatus:
    @responses.activate
    def test_update_available(self):
        responses.add(
            responses.PATCH, f"{_BASE}/printers/p-abc",
            json={"available": True}, status=200,
        )
        client = _client()
        client.update_printer_status("p-abc", available=True)
        body = json.loads(responses.calls[0].request.body)
        assert body["available"] is True

    @responses.activate
    def test_update_unavailable(self):
        responses.add(
            responses.PATCH, f"{_BASE}/printers/p-abc",
            json={"available": False}, status=200,
        )
        client = _client()
        client.update_printer_status("p-abc", available=False)
        body = json.loads(responses.calls[0].request.body)
        assert body["available"] is False

    @responses.activate
    def test_update_not_found(self):
        responses.add(
            responses.PATCH, f"{_BASE}/printers/p-nonexistent",
            json={"error": "printer not found"}, status=404,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 404"):
            client.update_printer_status("p-nonexistent", available=True)


# ---------------------------------------------------------------------------
# list_my_printers
# ---------------------------------------------------------------------------


class TestListMyPrinters:
    @responses.activate
    def test_list_success(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={"printers": [
                {"id": "p-1", "name": "Prusa MK4", "location": "NYC",
                 "capabilities": {"materials": ["PLA"]}, "available": True,
                 "price_per_gram": 0.05},
                {"id": "p-2", "name": "Bambu X1C", "location": "LA",
                 "capabilities": {}, "available": False},
            ]},
            status=200,
        )
        client = _client()
        result = client.list_my_printers()
        assert len(result) == 2
        assert all(isinstance(p, PrinterListing) for p in result)
        assert result[0].id == "p-1"
        assert result[1].available is False

    @responses.activate
    def test_list_empty(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={"printers": []}, status=200,
        )
        client = _client()
        assert client.list_my_printers() == []

    @responses.activate
    def test_list_missing_key(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={}, status=200,
        )
        client = _client()
        assert client.list_my_printers() == []


# ---------------------------------------------------------------------------
# find_printers
# ---------------------------------------------------------------------------


class TestFindPrinters:
    @responses.activate
    def test_find_with_material(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/search",
            json={"printers": [
                {"id": "p-remote", "name": "Berlin Printer", "location": "Berlin",
                 "available": True, "price_per_gram": 0.04, "currency": "EUR"},
            ]},
            status=200,
        )
        client = _client()
        result = client.find_printers(material="PLA")
        assert len(result) == 1
        assert result[0].currency == "EUR"

    @responses.activate
    def test_find_with_location_filter(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/search",
            json={"printers": [{"id": "p-nyc", "name": "NYC Printer", "location": "NYC"}]},
            status=200,
        )
        client = _client()
        client.find_printers(material="PLA", location="NYC")
        assert "material=PLA" in responses.calls[0].request.url
        assert "location=NYC" in responses.calls[0].request.url

    @responses.activate
    def test_find_no_results(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/search",
            json={"printers": []}, status=200,
        )
        client = _client()
        assert client.find_printers(material="Titanium") == []

    @responses.activate
    def test_find_multiple(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/search",
            json={"printers": [
                {"id": "p-1", "name": "A", "location": "NYC"},
                {"id": "p-2", "name": "B", "location": "LA"},
                {"id": "p-3", "name": "C", "location": "Berlin"},
            ]},
            status=200,
        )
        client = _client()
        result = client.find_printers(material="PLA")
        assert len(result) == 3
        assert [p.id for p in result] == ["p-1", "p-2", "p-3"]


# ---------------------------------------------------------------------------
# submit_network_job
# ---------------------------------------------------------------------------


class TestSubmitJob:
    @responses.activate
    def test_submit_success(self):
        responses.add(
            responses.POST, f"{_BASE}/jobs",
            json={"id": "j-abc", "file_url": "https://example.com/model.stl",
                  "material": "PLA", "status": "submitted",
                  "printer_id": "p-1", "estimated_cost": 4.50, "currency": "USD"},
            status=200,
        )
        client = _client()
        result = client.submit_network_job(
            file_url="https://example.com/model.stl",
            material="PLA", printer_id="p-1",
        )
        assert isinstance(result, NetworkJob)
        assert result.id == "j-abc"
        assert result.estimated_cost == 4.50

    @responses.activate
    def test_submit_auto_assign(self):
        responses.add(
            responses.POST, f"{_BASE}/jobs",
            json={"id": "j-auto", "status": "queued", "printer_id": "p-assigned"},
            status=200,
        )
        client = _client()
        result = client.submit_network_job(
            file_url="https://example.com/model.stl", material="PETG",
        )
        assert result.printer_id == "p-assigned"
        body = json.loads(responses.calls[0].request.body)
        assert "printer_id" not in body

    @responses.activate
    def test_submit_with_printer_id(self):
        responses.add(
            responses.POST, f"{_BASE}/jobs",
            json={"id": "j-targeted", "status": "submitted"},
            status=200,
        )
        client = _client()
        client.submit_network_job(
            file_url="https://example.com/model.stl",
            material="PLA", printer_id="p-specific",
        )
        body = json.loads(responses.calls[0].request.body)
        assert body["printer_id"] == "p-specific"

    @responses.activate
    def test_submit_validation_error(self):
        responses.add(
            responses.POST, f"{_BASE}/jobs",
            json={"error": "file_url is required"}, status=422,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 422"):
            client.submit_network_job(file_url="", material="PLA")

    @responses.activate
    def test_submit_sparse_response(self):
        responses.add(
            responses.POST, f"{_BASE}/jobs",
            json={"id": "j-sparse"}, status=200,
        )
        client = _client()
        result = client.submit_network_job(
            file_url="https://example.com/model.stl", material="PLA",
        )
        assert result.id == "j-sparse"
        assert result.status == "submitted"
        assert result.printer_id is None
        assert result.estimated_cost is None


# ---------------------------------------------------------------------------
# get_network_job
# ---------------------------------------------------------------------------


class TestGetNetworkJob:
    @responses.activate
    def test_get_success(self):
        responses.add(
            responses.GET, f"{_BASE}/jobs/j-abc",
            json={"id": "j-abc", "file_url": "https://example.com/model.stl",
                  "material": "PLA", "status": "printing",
                  "printer_id": "p-1", "estimated_cost": 4.50},
            status=200,
        )
        client = _client()
        result = client.get_network_job("j-abc")
        assert result.status == "printing"
        assert result.printer_id == "p-1"

    @responses.activate
    def test_get_not_found(self):
        responses.add(
            responses.GET, f"{_BASE}/jobs/j-nonexistent",
            json={"error": "job not found"}, status=404,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 404"):
            client.get_network_job("j-nonexistent")

    @responses.activate
    def test_get_sparse_defaults(self):
        responses.add(
            responses.GET, f"{_BASE}/jobs/j-sparse",
            json={"id": "j-sparse"}, status=200,
        )
        client = _client()
        result = client.get_network_job("j-sparse")
        assert result.file_url == ""
        assert result.material == ""
        assert result.status == "unknown"

    @responses.activate
    def test_get_id_fallback(self):
        responses.add(
            responses.GET, f"{_BASE}/jobs/j-fallback",
            json={"status": "completed", "material": "ABS"}, status=200,
        )
        client = _client()
        result = client.get_network_job("j-fallback")
        assert result.id == "j-fallback"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @responses.activate
    def test_timeout_error(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            body=requests.exceptions.Timeout("Connection timed out"),
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="Failed to reach 3DOS API"):
            client.list_my_printers()

    @responses.activate
    def test_connection_error(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="Failed to reach 3DOS API"):
            client.list_my_printers()

    @responses.activate
    def test_error_preserves_status_code(self):
        responses.add(
            responses.PATCH, f"{_BASE}/printers/p-1",
            json={"error": "forbidden"}, status=403,
        )
        client = _client()
        with pytest.raises(ThreeDOSError) as exc_info:
            client.update_printer_status("p-1", available=True)
        assert exc_info.value.status_code == 403

    @responses.activate
    def test_cause_chain_preserved(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            body=requests.exceptions.ConnectionError("DNS failed"),
        )
        client = _client()
        with pytest.raises(ThreeDOSError) as exc_info:
            client.list_my_printers()
        assert exc_info.value.__cause__ is not None

    @responses.activate
    def test_auth_header_sent(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={"printers": []}, status=200,
        )
        client = _client()
        client.list_my_printers()
        auth = responses.calls[0].request.headers.get("Authorization")
        assert auth == "Bearer test-key-123"

    @responses.activate
    def test_unauthorized(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={"error": "invalid API key"}, status=401,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 401"):
            client.list_my_printers()

    @responses.activate
    def test_rate_limit(self):
        responses.add(
            responses.GET, f"{_BASE}/printers/mine",
            json={"error": "rate limit exceeded"}, status=429,
        )
        client = _client()
        with pytest.raises(ThreeDOSError, match="3DOS API returned 429"):
            client.list_my_printers()
