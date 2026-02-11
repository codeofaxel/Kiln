"""Stripe payment provider for Kiln.

Implements :class:`~kiln.payments.base.PaymentProvider` using `Stripe's
Payment Intents API <https://stripe.com/docs/api/payment_intents>`_ for
per-transaction credit card charges.

The ``stripe`` Python package is an **optional dependency** and is imported
lazily so the rest of the Kiln stack loads without it.

Environment variables
---------------------
``KILN_STRIPE_SECRET_KEY``
    Stripe secret key (``sk_live_...`` or ``sk_test_...``).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)

logger = logging.getLogger(__name__)

# Stripe status string -> internal PaymentStatus
_STATUS_MAP: Dict[str, PaymentStatus] = {
    "succeeded": PaymentStatus.COMPLETED,
    "processing": PaymentStatus.PROCESSING,
    "requires_payment_method": PaymentStatus.FAILED,
    "requires_action": PaymentStatus.FAILED,
    "canceled": PaymentStatus.FAILED,
}


class StripeProvider(PaymentProvider):
    """Concrete :class:`PaymentProvider` backed by the Stripe API.

    Args:
        secret_key: Stripe secret key.  Falls back to
            ``KILN_STRIPE_SECRET_KEY`` if not provided.
        customer_id: Existing Stripe Customer ID (``cus_...``).
        payment_method_id: Default Stripe PaymentMethod ID (``pm_...``).

    Raises:
        PaymentError: If no secret key is available.
    """

    def __init__(
        self,
        secret_key: Optional[str] = None,
        customer_id: Optional[str] = None,
        payment_method_id: Optional[str] = None,
    ) -> None:
        self._secret_key = secret_key or os.environ.get(
            "KILN_STRIPE_SECRET_KEY", ""
        )
        if not self._secret_key:
            raise PaymentError(
                "Stripe secret key required. "
                "Set KILN_STRIPE_SECRET_KEY or pass secret_key.",
                code="MISSING_KEY",
            )

        self._customer_id = customer_id
        self._payment_method_id = payment_method_id

    # -- PaymentProvider identity ----------------------------------------------

    @property
    def name(self) -> str:
        return "stripe"

    @property
    def supported_currencies(self) -> List[Currency]:
        return [Currency.USD, Currency.EUR]

    @property
    def rail(self) -> PaymentRail:
        return PaymentRail.STRIPE

    # -- Lazy import helper ----------------------------------------------------

    def _import_stripe(self) -> Any:
        """Import and configure the ``stripe`` SDK.

        Returns:
            The ``stripe`` module, ready to use.

        Raises:
            PaymentError: If the ``stripe`` package is not installed.
        """
        try:
            import stripe  # type: ignore[import-untyped]
        except ImportError as exc:
            raise PaymentError(
                "stripe package not installed. "
                "Install it with: pip install stripe",
                code="MISSING_DEPENDENCY",
            ) from exc

        stripe.api_key = self._secret_key
        return stripe

    # -- Setup -----------------------------------------------------------------

    def create_setup_url(
        self, return_url: str = "https://kiln.dev/billing/done"
    ) -> str:
        """Create a URL the user can visit to save a payment method.

        If no ``customer_id`` was provided at construction time a new Stripe
        Customer is created automatically.  A SetupIntent with
        ``usage="off_session"`` is then created so future charges can happen
        without the user being present.

        Args:
            return_url: Where Stripe redirects the user after setup.

        Returns:
            The URL the user should open to complete card setup.

        Raises:
            PaymentError: On Stripe API errors.
        """
        stripe = self._import_stripe()

        try:
            # Ensure we have a customer
            if not self._customer_id:
                customer = stripe.Customer.create()
                self._customer_id = customer.id
                logger.info("Created Stripe customer %s", self._customer_id)

            # Create a SetupIntent for saving the card
            setup_intent = stripe.SetupIntent.create(
                customer=self._customer_id,
                usage="off_session",
                payment_method_types=["card"],
            )

            logger.info(
                "Created SetupIntent %s for customer %s",
                setup_intent.id,
                self._customer_id,
            )

            # Build the URL — Stripe Checkout or a hosted page.  For
            # simplicity we return the SetupIntent's client_secret in a
            # redirect-style URL that the frontend can consume.
            url = (
                f"https://checkout.stripe.com/setup/{setup_intent.client_secret}"
                f"?return_url={return_url}"
            )
            return url

        except stripe.error.StripeError as exc:
            raise PaymentError(
                f"Failed to create setup URL: {exc}",
                code="STRIPE_SETUP_ERROR",
            ) from exc

    # -- PaymentProvider methods -----------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Charge the saved card via a confirmed off-session PaymentIntent.

        Args:
            request: Payment parameters including amount and currency.

        Returns:
            Result with Stripe PaymentIntent ID and mapped status.

        Raises:
            PaymentError: On non-card Stripe errors.
        """
        stripe = self._import_stripe()

        if not self._customer_id or not self._payment_method_id:
            raise PaymentError(
                "Customer and payment method must be set before creating "
                "a payment. Call create_setup_url() first.",
                code="NO_PAYMENT_METHOD",
            )

        # Convert dollars/euros to cents
        amount_cents = int(round(request.amount * 100))

        metadata = dict(request.metadata) if request.metadata else {}
        metadata["job_id"] = request.job_id

        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=request.currency.value.lower(),
                customer=self._customer_id,
                payment_method=self._payment_method_id,
                off_session=True,
                confirm=True,
                description=request.description or f"Kiln job {request.job_id}",
                metadata=metadata,
            )

            status = _STATUS_MAP.get(intent.status, PaymentStatus.PENDING)
            logger.info(
                "PaymentIntent %s status=%s for job %s",
                intent.id,
                intent.status,
                request.job_id,
            )

            return PaymentResult(
                success=status == PaymentStatus.COMPLETED,
                payment_id=intent.id,
                status=status,
                amount=request.amount,
                currency=request.currency,
                rail=PaymentRail.STRIPE,
            )

        except stripe.error.CardError as exc:
            logger.warning("Card declined for job %s: %s", request.job_id, exc)
            return PaymentResult(
                success=False,
                payment_id=getattr(exc, "payment_intent", {}).get("id", "")
                if isinstance(getattr(exc, "payment_intent", None), dict)
                else "",
                status=PaymentStatus.FAILED,
                amount=request.amount,
                currency=request.currency,
                rail=PaymentRail.STRIPE,
                error=str(exc),
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                f"Stripe error creating payment: {exc}",
                code="STRIPE_ERROR",
            ) from exc

    def get_payment_status(self, payment_id: str) -> PaymentResult:
        """Retrieve the current status of a PaymentIntent.

        Args:
            payment_id: Stripe PaymentIntent ID (``pi_...``).

        Returns:
            Current payment state.

        Raises:
            PaymentError: If the intent cannot be retrieved.
        """
        stripe = self._import_stripe()

        try:
            intent = stripe.PaymentIntent.retrieve(payment_id)
            status = _STATUS_MAP.get(intent.status, PaymentStatus.PENDING)

            return PaymentResult(
                success=status == PaymentStatus.COMPLETED,
                payment_id=intent.id,
                status=status,
                amount=intent.amount / 100.0,
                currency=Currency(intent.currency.upper()),
                rail=PaymentRail.STRIPE,
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                f"Failed to retrieve payment {payment_id}: {exc}",
                code="STRIPE_RETRIEVE_ERROR",
            ) from exc

    def refund_payment(self, payment_id: str) -> PaymentResult:
        """Issue a full refund for a PaymentIntent.

        Args:
            payment_id: Stripe PaymentIntent ID to refund.

        Returns:
            Updated payment state with REFUNDED status on success.

        Raises:
            PaymentError: If the refund cannot be processed.
        """
        stripe = self._import_stripe()

        try:
            refund = stripe.Refund.create(payment_intent=payment_id)

            logger.info(
                "Refund %s created for PaymentIntent %s, status=%s",
                refund.id,
                payment_id,
                refund.status,
            )

            # Retrieve the intent to get amount/currency for the result
            intent = stripe.PaymentIntent.retrieve(payment_id)

            if refund.status == "succeeded":
                result_status = PaymentStatus.REFUNDED
                success = True
            elif refund.status == "pending":
                result_status = PaymentStatus.PROCESSING
                success = True
            else:
                result_status = PaymentStatus.FAILED
                success = False

            return PaymentResult(
                success=success,
                payment_id=payment_id,
                status=result_status,
                amount=intent.amount / 100.0,
                currency=Currency(intent.currency.upper()),
                rail=PaymentRail.STRIPE,
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                f"Failed to refund payment {payment_id}: {exc}",
                code="STRIPE_REFUND_ERROR",
            ) from exc

    def __repr__(self) -> str:
        key_hint = self._secret_key[:7] + "..." if self._secret_key else "unset"
        return (
            f"<StripeProvider key={key_hint!r} "
            f"customer={self._customer_id!r}>"
        )
