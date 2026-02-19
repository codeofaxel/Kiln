"""Per-printer usage billing for Enterprise tier.

Enterprise base price includes 20 printers. Each additional active
printer is billed at $15/month. This module tracks printer counts
and calculates overage charges.

Usage::

    from kiln.printer_billing import PrinterUsageBilling

    billing = PrinterUsageBilling()
    summary = billing.usage_summary(active_printer_count=35)
    print(summary["overage_charge"])  # 225.00 (15 extra * $15)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Enterprise base includes 20 printers.
_ENTERPRISE_INCLUDED_PRINTERS: int = 20

# Per-printer overage rate (USD/month).
_OVERAGE_RATE_USD: float = 15.0


@dataclass
class PrinterUsage:
    """Snapshot of printer usage for billing purposes.

    Attributes:
        active_count: Number of currently active (registered) printers.
        included_count: Printers included in the plan at no extra cost.
        overage_count: Printers exceeding the included allowance.
        overage_rate: Per-printer monthly overage rate in USD.
        overage_charge: Total monthly overage charge in USD.
        timestamp: When this snapshot was taken.
    """

    active_count: int
    included_count: int
    overage_count: int
    overage_rate: float
    overage_charge: float
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_count": self.active_count,
            "included_count": self.included_count,
            "overage_count": self.overage_count,
            "overage_rate": self.overage_rate,
            "overage_charge": round(self.overage_charge, 2),
            "timestamp": self.timestamp,
        }


class PrinterUsageBilling:
    """Calculates per-printer overage charges for Enterprise accounts.

    The included printer count and overage rate can be overridden via
    constructor for testing or custom agreements.
    """

    def __init__(
        self,
        *,
        included_printers: int = _ENTERPRISE_INCLUDED_PRINTERS,
        overage_rate: float = _OVERAGE_RATE_USD,
    ) -> None:
        self._included = included_printers
        self._rate = overage_rate

    @property
    def included_printers(self) -> int:
        return self._included

    @property
    def overage_rate(self) -> float:
        return self._rate

    def usage_summary(self, active_printer_count: int) -> PrinterUsage:
        """Calculate printer usage and overage for the current period.

        Args:
            active_printer_count: Number of active printers right now.

        Returns:
            A :class:`PrinterUsage` snapshot.
        """
        overage = max(0, active_printer_count - self._included)
        charge = round(overage * self._rate, 2)

        return PrinterUsage(
            active_count=active_printer_count,
            included_count=self._included,
            overage_count=overage,
            overage_rate=self._rate,
            overage_charge=charge,
            timestamp=time.time(),
        )

    def estimate_monthly_cost(
        self,
        active_printer_count: int,
        *,
        base_price: float = 499.0,
    ) -> dict[str, Any]:
        """Estimate total monthly Enterprise cost including overage.

        Args:
            active_printer_count: Number of active printers.
            base_price: Enterprise base subscription price.

        Returns:
            Cost breakdown dict.
        """
        usage = self.usage_summary(active_printer_count)
        total = round(base_price + usage.overage_charge, 2)

        return {
            "base_price": base_price,
            "included_printers": self._included,
            "active_printers": active_printer_count,
            "overage_printers": usage.overage_count,
            "overage_rate": self._rate,
            "overage_charge": usage.overage_charge,
            "total_monthly": total,
        }
