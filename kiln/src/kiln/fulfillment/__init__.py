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
from kiln.fulfillment.intelligence import (
    BatchQuote,
    BatchQuoteItem,
    BatchQuoteResult,
    HealthMonitor,
    InsuranceOption,
    InsuranceTier,
    MaterialFilter,
    OrderHistory,
    OrderRecord,
    ProviderHealth,
    ProviderQuote,
    ProviderStatus,
    QuoteComparison,
    RetryResult,
    batch_quote,
    compare_providers,
    filter_materials,
    get_health_monitor,
    get_insurance_options,
    get_order_history,
    place_order_with_retry,
)

__all__ = [
    "BatchQuote",
    "BatchQuoteItem",
    "BatchQuoteResult",
    "CraftcloudProvider",
    "FulfillmentError",
    "FulfillmentProvider",
    "HealthMonitor",
    "InsuranceOption",
    "InsuranceTier",
    "Material",
    "MaterialFilter",
    "OrderHistory",
    "OrderRecord",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "ProviderHealth",
    "ProviderQuote",
    "ProviderStatus",
    "Quote",
    "QuoteComparison",
    "QuoteRequest",
    "RetryResult",
    "SculpteoProvider",
    "ShippingOption",
    "batch_quote",
    "compare_providers",
    "filter_materials",
    "get_health_monitor",
    "get_insurance_options",
    "get_order_history",
    "get_provider",
    "get_provider_class",
    "list_providers",
    "place_order_with_retry",
    "register",
]
