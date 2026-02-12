"""Tests for kiln.fulfillment.intelligence — provider health, multi-provider
quotes, retry logic, material filtering, batch quoting, order history, and
shipping insurance.

Coverage areas:
    - ProviderHealth enum: all 4 values and string serialization
    - ProviderStatus: to_dict enum conversion, all fields
    - HealthMonitor: record/query health, failure thresholds, is_healthy,
      get_all_statuses, response time, errors, thread safety
    - get_health_monitor: singleton pattern, reset between tests
    - ProviderQuote / QuoteComparison: to_dict with/without optional fields
    - compare_providers: multi-provider quoting, cheapest/fastest/recommended,
      health filtering, explicit providers param, recommendation edge cases
    - MaterialFilter + filter_materials: technology, color, finish, price,
      min_wall_mm, search text, combined filters, edge cases, to_dict
    - BatchQuoteItem / BatchQuoteResult / BatchQuote: to_dict, labels
    - batch_quote: multi-item quoting, totals, failure counting, empty
    - RetryResult: to_dict with/without optional fields
    - place_order_with_retry: retry logic, fallback providers, error collection,
      default provider from list_providers, health recording
    - OrderHistory: save/update/list/get, provider filter, limit, sorted desc,
      not-found, to_dict
    - get_order_history: singleton pattern, reset between tests
    - InsuranceTier enum: all 4 values and string serialization
    - InsuranceOption: to_dict enum conversion
    - get_insurance_options: tier pricing with minimums, coverage, max_coverage
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

import kiln.fulfillment.intelligence as intelligence_module
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


# ---------------------------------------------------------------------------
# Fixtures — reset module-level singletons between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons so tests don't leak state."""
    intelligence_module._health_monitor = None
    intelligence_module._order_history = None
    yield
    intelligence_module._health_monitor = None
    intelligence_module._order_history = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_quote(
    *,
    quote_id: str = "q-1",
    provider: str = "test",
    total_price: float = 10.0,
    lead_time_days: Optional[int] = 5,
) -> Quote:
    """Create a Quote with sensible defaults for testing."""
    return Quote(
        quote_id=quote_id,
        provider=provider,
        material="PLA",
        quantity=1,
        unit_price=total_price,
        total_price=total_price,
        lead_time_days=lead_time_days,
    )


def _make_order_result(
    *,
    order_id: str = "ord-1",
    provider: str = "test",
    success: bool = True,
) -> OrderResult:
    """Create an OrderResult with sensible defaults for testing."""
    return OrderResult(
        success=success,
        order_id=order_id,
        status=OrderStatus.SUBMITTED,
        provider=provider,
    )


def _mock_provider(
    *,
    name: str = "prov-a",
    display_name: str = "Provider A",
    quote: Optional[Quote] = None,
    order_result: Optional[OrderResult] = None,
    quote_error: Optional[Exception] = None,
    order_error: Optional[Exception] = None,
) -> MagicMock:
    """Create a mock FulfillmentProvider."""
    mock = MagicMock(spec=FulfillmentProvider)
    mock.name = name
    mock.display_name = display_name
    mock.supported_technologies = ["FDM"]

    if quote_error:
        mock.get_quote.side_effect = quote_error
    elif quote:
        mock.get_quote.return_value = quote
    else:
        mock.get_quote.return_value = _make_quote(provider=name)

    if order_error:
        mock.place_order.side_effect = order_error
    elif order_result:
        mock.place_order.return_value = order_result
    else:
        mock.place_order.return_value = _make_order_result(provider=name)

    return mock


def _make_material(
    *,
    id: str = "pla-white",
    name: str = "PLA White",
    technology: str = "FDM",
    color: str = "white",
    finish: str = "raw",
    price_per_cm3: Optional[float] = 0.05,
    min_wall_mm: Optional[float] = 0.8,
) -> Material:
    """Create a Material with sensible defaults."""
    return Material(
        id=id,
        name=name,
        technology=technology,
        color=color,
        finish=finish,
        price_per_cm3=price_per_cm3,
        min_wall_mm=min_wall_mm,
    )


# ---------------------------------------------------------------------------
# TestProviderHealthEnum
# ---------------------------------------------------------------------------


class TestProviderHealthEnum:
    """ProviderHealth enum: all 4 values, string serialization."""

    def test_healthy_value(self):
        assert ProviderHealth.HEALTHY.value == "healthy"

    def test_degraded_value(self):
        assert ProviderHealth.DEGRADED.value == "degraded"

    def test_down_value(self):
        assert ProviderHealth.DOWN.value == "down"

    def test_unknown_value(self):
        assert ProviderHealth.UNKNOWN.value == "unknown"

    def test_enum_has_exactly_four_members(self):
        assert len(ProviderHealth) == 4


# ---------------------------------------------------------------------------
# TestProviderStatus
# ---------------------------------------------------------------------------


class TestProviderStatus:
    """ProviderStatus.to_dict: enum to string conversion, all fields."""

    def test_to_dict_converts_health_enum(self):
        status = ProviderStatus(
            provider="test",
            health=ProviderHealth.DEGRADED,
            consecutive_failures=2,
        )
        d = status.to_dict()
        assert d["health"] == "degraded"
        assert d["provider"] == "test"
        assert d["consecutive_failures"] == 2

    def test_to_dict_includes_all_fields(self):
        status = ProviderStatus(
            provider="prov",
            health=ProviderHealth.HEALTHY,
            last_check=1000.0,
            response_time_ms=42.5,
            error=None,
            consecutive_failures=0,
        )
        d = status.to_dict()
        assert d["provider"] == "prov"
        assert d["health"] == "healthy"
        assert d["last_check"] == 1000.0
        assert d["response_time_ms"] == 42.5
        assert d["error"] is None
        assert d["consecutive_failures"] == 0

    def test_to_dict_health_is_string_not_enum(self):
        for health in ProviderHealth:
            status = ProviderStatus(provider="x", health=health)
            d = status.to_dict()
            assert isinstance(d["health"], str)
            assert d["health"] == health.value


# ---------------------------------------------------------------------------
# TestHealthMonitor
# ---------------------------------------------------------------------------


class TestHealthMonitor:
    """HealthMonitor: initial state, recording success/failure, threshold
    transitions, is_healthy queries, get_all_statuses, response time, errors,
    thread safety."""

    def test_initial_status_unknown(self):
        monitor = HealthMonitor()
        status = monitor.get_status("new-provider")
        assert status.health == ProviderHealth.UNKNOWN

    def test_record_success_sets_healthy(self):
        monitor = HealthMonitor()
        monitor.record_success("prov")
        assert monitor.get_status("prov").health == ProviderHealth.HEALTHY

    def test_record_failure_sets_degraded(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="timeout")
        assert monitor.get_status("prov").health == ProviderHealth.DEGRADED

    def test_two_failures_still_degraded(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="fail 1")
        monitor.record_failure("prov", error="fail 2")
        status = monitor.get_status("prov")
        assert status.health == ProviderHealth.DEGRADED
        assert status.consecutive_failures == 2

    def test_three_failures_sets_down(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="fail 1")
        monitor.record_failure("prov", error="fail 2")
        monitor.record_failure("prov", error="fail 3")
        assert monitor.get_status("prov").health == ProviderHealth.DOWN

    def test_four_failures_stays_down(self):
        monitor = HealthMonitor()
        for i in range(4):
            monitor.record_failure("prov", error=f"fail {i}")
        status = monitor.get_status("prov")
        assert status.health == ProviderHealth.DOWN
        assert status.consecutive_failures == 4

    def test_success_after_failure_resets(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="oops")
        monitor.record_failure("prov", error="oops")
        assert monitor.get_status("prov").health == ProviderHealth.DEGRADED
        monitor.record_success("prov")
        assert monitor.get_status("prov").health == ProviderHealth.HEALTHY
        assert monitor.get_status("prov").consecutive_failures == 0

    def test_success_after_down_resets_to_healthy(self):
        monitor = HealthMonitor()
        for _ in range(5):
            monitor.record_failure("prov", error="fail")
        assert monitor.get_status("prov").health == ProviderHealth.DOWN
        monitor.record_success("prov")
        assert monitor.get_status("prov").health == ProviderHealth.HEALTHY

    def test_is_healthy_true_for_unknown(self):
        monitor = HealthMonitor()
        assert monitor.is_healthy("never-seen") is True

    def test_is_healthy_true_for_healthy(self):
        monitor = HealthMonitor()
        monitor.record_success("prov")
        assert monitor.is_healthy("prov") is True

    def test_is_healthy_false_for_degraded(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="fail")
        assert monitor.is_healthy("prov") is False

    def test_is_healthy_false_for_down(self):
        monitor = HealthMonitor()
        for _ in range(3):
            monitor.record_failure("prov", error="fail")
        assert monitor.is_healthy("prov") is False

    def test_get_all_statuses_empty(self):
        monitor = HealthMonitor()
        assert monitor.get_all_statuses() == []

    def test_get_all_statuses_returns_all(self):
        monitor = HealthMonitor()
        monitor.record_success("a")
        monitor.record_failure("b", error="err")
        statuses = monitor.get_all_statuses()
        assert len(statuses) == 2
        names = {s.provider for s in statuses}
        assert names == {"a", "b"}

    def test_response_time_recorded(self):
        monitor = HealthMonitor()
        monitor.record_success("prov", response_time_ms=42.5)
        assert monitor.get_status("prov").response_time_ms == 42.5

    def test_error_message_recorded(self):
        monitor = HealthMonitor()
        monitor.record_failure("prov", error="connection reset")
        assert monitor.get_status("prov").error == "connection reset"

    def test_last_check_set_on_success(self):
        monitor = HealthMonitor()
        before = time.time()
        monitor.record_success("prov")
        after = time.time()
        last_check = monitor.get_status("prov").last_check
        assert last_check is not None
        assert before <= last_check <= after

    def test_last_check_set_on_failure(self):
        monitor = HealthMonitor()
        before = time.time()
        monitor.record_failure("prov", error="err")
        after = time.time()
        last_check = monitor.get_status("prov").last_check
        assert last_check is not None
        assert before <= last_check <= after

    def test_to_dict_includes_health_value(self):
        monitor = HealthMonitor()
        monitor.record_success("prov")
        d = monitor.get_status("prov").to_dict()
        assert d["health"] == "healthy"
        assert isinstance(d["health"], str)

    def test_thread_safety_concurrent_writes(self):
        monitor = HealthMonitor()
        errors = []

        def record_ops(provider_name: str) -> None:
            try:
                for _ in range(50):
                    monitor.record_success(provider_name, response_time_ms=1.0)
                    monitor.record_failure(provider_name, error="test")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_ops, args=(f"prov-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All 4 providers should have been recorded
        assert len(monitor.get_all_statuses()) == 4


# ---------------------------------------------------------------------------
# TestGetHealthMonitor
# ---------------------------------------------------------------------------


class TestGetHealthMonitor:
    """get_health_monitor: returns singleton, same instance on repeated calls."""

    def test_returns_health_monitor_instance(self):
        monitor = get_health_monitor()
        assert isinstance(monitor, HealthMonitor)

    def test_returns_same_instance(self):
        m1 = get_health_monitor()
        m2 = get_health_monitor()
        assert m1 is m2

    def test_reset_creates_new_instance(self):
        m1 = get_health_monitor()
        intelligence_module._health_monitor = None
        m2 = get_health_monitor()
        assert m1 is not m2


# ---------------------------------------------------------------------------
# TestProviderQuote
# ---------------------------------------------------------------------------


class TestProviderQuote:
    """ProviderQuote.to_dict: with quote, with error, without optional fields."""

    def test_to_dict_with_quote(self):
        quote = _make_quote(total_price=15.0)
        pq = ProviderQuote(
            provider_name="prov-a",
            provider_display_name="Provider A",
            quote=quote,
            response_time_ms=120.5,
            health="healthy",
        )
        d = pq.to_dict()
        assert d["provider_name"] == "prov-a"
        assert d["provider_display_name"] == "Provider A"
        assert d["response_time_ms"] == 120.5
        assert d["health"] == "healthy"
        assert "quote" in d
        assert d["quote"]["total_price"] == 15.0

    def test_to_dict_with_error(self):
        pq = ProviderQuote(
            provider_name="prov-b",
            provider_display_name="Provider B",
            error="API unavailable",
            health="down",
        )
        d = pq.to_dict()
        assert d["error"] == "API unavailable"
        assert "quote" not in d

    def test_to_dict_without_quote_or_error(self):
        pq = ProviderQuote(
            provider_name="prov-c",
            provider_display_name="Provider C",
        )
        d = pq.to_dict()
        assert "quote" not in d
        assert "error" not in d


# ---------------------------------------------------------------------------
# TestQuoteComparison
# ---------------------------------------------------------------------------


class TestQuoteComparison:
    """QuoteComparison.to_dict: quotes serialized, optional fields."""

    def test_to_dict_serializes_quotes(self):
        quote = _make_quote(total_price=20.0)
        pq = ProviderQuote(
            provider_name="prov",
            provider_display_name="Prov",
            quote=quote,
            health="healthy",
        )
        comparison = QuoteComparison(
            quotes=[pq],
            cheapest="prov",
            fastest="prov",
            recommended="prov",
            summary="Test summary.",
        )
        d = comparison.to_dict()
        assert len(d["quotes"]) == 1
        assert d["cheapest"] == "prov"
        assert d["fastest"] == "prov"
        assert d["recommended"] == "prov"
        assert d["summary"] == "Test summary."

    def test_to_dict_with_no_successful_quotes(self):
        comparison = QuoteComparison(
            quotes=[],
            cheapest=None,
            fastest=None,
            recommended=None,
        )
        d = comparison.to_dict()
        assert d["quotes"] == []
        assert d["cheapest"] is None


# ---------------------------------------------------------------------------
# TestCompareProviders
# ---------------------------------------------------------------------------


class TestCompareProviders:
    """compare_providers: multi-provider quoting, health filtering, ranking,
    recommendation logic, explicit providers param, edge cases."""

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_compare_two_providers_both_succeed(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a", "prov-b"]

        quote_a = _make_quote(quote_id="qa", provider="prov-a", total_price=10.0, lead_time_days=5)
        quote_b = _make_quote(quote_id="qb", provider="prov-b", total_price=15.0, lead_time_days=3)

        prov_a = _mock_provider(name="prov-a", display_name="Provider A", quote=quote_a)
        prov_b = _mock_provider(name="prov-b", display_name="Provider B", quote=quote_b)

        mock_get.side_effect = lambda n: prov_a if n == "prov-a" else prov_b

        result = compare_providers("test.stl", "pla")
        assert len(result.quotes) == 2
        assert result.cheapest == "prov-a"
        assert result.fastest == "prov-b"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_compare_skips_unhealthy_provider(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        for _ in range(3):
            monitor.record_failure("prov-a", error="down")
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a", "prov-b"]

        quote_b = _make_quote(quote_id="qb", provider="prov-b", total_price=15.0)
        prov_b = _mock_provider(name="prov-b", display_name="Provider B", quote=quote_b)
        mock_get.side_effect = lambda n: prov_b

        result = compare_providers("test.stl", "pla")
        skipped = [q for q in result.quotes if q.provider_name == "prov-a"]
        assert len(skipped) == 1
        assert "unhealthy" in skipped[0].error
        assert result.cheapest == "prov-b"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_compare_handles_provider_failure(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a", "prov-b"]

        prov_a = _mock_provider(
            name="prov-a",
            quote_error=FulfillmentError("API error"),
        )
        quote_b = _make_quote(quote_id="qb", provider="prov-b", total_price=20.0)
        prov_b = _mock_provider(name="prov-b", display_name="Provider B", quote=quote_b)

        mock_get.side_effect = lambda n: prov_a if n == "prov-a" else prov_b

        result = compare_providers("test.stl", "pla")
        failed = [q for q in result.quotes if q.provider_name == "prov-a"]
        assert failed[0].error == "API error"
        assert result.cheapest == "prov-b"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_cheapest_identified_correctly(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "mid", "expensive"]

        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=5.0, lead_time_days=10),
            ),
            "mid": _mock_provider(
                name="mid", display_name="Mid",
                quote=_make_quote(total_price=10.0, lead_time_days=7),
            ),
            "expensive": _mock_provider(
                name="expensive", display_name="Expensive",
                quote=_make_quote(total_price=20.0, lead_time_days=3),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.cheapest == "cheap"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_fastest_identified_correctly(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["slow", "fast"]

        providers = {
            "slow": _mock_provider(
                name="slow", display_name="Slow",
                quote=_make_quote(total_price=5.0, lead_time_days=14),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=8.0, lead_time_days=2),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.fastest == "fast"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_recommended_defaults_to_cheapest(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "fast"]

        # Same lead time, different price — recommended should be cheapest.
        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=5.0, lead_time_days=5),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=6.0, lead_time_days=5),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.recommended == "cheap"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_recommended_switches_to_fastest_when_much_quicker(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "fast"]

        # Cheap is $10 with 15 days; fast is $11 with 3 days.
        # time_diff=12 > 3, price_diff_pct=10% < 20% -> recommended switches.
        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=10.0, lead_time_days=15),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=11.0, lead_time_days=3),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.recommended == "fast"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_recommended_stays_cheapest_when_price_premium_too_high(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "fast"]

        # Cheap is $10 with 15 days; fast is $13 with 3 days.
        # time_diff=12 > 3, but price_diff_pct=30% >= 20% -> stays cheapest.
        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=10.0, lead_time_days=15),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=13.0, lead_time_days=3),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.recommended == "cheap"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_recommended_stays_cheapest_when_time_diff_small(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "fast"]

        # Cheap is $10 with 6 days; fast is $10.50 with 4 days.
        # time_diff=2 <= 3, so recommended stays cheapest regardless of price.
        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=10.0, lead_time_days=6),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=10.50, lead_time_days=4),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        assert result.recommended == "cheap"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_recommended_stays_cheapest_when_lead_time_none(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["cheap", "fast"]

        # lead_time_days=None prevents the recommendation switch.
        providers = {
            "cheap": _mock_provider(
                name="cheap", display_name="Cheap",
                quote=_make_quote(total_price=10.0, lead_time_days=None),
            ),
            "fast": _mock_provider(
                name="fast", display_name="Fast",
                quote=_make_quote(total_price=11.0, lead_time_days=2),
            ),
        }
        mock_get.side_effect = lambda n: providers[n]

        result = compare_providers("test.stl", "pla")
        # cheapest has None lead_time, so the condition branch can't compute
        # time_diff; recommended stays cheapest.
        assert result.recommended == "cheap"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_empty_provider_list(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = []

        result = compare_providers("test.stl", "pla")
        assert result.quotes == []
        assert result.cheapest is None
        assert result.fastest is None
        assert result.recommended is None

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_all_providers_fail(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a", "prov-b"]

        prov_a = _mock_provider(name="prov-a", quote_error=FulfillmentError("fail-a"))
        prov_b = _mock_provider(name="prov-b", quote_error=FulfillmentError("fail-b"))
        mock_get.side_effect = lambda n: prov_a if n == "prov-a" else prov_b

        result = compare_providers("test.stl", "pla")
        assert result.cheapest is None
        assert result.fastest is None
        assert result.recommended is None
        assert len(result.quotes) == 2

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_summary_includes_count(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a"]

        quote_a = _make_quote(total_price=10.0, provider="prov-a")
        prov_a = _mock_provider(name="prov-a", display_name="A", quote=quote_a)
        mock_get.return_value = prov_a

        result = compare_providers("test.stl", "pla")
        assert "1 provider" in result.summary
        assert "1 returned" in result.summary

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_explicit_providers_param_overrides_list(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        # list_providers returns 3 but we pass only 1 explicitly
        mock_list.return_value = ["prov-a", "prov-b", "prov-c"]

        quote = _make_quote(total_price=5.0, provider="prov-b")
        prov_b = _mock_provider(name="prov-b", display_name="B", quote=quote)
        mock_get.return_value = prov_b

        result = compare_providers("test.stl", "pla", providers=["prov-b"])
        assert len(result.quotes) == 1
        assert result.quotes[0].provider_name == "prov-b"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_runtime_error_caught(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a"]

        prov_a = _mock_provider(name="prov-a", quote_error=RuntimeError("boom"))
        mock_get.return_value = prov_a

        result = compare_providers("test.stl", "pla")
        assert result.quotes[0].error == "boom"
        assert result.cheapest is None

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_file_not_found_error_caught(
        self, mock_monitor, mock_list, mock_get,
    ):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a"]

        prov_a = _mock_provider(name="prov-a", quote_error=FileNotFoundError("no file"))
        mock_get.return_value = prov_a

        result = compare_providers("test.stl", "pla")
        assert result.quotes[0].error == "no file"
        assert result.cheapest is None


# ---------------------------------------------------------------------------
# TestMaterialFilter
# ---------------------------------------------------------------------------


class TestMaterialFilter:
    """filter_materials + MaterialFilter: technology, color, finish, price,
    min_wall_mm, search text, combined filters, edge cases, to_dict."""

    def _sample_materials(self) -> List[Material]:
        return [
            _make_material(id="pla-white", name="PLA White", technology="FDM", color="white", finish="raw", price_per_cm3=0.05, min_wall_mm=0.8),
            _make_material(id="abs-black", name="ABS Black", technology="FDM", color="black", finish="polished", price_per_cm3=0.07, min_wall_mm=1.0),
            _make_material(id="resin-gray", name="Resin Gray", technology="SLA", color="gray", finish="raw", price_per_cm3=0.12, min_wall_mm=0.3),
            _make_material(id="nylon-white", name="Nylon PA12 White", technology="SLS", color="white", finish="dyed", price_per_cm3=0.20, min_wall_mm=0.6),
        ]

    def test_filter_by_technology(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(technology="FDM"))
        assert len(result) == 2
        assert all(m.technology == "FDM" for m in result)

    def test_filter_by_technology_case_insensitive(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(technology="fdm"))
        assert len(result) == 2

    def test_filter_by_color_partial_match(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(color="whi"))
        assert len(result) == 2
        assert {m.id for m in result} == {"pla-white", "nylon-white"}

    def test_filter_by_finish(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(finish="polished"))
        assert len(result) == 1
        assert result[0].id == "abs-black"

    def test_filter_by_max_price(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(max_price_per_cm3=0.10))
        assert len(result) == 2
        assert all(m.price_per_cm3 <= 0.10 for m in result)

    def test_filter_by_min_wall_mm(self):
        # min_wall_mm=0.5 should include materials with min_wall <= 0.5
        result = filter_materials(self._sample_materials(), MaterialFilter(min_wall_mm=0.5))
        assert len(result) == 1
        assert result[0].id == "resin-gray"

    def test_filter_by_min_wall_mm_inclusive(self):
        # min_wall_mm=0.8 should include materials with min_wall <= 0.8
        result = filter_materials(self._sample_materials(), MaterialFilter(min_wall_mm=0.8))
        ids = {m.id for m in result}
        assert "pla-white" in ids  # 0.8 <= 0.8
        assert "resin-gray" in ids  # 0.3 <= 0.8
        assert "nylon-white" in ids  # 0.6 <= 0.8

    def test_filter_excludes_none_price(self):
        materials = [_make_material(price_per_cm3=None)]
        result = filter_materials(materials, MaterialFilter(max_price_per_cm3=1.0))
        assert result == []

    def test_filter_excludes_none_min_wall(self):
        materials = [_make_material(min_wall_mm=None)]
        result = filter_materials(materials, MaterialFilter(min_wall_mm=1.0))
        assert result == []

    def test_filter_by_search_text(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(search_text="nylon"))
        assert len(result) == 1
        assert result[0].id == "nylon-white"

    def test_filter_by_search_text_matches_technology(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(search_text="SLA"))
        assert len(result) == 1
        assert result[0].id == "resin-gray"

    def test_filter_by_search_text_matches_color(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(search_text="black"))
        assert len(result) == 1
        assert result[0].id == "abs-black"

    def test_multiple_filters_combined(self):
        criteria = MaterialFilter(technology="FDM", color="white")
        result = filter_materials(self._sample_materials(), criteria)
        assert len(result) == 1
        assert result[0].id == "pla-white"

    def test_no_filters_returns_all(self):
        materials = self._sample_materials()
        result = filter_materials(materials, MaterialFilter())
        assert len(result) == len(materials)

    def test_filter_returns_empty_when_no_match(self):
        result = filter_materials(self._sample_materials(), MaterialFilter(technology="DMLS"))
        assert result == []

    def test_filter_empty_materials_list(self):
        result = filter_materials([], MaterialFilter(technology="FDM"))
        assert result == []

    def test_to_dict_excludes_none_values(self):
        f = MaterialFilter(technology="FDM", color="red")
        d = f.to_dict()
        assert "technology" in d
        assert "color" in d
        assert "finish" not in d
        assert "max_price_per_cm3" not in d
        assert "min_wall_mm" not in d
        assert "search_text" not in d

    def test_to_dict_all_fields_set(self):
        f = MaterialFilter(
            technology="FDM", color="red", finish="polished",
            max_price_per_cm3=0.5, min_wall_mm=1.0, search_text="test",
        )
        d = f.to_dict()
        assert len(d) == 6

    def test_to_dict_empty_filter(self):
        f = MaterialFilter()
        d = f.to_dict()
        assert d == {}


# ---------------------------------------------------------------------------
# TestBatchQuoteResult
# ---------------------------------------------------------------------------


class TestBatchQuoteResult:
    """BatchQuoteResult.to_dict: with quote, with error, without optional."""

    def test_to_dict_with_quote(self):
        quote = _make_quote(total_price=8.0)
        result = BatchQuoteResult(label="Bracket", file_path="/a.stl", quote=quote)
        d = result.to_dict()
        assert d["label"] == "Bracket"
        assert d["file_path"] == "/a.stl"
        assert "quote" in d
        assert d["quote"]["total_price"] == 8.0
        assert "error" not in d

    def test_to_dict_with_error(self):
        result = BatchQuoteResult(label="Bad part", file_path="/b.stl", error="unsupported")
        d = result.to_dict()
        assert d["error"] == "unsupported"
        assert "quote" not in d

    def test_to_dict_without_quote_or_error(self):
        result = BatchQuoteResult(label="x", file_path="/c.stl")
        d = result.to_dict()
        assert "quote" not in d
        assert "error" not in d


# ---------------------------------------------------------------------------
# TestBatchQuoteDataclass
# ---------------------------------------------------------------------------


class TestBatchQuoteDataclass:
    """BatchQuote.to_dict: items serialized, aggregated fields."""

    def test_to_dict_serializes_items(self):
        quote = _make_quote(total_price=10.0)
        item = BatchQuoteResult(label="Part", file_path="/a.stl", quote=quote)
        bq = BatchQuote(
            items=[item],
            total_price=10.0,
            currency="USD",
            successful_count=1,
            failed_count=0,
        )
        d = bq.to_dict()
        assert len(d["items"]) == 1
        assert d["total_price"] == 10.0
        assert d["currency"] == "USD"
        assert d["successful_count"] == 1
        assert d["failed_count"] == 0


# ---------------------------------------------------------------------------
# TestBatchQuote
# ---------------------------------------------------------------------------


class TestBatchQuote:
    """batch_quote: single/multi items, totals, failures, mixed, empty, labels."""

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_single_item_batch(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        quote = _make_quote(total_price=12.50)
        prov = _mock_provider(name="prov", quote=quote)
        mock_get.return_value = prov

        items = [BatchQuoteItem(file_path="/part.stl", material_id="pla")]
        result = batch_quote(items, provider_name="prov")
        assert result.successful_count == 1
        assert result.failed_count == 0
        assert result.total_price == 12.50
        assert len(result.items) == 1

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_multiple_items_batch(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        quote = _make_quote(total_price=10.0)
        prov = _mock_provider(name="prov", quote=quote)
        mock_get.return_value = prov

        items = [
            BatchQuoteItem(file_path="/a.stl", material_id="pla"),
            BatchQuoteItem(file_path="/b.stl", material_id="pla"),
            BatchQuoteItem(file_path="/c.stl", material_id="pla"),
        ]
        result = batch_quote(items, provider_name="prov")
        assert result.successful_count == 3
        assert len(result.items) == 3

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_total_price_sums_correctly(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        quotes = iter([
            _make_quote(total_price=10.0),
            _make_quote(total_price=5.50),
        ])
        prov = MagicMock(spec=FulfillmentProvider)
        prov.name = "prov"
        prov.get_quote.side_effect = lambda req: next(quotes)
        mock_get.return_value = prov

        items = [
            BatchQuoteItem(file_path="/a.stl", material_id="pla"),
            BatchQuoteItem(file_path="/b.stl", material_id="pla"),
        ]
        result = batch_quote(items, provider_name="prov")
        assert result.total_price == 15.50

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_failed_item_counted(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(
            name="prov",
            quote_error=FulfillmentError("unsupported"),
        )
        mock_get.return_value = prov

        items = [BatchQuoteItem(file_path="/bad.stl", material_id="pla")]
        result = batch_quote(items, provider_name="prov")
        assert result.failed_count == 1
        assert result.successful_count == 0
        assert result.total_price == 0.0
        assert result.items[0].error == "unsupported"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_file_not_found_error_counted_as_failure(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(
            name="prov",
            quote_error=FileNotFoundError("missing.stl"),
        )
        mock_get.return_value = prov

        items = [BatchQuoteItem(file_path="/missing.stl", material_id="pla")]
        result = batch_quote(items, provider_name="prov")
        assert result.failed_count == 1
        assert result.successful_count == 0

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_mixed_success_and_failure(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()

        call_count = 0
        def side_effect(req):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise FulfillmentError("file error")
            return _make_quote(total_price=7.0)

        prov = MagicMock(spec=FulfillmentProvider)
        prov.name = "prov"
        prov.get_quote.side_effect = side_effect
        mock_get.return_value = prov

        items = [
            BatchQuoteItem(file_path="/a.stl", material_id="pla"),
            BatchQuoteItem(file_path="/b.stl", material_id="pla"),
            BatchQuoteItem(file_path="/c.stl", material_id="pla"),
        ]
        result = batch_quote(items, provider_name="prov")
        assert result.successful_count == 2
        assert result.failed_count == 1
        assert result.total_price == 14.0

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_empty_items_list(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(name="prov")
        mock_get.return_value = prov

        result = batch_quote([], provider_name="prov")
        assert result.successful_count == 0
        assert result.failed_count == 0
        assert result.total_price == 0.0
        assert result.items == []

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_labels_used(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(name="prov", quote=_make_quote(total_price=5.0))
        mock_get.return_value = prov

        items = [
            BatchQuoteItem(file_path="/bracket.stl", material_id="pla", label="Left bracket"),
        ]
        result = batch_quote(items, provider_name="prov")
        assert result.items[0].label == "Left bracket"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_label_defaults_to_filename(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(name="prov", quote=_make_quote(total_price=5.0))
        mock_get.return_value = prov

        items = [BatchQuoteItem(file_path="/path/to/widget.stl", material_id="pla")]
        result = batch_quote(items, provider_name="prov")
        assert result.items[0].label == "widget.stl"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_currency_is_usd(self, mock_monitor, mock_get):
        mock_monitor.return_value = HealthMonitor()
        prov = _mock_provider(name="prov", quote=_make_quote(total_price=5.0))
        mock_get.return_value = prov

        result = batch_quote([BatchQuoteItem(file_path="/a.stl", material_id="pla")], provider_name="prov")
        assert result.currency == "USD"


# ---------------------------------------------------------------------------
# TestRetryResult
# ---------------------------------------------------------------------------


class TestRetryResult:
    """RetryResult.to_dict: with/without order_result and errors."""

    def test_to_dict_success_with_order(self):
        order = _make_order_result(order_id="ord-99")
        result = RetryResult(
            success=True,
            provider_used="prov-a",
            order_result=order,
            attempts=1,
            fallback_used=False,
            errors=[],
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["provider_used"] == "prov-a"
        assert d["attempts"] == 1
        assert d["fallback_used"] is False
        assert "order" in d
        assert d["order"]["order_id"] == "ord-99"
        assert "errors" not in d  # empty list excluded

    def test_to_dict_failure_with_errors(self):
        result = RetryResult(
            success=False,
            provider_used="prov-a",
            attempts=3,
            fallback_used=True,
            errors=["err-1", "err-2"],
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["errors"] == ["err-1", "err-2"]
        assert "order" not in d

    def test_to_dict_no_order_no_errors(self):
        result = RetryResult(
            success=False,
            provider_used="prov-a",
            attempts=1,
        )
        d = result.to_dict()
        assert "order" not in d
        assert "errors" not in d


# ---------------------------------------------------------------------------
# TestPlaceOrderWithRetry
# ---------------------------------------------------------------------------


class TestPlaceOrderWithRetry:
    """place_order_with_retry: first-attempt success, retry, fallback,
    all-fail, max_retries, error collection, fallback_used flag, default
    provider from list_providers, health recording."""

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_success_on_first_attempt(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a"]
        order = _make_order_result(order_id="ord-1", provider="prov-a")
        prov = _mock_provider(name="prov-a", order_result=order)
        mock_get.return_value = prov

        result = place_order_with_retry("q-1", primary_provider="prov-a")
        assert result.success is True
        assert result.attempts == 1
        assert result.fallback_used is False
        assert result.errors == []

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_retry_succeeds_on_second_attempt(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a"]

        prov = MagicMock(spec=FulfillmentProvider)
        prov.name = "prov-a"
        order = _make_order_result(order_id="ord-1", provider="prov-a")
        prov.place_order.side_effect = [
            FulfillmentError("transient"),
            order,
        ]
        mock_get.return_value = prov

        result = place_order_with_retry("q-1", primary_provider="prov-a", max_retries=2)
        assert result.success is True
        assert result.attempts == 2
        assert len(result.errors) == 1

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_fallback_to_second_provider(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a", "prov-b"]

        prov_a = _mock_provider(
            name="prov-a",
            order_error=FulfillmentError("down"),
        )
        order_b = _make_order_result(order_id="ord-b", provider="prov-b")
        prov_b = _mock_provider(name="prov-b", order_result=order_b)

        mock_get.side_effect = lambda n: prov_a if n == "prov-a" else prov_b

        result = place_order_with_retry(
            "q-1",
            primary_provider="prov-a",
            fallback_providers=["prov-b"],
            max_retries=0,
        )
        assert result.success is True
        assert result.provider_used == "prov-b"
        assert result.fallback_used is True

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_all_providers_fail(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a", "prov-b"]

        prov_a = _mock_provider(name="prov-a", order_error=FulfillmentError("fail-a"))
        prov_b = _mock_provider(name="prov-b", order_error=FulfillmentError("fail-b"))
        mock_get.side_effect = lambda n: prov_a if n == "prov-a" else prov_b

        result = place_order_with_retry(
            "q-1",
            primary_provider="prov-a",
            fallback_providers=["prov-b"],
            max_retries=0,
        )
        assert result.success is False
        assert len(result.errors) == 2

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_max_retries_respected(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a"]

        prov = _mock_provider(name="prov-a", order_error=FulfillmentError("fail"))
        mock_get.return_value = prov

        result = place_order_with_retry(
            "q-1",
            primary_provider="prov-a",
            max_retries=3,
        )
        # max_retries=3 means 4 total attempts (initial + 3 retries)
        assert result.attempts == 4
        assert result.success is False

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_errors_collected(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a"]

        prov = MagicMock(spec=FulfillmentProvider)
        prov.name = "prov-a"
        prov.place_order.side_effect = [
            FulfillmentError("err-1"),
            FulfillmentError("err-2"),
            _make_order_result(provider="prov-a"),
        ]
        mock_get.return_value = prov

        result = place_order_with_retry("q-1", primary_provider="prov-a", max_retries=2)
        assert result.success is True
        assert len(result.errors) == 2
        assert "err-1" in result.errors[0]
        assert "err-2" in result.errors[1]

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_fallback_used_flag(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["prov-a"]

        order = _make_order_result(provider="prov-a")
        prov = _mock_provider(name="prov-a", order_result=order)
        mock_get.return_value = prov

        result = place_order_with_retry("q-1", primary_provider="prov-a")
        assert result.fallback_used is False

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_no_primary_provider_uses_first_from_list(self, mock_monitor, mock_list, mock_get):
        mock_monitor.return_value = HealthMonitor()
        mock_list.return_value = ["default-prov"]

        order = _make_order_result(order_id="ord-default", provider="default-prov")
        prov = _mock_provider(name="default-prov", order_result=order)
        mock_get.return_value = prov

        result = place_order_with_retry("q-1")
        assert result.success is True
        assert result.provider_used == "default-prov"

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_health_recorded_on_success(self, mock_monitor, mock_list, mock_get):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a"]

        order = _make_order_result(provider="prov-a")
        prov = _mock_provider(name="prov-a", order_result=order)
        mock_get.return_value = prov

        place_order_with_retry("q-1", primary_provider="prov-a")
        assert monitor.get_status("prov-a").health == ProviderHealth.HEALTHY

    @patch("kiln.fulfillment.intelligence.get_provider")
    @patch("kiln.fulfillment.intelligence.list_providers")
    @patch("kiln.fulfillment.intelligence.get_health_monitor")
    def test_health_recorded_on_failure(self, mock_monitor, mock_list, mock_get):
        monitor = HealthMonitor()
        mock_monitor.return_value = monitor
        mock_list.return_value = ["prov-a"]

        prov = _mock_provider(name="prov-a", order_error=FulfillmentError("fail"))
        mock_get.return_value = prov

        place_order_with_retry("q-1", primary_provider="prov-a", max_retries=0)
        status = monitor.get_status("prov-a")
        assert status.health in (ProviderHealth.DEGRADED, ProviderHealth.DOWN)
        assert status.consecutive_failures >= 1


# ---------------------------------------------------------------------------
# TestOrderHistory
# ---------------------------------------------------------------------------


class TestOrderHistory:
    """OrderHistory: save, update, list, filter, limit, get, not-found, to_dict."""

    def test_save_and_retrieve_order(self):
        history = OrderHistory()
        record = history.save_order(
            order_id="ord-100",
            provider="craftcloud",
            status="submitted",
            file_path="/model.stl",
            material_id="pla",
            quantity=2,
            total_price=25.0,
        )
        assert record.order_id == "ord-100"
        assert record.provider == "craftcloud"

        retrieved = history.get_order("ord-100")
        assert retrieved is not None
        assert retrieved.order_id == "ord-100"

    def test_update_existing_order(self):
        history = OrderHistory()
        history.save_order(
            order_id="ord-200",
            provider="craftcloud",
            status="submitted",
            file_path="/model.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
        )
        updated = history.save_order(
            order_id="ord-200",
            provider="craftcloud",
            status="shipping",
            file_path="/model.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
            tracking_url="https://track.example.com/123",
        )
        assert updated.status == "shipping"
        assert updated.tracking_url == "https://track.example.com/123"

        # Should still be one record, not two
        orders = history.list_orders()
        assert len(orders) == 1

    def test_update_preserves_existing_tracking_url(self):
        history = OrderHistory()
        history.save_order(
            order_id="ord-300",
            provider="prov",
            status="submitted",
            file_path="/a.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
            tracking_url="https://track.example.com/orig",
        )
        updated = history.save_order(
            order_id="ord-300",
            provider="prov",
            status="printing",
            file_path="/a.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
            # No tracking_url provided; existing should be preserved.
        )
        assert updated.tracking_url == "https://track.example.com/orig"

    def test_update_sets_updated_at(self):
        history = OrderHistory()
        record = history.save_order(
            order_id="ord-ts",
            provider="prov",
            status="submitted",
            file_path="/a.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
        )
        original_updated_at = record.updated_at
        time.sleep(0.01)
        updated = history.save_order(
            order_id="ord-ts",
            provider="prov",
            status="printing",
            file_path="/a.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
        )
        assert updated.updated_at > original_updated_at

    def test_list_orders_sorted_by_date(self):
        history = OrderHistory()
        history.save_order(
            order_id="old",
            provider="a",
            status="delivered",
            file_path="/old.stl",
            material_id="pla",
            quantity=1,
            total_price=5.0,
        )
        # Small sleep to ensure created_at differs
        time.sleep(0.01)
        history.save_order(
            order_id="new",
            provider="b",
            status="submitted",
            file_path="/new.stl",
            material_id="abs",
            quantity=1,
            total_price=8.0,
        )
        orders = history.list_orders()
        assert len(orders) == 2
        # Most recent first
        assert orders[0].order_id == "new"
        assert orders[1].order_id == "old"

    def test_list_orders_with_provider_filter(self):
        history = OrderHistory()
        history.save_order(
            order_id="o1", provider="craftcloud", status="submitted",
            file_path="/a.stl", material_id="pla", quantity=1, total_price=10.0,
        )
        history.save_order(
            order_id="o2", provider="sculpteo", status="submitted",
            file_path="/b.stl", material_id="pla", quantity=1, total_price=12.0,
        )
        history.save_order(
            order_id="o3", provider="craftcloud", status="delivered",
            file_path="/c.stl", material_id="abs", quantity=1, total_price=15.0,
        )
        filtered = history.list_orders(provider="craftcloud")
        assert len(filtered) == 2
        assert all(o.provider == "craftcloud" for o in filtered)

    def test_list_orders_with_limit(self):
        history = OrderHistory()
        for i in range(5):
            history.save_order(
                order_id=f"ord-{i}",
                provider="test",
                status="submitted",
                file_path=f"/part{i}.stl",
                material_id="pla",
                quantity=1,
                total_price=10.0,
            )
        limited = history.list_orders(limit=3)
        assert len(limited) == 3

    def test_list_orders_empty_history(self):
        history = OrderHistory()
        assert history.list_orders() == []

    def test_get_order_not_found_returns_none(self):
        history = OrderHistory()
        assert history.get_order("nonexistent") is None

    def test_to_dict(self):
        history = OrderHistory()
        record = history.save_order(
            order_id="ord-dict",
            provider="craftcloud",
            status="submitted",
            file_path="/model.stl",
            material_id="pla",
            quantity=1,
            total_price=20.0,
            currency="EUR",
            notes="test order",
        )
        d = record.to_dict()
        assert d["order_id"] == "ord-dict"
        assert d["provider"] == "craftcloud"
        assert d["currency"] == "EUR"
        assert d["notes"] == "test order"
        assert isinstance(d["created_at"], float)

    def test_record_id_generated(self):
        history = OrderHistory()
        record = history.save_order(
            order_id="ord-id-test",
            provider="prov",
            status="submitted",
            file_path="/a.stl",
            material_id="pla",
            quantity=1,
            total_price=10.0,
        )
        assert record.id.startswith("rec-")
        assert len(record.id) > 4


# ---------------------------------------------------------------------------
# TestGetOrderHistory
# ---------------------------------------------------------------------------


class TestGetOrderHistory:
    """get_order_history: returns singleton, same instance on repeated calls."""

    def test_returns_order_history_instance(self):
        history = get_order_history()
        assert isinstance(history, OrderHistory)

    def test_returns_same_instance(self):
        h1 = get_order_history()
        h2 = get_order_history()
        assert h1 is h2

    def test_reset_creates_new_instance(self):
        h1 = get_order_history()
        intelligence_module._order_history = None
        h2 = get_order_history()
        assert h1 is not h2


# ---------------------------------------------------------------------------
# TestInsuranceTierEnum
# ---------------------------------------------------------------------------


class TestInsuranceTierEnum:
    """InsuranceTier enum: all 4 values, string serialization."""

    def test_none_value(self):
        assert InsuranceTier.NONE.value == "none"

    def test_basic_value(self):
        assert InsuranceTier.BASIC.value == "basic"

    def test_standard_value(self):
        assert InsuranceTier.STANDARD.value == "standard"

    def test_premium_value(self):
        assert InsuranceTier.PREMIUM.value == "premium"

    def test_enum_has_exactly_four_members(self):
        assert len(InsuranceTier) == 4


# ---------------------------------------------------------------------------
# TestInsuranceOptions
# ---------------------------------------------------------------------------


class TestInsuranceOptions:
    """get_insurance_options: tier count, pricing, minimums, coverage, to_dict."""

    def test_returns_four_tiers(self):
        options = get_insurance_options(100.0)
        assert len(options) == 4

    def test_tiers_in_order(self):
        options = get_insurance_options(100.0)
        assert options[0].tier == InsuranceTier.NONE
        assert options[1].tier == InsuranceTier.BASIC
        assert options[2].tier == InsuranceTier.STANDARD
        assert options[3].tier == InsuranceTier.PREMIUM

    def test_none_tier_is_free(self):
        options = get_insurance_options(100.0)
        none_opt = [o for o in options if o.tier == InsuranceTier.NONE][0]
        assert none_opt.price == 0.0
        assert none_opt.coverage_percent == 0

    def test_basic_tier_percentage_pricing(self):
        # For a $200 order, 3% = $6.00 (above $1.50 minimum)
        options = get_insurance_options(200.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        assert basic.price == 6.0

    def test_basic_tier_minimum_price(self):
        # For a $10 order, 3% = $0.30 but min is $1.50
        options = get_insurance_options(10.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        assert basic.price == 1.50

    def test_standard_tier_percentage_pricing(self):
        # For a $200 order, 5% = $10.00 (above $2.50 minimum)
        options = get_insurance_options(200.0)
        standard = [o for o in options if o.tier == InsuranceTier.STANDARD][0]
        assert standard.price == 10.0

    def test_standard_tier_minimum_price(self):
        # For a $10 order, 5% = $0.50 but min is $2.50
        options = get_insurance_options(10.0)
        standard = [o for o in options if o.tier == InsuranceTier.STANDARD][0]
        assert standard.price == 2.50

    def test_premium_tier_percentage_pricing(self):
        # For a $200 order, 10% = $20.00 (above $5.00 minimum)
        options = get_insurance_options(200.0)
        premium = [o for o in options if o.tier == InsuranceTier.PREMIUM][0]
        assert premium.price == 20.0

    def test_premium_tier_minimum_price(self):
        # For a $10 order, 10% = $1.00 but min is $5.00
        options = get_insurance_options(10.0)
        premium = [o for o in options if o.tier == InsuranceTier.PREMIUM][0]
        assert premium.price == 5.0

    def test_standard_tier_higher_than_basic(self):
        options = get_insurance_options(100.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        standard = [o for o in options if o.tier == InsuranceTier.STANDARD][0]
        assert standard.price > basic.price

    def test_premium_tier_highest(self):
        options = get_insurance_options(100.0)
        prices = {o.tier: o.price for o in options}
        assert prices[InsuranceTier.PREMIUM] > prices[InsuranceTier.STANDARD]
        assert prices[InsuranceTier.STANDARD] > prices[InsuranceTier.BASIC]
        assert prices[InsuranceTier.BASIC] > prices[InsuranceTier.NONE]

    def test_premium_includes_reprint_guarantee(self):
        options = get_insurance_options(50.0)
        premium = [o for o in options if o.tier == InsuranceTier.PREMIUM][0]
        assert "reprint" in premium.description.lower()

    def test_coverage_percent(self):
        options = get_insurance_options(100.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        assert basic.coverage_percent == 100

    def test_max_coverage_basic(self):
        options = get_insurance_options(100.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        assert basic.max_coverage == 100.0

    def test_max_coverage_premium_150_percent(self):
        options = get_insurance_options(100.0)
        premium = [o for o in options if o.tier == InsuranceTier.PREMIUM][0]
        assert premium.max_coverage == 150.0

    def test_currency_passed_through(self):
        options = get_insurance_options(100.0, currency="EUR")
        assert all(o.currency == "EUR" for o in options)

    def test_to_dict_converts_tier_enum(self):
        options = get_insurance_options(100.0)
        for opt in options:
            d = opt.to_dict()
            assert isinstance(d["tier"], str)
            assert d["tier"] == opt.tier.value

    def test_to_dict_includes_all_fields(self):
        options = get_insurance_options(100.0)
        basic = [o for o in options if o.tier == InsuranceTier.BASIC][0]
        d = basic.to_dict()
        assert "tier" in d
        assert "name" in d
        assert "description" in d
        assert "price" in d
        assert "currency" in d
        assert "coverage_percent" in d
        assert "max_coverage" in d
