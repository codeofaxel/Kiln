"""Tax calculation for Kiln platform fees.

Computes applicable sales tax, VAT, or GST on Kiln's platform fee based
on the buyer's jurisdiction.  Tax is applied to the **fee amount only**,
not the underlying manufacturing cost (that's the provider's obligation).

Supported jurisdictions (22):
  - US: CA, TX, NY, WA, FL, IL, MA, CO  (sales tax on platform/SaaS fees)
  - EU: DE, FR, NL, IT, ES, SE, PL       (VAT on digital services)
  - UK: GB                                (VAT on digital services)
  - Canada: CA-ON, CA-BC, CA-QC, CA-AB   (GST/HST/PST)
  - Australia: AU                         (GST on digital services)
  - Japan: JP                             (JCT on digital services)

B2B reverse-charge rules:
  EU, UK, AU, JP — when the buyer provides a valid business tax ID,
  the reverse-charge mechanism applies and Kiln does **not** collect tax.
  The buyer self-assesses on their own return.

Usage::

    from kiln.tax import TaxCalculator, TaxResult

    calc = TaxCalculator()
    result = calc.calculate_tax(fee_amount=6.00, jurisdiction="DE")
    print(result.tax_amount, result.effective_rate)

    # B2B with valid VAT ID — reverse charge, no tax collected
    result = calc.calculate_tax(fee_amount=6.00, jurisdiction="DE",
                                business_tax_id="DE123456789")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaxType(Enum):
    """Type of tax applied."""
    SALES_TAX = "sales_tax"
    VAT = "vat"
    GST = "gst"
    HST = "hst"
    JCT = "jct"
    NONE = "none"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TaxJurisdiction:
    """Tax rules for a single jurisdiction.

    Attributes:
        code: Jurisdiction code (e.g. ``"US-CA"``, ``"DE"``, ``"AU"``).
        name: Human-readable name.
        tax_type: Type of tax applied.
        rate: Standard tax rate as a decimal (e.g. 0.19 for 19%).
        platform_fee_taxable: Whether platform/SaaS fees are taxable here.
        b2b_reverse_charge: Whether B2B reverse charge applies (buyer self-assesses).
        notes: Implementation notes.
    """
    code: str
    name: str
    tax_type: TaxType
    rate: float
    platform_fee_taxable: bool = True
    b2b_reverse_charge: bool = False
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "tax_type": self.tax_type.value,
            "rate": self.rate,
            "rate_percent": round(self.rate * 100, 2),
            "platform_fee_taxable": self.platform_fee_taxable,
            "b2b_reverse_charge": self.b2b_reverse_charge,
            "notes": self.notes,
        }


@dataclass
class TaxResult:
    """Result of a tax calculation.

    Attributes:
        jurisdiction_code: Jurisdiction code used for calculation.
        tax_type: Type of tax applied.
        tax_amount: Computed tax in the order's currency.
        effective_rate: Actual rate applied (may be 0 if exempt).
        taxable_amount: The amount tax was calculated on (the fee).
        exempt: True if tax was waived (B2B reverse charge, non-taxable, etc.).
        exempt_reason: Human-readable explanation when ``exempt`` is True.
        reverse_charge: True if B2B reverse charge applies.
        business_tax_id: Buyer's tax ID if provided.
    """
    jurisdiction_code: str
    tax_type: TaxType
    tax_amount: float
    effective_rate: float
    taxable_amount: float
    exempt: bool = False
    exempt_reason: Optional[str] = None
    reverse_charge: bool = False
    business_tax_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "jurisdiction_code": self.jurisdiction_code,
            "tax_type": self.tax_type.value,
            "tax_amount": self.tax_amount,
            "effective_rate": self.effective_rate,
            "effective_rate_percent": round(self.effective_rate * 100, 2),
            "taxable_amount": self.taxable_amount,
            "exempt": self.exempt,
            "exempt_reason": self.exempt_reason,
            "reverse_charge": self.reverse_charge,
            "business_tax_id": self.business_tax_id,
        }


# ---------------------------------------------------------------------------
# Jurisdiction database
# ---------------------------------------------------------------------------

# Rates are base/standard rates.  For US states with variable local rates,
# we use the **base state rate** — a production system should integrate with
# a tax API (Avalara, Stripe Tax) for precise local rates.

_JURISDICTIONS: Dict[str, TaxJurisdiction] = {}


def _register(*jurisdictions: TaxJurisdiction) -> None:
    for j in jurisdictions:
        _JURISDICTIONS[j.code] = j


# -- United States (sales tax on platform fees) ----------------------------
_register(
    TaxJurisdiction(
        code="US-CA", name="California", tax_type=TaxType.SALES_TAX,
        rate=0.0725,
        platform_fee_taxable=True,
        notes="Base rate 7.25%; local surcharges up to ~10.25%. "
              "SaaS alone not taxable, but marketplace facilitator rules apply.",
    ),
    TaxJurisdiction(
        code="US-TX", name="Texas", tax_type=TaxType.SALES_TAX,
        rate=0.0625,
        platform_fee_taxable=True,
        notes="Base rate 6.25%; local up to 8.25%. "
              "SaaS and data processing services are taxable.",
    ),
    TaxJurisdiction(
        code="US-NY", name="New York", tax_type=TaxType.SALES_TAX,
        rate=0.04,
        platform_fee_taxable=True,
        notes="Base rate 4%; local up to 8.875%. "
              "SaaS taxable as pre-written software.",
    ),
    TaxJurisdiction(
        code="US-WA", name="Washington", tax_type=TaxType.SALES_TAX,
        rate=0.065,
        platform_fee_taxable=True,
        notes="Base rate 6.5%; local up to 10.25%. "
              "Digital services explicitly taxable since 2010.",
    ),
    TaxJurisdiction(
        code="US-FL", name="Florida", tax_type=TaxType.SALES_TAX,
        rate=0.06,
        platform_fee_taxable=True,
        notes="Base rate 6%; local up to 8.5%. "
              "SaaS taxable as of 2024.",
    ),
    TaxJurisdiction(
        code="US-IL", name="Illinois", tax_type=TaxType.SALES_TAX,
        rate=0.0625,
        platform_fee_taxable=True,
        notes="Base rate 6.25%; local up to 10.25%. "
              "State: SaaS generally not taxable, but marketplace facilitator rules apply. "
              "Chicago imposes 9% 'cloud tax'.",
    ),
    TaxJurisdiction(
        code="US-MA", name="Massachusetts", tax_type=TaxType.SALES_TAX,
        rate=0.0625,
        platform_fee_taxable=True,
        notes="Flat 6.25%. SaaS explicitly taxable since 2019.",
    ),
    TaxJurisdiction(
        code="US-CO", name="Colorado", tax_type=TaxType.SALES_TAX,
        rate=0.029,
        platform_fee_taxable=False,
        notes="Base rate 2.9%; local up to ~11.2%. "
              "State-level: SaaS generally NOT taxable, but some home-rule cities tax it.",
    ),
)

# -- European Union (VAT on digital services) ------------------------------
_register(
    TaxJurisdiction(
        code="DE", name="Germany", tax_type=TaxType.VAT,
        rate=0.19, b2b_reverse_charge=True,
        notes="19% standard VAT. B2B reverse charge for cross-border EU.",
    ),
    TaxJurisdiction(
        code="FR", name="France", tax_type=TaxType.VAT,
        rate=0.20, b2b_reverse_charge=True,
        notes="20% standard VAT. B2B reverse charge. "
              "DST of 3% on marketplace revenue above EUR 25M (not applied here).",
    ),
    TaxJurisdiction(
        code="NL", name="Netherlands", tax_type=TaxType.VAT,
        rate=0.21, b2b_reverse_charge=True,
        notes="21% standard VAT. B2B reverse charge.",
    ),
    TaxJurisdiction(
        code="IT", name="Italy", tax_type=TaxType.VAT,
        rate=0.22, b2b_reverse_charge=True,
        notes="22% standard VAT. B2B reverse charge.",
    ),
    TaxJurisdiction(
        code="ES", name="Spain", tax_type=TaxType.VAT,
        rate=0.21, b2b_reverse_charge=True,
        notes="21% standard VAT. B2B reverse charge.",
    ),
    TaxJurisdiction(
        code="SE", name="Sweden", tax_type=TaxType.VAT,
        rate=0.25, b2b_reverse_charge=True,
        notes="25% standard VAT. B2B reverse charge.",
    ),
    TaxJurisdiction(
        code="PL", name="Poland", tax_type=TaxType.VAT,
        rate=0.23, b2b_reverse_charge=True,
        notes="23% standard VAT. B2B reverse charge.",
    ),
)

# -- United Kingdom --------------------------------------------------------
_register(
    TaxJurisdiction(
        code="GB", name="United Kingdom", tax_type=TaxType.VAT,
        rate=0.20, b2b_reverse_charge=True,
        notes="20% standard VAT. B2B reverse charge for non-UK sellers. "
              "No registration threshold for non-UK digital service providers.",
    ),
)

# -- Canada ----------------------------------------------------------------
_register(
    TaxJurisdiction(
        code="CA-ON", name="Ontario", tax_type=TaxType.HST,
        rate=0.13,
        notes="13% HST (combined federal GST + provincial). "
              "Digital services taxable.",
    ),
    TaxJurisdiction(
        code="CA-BC", name="British Columbia", tax_type=TaxType.GST,
        rate=0.12,
        notes="5% GST + 7% PST = 12% combined. "
              "PST on software/digital services since April 2021.",
    ),
    TaxJurisdiction(
        code="CA-QC", name="Quebec", tax_type=TaxType.GST,
        rate=0.14975,
        notes="5% GST + 9.975% QST ≈ 15% combined. "
              "QST on digital services since Jan 2019.",
    ),
    TaxJurisdiction(
        code="CA-AB", name="Alberta", tax_type=TaxType.GST,
        rate=0.05,
        notes="5% GST only. No provincial sales tax.",
    ),
)

# -- Australia -------------------------------------------------------------
_register(
    TaxJurisdiction(
        code="AU", name="Australia", tax_type=TaxType.GST,
        rate=0.10, b2b_reverse_charge=True,
        notes="10% GST. B2B reverse charge when buyer is GST-registered. "
              "Non-resident suppliers must register above AUD 75K threshold.",
    ),
)

# -- Japan -----------------------------------------------------------------
_register(
    TaxJurisdiction(
        code="JP", name="Japan", tax_type=TaxType.JCT,
        rate=0.10, b2b_reverse_charge=True,
        notes="10% JCT (consumption tax). B2B reverse charge applies. "
              "Qualified invoice system since Oct 2023.",
    ),
)


# ---------------------------------------------------------------------------
# Tax ID validation (basic format checks)
# ---------------------------------------------------------------------------

# Pattern: prefix + digits/alphanumeric.  These are format-only checks;
# real validation requires VIES (EU), ABN lookup (AU), etc.
_TAX_ID_PATTERNS: Dict[str, re.Pattern[str]] = {
    # EU VAT IDs: 2-letter country code + 2-12 alphanumeric chars
    "DE": re.compile(r"^DE\d{9}$"),
    "FR": re.compile(r"^FR[A-Z0-9]{2}\d{9}$"),
    "NL": re.compile(r"^NL\d{9}B\d{2}$"),
    "IT": re.compile(r"^IT\d{11}$"),
    "ES": re.compile(r"^ES[A-Z0-9]\d{7}[A-Z0-9]$"),
    "SE": re.compile(r"^SE\d{12}$"),
    "PL": re.compile(r"^PL\d{10}$"),
    "GB": re.compile(r"^GB\d{9,12}$"),
    "AU": re.compile(r"^\d{11}$"),  # ABN: 11 digits
    "JP": re.compile(r"^T\d{13}$"),  # JCT registration number
}


def _validate_tax_id_format(jurisdiction_code: str, tax_id: str) -> bool:
    """Basic format validation for a business tax ID.

    Returns True if the tax ID matches the expected pattern for the
    jurisdiction, or True if no pattern is registered (unknown format).
    """
    pattern = _TAX_ID_PATTERNS.get(jurisdiction_code)
    if pattern is None:
        # No pattern for this jurisdiction — accept any non-empty string.
        return bool(tax_id.strip())
    return bool(pattern.match(tax_id.strip()))


# ---------------------------------------------------------------------------
# Tax calculator
# ---------------------------------------------------------------------------

class TaxCalculator:
    """Stateless tax calculator for Kiln platform fees.

    Computes tax on the **fee amount** based on the buyer's jurisdiction
    and optional business tax ID (for B2B reverse charge).
    """

    def calculate_tax(
        self,
        fee_amount: float,
        jurisdiction: str,
        *,
        business_tax_id: Optional[str] = None,
    ) -> TaxResult:
        """Calculate tax on a Kiln platform fee.

        Args:
            fee_amount: The Kiln platform fee amount (not the manufacturing cost).
            jurisdiction: Jurisdiction code (e.g. ``"US-CA"``, ``"DE"``, ``"AU"``).
            business_tax_id: Buyer's business tax ID for B2B reverse charge.

        Returns:
            A :class:`TaxResult` with the tax calculation.
        """
        jurisdiction = jurisdiction.upper().strip()

        # Unknown jurisdiction — no tax.
        jur = _JURISDICTIONS.get(jurisdiction)
        if jur is None:
            logger.debug("Unknown tax jurisdiction %r — no tax applied", jurisdiction)
            return TaxResult(
                jurisdiction_code=jurisdiction,
                tax_type=TaxType.NONE,
                tax_amount=0.0,
                effective_rate=0.0,
                taxable_amount=fee_amount,
                exempt=True,
                exempt_reason=f"Unknown jurisdiction: {jurisdiction}",
            )

        # Zero or negative fee — no tax.
        if fee_amount <= 0:
            return TaxResult(
                jurisdiction_code=jurisdiction,
                tax_type=jur.tax_type,
                tax_amount=0.0,
                effective_rate=0.0,
                taxable_amount=fee_amount,
                exempt=True,
                exempt_reason="No fee to tax (fee is zero or waived)",
            )

        # Not taxable in this jurisdiction.
        if not jur.platform_fee_taxable:
            return TaxResult(
                jurisdiction_code=jurisdiction,
                tax_type=jur.tax_type,
                tax_amount=0.0,
                effective_rate=0.0,
                taxable_amount=fee_amount,
                exempt=True,
                exempt_reason=f"Platform fees not taxable in {jur.name}",
            )

        # B2B reverse charge.
        if business_tax_id and jur.b2b_reverse_charge:
            valid_format = _validate_tax_id_format(jurisdiction, business_tax_id)
            if valid_format:
                return TaxResult(
                    jurisdiction_code=jurisdiction,
                    tax_type=jur.tax_type,
                    tax_amount=0.0,
                    effective_rate=0.0,
                    taxable_amount=fee_amount,
                    exempt=True,
                    exempt_reason=(
                        f"B2B reverse charge — buyer self-assesses "
                        f"{jur.tax_type.value.upper()} in {jur.name}"
                    ),
                    reverse_charge=True,
                    business_tax_id=business_tax_id,
                )
            else:
                logger.warning(
                    "Invalid tax ID format %r for %s — charging tax normally",
                    business_tax_id, jurisdiction,
                )

        # Standard tax calculation.
        tax = round(fee_amount * jur.rate, 2)

        return TaxResult(
            jurisdiction_code=jurisdiction,
            tax_type=jur.tax_type,
            tax_amount=tax,
            effective_rate=jur.rate,
            taxable_amount=fee_amount,
            business_tax_id=business_tax_id,
        )

    def estimate_tax(
        self,
        fee_amount: float,
        jurisdiction: str,
        *,
        business_tax_id: Optional[str] = None,
    ) -> TaxResult:
        """Alias for :meth:`calculate_tax` — same logic, clearer intent for previews."""
        return self.calculate_tax(
            fee_amount, jurisdiction, business_tax_id=business_tax_id,
        )

    def get_jurisdiction(self, code: str) -> Optional[TaxJurisdiction]:
        """Look up a jurisdiction by code."""
        return _JURISDICTIONS.get(code.upper().strip())

    def list_jurisdictions(self) -> List[TaxJurisdiction]:
        """Return all supported jurisdictions."""
        return list(_JURISDICTIONS.values())

    def list_jurisdiction_codes(self) -> List[str]:
        """Return all supported jurisdiction codes."""
        return list(_JURISDICTIONS.keys())
