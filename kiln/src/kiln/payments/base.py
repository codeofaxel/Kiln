"""Abstract payment interface and data types.

Every payment provider (Stripe, Circle, on-chain crypto) implements the
:class:`PaymentProvider` interface so the rest of the Kiln stack can
charge for prints without knowing the underlying rail.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


class PaymentStatus(enum.Enum):
    """Lifecycle states for a payment."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
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
    CIRCLE = "circle"          # USDC via Circle APIs
    ETHEREUM = "ethereum"      # On-chain ETH/ERC-20
    BASE = "base"              # On-chain Base L2
    SOLANA = "solana"          # On-chain SOL/SPL


@dataclass
class PaymentRequest:
    """A request to charge for a print job."""

    amount: float
    currency: Currency
    rail: PaymentRail
    job_id: str
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
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
    tx_hash: Optional[str] = None  # blockchain transaction hash
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["currency"] = self.currency.value
        data["rail"] = self.rail.value
        return data


class PaymentError(Exception):
    """Base exception for payment-related errors."""

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
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
