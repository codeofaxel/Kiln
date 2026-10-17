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


def _quote(cache: QuoteCache, quote_id: str, price: float = 3.04):
    return cache.put("craftcloud", "PLA Standard (Gray)", "pla-gray", 1, price, "USD", 5, quote_id=quote_id)


def test_a_second_quote_for_the_same_material_does_not_replace_the_first() -> None:
    """An order names its quote by id.  Two quotes for the same provider,
    material and quantity are two quotes; the later one used to overwrite the
    earlier, and the order placed against the earlier then found nothing."""
    cache = QuoteCache()

    first = _quote(cache, "quote-1", price=3.04)
    second = _quote(cache, "quote-2", price=4.10)

    assert cache.get_by_quote_id("quote-1") is first
    assert cache.get_by_quote_id("quote-2") is second
    assert cache.get_by_quote_id("quote-1").quoted_price == 3.04


def test_a_lookup_by_request_returns_the_newest_matching_quote() -> None:
    cache = QuoteCache()

    _quote(cache, "quote-1", price=3.04)
    newest = _quote(cache, "quote-2", price=4.10)
    newest.cached_at += 1.0  # two puts can share a clock tick

    assert cache.get("craftcloud", "PLA Standard (Gray)", "pla-gray", 1) is newest


def test_the_same_quote_cached_twice_is_still_one_entry() -> None:
    cache = QuoteCache()

    _quote(cache, "quote-1", price=3.04)
    again = _quote(cache, "quote-1", price=3.04)

    assert cache.get_by_quote_id("quote-1") is again
    assert sum(1 for q in cache._cache.values() if q.quote_id == "quote-1") == 1


def test_both_quotes_survive_a_restart(tmp_path) -> None:
    db_path = str(tmp_path / "quotes.db")
    cache = QuoteCache(db_path=db_path)
    _quote(cache, "quote-1", price=3.04)
    _quote(cache, "quote-2", price=4.10)

    reopened = QuoteCache(db_path=db_path)

    assert reopened.get_by_quote_id("quote-1").quoted_price == 3.04
    assert reopened.get_by_quote_id("quote-2").quoted_price == 4.10
