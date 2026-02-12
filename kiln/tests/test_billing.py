"""Tests for kiln.billing -- revenue / fee system.

Covers:
- FeePolicy defaults and custom values
- FeeCalculation.to_dict
- Fee calculation: normal case, min fee floor, max fee cap
- Free tier: first N jobs waived, (N+1)th charged
- Zero / negative job cost = zero fee
- Monthly revenue calculation
- Ledger recording and retrieval
- Thread safety of ledger
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from kiln.billing import BillingLedger, FeeCalculation, FeePolicy


# ---------------------------------------------------------------------------
# FeePolicy dataclass
# ---------------------------------------------------------------------------

class TestFeePolicy:
    """Tests for the FeePolicy dataclass."""

    def test_defaults(self):
        policy = FeePolicy()
        assert policy.network_fee_percent == 5.0
        assert policy.min_fee_usd == 0.25
        assert policy.max_fee_usd == 200.0
        assert policy.free_tier_jobs == 5
        assert policy.currency == "USD"

    def test_custom_values(self):
        policy = FeePolicy(
            network_fee_percent=10.0,
            min_fee_usd=0.50,
            max_fee_usd=100.0,
            free_tier_jobs=10,
            currency="EUR",
        )
        assert policy.network_fee_percent == 10.0
        assert policy.min_fee_usd == 0.50
        assert policy.max_fee_usd == 100.0
        assert policy.free_tier_jobs == 10
        assert policy.currency == "EUR"

    def test_zero_free_tier(self):
        policy = FeePolicy(free_tier_jobs=0)
        assert policy.free_tier_jobs == 0


# ---------------------------------------------------------------------------
# FeeCalculation dataclass
# ---------------------------------------------------------------------------

class TestFeeCalculation:
    """Tests for the FeeCalculation dataclass."""

    def test_to_dict_includes_all_keys(self):
        fc = FeeCalculation(
            job_cost=100.0,
            fee_amount=5.0,
            fee_percent=5.0,
            total_cost=105.0,
            currency="USD",
        )
        d = fc.to_dict()
        expected_keys = {
            "job_cost", "fee_amount", "fee_percent", "total_cost",
            "currency", "waived", "waiver_reason",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self):
        fc = FeeCalculation(
            job_cost=200.0,
            fee_amount=10.0,
            fee_percent=5.0,
            total_cost=210.0,
            currency="USD",
            waived=False,
            waiver_reason=None,
        )
        d = fc.to_dict()
        assert d["job_cost"] == 200.0
        assert d["fee_amount"] == 10.0
        assert d["fee_percent"] == 5.0
        assert d["total_cost"] == 210.0
        assert d["currency"] == "USD"
        assert d["waived"] is False
        assert d["waiver_reason"] is None

    def test_to_dict_waived(self):
        fc = FeeCalculation(
            job_cost=50.0,
            fee_amount=0.0,
            fee_percent=0.0,
            total_cost=50.0,
            currency="USD",
            waived=True,
            waiver_reason="Free tier: job 1 of 5 free this month",
        )
        d = fc.to_dict()
        assert d["waived"] is True
        assert "Free tier" in d["waiver_reason"]

    def test_defaults(self):
        fc = FeeCalculation(
            job_cost=10.0,
            fee_amount=0.5,
            fee_percent=5.0,
            total_cost=10.5,
            currency="USD",
        )
        assert fc.waived is False
        assert fc.waiver_reason is None


# ---------------------------------------------------------------------------
# BillingLedger -- fee calculation
# ---------------------------------------------------------------------------

class TestFeeCalculationLogic:
    """Tests for BillingLedger.calculate_fee."""

    def test_normal_fee(self):
        """Standard 5 % fee on a $100 job (no free-tier slots)."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        fc = ledger.calculate_fee(job_cost=100.0)

        assert fc.job_cost == 100.0
        assert fc.fee_amount == 5.0
        assert fc.fee_percent == 5.0
        assert fc.total_cost == 105.0
        assert fc.currency == "USD"
        assert fc.waived is False

    def test_custom_percentage(self):
        """10 % fee policy."""
        policy = FeePolicy(network_fee_percent=10.0, free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        fc = ledger.calculate_fee(job_cost=200.0)

        assert fc.fee_amount == 20.0
        assert fc.total_cost == 220.0

    def test_min_fee_floor(self):
        """Fee should not drop below min_fee_usd."""
        policy = FeePolicy(min_fee_usd=0.25, free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        # 5 % of $1.00 = $0.05, but min is $0.25
        fc = ledger.calculate_fee(job_cost=1.0)

        assert fc.fee_amount == 0.25
        assert fc.total_cost == 1.25

    def test_max_fee_cap(self):
        """Fee should not exceed max_fee_usd."""
        policy = FeePolicy(max_fee_usd=50.0, free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        # 5 % of $2000 = $100, but max is $50
        fc = ledger.calculate_fee(job_cost=2000.0)

        assert fc.fee_amount == 50.0
        assert fc.total_cost == 2050.0

    def test_fee_between_min_and_max(self):
        """Fee within bounds is not clamped."""
        policy = FeePolicy(
            min_fee_usd=0.25,
            max_fee_usd=50.0,
            free_tier_jobs=0,
        )
        ledger = BillingLedger(fee_policy=policy)

        # 5 % of $50 = $2.50, between $0.25 and $50
        fc = ledger.calculate_fee(job_cost=50.0)

        assert fc.fee_amount == 2.5
        assert fc.total_cost == 52.5

    def test_effective_percent_with_min_floor(self):
        """Effective percentage is recalculated after clamping."""
        policy = FeePolicy(min_fee_usd=1.0, free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        # 5 % of $2 = $0.10, clamped to $1.00
        fc = ledger.calculate_fee(job_cost=2.0)

        assert fc.fee_amount == 1.0
        # Effective: (1.0 / 2.0) * 100 = 50.0
        assert fc.fee_percent == 50.0

    def test_effective_percent_with_max_cap(self):
        """Effective percentage is recalculated after capping."""
        policy = FeePolicy(max_fee_usd=10.0, free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        # 5 % of $500 = $25, capped to $10
        fc = ledger.calculate_fee(job_cost=500.0)

        assert fc.fee_amount == 10.0
        # Effective: (10 / 500) * 100 = 2.0
        assert fc.fee_percent == 2.0

    def test_custom_currency(self):
        """Currency is passed through to the result."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        fc = ledger.calculate_fee(job_cost=100.0, currency="EUR")

        assert fc.currency == "EUR"


# ---------------------------------------------------------------------------
# Zero / negative job cost
# ---------------------------------------------------------------------------

class TestZeroNegativeJobCost:
    """Tests for zero and negative job costs."""

    def test_zero_job_cost(self):
        ledger = BillingLedger()
        fc = ledger.calculate_fee(job_cost=0.0)

        assert fc.fee_amount == 0.0
        assert fc.fee_percent == 0.0
        assert fc.total_cost == 0.0
        assert fc.waived is False

    def test_negative_job_cost(self):
        ledger = BillingLedger()
        fc = ledger.calculate_fee(job_cost=-10.0)

        assert fc.fee_amount == 0.0
        assert fc.fee_percent == 0.0
        assert fc.total_cost == -10.0
        assert fc.waived is False


# ---------------------------------------------------------------------------
# Free tier
# ---------------------------------------------------------------------------

class TestFreeTier:
    """Tests for the free-tier monthly allowance."""

    def test_first_jobs_waived(self):
        """First N jobs should be waived."""
        policy = FeePolicy(free_tier_jobs=3)
        ledger = BillingLedger(fee_policy=policy)

        for i in range(3):
            fc = ledger.calculate_fee(job_cost=100.0)
            assert fc.waived is True
            assert fc.fee_amount == 0.0
            assert fc.total_cost == 100.0
            assert "Free tier" in fc.waiver_reason
            assert f"job {i + 1} of 3" in fc.waiver_reason
            ledger.record_charge(f"job-{i}", fc)

    def test_job_after_free_tier_charged(self):
        """Job (N+1) should be charged normally."""
        policy = FeePolicy(free_tier_jobs=2)
        ledger = BillingLedger(fee_policy=policy)

        # Use up the free tier
        for i in range(2):
            fc = ledger.calculate_fee(job_cost=100.0)
            assert fc.waived is True
            ledger.record_charge(f"free-{i}", fc)

        # Third job should be charged
        fc = ledger.calculate_fee(job_cost=100.0)
        assert fc.waived is False
        assert fc.fee_amount == 5.0
        assert fc.total_cost == 105.0

    def test_zero_free_tier_always_charges(self):
        """With free_tier_jobs=0, the very first job is charged."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        fc = ledger.calculate_fee(job_cost=100.0)

        assert fc.waived is False
        assert fc.fee_amount == 5.0

    def test_free_tier_resets_monthly(self):
        """Free-tier count resets each calendar month."""
        policy = FeePolicy(free_tier_jobs=2)
        ledger = BillingLedger(fee_policy=policy)

        # Record 2 charges with January timestamps
        jan_ts = datetime(2025, 1, 15, tzinfo=timezone.utc).timestamp()
        for i in range(2):
            fc = FeeCalculation(
                job_cost=100.0,
                fee_amount=0.0,
                fee_percent=0.0,
                total_cost=100.0,
                currency="USD",
                waived=True,
                waiver_reason="Free tier",
            )
            entry = {"job_id": f"jan-{i}", "fee_calc": fc, "timestamp": jan_ts}
            ledger._charges.append(entry)

        # In February, the counter should be 0 -- so the next job is free
        feb_now = datetime(2025, 2, 10, tzinfo=timezone.utc)
        with patch("kiln.billing.datetime") as mock_dt:
            mock_dt.now.return_value = feb_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            count = ledger.network_jobs_this_month()
            assert count == 0

            fc = ledger.calculate_fee(job_cost=100.0)
            assert fc.waived is True

    def test_waiver_reason_includes_count(self):
        """Waiver reason should include the current and max count."""
        policy = FeePolicy(free_tier_jobs=5)
        ledger = BillingLedger(fee_policy=policy)

        fc = ledger.calculate_fee(job_cost=50.0)
        assert fc.waiver_reason == "Free tier: job 1 of 5 free this month"


# ---------------------------------------------------------------------------
# Ledger recording and retrieval
# ---------------------------------------------------------------------------

class TestLedgerRecording:
    """Tests for record_charge and get_job_charges."""

    def test_record_and_retrieve(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fc = ledger.calculate_fee(job_cost=100.0)
        ledger.record_charge("job-001", fc)

        result = ledger.get_job_charges("job-001")
        assert result is not None
        assert result.job_cost == 100.0
        assert result.fee_amount == 5.0

    def test_get_nonexistent_job(self):
        ledger = BillingLedger()
        assert ledger.get_job_charges("nonexistent") is None

    def test_multiple_jobs(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))

        for i in range(5):
            fc = ledger.calculate_fee(job_cost=float(10 * (i + 1)))
            ledger.record_charge(f"job-{i}", fc)

        # First job: $10 cost
        assert ledger.get_job_charges("job-0").job_cost == 10.0
        # Last job: $50 cost
        assert ledger.get_job_charges("job-4").job_cost == 50.0

    def test_network_jobs_this_month(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))

        for i in range(3):
            fc = ledger.calculate_fee(job_cost=100.0)
            ledger.record_charge(f"job-{i}", fc)

        assert ledger.network_jobs_this_month() == 3


# ---------------------------------------------------------------------------
# Monthly revenue
# ---------------------------------------------------------------------------

class TestMonthlyRevenue:
    """Tests for monthly_revenue calculation."""

    def test_revenue_with_charges(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))

        for i in range(3):
            fc = ledger.calculate_fee(job_cost=100.0)
            ledger.record_charge(f"job-{i}", fc)

        rev = ledger.monthly_revenue()
        assert rev["total_fees"] == 15.0  # 3 * $5.0
        assert rev["job_count"] == 3
        assert rev["waived_count"] == 0

    def test_revenue_with_waived_jobs(self):
        policy = FeePolicy(free_tier_jobs=2)
        ledger = BillingLedger(fee_policy=policy)

        # 2 waived + 1 charged
        for i in range(3):
            fc = ledger.calculate_fee(job_cost=100.0)
            ledger.record_charge(f"job-{i}", fc)

        rev = ledger.monthly_revenue()
        assert rev["total_fees"] == 5.0  # only 1 charged at $5
        assert rev["job_count"] == 3
        assert rev["waived_count"] == 2

    def test_revenue_empty_ledger(self):
        ledger = BillingLedger()
        rev = ledger.monthly_revenue()
        assert rev["total_fees"] == 0.0
        assert rev["job_count"] == 0
        assert rev["waived_count"] == 0

    def test_revenue_specific_month(self):
        """Filter revenue to a specific year/month."""
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))

        # Record charges with specific timestamps
        jan_ts = datetime(2025, 1, 15, tzinfo=timezone.utc).timestamp()
        feb_ts = datetime(2025, 2, 10, tzinfo=timezone.utc).timestamp()

        fc_jan = FeeCalculation(
            job_cost=100.0,
            fee_amount=5.0,
            fee_percent=5.0,
            total_cost=105.0,
            currency="USD",
        )
        fc_feb = FeeCalculation(
            job_cost=200.0,
            fee_amount=10.0,
            fee_percent=5.0,
            total_cost=210.0,
            currency="USD",
        )

        ledger._charges.append(
            {"job_id": "jan-1", "fee_calc": fc_jan, "timestamp": jan_ts}
        )
        ledger._charges.append(
            {"job_id": "feb-1", "fee_calc": fc_feb, "timestamp": feb_ts}
        )

        rev_jan = ledger.monthly_revenue(year=2025, month=1)
        assert rev_jan["total_fees"] == 5.0
        assert rev_jan["job_count"] == 1

        rev_feb = ledger.monthly_revenue(year=2025, month=2)
        assert rev_feb["total_fees"] == 10.0
        assert rev_feb["job_count"] == 1

    def test_revenue_rounds_to_two_decimals(self):
        """Total fees should be rounded to two decimal places."""
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))

        # Create a fee calculation with an amount that could cause
        # floating-point rounding issues.
        fc = FeeCalculation(
            job_cost=33.33,
            fee_amount=1.6665,
            fee_percent=5.0,
            total_cost=34.9965,
            currency="USD",
        )
        ledger.record_charge("rounding-job", fc)

        rev = ledger.monthly_revenue()
        # 1.6665 should round to 1.67
        assert rev["total_fees"] == 1.67


# ---------------------------------------------------------------------------
# Default policy used when none specified
# ---------------------------------------------------------------------------

class TestDefaultPolicy:
    """Tests that BillingLedger uses default FeePolicy when none is given."""

    def test_default_policy_applied(self):
        ledger = BillingLedger()
        # Default policy: 5 free-tier jobs, so the first job is waived
        fc = ledger.calculate_fee(job_cost=100.0)
        assert fc.waived is True

    def test_explicit_policy_overrides(self):
        policy = FeePolicy(free_tier_jobs=0, network_fee_percent=8.0)
        ledger = BillingLedger(fee_policy=policy)
        fc = ledger.calculate_fee(job_cost=100.0)
        assert fc.waived is False
        assert fc.fee_amount == 8.0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestBillingLedgerThreadSafety:
    """Tests for thread-safe concurrent operations on BillingLedger."""

    def test_concurrent_record_charge(self):
        """Concurrent record_charge calls should not lose entries."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)

        def record_batch(start: int, count: int) -> None:
            for i in range(count):
                fc = FeeCalculation(
                    job_cost=10.0,
                    fee_amount=0.5,
                    fee_percent=5.0,
                    total_cost=10.5,
                    currency="USD",
                )
                ledger.record_charge(f"job-{start + i}", fc)

        threads = [
            threading.Thread(target=record_batch, args=(i * 20, 20))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ledger._charges) == 100

    def test_concurrent_calculate_and_record(self):
        """Concurrent calculate_fee + record_charge should not raise."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)
        errors: list[Exception] = []

        def calculate_and_record(batch_id: int) -> None:
            for i in range(20):
                try:
                    fc = ledger.calculate_fee(job_cost=50.0)
                    ledger.record_charge(f"batch-{batch_id}-{i}", fc)
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=calculate_and_record, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(ledger._charges) == 100

    def test_concurrent_revenue_query(self):
        """Querying monthly_revenue while recording should not raise."""
        policy = FeePolicy(free_tier_jobs=0)
        ledger = BillingLedger(fee_policy=policy)
        errors: list[Exception] = []

        def record_batch() -> None:
            for i in range(50):
                fc = FeeCalculation(
                    job_cost=10.0,
                    fee_amount=0.5,
                    fee_percent=5.0,
                    total_cost=10.5,
                    currency="USD",
                )
                ledger.record_charge(f"job-{i}", fc)

        def query_batch() -> None:
            for _ in range(50):
                try:
                    ledger.monthly_revenue()
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=record_batch)
        t2 = threading.Thread(target=query_batch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0
