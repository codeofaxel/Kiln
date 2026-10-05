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


def order_service() -> ModuleType | None:
    """The order service when this install has one, else ``None``."""
    try:
        import kiln.fulfillment  # noqa: F401
    except ImportError:
        return None
    return sys.modules["kiln.fulfillment"]
