"""Revenue / fee system -- Kiln's platform fee on outsourced orders.

Kiln charges a percentage-based platform fee on orders placed through
external manufacturing services (Craftcloud, and future providers).
**All local printing is free.**  Kiln only takes a cut when it brokers
orders to external fulfillment providers via ``kiln order`` or the
``fulfillment_*`` MCP tools.

Fee schedule
~~~~~~~~~~~~
- ``network_fee_percent`` (default 5 %) of the provider's quoted price.
- Subject to a per-order minimum (``min_fee_usd``) and cap (``max_fee_usd``).
- First ``free_tier_jobs`` outsourced orders per calendar month are
  waived to encourage adoption.

Free operations (no fee ever)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- Local printing (start_print, cancel, pause, resume)
- File management (upload, delete, list)
- Status / monitoring
- Fleet management
- G-code validation / sending
- Event bus / webhooks
- Slicing
- Marketplace search / download

Paid operations (fee applies)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- Fulfillment orders via ``kiln order`` / ``fulfillment_order`` MCP tool

Example::

    ledger = BillingLedger()
    fee = ledger.calculate_fee(job_cost=120.00)
    print(fee.fee_amount, fee.total_cost)
    ledger.record_charge("job-abc", fee)
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaymentStatus(str, Enum):
    """Payment status values for billing charges."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FeePolicy:
    """Configurable fee schedule for Kiln platform services.

    Attributes:
        network_fee_percent: Kiln's percentage cut on outsourced orders.
        min_fee_usd: Minimum fee charged per order.
        max_fee_usd: Maximum fee cap per order.
        free_tier_jobs: Number of orders per calendar month that
            are waived (free) to encourage adoption.
        currency: Default currency for fees.
    """

    network_fee_percent: float = 5.0
    min_fee_usd: float = 0.25
    max_fee_usd: float = 200.0
    free_tier_jobs: int = 5
    currency: str = "USD"


@dataclass
class FeeCalculation:
    """Result of a fee calculation for a single order.

    Attributes:
        job_cost: Raw cost from the manufacturing provider.
        fee_amount: Kiln's calculated fee (may be 0 if waived).
        fee_percent: Effective fee percentage applied.
        total_cost: ``job_cost + fee_amount + tax_amount``.
        currency: Currency of the amounts.
        waived: ``True`` if the fee was waived (e.g. free tier).
        waiver_reason: Human-readable explanation when ``waived`` is True.
        tax_amount: Tax on the platform fee (0.0 if no tax applies).
        tax_rate: Effective tax rate applied (decimal, e.g. 0.19).
        tax_jurisdiction: Jurisdiction code (e.g. ``"DE"``, ``"US-CA"``).
        tax_type: Type of tax (``"vat"``, ``"sales_tax"``, etc.).
        tax_reverse_charge: ``True`` if B2B reverse charge applies.
    """

    job_cost: float
    fee_amount: float
    fee_percent: float
    total_cost: float
    currency: str
    waived: bool = False
    waiver_reason: str | None = None
    tax_amount: float = 0.0
    tax_rate: float = 0.0
    tax_jurisdiction: str | None = None
    tax_type: str | None = None
    tax_reverse_charge: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        d: dict[str, Any] = {
            "job_cost": self.job_cost,
            "fee_amount": self.fee_amount,
            "fee_percent": self.fee_percent,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "waived": self.waived,
            "waiver_reason": self.waiver_reason,
        }
        # Include tax fields only when a jurisdiction was applied.
        if self.tax_jurisdiction is not None:
            d["tax_amount"] = self.tax_amount
            d["tax_rate"] = self.tax_rate
            d["tax_rate_percent"] = round(self.tax_rate * 100, 2)
            d["tax_jurisdiction"] = self.tax_jurisdiction
            d["tax_type"] = self.tax_type
            d["tax_reverse_charge"] = self.tax_reverse_charge
        return d


@dataclass
class SpendLimits:
    """User-configurable spending limits for automated ordering.

    Attributes:
        max_per_order_usd: Reject single orders with fees above this.
        monthly_cap_usd: Reject when monthly fee total would exceed this.
    """

    max_per_order_usd: float = 500.0
    monthly_cap_usd: float = 2000.0


# ---------------------------------------------------------------------------
# Billing ledger
# ---------------------------------------------------------------------------


class BillingLedger:
    """Thread-safe billing ledger with optional SQLite persistence.

    When a :class:`~kiln.persistence.KilnDB` instance is provided, charges
    are persisted to the ``billing_charges`` table and survive restarts.
    Without a ``db``, the ledger operates in-memory (useful for tests).
    """

    def __init__(
        self,
        fee_policy: FeePolicy | None = None,
        db: KilnDB | None = None,
    ) -> None:
        self._policy: FeePolicy = fee_policy or FeePolicy()
        self._db = db
        # In-memory fallback when no db is provided.
        self._charges: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Fee calculation
    # ------------------------------------------------------------------

    def calculate_fee(
        self,
        job_cost: float,
        currency: str = "USD",
        *,
        jurisdiction: str | None = None,
        business_tax_id: str | None = None,
    ) -> FeeCalculation:
        """Calculate the Kiln fee for an outsourced order.

        Args:
            job_cost: Raw cost from the manufacturing provider.
            currency: Currency of the job cost.
            jurisdiction: Buyer's tax jurisdiction code (e.g. ``"US-CA"``, ``"DE"``).
            business_tax_id: Buyer's business tax ID for B2B reverse charge.

        Returns:
            A :class:`FeeCalculation` with all fee details (including tax if applicable).
        """
        fc = self._calculate_fee_locked(job_cost, currency)
        if jurisdiction and fc.fee_amount > 0:
            fc = self._apply_tax(fc, jurisdiction, business_tax_id=business_tax_id)
        return fc

    def _apply_tax(
        self,
        fee_calc: FeeCalculation,
        jurisdiction: str,
        *,
        business_tax_id: str | None = None,
    ) -> FeeCalculation:
        """Apply tax to a FeeCalculation based on jurisdiction.

        Mutates ``total_cost`` to include the tax amount.
        """
        from kiln.tax import TaxCalculator

        calc = TaxCalculator()
        result = calc.calculate_tax(
            fee_calc.fee_amount,
            jurisdiction,
            business_tax_id=business_tax_id,
        )
        fee_calc.tax_amount = result.tax_amount
        fee_calc.tax_rate = result.effective_rate
        fee_calc.tax_jurisdiction = result.jurisdiction_code
        fee_calc.tax_type = result.tax_type.value
        fee_calc.tax_reverse_charge = result.reverse_charge
        # Update total to include tax.
        fee_calc.total_cost = round(
            fee_calc.job_cost + fee_calc.fee_amount + result.tax_amount,
            2,
        )
        return fee_calc

    def _calculate_fee_locked(
        self,
        job_cost: float,
        currency: str = "USD",
    ) -> FeeCalculation:
        """Core fee calculation logic.

        May be called with or without ``self._lock`` held.  The public
        :meth:`calculate_fee` delegates here for backward compatibility;
        :meth:`calculate_and_record_fee` calls this while already
        holding the lock.
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
                waiver_reason=(f"Free tier: job {network_jobs + 1} of {policy.free_tier_jobs} free this month"),
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
    # Spend limits
    # ------------------------------------------------------------------

    def check_spend_limits(
        self,
        fee_amount: float,
        limits: SpendLimits | None = None,
    ) -> tuple:
        """Check whether a fee amount is within spend limits.

        Args:
            fee_amount: The fee about to be charged.
            limits: Spend limits to enforce.  Uses generous defaults
                if not provided.

        Returns:
            ``(True, None)`` if within limits, or
            ``(False, reason_string)`` if a limit would be exceeded.
        """
        lim = limits or SpendLimits()

        if fee_amount > lim.max_per_order_usd:
            return (
                False,
                f"Fee ${fee_amount:.2f} exceeds per-order limit ${lim.max_per_order_usd:.2f}",
            )

        month_total = self._get_monthly_fee_total()
        if month_total + fee_amount > lim.monthly_cap_usd:
            return (
                False,
                f"Monthly cap ${lim.monthly_cap_usd:.2f} would be exceeded (current: ${month_total:.2f})",
            )

        return (True, None)

    def _get_monthly_fee_total(self) -> float:
        """Sum of fees charged this month."""
        if self._db is not None:
            return self._db.monthly_fee_total()
        # In-memory fallback.
        now = datetime.now(timezone.utc)
        total = 0.0
        with self._lock:
            for entry in self._charges:
                ts = datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc)
                if ts.year == now.year and ts.month == now.month:
                    fc: FeeCalculation = entry["fee_calc"]
                    total += fc.fee_amount
        return round(total, 2)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_charge(
        self,
        job_id: str,
        fee_calc: FeeCalculation,
        *,
        payment_id: str | None = None,
        payment_rail: str | None = None,
        payment_status: str = "pending",
    ) -> str:
        """Record a fee calculation against a job in the ledger.

        Args:
            job_id: Unique identifier of the order.
            fee_calc: The :class:`FeeCalculation` to record.
            payment_id: Provider-specific payment ID (Stripe PI, Circle transfer).
            payment_rail: Payment rail used (``"stripe"``, ``"solana"``, etc.).
            payment_status: Status of the payment (``"completed"``, ``"failed"``, etc.).

        Returns:
            The generated charge ID.
        """
        return self._record_charge_locked(
            job_id,
            fee_calc,
            payment_id=payment_id,
            payment_rail=payment_rail,
            payment_status=payment_status,
        )

    def _record_charge_locked(
        self,
        job_id: str,
        fee_calc: FeeCalculation,
        *,
        payment_id: str | None = None,
        payment_rail: str | None = None,
        payment_status: str = "pending",
    ) -> str:
        """Core charge-recording logic.

        May be called with or without ``self._lock`` held.  The public
        :meth:`record_charge` delegates here for backward compatibility;
        :meth:`calculate_and_record_fee` calls this while already
        holding the lock.
        """
        charge_id = secrets.token_hex(8)
        now = time.time()

        if self._db is not None:
            self._db.save_billing_charge(
                {
                    "id": charge_id,
                    "job_id": job_id,
                    "order_id": job_id,
                    "fee_amount": fee_calc.fee_amount,
                    "fee_percent": fee_calc.fee_percent,
                    "job_cost": fee_calc.job_cost,
                    "total_cost": fee_calc.total_cost,
                    "currency": fee_calc.currency,
                    "waived": fee_calc.waived,
                    "waiver_reason": fee_calc.waiver_reason,
                    "tax_amount": fee_calc.tax_amount,
                    "tax_rate": fee_calc.tax_rate,
                    "tax_jurisdiction": fee_calc.tax_jurisdiction,
                    "tax_type": fee_calc.tax_type,
                    "tax_reverse_charge": fee_calc.tax_reverse_charge,
                    "payment_id": payment_id,
                    "payment_rail": payment_rail,
                    "payment_status": payment_status,
                    "created_at": now,
                }
            )
        else:
            # In-memory fallback.
            entry: dict[str, Any] = {
                "id": charge_id,
                "job_id": job_id,
                "fee_calc": fee_calc,
                "timestamp": now,
                "payment_id": payment_id,
                "payment_rail": payment_rail,
                "payment_status": payment_status,
            }
            with self._lock:
                self._charges.append(entry)

        logger.info(
            "Recorded charge %s for job %s: fee=%.2f tax=%.2f %s (waived=%s, payment=%s)",
            charge_id,
            job_id,
            fee_calc.fee_amount,
            fee_calc.tax_amount,
            fee_calc.currency,
            fee_calc.waived,
            payment_status,
        )
        return charge_id

    def calculate_and_record_fee(
        self,
        job_id: str,
        job_cost: float,
        currency: str = "USD",
        *,
        payment_id: str | None = None,
        payment_rail: str | None = None,
        payment_status: str = "pending",
        jurisdiction: str | None = None,
        business_tax_id: str | None = None,
    ) -> tuple[FeeCalculation, str]:
        """Atomically calculate fee (with tax) and record charge.

        Holds the lock for the entire calculate-then-record sequence to
        prevent race conditions where concurrent requests see stale
        free-tier counts or monthly totals.

        Args:
            job_id: Unique identifier of the order.
            job_cost: Raw cost from the manufacturing provider.
            currency: Currency of the job cost.
            payment_id: Provider-specific payment ID.
            payment_rail: Payment rail used.
            payment_status: Status of the payment.
            jurisdiction: Buyer's tax jurisdiction code.
            business_tax_id: Buyer's business tax ID for B2B reverse charge.

        Returns:
            Tuple of ``(FeeCalculation, charge_id)``.
        """
        with self._lock:
            fee_calc = self._calculate_fee_locked(job_cost, currency)
            if jurisdiction and fee_calc.fee_amount > 0:
                fee_calc = self._apply_tax(
                    fee_calc,
                    jurisdiction,
                    business_tax_id=business_tax_id,
                )
            charge_id = self._record_charge_locked(
                job_id,
                fee_calc,
                payment_id=payment_id,
                payment_rail=payment_rail,
                payment_status=payment_status,
            )
        return fee_calc, charge_id

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def monthly_revenue(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, Any]:
        """Summarise revenue for a given calendar month.

        Args:
            year: Calendar year (defaults to current year).
            month: Calendar month 1-12 (defaults to current month).

        Returns:
            Dictionary with ``total_fees``, ``job_count``, and
            ``waived_count``.
        """
        if self._db is not None:
            return self._db.monthly_billing_summary(year=year, month=month)

        # In-memory fallback.
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
        """Count orders recorded in the current calendar month."""
        if self._db is not None:
            return self._db.billing_charges_this_month()

        # In-memory fallback.
        now = datetime.now(timezone.utc)
        count = 0
        with self._lock:
            for entry in self._charges:
                ts = datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc)
                if ts.year == now.year and ts.month == now.month:
                    count += 1
        return count

    def get_job_charges(self, job_id: str) -> FeeCalculation | None:
        """Look up the fee calculation for a specific job.

        Args:
            job_id: The job identifier.

        Returns:
            The :class:`FeeCalculation` if found, otherwise ``None``.
        """
        if self._db is not None:
            charge = self._db.get_billing_charge(job_id)
            if charge is None:
                # Try by job_id column.
                self._db.list_billing_charges(limit=1)
                for c in self._db.list_billing_charges(limit=500):
                    if c["job_id"] == job_id:
                        charge = c
                        break
            if charge is None:
                return None
            return FeeCalculation(
                job_cost=charge["job_cost"],
                fee_amount=charge["fee_amount"],
                fee_percent=charge["fee_percent"],
                total_cost=charge["total_cost"],
                currency=charge["currency"],
                waived=charge["waived"],
                waiver_reason=charge.get("waiver_reason"),
                tax_amount=charge.get("tax_amount", 0.0),
                tax_rate=charge.get("tax_rate", 0.0),
                tax_jurisdiction=charge.get("tax_jurisdiction"),
                tax_type=charge.get("tax_type"),
                tax_reverse_charge=charge.get("tax_reverse_charge", False),
            )

        # In-memory fallback.
        with self._lock:
            for entry in self._charges:
                if entry["job_id"] == job_id:
                    return entry["fee_calc"]
        return None

    def list_charges(
        self,
        limit: int = 50,
        month: int | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent billing charges as dicts.

        Args:
            limit: Maximum records to return.
            month: Filter by calendar month (requires year).
            year: Filter by calendar year (requires month).

        Returns:
            List of charge dicts, newest first.
        """
        if self._db is not None:
            return self._db.list_billing_charges(
                limit=limit,
                month=month,
                year=year,
            )

        # In-memory fallback.
        results: list[dict[str, Any]] = []
        with self._lock:
            for entry in reversed(self._charges):
                fc: FeeCalculation = entry["fee_calc"]
                charge_dict: dict[str, Any] = {
                    "id": entry.get("id", ""),
                    "job_id": entry["job_id"],
                    "fee_amount": fc.fee_amount,
                    "fee_percent": fc.fee_percent,
                    "job_cost": fc.job_cost,
                    "total_cost": fc.total_cost,
                    "currency": fc.currency,
                    "waived": fc.waived,
                    "waiver_reason": fc.waiver_reason,
                    "payment_id": entry.get("payment_id"),
                    "payment_rail": entry.get("payment_rail"),
                    "payment_status": entry.get("payment_status", "pending"),
                    "created_at": entry["timestamp"],
                }
                if fc.tax_jurisdiction is not None:
                    charge_dict["tax_amount"] = fc.tax_amount
                    charge_dict["tax_rate"] = fc.tax_rate
                    charge_dict["tax_jurisdiction"] = fc.tax_jurisdiction
                    charge_dict["tax_type"] = fc.tax_type
                    charge_dict["tax_reverse_charge"] = fc.tax_reverse_charge
                results.append(charge_dict)
                if len(results) >= limit:
                    break
        return results

    # ------------------------------------------------------------------
    # Refunds & reconciliation
    # ------------------------------------------------------------------

    def refund_charge(
        self,
        *,
        charge_id: str | None = None,
        job_id: str | None = None,
        refund_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Refund a charge by updating its payment status to 'refunded'.

        Args:
            charge_id: The charge ID to refund (mutually exclusive with job_id).
            job_id: The job ID to refund (mutually exclusive with charge_id).
            refund_reason: Human-readable reason for the refund.

        Returns:
            The updated charge record, or ``None`` if not found.

        Raises:
            ValueError: If neither charge_id nor job_id is provided.
        """
        if charge_id is None and job_id is None:
            raise ValueError("Must provide either charge_id or job_id")

        refunded_at = time.time()

        if self._db is not None:
            # Find the charge.
            charge = None
            if charge_id:
                charge = self._db.get_billing_charge(charge_id)
            else:
                # Search by job_id.
                for c in self._db.list_billing_charges(limit=500):
                    if c["job_id"] == job_id:
                        charge = c
                        break

            if charge is None:
                logger.warning(
                    "Refund requested for charge_id=%s job_id=%s but not found",
                    charge_id,
                    job_id,
                )
                return None

            # Update payment status to refunded.
            self._db.update_billing_charge(
                charge["id"],
                payment_status=PaymentStatus.REFUNDED.value,
                refund_reason=refund_reason,
                refunded_at=refunded_at,
            )

            # Publish event if event bus is available.
            try:
                from kiln.events import get_event_bus

                bus = get_event_bus()
                if bus:
                    bus.publish(
                        "PAYMENT_REFUNDED",
                        {
                            "charge_id": charge["id"],
                            "job_id": charge["job_id"],
                            "fee_amount": charge["fee_amount"],
                            "refund_reason": refund_reason,
                            "refunded_at": refunded_at,
                        },
                    )
            except Exception as e:
                logger.debug("Event bus not available for PAYMENT_REFUNDED: %s", e)

            logger.info(
                "Refunded charge %s (job %s): fee=%.2f %s, reason=%s",
                charge["id"],
                charge["job_id"],
                charge["fee_amount"],
                charge["currency"],
                refund_reason,
            )

            # Return updated charge.
            return self._db.get_billing_charge(charge["id"])

        # In-memory fallback.
        with self._lock:
            for entry in self._charges:
                match = (charge_id and entry.get("id") == charge_id) or (job_id and entry["job_id"] == job_id)
                if match:
                    entry["payment_status"] = PaymentStatus.REFUNDED.value
                    entry["refund_reason"] = refund_reason
                    entry["refunded_at"] = refunded_at

                    logger.info(
                        "Refunded charge %s (job %s): fee=%.2f %s, reason=%s",
                        entry.get("id", "unknown"),
                        entry["job_id"],
                        entry["fee_calc"].fee_amount,
                        entry["fee_calc"].currency,
                        refund_reason,
                    )

                    # Build return dict.
                    fc: FeeCalculation = entry["fee_calc"]
                    return {
                        "id": entry.get("id", ""),
                        "job_id": entry["job_id"],
                        "fee_amount": fc.fee_amount,
                        "payment_status": PaymentStatus.REFUNDED.value,
                        "refund_reason": refund_reason,
                        "refunded_at": refunded_at,
                    }

        logger.warning(
            "Refund requested for charge_id=%s job_id=%s but not found",
            charge_id,
            job_id,
        )
        return None

    def reconcile_pending_charges(
        self,
        *,
        stale_threshold_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Reconcile stale pending charges by checking associated job status.

        Scans for charges with ``payment_status="pending"`` older than
        the threshold, checks if the associated job completed or failed,
        and auto-refunds failed jobs or flags completed jobs for collection.

        Args:
            stale_threshold_seconds: Age threshold for stale pending charges
                (default 1 hour).

        Returns:
            Reconciliation report with counts: ``refunded``,
            ``flagged_for_collection``, ``still_pending``.
        """
        now = time.time()
        cutoff = now - stale_threshold_seconds

        refunded = 0
        flagged_for_collection = 0
        still_pending = 0

        if self._db is not None:
            # DB-backed reconciliation.
            pending_charges = self._db.list_billing_charges_by_status(
                PaymentStatus.PENDING.value,
                limit=1000,
            )

            for charge in pending_charges:
                created_at = charge.get("created_at", now)
                if created_at > cutoff:
                    still_pending += 1
                    continue

                job_id = charge["job_id"]
                # Check job status (requires persistence to have job table).
                job_status = (
                    self._db.get_job_status(job_id)
                    if hasattr(
                        self._db,
                        "get_job_status",
                    )
                    else None
                )

                if job_status == "failed":
                    # Auto-refund failed job.
                    self.refund_charge(
                        charge_id=charge["id"],
                        refund_reason="Auto-refund: job failed",
                    )
                    refunded += 1
                elif job_status == "completed":
                    # Mark as needing collection.
                    self._db.update_billing_charge(
                        charge["id"],
                        payment_status=PaymentStatus.PROCESSING.value,
                    )
                    flagged_for_collection += 1
                else:
                    still_pending += 1

        else:
            # In-memory fallback.
            with self._lock:
                for entry in self._charges:
                    if entry.get("payment_status") != PaymentStatus.PENDING.value:
                        continue

                    created_at = entry.get("timestamp", now)
                    if created_at > cutoff:
                        still_pending += 1
                        continue

                    # In-memory mode doesn't track job status, so just flag as stale.
                    still_pending += 1

        logger.info(
            "Reconciled pending charges: refunded=%d, flagged_for_collection=%d, still_pending=%d",
            refunded,
            flagged_for_collection,
            still_pending,
        )

        return {
            "refunded": refunded,
            "flagged_for_collection": flagged_for_collection,
            "still_pending": still_pending,
        }

    def get_aging_report(self) -> dict[str, Any]:
        """Generate an aging report for all charges grouped by age buckets.

        Returns:
            Dictionary with age buckets (``<1h``, ``1-24h``, ``1-7d``, ``>7d``)
            and totals per bucket.
        """
        now = time.time()
        one_hour = 3600
        one_day = 86400
        one_week = 604800

        buckets = {
            "<1h": {"count": 0, "total_fees": 0.0},
            "1-24h": {"count": 0, "total_fees": 0.0},
            "1-7d": {"count": 0, "total_fees": 0.0},
            ">7d": {"count": 0, "total_fees": 0.0},
        }

        if self._db is not None:
            # DB-backed aging report.
            all_charges = self._db.list_billing_charges(limit=10000)
            for charge in all_charges:
                created_at = charge.get("created_at", now)
                age = now - created_at
                fee_amount = charge["fee_amount"]

                if age < one_hour:
                    bucket = "<1h"
                elif age < one_day:
                    bucket = "1-24h"
                elif age < one_week:
                    bucket = "1-7d"
                else:
                    bucket = ">7d"

                buckets[bucket]["count"] += 1
                buckets[bucket]["total_fees"] += fee_amount

        else:
            # In-memory fallback.
            with self._lock:
                for entry in self._charges:
                    created_at = entry.get("timestamp", now)
                    age = now - created_at
                    fee_amount = entry["fee_calc"].fee_amount

                    if age < one_hour:
                        bucket = "<1h"
                    elif age < one_day:
                        bucket = "1-24h"
                    elif age < one_week:
                        bucket = "1-7d"
                    else:
                        bucket = ">7d"

                    buckets[bucket]["count"] += 1
                    buckets[bucket]["total_fees"] += fee_amount

        # Round totals.
        for bucket in buckets.values():
            bucket["total_fees"] = round(bucket["total_fees"], 2)

        return {
            "buckets": buckets,
            "generated_at": now,
        }
