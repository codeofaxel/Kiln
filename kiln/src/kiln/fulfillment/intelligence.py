"""Fulfillment intelligence — provider health, multi-provider quotes, retry
logic, material filtering, batch quoting, order history, and shipping insurance.

Sits between the MCP tool layer and individual fulfillment providers to add
cross-cutting intelligence that individual providers don't handle alone.
"""

from __future__ import annotations

import enum
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
from kiln.fulfillment.registry import get_provider, list_providers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider health monitoring
# ---------------------------------------------------------------------------


class ProviderHealth(enum.Enum):
    """Health state of a fulfillment provider."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class ProviderStatus:
    """Health status for a single fulfillment provider."""

    provider: str
    health: ProviderHealth
    last_check: Optional[float] = None  # Unix timestamp
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    consecutive_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["health"] = self.health.value
        return data


class HealthMonitor:
    """Monitors fulfillment provider health via periodic probes.

    Tracks consecutive failures, response times, and overall availability.
    Used by FulfillmentRouter to skip unhealthy providers.
    """

    def __init__(self) -> None:
        self._statuses: Dict[str, ProviderStatus] = {}
        self._lock = threading.Lock()

    def record_success(
        self, provider_name: str, *, response_time_ms: float = 0.0,
    ) -> None:
        """Record a successful API call to a provider."""
        with self._lock:
            self._statuses[provider_name] = ProviderStatus(
                provider=provider_name,
                health=ProviderHealth.HEALTHY,
                last_check=time.time(),
                response_time_ms=response_time_ms,
                consecutive_failures=0,
            )

    def record_failure(
        self, provider_name: str, *, error: str = "",
    ) -> None:
        """Record a failed API call to a provider."""
        with self._lock:
            existing = self._statuses.get(provider_name)
            failures = (existing.consecutive_failures if existing else 0) + 1
            health = ProviderHealth.DEGRADED if failures < 3 else ProviderHealth.DOWN
            self._statuses[provider_name] = ProviderStatus(
                provider=provider_name,
                health=health,
                last_check=time.time(),
                error=error,
                consecutive_failures=failures,
            )

    def get_status(self, provider_name: str) -> ProviderStatus:
        """Return current health status for a provider."""
        with self._lock:
            return self._statuses.get(
                provider_name,
                ProviderStatus(provider=provider_name, health=ProviderHealth.UNKNOWN),
            )

    def get_all_statuses(self) -> List[ProviderStatus]:
        """Return health statuses for all known providers."""
        with self._lock:
            return list(self._statuses.values())

    def is_healthy(self, provider_name: str) -> bool:
        """Return True if the provider is healthy or unknown (untested)."""
        status = self.get_status(provider_name)
        return status.health in (ProviderHealth.HEALTHY, ProviderHealth.UNKNOWN)


# Module-level singleton.
_health_monitor: Optional[HealthMonitor] = None
_health_lock = threading.Lock()


def get_health_monitor() -> HealthMonitor:
    """Return the module-level HealthMonitor singleton."""
    global _health_monitor  # noqa: PLW0603
    if _health_monitor is None:
        with _health_lock:
            if _health_monitor is None:
                _health_monitor = HealthMonitor()
    return _health_monitor


# ---------------------------------------------------------------------------
# Multi-provider quote comparison
# ---------------------------------------------------------------------------


@dataclass
class ProviderQuote:
    """A quote from one provider, annotated with health and provider info."""

    provider_name: str
    provider_display_name: str
    quote: Optional[Quote] = None
    error: Optional[str] = None
    response_time_ms: float = 0.0
    health: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "provider_name": self.provider_name,
            "provider_display_name": self.provider_display_name,
            "response_time_ms": self.response_time_ms,
            "health": self.health,
        }
        if self.quote:
            data["quote"] = self.quote.to_dict()
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class QuoteComparison:
    """Side-by-side quotes from multiple fulfillment providers."""

    quotes: List[ProviderQuote]
    cheapest: Optional[str] = None        # Provider name
    fastest: Optional[str] = None         # Provider name
    recommended: Optional[str] = None     # Provider name (best balance)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["quotes"] = [q.to_dict() for q in self.quotes]
        return data


def compare_providers(
    file_path: str,
    material_id: str,
    *,
    quantity: int = 1,
    shipping_country: str = "US",
    providers: Optional[List[str]] = None,
) -> QuoteComparison:
    """Get quotes from multiple providers and compare them.

    Args:
        file_path: Path to the model file (STL/3MF/OBJ).
        material_id: Material ID (may differ per provider — uses best match).
        quantity: Number of copies.
        shipping_country: ISO country code for shipping.
        providers: Specific providers to query. If None, queries all registered.

    Returns:
        QuoteComparison with ranked results and recommendations.
    """
    monitor = get_health_monitor()
    provider_names = providers or list_providers()
    results: List[ProviderQuote] = []

    request = QuoteRequest(
        file_path=file_path,
        material_id=material_id,
        quantity=quantity,
        shipping_country=shipping_country,
    )

    for name in provider_names:
        if not monitor.is_healthy(name):
            results.append(ProviderQuote(
                provider_name=name,
                provider_display_name=name,
                error=f"Provider {name} is currently unhealthy, skipped.",
                health=monitor.get_status(name).health.value,
            ))
            continue

        start = time.monotonic()
        try:
            provider = get_provider(name)
            quote = provider.get_quote(request)
            elapsed = (time.monotonic() - start) * 1000
            monitor.record_success(name, response_time_ms=elapsed)
            results.append(ProviderQuote(
                provider_name=name,
                provider_display_name=provider.display_name,
                quote=quote,
                response_time_ms=round(elapsed, 1),
                health="healthy",
            ))
        except (FulfillmentError, RuntimeError, FileNotFoundError) as exc:
            elapsed = (time.monotonic() - start) * 1000
            monitor.record_failure(name, error=str(exc))
            results.append(ProviderQuote(
                provider_name=name,
                provider_display_name=name,
                error=str(exc),
                response_time_ms=round(elapsed, 1),
                health=monitor.get_status(name).health.value,
            ))

    # Find cheapest and fastest among successful quotes
    successful = [r for r in results if r.quote is not None]
    cheapest = None
    fastest = None
    recommended = None

    if successful:
        by_price = sorted(successful, key=lambda r: r.quote.total_price)
        cheapest = by_price[0].provider_name

        by_time = sorted(
            successful,
            key=lambda r: r.quote.lead_time_days or 999,
        )
        fastest = by_time[0].provider_name

        # Recommendation: prefer cheapest unless fastest is significantly quicker
        # and only modestly more expensive (< 20% premium for > 3 days faster).
        recommended = cheapest
        if (
            cheapest != fastest
            and by_price[0].quote.lead_time_days
            and by_time[0].quote.lead_time_days
        ):
            time_diff = by_price[0].quote.lead_time_days - by_time[0].quote.lead_time_days
            price_diff_pct = (
                (by_time[0].quote.total_price - by_price[0].quote.total_price)
                / by_price[0].quote.total_price * 100
            ) if by_price[0].quote.total_price > 0 else 0
            if time_diff > 3 and price_diff_pct < 20:
                recommended = fastest

    summary_parts = [f"Queried {len(provider_names)} provider(s), {len(successful)} returned quotes."]
    if cheapest:
        c = next(r for r in successful if r.provider_name == cheapest)
        summary_parts.append(f"Cheapest: {cheapest} at ${c.quote.total_price:.2f}.")
    if fastest:
        f = next(r for r in successful if r.provider_name == fastest)
        summary_parts.append(
            f"Fastest: {fastest} ({f.quote.lead_time_days or '?'} days)."
        )

    return QuoteComparison(
        quotes=results,
        cheapest=cheapest,
        fastest=fastest,
        recommended=recommended,
        summary=" ".join(summary_parts),
    )


# ---------------------------------------------------------------------------
# Material filtering
# ---------------------------------------------------------------------------


@dataclass
class MaterialFilter:
    """Criteria for filtering available materials."""

    technology: Optional[str] = None
    color: Optional[str] = None
    finish: Optional[str] = None
    max_price_per_cm3: Optional[float] = None
    min_wall_mm: Optional[float] = None
    search_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def filter_materials(
    materials: List[Material],
    criteria: MaterialFilter,
) -> List[Material]:
    """Filter a material list by technology, color, finish, price, or text search.

    Args:
        materials: Full material list from a provider.
        criteria: Filter criteria (all optional, combined with AND).

    Returns:
        Filtered list of materials matching all specified criteria.
    """
    result = list(materials)

    if criteria.technology:
        tech = criteria.technology.upper()
        result = [m for m in result if m.technology.upper() == tech]

    if criteria.color:
        color = criteria.color.lower()
        result = [m for m in result if color in m.color.lower()]

    if criteria.finish:
        finish = criteria.finish.lower()
        result = [m for m in result if finish in m.finish.lower()]

    if criteria.max_price_per_cm3 is not None:
        result = [
            m for m in result
            if m.price_per_cm3 is not None and m.price_per_cm3 <= criteria.max_price_per_cm3
        ]

    if criteria.min_wall_mm is not None:
        result = [
            m for m in result
            if m.min_wall_mm is not None and m.min_wall_mm <= criteria.min_wall_mm
        ]

    if criteria.search_text:
        text = criteria.search_text.lower()
        result = [
            m for m in result
            if text in m.name.lower() or text in m.technology.lower() or text in m.color.lower()
        ]

    return result


# ---------------------------------------------------------------------------
# Batch quoting (multi-part assemblies)
# ---------------------------------------------------------------------------


@dataclass
class BatchQuoteItem:
    """A single item in a batch quote request."""

    file_path: str
    material_id: str
    quantity: int = 1
    label: str = ""   # Optional user label like "Left bracket"


@dataclass
class BatchQuoteResult:
    """Result for one item in a batch quote."""

    label: str
    file_path: str
    quote: Optional[Quote] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "label": self.label,
            "file_path": self.file_path,
        }
        if self.quote:
            data["quote"] = self.quote.to_dict()
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class BatchQuote:
    """Aggregated batch quote for multiple parts."""

    items: List[BatchQuoteResult]
    total_price: float
    currency: str = "USD"
    successful_count: int = 0
    failed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["items"] = [i.to_dict() for i in self.items]
        return data


def batch_quote(
    items: List[BatchQuoteItem],
    *,
    provider_name: Optional[str] = None,
    shipping_country: str = "US",
) -> BatchQuote:
    """Get quotes for multiple parts in a single operation.

    Args:
        items: List of parts to quote.
        provider_name: Specific provider, or None for default.
        shipping_country: ISO country code for shipping.

    Returns:
        BatchQuote with per-item results and aggregated total.
    """
    provider = get_provider(provider_name)
    monitor = get_health_monitor()
    results: List[BatchQuoteResult] = []
    total = 0.0
    success = 0
    fail = 0

    for item in items:
        label = item.label or item.file_path.rsplit("/", 1)[-1]
        try:
            quote = provider.get_quote(QuoteRequest(
                file_path=item.file_path,
                material_id=item.material_id,
                quantity=item.quantity,
                shipping_country=shipping_country,
            ))
            monitor.record_success(provider.name)
            results.append(BatchQuoteResult(
                label=label, file_path=item.file_path, quote=quote,
            ))
            total += quote.total_price
            success += 1
        except (FulfillmentError, FileNotFoundError) as exc:
            monitor.record_failure(provider.name, error=str(exc))
            results.append(BatchQuoteResult(
                label=label, file_path=item.file_path, error=str(exc),
            ))
            fail += 1

    return BatchQuote(
        items=results,
        total_price=round(total, 2),
        currency="USD",
        successful_count=success,
        failed_count=fail,
    )


# ---------------------------------------------------------------------------
# Order retry with provider fallback
# ---------------------------------------------------------------------------


@dataclass
class RetryResult:
    """Result of an order attempt with retry/fallback."""

    success: bool
    provider_used: str
    order_result: Optional[OrderResult] = None
    attempts: int = 0
    fallback_used: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "success": self.success,
            "provider_used": self.provider_used,
            "attempts": self.attempts,
            "fallback_used": self.fallback_used,
        }
        if self.order_result:
            data["order"] = self.order_result.to_dict()
        if self.errors:
            data["errors"] = self.errors
        return data


def place_order_with_retry(
    quote_id: str,
    *,
    shipping_option_id: str = "",
    shipping_address: Optional[Dict[str, str]] = None,
    primary_provider: Optional[str] = None,
    fallback_providers: Optional[List[str]] = None,
    max_retries: int = 2,
) -> RetryResult:
    """Place an order with automatic retry and provider fallback.

    Tries the primary provider first. On failure, retries up to max_retries
    times, then falls back to alternative providers.

    Args:
        quote_id: Quote ID from a previous quote request.
        shipping_option_id: Selected shipping option.
        shipping_address: Validated shipping address dict.
        primary_provider: Provider to try first. None = default.
        fallback_providers: Alternative providers to try on failure.
        max_retries: Max retries per provider before fallback.

    Returns:
        RetryResult with the outcome and any errors encountered.
    """
    monitor = get_health_monitor()
    errors: List[str] = []
    attempts = 0

    request = OrderRequest(
        quote_id=quote_id,
        shipping_option_id=shipping_option_id,
        shipping_address=shipping_address or {},
    )

    # Try primary provider
    providers_to_try = []
    if primary_provider:
        providers_to_try.append(primary_provider)
    else:
        providers_to_try.append(list_providers()[0] if list_providers() else "")

    if fallback_providers:
        providers_to_try.extend(fallback_providers)

    for provider_name in providers_to_try:
        if not provider_name:
            continue
        is_fallback = provider_name != providers_to_try[0]

        for retry in range(max_retries + 1):
            attempts += 1
            try:
                provider = get_provider(provider_name)
                result = provider.place_order(request)
                monitor.record_success(provider_name)
                return RetryResult(
                    success=True,
                    provider_used=provider_name,
                    order_result=result,
                    attempts=attempts,
                    fallback_used=is_fallback,
                    errors=errors,
                )
            except FulfillmentError as exc:
                error_msg = f"{provider_name} attempt {retry + 1}: {exc}"
                errors.append(error_msg)
                monitor.record_failure(provider_name, error=str(exc))
                logger.warning(error_msg)

    return RetryResult(
        success=False,
        provider_used=providers_to_try[0] if providers_to_try else "",
        attempts=attempts,
        fallback_used=len(providers_to_try) > 1,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Order history (in-memory with persistence bridge)
# ---------------------------------------------------------------------------


@dataclass
class OrderRecord:
    """A persisted fulfillment order for history/reorder."""

    id: str
    order_id: str
    provider: str
    status: str
    file_path: str
    material_id: str
    quantity: int
    total_price: float
    currency: str = "USD"
    shipping_address: Dict[str, str] = field(default_factory=dict)
    tracking_url: Optional[str] = None
    tracking_number: Optional[str] = None
    quote_id: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OrderHistory:
    """In-memory order history with SQLite persistence bridge.

    Call ``save_order`` after placing fulfillment orders, and
    ``list_orders`` / ``get_order`` to query history.
    """

    def __init__(self, *, db: Any = None) -> None:
        self._orders: Dict[str, OrderRecord] = {}
        self._db = db
        self._lock = threading.Lock()

    def save_order(
        self,
        order_id: str,
        provider: str,
        status: str,
        file_path: str,
        material_id: str,
        quantity: int,
        total_price: float,
        *,
        currency: str = "USD",
        shipping_address: Optional[Dict[str, str]] = None,
        tracking_url: Optional[str] = None,
        tracking_number: Optional[str] = None,
        quote_id: Optional[str] = None,
        notes: str = "",
    ) -> OrderRecord:
        """Save or update an order in history."""
        now = time.time()
        record_id = f"rec-{secrets.token_hex(8)}"

        with self._lock:
            # Check if order already exists (update case)
            existing = None
            for rec in self._orders.values():
                if rec.order_id == order_id:
                    existing = rec
                    break

            if existing:
                existing.status = status
                existing.tracking_url = tracking_url or existing.tracking_url
                existing.tracking_number = tracking_number or existing.tracking_number
                existing.updated_at = now
                existing.notes = notes or existing.notes
                record = existing
            else:
                record = OrderRecord(
                    id=record_id,
                    order_id=order_id,
                    provider=provider,
                    status=status,
                    file_path=file_path,
                    material_id=material_id,
                    quantity=quantity,
                    total_price=total_price,
                    currency=currency,
                    shipping_address=shipping_address or {},
                    tracking_url=tracking_url,
                    tracking_number=tracking_number,
                    quote_id=quote_id,
                    created_at=now,
                    updated_at=now,
                    notes=notes,
                )
                self._orders[record.id] = record

        # Persist to SQLite if available
        if self._db is not None:
            self._persist_order(record)

        return record

    def list_orders(
        self, *, limit: int = 20, provider: Optional[str] = None,
    ) -> List[OrderRecord]:
        """Return recent orders, optionally filtered by provider."""
        with self._lock:
            orders = list(self._orders.values())

        if provider:
            orders = [o for o in orders if o.provider == provider]

        orders.sort(key=lambda o: o.created_at, reverse=True)
        return orders[:limit]

    def get_order(self, order_id: str) -> Optional[OrderRecord]:
        """Look up an order by order_id."""
        with self._lock:
            for rec in self._orders.values():
                if rec.order_id == order_id:
                    return rec
        return None

    def _persist_order(self, record: OrderRecord) -> None:
        """Persist an order record to SQLite."""
        try:
            import json
            self._db.execute(
                """INSERT OR REPLACE INTO fulfillment_orders
                   (id, order_id, provider, status, file_path, material_id,
                    quantity, total_price, currency, shipping_address,
                    tracking_url, tracking_number, quote_id, notes,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id, record.order_id, record.provider, record.status,
                    record.file_path, record.material_id, record.quantity,
                    record.total_price, record.currency,
                    json.dumps(record.shipping_address),
                    record.tracking_url, record.tracking_number,
                    record.quote_id, record.notes,
                    record.created_at, record.updated_at,
                ),
            )
            self._db.commit()
        except Exception:
            logger.error(
                "Failed to persist fulfillment order %s to database — "
                "order was placed with provider but may not appear in history. "
                "Check database connectivity and disk space.",
                record.order_id,
                exc_info=True,
            )


# Module-level singleton.
_order_history: Optional[OrderHistory] = None
_order_history_lock = threading.Lock()


def get_order_history(*, db: Any = None) -> OrderHistory:
    """Return the module-level OrderHistory singleton."""
    global _order_history  # noqa: PLW0603
    if _order_history is None:
        with _order_history_lock:
            if _order_history is None:
                _order_history = OrderHistory(db=db)
    return _order_history


# ---------------------------------------------------------------------------
# Shipping insurance / protection
# ---------------------------------------------------------------------------


class InsuranceTier(enum.Enum):
    """Shipping insurance tiers."""

    NONE = "none"
    BASIC = "basic"       # Covers loss only
    STANDARD = "standard" # Covers loss + damage
    PREMIUM = "premium"   # Covers loss + damage + reprint guarantee


@dataclass
class InsuranceOption:
    """A shipping insurance/protection option."""

    tier: InsuranceTier
    name: str
    description: str
    price: float
    currency: str = "USD"
    coverage_percent: int = 100
    max_coverage: float = 0.0  # 0 = unlimited up to order value

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tier"] = self.tier.value
        return data


def get_insurance_options(order_value: float, *, currency: str = "USD") -> List[InsuranceOption]:
    """Return available shipping insurance options for an order.

    Args:
        order_value: Total order value (used to calculate premiums).
        currency: Currency code.

    Returns:
        List of insurance options from none to premium.
    """
    return [
        InsuranceOption(
            tier=InsuranceTier.NONE,
            name="No Protection",
            description="No shipping insurance. Standard carrier liability applies.",
            price=0.0,
            currency=currency,
            coverage_percent=0,
        ),
        InsuranceOption(
            tier=InsuranceTier.BASIC,
            name="Loss Protection",
            description="Covers complete loss in transit. Full refund if package never arrives.",
            price=round(max(order_value * 0.03, 1.50), 2),
            currency=currency,
            coverage_percent=100,
            max_coverage=order_value,
        ),
        InsuranceOption(
            tier=InsuranceTier.STANDARD,
            name="Loss + Damage Protection",
            description="Covers loss and physical damage during shipping. Full refund or replacement.",
            price=round(max(order_value * 0.05, 2.50), 2),
            currency=currency,
            coverage_percent=100,
            max_coverage=order_value,
        ),
        InsuranceOption(
            tier=InsuranceTier.PREMIUM,
            name="Full Protection + Reprint Guarantee",
            description=(
                "Covers loss, damage, and print quality issues. "
                "Free reprint and reship if the part doesn't meet expectations."
            ),
            price=round(max(order_value * 0.10, 5.00), 2),
            currency=currency,
            coverage_percent=100,
            max_coverage=order_value * 1.5,
        ),
    ]
