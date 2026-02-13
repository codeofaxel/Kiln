"""Tests for the tax calculation system.

Covers:
- TaxCalculator: standard tax rates, zero/negative fees, unknown jurisdictions
- B2B reverse charge: valid/invalid tax IDs, US no-reverse-charge
- Tax ID format validation
- Jurisdiction database completeness
- TaxResult serialisation
- BillingLedger tax integration
"""

from __future__ import annotations

import pytest

from kiln.billing import BillingLedger, FeeCalculation, FeePolicy
from kiln.tax import (
    TaxCalculator,
    TaxJurisdiction,
    TaxResult,
    TaxType,
    _TAX_ID_PATTERNS,
    _validate_tax_id_format,
)


# ---------------------------------------------------------------------------
# TestTaxCalculator — core calculation
# ---------------------------------------------------------------------------


class TestTaxCalculator:
    """Core tax calculation: standard rates, edge cases, exemptions."""

    def setup_method(self):
        self.calc = TaxCalculator()

    def test_standard_us_sales_tax(self):
        result = self.calc.calculate_tax(10.0, "US-CA")
        # round(10.0 * 0.0725, 2) = 0.72 (banker's rounding on 0.725)
        assert result.tax_amount == pytest.approx(0.72)
        assert result.effective_rate == pytest.approx(0.0725)
        assert result.tax_type == TaxType.SALES_TAX
        assert result.exempt is False

    def test_standard_eu_vat(self):
        result = self.calc.calculate_tax(10.0, "DE")
        assert result.tax_amount == pytest.approx(1.90)
        assert result.effective_rate == pytest.approx(0.19)
        assert result.tax_type == TaxType.VAT
        assert result.exempt is False

    def test_standard_uk_vat(self):
        result = self.calc.calculate_tax(10.0, "GB")
        assert result.tax_amount == pytest.approx(2.00)
        assert result.effective_rate == pytest.approx(0.20)
        assert result.tax_type == TaxType.VAT
        assert result.exempt is False

    def test_standard_canada_hst(self):
        result = self.calc.calculate_tax(10.0, "CA-ON")
        assert result.tax_amount == pytest.approx(1.30)
        assert result.effective_rate == pytest.approx(0.13)
        assert result.tax_type == TaxType.HST
        assert result.exempt is False

    def test_standard_australia_gst(self):
        result = self.calc.calculate_tax(10.0, "AU")
        assert result.tax_amount == pytest.approx(1.00)
        assert result.effective_rate == pytest.approx(0.10)
        assert result.tax_type == TaxType.GST
        assert result.exempt is False

    def test_standard_japan_jct(self):
        result = self.calc.calculate_tax(10.0, "JP")
        assert result.tax_amount == pytest.approx(1.00)
        assert result.effective_rate == pytest.approx(0.10)
        assert result.tax_type == TaxType.JCT
        assert result.exempt is False

    def test_zero_fee_no_tax(self):
        result = self.calc.calculate_tax(0.0, "DE")
        assert result.tax_amount == 0.0
        assert result.exempt is True

    def test_negative_fee_no_tax(self):
        result = self.calc.calculate_tax(-5.0, "DE")
        assert result.tax_amount == 0.0
        assert result.exempt is True

    def test_unknown_jurisdiction_no_tax(self):
        result = self.calc.calculate_tax(10.0, "XX")
        assert result.tax_amount == 0.0
        assert result.exempt is True
        assert "unknown" in result.exempt_reason.lower()

    def test_non_taxable_jurisdiction(self):
        result = self.calc.calculate_tax(10.0, "US-CO")
        assert result.tax_amount == 0.0
        assert result.exempt is True
        assert "not taxable" in result.exempt_reason.lower()


# ---------------------------------------------------------------------------
# TestB2BReverseCharge
# ---------------------------------------------------------------------------


class TestB2BReverseCharge:
    """B2B reverse-charge exemptions with valid/invalid tax IDs."""

    def setup_method(self):
        self.calc = TaxCalculator()

    def test_eu_reverse_charge_valid_vat_id(self):
        result = self.calc.calculate_tax(
            10.0, "DE", business_tax_id="DE123456789",
        )
        assert result.exempt is True
        assert result.reverse_charge is True
        assert result.tax_amount == 0.0
        assert result.business_tax_id == "DE123456789"

    def test_uk_reverse_charge_valid_vat_id(self):
        result = self.calc.calculate_tax(
            10.0, "GB", business_tax_id="GB123456789",
        )
        assert result.exempt is True
        assert result.reverse_charge is True
        assert result.tax_amount == 0.0

    def test_au_reverse_charge_valid_abn(self):
        result = self.calc.calculate_tax(
            10.0, "AU", business_tax_id="12345678901",
        )
        assert result.exempt is True
        assert result.reverse_charge is True
        assert result.tax_amount == 0.0

    def test_jp_reverse_charge_valid_id(self):
        result = self.calc.calculate_tax(
            10.0, "JP", business_tax_id="T1234567890123",
        )
        assert result.exempt is True
        assert result.reverse_charge is True
        assert result.tax_amount == 0.0

    def test_reverse_charge_invalid_format(self):
        result = self.calc.calculate_tax(
            10.0, "DE", business_tax_id="INVALID",
        )
        # Invalid format => tax charged normally, not exempt.
        assert result.exempt is False
        assert result.reverse_charge is False
        assert result.tax_amount == pytest.approx(1.90)

    def test_us_no_reverse_charge(self):
        result = self.calc.calculate_tax(
            10.0, "US-CA", business_tax_id="123456789",
        )
        # US has no reverse-charge mechanism; tax still applied.
        assert result.exempt is False
        assert result.reverse_charge is False
        assert result.tax_amount == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# TestTaxIdValidation
# ---------------------------------------------------------------------------


class TestTaxIdValidation:
    """Format-only validation of business tax IDs."""

    def test_valid_de_vat_id(self):
        assert _validate_tax_id_format("DE", "DE123456789") is True

    def test_invalid_de_vat_id_too_short(self):
        assert _validate_tax_id_format("DE", "DE12345") is False

    def test_valid_fr_vat_id(self):
        assert _validate_tax_id_format("FR", "FRXX999999999") is True

    def test_valid_nl_vat_id(self):
        assert _validate_tax_id_format("NL", "NL123456789B01") is True

    def test_valid_au_abn(self):
        assert _validate_tax_id_format("AU", "12345678901") is True

    def test_valid_jp_jct(self):
        assert _validate_tax_id_format("JP", "T1234567890123") is True


# ---------------------------------------------------------------------------
# TestTaxJurisdictionDatabase
# ---------------------------------------------------------------------------


class TestTaxJurisdictionDatabase:
    """Jurisdiction database completeness and lookup."""

    def setup_method(self):
        self.calc = TaxCalculator()

    def test_all_us_states_present(self):
        codes = self.calc.list_jurisdiction_codes()
        expected_us = ["US-CA", "US-TX", "US-NY", "US-WA", "US-FL", "US-IL", "US-MA", "US-CO"]
        for state in expected_us:
            assert state in codes, f"Missing US state {state}"

    def test_all_eu_countries_present(self):
        codes = self.calc.list_jurisdiction_codes()
        expected_eu = ["DE", "FR", "NL", "IT", "ES", "SE", "PL"]
        for country in expected_eu:
            assert country in codes, f"Missing EU country {country}"

    def test_all_jurisdictions_have_rates(self):
        for jur in self.calc.list_jurisdictions():
            if jur.code == "US-CO":
                # US-CO is marked non-taxable; rate is still defined but not applied.
                assert jur.platform_fee_taxable is False
            else:
                assert jur.rate > 0, f"{jur.code} has rate <= 0"

    def test_list_jurisdictions_returns_all(self):
        jurisdictions = self.calc.list_jurisdictions()
        assert len(jurisdictions) == 22

    def test_list_jurisdiction_codes(self):
        codes = self.calc.list_jurisdiction_codes()
        assert len(codes) == 22

    def test_get_jurisdiction_unknown(self):
        result = self.calc.get_jurisdiction("XX")
        assert result is None


# ---------------------------------------------------------------------------
# TestTaxResult
# ---------------------------------------------------------------------------


class TestTaxResult:
    """TaxResult serialisation via to_dict."""

    def test_to_dict_standard(self):
        result = TaxResult(
            jurisdiction_code="DE",
            tax_type=TaxType.VAT,
            tax_amount=1.90,
            effective_rate=0.19,
            taxable_amount=10.0,
        )
        d = result.to_dict()
        assert d["jurisdiction_code"] == "DE"
        assert d["tax_type"] == "vat"
        assert d["tax_amount"] == pytest.approx(1.90)
        assert d["effective_rate"] == pytest.approx(0.19)
        assert d["effective_rate_percent"] == pytest.approx(19.0)
        assert d["taxable_amount"] == pytest.approx(10.0)
        assert d["exempt"] is False
        assert d["exempt_reason"] is None
        assert d["reverse_charge"] is False
        assert d["business_tax_id"] is None

    def test_to_dict_exempt(self):
        result = TaxResult(
            jurisdiction_code="US-CO",
            tax_type=TaxType.SALES_TAX,
            tax_amount=0.0,
            effective_rate=0.0,
            taxable_amount=10.0,
            exempt=True,
            exempt_reason="Platform fees not taxable in Colorado",
        )
        d = result.to_dict()
        assert d["exempt"] is True
        assert d["exempt_reason"] == "Platform fees not taxable in Colorado"
        assert d["tax_amount"] == 0.0

    def test_to_dict_reverse_charge(self):
        result = TaxResult(
            jurisdiction_code="DE",
            tax_type=TaxType.VAT,
            tax_amount=0.0,
            effective_rate=0.0,
            taxable_amount=10.0,
            exempt=True,
            exempt_reason="B2B reverse charge",
            reverse_charge=True,
            business_tax_id="DE123456789",
        )
        d = result.to_dict()
        assert d["reverse_charge"] is True
        assert d["business_tax_id"] == "DE123456789"
        assert d["exempt"] is True
        assert d["tax_amount"] == 0.0


# ---------------------------------------------------------------------------
# TestBillingLedgerTaxIntegration
# ---------------------------------------------------------------------------


class TestBillingLedgerTaxIntegration:
    """BillingLedger.calculate_fee correctly applies tax via TaxCalculator."""

    def test_calculate_fee_with_jurisdiction(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fee = ledger.calculate_fee(100.0, jurisdiction="DE")

        # 5% of 100 = 5.00 fee; 19% of 5.00 = 0.95 tax.
        assert fee.fee_amount == pytest.approx(5.00)
        assert fee.tax_amount == pytest.approx(0.95)
        assert fee.tax_jurisdiction == "DE"
        assert fee.tax_type == "vat"
        assert fee.total_cost == pytest.approx(105.95)

    def test_calculate_fee_without_jurisdiction(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fee = ledger.calculate_fee(100.0)

        assert fee.fee_amount == pytest.approx(5.00)
        assert fee.tax_amount == 0.0
        assert fee.tax_jurisdiction is None
        assert fee.total_cost == pytest.approx(105.00)

    def test_calculate_fee_waived_no_tax(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=10))
        fee = ledger.calculate_fee(100.0, jurisdiction="DE")

        # Free tier => fee=0, tax not applied even with jurisdiction.
        assert fee.fee_amount == 0.0
        assert fee.tax_amount == 0.0
        assert fee.waived is True

    def test_calculate_and_record_fee_with_tax(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fee, charge_id = ledger.calculate_and_record_fee(
            "job-tax-1", 100.0, jurisdiction="DE",
        )

        assert fee.tax_amount == pytest.approx(0.95)
        assert fee.tax_jurisdiction == "DE"
        assert charge_id  # Non-empty string.

        # Verify the charge is in the ledger.
        charges = ledger.list_charges()
        assert len(charges) == 1
        assert charges[0]["job_id"] == "job-tax-1"

    def test_fee_to_dict_includes_tax_when_set(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fee = ledger.calculate_fee(100.0, jurisdiction="DE")

        d = fee.to_dict()
        assert "tax_amount" in d
        assert "tax_rate" in d
        assert "tax_jurisdiction" in d
        assert "tax_type" in d
        assert "tax_reverse_charge" in d
        assert d["tax_amount"] == pytest.approx(0.95)
        assert d["tax_jurisdiction"] == "DE"

    def test_fee_to_dict_excludes_tax_when_unset(self):
        ledger = BillingLedger(fee_policy=FeePolicy(free_tier_jobs=0))
        fee = ledger.calculate_fee(100.0)

        d = fee.to_dict()
        assert "tax_amount" not in d
        assert "tax_rate" not in d
        assert "tax_jurisdiction" not in d
        assert "tax_type" not in d
        assert "tax_reverse_charge" not in d
