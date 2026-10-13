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


def test_order_terms_say_who_refunds_what():
    """The refund line names the refunder.  "You get a full refund" read as
    if Kiln refunded the whole order; the print service refunds the
    manufacturing price and Kiln refunds only its own fee (Terms §5.6)."""
    said = " ".join(provider_terms.order_terms("craftcloud")).lower()
    for fact in (
        "nothing is made until you pay",
        "7%",
        "craftcloud refunds you in full",
        "kiln refunds its own fee",
        "customs",
    ):
        assert fact in said, fact
    assert "you get a full refund" not in said


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


def test_the_pay_link_is_the_orders_own_checkout_page():
    """A buyer pays for an order on that order's own checkout page, named by
    the service's id for the order -- never by the number they quote to
    support -- and an order nothing is known about gets no link at all."""
    order_id = "20106a2d-1fc5-4a04-8393-6139e5803dfd"
    assert provider_terms.checkout_url("Craftcloud", order_id) == (
        f"https://craftcloud3d.com/order/{order_id}/checkout"
    )
    assert provider_terms.checkout_url("craftcloud", "") == ""
    assert provider_terms.checkout_url("nobody", order_id) == ""
    assert provider_terms.checkout_url("craftcloud", "../x?y") == (
        "https://craftcloud3d.com/order/..%2Fx%3Fy/checkout"
    ), "an id never reshapes the address"


def test_the_tax_sentence_follows_where_the_order_ships():
    """A US quote carries no sales tax, and the checkout adds it.  Elsewhere
    a quoted price may already include tax (a German price answer does), so
    the sentence promises only what is true everywhere -- and the terms,
    said to every buyer, carry no tax claim at all."""
    us = "Sales tax, where it applies, is added at Craftcloud's checkout."
    elsewhere = "The checkout shows any tax for your country before you pay."
    assert provider_terms.tax_note("craftcloud", "US") == provider_terms.tax_note("Craftcloud", " us ") == us
    for country in ("DE", "GB", "CA", ""):
        assert provider_terms.tax_note("craftcloud", country) == elsewhere, country
    assert not any("tax" in term.lower() and "import" not in term.lower()
                   for term in provider_terms.order_terms("craftcloud"))
    assert server._provider_routing_metadata("craftcloud", shipping_country="US")["provider_tax_note"] == us
    assert server._provider_routing_metadata("craftcloud", shipping_country="DE")["provider_tax_note"] == elsewhere


def test_how_long_an_unpaid_order_stays_open_is_recorded_once():
    assert provider_terms.unpaid_expiry_days("Craftcloud") == provider_terms.CRAFTCLOUD_UNPAID_EXPIRY_DAYS == 90
    assert provider_terms.unpaid_expiry_days("nobody") is None, "not recorded, never 'never expires'"


def test_how_to_pay_is_said_once_beside_the_link():
    """One sentence, from one list: a confident nudge, and honest that what
    a buyer sees depends on their country and device."""
    assert provider_terms.pay_methods_line("Craftcloud") == (
        "Pay however is easiest: card, PayPal, Apple Pay, Google Pay or bank transfer; "
        "what's offered depends on your country and device."
    )
    methods = provider_terms.pay_methods("craftcloud")
    for method in ("card", "PayPal", "Apple Pay", "Google Pay", "bank transfer", "American Express", "Klarna (pay later)"):
        assert method in methods
    assert "Bitcoin" not in methods
    assert provider_terms.pay_methods("nobody") == [] and provider_terms.pay_methods_line("nobody") == ""
    assert server._provider_routing_metadata("craftcloud")["provider_pay_methods"] == methods


def test_a_service_with_no_tax_sentence_says_none():
    assert provider_terms.tax_note("nobody", "US") == ""
    assert server._provider_routing_metadata("nobody")["provider_tax_note"] == ""
