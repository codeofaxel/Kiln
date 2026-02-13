"""Tests for the Thangs marketplace adapter (kiln.marketplaces.thangs)."""

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
from kiln.marketplaces.thangs import ThangsAdapter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THANGS_BASE = "https://thangs-test.local/v1"
THANGS_API_KEY = "test-thangs-key-abc"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def thangs_adapter():
    """ThangsAdapter configured with test base URL."""
    return ThangsAdapter(api_key=THANGS_API_KEY, base_url=THANGS_BASE)


# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------

THANGS_SEARCH_RESPONSE = {
    "results": [
        {
            "id": 42,
            "name": "Benchy",
            "url": "https://thangs.com/models/42",
            "creator": {"name": "TestCreator", "username": "testcreator"},
            "thumbnail_url": "https://cdn.thangs.com/thumb42.jpg",
            "likes_count": 150,
            "downloads_count": 3000,
            "license": "CC-BY",
            "files": [
                {"file_name": "benchy.stl"},
            ],
        },
    ],
}

THANGS_DETAIL_RESPONSE = {
    "id": 42,
    "name": "Benchy",
    "url": "https://thangs.com/models/42",
    "creator": {"name": "TestCreator", "username": "testcreator"},
    "description": "A classic 3D printing benchmark.",
    "instructions": "Print with 0.2mm layer height.",
    "license": "CC-BY",
    "thumbnail_url": "https://cdn.thangs.com/thumb42.jpg",
    "likes_count": 150,
    "downloads_count": 3000,
    "category": {"name": "3D Printing"},
    "tags": [{"name": "benchy"}, {"name": "benchmark"}],
    "files": [{"id": 100}, {"id": 101}],
}

THANGS_FILES_RESPONSE = {
    "files": [
        {
            "id": 100,
            "file_name": "benchy_body.stl",
            "size": 256000,
            "download_url": "https://cdn.thangs.com/dl/100",
            "preview_url": "https://cdn.thangs.com/fp100.jpg",
        },
        {
            "id": 101,
            "file_name": "benchy_base.3mf",
            "size": 128000,
            "download_url": "https://cdn.thangs.com/dl/101",
        },
    ],
}

THANGS_FILE_META_RESPONSE = {
    "id": 100,
    "file_name": "benchy_body.stl",
    "download_url": "https://cdn.thangs.com/dl/100",
}


# ===================================================================
# Init tests
# ===================================================================


class TestThangsAdapterInit:
    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_THANGS_API_KEY", None)
            with pytest.raises(MarketplaceAuthError, match="API key is required"):
                ThangsAdapter(api_key="")

    def test_api_key_from_env(self):
        with patch.dict(os.environ, {"KILN_THANGS_API_KEY": "env-key"}):
            adapter = ThangsAdapter(base_url=THANGS_BASE)
            assert adapter._api_key == "env-key"

    def test_explicit_key_overrides_env(self):
        with patch.dict(os.environ, {"KILN_THANGS_API_KEY": "env-key"}):
            adapter = ThangsAdapter(api_key="explicit", base_url=THANGS_BASE)
            assert adapter._api_key == "explicit"

    def test_base_url_trailing_slash_stripped(self):
        adapter = ThangsAdapter(api_key="key", base_url="https://example.com/v1/")
        assert adapter._base_url == "https://example.com/v1"


# ===================================================================
# Properties tests
# ===================================================================


class TestThangsAdapterProperties:
    def test_name(self, thangs_adapter):
        assert thangs_adapter.name == "thangs"

    def test_display_name(self, thangs_adapter):
        assert thangs_adapter.display_name == "Thangs"

    def test_supports_download_true(self, thangs_adapter):
        assert thangs_adapter.supports_download is True


# ===================================================================
# Search tests
# ===================================================================


class TestThangsAdapterSearch:
    @responses.activate
    def test_search_success(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            json=THANGS_SEARCH_RESPONSE,
        )
        results = thangs_adapter.search("benchy")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "42"
        assert r.name == "Benchy"
        assert r.source == "thangs"
        assert r.creator == "TestCreator"
        assert r.like_count == 150
        assert r.download_count == 3000
        assert r.license == "CC-BY"
        assert r.thumbnail == "https://cdn.thangs.com/thumb42.jpg"

    @responses.activate
    def test_search_passes_params(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"q": "dragon", "page": "2", "per_page": "5", "sort": "relevance"},
                strict_match=False,
            )],
        )
        thangs_adapter.search("dragon", page=2, per_page=5, sort="relevant")

    @responses.activate
    def test_search_sort_mapping_popular(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"sort": "popularity"},
                strict_match=False,
            )],
        )
        thangs_adapter.search("test", sort="popular")

    @responses.activate
    def test_search_sort_mapping_newest(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"sort": "date"},
                strict_match=False,
            )],
        )
        thangs_adapter.search("test", sort="newest")

    @responses.activate
    def test_search_empty_response(self, thangs_adapter):
        responses.add(responses.GET, f"{THANGS_BASE}/search", json={"results": []})
        assert thangs_adapter.search("noresults") == []

    @responses.activate
    def test_search_per_page_capped(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            json={"results": []},
            match=[matchers.query_param_matcher(
                {"per_page": "100"},
                strict_match=False,
            )],
        )
        thangs_adapter.search("test", per_page=999)

    @responses.activate
    def test_search_sends_bearer_auth(self, thangs_adapter):
        def check_auth(request):
            assert request.headers["Authorization"] == f"Bearer {THANGS_API_KEY}"
            return (200, {}, '{"results": []}')

        responses.add_callback(
            responses.GET,
            f"{THANGS_BASE}/search",
            callback=check_auth,
            content_type="application/json",
        )
        thangs_adapter.search("test")

    @responses.activate
    def test_search_non_dict_data(self, thangs_adapter):
        responses.add(responses.GET, f"{THANGS_BASE}/search", json=[])
        assert thangs_adapter.search("test") == []


# ===================================================================
# GetDetails tests
# ===================================================================


class TestThangsAdapterGetDetails:
    @responses.activate
    def test_get_details_success(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/42",
            json=THANGS_DETAIL_RESPONSE,
        )
        detail = thangs_adapter.get_details("42")
        assert isinstance(detail, ModelDetail)
        assert detail.id == "42"
        assert detail.name == "Benchy"
        assert detail.source == "thangs"
        assert detail.description == "A classic 3D printing benchmark."
        assert detail.instructions == "Print with 0.2mm layer height."
        assert detail.license == "CC-BY"
        assert detail.category == "3D Printing"
        assert detail.tags == ["benchy", "benchmark"]
        assert detail.file_count == 2
        assert detail.like_count == 150
        assert detail.download_count == 3000

    @responses.activate
    def test_get_details_category_as_string(self, thangs_adapter):
        data = dict(THANGS_DETAIL_RESPONSE, category="Art")
        responses.add(responses.GET, f"{THANGS_BASE}/models/42", json=data)
        detail = thangs_adapter.get_details("42")
        assert detail.category == "Art"

    @responses.activate
    def test_get_details_no_category(self, thangs_adapter):
        data = dict(THANGS_DETAIL_RESPONSE, category="")
        responses.add(responses.GET, f"{THANGS_BASE}/models/42", json=data)
        detail = thangs_adapter.get_details("42")
        assert detail.category is None

    @responses.activate
    def test_get_details_tags_as_strings(self, thangs_adapter):
        data = dict(THANGS_DETAIL_RESPONSE, tags=["alpha", "beta"])
        responses.add(responses.GET, f"{THANGS_BASE}/models/42", json=data)
        detail = thangs_adapter.get_details("42")
        assert detail.tags == ["alpha", "beta"]

    @responses.activate
    def test_get_details_not_found(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            thangs_adapter.get_details("999")


# ===================================================================
# GetFiles tests
# ===================================================================


class TestThangsAdapterGetFiles:
    @responses.activate
    def test_get_files_success(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/42/files",
            json=THANGS_FILES_RESPONSE,
        )
        files = thangs_adapter.get_files("42")
        assert len(files) == 2

        stl = files[0]
        assert isinstance(stl, ModelFile)
        assert stl.id == "100"
        assert stl.name == "benchy_body.stl"
        assert stl.file_type == "stl"
        assert stl.size_bytes == 256000
        assert stl.download_url == "https://cdn.thangs.com/dl/100"
        assert stl.thumbnail_url == "https://cdn.thangs.com/fp100.jpg"

        mf = files[1]
        assert mf.id == "101"
        assert mf.name == "benchy_base.3mf"
        assert mf.file_type == "3mf"
        assert mf.size_bytes == 128000

    @responses.activate
    def test_get_files_empty(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/42/files",
            json={"files": []},
        )
        assert thangs_adapter.get_files("42") == []

    @responses.activate
    def test_get_files_non_dict_data(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/42/files",
            json=[],
        )
        assert thangs_adapter.get_files("42") == []


# ===================================================================
# Download tests
# ===================================================================


class TestThangsAdapterDownload:
    @responses.activate
    def test_download_success(self, thangs_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/files/100",
            json=THANGS_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://cdn.thangs.com/dl/100",
            body=b"FAKE_STL_BYTES",
            content_type="application/octet-stream",
        )
        path = thangs_adapter.download_file("100", str(tmp_path))
        assert Path(path).exists()
        assert Path(path).name == "benchy_body.stl"
        assert Path(path).read_bytes() == b"FAKE_STL_BYTES"

    @responses.activate
    def test_download_custom_filename(self, thangs_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/files/100",
            json=THANGS_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://cdn.thangs.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = thangs_adapter.download_file("100", str(tmp_path), file_name="custom.stl")
        assert Path(path).name == "custom.stl"

    @responses.activate
    def test_download_no_url_raises(self, thangs_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/files/100",
            json={"id": 100, "file_name": "x.stl", "download_url": ""},
        )
        with pytest.raises(MarketplaceError, match="No download URL"):
            thangs_adapter.download_file("100", str(tmp_path))

    @responses.activate
    def test_download_creates_dir(self, thangs_adapter, tmp_path):
        dest = tmp_path / "sub" / "dir"
        assert not dest.exists()
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/files/100",
            json=THANGS_FILE_META_RESPONSE,
        )
        responses.add(
            responses.GET,
            "https://cdn.thangs.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = thangs_adapter.download_file("100", str(dest))
        assert Path(path).exists()

    @responses.activate
    def test_download_fallback_filename(self, thangs_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/files/100",
            json={"id": 100, "download_url": "https://cdn.thangs.com/dl/100"},
        )
        responses.add(
            responses.GET,
            "https://cdn.thangs.com/dl/100",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = thangs_adapter.download_file("100", str(tmp_path))
        assert Path(path).name == "file_100"


# ===================================================================
# Error handling tests
# ===================================================================


class TestThangsAdapterErrors:
    @responses.activate
    def test_401_raises_auth_error(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            status=401,
            json={"error": "Unauthorized"},
        )
        with pytest.raises(MarketplaceAuthError, match="Invalid or expired"):
            thangs_adapter.search("test")

    @responses.activate
    def test_404_raises_not_found(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/models/999",
            status=404,
            json={"error": "Not found"},
        )
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            thangs_adapter.get_details("999")

    @responses.activate
    def test_429_raises_rate_limit(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            status=429,
            json={"error": "Too many"},
        )
        with pytest.raises(MarketplaceRateLimitError, match="rate limit"):
            thangs_adapter.search("test")

    @responses.activate
    def test_500_raises_generic_error(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            status=500,
            body="Internal Server Error",
        )
        with pytest.raises(MarketplaceError, match="500"):
            thangs_adapter.search("test")

    @responses.activate
    def test_connection_error(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(MarketplaceError, match="Connection"):
            thangs_adapter.search("test")

    @responses.activate
    def test_timeout_error(self, thangs_adapter):
        responses.add(
            responses.GET,
            f"{THANGS_BASE}/search",
            body=requests.Timeout("timed out"),
        )
        with pytest.raises(MarketplaceError, match="timed out"):
            thangs_adapter.search("test")


# ===================================================================
# Parse helper tests
# ===================================================================


class TestThangsParseHelpers:
    def test_parse_summary_owner_fallback(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "owner": {"username": "fallback_user"},
        }
        s = ThangsAdapter._parse_summary(data)
        assert s.creator == "fallback_user"

    def test_parse_summary_no_creator(self):
        data = {"id": 1, "name": "X", "url": "/x", "creator": None}
        s = ThangsAdapter._parse_summary(data)
        assert s.creator == ""

    def test_parse_summary_title_fallback(self):
        data = {"id": 1, "title": "Fallback Title", "url": "/x", "creator": {"name": "u"}}
        s = ThangsAdapter._parse_summary(data)
        assert s.name == "Fallback Title"

    def test_parse_summary_preview_url_fallback(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "preview_url": "https://cdn/preview.jpg",
        }
        s = ThangsAdapter._parse_summary(data)
        assert s.thumbnail == "https://cdn/preview.jpg"

    def test_parse_summary_like_count_fallback(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "like_count": 42,
        }
        s = ThangsAdapter._parse_summary(data)
        assert s.like_count == 42

    def test_parse_summary_download_count_fallback(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "download_count": 99,
        }
        s = ThangsAdapter._parse_summary(data)
        assert s.download_count == 99

    def test_parse_detail_tags_as_dicts(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "tags": [{"name": "alpha"}, {"name": "beta"}],
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.tags == ["alpha", "beta"]

    def test_parse_detail_tags_as_strings(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "tags": ["a", "b"],
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.tags == ["a", "b"]

    def test_parse_detail_category_as_dict(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "category": {"name": "Figurines"},
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.category == "Figurines"

    def test_parse_detail_category_as_string(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "category": "Art",
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.category == "Art"

    def test_parse_detail_empty_category(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "category": "",
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.category is None

    def test_parse_detail_null_tags(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "creator": {"name": "u"},
            "tags": None,
            "files": [],
        }
        d = ThangsAdapter._parse_detail(data)
        assert d.tags == []

    def test_parse_file_name_fallback(self):
        data = {"id": 1, "name": "fallback.obj", "size": 100}
        f = ThangsAdapter._parse_file(data)
        assert f.name == "fallback.obj"
        assert f.file_type == "obj"

    def test_parse_file_no_extension(self):
        data = {"id": 1, "file_name": "noext"}
        f = ThangsAdapter._parse_file(data)
        assert f.file_type == ""

    def test_parse_file_size_bytes_fallback(self):
        data = {"id": 1, "file_name": "x.stl", "size_bytes": 512}
        f = ThangsAdapter._parse_file(data)
        assert f.size_bytes == 512

    def test_parse_file_thumbnail_fallback(self):
        data = {"id": 1, "file_name": "x.stl", "thumbnail_url": "https://cdn/t.jpg"}
        f = ThangsAdapter._parse_file(data)
        assert f.thumbnail_url == "https://cdn/t.jpg"
