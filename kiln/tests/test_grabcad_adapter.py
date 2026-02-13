"""Tests for the GrabCAD marketplace adapter (kiln.marketplaces.grabcad)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import responses
from responses import matchers

from kiln.marketplaces.base import (
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplaceRateLimitError,
    ModelDetail,
    ModelFile,
    ModelSummary,
)
from kiln.marketplaces.grabcad import GrabCADAdapter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRABCAD_BASE = "https://grabcad-test.local/api/v1"
GRABCAD_TOKEN = "test-grabcad-token-xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def grabcad_adapter():
    """GrabCADAdapter configured with test base URL."""
    return GrabCADAdapter(api_token=GRABCAD_TOKEN, base_url=GRABCAD_BASE)


# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------

GRABCAD_SEARCH_RESPONSE = {
    "models": [
        {
            "id": 500,
            "name": "Planetary Gear Set",
            "url": "https://grabcad.com/library/planetary-gear-set",
            "creator": {"name": "EngineerOne", "username": "eng1"},
            "preview_image": "https://cdn.grabcad.com/thumb500.jpg",
            "likes_count": 80,
            "downloads_count": 4200,
            "license": "CC-BY",
        },
    ],
}

GRABCAD_DETAIL_RESPONSE = {
    "id": 500,
    "name": "Planetary Gear Set",
    "url": "https://grabcad.com/library/planetary-gear-set",
    "creator": {"name": "EngineerOne", "username": "eng1"},
    "description": "A parametric planetary gear assembly.",
    "instructions": "Import into Fusion 360 or SolidWorks.",
    "license": "CC-BY",
    "preview_image": "https://cdn.grabcad.com/thumb500.jpg",
    "likes_count": 80,
    "downloads_count": 4200,
    "category": "Mechanical Parts",
    "tags": [{"name": "gear"}, {"name": "planetary"}],
    "file_count": 3,
}

GRABCAD_FILES_RESPONSE = {
    "files": [
        {
            "id": 601,
            "file_name": "gear_assembly.step",
            "size": 2048000,
            "download_url": "https://cdn.grabcad.com/dl/601",
            "preview_url": "https://cdn.grabcad.com/ft601.jpg",
        },
        {
            "id": 602,
            "file_name": "gear_body.stl",
            "size": 512000,
            "download_url": "https://cdn.grabcad.com/dl/602",
            "preview_url": None,
        },
    ],
}


# ===================================================================
# Init tests
# ===================================================================


class TestGrabCADAdapterInit:
    def test_requires_api_token(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_GRABCAD_TOKEN", None)
            with pytest.raises(MarketplaceAuthError, match="API token is required"):
                GrabCADAdapter(api_token="")

    def test_api_token_from_env(self):
        with patch.dict(os.environ, {"KILN_GRABCAD_TOKEN": "env-token"}):
            adapter = GrabCADAdapter(base_url=GRABCAD_BASE)
            assert adapter._api_token == "env-token"

    def test_explicit_token_overrides_env(self):
        with patch.dict(os.environ, {"KILN_GRABCAD_TOKEN": "env-token"}):
            adapter = GrabCADAdapter(api_token="explicit", base_url=GRABCAD_BASE)
            assert adapter._api_token == "explicit"

    def test_base_url_stripped(self):
        adapter = GrabCADAdapter(api_token="tok", base_url="https://example.com/api/")
        assert adapter._base_url == "https://example.com/api"


# ===================================================================
# Properties
# ===================================================================


class TestGrabCADAdapterProperties:
    def test_name(self, grabcad_adapter):
        assert grabcad_adapter.name == "grabcad"

    def test_display_name(self, grabcad_adapter):
        assert grabcad_adapter.display_name == "GrabCAD"

    def test_supports_download_true(self, grabcad_adapter):
        assert grabcad_adapter.supports_download is True


# ===================================================================
# Search tests
# ===================================================================


class TestGrabCADAdapterSearch:
    @responses.activate
    def test_search_success(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            json=GRABCAD_SEARCH_RESPONSE,
        )
        results = grabcad_adapter.search("gear")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "500"
        assert r.name == "Planetary Gear Set"
        assert r.source == "grabcad"
        assert r.creator == "EngineerOne"
        assert r.thumbnail == "https://cdn.grabcad.com/thumb500.jpg"
        assert r.like_count == 80
        assert r.download_count == 4200
        assert r.license == "CC-BY"
        assert r.has_sliceable_files is True

    @responses.activate
    def test_search_passes_params(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            json={"models": []},
            match=[matchers.query_param_matcher(
                {"query": "benchy", "page": "2", "per_page": "5", "sort": "relevance"},
                strict_match=False,
            )],
        )
        grabcad_adapter.search("benchy", page=2, per_page=5, sort="relevant")

    @responses.activate
    def test_search_sort_mapping_popular(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            json={"models": []},
            match=[matchers.query_param_matcher(
                {"sort": "popularity"},
                strict_match=False,
            )],
        )
        grabcad_adapter.search("test", sort="popular")

    @responses.activate
    def test_search_sort_mapping_newest(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            json={"models": []},
            match=[matchers.query_param_matcher(
                {"sort": "newest"},
                strict_match=False,
            )],
        )
        grabcad_adapter.search("test", sort="newest")

    @responses.activate
    def test_search_empty_response(self, grabcad_adapter):
        responses.add(
            responses.GET, f"{GRABCAD_BASE}/models", json={"models": []},
        )
        assert grabcad_adapter.search("noresults") == []

    @responses.activate
    def test_search_per_page_capped(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            json={"models": []},
            match=[matchers.query_param_matcher(
                {"per_page": "100"},
                strict_match=False,
            )],
        )
        grabcad_adapter.search("test", per_page=999)

    @responses.activate
    def test_search_non_dict_data(self, grabcad_adapter):
        """If the API returns a non-dict response, treat as empty."""
        responses.add(
            responses.GET, f"{GRABCAD_BASE}/models", json=[],
        )
        assert grabcad_adapter.search("test") == []

    @responses.activate
    def test_search_sends_bearer_auth(self, grabcad_adapter):
        def check_auth(request):
            assert request.headers["Authorization"] == f"Bearer {GRABCAD_TOKEN}"
            return (200, {}, '{"models": []}')

        responses.add_callback(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            callback=check_auth,
            content_type="application/json",
        )
        grabcad_adapter.search("test")


# ===================================================================
# Get details tests
# ===================================================================


class TestGrabCADAdapterGetDetails:
    @responses.activate
    def test_get_details_success(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/500",
            json=GRABCAD_DETAIL_RESPONSE,
        )
        detail = grabcad_adapter.get_details("500")
        assert isinstance(detail, ModelDetail)
        assert detail.id == "500"
        assert detail.name == "Planetary Gear Set"
        assert detail.source == "grabcad"
        assert detail.creator == "EngineerOne"
        assert detail.description == "A parametric planetary gear assembly."
        assert detail.instructions == "Import into Fusion 360 or SolidWorks."
        assert detail.license == "CC-BY"
        assert detail.category == "Mechanical Parts"
        assert detail.tags == ["gear", "planetary"]
        assert detail.file_count == 3
        assert detail.like_count == 80
        assert detail.download_count == 4200

    @responses.activate
    def test_get_details_not_found(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError):
            grabcad_adapter.get_details("999")

    @responses.activate
    def test_get_details_empty_category(self, grabcad_adapter):
        """Empty category string maps to None."""
        data = {**GRABCAD_DETAIL_RESPONSE, "category": ""}
        responses.add(
            responses.GET, f"{GRABCAD_BASE}/models/500", json=data,
        )
        detail = grabcad_adapter.get_details("500")
        assert detail.category is None

    @responses.activate
    def test_get_details_tags_as_strings(self, grabcad_adapter):
        """Tags provided as plain strings (not dicts) are handled."""
        data = {**GRABCAD_DETAIL_RESPONSE, "tags": ["gear", "assembly"]}
        responses.add(
            responses.GET, f"{GRABCAD_BASE}/models/500", json=data,
        )
        detail = grabcad_adapter.get_details("500")
        assert detail.tags == ["gear", "assembly"]

    @responses.activate
    def test_get_details_null_creator(self, grabcad_adapter):
        """Null creator field doesn't crash."""
        data = {**GRABCAD_DETAIL_RESPONSE, "creator": None}
        responses.add(
            responses.GET, f"{GRABCAD_BASE}/models/500", json=data,
        )
        detail = grabcad_adapter.get_details("500")
        assert detail.creator == ""


# ===================================================================
# Get files tests
# ===================================================================


class TestGrabCADAdapterGetFiles:
    @responses.activate
    def test_get_files_success(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/500/files",
            json=GRABCAD_FILES_RESPONSE,
        )
        files = grabcad_adapter.get_files("500")
        assert len(files) == 2

        step = files[0]
        assert isinstance(step, ModelFile)
        assert step.id == "601"
        assert step.name == "gear_assembly.step"
        assert step.file_type == "step"
        assert step.size_bytes == 2048000
        assert step.download_url == "https://cdn.grabcad.com/dl/601"
        assert step.thumbnail_url == "https://cdn.grabcad.com/ft601.jpg"

        stl = files[1]
        assert stl.id == "602"
        assert stl.name == "gear_body.stl"
        assert stl.file_type == "stl"
        assert stl.size_bytes == 512000
        assert stl.thumbnail_url is None

    @responses.activate
    def test_get_files_empty(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/500/files",
            json={"files": []},
        )
        assert grabcad_adapter.get_files("500") == []

    @responses.activate
    def test_get_files_non_dict_response(self, grabcad_adapter):
        """Non-dict top-level response yields empty list."""
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/500/files",
            json=[],
        )
        assert grabcad_adapter.get_files("500") == []


# ===================================================================
# Download tests
# ===================================================================


class TestGrabCADAdapterDownload:
    @responses.activate
    def test_download_success(self, grabcad_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/files/601",
            json={
                "id": 601,
                "file_name": "gear_assembly.step",
                "download_url": "https://cdn.grabcad.com/dl/601",
            },
        )
        responses.add(
            responses.GET,
            "https://cdn.grabcad.com/dl/601",
            body=b"FAKE_STEP_BYTES",
            content_type="application/octet-stream",
        )
        path = grabcad_adapter.download_file("601", str(tmp_path))
        assert Path(path).exists()
        assert Path(path).name == "gear_assembly.step"
        assert Path(path).read_bytes() == b"FAKE_STEP_BYTES"

    @responses.activate
    def test_download_custom_filename(self, grabcad_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/files/601",
            json={
                "id": 601,
                "file_name": "gear_assembly.step",
                "download_url": "https://cdn.grabcad.com/dl/601",
            },
        )
        responses.add(
            responses.GET,
            "https://cdn.grabcad.com/dl/601",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = grabcad_adapter.download_file(
            "601", str(tmp_path), file_name="custom.step",
        )
        assert Path(path).name == "custom.step"

    @responses.activate
    def test_download_no_url_raises(self, grabcad_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/files/601",
            json={"id": 601, "file_name": "gear.step", "download_url": ""},
        )
        with pytest.raises(MarketplaceError, match="No download URL"):
            grabcad_adapter.download_file("601", str(tmp_path))

    @responses.activate
    def test_download_creates_dir(self, grabcad_adapter, tmp_path):
        dest = tmp_path / "sub" / "dir"
        assert not dest.exists()
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/files/601",
            json={
                "id": 601,
                "file_name": "f.step",
                "download_url": "https://cdn.grabcad.com/dl/601",
            },
        )
        responses.add(
            responses.GET,
            "https://cdn.grabcad.com/dl/601",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = grabcad_adapter.download_file("601", str(dest))
        assert Path(path).exists()

    @responses.activate
    def test_download_fallback_filename(self, grabcad_adapter, tmp_path):
        """When file_name is missing from metadata, use fallback."""
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/files/601",
            json={
                "id": 601,
                "download_url": "https://cdn.grabcad.com/dl/601",
            },
        )
        responses.add(
            responses.GET,
            "https://cdn.grabcad.com/dl/601",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = grabcad_adapter.download_file("601", str(tmp_path))
        assert Path(path).name == "file_601"


# ===================================================================
# Error handling tests
# ===================================================================


class TestGrabCADAdapterErrors:
    @responses.activate
    def test_401_raises_auth_error(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            status=401,
            json={"error": "Unauthorized"},
        )
        with pytest.raises(MarketplaceAuthError, match="Invalid or expired"):
            grabcad_adapter.search("test")

    @responses.activate
    def test_404_raises_not_found(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            grabcad_adapter.get_details("999")

    @responses.activate
    def test_429_raises_rate_limit(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            status=429,
            json={"error": "Too many"},
        )
        with pytest.raises(MarketplaceRateLimitError, match="rate limit"):
            grabcad_adapter.search("test")

    @responses.activate
    def test_500_raises_generic_error(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            status=500,
            body="Internal Server Error",
        )
        with pytest.raises(MarketplaceError, match="500"):
            grabcad_adapter.search("test")

    @responses.activate
    def test_connection_error(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(MarketplaceError, match="Connection"):
            grabcad_adapter.search("test")

    @responses.activate
    def test_timeout_error(self, grabcad_adapter):
        responses.add(
            responses.GET,
            f"{GRABCAD_BASE}/models",
            body=requests.Timeout("timed out"),
        )
        with pytest.raises(MarketplaceError, match="timed out"):
            grabcad_adapter.search("test")


# ===================================================================
# Parse helper edge cases
# ===================================================================


class TestGrabCADParseHelpers:
    def test_parse_summary_no_creator(self):
        data = {"id": 1, "name": "X", "url": "/x", "creator": None}
        s = GrabCADAdapter._parse_summary(data)
        assert s.creator == ""

    def test_parse_summary_creator_username_fallback(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"username": "user1"},
        }
        s = GrabCADAdapter._parse_summary(data)
        assert s.creator == "user1"

    def test_parse_summary_zero_counts(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "likes_count": None,
            "downloads_count": None,
        }
        s = GrabCADAdapter._parse_summary(data)
        assert s.like_count == 0
        assert s.download_count == 0

    def test_parse_detail_null_tags(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "tags": None,
        }
        d = GrabCADAdapter._parse_detail(data)
        assert d.tags == []

    def test_parse_detail_non_string_category(self):
        """Non-string category (e.g. int) maps to None."""
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "category": 42,
        }
        d = GrabCADAdapter._parse_detail(data)
        assert d.category is None

    def test_parse_file_no_extension(self):
        data = {"id": 1, "file_name": "readme", "size": 100}
        f = GrabCADAdapter._parse_file(data)
        assert f.file_type == ""
        assert f.name == "readme"

    def test_parse_file_empty_filename(self):
        data = {"id": 1, "file_name": ""}
        f = GrabCADAdapter._parse_file(data)
        assert f.name == ""
        assert f.file_type == ""

    def test_parse_file_extension_case_insensitive(self):
        data = {"id": 1, "file_name": "Model.STEP", "size": 500}
        f = GrabCADAdapter._parse_file(data)
        assert f.file_type == "step"
