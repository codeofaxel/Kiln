"""Marketplace adapters for 3D model discovery and download.

Provides a uniform interface across multiple 3D model repositories
(Thingiverse, MyMiniFactory, Cults3D, etc.) so agents can search,
browse, and download models from any source through a single set of tools.

Includes per-marketplace health monitoring and circuit breaker logic:
marketplaces that fail 3+ consecutive times are marked DOWN and skipped
until they recover.

Usage::

    from kiln.marketplaces import MarketplaceRegistry

    registry = MarketplaceRegistry()
    registry.register(ThingiverseAdapter(client))
    registry.register(MyMiniFactoryAdapter(api_key="..."))

    # Fan-out search across all connected marketplaces
    results = registry.search_all("benchy")
    print(results.summary)          # "thingiverse: healthy, cults3d: DOWN — ..."
    print(results.models)           # interleaved ModelSummary list

    # Inspect health directly
    for status in registry.marketplace_health():
        print(status.to_dict())
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

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
from kiln.marketplaces.cults3d import Cults3DAdapter
from kiln.marketplaces.myminifactory import MyMiniFactoryAdapter
from kiln.marketplaces.thingiverse import ThingiverseAdapter

logger = logging.getLogger(__name__)

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
    # Health monitoring
    "MarketplaceHealth",
    "MarketplaceStatus",
    "MarketplaceHealthMonitor",
    "MarketplaceSearchResults",
    # Registry
    "MarketplaceRegistry",
]


# ---------------------------------------------------------------------------
# Health monitoring — circuit breaker for marketplace adapters
# ---------------------------------------------------------------------------

_CONSECUTIVE_FAILURE_THRESHOLD = 3


class MarketplaceHealth(enum.Enum):
    """Health state of a marketplace adapter."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class MarketplaceStatus:
    """Health status for a single marketplace."""

    marketplace: str
    health: MarketplaceHealth
    last_check: float | None = None
    response_time_ms: float | None = None
    error: str | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["health"] = self.health.value
        return data


class MarketplaceHealthMonitor:
    """Tracks per-marketplace health via consecutive failure counting.

    Follows the same pattern as the fulfillment HealthMonitor:
    * 0 consecutive failures -> HEALTHY
    * 1-2 consecutive failures -> DEGRADED
    * 3+ consecutive failures -> DOWN (circuit breaker open)

    A single success resets the counter back to HEALTHY.
    """

    def __init__(self) -> None:
        self._statuses: dict[str, MarketplaceStatus] = {}
        self._lock = threading.Lock()

    def record_success(
        self,
        marketplace_name: str,
        *,
        response_time_ms: float = 0.0,
    ) -> None:
        """Record a successful API call to a marketplace."""
        with self._lock:
            self._statuses[marketplace_name] = MarketplaceStatus(
                marketplace=marketplace_name,
                health=MarketplaceHealth.HEALTHY,
                last_check=time.time(),
                response_time_ms=response_time_ms,
                consecutive_failures=0,
            )

    def record_failure(
        self,
        marketplace_name: str,
        *,
        error: str = "",
    ) -> None:
        """Record a failed API call to a marketplace."""
        with self._lock:
            existing = self._statuses.get(marketplace_name)
            failures = (existing.consecutive_failures if existing else 0) + 1
            health = MarketplaceHealth.DEGRADED if failures < _CONSECUTIVE_FAILURE_THRESHOLD else MarketplaceHealth.DOWN
            self._statuses[marketplace_name] = MarketplaceStatus(
                marketplace=marketplace_name,
                health=health,
                last_check=time.time(),
                error=error,
                consecutive_failures=failures,
            )

    def get_status(self, marketplace_name: str) -> MarketplaceStatus:
        """Return current health status for a marketplace."""
        with self._lock:
            return self._statuses.get(
                marketplace_name,
                MarketplaceStatus(
                    marketplace=marketplace_name,
                    health=MarketplaceHealth.UNKNOWN,
                ),
            )

    def get_all_statuses(self) -> list[MarketplaceStatus]:
        """Return health statuses for all known marketplaces."""
        with self._lock:
            return list(self._statuses.values())

    def is_available(self, marketplace_name: str) -> bool:
        """Return True if the marketplace is not DOWN.

        UNKNOWN and DEGRADED marketplaces are still queried — only DOWN
        (3+ consecutive failures) triggers the circuit breaker.
        """
        status = self.get_status(marketplace_name)
        return status.health != MarketplaceHealth.DOWN


# ---------------------------------------------------------------------------
# Search results dataclass (models + health context for the agent)
# ---------------------------------------------------------------------------


@dataclass
class MarketplaceSearchResults:
    """Search results enriched with per-marketplace health information.

    Agents receive both the interleaved model list and a human-readable
    summary indicating which sources were queried, which were skipped
    (circuit-breaker open), and which failed on this request.
    """

    models: list[ModelSummary]
    health: dict[str, MarketplaceStatus]
    searched: list[str]
    skipped: list[str]
    failed: list[str]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": [m.to_dict() for m in self.models],
            "health": {k: v.to_dict() for k, v in self.health.items()},
            "searched": self.searched,
            "skipped": self.skipped,
            "failed": self.failed,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Registry — manages connected marketplace adapters
# ---------------------------------------------------------------------------


class MarketplaceRegistry:
    """Registry of connected marketplace adapters with unified search.

    Integrates a :class:`MarketplaceHealthMonitor` to track per-marketplace
    health and skip DOWN sources automatically (circuit breaker pattern).
    """

    def __init__(self) -> None:
        self._adapters: dict[str, MarketplaceAdapter] = {}
        self._health = MarketplaceHealthMonitor()

    @property
    def health_monitor(self) -> MarketplaceHealthMonitor:
        """Expose the health monitor for external inspection."""
        return self._health

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
    def connected(self) -> list[str]:
        """Names of all connected marketplaces."""
        return list(self._adapters.keys())

    @property
    def count(self) -> int:
        return len(self._adapters)

    def marketplace_health(self) -> list[MarketplaceStatus]:
        """Return current health status of all connected marketplaces.

        Marketplaces that have never been queried are reported as UNKNOWN.
        """
        statuses: list[MarketplaceStatus] = []
        for name in self._adapters:
            statuses.append(self._health.get_status(name))
        return statuses

    def search_all(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 10,
        sort: str = "relevant",
        sources: list[str] | None = None,
    ) -> MarketplaceSearchResults:
        """Search all connected marketplaces and merge results.

        Marketplaces marked DOWN by the circuit breaker are skipped.
        Success and failure are recorded in the health monitor so that
        the circuit breaker state stays current.

        Args:
            query: Search keywords.
            page: Page number (1-based).
            per_page: Results per source (each marketplace returns up to
                this many results).
            sort: Sort order hint (adapters may interpret differently).
            sources: Optional list of marketplace names to search.
                If omitted, searches all connected marketplaces.

        Returns:
            :class:`MarketplaceSearchResults` with interleaved models,
            per-marketplace health, and a human-readable summary.
        """
        candidates = list(self._adapters.values())
        if sources:
            candidates = [a for a in candidates if a.name in sources]

        # Separate available vs circuit-broken marketplaces
        targets: list[MarketplaceAdapter] = []
        skipped: list[str] = []
        for adapter in candidates:
            if self._health.is_available(adapter.name):
                targets.append(adapter)
            else:
                skipped.append(adapter.name)
                logger.info(
                    "Skipping %s (DOWN — %d consecutive failures)",
                    adapter.display_name,
                    self._health.get_status(adapter.name).consecutive_failures,
                )

        all_results: dict[str, list[ModelSummary]] = {}
        searched: list[str] = []
        failed: list[str] = []

        if targets:
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
                    start_ns = time.monotonic_ns()
                    try:
                        results = future.result()
                        elapsed_ms = (time.monotonic_ns() - start_ns) / 1e6
                        self._health.record_success(
                            adapter.name,
                            response_time_ms=elapsed_ms,
                        )
                        all_results[adapter.name] = results
                        searched.append(adapter.name)
                    except (MarketplaceError, RuntimeError, OSError) as exc:
                        logger.warning(
                            "Search failed for %s: %s",
                            adapter.display_name,
                            exc,
                        )
                        self._health.record_failure(
                            adapter.name,
                            error=str(exc),
                        )
                        failed.append(adapter.name)

        # Interleave results for variety (round-robin across sources)
        merged: list[ModelSummary] = []
        source_iters = {name: iter(results) for name, results in all_results.items()}
        while source_iters:
            exhausted: list[str] = []
            for name, it in source_iters.items():
                item = next(it, None)
                if item is not None:
                    merged.append(item)
                else:
                    exhausted.append(name)
            for name in exhausted:
                del source_iters[name]

        # Build human-readable summary for the agent
        health_snapshot = {name: self._health.get_status(name) for name in self._adapters}
        summary = _build_search_summary(
            searched=searched,
            skipped=skipped,
            failed=failed,
            health=health_snapshot,
            result_count=len(merged),
        )

        return MarketplaceSearchResults(
            models=merged,
            health=health_snapshot,
            searched=searched,
            skipped=skipped,
            failed=failed,
            summary=summary,
        )


def _build_search_summary(
    *,
    searched: list[str],
    skipped: list[str],
    failed: list[str],
    health: dict[str, MarketplaceStatus],
    result_count: int,
) -> str:
    """Build a concise human-readable summary of the search outcome."""
    parts: list[str] = []

    # Health status line: "thingiverse: healthy, cults3d: DOWN"
    status_tokens = [f"{name}: {status.health.value}" for name, status in health.items()]
    if status_tokens:
        parts.append(", ".join(status_tokens))

    if searched:
        parts.append(f"Results from {', '.join(searched)} ({result_count} total).")
    elif not skipped and not failed:
        parts.append("No marketplaces connected.")

    if skipped:
        parts.append(f"Skipped (DOWN): {', '.join(skipped)}.")

    if failed:
        parts.append(f"Failed this request: {', '.join(failed)}.")

    return " ".join(parts)
