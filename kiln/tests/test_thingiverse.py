"""Tests for the Thingiverse API client (kiln.thingiverse)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses
from responses import matchers

from kiln.thingiverse import (
    Category,
    ThingDetail,
    ThingFile,
    ThingSummary,
    ThingiverseAuthError,
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
    ThingiverseRateLimitError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE = "https://api.thingiverse.com"
TOKEN = "test-token-abc123"


@pytest.fixture
def client():
    """Return a ThingiverseClient with a test token."""
    return ThingiverseClient(token=TOKEN)


# ---------------------------------------------------------------------------
# Helpers — sample API responses
# ---------------------------------------------------------------------------

SAMPLE_THING_SUMMARY = {
    "id": 123,
    "name": "Benchy",
    "public_url": "https://www.thingiverse.com/thing:123",
    "creator": {"name": "testuser"},
    "thumbnail": "https://cdn.thingiverse.com/thumb.jpg",
    "like_count": 42,
    "download_count": 1000,
    "collect_count": 50,
}

SAMPLE_THING_DETAIL = {
    **SAMPLE_THING_SUMMARY,
    "description": "A benchmark boat.",
    "instructions": "Print with PLA at 0.2mm.",
    "license": "Creative Commons - Attribution",
    "category": "3D Printing",
    "tags": [{"name": "benchy"}, {"name": "benchmark"}],
    "file_count": 2,
}

SAMPLE_FILE = {
    "id": 456,
    "name": "benchy.stl",
    "size": 123456,
    "download_url": f"{BASE}/files/456/download",
    "thumbnail": "https://cdn.thingiverse.com/file_thumb.jpg",
    "date": "2024-01-15T12:00:00+00:00",
}

SAMPLE_CATEGORY = {
    "name": "3D Printing",
    "slug": "3d-printing",
    "url": "/categories/3d-printing",
    "count": 500,
}


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestThingiverseClientInit:
    def test_token_required(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_THINGIVERSE_TOKEN", None)
            with pytest.raises(ThingiverseAuthError, match="token is required"):
                ThingiverseClient(token="")

    def test_token_from_env(self):
        with patch.dict(os.environ, {"KILN_THINGIVERSE_TOKEN": "env-tok"}):
            c = ThingiverseClient()
            assert c._token == "env-tok"

    def test_explicit_token_overrides_env(self):
        with patch.dict(os.environ, {"KILN_THINGIVERSE_TOKEN": "env-tok"}):
            c = ThingiverseClient(token="explicit")
            assert c._token == "explicit"

    def test_custom_base_url(self):
        c = ThingiverseClient(token=TOKEN, base_url="http://localhost:9999/")
        assert c._base_url == "http://localhost:9999"  # trailing slash stripped


# ---------------------------------------------------------------------------
# Request helper tests
# ---------------------------------------------------------------------------


class TestRequestHelper:
    @responses.activate
    def test_auth_error_401(self, client):
        responses.add(responses.GET, f"{BASE}/test", status=401, json={"error": "Unauthorized"})
        with pytest.raises(ThingiverseAuthError, match="Invalid or expired"):
            client._request("GET", "/test")

    @responses.activate
    def test_not_found_404(self, client):
        responses.add(responses.GET, f"{BASE}/things/999", status=404, json={"error": "Not found"})
        with pytest.raises(ThingiverseNotFoundError, match="not found"):
            client._request("GET", "/things/999")

    @responses.activate
    def test_rate_limit_429(self, client):
        responses.add(responses.GET, f"{BASE}/test", status=429, json={"error": "Rate limited"})
        with pytest.raises(ThingiverseRateLimitError, match="rate limit"):
            client._request("GET", "/test")

    @responses.activate
    def test_server_error_500(self, client):
        responses.add(responses.GET, f"{BASE}/test", status=500, body="Internal Server Error")
        with pytest.raises(ThingiverseError, match="500"):
            client._request("GET", "/test")

    @responses.activate
    def test_connection_error(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/test",
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(ThingiverseError, match="Connection"):
            client._request("GET", "/test")

    @responses.activate
    def test_token_sent_as_param(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/test",
            json={"ok": True},
            match=[matchers.query_param_matcher({"access_token": TOKEN}, strict_match=False)],
        )
        result = client._request("GET", "/test")
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    @responses.activate
    def test_search_with_hits_key(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/search/benchy",
            json={"hits": [SAMPLE_THING_SUMMARY]},
        )
        results = client.search("benchy")
        assert len(results) == 1
        assert results[0].id == 123
        assert results[0].name == "Benchy"
        assert results[0].creator == "testuser"

    @responses.activate
    def test_search_with_list_response(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/search/case",
            json=[SAMPLE_THING_SUMMARY],
        )
        results = client.search("case")
        assert len(results) == 1

    @responses.activate
    def test_search_empty(self, client):
        responses.add(responses.GET, f"{BASE}/search/zzznoresults", json={"hits": []})
        results = client.search("zzznoresults")
        assert results == []

    @responses.activate
    def test_search_pagination_params(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/search/test",
            json={"hits": []},
            match=[matchers.query_param_matcher(
                {"access_token": TOKEN, "page": "2", "per_page": "5", "sort": "popular"},
                strict_match=False,
            )],
        )
        client.search("test", page=2, per_page=5, sort="popular")

    @responses.activate
    def test_search_per_page_capped_at_100(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/search/test",
            json={"hits": []},
            match=[matchers.query_param_matcher(
                {"per_page": "100"},
                strict_match=False,
            )],
        )
        client.search("test", per_page=999)

    @responses.activate
    def test_search_creator_as_string(self, client):
        item = {**SAMPLE_THING_SUMMARY, "creator": "plain-string-user"}
        responses.add(responses.GET, f"{BASE}/search/test", json=[item])
        results = client.search("test")
        assert results[0].creator == "plain-string-user"


# ---------------------------------------------------------------------------
# Thing detail
# ---------------------------------------------------------------------------


class TestGetThing:
    @responses.activate
    def test_get_thing_success(self, client):
        responses.add(responses.GET, f"{BASE}/things/123", json=SAMPLE_THING_DETAIL)
        thing = client.get_thing(123)
        assert isinstance(thing, ThingDetail)
        assert thing.id == 123
        assert thing.name == "Benchy"
        assert thing.description == "A benchmark boat."
        assert thing.tags == ["benchy", "benchmark"]
        assert thing.file_count == 2

    @responses.activate
    def test_get_thing_not_found(self, client):
        responses.add(responses.GET, f"{BASE}/things/999", status=404, json={})
        with pytest.raises(ThingiverseNotFoundError):
            client.get_thing(999)

    @responses.activate
    def test_get_thing_tags_as_strings(self, client):
        detail = {**SAMPLE_THING_DETAIL, "tags": ["tag-a", "tag-b"]}
        responses.add(responses.GET, f"{BASE}/things/123", json=detail)
        thing = client.get_thing(123)
        assert thing.tags == ["tag-a", "tag-b"]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestGetFiles:
    @responses.activate
    def test_get_files_success(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/things/123/files",
            json=[SAMPLE_FILE],
        )
        files = client.get_files(123)
        assert len(files) == 1
        assert files[0].id == 456
        assert files[0].name == "benchy.stl"
        assert files[0].size_bytes == 123456

    @responses.activate
    def test_get_files_empty(self, client):
        responses.add(responses.GET, f"{BASE}/things/123/files", json=[])
        files = client.get_files(123)
        assert files == []

    @responses.activate
    def test_get_files_non_list_response(self, client):
        responses.add(responses.GET, f"{BASE}/things/123/files", json={"error": "weird"})
        files = client.get_files(123)
        assert files == []


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class TestDownloadFile:
    @responses.activate
    def test_download_success(self, client, tmp_path):
        responses.add(
            responses.GET,
            f"{BASE}/files/456",
            json={"id": 456, "name": "benchy.stl", "download_url": f"{BASE}/files/456/download"},
        )
        responses.add(
            responses.GET,
            f"{BASE}/files/456/download",
            body=b"FAKE_STL_DATA",
            content_type="application/octet-stream",
        )
        path = client.download_file(456, str(tmp_path))
        assert Path(path).exists()
        assert Path(path).name == "benchy.stl"
        assert Path(path).read_bytes() == b"FAKE_STL_DATA"

    @responses.activate
    def test_download_custom_name(self, client, tmp_path):
        responses.add(
            responses.GET,
            f"{BASE}/files/456",
            json={"id": 456, "name": "benchy.stl", "download_url": f"{BASE}/files/456/download"},
        )
        responses.add(
            responses.GET,
            f"{BASE}/files/456/download",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = client.download_file(456, str(tmp_path), file_name="custom.stl")
        assert Path(path).name == "custom.stl"

    @responses.activate
    def test_download_creates_dest_dir(self, client, tmp_path):
        dest = tmp_path / "sub" / "dir"
        assert not dest.exists()
        responses.add(
            responses.GET,
            f"{BASE}/files/456",
            json={"id": 456, "name": "f.stl", "download_url": f"{BASE}/files/456/download"},
        )
        responses.add(
            responses.GET,
            f"{BASE}/files/456/download",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = client.download_file(456, str(dest))
        assert Path(path).exists()

    @responses.activate
    def test_download_no_url(self, client, tmp_path):
        responses.add(
            responses.GET,
            f"{BASE}/files/456",
            json={"id": 456, "name": "f.stl", "download_url": ""},
        )
        with pytest.raises(ThingiverseError, match="No download URL"):
            client.download_file(456, str(tmp_path))

    @responses.activate
    def test_download_not_found(self, client, tmp_path):
        responses.add(responses.GET, f"{BASE}/files/999", status=404, json={})
        with pytest.raises(ThingiverseNotFoundError):
            client.download_file(999, str(tmp_path))

    @responses.activate
    def test_download_fallback_name(self, client, tmp_path):
        responses.add(
            responses.GET,
            f"{BASE}/files/456",
            json={"id": 456, "download_url": f"{BASE}/files/456/download"},
        )
        responses.add(
            responses.GET,
            f"{BASE}/files/456/download",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = client.download_file(456, str(tmp_path))
        assert "file_456" in Path(path).name


# ---------------------------------------------------------------------------
# Browse — popular / newest / featured
# ---------------------------------------------------------------------------


class TestBrowse:
    @responses.activate
    def test_popular(self, client):
        responses.add(responses.GET, f"{BASE}/popular", json=[SAMPLE_THING_SUMMARY])
        results = client.popular()
        assert len(results) == 1
        assert results[0].id == 123

    @responses.activate
    def test_newest(self, client):
        responses.add(responses.GET, f"{BASE}/newest", json=[SAMPLE_THING_SUMMARY])
        results = client.newest()
        assert len(results) == 1

    @responses.activate
    def test_featured(self, client):
        responses.add(responses.GET, f"{BASE}/featured", json=[SAMPLE_THING_SUMMARY])
        results = client.featured()
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategories:
    @responses.activate
    def test_list_categories(self, client):
        responses.add(responses.GET, f"{BASE}/categories", json=[SAMPLE_CATEGORY])
        cats = client.list_categories()
        assert len(cats) == 1
        assert cats[0].name == "3D Printing"
        assert cats[0].slug == "3d-printing"

    @responses.activate
    def test_list_categories_empty(self, client):
        responses.add(responses.GET, f"{BASE}/categories", json=[])
        cats = client.list_categories()
        assert cats == []

    @responses.activate
    def test_list_categories_non_list(self, client):
        responses.add(responses.GET, f"{BASE}/categories", json={"error": "bad"})
        cats = client.list_categories()
        assert cats == []

    @responses.activate
    def test_category_things(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/categories/3d-printing/things",
            json=[SAMPLE_THING_SUMMARY],
        )
        results = client.category_things("3d-printing")
        assert len(results) == 1

    @responses.activate
    def test_category_slug_generated_from_name(self, client):
        cat_data = {"name": "Some Category", "url": "/cat", "count": 10}
        responses.add(responses.GET, f"{BASE}/categories", json=[cat_data])
        cats = client.list_categories()
        assert cats[0].slug == "some-category"


# ---------------------------------------------------------------------------
# Dataclass serialisation
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_thing_summary_to_dict(self):
        s = ThingSummary(id=1, name="T", url="/t", creator="u")
        d = s.to_dict()
        assert d["id"] == 1
        assert d["name"] == "T"

    def test_thing_detail_to_dict(self):
        t = ThingDetail(id=1, name="T", url="/t", creator="u", tags=["a", "b"])
        d = t.to_dict()
        assert d["tags"] == ["a", "b"]

    def test_thing_file_to_dict(self):
        f = ThingFile(id=1, name="f.stl", size_bytes=100, download_url="/dl")
        d = f.to_dict()
        assert d["size_bytes"] == 100

    def test_category_to_dict(self):
        c = Category(name="Art", slug="art", url="/art", count=10)
        d = c.to_dict()
        assert d["slug"] == "art"


# ---------------------------------------------------------------------------
# Parse helpers edge cases
# ---------------------------------------------------------------------------


class TestParseHelpers:
    def test_parse_thing_list_dict_with_results_key(self):
        data = {"results": [SAMPLE_THING_SUMMARY]}
        results = ThingiverseClient._parse_thing_list(data)
        assert len(results) == 1

    def test_parse_thing_list_non_dict_items_skipped(self):
        data = [SAMPLE_THING_SUMMARY, "bad", 42, None]
        results = ThingiverseClient._parse_thing_list(data)
        assert len(results) == 1

    def test_parse_thing_list_unexpected_type(self):
        results = ThingiverseClient._parse_thing_list("unexpected")
        assert results == []

    def test_parse_thing_detail_missing_fields(self):
        detail = ThingiverseClient._parse_thing_detail({})
        assert detail.id == 0
        assert detail.name == ""
        assert detail.tags == []

    def test_parse_file_missing_fields(self):
        f = ThingiverseClient._parse_file({})
        assert f.id == 0
        assert f.name == ""
        assert f.size_bytes == 0
