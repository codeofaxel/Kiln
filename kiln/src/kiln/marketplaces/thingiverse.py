"""Thingiverse marketplace adapter.

Wraps :class:`kiln.thingiverse.ThingiverseClient` to implement the
:class:`~kiln.marketplaces.base.MarketplaceAdapter` interface.
"""

from __future__ import annotations

from typing import List, Optional

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
from kiln.thingiverse import (
    ThingiverseAuthError,
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
    ThingiverseRateLimitError,
)


def _wrap_error(exc: ThingiverseError) -> MarketplaceError:
    """Convert a Thingiverse-specific exception to a generic one."""
    if isinstance(exc, ThingiverseAuthError):
        return MarketplaceAuthError(str(exc), status_code=exc.status_code)
    if isinstance(exc, ThingiverseNotFoundError):
        return MarketplaceNotFoundError(str(exc), status_code=exc.status_code)
    if isinstance(exc, ThingiverseRateLimitError):
        return MarketplaceRateLimitError(str(exc), status_code=exc.status_code)
    return MarketplaceError(str(exc), status_code=exc.status_code)


class ThingiverseAdapter(MarketplaceAdapter):
    """Marketplace adapter backed by the Thingiverse REST API."""

    def __init__(self, client: ThingiverseClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "thingiverse"

    @property
    def display_name(self) -> str:
        return "Thingiverse"

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> List[ModelSummary]:
        try:
            results = self._client.search(query, page=page, per_page=per_page, sort=sort)
        except ThingiverseError as exc:
            raise _wrap_error(exc) from exc

        return [
            ModelSummary(
                id=str(r.id),
                name=r.name,
                url=r.url,
                creator=r.creator,
                source="thingiverse",
                thumbnail=r.thumbnail,
                like_count=r.like_count,
                download_count=r.download_count,
            )
            for r in results
        ]

    def get_details(self, model_id: str) -> ModelDetail:
        try:
            d = self._client.get_thing(int(model_id))
        except ThingiverseError as exc:
            raise _wrap_error(exc) from exc

        return ModelDetail(
            id=str(d.id),
            name=d.name,
            url=d.url,
            creator=d.creator,
            source="thingiverse",
            description=d.description,
            instructions=d.instructions,
            license=d.license,
            thumbnail=d.thumbnail,
            like_count=d.like_count,
            download_count=d.download_count,
            category=d.category,
            tags=d.tags,
            file_count=d.file_count,
        )

    def get_files(self, model_id: str) -> List[ModelFile]:
        try:
            files = self._client.get_files(int(model_id))
        except ThingiverseError as exc:
            raise _wrap_error(exc) from exc

        return [
            ModelFile(
                id=str(f.id),
                name=f.name,
                size_bytes=f.size_bytes,
                download_url=f.download_url,
                thumbnail_url=f.thumbnail_url,
                date=f.date,
                file_type=f.name.rsplit(".", 1)[-1].lower() if "." in f.name else "",
            )
            for f in files
        ]

    def download_file(
        self,
        file_id: str,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        try:
            return self._client.download_file(
                int(file_id), dest_dir, file_name=file_name,
            )
        except ThingiverseError as exc:
            raise _wrap_error(exc) from exc
