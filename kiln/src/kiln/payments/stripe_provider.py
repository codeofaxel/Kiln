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
    "requires_capture": PaymentStatus.AUTHORIZED,
    "requires_payment_method": PaymentStatus.FAILED,
    "requires_action": PaymentStatus.FAILED,
    "canceled": PaymentStatus.CANCELLED,
}

# Map Stripe decline codes to actionable user-facing messages.
_DECLINE_MESSAGES: Dict[str, str] = {
    "insufficient_funds": "Insufficient funds. Please use a different card or add funds.",
    "lost_card": "Card reported lost. Please use a different payment method.",
    "stolen_card": "Card reported stolen. Please use a different payment method.",
    "expired_card": "Card has expired. Update your payment method with 'billing_setup_url'.",
    "incorrect_cvc": "Incorrect CVC code. Please retry with the correct CVC.",
    "card_declined": (
        "Card was declined by your bank. Try a different card or contact your bank."
    ),
    "processing_error": "Card processor error. Please try again in a few minutes.",
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
        self._pending_setup_intent_id: Optional[str] = None

    def set_payment_method(self, payment_method_id: str) -> None:
        """Update the default payment method for future charges."""
        self._payment_method_id = payment_method_id

    def poll_setup_intent(
        self, setup_intent_id: Optional[str] = None
    ) -> Optional[str]:
        """Check if a SetupIntent has completed and return the payment_method_id.

        Args:
            setup_intent_id: Specific SetupIntent to check.  Falls back to
                the most recently created one from :meth:`create_setup_url`.

        Returns:
            The ``pm_...`` payment method ID if setup succeeded, else ``None``.
        """
        sid = setup_intent_id or self._pending_setup_intent_id
        if not sid:
            return None
        stripe = self._import_stripe()
        try:
            si = stripe.SetupIntent.retrieve(sid)
            if si.status == "succeeded" and si.payment_method:
                return si.payment_method
            return None
        except Exception:
            return None

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

            self._pending_setup_intent_id = setup_intent.id

            logger.info(
                "Created SetupIntent %s for customer %s",
                setup_intent.id,
                self._customer_id,
            )

            # Build the URL -- Stripe Checkout or a hosted page.  For
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
            decline_code = (
                exc.error.decline_code
                if hasattr(exc, "error")
                and hasattr(exc.error, "decline_code")
                else "unknown"
            )
            message = _DECLINE_MESSAGES.get(
                decline_code,
                f"Card was declined (code: {decline_code}). "
                "Please try a different payment method or contact your bank.",
            )
            logger.warning(
                "Card declined for job %s (code=%s): %s",
                request.job_id, decline_code, exc,
            )
            return PaymentResult(
                success=False,
                payment_id=getattr(exc, "payment_intent", {}).get("id", "")
                if isinstance(getattr(exc, "payment_intent", None), dict)
                else "",
                status=PaymentStatus.FAILED,
                amount=request.amount,
                currency=request.currency,
                rail=PaymentRail.STRIPE,
                error=message,
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                "Payment processing error. This is usually temporary — "
                "please try again in 1-2 minutes. If the problem persists, "
                "check your payment method with 'billing_status'.",
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

    # -- Auth-and-capture methods -----------------------------------------------

    def authorize_payment(self, request: PaymentRequest) -> PaymentResult:
        """Create a PaymentIntent with ``capture_method: manual``.

        Places a hold on the card for the fee amount.  The hold expires
        after 7 days if not captured.  Use :meth:`capture_payment` to
        collect or :meth:`cancel_payment` to release.

        Args:
            request: Payment parameters including amount and currency.

        Returns:
            Result with ``AUTHORIZED`` status and the PaymentIntent ID.

        Raises:
            PaymentError: On Stripe API errors.
        """
        stripe = self._import_stripe()

        if not self._customer_id or not self._payment_method_id:
            raise PaymentError(
                "Customer and payment method must be set before authorizing. "
                "Call create_setup_url() first.",
                code="NO_PAYMENT_METHOD",
            )

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
                capture_method="manual",
                description=request.description or f"Kiln fee hold for {request.job_id}",
                metadata=metadata,
            )

            status = _STATUS_MAP.get(intent.status, PaymentStatus.PENDING)
            logger.info(
                "Authorized PaymentIntent %s status=%s for job %s",
                intent.id, intent.status, request.job_id,
            )

            return PaymentResult(
                success=status == PaymentStatus.AUTHORIZED,
                payment_id=intent.id,
                status=status,
                amount=request.amount,
                currency=request.currency,
                rail=PaymentRail.STRIPE,
            )

        except stripe.error.CardError as exc:
            decline_code = (
                exc.error.decline_code
                if hasattr(exc, "error")
                and hasattr(exc.error, "decline_code")
                else "unknown"
            )
            message = _DECLINE_MESSAGES.get(
                decline_code,
                f"Card was declined (code: {decline_code}). "
                "Please try a different payment method or contact your bank.",
            )
            logger.warning(
                "Card declined during auth for job %s (code=%s): %s",
                request.job_id, decline_code, exc,
            )
            return PaymentResult(
                success=False,
                payment_id="",
                status=PaymentStatus.FAILED,
                amount=request.amount,
                currency=request.currency,
                rail=PaymentRail.STRIPE,
                error=message,
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                "Payment processing error. This is usually temporary — "
                "please try again in 1-2 minutes. If the problem persists, "
                "check your payment method with 'billing_status'.",
                code="STRIPE_AUTH_ERROR",
            ) from exc

    def capture_payment(self, payment_id: str) -> PaymentResult:
        """Capture a previously authorized PaymentIntent.

        Args:
            payment_id: Stripe PaymentIntent ID (``pi_...``) from
                :meth:`authorize_payment`.

        Returns:
            Result with ``COMPLETED`` status on success.

        Raises:
            PaymentError: If the intent cannot be captured (e.g. already
                captured, expired, or cancelled).
        """
        stripe = self._import_stripe()

        try:
            intent = stripe.PaymentIntent.capture(payment_id)
            status = _STATUS_MAP.get(intent.status, PaymentStatus.PENDING)
            logger.info(
                "Captured PaymentIntent %s status=%s",
                intent.id, intent.status,
            )

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
                f"Failed to capture payment {payment_id}: {exc}",
                code="STRIPE_CAPTURE_ERROR",
            ) from exc

    def cancel_payment(self, payment_id: str) -> PaymentResult:
        """Cancel a previously authorized PaymentIntent (release hold).

        Args:
            payment_id: Stripe PaymentIntent ID to cancel.

        Returns:
            Result with ``CANCELLED`` status.

        Raises:
            PaymentError: If the intent cannot be cancelled.
        """
        stripe = self._import_stripe()

        try:
            intent = stripe.PaymentIntent.cancel(payment_id)
            logger.info(
                "Cancelled PaymentIntent %s status=%s",
                intent.id, intent.status,
            )

            return PaymentResult(
                success=True,
                payment_id=intent.id,
                status=PaymentStatus.CANCELLED,
                amount=intent.amount / 100.0,
                currency=Currency(intent.currency.upper()),
                rail=PaymentRail.STRIPE,
            )

        except stripe.error.StripeError as exc:
            raise PaymentError(
                f"Failed to cancel payment {payment_id}: {exc}",
                code="STRIPE_CANCEL_ERROR",
            ) from exc

    def __repr__(self) -> str:
        key_hint = self._secret_key[:7] + "..." if self._secret_key else "unset"
        return (
            f"<StripeProvider key={key_hint!r} "
            f"customer={self._customer_id!r}>"
        )
