"""What a buyer agrees to when Kiln routes an order to a print service.

Kiln is orchestration: the print service is the merchant of record, and its
terms govern the order.  This module is the ONE place those terms are
recorded, so every surface that shows them -- the quote, the order, the
status reply, in this repo and in kiln-pro -- reads the same address and
says the same thing.  A second copy of the address rotted silently once
(the service moved its terms page and Kiln kept sending buyers to a 404),
which is why nothing else may spell it out.

Only facts a buyer needs before paying belong here, in plain English.
"""

from __future__ import annotations

#: Craftcloud's Terms of Contract, the page a buyer is sent to.
CRAFTCLOUD_TERMS_URL = "https://craftcloud3d.com/en/p/terms-and-conditions"

#: What a buyer should know before ordering through Craftcloud, said at
#: quote time so nobody has to go looking.
CRAFTCLOUD_ORDER_TERMS: tuple[str, ...] = (
    "Nothing is made until you pay on Craftcloud's checkout. The order "
    "becomes binding when you confirm the purchase there.",
    "A custom print can't be cancelled for a change of mind. A change after "
    "ordering is refunded minus a handling fee of up to 7%.",
    "You get a full refund if no print partner can make it, or if it ships "
    "much later than promised.",
    "Customs, duties and import taxes are yours to pay.",
)

_ORDER_TERMS: dict[str, tuple[str, ...]] = {
    "craftcloud": CRAFTCLOUD_ORDER_TERMS,
}


def order_terms(provider_name: str) -> list[str]:
    """The plain-English terms a buyer should hear for ``provider_name``.

    Empty for a service nobody has written them for: an empty list is
    "none recorded", never a promise that there is nothing to know.
    """
    normalized = (provider_name or "").strip().lower()
    return list(_ORDER_TERMS.get(normalized, ()))
