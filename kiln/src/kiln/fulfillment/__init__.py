"""Fulfillment service adapters for outsourced 3D printing.

Routes print jobs to external manufacturing services (Craftcloud, Sculpteo,
etc.) when local printers lack the required material, capacity, or capability.

Re-exports the public API so consumers can write::

    from kiln.fulfillment import FulfillmentProvider, CraftcloudProvider, ...
    from kiln.fulfillment import get_provider, list_providers
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
from kiln.fulfillment.registry import (
    get_provider,
    get_provider_class,
    list_providers,
    register,
)
from kiln.fulfillment.sculpteo import SculpteoProvider

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
    "SculpteoProvider",
    "ShippingOption",
    "get_provider",
    "get_provider_class",
    "list_providers",
    "register",
]
