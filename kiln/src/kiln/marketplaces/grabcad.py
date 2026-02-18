"""GrabCAD Community marketplace adapter.

Wraps the GrabCAD Community REST API v1.  Supports search, model details,
file listing, and file downloads.

Environment variables
---------------------
``KILN_GRABCAD_TOKEN``
    GrabCAD API token.  Required for all operations.
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

logger = logging.getLogger(__name__)

_BASE_URL = "https://grabcad.com/api/v1"
_REQUEST_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 120

# Maps GrabCAD categories to printability hints.
# GrabCAD is primarily a CAD/engineering library — most files are STEP/IGES
# for CNC or SLA workflows, so ``has_sliceable_files`` defaults to True and
# ``has_printable_files`` defaults to False.
_PRINTABLE_EXTENSIONS = {"stl", "3mf", "obj", "step", "stp", "iges", "igs"}


class GrabCADAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the GrabCAD Community REST API v1.

    :param api_token: GrabCAD API token.  Falls back to
        ``KILN_GRABCAD_TOKEN`` env var.
    :param base_url: Override the API base URL (useful for testing).
    :param session: Optional :class:`requests.Session` for connection pooling.
    """

    def __init__(
        self,
        api_token: str | None = None,
        *,
        base_url: str = _BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._api_token = api_token or os.environ.get("KILN_GRABCAD_TOKEN", "")
        if not self._api_token:
            raise MarketplaceAuthError(
                "GrabCAD API token is required.  Set KILN_GRABCAD_TOKEN or pass api_token= to GrabCADAdapter."
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return "grabcad"

    @property
    def display_name(self) -> str:
        return "GrabCAD"

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
            "Authorization": f"Bearer {self._api_token}",
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
                f"Connection to GrabCAD failed: {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise MarketplaceError(
                f"GrabCAD request timed out: {exc}",
            ) from exc

        if resp.status_code == 401:
            raise MarketplaceAuthError(
                "Invalid or expired GrabCAD API token.",
                status_code=401,
            )
        if resp.status_code == 404:
            raise MarketplaceNotFoundError(
                f"Resource not found: {path}",
                status_code=404,
            )
        if resp.status_code == 429:
            raise MarketplaceRateLimitError(
                "GrabCAD API rate limit exceeded.",
                status_code=429,
            )
        if resp.status_code >= 400:
            raise MarketplaceError(
                f"GrabCAD API error {resp.status_code}: {resp.text[:200]}",
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
            "relevant": "relevance",
            "popular": "popularity",
            "newest": "newest",
        }
        data = self._request(
            "GET",
            "/models",
            params={
                "query": query,
                "page": page,
                "per_page": min(per_page, 100),
                "sort": sort_map.get(sort, sort),
            },
        )
        items = data.get("models", []) if isinstance(data, dict) else []
        return [self._parse_summary(item) for item in items if isinstance(item, dict)]

    def get_details(self, model_id: str) -> ModelDetail:
        data = self._request("GET", f"/models/{model_id}")
        return self._parse_detail(data)

    def get_files(self, model_id: str) -> list[ModelFile]:
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
                f"No download URL for GrabCAD file {file_id}.",
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
                f"Failed to download GrabCAD file {file_id}: {exc}",
            ) from exc

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_summary(data: dict[str, Any]) -> ModelSummary:
        creator = data.get("creator", {}) or {}
        return ModelSummary(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            url=data.get("url", ""),
            creator=creator.get("name", creator.get("username", "")),
            source="grabcad",
            thumbnail=data.get("preview_image"),
            like_count=data.get("likes_count", 0) or 0,
            download_count=data.get("downloads_count", 0) or 0,
            license=data.get("license", ""),
            has_sliceable_files=True,
        )

    @staticmethod
    def _parse_detail(data: dict[str, Any]) -> ModelDetail:
        creator = data.get("creator", {}) or {}
        tags_raw = data.get("tags", []) or []
        tag_strs: list[str] = []
        for t in tags_raw:
            if isinstance(t, str):
                tag_strs.append(t)
            elif isinstance(t, dict):
                tag_strs.append(t.get("name", ""))

        category = data.get("category", "")

        return ModelDetail(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            url=data.get("url", ""),
            creator=creator.get("name", creator.get("username", "")),
            source="grabcad",
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            license=data.get("license", ""),
            thumbnail=data.get("preview_image"),
            like_count=data.get("likes_count", 0) or 0,
            download_count=data.get("downloads_count", 0) or 0,
            category=category if isinstance(category, str) and category else None,
            tags=tag_strs,
            file_count=data.get("file_count", 0) or 0,
        )

    @staticmethod
    def _parse_file(data: dict[str, Any]) -> ModelFile:
        filename = data.get("file_name", "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ModelFile(
            id=str(data.get("id", "")),
            name=filename,
            size_bytes=data.get("size", 0) or 0,
            download_url=data.get("download_url", ""),
            thumbnail_url=data.get("preview_url"),
            file_type=ext,
        )
