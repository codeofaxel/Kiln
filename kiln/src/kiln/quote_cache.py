"""Quote cache for fulfillment provider manufacturing quotes.

Agents frequently request quotes from external fulfillment providers
(Sculpteo, CraftCloud, etc.) for the same printer/material/quantity
combination.  This module caches those quotes with configurable TTL
so repeated requests are served from memory (or optional SQLite
persistence) instead of hitting external APIs.

TTL resolution order::

    1. Provider-specific TTL  (``config.ttl_by_provider[provider]``)
    2. Service-specific TTL   (``config.ttl_by_service[service_type]``)
    3. Default TTL            (``config.default_ttl_seconds``)

The default TTL can be overridden via the ``KILN_QUOTE_CACHE_TTL``
environment variable (value in seconds).

Usage::

    from kiln.quote_cache import cache_quote, get_cached_quote

    cached = get_cached_quote("sculpteo", "fdm_printing", "pla_white", 10)
    if cached is None:
        quote = provider.get_quote(file, material, qty)
        cached = cache_quote("sculpteo", "fdm_printing", "pla_white", 10,
                             quote.total_price_usd, "USD", quote.lead_time_days)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CachedQuote:
    """A cached manufacturing quote with expiry metadata.

    :param quote_id: Auto-generated unique identifier.
    :param provider_name: Machine-readable provider name (e.g. ``"sculpteo"``).
    :param service_type: Printing service (e.g. ``"fdm_printing"``).
    :param material: Material identifier (e.g. ``"pla_white"``).
    :param quantity: Number of parts quoted.
    :param quoted_price: Total quoted price.
    :param currency: ISO 4217 currency code.
    :param lead_time_days: Estimated manufacturing lead time.
    :param cached_at: Unix timestamp when the quote was cached.
    :param expires_at: Unix timestamp when the quote expires.
    :param cache_key: SHA-256 dedup key derived from quote parameters.
    :param metadata: Arbitrary extra data from the provider.
    """

    quote_id: str
    provider_name: str
    service_type: str
    material: str
    quantity: int
    quoted_price: float
    currency: str
    lead_time_days: int
    cached_at: float
    expires_at: float
    cache_key: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Return ``True`` if the quote has passed its expiry time."""
        return time.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        data = asdict(self)
        data["is_expired"] = self.is_expired
        return data


@dataclass
class QuoteCacheConfig:
    """Configuration for :class:`QuoteCache` TTL and size limits.

    :param default_ttl_seconds: Default time-to-live in seconds (1 hour).
        Overridable via ``KILN_QUOTE_CACHE_TTL`` env var.
    :param max_entries: Maximum number of cached quotes before eviction.
    :param ttl_by_provider: Per-provider TTL overrides.
    :param ttl_by_service: Per-service-type TTL overrides.
    """

    default_ttl_seconds: int = 3600
    max_entries: int = 1000
    ttl_by_provider: dict[str, int] = field(default_factory=dict)
    ttl_by_service: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Apply env var override for default TTL if set."""
        env_ttl = os.environ.get("KILN_QUOTE_CACHE_TTL")
        if env_ttl is not None:
            try:
                self.default_ttl_seconds = int(env_ttl)
            except ValueError:
                logger.warning(
                    "KILN_QUOTE_CACHE_TTL=%r is not a valid integer, "
                    "using default %d",
                    env_ttl,
                    self.default_ttl_seconds,
                )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return asdict(self)


# ---------------------------------------------------------------------------
# QuoteCache
# ---------------------------------------------------------------------------


class QuoteCache:
    """In-memory quote cache with optional SQLite persistence.

    Thread-safe via :class:`threading.Lock`.  Supports configurable
    TTL per provider, per service type, or a global default.

    :param config: Cache configuration.  Uses defaults if ``None``.
    :param db_path: Path to a SQLite database for persistence.
        If ``None``, the cache is in-memory only.
    """

    def __init__(
        self,
        *,
        config: Optional[QuoteCacheConfig] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._config = config or QuoteCacheConfig()
        self._lock = threading.Lock()
        self._cache: dict[str, CachedQuote] = {}
        self._hits: int = 0
        self._misses: int = 0

        # Optional SQLite persistence.
        self._conn: Optional[sqlite3.Connection] = None
        if db_path is not None:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._ensure_schema()
            self._load_from_db()

    # ------------------------------------------------------------------
    # Schema (SQLite persistence)
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the quotes table if it does not already exist."""
        if self._conn is None:
            return
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quote_cache (
                cache_key       TEXT PRIMARY KEY,
                quote_id        TEXT NOT NULL,
                provider_name   TEXT NOT NULL,
                service_type    TEXT NOT NULL,
                material        TEXT NOT NULL,
                quantity        INTEGER NOT NULL,
                quoted_price    REAL NOT NULL,
                currency        TEXT NOT NULL,
                lead_time_days  INTEGER NOT NULL,
                cached_at       REAL NOT NULL,
                expires_at      REAL NOT NULL,
                metadata_json   TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_qc_provider
                ON quote_cache(provider_name);
            CREATE INDEX IF NOT EXISTS idx_qc_service
                ON quote_cache(service_type);
            CREATE INDEX IF NOT EXISTS idx_qc_expires
                ON quote_cache(expires_at);
            """
        )
        self._conn.commit()

    def _load_from_db(self) -> None:
        """Load non-expired entries from SQLite into memory."""
        if self._conn is None:
            return
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM quote_cache WHERE expires_at > ?", (now,)
        ).fetchall()
        for row in rows:
            d = dict(row)
            quote = CachedQuote(
                quote_id=d["quote_id"],
                provider_name=d["provider_name"],
                service_type=d["service_type"],
                material=d["material"],
                quantity=d["quantity"],
                quoted_price=d["quoted_price"],
                currency=d["currency"],
                lead_time_days=d["lead_time_days"],
                cached_at=d["cached_at"],
                expires_at=d["expires_at"],
                cache_key=d["cache_key"],
                metadata=json.loads(d["metadata_json"]),
            )
            self._cache[quote.cache_key] = quote

    def _persist(self, quote: CachedQuote) -> None:
        """Write a single quote to the SQLite store."""
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO quote_cache
                (cache_key, quote_id, provider_name, service_type, material,
                 quantity, quoted_price, currency, lead_time_days,
                 cached_at, expires_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote.cache_key,
                quote.quote_id,
                quote.provider_name,
                quote.service_type,
                quote.material,
                quote.quantity,
                quote.quoted_price,
                quote.currency,
                quote.lead_time_days,
                quote.cached_at,
                quote.expires_at,
                json.dumps(quote.metadata),
            ),
        )
        self._conn.commit()

    def _delete_from_db(self, cache_key: str) -> None:
        """Remove a single entry from the SQLite store."""
        if self._conn is None:
            return
        self._conn.execute(
            "DELETE FROM quote_cache WHERE cache_key = ?", (cache_key,)
        )
        self._conn.commit()

    def _delete_provider_from_db(self, provider: str) -> None:
        """Remove all entries for a provider from the SQLite store."""
        if self._conn is None:
            return
        self._conn.execute(
            "DELETE FROM quote_cache WHERE provider_name = ?", (provider,)
        )
        self._conn.commit()

    def _clear_db(self) -> None:
        """Remove all entries from the SQLite store."""
        if self._conn is None:
            return
        self._conn.execute("DELETE FROM quote_cache")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_cache_key(
        self,
        provider: str,
        service_type: str,
        material: str,
        quantity: int,
    ) -> str:
        """Generate a deterministic cache key from quote parameters.

        :param provider: Provider name.
        :param service_type: Printing service type.
        :param material: Material identifier.
        :param quantity: Part quantity.
        :returns: SHA-256 hex digest of the normalised concatenation.
        """
        raw = (
            f"{provider.lower().strip()}|"
            f"{service_type.lower().strip()}|"
            f"{material.lower().strip()}|"
            f"{quantity}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_ttl(self, provider: str, service_type: str) -> int:
        """Resolve the TTL for a given provider and service type.

        Resolution order: provider-specific -> service-specific -> default.

        :param provider: Provider name.
        :param service_type: Printing service type.
        :returns: TTL in seconds.
        """
        if provider in self._config.ttl_by_provider:
            return self._config.ttl_by_provider[provider]
        if service_type in self._config.ttl_by_service:
            return self._config.ttl_by_service[service_type]
        return self._config.default_ttl_seconds

    def _evict_oldest(self) -> None:
        """Evict the oldest entry if the cache exceeds max_entries.

        Must be called while holding ``self._lock``.
        """
        while len(self._cache) > self._config.max_entries:
            oldest_key = min(
                self._cache, key=lambda k: self._cache[k].cached_at
            )
            evicted = self._cache.pop(oldest_key)
            self._delete_from_db(evicted.cache_key)
            logger.debug(
                "Evicted quote %s (provider=%s) due to max_entries limit",
                evicted.quote_id,
                evicted.provider_name,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        provider: str,
        service_type: str,
        material: str,
        quantity: int,
        price: float,
        currency: str,
        lead_time_days: int,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> CachedQuote:
        """Cache a fulfillment provider quote.

        :param provider: Provider name (e.g. ``"sculpteo"``).
        :param service_type: Printing service type (e.g. ``"fdm_printing"``).
        :param material: Material identifier (e.g. ``"pla_white"``).
        :param quantity: Part quantity.
        :param price: Quoted total price.
        :param currency: ISO 4217 currency code.
        :param lead_time_days: Manufacturing lead time in days.
        :param metadata: Optional extra data from the provider.
        :returns: The newly cached :class:`CachedQuote`.
        """
        cache_key = self._make_cache_key(provider, service_type, material, quantity)
        ttl = self._get_ttl(provider, service_type)
        now = time.time()

        quote = CachedQuote(
            quote_id=str(uuid.uuid4()),
            provider_name=provider,
            service_type=service_type,
            material=material,
            quantity=quantity,
            quoted_price=price,
            currency=currency,
            lead_time_days=lead_time_days,
            cached_at=now,
            expires_at=now + ttl,
            cache_key=cache_key,
            metadata=metadata or {},
        )

        with self._lock:
            self._cache[cache_key] = quote
            self._evict_oldest()
            self._persist(quote)

        logger.debug(
            "Cached quote %s for %s/%s/%s x%d (ttl=%ds)",
            quote.quote_id,
            provider,
            service_type,
            material,
            quantity,
            ttl,
        )
        return quote

    def get(
        self,
        provider: str,
        service_type: str,
        material: str,
        quantity: int,
    ) -> Optional[CachedQuote]:
        """Retrieve a cached quote if it exists and has not expired.

        Returns ``None`` if no matching quote is found or if the
        cached entry has expired (expired entries are auto-cleaned).

        :param provider: Provider name.
        :param service_type: Printing service type.
        :param material: Material identifier.
        :param quantity: Part quantity.
        :returns: The cached quote, or ``None``.
        """
        cache_key = self._make_cache_key(provider, service_type, material, quantity)

        with self._lock:
            quote = self._cache.get(cache_key)
            if quote is None:
                self._misses += 1
                return None
            if quote.is_expired:
                del self._cache[cache_key]
                self._delete_from_db(cache_key)
                self._misses += 1
                return None
            self._hits += 1
            return quote

    def get_all_for_printer(self, service_type: str) -> list[CachedQuote]:
        """Return all non-expired cached quotes for a service type.

        Useful for cross-provider comparison (e.g. all FDM printing quotes
        across Sculpteo and CraftCloud).

        :param service_type: Printing service type to filter by.
        :returns: List of non-expired quotes for the service.
        """
        results: list[CachedQuote] = []
        expired_keys: list[str] = []

        with self._lock:
            for key, quote in self._cache.items():
                if quote.service_type != service_type:
                    continue
                if quote.is_expired:
                    expired_keys.append(key)
                    continue
                results.append(quote)

            # Auto-clean expired entries found during scan.
            for key in expired_keys:
                del self._cache[key]
                self._delete_from_db(key)

        return results

    def get_by_quote_id(self, quote_id: str) -> Optional[CachedQuote]:
        """Look up a cached quote by its provider-assigned quote ID.

        Unlike :meth:`get`, this does **not** auto-evict expired entries —
        the caller decides how to handle expiry (e.g. validation errors).

        :param quote_id: The quote ID assigned by the fulfillment provider.
        :returns: The matching :class:`CachedQuote`, or ``None``.
        """
        with self._lock:
            for quote in self._cache.values():
                if quote.quote_id == quote_id:
                    return quote
        return None

    def invalidate(self, provider: str) -> int:
        """Remove all cached quotes for a specific provider.

        :param provider: Provider name to invalidate.
        :returns: Number of entries removed.
        """
        removed = 0
        with self._lock:
            keys_to_remove = [
                key
                for key, quote in self._cache.items()
                if quote.provider_name == provider
            ]
            for key in keys_to_remove:
                del self._cache[key]
                removed += 1
            self._delete_provider_from_db(provider)

        if removed:
            logger.debug(
                "Invalidated %d cached quotes for provider %s", removed, provider
            )
        return removed

    def invalidate_all(self) -> int:
        """Clear the entire quote cache.

        :returns: Number of entries removed.
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._clear_db()

        if count:
            logger.debug("Invalidated all %d cached quotes", count)
        return count

    def cleanup(self) -> int:
        """Remove all expired entries from the cache.

        :returns: Number of expired entries removed.
        """
        expired_keys: list[str] = []

        with self._lock:
            for key, quote in self._cache.items():
                if quote.is_expired:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._cache[key]
                self._delete_from_db(key)

        if expired_keys:
            logger.debug(
                "Cleaned up %d expired quote cache entries", len(expired_keys)
            )
        return len(expired_keys)

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        :returns: Dict with ``total``, ``expired``, ``by_provider``,
            and ``hit_rate`` keys.
        """
        with self._lock:
            total = len(self._cache)
            expired = sum(1 for q in self._cache.values() if q.is_expired)
            by_provider: dict[str, int] = {}
            for quote in self._cache.values():
                by_provider[quote.provider_name] = (
                    by_provider.get(quote.provider_name, 0) + 1
                )
            total_requests = self._hits + self._misses
            hit_rate = (
                self._hits / total_requests if total_requests > 0 else 0.0
            )

        return {
            "total": total,
            "expired": expired,
            "by_provider": by_provider,
            "hit_rate": hit_rate,
            "hits": self._hits,
            "misses": self._misses,
        }

    def close(self) -> None:
        """Close the SQLite connection if one is open."""
        if self._conn is not None:
            self._conn.close()


# ---------------------------------------------------------------------------
# Module-level singleton and convenience functions
# ---------------------------------------------------------------------------

_quote_cache: Optional[QuoteCache] = None


def get_quote_cache() -> QuoteCache:
    """Return the module-level :class:`QuoteCache` singleton.

    Lazily created on first call with default configuration.
    The default TTL can be overridden via the ``KILN_QUOTE_CACHE_TTL``
    environment variable.
    """
    global _quote_cache
    if _quote_cache is None:
        _quote_cache = QuoteCache()
    return _quote_cache


def cache_quote(
    provider: str,
    service_type: str,
    material: str,
    quantity: int,
    price: float,
    currency: str,
    lead_time_days: int,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> CachedQuote:
    """Convenience wrapper to cache a quote via the singleton.

    :param provider: Provider name (e.g. ``"sculpteo"``).
    :param service_type: Printing service type.
    :param material: Material identifier.
    :param quantity: Part quantity.
    :param price: Quoted total price.
    :param currency: ISO 4217 currency code.
    :param lead_time_days: Manufacturing lead time in days.
    :param metadata: Optional extra data.
    :returns: The newly cached :class:`CachedQuote`.
    """
    return get_quote_cache().put(
        provider,
        service_type,
        material,
        quantity,
        price,
        currency,
        lead_time_days,
        metadata=metadata,
    )


def get_cached_quote(
    provider: str,
    service_type: str,
    material: str,
    quantity: int,
) -> Optional[CachedQuote]:
    """Convenience wrapper to look up a cached quote via the singleton.

    :param provider: Provider name.
    :param service_type: Printing service type.
    :param material: Material identifier.
    :param quantity: Part quantity.
    :returns: The cached quote, or ``None`` if not found / expired.
    """
    return get_quote_cache().get(provider, service_type, material, quantity)


def get_cached_quote_by_id(quote_id: str) -> Optional[CachedQuote]:
    """Look up a cached quote by its provider-assigned quote ID.

    :param quote_id: The quote ID from the fulfillment provider.
    :returns: The cached quote, or ``None`` if not found.
    """
    return get_quote_cache().get_by_quote_id(quote_id)


__all__ = [
    "CachedQuote",
    "QuoteCache",
    "QuoteCacheConfig",
    "cache_quote",
    "get_cached_quote",
    "get_cached_quote_by_id",
    "get_quote_cache",
]
