"""Tests for kiln.printer_billing."""

from __future__ import annotations

from kiln.printer_billing import PrinterUsageBilling


class TestPrinterUsageBilling:
    def test_no_overage_under_limit(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(10)
        assert usage.overage_count == 0
        assert usage.overage_charge == 0.0
        assert usage.included_count == 20

    def test_no_overage_at_limit(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(20)
        assert usage.overage_count == 0
        assert usage.overage_charge == 0.0

    def test_overage_above_limit(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(35)
        assert usage.overage_count == 15
        assert usage.overage_charge == 225.0

    def test_zero_printers(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(0)
        assert usage.overage_count == 0
        assert usage.overage_charge == 0.0

    def test_single_overage(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(21)
        assert usage.overage_count == 1
        assert usage.overage_charge == 15.0

    def test_custom_rate(self):
        billing = PrinterUsageBilling(overage_rate=20.0, included_printers=10)
        usage = billing.usage_summary(15)
        assert usage.overage_count == 5
        assert usage.overage_charge == 100.0

    def test_to_dict(self):
        billing = PrinterUsageBilling()
        usage = billing.usage_summary(25)
        d = usage.to_dict()
        assert d["active_count"] == 25
        assert d["overage_count"] == 5
        assert d["overage_charge"] == 75.0


class TestEstimateMonthlyCost:
    def test_base_only(self):
        billing = PrinterUsageBilling()
        est = billing.estimate_monthly_cost(10)
        assert est["total_monthly"] == 499.0
        assert est["overage_charge"] == 0.0

    def test_with_overage(self):
        billing = PrinterUsageBilling()
        est = billing.estimate_monthly_cost(50)
        assert est["overage_printers"] == 30
        assert est["overage_charge"] == 450.0
        assert est["total_monthly"] == 949.0

    def test_custom_base_price(self):
        billing = PrinterUsageBilling()
        est = billing.estimate_monthly_cost(20, base_price=399.0)
        assert est["total_monthly"] == 399.0
