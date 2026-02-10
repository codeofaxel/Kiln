"""Revenue / fee system -- Kiln's take rate on network-routed jobs.

Kiln charges a percentage-based fee on jobs routed through external
manufacturing networks (e.g. 3DOS).  **All local printing is free.**
Kiln only takes a cut when it brokers jobs across the distributed
manufacturing network.

Fee schedule
~~~~~~~~~~~~
- ``network_fee_percent`` (default 5 %) of the raw network job cost.
- Subject to a per-job minimum (``min_fee_usd``) and cap (``max_fee_usd``).
- First ``free_tier_jobs`` network jobs per calendar month are waived to
  encourage adoption.

Free operations (no fee ever)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- Local printing (start_print, cancel, pause, resume)
- File management (upload, delete, list)
- Status / monitoring
- Fleet management
- G-code validation / sending
- Event bus / webhooks

Paid operations (fee applies)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- 3DOS network job routing (``submit_network_job`` via ``threedos.py``)
- Future: premium cloud sync, analytics API

Example::

    ledger = BillingLedger()
    fee = ledger.calculate_fee(job_cost=120.00)
    print(fee.fee_amount, fee.total_cost)
    ledger.record_charge("job-abc", fee)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FeePolicy:
    """Configurable fee schedule for Kiln network services.

    Attributes:
        network_fee_percent: Kiln's percentage cut on network jobs.
        min_fee_usd: Minimum fee charged per network job.
        max_fee_usd: Maximum fee cap per network job.
        free_tier_jobs: Number of network jobs per calendar month that
            are waived (free) to encourage adoption.
        currency: Default currency for fees.
    """

    network_fee_percent: float = 5.0
    min_fee_usd: float = 0.25
    max_fee_usd: float = 50.0
    free_tier_jobs: int = 5
    currency: str = "USD"


@dataclass
class FeeCalculation:
    """Result of a fee calculation for a single network job.

    Attributes:
        job_cost: Raw cost from the manufacturing network.
        fee_amount: Kiln's calculated fee (may be 0 if waived).
        fee_percent: Effective fee percentage applied.
        total_cost: ``job_cost + fee_amount``.
        currency: Currency of the amounts.
        waived: ``True`` if the fee was waived (e.g. free tier).
        waiver_reason: Human-readable explanation when ``waived`` is True.
    """

    job_cost: float
    fee_amount: float
    fee_percent: float
    total_cost: float
    currency: str
    waived: bool = False
    waiver_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "job_cost": self.job_cost,
            "fee_amount": self.fee_amount,
            "fee_percent": self.fee_percent,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "waived": self.waived,
            "waiver_reason": self.waiver_reason,
        }


# ---------------------------------------------------------------------------
# Billing ledger
# ---------------------------------------------------------------------------

class BillingLedger:
    """Thread-safe in-memory billing ledger.

    Tracks fee calculations and charges for network jobs.  This is an
    in-memory implementation; a future version will persist to SQLite or
    Supabase for crash recovery.
    """

    def __init__(self, fee_policy: Optional[FeePolicy] = None) -> None:
        self._policy: FeePolicy = fee_policy or FeePolicy()
        self._charges: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Fee calculation
    # ------------------------------------------------------------------

    def calculate_fee(
        self,
        job_cost: float,
        currency: str = "USD",
    ) -> FeeCalculation:
        """Calculate the Kiln fee for a network job.

        Args:
            job_cost: Raw cost from the manufacturing network.
            currency: Currency of the job cost.

        Returns:
            A :class:`FeeCalculation` with all fee details.
        """
        policy = self._policy

        # Zero or negative job cost -- no fee.
        if job_cost <= 0:
            return FeeCalculation(
                job_cost=job_cost,
                fee_amount=0.0,
                fee_percent=0.0,
                total_cost=job_cost,
                currency=currency,
                waived=False,
                waiver_reason=None,
            )

        # Free-tier check: waive if under the monthly allowance.
        network_jobs = self.network_jobs_this_month()
        if network_jobs < policy.free_tier_jobs:
            return FeeCalculation(
                job_cost=job_cost,
                fee_amount=0.0,
                fee_percent=0.0,
                total_cost=job_cost,
                currency=currency,
                waived=True,
                waiver_reason=(
                    f"Free tier: job {network_jobs + 1} of "
                    f"{policy.free_tier_jobs} free this month"
                ),
            )

        # Standard fee: percentage of job cost, clamped to [min, max].
        raw_fee = job_cost * policy.network_fee_percent / 100.0
        fee = max(min(raw_fee, policy.max_fee_usd), policy.min_fee_usd)
        effective_percent = round((fee / job_cost) * 100.0, 4)

        return FeeCalculation(
            job_cost=job_cost,
            fee_amount=round(fee, 2),
            fee_percent=effective_percent,
            total_cost=round(job_cost + fee, 2),
            currency=currency,
            waived=False,
            waiver_reason=None,
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_charge(
        self,
        job_id: str,
        fee_calc: FeeCalculation,
    ) -> None:
        """Record a fee calculation against a job in the ledger.

        Args:
            job_id: Unique identifier of the network job.
            fee_calc: The :class:`FeeCalculation` to record.
        """
        entry: Dict[str, Any] = {
            "job_id": job_id,
            "fee_calc": fee_calc,
            "timestamp": time.time(),
        }
        with self._lock:
            self._charges.append(entry)
        logger.info(
            "Recorded charge for job %s: fee=%.2f %s (waived=%s)",
            job_id,
            fee_calc.fee_amount,
            fee_calc.currency,
            fee_calc.waived,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def monthly_revenue(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Summarise revenue for a given calendar month.

        Args:
            year: Calendar year (defaults to current year).
            month: Calendar month 1-12 (defaults to current month).

        Returns:
            Dictionary with ``total_fees``, ``job_count``, and
            ``waived_count``.
        """
        now = datetime.now(timezone.utc)
        target_year = year if year is not None else now.year
        target_month = month if month is not None else now.month

        total_fees = 0.0
        job_count = 0
        waived_count = 0

        with self._lock:
            for entry in self._charges:
                ts = datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc)
                if ts.year == target_year and ts.month == target_month:
                    fc: FeeCalculation = entry["fee_calc"]
                    job_count += 1
                    total_fees += fc.fee_amount
                    if fc.waived:
                        waived_count += 1

        return {
            "total_fees": round(total_fees, 2),
            "job_count": job_count,
            "waived_count": waived_count,
        }

    def network_jobs_this_month(self) -> int:
        """Count network jobs recorded in the current calendar month."""
        now = datetime.now(timezone.utc)
        count = 0
        with self._lock:
            for entry in self._charges:
                ts = datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc)
                if ts.year == now.year and ts.month == now.month:
                    count += 1
        return count

    def get_job_charges(self, job_id: str) -> Optional[FeeCalculation]:
        """Look up the fee calculation for a specific job.

        Args:
            job_id: The job identifier.

        Returns:
            The :class:`FeeCalculation` if found, otherwise ``None``.
        """
        with self._lock:
            for entry in self._charges:
                if entry["job_id"] == job_id:
                    return entry["fee_calc"]
        return None
