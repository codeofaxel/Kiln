"""Etsy marketplace adapter for digital download 3D models.

Wraps the Etsy Open API v3.  Searches for digital download listings
containing 3D-printable files (STL, 3MF, etc.).

Environment variables
---------------------
``KILN_ETSY_API_KEY``
    Etsy API key (Open API v3 keystring).  Required for all operations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests

from kiln.marketplaces.base import (
    MarketplaceAdapter,
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplaceRateLimitError,
    ModelDetail,
    ModelFile,
    ModelSummary,
    resumable_download,
)

_logger = logging.getLogger(__name__)

_BASE_URL = "https://openapi.etsy.com/v3"
_REQUEST_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 120


class EtsyAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the Etsy Open API v3.

    Focuses on digital download listings containing 3D-printable files
    (STL, 3MF, OBJ, G-code).

    :param api_key: Etsy API key (keystring).  Falls back to
        ``KILN_ETSY_API_KEY`` env var.
    :param base_url: Override the API base URL (useful for testing).
    :param session: Optional :class:`requests.Session` for connection pooling.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_ETSY_API_KEY", "")
        if not self._api_key:
            raise MarketplaceAuthError(
                "Etsy API key is required.  Set KILN_ETSY_API_KEY or pass api_key= to EtsyAdapter."
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return "etsy"

    @property
    def display_name(self) -> str:
        return "Etsy"

    # -- low-level request helper ------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = _REQUEST_TIMEOUT,
    ) -> Any:
        """Make an authenticated API request and return parsed JSON."""
        url = f"{self._base_url}{path}"
        headers = {
            "x-api-key": self._api_key,
            "Accept": "application/json",
        }

        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
        except requests.ConnectionError as exc:
            raise MarketplaceError(
                f"Connection to Etsy failed: {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise MarketplaceError(
                f"Etsy request timed out: {exc}",
            ) from exc

        if resp.status_code == 401:
            raise MarketplaceAuthError(
                "Invalid or expired Etsy API key.",
                status_code=401,
            )
        if resp.status_code == 404:
            raise MarketplaceNotFoundError(
                f"Resource not found: {path}",
                status_code=404,
            )
        if resp.status_code == 429:
            raise MarketplaceRateLimitError(
                "Etsy API rate limit exceeded.",
                status_code=429,
            )
        if resp.status_code >= 400:
            raise MarketplaceError(
                f"Etsy API error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        return resp.json()

    # -- MarketplaceAdapter interface --------------------------------------

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> list[ModelSummary]:
        sort_map = {
            "relevant": "relevancy",
            "popular": "most_relevant",
            "newest": "created",
        }
        # Append 3D printing keywords to focus on printable models
        search_query = f"{query} STL 3D print digital download"
        offset = (page - 1) * per_page

        data = self._request(
            "GET",
            "/application/listings/active",
            params={
                "keywords": search_query,
                "limit": min(per_page, 100),
                "offset": offset,
                "sort_on": sort_map.get(sort, "relevancy"),
            },
        )
        items = data.get("results", []) if isinstance(data, dict) else []
        return [self._parse_summary(item) for item in items if isinstance(item, dict)]

    def get_details(self, model_id: str) -> ModelDetail:
        data = self._request("GET", f"/application/listings/{model_id}")
        return self._parse_detail(data)

    def get_files(self, model_id: str) -> list[ModelFile]:
        data = self._request("GET", f"/application/listings/{model_id}/files")
        items = data.get("results", []) if isinstance(data, dict) else []
        return [self._parse_file(f) for f in items if isinstance(f, dict)]

    def download_file(
        self,
        file_id: str,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        meta = self._request("GET", f"/application/listings/files/{file_id}")
        download_url = meta.get("url", meta.get("download_url", ""))
        if not download_url:
            raise MarketplaceError(
                f"No download URL for Etsy file {file_id}.  Etsy may require OAuth for digital file downloads.",
            )

        name = file_name or meta.get("filename", f"file_{file_id}")
        dest = Path(dest_dir)
        out_path = dest / name

        try:
            return resumable_download(
                self._session,
                download_url,
                out_path,
                timeout=_DOWNLOAD_TIMEOUT,
            )
        except MarketplaceError:
            raise
        except Exception as exc:
            raise MarketplaceError(
                f"Failed to download Etsy file {file_id}: {exc}",
            ) from exc

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_summary(data: dict[str, Any]) -> ModelSummary:
        tags = data.get("tags", []) or []
        is_digital = data.get("is_digital", False)

        price_raw = data.get("price", {}) or {}
        if isinstance(price_raw, dict):
            # Etsy returns price as {"amount": 1299, "divisor": 100, ...}
            amount = price_raw.get("amount", 0) or 0
            divisor = price_raw.get("divisor", 100) or 100
            price_cents = int(amount * 100 / divisor) if divisor != 100 else amount
        else:
            price_cents = 0

        shop = data.get("shop", {}) or {}
        creator = shop.get("shop_name", data.get("shop_name", ""))

        images = data.get("images", []) or []
        thumbnail: str | None = None
        if images and isinstance(images[0], dict):
            thumbnail = images[0].get("url_570xN", images[0].get("url_170x135"))

        # Infer printability from tags
        _printable_tags = {"stl", "3mf", "gcode", "3d print", "3d printing"}
        has_printable = any(t.lower() in _printable_tags for t in tags if isinstance(t, str))

        return ModelSummary(
            id=str(data.get("listing_id", data.get("id", ""))),
            name=data.get("title", ""),
            url=data.get("url", ""),
            creator=creator,
            source="etsy",
            thumbnail=thumbnail,
            like_count=data.get("num_favorers", 0) or 0,
            download_count=data.get("views", 0) or 0,
            license="",
            is_free=price_cents == 0,
            price_cents=price_cents,
            has_sliceable_files=is_digital and has_printable,
            can_download=is_digital,
        )

    @staticmethod
    def _parse_detail(data: dict[str, Any]) -> ModelDetail:
        tags = data.get("tags", []) or []
        is_digital = data.get("is_digital", False)

        price_raw = data.get("price", {}) or {}
        if isinstance(price_raw, dict):
            amount = price_raw.get("amount", 0) or 0
            divisor = price_raw.get("divisor", 100) or 100
            price_cents = int(amount * 100 / divisor) if divisor != 100 else amount
        else:
            price_cents = 0

        shop = data.get("shop", {}) or {}
        creator = shop.get("shop_name", data.get("shop_name", ""))

        images = data.get("images", []) or []
        thumbnail: str | None = None
        if images and isinstance(images[0], dict):
            thumbnail = images[0].get("url_570xN", images[0].get("url_170x135"))

        category_path = data.get("taxonomy_path", []) or []
        category: str | None = None
        if category_path:
            last = category_path[-1]
            category = last if isinstance(last, str) else (last.get("name", "") if isinstance(last, dict) else None)

        return ModelDetail(
            id=str(data.get("listing_id", data.get("id", ""))),
            name=data.get("title", ""),
            url=data.get("url", ""),
            creator=creator,
            source="etsy",
            description=data.get("description", ""),
            license="",
            thumbnail=thumbnail,
            like_count=data.get("num_favorers", 0) or 0,
            download_count=data.get("views", 0) or 0,
            category=category,
            tags=tags,
            file_count=data.get("file_count", 0) or 0,
            is_free=price_cents == 0,
            price_cents=price_cents,
            can_download=is_digital,
        )

    @staticmethod
    def _parse_file(data: dict[str, Any]) -> ModelFile:
        filename = data.get("filename", data.get("name", ""))
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ModelFile(
            id=str(data.get("listing_file_id", data.get("id", ""))),
            name=filename,
            size_bytes=data.get("size", data.get("file_size", 0)) or 0,
            download_url=data.get("url", data.get("download_url", "")),
            file_type=ext,
        )
