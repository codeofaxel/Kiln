"""Abstract fulfillment provider interface and data types.

Every external manufacturing service (Craftcloud, Sculpteo, etc.)
implements :class:`FulfillmentProvider` so the rest of Kiln can outsource
print jobs without knowing the underlying service.

Workflow::

    1. get_quote(QuoteRequest)  → Quote (price, lead time, shipping)
    2. place_order(OrderRequest) → OrderResult (order ID, tracking)
    3. get_order_status(order_id) → OrderResult (updated status)
    4. list_materials() → List[Material] (available materials + finishes)
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


class OrderStatus(enum.Enum):
    """Lifecycle states for a fulfillment order."""

    QUOTING = "quoting"
    QUOTED = "quoted"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    PRINTING = "printing"
    SHIPPING = "shipping"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Material:
    """A material available from a fulfillment service."""

    id: str
    name: str
    technology: str = ""  # FDM, SLA, SLS, MJF, etc.
    color: str = ""
    finish: str = ""  # raw, polished, dyed, etc.
    min_wall_mm: float | None = None
    price_per_cm3: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShippingOption:
    """A shipping option from a fulfillment service."""

    id: str
    name: str
    price: float
    currency: str = "USD"
    estimated_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteRequest:
    """Parameters for requesting a manufacturing quote."""

    file_path: str
    material_id: str
    quantity: int = 1
    shipping_country: str = "US"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Quote:
    """A price quote from a fulfillment service."""

    quote_id: str
    provider: str
    material: str
    quantity: int
    unit_price: float
    total_price: float
    currency: str = "USD"
    lead_time_days: int | None = None
    shipping_options: list[ShippingOption] = field(default_factory=list)
    expires_at: float | None = None  # Unix timestamp
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        data["shipping_options"] = [s.to_dict() for s in self.shipping_options]
        return data


@dataclass
class OrderRequest:
    """Parameters for placing a manufacturing order."""

    quote_id: str
    shipping_option_id: str = ""
    shipping_address: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderResult:
    """Outcome of a fulfillment order operation."""

    success: bool
    order_id: str
    status: OrderStatus
    provider: str
    tracking_url: str | None = None
    tracking_number: str | None = None
    estimated_delivery: str | None = None
    total_price: float | None = None
    currency: str = "USD"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class FulfillmentError(Exception):
    """Base exception for fulfillment-related errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class FulfillmentProvider(ABC):
    """Abstract base for external manufacturing service providers.

    Concrete implementations handle the specifics of each service's API
    (REST, GraphQL, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Machine-readable provider identifier (e.g. ``"craftcloud"``)."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name (e.g. ``"Craftcloud by All3DP"``)."""

    @property
    @abstractmethod
    def supported_technologies(self) -> list[str]:
        """Manufacturing technologies supported (e.g. ``["FDM", "SLA", "SLS"]``)."""

    @abstractmethod
    def list_materials(self) -> list[Material]:
        """Return all available materials from this provider.

        Returns:
            List of materials with pricing and specifications.

        Raises:
            FulfillmentError: If the materials cannot be retrieved.
        """

    @abstractmethod
    def get_quote(self, request: QuoteRequest) -> Quote:
        """Request a price quote for manufacturing a part.

        Args:
            request: Quote parameters including file, material, and quantity.

        Returns:
            A quote with pricing, lead time, and shipping options.

        Raises:
            FulfillmentError: If the quote cannot be generated.
            FileNotFoundError: If the model file does not exist.
        """

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a manufacturing order based on a previous quote.

        Args:
            request: Order parameters referencing a quote ID.

        Returns:
            Order result with ID, status, and tracking info.

        Raises:
            FulfillmentError: If the order cannot be placed.
        """

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult:
        """Check the current status of an existing order.

        Args:
            order_id: The order ID returned by :meth:`place_order`.

        Returns:
            Current order state.

        Raises:
            FulfillmentError: If the order cannot be found or queried.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order (if still cancellable).

        Args:
            order_id: The order ID to cancel.

        Returns:
            Updated order state.

        Raises:
            FulfillmentError: If the order cannot be cancelled.
        """
