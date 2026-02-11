"""Payment abstraction layer for Kiln.

Supports fiat (Stripe) and crypto (USDC on Solana/Base via Circle) rails.

Public API::

    from kiln.payments import (
        PaymentManager,
        PaymentProvider,
        PaymentRequest,
        PaymentResult,
        PaymentStatus,
        PaymentRail,
        PaymentError,
        Currency,
    )
"""

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)
from kiln.payments.manager import PaymentManager

__all__ = [
    "Currency",
    "PaymentError",
    "PaymentManager",
    "PaymentProvider",
    "PaymentRail",
    "PaymentRequest",
    "PaymentResult",
    "PaymentStatus",
]
