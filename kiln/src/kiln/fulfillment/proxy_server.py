"""Server-side proxy orchestration for fulfillment services.

Handles proxy fulfillment requests from the REST API:
- License validation
- Per-user usage limits
- 5% platform fee calculation and collection
- Order forwarding to fulfillment providers
- Auto-refund on order failure
- Server-side quote caching (prevents client-side price manipulation)

Used by :class:`kiln.rest_api.FulfillmentProxyAPI` to process agent requests
through the Kiln backend.

Example::

    orch = get_orchestrator()
    quote_resp = orch.handle_quote("craftcloud", "/path/to/model.stl", request, user_email="user@example.com")
    order_resp = orch.handle_order("craftcloud", order_request, user_email="user@example.com", user_tier=LicenseTier.FREE)
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from kiln.billing import BillingLedger, FeeCalculation
from kiln.fulfillment.base import (
    FulfillmentError,
    OrderRequest,
    OrderResult,
    QuoteRequest,
)
from kiln.fulfillment.registry import get_provider as get_fulfillment_provider
from kiln.licensing import LicenseInfo, LicenseManager, LicenseTier, generate_license_key
from kiln.payments.base import PaymentError

if TYPE_CHECKING:
    from kiln.payments.manager import PaymentManager
    from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)


class ProxyOrchestrator:
    """Orchestrates proxy fulfillment requests with billing and license checks.

    Args:
        db: SQLite persistence layer.
        event_bus: Optional event bus for lifecycle events.
    """

    def __init__(
        self,
        db: KilnDB,
        *,
        event_bus: Any | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._ledger = BillingLedger(db=db)
        self._payment_mgr: PaymentManager | None = None
        self._payment_lock = threading.Lock()
        # Server-side quote cache: quote_id → (total_price, currency, provider, user_email, expires_at)
        # Prevents clients from manipulating quoted_price at order time.
        self._quote_cache: dict[str, dict[str, Any]] = {}
        self._quote_cache_lock = threading.Lock()
        self._quote_ttl_seconds = 3600  # Quotes expire after 1 hour
        # Per-user locks for atomic free-tier check + charge to prevent
        # race conditions where concurrent requests bypass the limit.
        self._user_order_locks: dict[str, threading.Lock] = {}
        self._user_locks_lock = threading.Lock()

    # ------------------------------------------------------------------
    # License validation
    # ------------------------------------------------------------------

    def validate_license(self, key: str) -> dict[str, Any]:
        """Validate a license key and return tier info.

        Args:
            key: License key string.

        Returns:
            Dict with ``tier``, ``email``, ``valid``, and ``info`` fields.
            If key is invalid, returns ``tier=FREE`` and ``valid=False``.
        """
        if not key or not key.strip():
            return {
                "tier": LicenseTier.FREE.value,
                "valid": False,
                "error": "No license key provided",
            }

        try:
            mgr = LicenseManager(license_key=key)
            tier = mgr.get_tier()
            info = mgr.get_info()
            return {
                "tier": tier.value,
                "email": "",  # Not stored in license key payload currently
                "valid": info.is_valid,
                "info": info.to_dict(),
            }
        except Exception as exc:
            logger.warning("License validation failed: %s", exc)
            return {
                "tier": LicenseTier.FREE.value,
                "valid": False,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Material listing
    # ------------------------------------------------------------------

    def handle_materials(self, provider_name: str) -> list[dict]:
        """List available materials from a fulfillment provider.

        Args:
            provider_name: Provider identifier (e.g. ``"craftcloud"``).

        Returns:
            List of material dicts.

        Raises:
            FulfillmentError: If materials cannot be retrieved.
        """
        provider = get_fulfillment_provider(provider_name)
        materials = provider.list_materials()
        return [m.to_dict() for m in materials]

    # ------------------------------------------------------------------
    # Quote handling
    # ------------------------------------------------------------------

    def handle_quote(
        self,
        provider_name: str,
        file_path: str,
        request: QuoteRequest,
        *,
        user_email: str,
    ) -> dict[str, Any]:
        """Request a quote and calculate the Kiln fee.

        Stores the quote server-side so ``handle_order`` can look up the
        authoritative price instead of trusting client-supplied values.

        Args:
            provider_name: Provider identifier.
            file_path: Path to the model file.
            request: Quote request parameters.
            user_email: User's email address for tracking.

        Returns:
            Dict with ``quote``, ``kiln_fee``, ``total_with_fee``, and
            ``quote_token`` fields.  The ``quote_token`` must be passed
            back at order time.

        Raises:
            FulfillmentError: If quote cannot be generated.
        """
        provider = get_fulfillment_provider(provider_name)
        quote = provider.get_quote(request)

        # Calculate Kiln fee
        fee_calc = self._ledger.calculate_fee(
            quote.total_price,
            currency=quote.currency,
        )

        # Cache quote server-side — clients must present the quote_token
        # at order time; we never trust a client-supplied price.
        quote_token = uuid.uuid4().hex
        with self._quote_cache_lock:
            self._purge_expired_quotes()
            self._quote_cache[quote_token] = {
                "total_price": quote.total_price,
                "currency": quote.currency,
                "provider": provider_name,
                "user_email": user_email,
                "quote_id": quote.quote_id if hasattr(quote, "quote_id") else "",
                "expires_at": time.time() + self._quote_ttl_seconds,
            }

        return {
            "quote": quote.to_dict(),
            "kiln_fee": fee_calc.to_dict(),
            "total_with_fee": fee_calc.total_cost,
            "quote_token": quote_token,
        }

    # ------------------------------------------------------------------
    # Order handling
    # ------------------------------------------------------------------

    def handle_order(
        self,
        provider_name: str,
        request: OrderRequest,
        *,
        user_email: str,
        user_tier: LicenseTier,
        quote_token: str,
    ) -> dict[str, Any]:
        """Place a fulfillment order with fee collection.

        Uses the server-side quote cache (populated by :meth:`handle_quote`)
        to determine the authoritative price.  Client-supplied prices are
        never trusted.

        Workflow:
            1. Look up cached quote by ``quote_token``.
            2. Check free tier limits if user is on free tier.
            3. Calculate and charge the platform fee.
            4. Forward order to the fulfillment provider.
            5. Auto-refund if order fails after payment.

        Args:
            provider_name: Provider identifier.
            request: Order request parameters.
            user_email: User's email address.
            user_tier: User's license tier.
            quote_token: Server-issued token from :meth:`handle_quote`.

        Returns:
            Dict with ``order`` and ``kiln_fee`` fields.

        Raises:
            FulfillmentError: If quote not found, expired, free tier limit
                reached, or order fails.
            PaymentError: If fee collection fails.
        """
        # 0. Retrieve authoritative price from server-side cache
        with self._quote_cache_lock:
            cached = self._quote_cache.pop(quote_token, None)

        if cached is None:
            raise FulfillmentError(
                "Quote not found or already used. Please request a new quote.",
                code="QUOTE_NOT_FOUND",
            )

        if cached["expires_at"] < time.time():
            raise FulfillmentError(
                "Quote has expired. Please request a new quote.",
                code="QUOTE_EXPIRED",
            )

        # Verify the provider matches what was quoted
        if cached["provider"] != provider_name:
            raise FulfillmentError(
                f"Provider mismatch: quote was for '{cached['provider']}', "
                f"but order specifies '{provider_name}'.",
                code="PROVIDER_MISMATCH",
            )

        # Verify quote ownership — the user placing the order must be the
        # same user who requested the quote.
        cached_email = cached.get("user_email", "")
        if cached_email and user_email and cached_email != user_email:
            logger.warning(
                "Order ownership mismatch: quote for %s, order by %s",
                cached_email,
                user_email,
            )
            raise FulfillmentError(
                "Quote was issued to a different user.",
                code="OWNERSHIP_MISMATCH",
            )

        quoted_price = cached["total_price"]
        currency = cached["currency"]

        # Serialize free-tier check + charge per user to prevent race
        # conditions where two concurrent requests both pass the limit check
        # before either records a charge.
        user_lock = self._get_user_lock(user_email)
        with user_lock:
            # 1. Free tier check
            if user_tier < LicenseTier.BUSINESS:
                jobs = self._ledger.network_jobs_this_month_for_user(user_email)
                if jobs >= self._ledger._policy.free_tier_jobs:
                    raise FulfillmentError(
                        f"Free tier limit reached: {jobs}/{self._ledger._policy.free_tier_jobs} orders this month. "
                        "Upgrade to Business tier for unlimited orders.",
                        code="FREE_TIER_LIMIT",
                    )

            # 2. Calculate fee from server-authoritative price
            fee_calc = self._ledger.calculate_fee(
                quoted_price,
                currency=currency,
            )

            # 3. Charge fee if payment manager available
            payment_id = None
            payment_mgr = self._get_payment_mgr()
            if payment_mgr is not None and payment_mgr.available_rails:
                try:
                    payment_result = payment_mgr.charge_fee(
                        request.quote_id,
                        fee_calc,
                    )
                    payment_id = payment_result.payment_id
                    logger.info(
                        "Fee charged for order %s: payment_id=%s",
                        request.quote_id,
                        payment_id,
                    )
                except PaymentError as exc:
                    logger.error("Fee collection failed for order %s: %s", request.quote_id, exc)
                    raise
            else:
                # No payment manager configured — record charge for tracking only.
                charge_id = self._ledger.calculate_and_record_fee(
                    request.quote_id,
                    quoted_price,
                    currency=currency,
                )[1]
                # Tag charge with user_email for free tier tracking
                self._tag_charge_with_user(charge_id, user_email)
                logger.info(
                    "Fee recorded (no payment rail) for order %s: charge_id=%s",
                    request.quote_id,
                    charge_id,
                )

        # 4. Place order with provider
        provider = get_fulfillment_provider(provider_name)
        try:
            result = provider.place_order(request)
        except FulfillmentError as exc:
            # Order failed after fee was charged — attempt refund
            if payment_id:
                logger.warning(
                    "Order %s failed, attempting refund for payment %s",
                    request.quote_id,
                    payment_id,
                )
                try:
                    payment_mgr.cancel_fee(payment_id)
                except Exception as refund_exc:
                    logger.error(
                        "Refund failed for payment %s: %s",
                        payment_id,
                        refund_exc,
                    )
            else:
                logger.warning(
                    "Order %s failed, no payment to refund",
                    request.quote_id,
                )
            raise exc

        return {
            "order": result.to_dict(),
            "kiln_fee": fee_calc.to_dict(),
        }

    # ------------------------------------------------------------------
    # Order status
    # ------------------------------------------------------------------

    def handle_status(
        self,
        provider_name: str,
        order_id: str,
    ) -> dict[str, Any]:
        """Check the status of an existing order.

        Args:
            provider_name: Provider identifier.
            order_id: Order ID from :meth:`handle_order`.

        Returns:
            Order result dict.

        Raises:
            FulfillmentError: If order cannot be queried.
        """
        provider = get_fulfillment_provider(provider_name)
        result = provider.get_order_status(order_id)
        return result.to_dict()

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    def handle_cancel(
        self,
        provider_name: str,
        order_id: str,
        *,
        user_tier: LicenseTier,
    ) -> dict[str, Any]:
        """Cancel an existing order.

        Args:
            provider_name: Provider identifier.
            order_id: Order ID to cancel.
            user_tier: User's license tier (unused currently).

        Returns:
            Updated order result dict.

        Raises:
            FulfillmentError: If order cannot be cancelled.
        """
        provider = get_fulfillment_provider(provider_name)
        result = provider.cancel_order(order_id)
        return result.to_dict()

    # ------------------------------------------------------------------
    # User registration
    # ------------------------------------------------------------------

    def register_user(self, email: str) -> dict[str, Any]:
        """Generate a free-tier license key for a new user.

        Args:
            email: User's email address.

        Returns:
            Dict with ``license_key``, ``tier``, and ``email`` fields.

        Raises:
            ValueError: If signing key is not configured.
        """
        key = generate_license_key(LicenseTier.FREE, email)
        return {
            "license_key": key,
            "tier": "free",
            "email": email,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_user_lock(self, user_email: str) -> threading.Lock:
        """Return a per-user lock for serializing order placement."""
        with self._user_locks_lock:
            if user_email not in self._user_order_locks:
                self._user_order_locks[user_email] = threading.Lock()
            return self._user_order_locks[user_email]

    def _purge_expired_quotes(self) -> None:
        """Remove expired entries from the quote cache.

        Must be called while holding ``_quote_cache_lock``.
        """
        now = time.time()
        expired = [k for k, v in self._quote_cache.items() if v["expires_at"] < now]
        for k in expired:
            del self._quote_cache[k]

    def _get_payment_mgr(self) -> PaymentManager:
        """Lazily initialize the payment manager with auto-detected providers."""
        if self._payment_mgr is not None:
            return self._payment_mgr

        with self._payment_lock:
            if self._payment_mgr is not None:
                return self._payment_mgr

            from kiln.payments.manager import PaymentManager

            self._payment_mgr = PaymentManager(
                db=self._db,
                event_bus=self._event_bus,
                ledger=self._ledger,
            )

            # Auto-register Stripe if configured
            stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
            if stripe_key:
                try:
                    from kiln.payments.stripe_provider import StripeProvider

                    self._payment_mgr.register_provider(
                        StripeProvider(secret_key=stripe_key)
                    )
                    logger.info("Registered Stripe provider for proxy fulfillment")
                except Exception as exc:
                    logger.debug(
                        "Could not register Stripe provider: %s",
                        exc,
                    )

            return self._payment_mgr

    def _tag_charge_with_user(self, charge_id: str, user_email: str) -> None:
        """Tag a billing charge with the user's email for free tier tracking."""
        if not user_email:
            return
        try:
            # Direct SQL update — no update_billing_charge API yet
            self._db._conn.execute(
                "UPDATE billing_charges SET user_email = ? WHERE id = ?",
                (user_email, charge_id),
            )
            self._db._conn.commit()
        except Exception as exc:
            logger.debug(
                "Could not tag charge %s with user_email: %s",
                charge_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_orchestrator: ProxyOrchestrator | None = None
_orch_lock = threading.Lock()


def get_orchestrator() -> ProxyOrchestrator:
    """Return the module-level singleton orchestrator.

    Lazily creates the orchestrator with the default DB on first access.
    """
    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator

    with _orch_lock:
        if _orchestrator is not None:
            return _orchestrator

        from kiln.persistence import get_db

        _orchestrator = ProxyOrchestrator(db=get_db())
        return _orchestrator
