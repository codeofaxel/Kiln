"""Marketplace adapters for 3D model discovery and download.

Provides a uniform interface across multiple 3D model repositories
(Thingiverse, MyMiniFactory, Cults3D, etc.) so agents can search,
browse, and download models from any source through a single set of tools.

Usage::

    from kiln.marketplaces import MarketplaceRegistry

    registry = MarketplaceRegistry()
    registry.register(ThingiverseAdapter(client))
    registry.register(MyMiniFactoryAdapter(api_key="..."))

    # Fan-out search across all connected marketplaces
    results = registry.search_all("benchy")
"""

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
from kiln.marketplaces.thingiverse import ThingiverseAdapter
from kiln.marketplaces.myminifactory import MyMiniFactoryAdapter
from kiln.marketplaces.cults3d import Cults3DAdapter

__all__ = [
    # Base
    "MarketplaceAdapter",
    "MarketplaceError",
    "MarketplaceAuthError",
    "MarketplaceNotFoundError",
    "MarketplaceRateLimitError",
    "ModelDetail",
    "ModelFile",
    "ModelSummary",
    # Adapters
    "ThingiverseAdapter",
    "MyMiniFactoryAdapter",
    "Cults3DAdapter",
    # Registry
    "MarketplaceRegistry",
]


# ---------------------------------------------------------------------------
# Registry — manages connected marketplace adapters
# ---------------------------------------------------------------------------


import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class MarketplaceRegistry:
    """Registry of connected marketplace adapters with unified search."""

    def __init__(self) -> None:
        self._adapters: Dict[str, MarketplaceAdapter] = {}

    def register(self, adapter: MarketplaceAdapter) -> None:
        """Register a marketplace adapter."""
        self._adapters[adapter.name] = adapter

    def unregister(self, name: str) -> bool:
        """Remove an adapter by name.  Returns True if found."""
        return self._adapters.pop(name, None) is not None

    def get(self, name: str) -> MarketplaceAdapter:
        """Get an adapter by name."""
        adapter = self._adapters.get(name)
        if adapter is None:
            raise MarketplaceError(f"Marketplace {name!r} is not connected.")
        return adapter

    @property
    def connected(self) -> List[str]:
        """Names of all connected marketplaces."""
        return list(self._adapters.keys())

    @property
    def count(self) -> int:
        return len(self._adapters)

    def search_all(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 10,
        sort: str = "relevant",
        sources: List[str] | None = None,
    ) -> List[ModelSummary]:
        """Search all connected marketplaces and merge results.

        Args:
            query: Search keywords.
            page: Page number (1-based).
            per_page: Results per source (each marketplace returns up to
                this many results).
            sort: Sort order hint (adapters may interpret differently).
            sources: Optional list of marketplace names to search.
                If omitted, searches all connected marketplaces.

        Returns:
            Merged results from all sources, interleaved for variety.
        """
        targets = list(self._adapters.values())
        if sources:
            targets = [a for a in targets if a.name in sources]

        if not targets:
            return []

        all_results: Dict[str, List[ModelSummary]] = {}
        errors: Dict[str, str] = {}

        # Fan out searches in parallel
        with ThreadPoolExecutor(max_workers=min(len(targets), 5)) as pool:
            futures = {
                pool.submit(
                    adapter.search,
                    query,
                    page=page,
                    per_page=per_page,
                    sort=sort,
                ): adapter
                for adapter in targets
            }
            for future in as_completed(futures):
                adapter = futures[future]
                try:
                    all_results[adapter.name] = future.result()
                except MarketplaceError as exc:
                    logger.warning(
                        "Search failed for %s: %s", adapter.display_name, exc,
                    )
                    errors[adapter.name] = str(exc)
                except Exception as exc:
                    logger.warning(
                        "Unexpected error searching %s: %s",
                        adapter.display_name, exc,
                    )
                    errors[adapter.name] = str(exc)

        # Interleave results for variety (round-robin across sources)
        merged: List[ModelSummary] = []
        source_iters = {
            name: iter(results) for name, results in all_results.items()
        }
        while source_iters:
            exhausted = []
            for name, it in source_iters.items():
                item = next(it, None)
                if item is not None:
                    merged.append(item)
                else:
                    exhausted.append(name)
            for name in exhausted:
                del source_iters[name]

        return merged
