"""Thingiverse API client for discovering and downloading 3D models.

Provides a typed client that wraps the Thingiverse REST API so that
AI agents can autonomously search for models, inspect their details,
and download print-ready files.

Environment variables
---------------------
``KILN_THINGIVERSE_TOKEN``
    Application token for the Thingiverse API.  Required for all
    operations.  Obtain one at https://www.thingiverse.com/apps/create.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.thingiverse.com"
_REQUEST_TIMEOUT = 30  # seconds
_DOWNLOAD_TIMEOUT = 120  # seconds


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ThingiverseError(Exception):
    """Base exception for Thingiverse API errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ThingiverseAuthError(ThingiverseError):
    """Raised when the API token is missing or invalid."""


class ThingiverseNotFoundError(ThingiverseError):
    """Raised when a requested resource does not exist."""


class ThingiverseRateLimitError(ThingiverseError):
    """Raised when the API rate limit has been exceeded."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ThingFile:
    """A downloadable file attached to a Thingiverse *thing*."""

    id: int
    name: str
    size_bytes: int
    download_url: str
    thumbnail_url: Optional[str] = None
    date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ThingSummary:
    """Lightweight summary returned from search / browse endpoints."""

    id: int
    name: str
    url: str
    creator: str
    thumbnail: Optional[str] = None
    like_count: int = 0
    download_count: int = 0
    collect_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ThingDetail:
    """Full details for a single thing."""

    id: int
    name: str
    url: str
    creator: str
    description: str = ""
    instructions: str = ""
    license: str = ""
    thumbnail: Optional[str] = None
    like_count: int = 0
    download_count: int = 0
    collect_count: int = 0
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    file_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Category:
    """A Thingiverse content category."""

    name: str
    slug: str
    url: str
    count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ThingiverseClient:
    """Typed HTTP client for the Thingiverse REST API.

    Args:
        token: API access token.  If *None*, falls back to the
            ``KILN_THINGIVERSE_TOKEN`` environment variable.
        base_url: Override the API base URL (useful for testing).
        session: Optional :class:`requests.Session` for connection pooling
            or test injection.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = _BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token or os.environ.get("KILN_THINGIVERSE_TOKEN", "")
        if not self._token:
            raise ThingiverseAuthError(
                "Thingiverse API token is required.  Set KILN_THINGIVERSE_TOKEN "
                "or pass token= to ThingiverseClient."
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    # -- low-level request helper ------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout: int = _REQUEST_TIMEOUT,
        _rate_limit_retries: int = 3,
    ) -> Any:
        """Make an authenticated API request and return parsed JSON.

        Automatically retries with exponential backoff on HTTP 429
        (rate limit) responses, respecting the ``Retry-After`` header
        when present.
        """
        url = f"{self._base_url}{path}"
        params = dict(params) if params else {}
        params["access_token"] = self._token

        for attempt in range(_rate_limit_retries + 1):
            try:
                resp = self._session.request(
                    method, url, params=params, timeout=timeout,
                )
            except requests.ConnectionError as exc:
                raise ThingiverseError(
                    f"Connection to Thingiverse failed: {exc}", status_code=None,
                ) from exc
            except requests.Timeout as exc:
                raise ThingiverseError(
                    f"Thingiverse request timed out: {exc}", status_code=None,
                ) from exc

            if resp.status_code == 401:
                raise ThingiverseAuthError(
                    "Invalid or expired Thingiverse API token.",
                    status_code=401,
                )
            if resp.status_code == 404:
                raise ThingiverseNotFoundError(
                    f"Resource not found: {path}", status_code=404,
                )
            if resp.status_code == 429:
                if attempt < _rate_limit_retries:
                    # Parse Retry-After header, fall back to exponential backoff
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = float(retry_after)
                        except (ValueError, TypeError):
                            delay = 2.0 ** attempt
                    else:
                        delay = 2.0 ** attempt  # 1s, 2s, 4s
                    delay = min(delay, 60.0)  # cap at 60s
                    logger.info(
                        "Thingiverse rate limited (429), retrying in %.1fs (attempt %d/%d)",
                        delay, attempt + 1, _rate_limit_retries,
                    )
                    time.sleep(delay)
                    continue
                raise ThingiverseRateLimitError(
                    "Thingiverse API rate limit exceeded after retries.  Try again later.",
                    status_code=429,
                )
            if resp.status_code >= 400:
                raise ThingiverseError(
                    f"Thingiverse API error {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            return resp.json()

        # Should not reach here, but just in case
        raise ThingiverseError("Request failed after retries.", status_code=None)

    # -- search ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> List[ThingSummary]:
        """Search for things by keyword.

        Args:
            query: Search terms.
            page: Page number (1-based).
            per_page: Results per page (max 100).
            sort: Sort order — ``"relevant"``, ``"popular"``, ``"newest"``,
                or ``"makes"``.

        Returns:
            List of matching :class:`ThingSummary` objects.
        """
        data = self._request("GET", f"/search/{quote(query)}", params={
            "page": page,
            "per_page": min(per_page, 100),
            "sort": sort,
        })
        return self._parse_thing_list(data)

    # -- thing detail ------------------------------------------------------

    def get_thing(self, thing_id: int) -> ThingDetail:
        """Get full details for a single thing.

        Args:
            thing_id: Numeric Thingiverse thing ID.

        Raises:
            ThingiverseNotFoundError: If the thing does not exist.
        """
        data = self._request("GET", f"/things/{thing_id}")
        return self._parse_thing_detail(data)

    # -- files -------------------------------------------------------------

    def get_files(self, thing_id: int) -> List[ThingFile]:
        """List downloadable files for a thing.

        Args:
            thing_id: Numeric Thingiverse thing ID.
        """
        data = self._request("GET", f"/things/{thing_id}/files")
        if not isinstance(data, list):
            return []
        return [self._parse_file(f) for f in data]

    def download_file(
        self,
        file_id: int,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        """Download a thing file to a local directory.

        Args:
            file_id: Numeric file ID (from :meth:`get_files`).
            dest_dir: Local directory to save to (created if needed).
            file_name: Override the saved file name.  If *None*, uses the
                original name from the API.

        Returns:
            Absolute path to the downloaded file.

        Raises:
            ThingiverseNotFoundError: If the file does not exist.
            ThingiverseError: On download failure.
        """
        # Get file metadata to learn the download URL and default name.
        meta = self._request("GET", f"/files/{file_id}")
        download_url = meta.get("download_url", "")
        if not download_url:
            raise ThingiverseError(
                f"No download URL for file {file_id}.", status_code=None,
            )

        name = file_name or meta.get("name", f"file_{file_id}")
        dest = Path(dest_dir)
        out_path = dest / name

        from kiln.marketplaces.base import resumable_download

        try:
            return resumable_download(
                self._session,
                download_url,
                out_path,
                params={"access_token": self._token},
                timeout=_DOWNLOAD_TIMEOUT,
            )
        except Exception as exc:
            raise ThingiverseError(
                f"Failed to download file {file_id}: {exc}",
            ) from exc

    # -- browse endpoints --------------------------------------------------

    def popular(self, *, page: int = 1, per_page: int = 20) -> List[ThingSummary]:
        """Browse popular (trending) things."""
        data = self._request("GET", "/popular", params={
            "page": page, "per_page": min(per_page, 100),
        })
        return self._parse_thing_list(data)

    def newest(self, *, page: int = 1, per_page: int = 20) -> List[ThingSummary]:
        """Browse newest things."""
        data = self._request("GET", "/newest", params={
            "page": page, "per_page": min(per_page, 100),
        })
        return self._parse_thing_list(data)

    def featured(self, *, page: int = 1, per_page: int = 20) -> List[ThingSummary]:
        """Browse featured things."""
        data = self._request("GET", "/featured", params={
            "page": page, "per_page": min(per_page, 100),
        })
        return self._parse_thing_list(data)

    # -- categories --------------------------------------------------------

    def list_categories(self) -> List[Category]:
        """List top-level content categories."""
        data = self._request("GET", "/categories")
        if not isinstance(data, list):
            return []
        results: List[Category] = []
        for cat in data:
            if not isinstance(cat, dict):
                continue
            results.append(Category(
                name=cat.get("name", ""),
                slug=cat.get("slug", cat.get("name", "").lower().replace(" ", "-")),
                url=cat.get("url", ""),
                count=cat.get("count", 0),
            ))
        return results

    def category_things(
        self,
        category_slug: str,
        *,
        page: int = 1,
        per_page: int = 20,
    ) -> List[ThingSummary]:
        """Browse things in a specific category."""
        data = self._request(
            "GET",
            f"/categories/{quote(category_slug)}/things",
            params={"page": page, "per_page": min(per_page, 100)},
        )
        return self._parse_thing_list(data)

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_thing_list(data: Any) -> List[ThingSummary]:
        """Parse a list of thing summaries from raw API data."""
        # The search endpoint wraps results in a "hits" key; browse
        # endpoints return a plain list.
        items: list
        if isinstance(data, dict):
            items = data.get("hits", data.get("results", []))
        elif isinstance(data, list):
            items = data
        else:
            return []

        results: List[ThingSummary] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            creator_raw = item.get("creator", {})
            creator_name = (
                creator_raw.get("name", "")
                if isinstance(creator_raw, dict)
                else str(creator_raw)
            )
            results.append(ThingSummary(
                id=item.get("id", 0),
                name=item.get("name", ""),
                url=item.get("public_url", item.get("url", "")),
                creator=creator_name,
                thumbnail=item.get("thumbnail", item.get("preview_image", None)),
                like_count=item.get("like_count", 0),
                download_count=item.get("download_count", 0),
                collect_count=item.get("collect_count", 0),
            ))
        return results

    @staticmethod
    def _parse_thing_detail(data: Dict[str, Any]) -> ThingDetail:
        """Parse a single thing detail from raw API data."""
        creator_raw = data.get("creator", {})
        creator_name = (
            creator_raw.get("name", "")
            if isinstance(creator_raw, dict)
            else str(creator_raw)
        )
        tags_raw = data.get("tags", [])
        tags: List[str] = []
        if isinstance(tags_raw, list):
            for t in tags_raw:
                if isinstance(t, dict):
                    tags.append(t.get("name", str(t.get("tag", ""))))
                else:
                    tags.append(str(t))

        return ThingDetail(
            id=data.get("id", 0),
            name=data.get("name", ""),
            url=data.get("public_url", data.get("url", "")),
            creator=creator_name,
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            license=data.get("license", ""),
            thumbnail=data.get("thumbnail", None),
            like_count=data.get("like_count", 0),
            download_count=data.get("download_count", 0),
            collect_count=data.get("collect_count", 0),
            category=data.get("category", None),
            tags=tags,
            file_count=data.get("file_count", 0),
        )

    @staticmethod
    def _parse_file(data: Dict[str, Any]) -> ThingFile:
        """Parse a single file entry from raw API data."""
        return ThingFile(
            id=data.get("id", 0),
            name=data.get("name", ""),
            size_bytes=data.get("size", 0),
            download_url=data.get("download_url", ""),
            thumbnail_url=data.get("thumbnail", None),
            date=data.get("date", None),
        )
