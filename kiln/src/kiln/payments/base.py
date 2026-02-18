"""Abstract payment interface and data types.

Every payment provider (Stripe, Circle, on-chain crypto) implements the
:class:`PaymentProvider` interface so the rest of the Kiln stack can
charge for prints without knowing the underlying rail.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


class PaymentStatus(enum.Enum):
    """Lifecycle states for a payment."""

    PENDING = "pending"
    PROCESSING = "processing"
    AUTHORIZED = "authorized"  # funds held, not yet captured
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Currency(enum.Enum):
    """Supported currencies."""

    # Fiat
    USD = "USD"
    EUR = "EUR"

    # Stablecoins
    USDC = "USDC"
    USDT = "USDT"

    # Native crypto
    ETH = "ETH"
    SOL = "SOL"


class PaymentRail(enum.Enum):
    """Payment network / rail."""

    STRIPE = "stripe"
    CIRCLE = "circle"  # USDC via Circle APIs
    ETHEREUM = "ethereum"  # On-chain ETH/ERC-20
    BASE = "base"  # On-chain Base L2
    SOLANA = "solana"  # On-chain SOL/SPL


@dataclass
class PaymentRequest:
    """A request to charge for a print job."""

    amount: float
    currency: Currency
    rail: PaymentRail
    job_id: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["currency"] = self.currency.value
        data["rail"] = self.rail.value
        return data


@dataclass
class PaymentResult:
    """Outcome of a payment operation."""

    success: bool
    payment_id: str
    status: PaymentStatus
    amount: float
    currency: Currency
    rail: PaymentRail
    tx_hash: str | None = None  # blockchain transaction hash
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["currency"] = self.currency.value
        data["rail"] = self.rail.value
        return data


BILLING_SUPPORT_SUFFIX = (
    " For help, run 'billing_status' to check your account, or open an issue at https://github.com/Kiln3D/kiln/issues."
)


class PaymentError(Exception):
    """Base exception for payment-related errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PaymentProvider(ABC):
    """Abstract base for payment providers.

    Concrete implementations handle the specifics of each payment rail
    (Stripe API calls, blockchain transactions, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (e.g. ``"stripe"``, ``"circle"``)."""

    @property
    @abstractmethod
    def supported_currencies(self) -> list[Currency]:
        """Currencies this provider can process."""

    @property
    @abstractmethod
    def rail(self) -> PaymentRail:
        """The payment rail this provider operates on."""

    @abstractmethod
    def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Initiate a payment.

        Args:
            request: The payment parameters.

        Returns:
            Result with payment ID and initial status.

        Raises:
            PaymentError: If the payment cannot be initiated.
        """

    @abstractmethod
    def get_payment_status(self, payment_id: str) -> PaymentResult:
        """Check the status of an existing payment.

        Args:
            payment_id: The ID returned by :meth:`create_payment`.

        Returns:
            Current payment state.

        Raises:
            PaymentError: If the payment cannot be found or queried.
        """

    @abstractmethod
    def refund_payment(self, payment_id: str) -> PaymentResult:
        """Refund a completed payment.

        Args:
            payment_id: The ID of the payment to refund.

        Returns:
            Updated payment state.

        Raises:
            PaymentError: If the refund cannot be processed.
        """

    # -- Optional auth-and-capture methods ---------------------------------
    # These allow a two-phase flow: authorize (hold funds) at quote time,
    # capture (collect) at order time, or cancel if the user backs out.
    # Default implementations raise NotImplementedError so providers that
    # don't support holds still work with the one-shot create_payment flow.

    def authorize_payment(self, request: PaymentRequest) -> PaymentResult:
        """Place a hold on funds without capturing.

        Providers that support this (e.g. Stripe with ``capture_method:
        manual``) return a result with ``AUTHORIZED`` status.  The hold
        must be captured via :meth:`capture_payment` or released via
        :meth:`cancel_payment`.

        Args:
            request: Payment parameters.

        Returns:
            Result with ``AUTHORIZED`` status on success.

        Raises:
            PaymentError: If the hold cannot be placed.
            NotImplementedError: If the provider doesn't support holds.
        """
        raise NotImplementedError(f"{self.name} does not support auth-and-capture.")

    def capture_payment(self, payment_id: str) -> PaymentResult:
        """Capture a previously authorized payment.

        Args:
            payment_id: ID from :meth:`authorize_payment`.

        Returns:
            Result with ``COMPLETED`` status on success.

        Raises:
            PaymentError: If capture fails.
            NotImplementedError: If the provider doesn't support holds.
        """
        raise NotImplementedError(f"{self.name} does not support auth-and-capture.")

    def cancel_payment(self, payment_id: str) -> PaymentResult:
        """Release a previously authorized hold without charging.

        Args:
            payment_id: ID from :meth:`authorize_payment`.

        Returns:
            Result with ``CANCELLED`` status.

        Raises:
            PaymentError: If cancellation fails.
            NotImplementedError: If the provider doesn't support holds.
        """
        raise NotImplementedError(f"{self.name} does not support auth-and-capture.")
