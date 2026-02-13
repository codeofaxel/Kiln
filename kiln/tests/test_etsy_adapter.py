"""Tests for the Etsy marketplace adapter (kiln.marketplaces.etsy)."""

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
from kiln.marketplaces.etsy import EtsyAdapter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ETSY_BASE = "https://etsy-test.local/v3"
ETSY_API_KEY = "test-etsy-key-abc"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def etsy_adapter():
    """EtsyAdapter configured with test base URL."""
    return EtsyAdapter(api_key=ETSY_API_KEY, base_url=ETSY_BASE)


# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------

ETSY_SEARCH_RESPONSE = {
    "results": [
        {
            "listing_id": 12345,
            "title": "3D Benchy STL File",
            "url": "https://www.etsy.com/listing/12345",
            "is_digital": True,
            "num_favorers": 42,
            "views": 1000,
            "tags": ["stl", "3d print", "benchy"],
            "shop": {"shop_name": "PrintShop3D"},
            "images": [
                {"url_570xN": "https://i.etsystatic.com/thumb_570.jpg",
                 "url_170x135": "https://i.etsystatic.com/thumb_170.jpg"},
            ],
            "price": {"amount": 499, "divisor": 100, "currency_code": "USD"},
        },
    ],
}

ETSY_DETAIL_RESPONSE = {
    "listing_id": 12345,
    "title": "3D Benchy STL File",
    "url": "https://www.etsy.com/listing/12345",
    "is_digital": True,
    "description": "High-quality 3D Benchy model for calibration.",
    "num_favorers": 42,
    "views": 1000,
    "tags": ["stl", "3d print", "benchy"],
    "shop": {"shop_name": "PrintShop3D"},
    "images": [
        {"url_570xN": "https://i.etsystatic.com/thumb_570.jpg"},
    ],
    "price": {"amount": 499, "divisor": 100, "currency_code": "USD"},
    "materials": ["PLA", "PETG"],
    "taxonomy_path": ["Craft Supplies", "3D Printing"],
    "file_count": 3,
}

ETSY_FILES_RESPONSE = {
    "results": [
        {
            "listing_file_id": 100,
            "filename": "benchy.stl",
            "size": 256000,
            "url": "https://files.etsy.com/dl/100",
        },
        {
            "listing_file_id": 101,
            "filename": "benchy_supports.3mf",
            "size": 512000,
            "url": "https://files.etsy.com/dl/101",
        },
    ],
}

ETSY_FILE_META_RESPONSE = {
    "listing_file_id": 100,
    "filename": "benchy.stl",
    "url": "https://files.etsy.com/dl/100",
}


# ===================================================================
# Constructor tests
# ===================================================================


class TestEtsyAdapterInit:
    """EtsyAdapter constructor and API key validation."""

    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_ETSY_API_KEY", None)
            with pytest.raises(MarketplaceAuthError, match="API key is required"):
                EtsyAdapter(api_key="")

    def test_api_key_from_env(self):
        with patch.dict(os.environ, {"KILN_ETSY_API_KEY": "env-key"}):
            adapter = EtsyAdapter(base_url=ETSY_BASE)
            assert adapter._api_key == "env-key"

    def test_explicit_key_overrides_env(self):
        with patch.dict(os.environ, {"KILN_ETSY_API_KEY": "env-key"}):
            adapter = EtsyAdapter(api_key="explicit", base_url=ETSY_BASE)
            assert adapter._api_key == "explicit"

    def test_base_url_trailing_slash_stripped(self):
        adapter = EtsyAdapter(api_key="key", base_url="https://example.com/v3/")
        assert adapter._base_url == "https://example.com/v3"

    def test_default_session_created(self):
        adapter = EtsyAdapter(api_key="key", base_url=ETSY_BASE)
        assert isinstance(adapter._session, requests.Session)

    def test_custom_session_used(self):
        session = requests.Session()
        adapter = EtsyAdapter(api_key="key", base_url=ETSY_BASE, session=session)
        assert adapter._session is session


# ===================================================================
# Property tests
# ===================================================================


class TestEtsyAdapterProperties:
    """EtsyAdapter name/display_name/supports_download properties."""

    def test_name(self, etsy_adapter):
        assert etsy_adapter.name == "etsy"

    def test_display_name(self, etsy_adapter):
        assert etsy_adapter.display_name == "Etsy"

    def test_supports_download_true(self, etsy_adapter):
        assert etsy_adapter.supports_download is True


# ===================================================================
# Search tests
# ===================================================================


class TestEtsyAdapterSearch:
    """EtsyAdapter.search() — maps Etsy listings to ModelSummary."""

    @responses.activate
    def test_search_success(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json=ETSY_SEARCH_RESPONSE,
        )
        results = etsy_adapter.search("benchy")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "12345"
        assert r.name == "3D Benchy STL File"
        assert r.source == "etsy"
        assert r.creator == "PrintShop3D"
        assert r.like_count == 42
        assert r.download_count == 1000
        assert r.thumbnail == "https://i.etsystatic.com/thumb_570.jpg"
        assert r.price_cents == 499
        assert r.is_free is False
        assert r.can_download is True

    @responses.activate
    def test_search_passes_params(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"keywords": "benchy STL 3D print digital download",
                 "limit": "5", "offset": "5", "sort_on": "relevancy"},
                strict_match=False,
            )],
        )
        etsy_adapter.search("benchy", page=2, per_page=5, sort="relevant")

    @responses.activate
    def test_search_sort_mapping_newest(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"sort_on": "created"},
                strict_match=False,
            )],
        )
        etsy_adapter.search("test", sort="newest")

    @responses.activate
    def test_search_sort_mapping_popular(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"sort_on": "most_relevant"},
                strict_match=False,
            )],
        )
        etsy_adapter.search("test", sort="popular")

    @responses.activate
    def test_search_empty_response(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json={"results": []},
        )
        assert etsy_adapter.search("noresults") == []

    @responses.activate
    def test_search_per_page_capped_at_100(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"limit": "100"},
                strict_match=False,
            )],
        )
        etsy_adapter.search("test", per_page=999)

    @responses.activate
    def test_search_non_dict_data_returns_empty(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            json=["not", "a", "dict"],
        )
        assert etsy_adapter.search("test") == []

    @responses.activate
    def test_search_sends_api_key_header(self, etsy_adapter):
        def check_headers(request):
            assert request.headers["x-api-key"] == ETSY_API_KEY
            assert request.headers["Accept"] == "application/json"
            return (200, {}, '{"results": []}')

        responses.add_callback(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            callback=check_headers,
            content_type="application/json",
        )
        etsy_adapter.search("test")


# ===================================================================
# GetDetails tests
# ===================================================================


class TestEtsyAdapterGetDetails:
    """EtsyAdapter.get_details() — maps Etsy listing to ModelDetail."""

    @responses.activate
    def test_get_details_success(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/12345",
            json=ETSY_DETAIL_RESPONSE,
        )
        detail = etsy_adapter.get_details("12345")
        assert isinstance(detail, ModelDetail)
        assert detail.id == "12345"
        assert detail.name == "3D Benchy STL File"
        assert detail.source == "etsy"
        assert detail.creator == "PrintShop3D"
        assert detail.description == "High-quality 3D Benchy model for calibration."
        assert detail.like_count == 42
        assert detail.download_count == 1000
        assert detail.category == "3D Printing"
        assert detail.tags == ["stl", "3d print", "benchy"]
        assert detail.file_count == 3
        assert detail.price_cents == 499
        assert detail.is_free is False
        assert detail.can_download is True
        assert detail.thumbnail == "https://i.etsystatic.com/thumb_570.jpg"

    @responses.activate
    def test_get_details_not_found(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError):
            etsy_adapter.get_details("999")


# ===================================================================
# GetFiles tests
# ===================================================================


class TestEtsyAdapterGetFiles:
    """EtsyAdapter.get_files() — maps Etsy files to ModelFile."""

    @responses.activate
    def test_get_files_success(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/12345/files",
            json=ETSY_FILES_RESPONSE,
        )
        files = etsy_adapter.get_files("12345")
        assert len(files) == 2

        stl = files[0]
        assert isinstance(stl, ModelFile)
        assert stl.id == "100"
        assert stl.name == "benchy.stl"
        assert stl.file_type == "stl"
        assert stl.size_bytes == 256000
        assert stl.download_url == "https://files.etsy.com/dl/100"

        f3mf = files[1]
        assert f3mf.id == "101"
        assert f3mf.name == "benchy_supports.3mf"
        assert f3mf.file_type == "3mf"
        assert f3mf.size_bytes == 512000

    @responses.activate
    def test_get_files_empty(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/12345/files",
            json={"results": []},
        )
        assert etsy_adapter.get_files("12345") == []

    @responses.activate
    def test_get_files_non_dict_results_skipped(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/12345/files",
            json={"results": ["not_a_dict", {"listing_file_id": 1, "filename": "f.stl"}]},
        )
        files = etsy_adapter.get_files("12345")
        assert len(files) == 1
        assert files[0].name == "f.stl"


# ===================================================================
# Download tests
# ===================================================================


class TestEtsyAdapterDownload:
    """EtsyAdapter.download_file() — fetches file metadata then downloads."""

    @responses.activate
    def test_download_success(self, etsy_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json=ETSY_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://files.etsy.com/dl/100",
            body=b"FAKE_STL_BYTES",
            content_type="application/octet-stream",
        )
        path = etsy_adapter.download_file("100", str(tmp_path))
        assert Path(path).exists()
        assert Path(path).name == "benchy.stl"
        assert Path(path).read_bytes() == b"FAKE_STL_BYTES"

    @responses.activate
    def test_download_custom_filename(self, etsy_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json=ETSY_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://files.etsy.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = etsy_adapter.download_file("100", str(tmp_path), file_name="custom.stl")
        assert Path(path).name == "custom.stl"

    @responses.activate
    def test_download_no_url_raises(self, etsy_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json={"listing_file_id": 100, "filename": "f.stl", "url": ""},
        )
        with pytest.raises(MarketplaceError, match="No download URL"):
            etsy_adapter.download_file("100", str(tmp_path))

    @responses.activate
    def test_download_fallback_download_url_field(self, etsy_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json={"listing_file_id": 100, "filename": "f.stl",
                  "download_url": "https://files.etsy.com/dl/100"},
        )
        responses.add(
            responses.GET,
            "https://files.etsy.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = etsy_adapter.download_file("100", str(tmp_path))
        assert Path(path).exists()

    @responses.activate
    def test_download_creates_dir(self, etsy_adapter, tmp_path):
        dest = tmp_path / "sub" / "dir"
        assert not dest.exists()
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json=ETSY_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://files.etsy.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = etsy_adapter.download_file("100", str(dest))
        assert Path(path).exists()

    @responses.activate
    def test_download_fallback_filename(self, etsy_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/files/100",
            json={"listing_file_id": 100, "url": "https://files.etsy.com/dl/100"},
        )
        responses.add(
            responses.GET,
            "https://files.etsy.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = etsy_adapter.download_file("100", str(tmp_path))
        assert Path(path).name == "file_100"


# ===================================================================
# Error handling tests
# ===================================================================


class TestEtsyAdapterErrors:
    """EtsyAdapter HTTP error handling."""

    @responses.activate
    def test_401_raises_auth_error(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            status=401,
            json={"error": "Unauthorized"},
        )
        with pytest.raises(MarketplaceAuthError, match="Invalid or expired"):
            etsy_adapter.search("test")

    @responses.activate
    def test_404_raises_not_found(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            etsy_adapter.get_details("999")

    @responses.activate
    def test_429_raises_rate_limit(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            status=429,
            json={"error": "Too many requests"},
        )
        with pytest.raises(MarketplaceRateLimitError, match="rate limit"):
            etsy_adapter.search("test")

    @responses.activate
    def test_500_raises_generic_error(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            status=500,
            body="Internal Server Error",
        )
        with pytest.raises(MarketplaceError, match="500"):
            etsy_adapter.search("test")

    @responses.activate
    def test_connection_error(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(MarketplaceError, match="Connection"):
            etsy_adapter.search("test")

    @responses.activate
    def test_timeout_error(self, etsy_adapter):
        responses.add(
            responses.GET,
            f"{ETSY_BASE}/application/listings/active",
            body=requests.Timeout("timed out"),
        )
        with pytest.raises(MarketplaceError, match="timed out"):
            etsy_adapter.search("test")


# ===================================================================
# Parse helper edge cases
# ===================================================================


class TestEtsyParseHelpers:
    """EtsyAdapter static parse helpers — edge cases."""

    def test_parse_summary_free_item(self):
        data = {
            "listing_id": 1, "title": "Free Model", "url": "/m",
            "is_digital": True,
            "shop": {"shop_name": "FreeShop"},
            "price": {"amount": 0, "divisor": 100},
            "tags": ["stl"],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.is_free is True
        assert s.price_cents == 0

    def test_parse_summary_no_shop(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": None,
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.creator == ""

    def test_parse_summary_no_images(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "images": [],
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.thumbnail is None

    def test_parse_summary_fallback_thumbnail(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "images": [{"url_170x135": "https://thumb.jpg"}],
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.thumbnail == "https://thumb.jpg"

    def test_parse_summary_non_digital(self):
        data = {
            "listing_id": 1, "title": "Physical Item", "url": "/p",
            "is_digital": False,
            "shop": {"shop_name": "S"},
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.can_download is False

    def test_parse_summary_price_non_100_divisor(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "price": {"amount": 1500, "divisor": 1000},
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.price_cents == 150

    def test_parse_summary_price_not_dict(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "price": "invalid",
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.price_cents == 0

    def test_parse_summary_shop_name_fallback(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {},
            "shop_name": "TopLevelShop",
            "tags": [],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.creator == "TopLevelShop"

    def test_parse_summary_printable_tags(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "tags": ["STL", "3d print"],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.has_sliceable_files is True

    def test_parse_summary_no_printable_tags(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "tags": ["jewelry", "earring"],
        }
        s = EtsyAdapter._parse_summary(data)
        assert s.has_sliceable_files is False

    def test_parse_detail_taxonomy_path_as_dicts(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "taxonomy_path": [{"name": "Supplies"}, {"name": "3D Printing"}],
            "tags": [],
        }
        d = EtsyAdapter._parse_detail(data)
        assert d.category == "3D Printing"

    def test_parse_detail_empty_taxonomy_path(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "taxonomy_path": [],
            "tags": [],
        }
        d = EtsyAdapter._parse_detail(data)
        assert d.category is None

    def test_parse_detail_taxonomy_path_none(self):
        data = {
            "listing_id": 1, "title": "X", "url": "/x",
            "is_digital": True,
            "shop": {"shop_name": "S"},
            "taxonomy_path": None,
            "tags": [],
        }
        d = EtsyAdapter._parse_detail(data)
        assert d.category is None

    def test_parse_file_no_extension(self):
        data = {"listing_file_id": 1, "filename": "noext", "size": 100}
        f = EtsyAdapter._parse_file(data)
        assert f.file_type == ""
        assert f.name == "noext"

    def test_parse_file_fallback_to_name_field(self):
        data = {"id": 99, "name": "model.obj", "file_size": 200}
        f = EtsyAdapter._parse_file(data)
        assert f.id == "99"
        assert f.name == "model.obj"
        assert f.file_type == "obj"
        assert f.size_bytes == 200

    def test_parse_file_download_url_fallback(self):
        data = {
            "listing_file_id": 1, "filename": "f.stl",
            "download_url": "https://example.com/dl",
        }
        f = EtsyAdapter._parse_file(data)
        assert f.download_url == "https://example.com/dl"
