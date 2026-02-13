"""Invoice and receipt generation for Kiln billing charges.

Generates structured invoice data for each billing charge.  Invoices
can be retrieved as JSON dicts or formatted as plain-text receipts.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Invoice number prefix.
_INVOICE_PREFIX = "KLN"


@dataclass
class Invoice:
    """A billing invoice/receipt for a single charge."""

    invoice_number: str
    charge_id: str
    job_id: str
    order_id: str
    issued_at: float
    # Amounts
    job_cost: float
    fee_amount: float
    fee_percent: float
    total_cost: float
    currency: str
    # Payment details
    payment_id: Optional[str]
    payment_rail: Optional[str]
    payment_status: str
    # Waiver info
    waived: bool
    waiver_reason: Optional[str]
    # Integrity
    checksum: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invoice_number": self.invoice_number,
            "charge_id": self.charge_id,
            "job_id": self.job_id,
            "order_id": self.order_id,
            "issued_at": self.issued_at,
            "issued_at_human": datetime.fromtimestamp(
                self.issued_at, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC"),
            "job_cost": self.job_cost,
            "fee_amount": self.fee_amount,
            "fee_percent": self.fee_percent,
            "total_cost": self.total_cost,
            "currency": self.currency,
            "payment_id": self.payment_id,
            "payment_rail": self.payment_rail,
            "payment_status": self.payment_status,
            "waived": self.waived,
            "waiver_reason": self.waiver_reason,
            "checksum": self.checksum,
        }

    def to_receipt_text(self) -> str:
        """Format as a human-readable plain-text receipt."""
        lines = [
            "=" * 50,
            "KILN PLATFORM FEE RECEIPT",
            "=" * 50,
            f"Invoice:    {self.invoice_number}",
            f"Date:       {datetime.fromtimestamp(self.issued_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Order:      {self.order_id}",
            "-" * 50,
        ]
        if self.waived:
            lines.append(f"Status:     WAIVED ({self.waiver_reason})")
            lines.append(f"Amount:     $0.00 {self.currency}")
        else:
            lines.append(f"Mfg cost:   ${self.job_cost:.2f} {self.currency}")
            lines.append(f"Kiln fee:   ${self.fee_amount:.2f} ({self.fee_percent}%)")
            lines.append(f"Total:      ${self.total_cost:.2f} {self.currency}")
        lines.append("-" * 50)
        if self.payment_id:
            lines.append(f"Payment:    {self.payment_rail or 'unknown'} ({self.payment_status})")
            lines.append(f"Ref:        {self.payment_id}")
        lines.extend([
            "-" * 50,
            f"Checksum:   {self.checksum[:16]}...",
            "=" * 50,
        ])
        return "\n".join(lines)


def _generate_invoice_number(charge_id: str, timestamp: float) -> str:
    """Generate a deterministic, sequential-looking invoice number."""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    date_part = dt.strftime("%Y%m")
    # Use first 6 chars of charge_id for uniqueness.
    return f"{_INVOICE_PREFIX}-{date_part}-{charge_id[:6].upper()}"


def _compute_checksum(data: Dict[str, Any]) -> str:
    """Compute SHA-256 checksum of invoice data for tamper detection."""
    content = (
        f"{data.get('charge_id', '')}:"
        f"{data.get('job_id', '')}:"
        f"{data.get('fee_amount', 0)}:"
        f"{data.get('total_cost', 0)}:"
        f"{data.get('created_at', 0)}"
    )
    return hashlib.sha256(content.encode()).hexdigest()


def generate_invoice(charge: Dict[str, Any]) -> Invoice:
    """Generate an invoice from a billing charge record.

    :param charge: A charge dict from ``BillingLedger.list_charges()``
        or ``KilnDB.list_billing_charges()``.
    :returns: An :class:`Invoice` instance.
    """
    charge_id = charge.get("id", "unknown")
    timestamp = charge.get("created_at", time.time())

    return Invoice(
        invoice_number=_generate_invoice_number(charge_id, timestamp),
        charge_id=charge_id,
        job_id=charge.get("job_id", ""),
        order_id=charge.get("order_id", charge.get("job_id", "")),
        issued_at=timestamp,
        job_cost=charge.get("job_cost", 0.0),
        fee_amount=charge.get("fee_amount", 0.0),
        fee_percent=charge.get("fee_percent", 0.0),
        total_cost=charge.get("total_cost", 0.0),
        currency=charge.get("currency", "USD"),
        payment_id=charge.get("payment_id"),
        payment_rail=charge.get("payment_rail"),
        payment_status=charge.get("payment_status", "unknown"),
        waived=bool(charge.get("waived", False)),
        waiver_reason=charge.get("waiver_reason"),
        checksum=_compute_checksum(charge),
    )


def generate_invoices(
    charges: List[Dict[str, Any]],
) -> List[Invoice]:
    """Generate invoices for a list of billing charges."""
    return [generate_invoice(c) for c in charges]


def export_billing_csv(charges: List[Dict[str, Any]]) -> str:
    """Export billing charges as CSV for accounting.

    :returns: CSV string with headers.
    """
    headers = [
        "invoice_number", "date", "order_id", "job_cost",
        "fee_amount", "fee_percent", "total_cost", "currency",
        "payment_rail", "payment_status", "waived",
    ]
    lines = [",".join(headers)]
    for charge in charges:
        inv = generate_invoice(charge)
        dt = datetime.fromtimestamp(inv.issued_at, tz=timezone.utc)
        row = [
            inv.invoice_number,
            dt.strftime("%Y-%m-%d"),
            inv.order_id,
            f"{inv.job_cost:.2f}",
            f"{inv.fee_amount:.2f}",
            f"{inv.fee_percent:.2f}",
            f"{inv.total_cost:.2f}",
            inv.currency,
            inv.payment_rail or "",
            inv.payment_status,
            str(inv.waived).lower(),
        ]
        lines.append(",".join(row))
    return "\n".join(lines)
