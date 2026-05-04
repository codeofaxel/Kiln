"""Tests for fulfillment quote cache helpers."""

from __future__ import annotations

from kiln.quote_cache import QuoteCache


def test_quote_cache_accepts_provider_quote_id() -> None:
    cache = QuoteCache()

    cached = cache.put(
        "craftcloud",
        "PLA Standard (Gray)",
        "pla-gray",
        1,
        3.04,
        "USD",
        5,
        quote_id="provider-quote-1",
    )

    assert cached.quote_id == "provider-quote-1"
    assert cache.get_by_quote_id("provider-quote-1") is cached
