"""MyMiniFactory marketplace adapter.

Wraps the MyMiniFactory REST API v2.  Supports search, model details,
file listing, and file downloads (with API key auth).

Environment variables
---------------------
``KILN_MMF_API_KEY``
    MyMiniFactory API key.  Required for all operations.
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
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.myminifactory.com/api/v2"
_REQUEST_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 120


class MyMiniFactoryAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the MyMiniFactory REST API v2.

    Args:
        api_key: MyMiniFactory API key.  Falls back to
            ``KILN_MMF_API_KEY`` env var.
        base_url: Override the API base URL (useful for testing).
        session: Optional :class:`requests.Session` for connection pooling.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_MMF_API_KEY", "")
        if not self._api_key:
            raise MarketplaceAuthError(
                "MyMiniFactory API key is required.  Set KILN_MMF_API_KEY "
                "or pass api_key= to MyMiniFactoryAdapter."
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return "myminifactory"

    @property
    def display_name(self) -> str:
        return "MyMiniFactory"

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
        params = dict(params) if params else {}
        params["key"] = self._api_key

        try:
            resp = self._session.request(
                method, url, params=params, timeout=timeout,
            )
        except requests.ConnectionError as exc:
            raise MarketplaceError(
                f"Connection to MyMiniFactory failed: {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise MarketplaceError(
                f"MyMiniFactory request timed out: {exc}",
            ) from exc

        if resp.status_code == 401:
            raise MarketplaceAuthError(
                "Invalid or expired MyMiniFactory API key.",
                status_code=401,
            )
        if resp.status_code == 404:
            raise MarketplaceNotFoundError(
                f"Resource not found: {path}", status_code=404,
            )
        if resp.status_code == 429:
            raise MarketplaceRateLimitError(
                "MyMiniFactory API rate limit exceeded.",
                status_code=429,
            )
        if resp.status_code >= 400:
            raise MarketplaceError(
                f"MyMiniFactory API error {resp.status_code}: {resp.text[:200]}",
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
            "relevant": "popularity",
            "popular": "visits",
            "newest": "date",
        }
        data = self._request("GET", "/search", params={
            "q": query,
            "page": page,
            "per_page": min(per_page, 100),
            "sort": sort_map.get(sort, sort),
        })
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._parse_summary(item) for item in items if isinstance(item, dict)]

    def get_details(self, model_id: str) -> ModelDetail:
        data = self._request("GET", f"/objects/{model_id}")
        return self._parse_detail(data)

    def get_files(self, model_id: str) -> List[ModelFile]:
        data = self._request("GET", f"/objects/{model_id}/files")
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._parse_file(f) for f in items if isinstance(f, dict)]

    def download_file(
        self,
        file_id: str,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        # Get file metadata for the download URL
        meta = self._request("GET", f"/files/{file_id}")
        download_url = meta.get("download_url", "")
        if not download_url:
            raise MarketplaceError(
                f"No download URL for file {file_id}.  "
                "MyMiniFactory may require OAuth2 authentication for downloads.",
            )

        name = file_name or meta.get("filename", f"file_{file_id}")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / name

        try:
            resp = self._session.get(
                download_url,
                params={"key": self._api_key},
                timeout=_DOWNLOAD_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MarketplaceError(
                f"Failed to download file {file_id}: {exc}",
            ) from exc

        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

        logger.info("Downloaded %s (%d bytes)", out_path, out_path.stat().st_size)
        return str(out_path.resolve())

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_summary(data: Dict[str, Any]) -> ModelSummary:
        designer = data.get("designer", {}) or {}
        licenses = data.get("licenses", [])
        license_str = ""
        if isinstance(licenses, list) and licenses:
            first = licenses[0]
            if isinstance(first, dict):
                license_str = first.get("value", first.get("label", ""))
            elif isinstance(first, str):
                license_str = first

        return ModelSummary(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            url=data.get("url", ""),
            creator=designer.get("name", designer.get("username", "")),
            source="myminifactory",
            thumbnail=(data.get("images", [{}]) or [{}])[0].get("thumbnail", {}).get("url")
            if data.get("images")
            else None,
            like_count=data.get("likes", 0),
            download_count=data.get("views", 0),  # MMF reports views, not downloads
            license=license_str,
        )

    @staticmethod
    def _parse_detail(data: Dict[str, Any]) -> ModelDetail:
        designer = data.get("designer", {}) or {}
        licenses = data.get("licenses", [])
        license_str = ""
        if isinstance(licenses, list) and licenses:
            first = licenses[0]
            if isinstance(first, dict):
                license_str = first.get("value", first.get("label", ""))
            elif isinstance(first, str):
                license_str = first

        tags_raw = data.get("tags", [])
        tags = []
        if isinstance(tags_raw, list):
            for t in tags_raw:
                if isinstance(t, dict):
                    tags.append(t.get("name", t.get("label", "")))
                elif isinstance(t, str):
                    tags.append(t)

        categories = data.get("categories", [])
        category = None
        if isinstance(categories, list) and categories:
            first = categories[0]
            if isinstance(first, dict):
                category = first.get("name", "")
            elif isinstance(first, str):
                category = first

        return ModelDetail(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            url=data.get("url", ""),
            creator=designer.get("name", designer.get("username", "")),
            source="myminifactory",
            description=data.get("description", ""),
            instructions=data.get("printing_details", ""),
            license=license_str,
            thumbnail=(data.get("images", [{}]) or [{}])[0].get("thumbnail", {}).get("url")
            if data.get("images")
            else None,
            like_count=data.get("likes", 0),
            download_count=data.get("views", 0),
            category=category,
            tags=tags,
            file_count=len(data.get("files", [])),
        )

    @staticmethod
    def _parse_file(data: Dict[str, Any]) -> ModelFile:
        filename = data.get("filename", "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ModelFile(
            id=str(data.get("id", "")),
            name=filename,
            size_bytes=data.get("size", 0),
            download_url=data.get("download_url", ""),
            thumbnail_url=data.get("thumbnail_url", ""),
            file_type=ext,
        )
