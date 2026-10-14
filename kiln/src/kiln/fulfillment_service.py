"""Whether this install can order prints from a print service.

Ordering a print from a print service (its materials, a quote, placing,
tracking or cancelling an order, the order history) runs through Kiln's
order service.  It ships with kiln-pro (https://kiln3d.com), which makes it
importable as ``kiln.fulfillment``; a plain ``pip install kiln3d`` has none.

Every ordering door -- the ``kiln order`` commands, ``kiln
fulfillment-materials``, the outsourced half of ``kiln compare-cost``, and
the ``fulfillment_*`` and ``compare_print_options`` agent tools -- asks
:func:`order_service` first.  When the answer is ``None`` the door says
:data:`NOT_INCLUDED`, with :data:`NOT_INCLUDED_CODE` where a code is shown,
and sends nothing anywhere; otherwise it works from the module returned.
"""

from __future__ import annotations

import sys
from types import ModuleType

#: Where ordering is set up for an install without the order service: the
#: page for Kiln's hosted connector, which prices, places and tracks orders.
CONNECTOR_DOCS_URL = "https://kiln3d.com/docs/connector"

#: The code shown beside :data:`NOT_INCLUDED`.
NOT_INCLUDED_CODE = "NOT_AVAILABLE"

#: The one sentence every ordering door says on an install without the
#: order service.  It says where ordering lives; it does not promise that an
#: order placed there will go through.
NOT_INCLUDED = (
    "This install of Kiln doesn't include ordering prints from a print service, "
    "so nothing was sent to one. For quotes and orders, see Kiln's hosted "
    f"connector: {CONNECTOR_DOCS_URL}"
)


#: Cancel refusals the order service words for the person in full: there is
#: nothing to cancel (an unpaid order is never made), or the print service
#: cancels only on a written request and the refusal says where to write.
#: Every door says them as they are, never wrapped in "Failed to cancel".
WORDED_CANCEL_REFUSALS = frozenset({"NOTHING_TO_CANCEL", "CANCEL_NOT_SUPPORTED"})


def order_service() -> ModuleType | None:
    """The order service when this install has one, else ``None``."""
    try:
        import kiln.fulfillment  # noqa: F401
    except ImportError:
        return None
    return sys.modules["kiln.fulfillment"]


def fee_basis(priced: object) -> float | None:
    """The amount Kiln's fee is charged on: the printing, and nothing else.

    Kiln's fee on an order is charged on the production amount (the printing,
    plus any print shop's minimum per order), never on shipping or tax, at
    every door that quotes or charges it.  Quotes and placed orders both state
    that amount as ``production_price``.  A placed order also states its
    checkout total, which carries shipping and tax; that total is never read
    here.  ``None`` when the print service did not state a production amount:
    the fee is then not worked out, rather than worked out on the wrong
    amount.
    """
    value = getattr(priced, "production_price", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def server_takes_fee(provider: object) -> bool:
    """Whether orders placed through ``provider`` have Kiln's fee charged by
    Kiln's hosted server, so nothing on this machine may record or charge it.

    The provider says so itself (``charges_kiln_fee``): Kiln's hosted
    connector charges the fee on the server when it places the order.  This
    is the provider's own contract, never a guess from which keys are set.
    A door that charges before placing (the agent order tool) asks this; the
    order it gets back then confirms it (:func:`fee_taken_by_server`).
    """
    return getattr(provider, "charges_kiln_fee", False) is True


def fee_taken_by_server(order: object) -> dict | None:
    """Kiln's fee as the hosted server already charged it, or ``None``.

    An order placed through Kiln's hosted connector comes back saying what
    the server charged (``kiln_fee_charged``).  Every door that records or
    charges Kiln's fee on this machine asks this first: when it answers, the
    door shows that fee, once, and records and charges nothing itself, so
    the fee is never taken twice.  ``None`` means no server took it, and the
    door settles the fee as it always has.
    """
    fee = getattr(order, "kiln_fee_charged", None)
    return dict(fee) if isinstance(fee, dict) else None
