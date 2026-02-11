"""Fulfillment service adapters for outsourced 3D printing.

Routes print jobs to external manufacturing services (Craftcloud, Shapeways,
etc.) when local printers lack the required material, capacity, or capability.

Re-exports the public API so consumers can write::

    from kiln.fulfillment import FulfillmentProvider, CraftcloudProvider, ...
"""

from __future__ import annotations

from kiln.fulfillment.base import (
    FulfillmentError,
    FulfillmentProvider,
    Material,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    QuoteRequest,
    ShippingOption,
)
from kiln.fulfillment.craftcloud import CraftcloudProvider

__all__ = [
    "CraftcloudProvider",
    "FulfillmentError",
    "FulfillmentProvider",
    "Material",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "Quote",
    "QuoteRequest",
    "ShippingOption",
]
