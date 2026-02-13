"""Thangs marketplace adapter.

Wraps the Thangs REST API v1.  Supports search, model details,
file listing, and file downloads.

Environment variables
---------------------
``KILN_THANGS_API_KEY``
    Thangs API key.  Required for all operations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

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

_BASE_URL = "https://api.thangs.com/v1"
_REQUEST_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 120


class ThangsAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the Thangs REST API v1.

    :param api_key: Thangs API key.  Falls back to
        ``KILN_THANGS_API_KEY`` env var.
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
        self._api_key = api_key or os.environ.get("KILN_THANGS_API_KEY", "")
        if not self._api_key:
            raise MarketplaceAuthError(
                "Thangs API key is required.  Set KILN_THANGS_API_KEY "
                "or pass api_key= to ThangsAdapter."
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return "thangs"

    @property
    def display_name(self) -> str:
        return "Thangs"

    # -- low-level request helper ------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout: int = _REQUEST_TIMEOUT,
    ) -> Any:
        """Make an authenticated API request and return parsed JSON."""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

        try:
            resp = self._session.request(
                method, url, params=params, headers=headers, timeout=timeout,
            )
        except requests.ConnectionError as exc:
            raise MarketplaceError(
                f"Connection to Thangs failed: {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise MarketplaceError(
                f"Thangs request timed out: {exc}",
            ) from exc

        if resp.status_code == 401:
            raise MarketplaceAuthError(
                "Invalid or expired Thangs API key.",
                status_code=401,
            )
        if resp.status_code == 404:
            raise MarketplaceNotFoundError(
                f"Resource not found: {path}", status_code=404,
            )
        if resp.status_code == 429:
            raise MarketplaceRateLimitError(
                "Thangs API rate limit exceeded.",
                status_code=429,
            )
        if resp.status_code >= 400:
            raise MarketplaceError(
                f"Thangs API error {resp.status_code}: {resp.text[:200]}",
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
    ) -> List[ModelSummary]:
        sort_map = {
            "relevant": "relevance",
            "popular": "popularity",
            "newest": "date",
        }
        data = self._request("GET", "/search", params={
            "q": query,
            "page": page,
            "per_page": min(per_page, 100),
            "sort": sort_map.get(sort, sort),
        })
        items = data.get("results", []) if isinstance(data, dict) else []
        return [self._parse_summary(item) for item in items if isinstance(item, dict)]

    def get_details(self, model_id: str) -> ModelDetail:
        data = self._request("GET", f"/models/{model_id}")
        return self._parse_detail(data)

    def get_files(self, model_id: str) -> List[ModelFile]:
        data = self._request("GET", f"/models/{model_id}/files")
        items = data.get("files", []) if isinstance(data, dict) else []
        return [self._parse_file(f) for f in items if isinstance(f, dict)]

    def download_file(
        self,
        file_id: str,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        meta = self._request("GET", f"/files/{file_id}")
        download_url = meta.get("download_url", "")
        if not download_url:
            raise MarketplaceError(
                f"No download URL for Thangs file {file_id}.",
            )

        name = file_name or meta.get("file_name", f"file_{file_id}")
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
                f"Failed to download Thangs file {file_id}: {exc}",
            ) from exc

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_summary(data: Dict[str, Any]) -> ModelSummary:
        creator = data.get("creator", data.get("owner", {})) or {}
        return ModelSummary(
            id=str(data.get("id", "")),
            name=data.get("name", data.get("title", "")),
            url=data.get("url", ""),
            creator=creator.get("name", creator.get("username", "")),
            source="thangs",
            thumbnail=data.get("thumbnail_url", data.get("preview_url")),
            like_count=data.get("likes_count", data.get("like_count", 0)) or 0,
            download_count=data.get("downloads_count", data.get("download_count", 0)) or 0,
            license=data.get("license", ""),
        )

    @staticmethod
    def _parse_detail(data: Dict[str, Any]) -> ModelDetail:
        creator = data.get("creator", data.get("owner", {})) or {}
        tags_raw = data.get("tags", []) or []
        tags: list[str] = []
        for t in tags_raw:
            if isinstance(t, str):
                tags.append(t)
            elif isinstance(t, dict):
                tags.append(t.get("name", ""))

        category = data.get("category", "")
        if isinstance(category, dict):
            category = category.get("name", "")

        files = data.get("files", []) or []

        return ModelDetail(
            id=str(data.get("id", "")),
            name=data.get("name", data.get("title", "")),
            url=data.get("url", ""),
            creator=creator.get("name", creator.get("username", "")),
            source="thangs",
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            license=data.get("license", ""),
            thumbnail=data.get("thumbnail_url", data.get("preview_url")),
            like_count=data.get("likes_count", data.get("like_count", 0)) or 0,
            download_count=data.get("downloads_count", data.get("download_count", 0)) or 0,
            category=category if category else None,
            tags=tags,
            file_count=len(files),
        )

    @staticmethod
    def _parse_file(data: Dict[str, Any]) -> ModelFile:
        filename = data.get("file_name", data.get("name", ""))
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ModelFile(
            id=str(data.get("id", "")),
            name=filename,
            size_bytes=data.get("size", data.get("size_bytes", 0)) or 0,
            download_url=data.get("download_url", ""),
            thumbnail_url=data.get("preview_url", data.get("thumbnail_url")),
            file_type=ext,
        )
