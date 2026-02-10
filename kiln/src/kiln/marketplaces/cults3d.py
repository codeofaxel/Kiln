"""Cults3D marketplace adapter (metadata-only).

Wraps the Cults3D GraphQL API.  Supports search, model details, and
file listing, but does NOT support direct file downloads — users must
visit the Cults3D website to download files.

Environment variables
---------------------
``KILN_CULTS3D_API_KEY``
    Cults3D API key (generated at https://cults3d.com/en/api/keys).
``KILN_CULTS3D_USERNAME``
    Cults3D account username for HTTP Basic auth.
"""

from __future__ import annotations

import base64
import logging
import os
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

_GRAPHQL_URL = "https://cults3d.com/graphql"
_REQUEST_TIMEOUT = 30


class Cults3DAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the Cults3D GraphQL API.

    This is a **metadata-only** adapter — it can search and browse
    models, but cannot download files.  File URLs link to the Cults3D
    website where the user must download manually.

    Args:
        username: Cults3D username for Basic auth.  Falls back to
            ``KILN_CULTS3D_USERNAME``.
        api_key: Cults3D API key.  Falls back to
            ``KILN_CULTS3D_API_KEY``.
        graphql_url: Override the GraphQL endpoint (for testing).
        session: Optional :class:`requests.Session`.
    """

    def __init__(
        self,
        username: str | None = None,
        api_key: str | None = None,
        *,
        graphql_url: str = _GRAPHQL_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._username = username or os.environ.get("KILN_CULTS3D_USERNAME", "")
        self._api_key = api_key or os.environ.get("KILN_CULTS3D_API_KEY", "")
        if not self._username or not self._api_key:
            raise MarketplaceAuthError(
                "Cults3D credentials are required.  Set KILN_CULTS3D_USERNAME "
                "and KILN_CULTS3D_API_KEY, or pass username= and api_key=."
            )
        self._graphql_url = graphql_url
        self._session = session or requests.Session()

        # Pre-compute Basic auth header
        creds = f"{self._username}:{self._api_key}"
        self._auth_header = "Basic " + base64.b64encode(creds.encode()).decode()

    @property
    def name(self) -> str:
        return "cults3d"

    @property
    def display_name(self) -> str:
        return "Cults3D"

    @property
    def supports_download(self) -> bool:
        return False

    # -- low-level GraphQL helper ------------------------------------------

    def _query(
        self,
        graphql: str,
        variables: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL query and return the ``data`` dict."""
        payload: Dict[str, Any] = {"query": graphql}
        if variables:
            payload["variables"] = variables

        try:
            resp = self._session.post(
                self._graphql_url,
                json=payload,
                headers={
                    "Authorization": self._auth_header,
                    "Content-Type": "application/json",
                },
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise MarketplaceError(
                f"Connection to Cults3D failed: {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise MarketplaceError(
                f"Cults3D request timed out: {exc}",
            ) from exc

        if resp.status_code == 401:
            raise MarketplaceAuthError(
                "Invalid Cults3D credentials.", status_code=401,
            )
        if resp.status_code == 429:
            raise MarketplaceRateLimitError(
                "Cults3D rate limit exceeded (60/30s or 500/day).",
                status_code=429,
            )
        if resp.status_code >= 400:
            raise MarketplaceError(
                f"Cults3D API error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        body = resp.json()
        errors = body.get("errors")
        if errors:
            msg = errors[0].get("message", "Unknown GraphQL error")
            raise MarketplaceError(f"Cults3D GraphQL error: {msg}")

        return body.get("data", {})

    # -- MarketplaceAdapter interface --------------------------------------

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> List[ModelSummary]:
        offset = (page - 1) * per_page
        limit = min(per_page, 100)

        gql = """
        query($query: String!, $limit: Int!, $offset: Int!) {
          creationsSearchBatch(query: $query, limit: $limit, offset: $offset) {
            total
            results {
              identifier
              name(locale: EN)
              url(locale: EN)
              shortUrl
              price(currency: USD) { cents }
              license { code name(locale: EN) }
              likesCount
              downloadsCount
              illustrationImageUrl
              creator { nick }
              blueprints { fileUrl imageUrl }
            }
          }
        }
        """
        data = self._query(gql, {
            "query": query,
            "limit": limit,
            "offset": offset,
        })
        batch = data.get("creationsSearchBatch", {})
        results = batch.get("results", [])
        return [self._parse_summary(item) for item in results if isinstance(item, dict)]

    def get_details(self, model_id: str) -> ModelDetail:
        gql = """
        query($id: ID!) {
          creation(identifier: $id) {
            identifier
            name(locale: EN)
            url(locale: EN)
            shortUrl
            publishedAt
            price(currency: USD) { cents }
            license { code name(locale: EN) }
            category { code name(locale: EN) }
            tags(locale: EN)
            likesCount
            downloadsCount
            viewsCount(cached: true)
            illustrationImageUrl
            creator { nick bio }
            blueprints { fileUrl imageUrl }
          }
        }
        """
        data = self._query(gql, {"id": model_id})
        creation = data.get("creation")
        if creation is None:
            raise MarketplaceNotFoundError(
                f"Model {model_id} not found on Cults3D.",
            )
        return self._parse_detail(creation)

    def get_files(self, model_id: str) -> List[ModelFile]:
        gql = """
        query($id: ID!) {
          creation(identifier: $id) {
            blueprints { fileUrl imageUrl }
          }
        }
        """
        data = self._query(gql, {"id": model_id})
        creation = data.get("creation")
        if creation is None:
            raise MarketplaceNotFoundError(
                f"Model {model_id} not found on Cults3D.",
            )
        blueprints = creation.get("blueprints", [])
        return [
            self._parse_blueprint(i, bp)
            for i, bp in enumerate(blueprints)
            if isinstance(bp, dict)
        ]

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _parse_summary(data: Dict[str, Any]) -> ModelSummary:
        price = data.get("price", {}) or {}
        price_cents = price.get("cents", 0) or 0
        is_free = price_cents == 0

        license_data = data.get("license", {}) or {}
        license_str = license_data.get("name", license_data.get("code", ""))

        creator = data.get("creator", {}) or {}

        # Check if any blueprints have file URLs that suggest printable formats
        blueprints = data.get("blueprints", []) or []
        has_files = len(blueprints) > 0

        return ModelSummary(
            id=str(data.get("identifier", "")),
            name=data.get("name", ""),
            url=data.get("url", data.get("shortUrl", "")),
            creator=creator.get("nick", ""),
            source="cults3d",
            thumbnail=data.get("illustrationImageUrl"),
            like_count=data.get("likesCount", 0) or 0,
            download_count=data.get("downloadsCount", 0) or 0,
            license=license_str,
            is_free=is_free,
            price_cents=price_cents,
            has_sliceable_files=has_files,
        )

    @staticmethod
    def _parse_detail(data: Dict[str, Any]) -> ModelDetail:
        price = data.get("price", {}) or {}
        price_cents = price.get("cents", 0) or 0

        license_data = data.get("license", {}) or {}
        license_str = license_data.get("name", license_data.get("code", ""))

        category_data = data.get("category", {}) or {}
        category = category_data.get("name")

        creator = data.get("creator", {}) or {}
        tags = data.get("tags", []) or []
        if not isinstance(tags, list):
            tags = []
        # Flatten tag objects if needed
        tag_strs = []
        for t in tags:
            if isinstance(t, str):
                tag_strs.append(t)
            elif isinstance(t, dict):
                tag_strs.append(t.get("name", str(t)))

        blueprints = data.get("blueprints", []) or []

        return ModelDetail(
            id=str(data.get("identifier", "")),
            name=data.get("name", ""),
            url=data.get("url", data.get("shortUrl", "")),
            creator=creator.get("nick", ""),
            source="cults3d",
            description="",  # Cults3D doesn't expose description via GraphQL
            license=license_str,
            thumbnail=data.get("illustrationImageUrl"),
            like_count=data.get("likesCount", 0) or 0,
            download_count=data.get("downloadsCount", 0) or 0,
            category=category,
            tags=tag_strs,
            file_count=len(blueprints),
            is_free=price_cents == 0,
            price_cents=price_cents,
            can_download=False,  # Cults3D is metadata-only
        )

    @staticmethod
    def _parse_blueprint(index: int, data: Dict[str, Any]) -> ModelFile:
        file_url = data.get("fileUrl", "")
        # Try to extract filename from URL
        name = ""
        if file_url:
            name = file_url.rsplit("/", 1)[-1].split("?")[0]
        if not name:
            name = f"file_{index}"

        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        return ModelFile(
            id=str(index),
            name=name,
            download_url=file_url,
            thumbnail_url=data.get("imageUrl", ""),
            file_type=ext,
        )
