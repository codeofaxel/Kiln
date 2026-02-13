"""Payment orchestration layer.

Routes payments to the correct provider (Stripe or Circle), enforces
spend limits, persists payment records, and emits events.

Example::

    mgr = PaymentManager(db=get_db(), config=get_billing_config())
    mgr.register_provider(StripeProvider(...))
    result = mgr.charge_fee(job_id="abc", fee_calc=fee)
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from kiln.billing import BillingLedger, FeeCalculation, SpendLimits
from kiln.payments.base import (
    BILLING_SUPPORT_SUFFIX,
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)

if TYPE_CHECKING:
    from kiln.events import EventBus
    from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)

# Map config rail strings to PaymentRail enum values.
_RAIL_NAMES: Dict[str, PaymentRail] = {
    "stripe": PaymentRail.STRIPE,
    "circle": PaymentRail.CIRCLE,
    "solana": PaymentRail.SOLANA,
    "base": PaymentRail.BASE,
    "crypto": PaymentRail.SOLANA,  # default crypto → Solana
}


class PaymentManager:
    """Orchestrates payment collection across providers.

    Holds registered :class:`PaymentProvider` instances and routes
    :meth:`charge_fee` calls to the correct one based on the active
    rail.  Persists every payment attempt to the ``payments`` table
    and emits events via the :class:`EventBus`.

    Args:
        db: SQLite persistence layer.
        config: Billing config dict (from ``get_billing_config()``).
        event_bus: Optional event bus for payment lifecycle events.
        ledger: Optional billing ledger; created automatically if
            not provided.
    """

    def __init__(
        self,
        db: KilnDB,
        config: Optional[Dict[str, Any]] = None,
        event_bus: Optional[EventBus] = None,
        ledger: Optional[BillingLedger] = None,
    ) -> None:
        self._db = db
        self._config = config or {}
        self._event_bus = event_bus
        self._ledger = ledger or BillingLedger(db=db)
        self._providers: Dict[str, PaymentProvider] = {}
        self._charge_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Provider registration
    # ------------------------------------------------------------------

    def register_provider(self, provider: PaymentProvider) -> None:
        """Register a payment provider by name.

        Args:
            provider: Concrete :class:`PaymentProvider` instance.
        """
        self._providers[provider.name] = provider
        logger.info("Registered payment provider: %s", provider.name)

    def get_provider(self, name: str) -> Optional[PaymentProvider]:
        """Return a registered provider by name, or ``None``."""
        return self._providers.get(name)

    @property
    def available_rails(self) -> List[str]:
        """Return names of all registered providers."""
        return list(self._providers.keys())

    # ------------------------------------------------------------------
    # Rail resolution
    # ------------------------------------------------------------------

    def get_active_rail(self) -> str:
        """Determine which payment rail to use.

        Resolution order:
            1. ``billing.default_rail`` in config
            2. First registered provider

        Returns:
            Provider name string (e.g. ``"stripe"``).

        Raises:
            PaymentError: If no providers are registered.
        """
        configured = self._config.get("default_rail", "")
        if configured and configured in self._providers:
            return configured
        # Also check mapped names (e.g. "crypto" → "circle")
        if configured and configured in _RAIL_NAMES:
            rail = _RAIL_NAMES[configured]
            for prov in self._providers.values():
                if prov.rail == rail:
                    return prov.name
        if self._providers:
            return next(iter(self._providers))
        raise PaymentError(
            "No payment method configured. "
            "Set up a payment method first: use 'billing_setup_url' to get a "
            "Stripe setup link, or configure Circle USDC with 'kiln billing setup'."
            + BILLING_SUPPORT_SUFFIX,
            code="NO_PROVIDER",
        )

    # ------------------------------------------------------------------
    # Spend limits
    # ------------------------------------------------------------------

    def check_spend_limits(self, amount: float) -> tuple:
        """Check whether *amount* is within configured spend limits.

        Returns:
            ``(True, None)`` if within limits, or
            ``(False, reason)`` if a limit would be exceeded.
        """
        limits_cfg = self._config.get("spend_limits", {})
        limits = SpendLimits(
            max_per_order_usd=limits_cfg.get("max_per_order_usd", 500.0),
            monthly_cap_usd=limits_cfg.get("monthly_cap_usd", 2000.0),
        )
        return self._ledger.check_spend_limits(amount, limits)

    # ------------------------------------------------------------------
    # Payment collection
    # ------------------------------------------------------------------

    def charge_fee(
        self,
        job_id: str,
        fee_calc: FeeCalculation,
        *,
        rail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> PaymentResult:
        """Collect a platform fee for an outsourced order.

        The entire flow is serialized behind ``_charge_lock`` to prevent
        concurrent double-charges for the same job.  An idempotency
        check returns the cached result when a charge already exists for
        the given ``job_id``.

        1. Checks for existing charge (idempotency).
        2. Checks spend limits.
        3. Sends the fee to the chosen provider.
        4. Records the payment in SQLite.
        5. Records the billing charge in the ledger.
        6. Emits payment events.

        Args:
            job_id: The fulfillment order/job ID.
            fee_calc: Pre-calculated fee from :class:`BillingLedger`.
            rail: Provider name override (defaults to active rail).
            idempotency_key: Optional caller-supplied key.  Currently
                the ``job_id`` itself provides idempotency; this param
                is accepted for forward-compatibility with external
                retry protocols.

        Returns:
            :class:`PaymentResult` from the provider.

        Raises:
            PaymentError: If spend limits exceeded, no provider
                available, or payment fails unrecoverably.
        """
        with self._charge_lock:
            # 0. Idempotency — return cached result if already charged.
            existing = self._ledger.get_job_charges(job_id)
            if existing is not None:
                logger.info(
                    "Charge already exists for job %s — returning cached result",
                    job_id,
                )
                return PaymentResult(
                    success=True,
                    payment_id="",
                    status=PaymentStatus.COMPLETED,
                    amount=existing.fee_amount,
                    currency=Currency(existing.currency),
                    rail=PaymentRail.STRIPE,
                )

            # Waived fees need no payment.
            if fee_calc.waived or fee_calc.fee_amount <= 0:
                self._ledger.record_charge(
                    job_id, fee_calc,
                    payment_status="waived",
                )
                return PaymentResult(
                    success=True,
                    payment_id="",
                    status=PaymentStatus.COMPLETED,
                    amount=0.0,
                    currency=Currency.USD,
                    rail=PaymentRail.STRIPE,
                )

            # 1. Spend limits
            ok, reason = self.check_spend_limits(fee_calc.fee_amount)
            if not ok:
                self._emit("SPEND_LIMIT_REACHED", {
                    "job_id": job_id,
                    "fee_amount": fee_calc.fee_amount,
                    "reason": reason,
                })
                raise PaymentError(
                    f"Spend limit exceeded: {reason}. "
                    "Adjust your limits in billing settings or wait until next month."
                    + BILLING_SUPPORT_SUFFIX,
                    code="SPEND_LIMIT",
                )

            # 2. Resolve provider
            provider_name = rail or self.get_active_rail()
            provider = self._providers.get(provider_name)
            if provider is None:
                raise PaymentError(
                    "No payment method configured. "
                    "Set up a payment method first: use 'billing_setup_url' to get a "
                    "Stripe setup link, or configure Circle USDC with 'kiln billing setup'."
                    + BILLING_SUPPORT_SUFFIX,
                    code="NO_PROVIDER",
                )

            # 3. Build request
            metadata: Dict[str, Any] = {}
            if provider.name == "circle":
                from kiln.wallets import get_ethereum_wallet, get_solana_wallet
                if provider.rail in (PaymentRail.BASE, PaymentRail.ETHEREUM):
                    metadata["destination_address"] = get_ethereum_wallet().address
                else:
                    metadata["destination_address"] = get_solana_wallet().address
            request = PaymentRequest(
                amount=fee_calc.fee_amount,
                currency=_fee_currency(fee_calc, provider),
                rail=provider.rail,
                job_id=job_id,
                description=f"Kiln platform fee for order {job_id}",
                metadata=metadata,
            )

            # 4. Emit initiated event
            self._emit("PAYMENT_INITIATED", {
                "job_id": job_id,
                "amount": fee_calc.fee_amount,
                "rail": provider.name,
            })

            # 5. Execute payment
            payment_id = secrets.token_hex(8)
            try:
                result = provider.create_payment(request)
            except PaymentError:
                # Record the failed attempt
                self._persist_payment(
                    payment_id, "", provider.name, provider.rail.value,
                    fee_calc.fee_amount, fee_calc.currency,
                    "failed", error="Payment error",
                )
                self._emit("PAYMENT_FAILED", {
                    "job_id": job_id,
                    "rail": provider.name,
                })
                raise

            # 6. Persist payment record
            self._persist_payment(
                payment_id, result.payment_id, provider.name,
                result.rail.value, result.amount, result.currency.value,
                result.status.value,
                tx_hash=result.tx_hash,
                error=result.error,
            )

            # 7. Record billing charge — if this fails, update payment with error
            try:
                charge_id = self._ledger.record_charge(
                    job_id, fee_calc,
                    payment_id=result.payment_id,
                    payment_rail=provider.name,
                    payment_status=result.status.value,
                )
            except Exception as exc:
                logger.error(
                    "Failed to record billing charge for payment %s: %s",
                    payment_id, exc,
                )
                # Update payment record to reflect the billing failure
                self._db.save_payment({
                    "id": payment_id,
                    "error": f"Payment succeeded but billing record failed: {type(exc).__name__}",
                    "status": "billing_error",
                    "updated_at": time.time(),
                })
                raise

            # 8. Emit outcome event
            if result.success:
                self._emit("PAYMENT_COMPLETED", {
                    "job_id": job_id,
                    "charge_id": charge_id,
                    "payment_id": result.payment_id,
                    "amount": result.amount,
                    "rail": provider.name,
                })
            elif result.status == PaymentStatus.PROCESSING:
                self._emit("PAYMENT_PROCESSING", {
                    "job_id": job_id,
                    "charge_id": charge_id,
                    "payment_id": result.payment_id,
                    "amount": result.amount,
                    "rail": provider.name,
                    "message": (
                        "Payment initiated but not yet confirmed. "
                        "Use check_payment_status to poll for completion."
                    ),
                })
            else:
                self._emit("PAYMENT_FAILED", {
                    "job_id": job_id,
                    "payment_id": result.payment_id,
                    "error": result.error,
                    "rail": provider.name,
                })

            return result

    # ------------------------------------------------------------------
    # Auth-and-capture flow
    # ------------------------------------------------------------------

    def authorize_fee(
        self,
        job_id: str,
        fee_calc: FeeCalculation,
        *,
        rail: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> PaymentResult:
        """Place a hold for the platform fee (quote-time).

        Use this at quote acceptance to hold the fee on the user's card.
        If the provider doesn't support holds, falls back to a no-op
        (the fee will be collected at order time via :meth:`charge_fee`).

        The entire flow is serialized behind ``_charge_lock`` to prevent
        concurrent duplicate authorizations.

        Args:
            job_id: Quote or job identifier.
            fee_calc: Pre-calculated fee.
            rail: Provider name override.
            idempotency_key: Optional caller-supplied key for
                forward-compatibility with external retry protocols.

        Returns:
            :class:`PaymentResult` with ``AUTHORIZED`` status on success.
        """
        with self._charge_lock:
            if fee_calc.waived or fee_calc.fee_amount <= 0:
                return PaymentResult(
                    success=True,
                    payment_id="",
                    status=PaymentStatus.COMPLETED,
                    amount=0.0,
                    currency=Currency.USD,
                    rail=PaymentRail.STRIPE,
                )

            ok, reason = self.check_spend_limits(fee_calc.fee_amount)
            if not ok:
                self._emit("SPEND_LIMIT_REACHED", {
                    "job_id": job_id,
                    "fee_amount": fee_calc.fee_amount,
                    "reason": reason,
                })
                raise PaymentError(
                    f"Spend limit exceeded: {reason}. "
                    "Adjust your limits in billing settings or wait until next month."
                    + BILLING_SUPPORT_SUFFIX,
                    code="SPEND_LIMIT",
                )

            provider_name = rail or self.get_active_rail()
            provider = self._providers.get(provider_name)
            if provider is None:
                raise PaymentError(
                    "No payment method configured. "
                    "Set up a payment method first: use 'billing_setup_url' to get a "
                    "Stripe setup link, or configure Circle USDC with 'kiln billing setup'."
                    + BILLING_SUPPORT_SUFFIX,
                    code="NO_PROVIDER",
                )

            metadata: Dict[str, Any] = {}
            if provider.name == "circle":
                from kiln.wallets import get_ethereum_wallet, get_solana_wallet
                if provider.rail in (PaymentRail.BASE, PaymentRail.ETHEREUM):
                    metadata["destination_address"] = get_ethereum_wallet().address
                else:
                    metadata["destination_address"] = get_solana_wallet().address
            request = PaymentRequest(
                amount=fee_calc.fee_amount,
                currency=_fee_currency(fee_calc, provider),
                rail=provider.rail,
                job_id=job_id,
                description=f"Kiln fee hold for quote {job_id}",
                metadata=metadata,
            )

            try:
                result = provider.authorize_payment(request)
            except NotImplementedError:
                # Provider doesn't support holds — return a synthetic
                # "authorized" result; charge_fee will do the real work later.
                return PaymentResult(
                    success=True,
                    payment_id="",
                    status=PaymentStatus.AUTHORIZED,
                    amount=fee_calc.fee_amount,
                    currency=_fee_currency(fee_calc, provider),
                    rail=provider.rail,
                )

            # Persist the authorization
            payment_id = secrets.token_hex(8)
            self._persist_payment(
                payment_id, result.payment_id, provider.name,
                result.rail.value, result.amount, result.currency.value,
                result.status.value,
            )

            self._emit("PAYMENT_INITIATED", {
                "job_id": job_id,
                "amount": fee_calc.fee_amount,
                "rail": provider.name,
                "type": "authorization",
            })

            return result

    def capture_fee(
        self,
        payment_id: str,
        job_id: str,
        fee_calc: FeeCalculation,
        *,
        rail: Optional[str] = None,
    ) -> PaymentResult:
        """Capture a previously authorized hold (order-time).

        If the hold was a no-op (provider doesn't support auth), this
        falls back to :meth:`charge_fee` for a one-shot payment.

        Args:
            payment_id: PaymentIntent ID from :meth:`authorize_fee`.
            job_id: The fulfillment order ID.
            fee_calc: Fee calculation (same as at auth time).
            rail: Provider name override.

        Returns:
            :class:`PaymentResult` with ``COMPLETED`` status.
        """
        # No real hold was placed — do a normal charge.
        if not payment_id:
            return self.charge_fee(job_id, fee_calc, rail=rail)

        provider_name = rail or self.get_active_rail()
        provider = self._providers.get(provider_name)
        if provider is None:
            raise PaymentError(
                f"Payment provider {provider_name!r} not registered.",
                code="NO_PROVIDER",
            )

        try:
            result = provider.capture_payment(payment_id)
        except NotImplementedError:
            # Shouldn't happen if authorize succeeded, but fallback.
            return self.charge_fee(job_id, fee_calc, rail=rail)

        # Record billing charge with the captured payment.
        charge_id = self._ledger.record_charge(
            job_id, fee_calc,
            payment_id=result.payment_id,
            payment_rail=provider.name,
            payment_status=result.status.value,
        )

        # Update persisted payment status.
        self._db.update_payment_status(
            payment_id, result.status.value,
        )

        if result.success:
            self._emit("PAYMENT_COMPLETED", {
                "job_id": job_id,
                "charge_id": charge_id,
                "payment_id": result.payment_id,
                "amount": result.amount,
                "rail": provider.name,
            })

        return result

    def cancel_fee(
        self,
        payment_id: str,
        *,
        rail: Optional[str] = None,
    ) -> PaymentResult:
        """Release a hold without charging (cancellation).

        Args:
            payment_id: PaymentIntent ID from :meth:`authorize_fee`.
            rail: Provider name override.

        Returns:
            :class:`PaymentResult` with ``CANCELLED`` status.
        """
        if not payment_id:
            return PaymentResult(
                success=True,
                payment_id="",
                status=PaymentStatus.CANCELLED,
                amount=0.0,
                currency=Currency.USD,
                rail=PaymentRail.STRIPE,
            )

        provider_name = rail or self.get_active_rail()
        provider = self._providers.get(provider_name)
        if provider is None:
            raise PaymentError(
                f"Payment provider {provider_name!r} not registered.",
                code="NO_PROVIDER",
            )

        try:
            result = provider.cancel_payment(payment_id)
        except NotImplementedError:
            return PaymentResult(
                success=True,
                payment_id=payment_id,
                status=PaymentStatus.CANCELLED,
                amount=0.0,
                currency=Currency.USD,
                rail=provider.rail,
            )

        self._db.update_payment_status(
            payment_id, result.status.value,
        )

        return result

    # ------------------------------------------------------------------
    # Setup URLs
    # ------------------------------------------------------------------

    def get_setup_url(self, rail: str = "stripe") -> str:
        """Return a URL the user can visit to link a payment method.

        Args:
            rail: Provider name (default ``"stripe"``).

        Returns:
            URL string.

        Raises:
            PaymentError: If the provider doesn't support setup URLs
                or is not registered.
        """
        provider = self._providers.get(rail)
        if provider is None:
            raise PaymentError(
                f"Provider {rail!r} not registered.",
                code="NO_PROVIDER",
            )
        if not hasattr(provider, "create_setup_url"):
            raise PaymentError(
                f"Provider {rail!r} does not support setup URLs. "
                "Use the provider's dashboard to add a payment method.",
                code="NO_SETUP_URL",
            )
        return provider.create_setup_url()

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def get_billing_status(self, user_id: str) -> Dict[str, Any]:
        """Return enriched billing status for a user.

        Combines fee policy, monthly spend, payment methods, and
        spend limits into a single dict for the ``billing_status``
        MCP tool and ``kiln billing status`` CLI command.
        """
        revenue = self._ledger.monthly_revenue()
        policy = self._ledger._policy

        methods = self._db.list_payment_methods(user_id)
        default_method = self._db.get_default_payment_method(user_id)

        limits_cfg = self._config.get("spend_limits", {})

        return {
            "user_id": user_id,
            "month_revenue": revenue,
            "fee_policy": {
                "network_fee_percent": policy.network_fee_percent,
                "min_fee_usd": policy.min_fee_usd,
                "max_fee_usd": policy.max_fee_usd,
                "free_tier_jobs": policy.free_tier_jobs,
                "currency": policy.currency,
            },
            "network_jobs_this_month": self._ledger.network_jobs_this_month(),
            "payment_methods": [
                {
                    "id": m["id"],
                    "rail": m["rail"],
                    "label": m.get("label", ""),
                    "is_default": m.get("is_default", False),
                }
                for m in methods
            ],
            "default_payment_method": (
                {
                    "id": default_method["id"],
                    "rail": default_method["rail"],
                    "label": default_method.get("label", ""),
                }
                if default_method
                else None
            ),
            "spend_limits": {
                "max_per_order_usd": limits_cfg.get("max_per_order_usd", 500.0),
                "monthly_cap_usd": limits_cfg.get("monthly_cap_usd", 2000.0),
            },
            "available_rails": self.available_rails,
        }

    def get_billing_history(
        self, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return recent billing charges with payment outcomes."""
        return self._ledger.list_charges(limit=limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_payment(
        self,
        internal_id: str,
        provider_id: str,
        provider_name: str,
        rail: str,
        amount: float,
        currency: str,
        status: str,
        *,
        tx_hash: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Write a payment transaction to the ``payments`` table."""
        now = time.time()
        self._db.save_payment({
            "id": internal_id,
            "charge_id": "",  # filled in after charge is recorded
            "provider_id": provider_id,
            "rail": rail,
            "amount": amount,
            "currency": currency,
            "status": status,
            "tx_hash": tx_hash,
            "error": error,
            "created_at": now,
            "updated_at": now,
        })

    def _emit(self, event_name: str, data: Dict[str, Any]) -> None:
        """Emit a payment event if an event bus is available."""
        if self._event_bus is None:
            return
        try:
            from kiln.events import EventType
            event_type = EventType[event_name]
            self._event_bus.publish(event_type, data, source="payments")
        except KeyError:
            logger.warning(
                "Unknown payment event type %r — check EventType enum has this member",
                event_name,
            )
        except Exception:
            logger.warning("Failed to emit payment event %s", event_name, exc_info=True)


def _fee_currency(
    fee_calc: FeeCalculation,
    provider: PaymentProvider,
) -> Currency:
    """Map the fee currency string to the provider's supported currency."""
    try:
        return Currency(fee_calc.currency)
    except ValueError:
        logger.warning(
            "Currency %r not recognized — falling back to provider default. "
            "Supported currencies: %s",
            fee_calc.currency,
            [c.value for c in provider.supported_currencies] if provider.supported_currencies else ["USD"],
        )
    # Default to the first currency the provider supports.
    if provider.supported_currencies:
        return provider.supported_currencies[0]
    return Currency.USD
