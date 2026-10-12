"""Craftcloud's terms are recorded once and said at quote time.

The address was hand-copied in two repos and both copies pointed at a page
that had moved (a 404) for months.  Now :mod:`kiln.provider_terms` is the
single home, the routing metadata every quote, order and status reply
carries reads from it, and the plain-English terms ride along so a buyer
hears them with the price.
"""

from __future__ import annotations

import kiln.server as server
from kiln import provider_terms


def test_terms_url_is_the_live_page_not_the_moved_one():
    assert provider_terms.CRAFTCLOUD_TERMS_URL == (
        "https://craftcloud3d.com/en/p/terms-and-conditions"
    )


def test_routing_metadata_reads_the_single_copy():
    meta = server._provider_routing_metadata("Craftcloud")
    assert meta["provider_terms_url"] == provider_terms.CRAFTCLOUD_TERMS_URL
    assert meta["provider_order_terms"] == list(provider_terms.CRAFTCLOUD_ORDER_TERMS)
    assert meta["merchant_of_record"] == "provider"


def test_order_terms_cover_what_a_buyer_must_know_before_paying():
    said = " ".join(provider_terms.order_terms("craftcloud")).lower()
    for fact in ("nothing is made until you pay", "7%", "full refund", "customs"):
        assert fact in said, fact


def test_unknown_provider_has_no_terms_recorded_not_a_blank_promise():
    assert provider_terms.order_terms("nobody") == []
    assert server._provider_routing_metadata("nobody")["provider_order_terms"] == []


def test_no_second_copy_of_the_address_in_server_source():
    import inspect

    src = inspect.getsource(server)
    assert "craftcloud3d.com/terms" not in src
    assert "craftcloud3d.com/en/p/terms" not in src


def test_the_cancel_contact_is_recorded_once():
    assert provider_terms.CRAFTCLOUD_SUPPORT_EMAIL == "support@craftcloud3d.com"
