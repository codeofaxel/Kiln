"""Tests for the marketplace adapter system (kiln.marketplaces)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses
from responses import matchers

from kiln.marketplaces.base import (
    MarketplaceAdapter,
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplaceRateLimitError,
    ModelDetail,
    ModelFile,
    ModelSummary,
)
from kiln.marketplaces.thingiverse import ThingiverseAdapter, _wrap_error
from kiln.marketplaces.myminifactory import MyMiniFactoryAdapter
from kiln.marketplaces.cults3d import Cults3DAdapter
from kiln.marketplaces import (
    MarketplaceHealth,
    MarketplaceHealthMonitor,
    MarketplaceRegistry,
    MarketplaceSearchResults,
    MarketplaceStatus,
)

from kiln.thingiverse import (
    ThingDetail,
    ThingFile as TvThingFile,
    ThingSummary as TvThingSummary,
    ThingiverseAuthError,
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
    ThingiverseRateLimitError,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MMF_BASE = "https://mmf-test.local/api/v2"
MMF_API_KEY = "test-mmf-key-abc"

CULTS_GQL_URL = "https://cults-test.local/graphql"
CULTS_USERNAME = "testuser"
CULTS_API_KEY = "test-cults-key-xyz"


# ===================================================================
# base.py tests
# ===================================================================


class TestModelFileProperties:
    """ModelFile.is_printable and needs_slicing computed properties."""

    def test_gcode_is_printable(self):
        f = ModelFile(id="1", name="part.gcode", size_bytes=100)
        assert f.is_printable is True
        assert f.needs_slicing is False

    def test_gco_is_printable(self):
        f = ModelFile(id="1", name="part.gco", size_bytes=100)
        assert f.is_printable is True

    def test_g_is_printable(self):
        f = ModelFile(id="1", name="part.g", size_bytes=100)
        assert f.is_printable is True

    def test_stl_needs_slicing(self):
        f = ModelFile(id="1", name="model.stl", size_bytes=200)
        assert f.needs_slicing is True
        assert f.is_printable is False

    def test_3mf_needs_slicing(self):
        f = ModelFile(id="1", name="model.3mf", size_bytes=200)
        assert f.needs_slicing is True

    def test_obj_needs_slicing(self):
        f = ModelFile(id="1", name="model.obj", size_bytes=200)
        assert f.needs_slicing is True

    def test_step_needs_slicing(self):
        f = ModelFile(id="1", name="model.step", size_bytes=200)
        assert f.needs_slicing is True

    def test_stp_needs_slicing(self):
        f = ModelFile(id="1", name="model.stp", size_bytes=200)
        assert f.needs_slicing is True

    def test_unknown_extension(self):
        f = ModelFile(id="1", name="readme.txt", size_bytes=50)
        assert f.is_printable is False
        assert f.needs_slicing is False

    def test_no_extension(self):
        f = ModelFile(id="1", name="noext", size_bytes=50)
        assert f.is_printable is False
        assert f.needs_slicing is False

    def test_case_insensitive(self):
        f = ModelFile(id="1", name="Model.STL", size_bytes=200)
        assert f.needs_slicing is True

        f2 = ModelFile(id="2", name="file.GCODE", size_bytes=100)
        assert f2.is_printable is True


class TestModelFileToDict:
    def test_includes_computed_properties(self):
        f = ModelFile(id="1", name="part.stl", size_bytes=100, download_url="/dl")
        d = f.to_dict()
        assert d["id"] == "1"
        assert d["name"] == "part.stl"
        assert d["is_printable"] is False
        assert d["needs_slicing"] is True
        assert d["size_bytes"] == 100
        assert d["download_url"] == "/dl"

    def test_gcode_file_dict(self):
        f = ModelFile(id="2", name="print.gcode", size_bytes=500)
        d = f.to_dict()
        assert d["is_printable"] is True
        assert d["needs_slicing"] is False


class TestModelSummaryToDict:
    def test_to_dict_roundtrip(self):
        s = ModelSummary(
            id="42", name="Benchy", url="/benchy", creator="user1",
            source="thingiverse", like_count=10, download_count=200,
        )
        d = s.to_dict()
        assert d["id"] == "42"
        assert d["name"] == "Benchy"
        assert d["source"] == "thingiverse"
        assert d["like_count"] == 10
        assert d["is_free"] is True
        assert d["price_cents"] == 0


class TestModelDetailToDict:
    def test_to_dict_roundtrip(self):
        detail = ModelDetail(
            id="99", name="Dragon", url="/dragon", creator="artist",
            source="cults3d", tags=["fantasy", "dragon"], file_count=3,
        )
        d = detail.to_dict()
        assert d["id"] == "99"
        assert d["tags"] == ["fantasy", "dragon"]
        assert d["file_count"] == 3
        assert d["can_download"] is True


class TestMarketplaceAdapterDefaultDownload:
    """MarketplaceAdapter.download_file raises by default."""

    def test_default_download_raises(self):
        """A concrete adapter that doesn't override download_file should raise."""

        class StubAdapter(MarketplaceAdapter):
            @property
            def name(self) -> str:
                return "stub"

            @property
            def display_name(self) -> str:
                return "Stub"

            def search(self, query, *, page=1, per_page=20, sort="relevant"):
                return []

            def get_details(self, model_id):
                return ModelDetail(id=model_id, name="", url="", creator="", source="stub")

            def get_files(self, model_id):
                return []

        adapter = StubAdapter()
        with pytest.raises(MarketplaceError, match="does not support direct file downloads"):
            adapter.download_file("1", "/tmp")


class TestMarketplaceExceptions:
    def test_marketplace_error_status_code(self):
        err = MarketplaceError("oops", status_code=500)
        assert str(err) == "oops"
        assert err.status_code == 500

    def test_marketplace_error_no_status(self):
        err = MarketplaceError("oops")
        assert err.status_code is None

    def test_subclass_hierarchy(self):
        assert issubclass(MarketplaceAuthError, MarketplaceError)
        assert issubclass(MarketplaceNotFoundError, MarketplaceError)
        assert issubclass(MarketplaceRateLimitError, MarketplaceError)


# ===================================================================
# ThingiverseAdapter tests
# ===================================================================


class TestThingiverseAdapterProperties:
    def test_name(self):
        client = MagicMock(spec=ThingiverseClient)
        adapter = ThingiverseAdapter(client)
        assert adapter.name == "thingiverse"

    def test_display_name(self):
        client = MagicMock(spec=ThingiverseClient)
        adapter = ThingiverseAdapter(client)
        assert adapter.display_name == "Thingiverse"

    def test_supports_download_true(self):
        client = MagicMock(spec=ThingiverseClient)
        adapter = ThingiverseAdapter(client)
        assert adapter.supports_download is True


class TestThingiverseAdapterSearch:
    def test_search_maps_to_model_summary(self):
        client = MagicMock(spec=ThingiverseClient)
        client.search.return_value = [
            TvThingSummary(
                id=123, name="Benchy", url="https://thingiverse.com/thing:123",
                creator="testuser", thumbnail="https://cdn/thumb.jpg",
                like_count=42, download_count=1000, collect_count=50,
            ),
        ]
        adapter = ThingiverseAdapter(client)
        results = adapter.search("benchy", page=2, per_page=5, sort="popular")

        client.search.assert_called_once_with("benchy", page=2, per_page=5, sort="popular")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "123"
        assert r.name == "Benchy"
        assert r.source == "thingiverse"
        assert r.creator == "testuser"
        assert r.thumbnail == "https://cdn/thumb.jpg"
        assert r.like_count == 42
        assert r.download_count == 1000

    def test_search_empty(self):
        client = MagicMock(spec=ThingiverseClient)
        client.search.return_value = []
        adapter = ThingiverseAdapter(client)
        assert adapter.search("nothing") == []

    def test_search_wraps_error(self):
        client = MagicMock(spec=ThingiverseClient)
        client.search.side_effect = ThingiverseRateLimitError("rate limited", status_code=429)
        adapter = ThingiverseAdapter(client)
        with pytest.raises(MarketplaceRateLimitError, match="rate limited"):
            adapter.search("test")


class TestThingiverseAdapterGetDetails:
    def test_get_details_maps_to_model_detail(self):
        client = MagicMock(spec=ThingiverseClient)
        client.get_thing.return_value = ThingDetail(
            id=123, name="Benchy", url="https://thingiverse.com/thing:123",
            creator="testuser", description="A boat", instructions="Print it",
            license="CC-BY", thumbnail="https://cdn/thumb.jpg",
            like_count=42, download_count=1000, collect_count=50,
            category="3D Printing", tags=["benchy", "test"], file_count=2,
        )
        adapter = ThingiverseAdapter(client)
        detail = adapter.get_details("123")

        client.get_thing.assert_called_once_with(123)
        assert isinstance(detail, ModelDetail)
        assert detail.id == "123"
        assert detail.name == "Benchy"
        assert detail.source == "thingiverse"
        assert detail.description == "A boat"
        assert detail.instructions == "Print it"
        assert detail.license == "CC-BY"
        assert detail.category == "3D Printing"
        assert detail.tags == ["benchy", "test"]
        assert detail.file_count == 2

    def test_get_details_wraps_not_found(self):
        client = MagicMock(spec=ThingiverseClient)
        client.get_thing.side_effect = ThingiverseNotFoundError("not found", status_code=404)
        adapter = ThingiverseAdapter(client)
        with pytest.raises(MarketplaceNotFoundError):
            adapter.get_details("999")


class TestThingiverseAdapterGetFiles:
    def test_get_files_maps_to_model_file(self):
        client = MagicMock(spec=ThingiverseClient)
        client.get_files.return_value = [
            TvThingFile(
                id=456, name="benchy.stl", size_bytes=123456,
                download_url="https://api/files/456/download",
                thumbnail_url="https://cdn/file_thumb.jpg",
                date="2024-01-15T12:00:00+00:00",
            ),
            TvThingFile(
                id=789, name="benchy.gcode", size_bytes=99999,
                download_url="https://api/files/789/download",
            ),
        ]
        adapter = ThingiverseAdapter(client)
        files = adapter.get_files("123")

        client.get_files.assert_called_once_with(123)
        assert len(files) == 2

        stl = files[0]
        assert isinstance(stl, ModelFile)
        assert stl.id == "456"
        assert stl.name == "benchy.stl"
        assert stl.file_type == "stl"
        assert stl.size_bytes == 123456

        gcode = files[1]
        assert gcode.id == "789"
        assert gcode.file_type == "gcode"

    def test_get_files_wraps_error(self):
        client = MagicMock(spec=ThingiverseClient)
        client.get_files.side_effect = ThingiverseAuthError("unauthorized", status_code=401)
        adapter = ThingiverseAdapter(client)
        with pytest.raises(MarketplaceAuthError):
            adapter.get_files("123")


class TestThingiverseAdapterDownload:
    def test_download_delegates_to_client(self, tmp_path):
        client = MagicMock(spec=ThingiverseClient)
        expected_path = str(tmp_path / "benchy.stl")
        client.download_file.return_value = expected_path
        adapter = ThingiverseAdapter(client)

        result = adapter.download_file("456", str(tmp_path), file_name="benchy.stl")

        client.download_file.assert_called_once_with(456, str(tmp_path), file_name="benchy.stl")
        assert result == expected_path

    def test_download_wraps_error(self, tmp_path):
        client = MagicMock(spec=ThingiverseClient)
        client.download_file.side_effect = ThingiverseError("download failed", status_code=500)
        adapter = ThingiverseAdapter(client)
        with pytest.raises(MarketplaceError, match="download failed"):
            adapter.download_file("456", str(tmp_path))


class TestWrapError:
    def test_wraps_auth_error(self):
        exc = ThingiverseAuthError("auth", status_code=401)
        wrapped = _wrap_error(exc)
        assert isinstance(wrapped, MarketplaceAuthError)
        assert wrapped.status_code == 401

    def test_wraps_not_found_error(self):
        exc = ThingiverseNotFoundError("nf", status_code=404)
        wrapped = _wrap_error(exc)
        assert isinstance(wrapped, MarketplaceNotFoundError)

    def test_wraps_rate_limit_error(self):
        exc = ThingiverseRateLimitError("rl", status_code=429)
        wrapped = _wrap_error(exc)
        assert isinstance(wrapped, MarketplaceRateLimitError)

    def test_wraps_generic_error(self):
        exc = ThingiverseError("generic", status_code=500)
        wrapped = _wrap_error(exc)
        assert isinstance(wrapped, MarketplaceError)
        assert not isinstance(wrapped, MarketplaceAuthError)
        assert wrapped.status_code == 500


# ===================================================================
# MyMiniFactoryAdapter tests
# ===================================================================


@pytest.fixture
def mmf_adapter():
    """MyMiniFactoryAdapter configured with test base URL."""
    return MyMiniFactoryAdapter(api_key=MMF_API_KEY, base_url=MMF_BASE)


# Sample MMF API responses
MMF_SEARCH_RESPONSE = {
    "items": [
        {
            "id": 100,
            "name": "Dragon Miniature",
            "url": "https://mmf.io/d/100",
            "designer": {"name": "ArtistOne", "username": "artist1"},
            "images": [{"thumbnail": {"url": "https://cdn.mmf.io/thumb100.jpg"}}],
            "likes": 55,
            "views": 3000,
            "licenses": [{"value": "CC-BY-NC"}],
        },
    ],
}

MMF_DETAIL_RESPONSE = {
    "id": 100,
    "name": "Dragon Miniature",
    "url": "https://mmf.io/d/100",
    "designer": {"name": "ArtistOne", "username": "artist1"},
    "description": "A detailed dragon figurine.",
    "printing_details": "Print with supports enabled.",
    "images": [{"thumbnail": {"url": "https://cdn.mmf.io/thumb100.jpg"}}],
    "likes": 55,
    "views": 3000,
    "licenses": [{"value": "CC-BY-NC"}],
    "tags": [{"name": "dragon"}, {"name": "miniature"}],
    "categories": [{"name": "Tabletop"}],
    "files": [{"id": 200}, {"id": 201}],
}

MMF_FILES_RESPONSE = {
    "items": [
        {
            "id": 200,
            "filename": "dragon_body.stl",
            "size": 512000,
            "download_url": "https://cdn.mmf.io/dl/200",
            "thumbnail_url": "https://cdn.mmf.io/ft200.jpg",
        },
    ],
}


class TestMyMiniFactoryAdapterInit:
    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_MMF_API_KEY", None)
            with pytest.raises(MarketplaceAuthError, match="API key is required"):
                MyMiniFactoryAdapter(api_key="")

    def test_api_key_from_env(self):
        with patch.dict(os.environ, {"KILN_MMF_API_KEY": "env-key"}):
            adapter = MyMiniFactoryAdapter(base_url=MMF_BASE)
            assert adapter._api_key == "env-key"

    def test_explicit_key_overrides_env(self):
        with patch.dict(os.environ, {"KILN_MMF_API_KEY": "env-key"}):
            adapter = MyMiniFactoryAdapter(api_key="explicit", base_url=MMF_BASE)
            assert adapter._api_key == "explicit"


class TestMyMiniFactoryAdapterProperties:
    def test_name(self, mmf_adapter):
        assert mmf_adapter.name == "myminifactory"

    def test_display_name(self, mmf_adapter):
        assert mmf_adapter.display_name == "MyMiniFactory"

    def test_supports_download_true(self, mmf_adapter):
        assert mmf_adapter.supports_download is True


class TestMyMiniFactoryAdapterSearch:
    @responses.activate
    def test_search_success(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            json=MMF_SEARCH_RESPONSE,
        )
        results = mmf_adapter.search("dragon")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "100"
        assert r.name == "Dragon Miniature"
        assert r.source == "myminifactory"
        assert r.creator == "ArtistOne"
        assert r.like_count == 55
        assert r.download_count == 3000
        assert r.license == "CC-BY-NC"

    @responses.activate
    def test_search_passes_params(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            json={"items": []},
            match=[matchers.query_param_matcher(
                {"key": MMF_API_KEY, "q": "benchy", "page": "2", "per_page": "5", "sort": "popularity"},
                strict_match=False,
            )],
        )
        mmf_adapter.search("benchy", page=2, per_page=5, sort="relevant")

    @responses.activate
    def test_search_sort_mapping(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            json={"items": []},
            match=[matchers.query_param_matcher(
                {"sort": "date"},
                strict_match=False,
            )],
        )
        mmf_adapter.search("test", sort="newest")

    @responses.activate
    def test_search_empty_response(self, mmf_adapter):
        responses.add(responses.GET, f"{MMF_BASE}/search", json={"items": []})
        assert mmf_adapter.search("noresults") == []

    @responses.activate
    def test_search_per_page_capped(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            json={"items": []},
            match=[matchers.query_param_matcher(
                {"per_page": "100"},
                strict_match=False,
            )],
        )
        mmf_adapter.search("test", per_page=999)


class TestMyMiniFactoryAdapterGetDetails:
    @responses.activate
    def test_get_details_success(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/objects/100",
            json=MMF_DETAIL_RESPONSE,
        )
        detail = mmf_adapter.get_details("100")
        assert isinstance(detail, ModelDetail)
        assert detail.id == "100"
        assert detail.name == "Dragon Miniature"
        assert detail.source == "myminifactory"
        assert detail.description == "A detailed dragon figurine."
        assert detail.instructions == "Print with supports enabled."
        assert detail.category == "Tabletop"
        assert detail.tags == ["dragon", "miniature"]
        assert detail.file_count == 2
        assert detail.license == "CC-BY-NC"


class TestMyMiniFactoryAdapterGetFiles:
    @responses.activate
    def test_get_files_success(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/objects/100/files",
            json=MMF_FILES_RESPONSE,
        )
        files = mmf_adapter.get_files("100")
        assert len(files) == 1
        f = files[0]
        assert isinstance(f, ModelFile)
        assert f.id == "200"
        assert f.name == "dragon_body.stl"
        assert f.size_bytes == 512000
        assert f.file_type == "stl"
        assert f.download_url == "https://cdn.mmf.io/dl/200"


class TestMyMiniFactoryAdapterDownload:
    @responses.activate
    def test_download_success(self, mmf_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/files/200",
            json={"id": 200, "filename": "dragon.stl", "download_url": "https://cdn.mmf.io/dl/200"},
        )
        responses.add(
            responses.GET,
            "https://cdn.mmf.io/dl/200",
            body=b"FAKE_STL_BYTES",
            content_type="application/octet-stream",
        )
        path = mmf_adapter.download_file("200", str(tmp_path))
        assert Path(path).exists()
        assert Path(path).name == "dragon.stl"
        assert Path(path).read_bytes() == b"FAKE_STL_BYTES"

    @responses.activate
    def test_download_custom_filename(self, mmf_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/files/200",
            json={"id": 200, "filename": "dragon.stl", "download_url": "https://cdn.mmf.io/dl/200"},
        )
        responses.add(
            responses.GET,
            "https://cdn.mmf.io/dl/200",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = mmf_adapter.download_file("200", str(tmp_path), file_name="custom.stl")
        assert Path(path).name == "custom.stl"

    @responses.activate
    def test_download_no_url_raises(self, mmf_adapter, tmp_path):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/files/200",
            json={"id": 200, "filename": "dragon.stl", "download_url": ""},
        )
        with pytest.raises(MarketplaceError, match="No download URL"):
            mmf_adapter.download_file("200", str(tmp_path))

    @responses.activate
    def test_download_creates_dir(self, mmf_adapter, tmp_path):
        dest = tmp_path / "sub" / "dir"
        assert not dest.exists()
        responses.add(
            responses.GET,
            f"{MMF_BASE}/files/200",
            json={"id": 200, "filename": "f.stl", "download_url": "https://cdn.mmf.io/dl/200"},
        )
        responses.add(
            responses.GET,
            "https://cdn.mmf.io/dl/200",
            body=b"DATA",
            content_type="application/octet-stream",
        )
        path = mmf_adapter.download_file("200", str(dest))
        assert Path(path).exists()


class TestMyMiniFactoryAdapterErrors:
    @responses.activate
    def test_401_raises_auth_error(self, mmf_adapter):
        responses.add(responses.GET, f"{MMF_BASE}/search", status=401, json={"error": "Unauthorized"})
        with pytest.raises(MarketplaceAuthError, match="Invalid or expired"):
            mmf_adapter.search("test")

    @responses.activate
    def test_404_raises_not_found(self, mmf_adapter):
        responses.add(responses.GET, f"{MMF_BASE}/objects/999", status=404, json={"error": "Not found"})
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            mmf_adapter.get_details("999")

    @responses.activate
    def test_429_raises_rate_limit(self, mmf_adapter):
        responses.add(responses.GET, f"{MMF_BASE}/search", status=429, json={"error": "Too many"})
        with pytest.raises(MarketplaceRateLimitError, match="rate limit"):
            mmf_adapter.search("test")

    @responses.activate
    def test_500_raises_generic_error(self, mmf_adapter):
        responses.add(responses.GET, f"{MMF_BASE}/search", status=500, body="Internal Server Error")
        with pytest.raises(MarketplaceError, match="500"):
            mmf_adapter.search("test")

    @responses.activate
    def test_connection_error(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(MarketplaceError, match="Connection"):
            mmf_adapter.search("test")

    @responses.activate
    def test_timeout_error(self, mmf_adapter):
        responses.add(
            responses.GET,
            f"{MMF_BASE}/search",
            body=requests.Timeout("timed out"),
        )
        with pytest.raises(MarketplaceError, match="timed out"):
            mmf_adapter.search("test")


class TestMyMiniFactoryParseHelpers:
    def test_parse_summary_license_as_string(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "designer": {"name": "u"},
            "licenses": ["MIT"],
        }
        s = MyMiniFactoryAdapter._parse_summary(data)
        assert s.license == "MIT"

    def test_parse_summary_no_designer(self):
        data = {"id": 1, "name": "X", "url": "/x", "designer": None}
        s = MyMiniFactoryAdapter._parse_summary(data)
        assert s.creator == ""

    def test_parse_summary_no_images(self):
        data = {"id": 1, "name": "X", "url": "/x", "designer": {"name": "u"}}
        s = MyMiniFactoryAdapter._parse_summary(data)
        assert s.thumbnail is None

    def test_parse_detail_tags_as_strings(self):
        data = {
            "id": 1, "name": "X", "url": "/x",
            "designer": {"name": "u"},
            "tags": ["a", "b"],
            "categories": ["Art"],
            "files": [],
        }
        d = MyMiniFactoryAdapter._parse_detail(data)
        assert d.tags == ["a", "b"]
        assert d.category == "Art"


# ===================================================================
# Cults3DAdapter tests
# ===================================================================


@pytest.fixture
def cults_adapter():
    """Cults3DAdapter configured with test GraphQL URL."""
    return Cults3DAdapter(
        username=CULTS_USERNAME,
        api_key=CULTS_API_KEY,
        graphql_url=CULTS_GQL_URL,
    )


# Sample Cults3D GraphQL responses
CULTS_SEARCH_DATA = {
    "data": {
        "creationsSearchBatch": {
            "total": 1,
            "results": [
                {
                    "identifier": "benchy-abc",
                    "name": "3D Benchy",
                    "url": "https://cults3d.com/en/3d-model/benchy-abc",
                    "shortUrl": "https://cults3d.com/s/benchy",
                    "price": {"cents": 0},
                    "license": {"code": "CC-BY", "name": "Creative Commons Attribution"},
                    "likesCount": 120,
                    "downloadsCount": 5000,
                    "illustrationImageUrl": "https://cdn.cults3d.com/thumb.jpg",
                    "creator": {"nick": "benchy_maker"},
                    "blueprints": [
                        {"fileUrl": "https://files.cults3d.com/benchy.stl", "imageUrl": "https://cdn.cults3d.com/bp.jpg"},
                    ],
                },
            ],
        },
    },
}

CULTS_DETAIL_DATA = {
    "data": {
        "creation": {
            "identifier": "benchy-abc",
            "name": "3D Benchy",
            "url": "https://cults3d.com/en/3d-model/benchy-abc",
            "shortUrl": "https://cults3d.com/s/benchy",
            "publishedAt": "2024-01-01",
            "price": {"cents": 0},
            "license": {"code": "CC-BY", "name": "Creative Commons Attribution"},
            "category": {"code": "3d-printing", "name": "3D Printing"},
            "tags": ["benchy", "benchmark"],
            "likesCount": 120,
            "downloadsCount": 5000,
            "viewsCount": 20000,
            "illustrationImageUrl": "https://cdn.cults3d.com/thumb.jpg",
            "creator": {"nick": "benchy_maker", "bio": "I make things"},
            "blueprints": [
                {"fileUrl": "https://files.cults3d.com/benchy.stl", "imageUrl": "https://cdn.cults3d.com/bp.jpg"},
            ],
        },
    },
}

CULTS_FILES_DATA = {
    "data": {
        "creation": {
            "blueprints": [
                {"fileUrl": "https://files.cults3d.com/benchy.stl", "imageUrl": "https://cdn.cults3d.com/bp.jpg"},
                {"fileUrl": "https://files.cults3d.com/base.3mf", "imageUrl": ""},
            ],
        },
    },
}


class TestCults3DAdapterInit:
    def test_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_CULTS3D_USERNAME", None)
            os.environ.pop("KILN_CULTS3D_API_KEY", None)
            with pytest.raises(MarketplaceAuthError, match="credentials are required"):
                Cults3DAdapter(username="", api_key="")

    def test_requires_both_username_and_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("KILN_CULTS3D_USERNAME", None)
            os.environ.pop("KILN_CULTS3D_API_KEY", None)
            with pytest.raises(MarketplaceAuthError):
                Cults3DAdapter(username="user", api_key="")

    def test_credentials_from_env(self):
        with patch.dict(os.environ, {
            "KILN_CULTS3D_USERNAME": "envuser",
            "KILN_CULTS3D_API_KEY": "envkey",
        }):
            adapter = Cults3DAdapter(graphql_url=CULTS_GQL_URL)
            assert adapter._username == "envuser"
            assert adapter._api_key == "envkey"

    def test_explicit_overrides_env(self):
        with patch.dict(os.environ, {
            "KILN_CULTS3D_USERNAME": "envuser",
            "KILN_CULTS3D_API_KEY": "envkey",
        }):
            adapter = Cults3DAdapter(
                username="explicit_user", api_key="explicit_key",
                graphql_url=CULTS_GQL_URL,
            )
            assert adapter._username == "explicit_user"
            assert adapter._api_key == "explicit_key"

    def test_basic_auth_header_computed(self):
        adapter = Cults3DAdapter(
            username="user", api_key="key", graphql_url=CULTS_GQL_URL,
        )
        expected = "Basic " + base64.b64encode(b"user:key").decode()
        assert adapter._auth_header == expected


class TestCults3DAdapterProperties:
    def test_name(self, cults_adapter):
        assert cults_adapter.name == "cults3d"

    def test_display_name(self, cults_adapter):
        assert cults_adapter.display_name == "Cults3D"

    def test_supports_download_false(self, cults_adapter):
        assert cults_adapter.supports_download is False


class TestCults3DAdapterSearch:
    @responses.activate
    def test_search_success(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json=CULTS_SEARCH_DATA,
        )
        results = cults_adapter.search("benchy")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ModelSummary)
        assert r.id == "benchy-abc"
        assert r.name == "3D Benchy"
        assert r.source == "cults3d"
        assert r.creator == "benchy_maker"
        assert r.like_count == 120
        assert r.download_count == 5000
        assert r.is_free is True
        assert r.price_cents == 0
        assert r.license == "Creative Commons Attribution"

    @responses.activate
    def test_search_sends_basic_auth(self, cults_adapter):
        expected_auth = "Basic " + base64.b64encode(
            f"{CULTS_USERNAME}:{CULTS_API_KEY}".encode()
        ).decode()

        def check_auth(request):
            assert request.headers["Authorization"] == expected_auth
            return (200, {}, json.dumps(CULTS_SEARCH_DATA))

        responses.add_callback(
            responses.POST,
            CULTS_GQL_URL,
            callback=check_auth,
            content_type="application/json",
        )
        cults_adapter.search("test")

    @responses.activate
    def test_search_sends_graphql_query(self, cults_adapter):
        def check_body(request):
            body = json.loads(request.body)
            assert "query" in body
            assert "creationsSearchBatch" in body["query"]
            assert body["variables"]["query"] == "benchy"
            assert body["variables"]["limit"] == 20
            assert body["variables"]["offset"] == 0
            return (200, {}, json.dumps(CULTS_SEARCH_DATA))

        responses.add_callback(
            responses.POST,
            CULTS_GQL_URL,
            callback=check_body,
            content_type="application/json",
        )
        cults_adapter.search("benchy")

    @responses.activate
    def test_search_pagination_offset(self, cults_adapter):
        def check_offset(request):
            body = json.loads(request.body)
            assert body["variables"]["offset"] == 10  # (page 2 - 1) * 10
            assert body["variables"]["limit"] == 10
            return (200, {}, json.dumps({"data": {"creationsSearchBatch": {"total": 0, "results": []}}}))

        responses.add_callback(
            responses.POST,
            CULTS_GQL_URL,
            callback=check_offset,
            content_type="application/json",
        )
        cults_adapter.search("test", page=2, per_page=10)

    @responses.activate
    def test_search_paid_model(self, cults_adapter):
        paid_data = {
            "data": {
                "creationsSearchBatch": {
                    "total": 1,
                    "results": [
                        {
                            "identifier": "paid-model",
                            "name": "Premium Dragon",
                            "url": "https://cults3d.com/en/3d-model/paid",
                            "price": {"cents": 499},
                            "license": {"code": "personal", "name": "Personal Use"},
                            "likesCount": 10,
                            "downloadsCount": 50,
                            "creator": {"nick": "artist"},
                            "blueprints": [],
                        },
                    ],
                },
            },
        }
        responses.add(responses.POST, CULTS_GQL_URL, json=paid_data)
        results = cults_adapter.search("dragon")
        assert len(results) == 1
        r = results[0]
        assert r.is_free is False
        assert r.price_cents == 499


class TestCults3DAdapterGetDetails:
    @responses.activate
    def test_get_details_success(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json=CULTS_DETAIL_DATA,
        )
        detail = cults_adapter.get_details("benchy-abc")
        assert isinstance(detail, ModelDetail)
        assert detail.id == "benchy-abc"
        assert detail.name == "3D Benchy"
        assert detail.source == "cults3d"
        assert detail.creator == "benchy_maker"
        assert detail.category == "3D Printing"
        assert detail.tags == ["benchy", "benchmark"]
        assert detail.file_count == 1
        assert detail.can_download is False
        assert detail.is_free is True

    @responses.activate
    def test_get_details_not_found(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json={"data": {"creation": None}},
        )
        with pytest.raises(MarketplaceNotFoundError, match="not found"):
            cults_adapter.get_details("nonexistent")


class TestCults3DAdapterGetFiles:
    @responses.activate
    def test_get_files_success(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json=CULTS_FILES_DATA,
        )
        files = cults_adapter.get_files("benchy-abc")
        assert len(files) == 2

        f0 = files[0]
        assert isinstance(f0, ModelFile)
        assert f0.id == "0"
        assert f0.name == "benchy.stl"
        assert f0.file_type == "stl"
        assert f0.download_url == "https://files.cults3d.com/benchy.stl"

        f1 = files[1]
        assert f1.id == "1"
        assert f1.name == "base.3mf"
        assert f1.file_type == "3mf"

    @responses.activate
    def test_get_files_not_found(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json={"data": {"creation": None}},
        )
        with pytest.raises(MarketplaceNotFoundError):
            cults_adapter.get_files("nonexistent")


class TestCults3DAdapterDownload:
    def test_download_raises(self, cults_adapter):
        with pytest.raises(MarketplaceError, match="does not support direct file downloads"):
            cults_adapter.download_file("0", "/tmp")


class TestCults3DAdapterErrors:
    @responses.activate
    def test_401_raises_auth_error(self, cults_adapter):
        responses.add(responses.POST, CULTS_GQL_URL, status=401, body="Unauthorized")
        with pytest.raises(MarketplaceAuthError, match="Invalid Cults3D credentials"):
            cults_adapter.search("test")

    @responses.activate
    def test_429_raises_rate_limit(self, cults_adapter):
        responses.add(responses.POST, CULTS_GQL_URL, status=429, body="Rate limited")
        with pytest.raises(MarketplaceRateLimitError, match="rate limit"):
            cults_adapter.search("test")

    @responses.activate
    def test_500_raises_generic_error(self, cults_adapter):
        responses.add(responses.POST, CULTS_GQL_URL, status=500, body="Internal Server Error")
        with pytest.raises(MarketplaceError, match="500"):
            cults_adapter.search("test")

    @responses.activate
    def test_graphql_error_raises(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            json={"errors": [{"message": "Field 'xyz' not found"}]},
        )
        with pytest.raises(MarketplaceError, match="GraphQL error.*Field 'xyz'"):
            cults_adapter.search("test")

    @responses.activate
    def test_connection_error(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            body=requests.ConnectionError("conn failed"),
        )
        with pytest.raises(MarketplaceError, match="Connection"):
            cults_adapter.search("test")

    @responses.activate
    def test_timeout_error(self, cults_adapter):
        responses.add(
            responses.POST,
            CULTS_GQL_URL,
            body=requests.Timeout("timed out"),
        )
        with pytest.raises(MarketplaceError, match="timed out"):
            cults_adapter.search("test")


class TestCults3DParseHelpers:
    def test_parse_blueprint_no_url(self):
        f = Cults3DAdapter._parse_blueprint(0, {"fileUrl": "", "imageUrl": ""})
        assert f.name == "file_0"
        assert f.file_type == ""

    def test_parse_blueprint_url_with_querystring(self):
        f = Cults3DAdapter._parse_blueprint(0, {
            "fileUrl": "https://files.cults3d.com/model.stl?token=abc",
            "imageUrl": "",
        })
        assert f.name == "model.stl"
        assert f.file_type == "stl"

    def test_parse_summary_no_price(self):
        data = {
            "identifier": "x", "name": "X", "url": "/x",
            "creator": {"nick": "u"},
            "price": None,
            "license": None,
            "blueprints": [],
        }
        s = Cults3DAdapter._parse_summary(data)
        assert s.is_free is True
        assert s.price_cents == 0

    def test_parse_detail_tags_as_dicts(self):
        data = {
            "identifier": "x", "name": "X", "url": "/x",
            "creator": {"nick": "u"},
            "price": {"cents": 0},
            "license": {"code": "CC0"},
            "category": {"name": "Art"},
            "tags": [{"name": "fantasy"}, {"name": "sculpt"}],
            "blueprints": [],
        }
        d = Cults3DAdapter._parse_detail(data)
        assert d.tags == ["fantasy", "sculpt"]


# ===================================================================
# MarketplaceRegistry tests
# ===================================================================


def _make_stub_adapter(name: str, display: str, results: list[ModelSummary] | None = None, error: Exception | None = None):
    """Create a MagicMock adapter with the given properties."""
    adapter = MagicMock(spec=MarketplaceAdapter)
    adapter.name = name
    adapter.display_name = display
    adapter.supports_download = True
    if error:
        adapter.search.side_effect = error
    else:
        adapter.search.return_value = results or []
    return adapter


class TestMarketplaceRegistryBasics:
    def test_register_and_get(self):
        registry = MarketplaceRegistry()
        adapter = _make_stub_adapter("test", "Test")
        registry.register(adapter)
        assert registry.get("test") is adapter

    def test_get_unknown_raises(self):
        registry = MarketplaceRegistry()
        with pytest.raises(MarketplaceError, match="not connected"):
            registry.get("unknown")

    def test_connected_lists_names(self):
        registry = MarketplaceRegistry()
        a1 = _make_stub_adapter("alpha", "Alpha")
        a2 = _make_stub_adapter("beta", "Beta")
        registry.register(a1)
        registry.register(a2)
        assert set(registry.connected) == {"alpha", "beta"}

    def test_count(self):
        registry = MarketplaceRegistry()
        assert registry.count == 0
        registry.register(_make_stub_adapter("a", "A"))
        assert registry.count == 1
        registry.register(_make_stub_adapter("b", "B"))
        assert registry.count == 2

    def test_unregister_existing(self):
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter("a", "A"))
        assert registry.unregister("a") is True
        assert registry.count == 0

    def test_unregister_nonexistent(self):
        registry = MarketplaceRegistry()
        assert registry.unregister("nonexistent") is False

    def test_register_overwrites(self):
        registry = MarketplaceRegistry()
        a1 = _make_stub_adapter("same", "Same1")
        a2 = _make_stub_adapter("same", "Same2")
        registry.register(a1)
        registry.register(a2)
        assert registry.count == 1
        assert registry.get("same") is a2


class TestMarketplaceRegistrySearchAll:
    def test_search_all_fans_out(self):
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="alpha")
        s2 = ModelSummary(id="2", name="B", url="/b", creator="u", source="beta")
        registry.register(_make_stub_adapter("alpha", "Alpha", [s1]))
        registry.register(_make_stub_adapter("beta", "Beta", [s2]))

        results = registry.search_all("test")
        assert isinstance(results, MarketplaceSearchResults)
        assert len(results.models) == 2
        ids = {r.id for r in results.models}
        assert ids == {"1", "2"}

    def test_search_all_interleaves(self):
        registry = MarketplaceRegistry()
        a_results = [
            ModelSummary(id="a1", name="A1", url="/a1", creator="u", source="alpha"),
            ModelSummary(id="a2", name="A2", url="/a2", creator="u", source="alpha"),
        ]
        b_results = [
            ModelSummary(id="b1", name="B1", url="/b1", creator="u", source="beta"),
            ModelSummary(id="b2", name="B2", url="/b2", creator="u", source="beta"),
        ]
        registry.register(_make_stub_adapter("alpha", "Alpha", a_results))
        registry.register(_make_stub_adapter("beta", "Beta", b_results))

        results = registry.search_all("test")
        assert len(results.models) == 4
        # Results should be interleaved: one from each source alternating
        sources = [r.source for r in results.models]
        # Verify interleaving: no three consecutive from same source
        for i in range(len(sources) - 2):
            assert not (sources[i] == sources[i + 1] == sources[i + 2])

    def test_search_all_handles_adapter_failure(self):
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="good")
        registry.register(_make_stub_adapter("good", "Good", [s1]))
        registry.register(_make_stub_adapter(
            "bad", "Bad", error=MarketplaceError("API down"),
        ))

        results = registry.search_all("test")
        assert len(results.models) == 1
        assert results.models[0].id == "1"
        assert "bad" in results.failed

    def test_search_all_handles_unexpected_exception(self):
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="good")
        registry.register(_make_stub_adapter("good", "Good", [s1]))
        registry.register(_make_stub_adapter(
            "bad", "Bad", error=RuntimeError("unexpected"),
        ))

        results = registry.search_all("test")
        assert len(results.models) == 1
        assert "bad" in results.failed

    def test_search_all_with_sources_filter(self):
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="alpha")
        s2 = ModelSummary(id="2", name="B", url="/b", creator="u", source="beta")
        registry.register(_make_stub_adapter("alpha", "Alpha", [s1]))
        registry.register(_make_stub_adapter("beta", "Beta", [s2]))

        results = registry.search_all("test", sources=["alpha"])
        assert len(results.models) == 1
        assert results.models[0].source == "alpha"

    def test_search_all_with_empty_sources_filter(self):
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter("alpha", "Alpha", [
            ModelSummary(id="1", name="A", url="/a", creator="u", source="alpha"),
        ]))

        results = registry.search_all("test", sources=["nonexistent"])
        assert results.models == []

    def test_search_all_no_adapters(self):
        registry = MarketplaceRegistry()
        results = registry.search_all("test")
        assert results.models == []

    def test_search_all_all_adapters_fail(self):
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter(
            "bad1", "Bad1", error=MarketplaceError("fail1"),
        ))
        registry.register(_make_stub_adapter(
            "bad2", "Bad2", error=MarketplaceError("fail2"),
        ))
        results = registry.search_all("test")
        assert results.models == []
        assert set(results.failed) == {"bad1", "bad2"}

    def test_search_all_passes_params(self):
        registry = MarketplaceRegistry()
        adapter = _make_stub_adapter("a", "A", [])
        registry.register(adapter)

        registry.search_all("query", page=3, per_page=5, sort="newest")
        adapter.search.assert_called_once_with("query", page=3, per_page=5, sort="newest")

    def test_search_all_uneven_results(self):
        """One adapter returns more results than another; interleaving handles it."""
        registry = MarketplaceRegistry()
        a_results = [
            ModelSummary(id=f"a{i}", name=f"A{i}", url=f"/a{i}", creator="u", source="alpha")
            for i in range(5)
        ]
        b_results = [
            ModelSummary(id="b0", name="B0", url="/b0", creator="u", source="beta"),
        ]
        registry.register(_make_stub_adapter("alpha", "Alpha", a_results))
        registry.register(_make_stub_adapter("beta", "Beta", b_results))

        results = registry.search_all("test")
        assert len(results.models) == 6
        # Beta's single result should appear early (interleaved), not at the end
        beta_indices = [i for i, r in enumerate(results.models) if r.source == "beta"]
        assert beta_indices[0] <= 1  # Should be in the first round

    def test_search_all_records_success(self):
        """Successful searches update health monitor to HEALTHY."""
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="alpha")
        registry.register(_make_stub_adapter("alpha", "Alpha", [s1]))

        results = registry.search_all("test")
        assert results.health["alpha"].health == MarketplaceHealth.HEALTHY
        assert "alpha" in results.searched

    def test_search_all_records_failure(self):
        """Failed searches update health monitor toward DEGRADED/DOWN."""
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter(
            "bad", "Bad", error=MarketplaceError("oops"),
        ))

        results = registry.search_all("test")
        assert results.health["bad"].health == MarketplaceHealth.DEGRADED
        assert results.health["bad"].consecutive_failures == 1

    def test_search_all_skips_down_marketplace(self):
        """Marketplaces that are DOWN are skipped (circuit breaker)."""
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="good")
        registry.register(_make_stub_adapter("good", "Good", [s1]))
        bad_adapter = _make_stub_adapter(
            "bad", "Bad", error=MarketplaceError("fail"),
        )
        registry.register(bad_adapter)

        # Fail 3 times to trigger DOWN
        for _ in range(3):
            registry.search_all("test")

        # Now "bad" should be skipped entirely
        bad_adapter.search.reset_mock()
        results = registry.search_all("test")
        bad_adapter.search.assert_not_called()
        assert "bad" in results.skipped
        assert len(results.models) == 1
        assert results.models[0].source == "good"

    def test_search_all_down_marketplace_recovers_on_success(self):
        """A DOWN marketplace recovers when manually recorded as healthy."""
        registry = MarketplaceRegistry()
        bad_adapter = _make_stub_adapter(
            "flaky", "Flaky", error=MarketplaceError("fail"),
        )
        registry.register(bad_adapter)

        # Drive to DOWN
        for _ in range(3):
            registry.search_all("test")

        status = registry.health_monitor.get_status("flaky")
        assert status.health == MarketplaceHealth.DOWN

        # Simulate recovery
        registry.health_monitor.record_success("flaky")
        status = registry.health_monitor.get_status("flaky")
        assert status.health == MarketplaceHealth.HEALTHY

    def test_search_all_summary_includes_health(self):
        """The summary string mentions marketplace health states."""
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="alpha")
        registry.register(_make_stub_adapter("alpha", "Alpha", [s1]))

        results = registry.search_all("test")
        assert "alpha" in results.summary
        assert "healthy" in results.summary.lower() or "Results from" in results.summary


class TestMarketplaceHealthMonitor:
    """MarketplaceHealthMonitor consecutive failure counting and circuit breaker."""

    def test_initial_state_is_unknown(self):
        monitor = MarketplaceHealthMonitor()
        status = monitor.get_status("new")
        assert status.health == MarketplaceHealth.UNKNOWN
        assert status.consecutive_failures == 0

    def test_record_success_sets_healthy(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_success("test", response_time_ms=42.0)
        status = monitor.get_status("test")
        assert status.health == MarketplaceHealth.HEALTHY
        assert status.response_time_ms == 42.0
        assert status.consecutive_failures == 0

    def test_single_failure_sets_degraded(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_failure("test", error="timeout")
        status = monitor.get_status("test")
        assert status.health == MarketplaceHealth.DEGRADED
        assert status.consecutive_failures == 1
        assert status.error == "timeout"

    def test_two_failures_still_degraded(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_failure("test", error="err1")
        monitor.record_failure("test", error="err2")
        status = monitor.get_status("test")
        assert status.health == MarketplaceHealth.DEGRADED
        assert status.consecutive_failures == 2

    def test_three_failures_sets_down(self):
        monitor = MarketplaceHealthMonitor()
        for i in range(3):
            monitor.record_failure("test", error=f"err{i}")
        status = monitor.get_status("test")
        assert status.health == MarketplaceHealth.DOWN
        assert status.consecutive_failures == 3

    def test_success_resets_failures(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_failure("test", error="err1")
        monitor.record_failure("test", error="err2")
        monitor.record_success("test")
        status = monitor.get_status("test")
        assert status.health == MarketplaceHealth.HEALTHY
        assert status.consecutive_failures == 0

    def test_is_available_for_healthy(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_success("test")
        assert monitor.is_available("test") is True

    def test_is_available_for_unknown(self):
        monitor = MarketplaceHealthMonitor()
        assert monitor.is_available("new") is True

    def test_is_available_for_degraded(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_failure("test")
        assert monitor.is_available("test") is True

    def test_is_available_false_for_down(self):
        monitor = MarketplaceHealthMonitor()
        for _ in range(3):
            monitor.record_failure("test")
        assert monitor.is_available("test") is False

    def test_get_all_statuses(self):
        monitor = MarketplaceHealthMonitor()
        monitor.record_success("a")
        monitor.record_failure("b")
        statuses = monitor.get_all_statuses()
        assert len(statuses) == 2
        names = {s.marketplace for s in statuses}
        assert names == {"a", "b"}

    def test_status_to_dict(self):
        status = MarketplaceStatus(
            marketplace="test",
            health=MarketplaceHealth.DEGRADED,
            consecutive_failures=2,
            error="timeout",
        )
        d = status.to_dict()
        assert d["marketplace"] == "test"
        assert d["health"] == "degraded"
        assert d["consecutive_failures"] == 2
        assert d["error"] == "timeout"


class TestMarketplaceRegistryHealth:
    """MarketplaceRegistry.marketplace_health() method."""

    def test_health_returns_all_connected(self):
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter("a", "A"))
        registry.register(_make_stub_adapter("b", "B"))
        statuses = registry.marketplace_health()
        assert len(statuses) == 2
        names = {s.marketplace for s in statuses}
        assert names == {"a", "b"}

    def test_health_unknown_before_any_search(self):
        registry = MarketplaceRegistry()
        registry.register(_make_stub_adapter("a", "A"))
        statuses = registry.marketplace_health()
        assert statuses[0].health == MarketplaceHealth.UNKNOWN

    def test_health_updates_after_search(self):
        registry = MarketplaceRegistry()
        s1 = ModelSummary(id="1", name="A", url="/a", creator="u", source="a")
        registry.register(_make_stub_adapter("a", "A", [s1]))
        registry.search_all("test")
        statuses = registry.marketplace_health()
        assert statuses[0].health == MarketplaceHealth.HEALTHY
