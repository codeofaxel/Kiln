"""What a buyer agrees to when Kiln routes an order to a print service.

Kiln is orchestration: the print service is the merchant of record, and its
terms govern the order.  This module is the ONE place those terms are
recorded, so every surface that shows them -- the quote, the order, the
status reply, in this repo and in kiln-pro -- reads the same address and
says the same thing.  A second copy of the address rotted silently once
(the service moved its terms page and Kiln kept sending buyers to a 404),
which is why nothing else may spell it out.

Only facts a buyer needs belong here, in plain English: what they agree
to, where they pay, where they write to cancel, and how long an unpaid
order stays open.
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

#: Days an order nobody pays for stays open at Craftcloud before it expires
#: by itself.  An unpaid order is never made, so it has nothing to cancel.
CRAFTCLOUD_UNPAID_EXPIRY_DAYS = 90

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
    "Sales tax, where it applies, is added at Craftcloud's checkout.",
)

_ORDER_TERMS: dict[str, tuple[str, ...]] = {
    "craftcloud": CRAFTCLOUD_ORDER_TERMS,
}

_CHECKOUT_URLS: dict[str, str] = {
    "craftcloud": CRAFTCLOUD_CHECKOUT_URL,
}

_UNPAID_EXPIRY_DAYS: dict[str, int] = {
    "craftcloud": CRAFTCLOUD_UNPAID_EXPIRY_DAYS,
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
    template = _CHECKOUT_URLS.get(_normalized(provider_name), "")
    order_id = (provider_order_id or "").strip()
    if not template or not order_id:
        return ""
    return template.format(order_id=quote(order_id, safe=""))


def unpaid_expiry_days(provider_name: str) -> int | None:
    """Days an unpaid order stays open before it expires, or ``None``.

    ``None`` is "not recorded", never "it never expires".
    """
    return _UNPAID_EXPIRY_DAYS.get(_normalized(provider_name))
