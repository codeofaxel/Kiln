"""What a buyer agrees to when Kiln routes an order to a print service.

Kiln is orchestration: the print service is the merchant of record, and its
terms govern the order.  This module is the ONE place those terms are
recorded, so every surface that shows them -- the quote, the order, the
status reply, in this repo and in kiln-pro -- reads the same address and
says the same thing.  A second copy of the address rotted silently once
(the service moved its terms page and Kiln kept sending buyers to a 404),
which is why nothing else may spell it out.

Only facts a buyer needs belong here, in plain English: what they agree
to, where and how they pay, where they write to cancel, and how long an
unpaid order stays open.
"""

from __future__ import annotations

from urllib.parse import quote

#: Craftcloud's Terms of Contract, the page a buyer is sent to.
CRAFTCLOUD_TERMS_URL = "https://craftcloud3d.com/en/p/terms-and-conditions"

#: Where a buyer asks Craftcloud to cancel or change an order.  Craftcloud's
#: customer API has no cancel call, and its status change refuses Kiln
#: (measured 2026-10-12: HTTP 401), so cancelling is a message to them.
CRAFTCLOUD_SUPPORT_EMAIL = "support@craftcloud3d.com"

#: Where a buyer pays for an order Kiln placed with Craftcloud: that
#: order's own checkout page.  ``{order_id}`` is Craftcloud's id for the
#: order (the one Kiln keeps as the provider order id), not the order
#: number the buyer quotes to support.
CRAFTCLOUD_CHECKOUT_URL = "https://craftcloud3d.com/order/{order_id}/checkout"

#: Where a buyer follows a paid order on Craftcloud, addressed by the same
#: id as its checkout.
CRAFTCLOUD_ORDER_STATUS_URL = "https://craftcloud3d.com/order-status/{order_id}"

#: How a buyer can pay on Craftcloud's checkout, the ways most people
#: reach for.  What a given buyer is offered depends on their country and
#: device: Apple Pay shows only on Apple devices, and several are regional.
CRAFTCLOUD_PAY_METHODS: tuple[str, ...] = (
    "card", "PayPal", "Apple Pay", "Google Pay", "bank transfer",
)

#: The card networks and pay-later service the same checkout shows, so a
#: buyer asking about one hears yes only for one it shows.
CRAFTCLOUD_PAY_ALSO: tuple[str, ...] = (
    "Visa", "Mastercard", "American Express", "Discover", "JCB", "UnionPay",
    "Diners Club", "wire transfer", "Klarna (pay later)",
)

#: Days an order nobody pays for stays open at Craftcloud before it expires
#: by itself.  An unpaid order is never made, so it has nothing to cancel.
CRAFTCLOUD_UNPAID_EXPIRY_DAYS = 90

#: Tax, said beside a quote's price, by where the order ships.  In the US
#: a quote carries no sales tax: the checkout adds it for the buyer's
#: address, which a price request cannot carry.
CRAFTCLOUD_TAX_AT_CHECKOUT = "Sales tax, where it applies, is added at Craftcloud's checkout."

#: Anywhere else, what tax a quoted price already includes depends on the
#: country, so the sentence promises only what is true everywhere.
CRAFTCLOUD_TAX_ELSEWHERE = "The checkout shows any tax for your country before you pay."

#: What a buyer should know before ordering through Craftcloud, said at
#: quote time so nobody has to go looking.
CRAFTCLOUD_ORDER_TERMS: tuple[str, ...] = (
    "Nothing is made until you pay on Craftcloud's checkout. The order "
    "becomes binding when you confirm the purchase there.",
    "A custom print can't be cancelled for a change of mind. A change after "
    "ordering is refunded minus a handling fee of up to 7%.",
    "Craftcloud refunds you in full if no print partner can make it or it "
    "ships much later than promised; Kiln refunds its own fee automatically.",
    "Customs, duties and import taxes are yours to pay.",
)

_ORDER_TERMS: dict[str, tuple[str, ...]] = {
    "craftcloud": CRAFTCLOUD_ORDER_TERMS,
}

#: By provider, the tax sentence by ISO country; ``""`` is everywhere else.
_TAX_NOTES: dict[str, dict[str, str]] = {
    "craftcloud": {"US": CRAFTCLOUD_TAX_AT_CHECKOUT, "": CRAFTCLOUD_TAX_ELSEWHERE},
}

_ORDER_STATUS_URLS: dict[str, str] = {
    "craftcloud": CRAFTCLOUD_ORDER_STATUS_URL,
}

_CHECKOUT_URLS: dict[str, str] = {
    "craftcloud": CRAFTCLOUD_CHECKOUT_URL,
}

_UNPAID_EXPIRY_DAYS: dict[str, int] = {
    "craftcloud": CRAFTCLOUD_UNPAID_EXPIRY_DAYS,
}

_PAY_METHODS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "craftcloud": (CRAFTCLOUD_PAY_METHODS, CRAFTCLOUD_PAY_ALSO),
}


def _normalized(provider_name: str) -> str:
    return (provider_name or "").strip().lower()


def order_terms(provider_name: str) -> list[str]:
    """The plain-English terms a buyer should hear for ``provider_name``.

    Empty for a service nobody has written them for: an empty list is
    "none recorded", never a promise that there is nothing to know.
    """
    return list(_ORDER_TERMS.get(_normalized(provider_name), ()))


def checkout_url(provider_name: str, provider_order_id: str) -> str:
    """Where the buyer pays for ``provider_order_id``, or ``""``.

    Blank for a service with no recorded checkout address and for an empty
    id: no link is better than a link to somebody else's order.
    """
    return _order_page(_CHECKOUT_URLS, provider_name, provider_order_id)


def order_status_url(provider_name: str, provider_order_id: str) -> str:
    """Where the buyer follows ``provider_order_id`` once paid, or ``""``."""
    return _order_page(_ORDER_STATUS_URLS, provider_name, provider_order_id)


def _order_page(pages: dict[str, str], provider_name: str, provider_order_id: str) -> str:
    template = pages.get(_normalized(provider_name), "")
    order_id = (provider_order_id or "").strip()
    if not template or not order_id:
        return ""
    return template.format(order_id=quote(order_id, safe=""))


def unpaid_expiry_days(provider_name: str) -> int | None:
    """Days an unpaid order stays open before it expires, or ``None``.

    ``None`` is "not recorded", never "it never expires".
    """
    return _UNPAID_EXPIRY_DAYS.get(_normalized(provider_name))


def pay_methods(provider_name: str) -> list[str]:
    """Every way the service's checkout offers to pay, as a buyer names them.

    Empty for a service nobody has recorded: "not known", never "none".
    What a given buyer sees still depends on their country and device.
    """
    main, also = _PAY_METHODS.get(_normalized(provider_name), ((), ()))
    return [*main, *also]


def pay_methods_line(provider_name: str) -> str:
    """The one sentence said beside the link to pay, or ``""``."""
    main, _also = _PAY_METHODS.get(_normalized(provider_name), ((), ()))
    if not main:
        return ""
    ways = main[0] if len(main) == 1 else f"{', '.join(main[:-1])} or {main[-1]}"
    return f"Pay however is easiest: {ways}; what's offered depends on your country and device."


def tax_note(provider_name: str, country: str = "") -> str:
    """The one sentence about tax said beside a quote's total, or ``""``.

    By where the order ships (an ISO country code).  A country with no
    sentence of its own, or none given, gets the one true everywhere.
    """
    notes = _TAX_NOTES.get(_normalized(provider_name))
    if not notes:
        return ""
    return notes.get((country or "").strip().upper()) or notes[""]
