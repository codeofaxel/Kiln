"""Tests for consumer_tools.py plugin MCP tools.

Covers the MCP tool wrappers (not the underlying modules, which have
their own tests in test_consumer.py):
- tax_estimate — happy path, failure
- tax_jurisdictions — happy path, failure
- tax_jurisdiction_lookup — found, not found, failure
- donate_info — happy path, failure
- consumer_onboarding — happy path, failure
- validate_shipping_address — happy path, failure
- supported_shipping_countries — happy path, failure
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure kiln_pro.tax is importable so @patch("kiln_pro.tax.TaxCalculator") can
# resolve the module path.  The actual TaxCalculator class lives in kiln-pro
# which may or may not be installed; we only need the module to exist in
# sys.modules so unittest.mock can traverse it.
if "kiln_pro.tax" not in sys.modules:
    _fake_tax = types.ModuleType("kiln_pro.tax")
    _fake_tax.TaxCalculator = type("TaxCalculator", (), {})  # type: ignore[attr-defined]
    sys.modules["kiln_pro.tax"] = _fake_tax

# ---------------------------------------------------------------------------
# Helpers — register tools and capture them
# ---------------------------------------------------------------------------


def _register_consumer_tools() -> dict:
    """Register the plugin on a mock MCP and return captured tool functions."""
    from kiln.plugins.consumer_tools import plugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    plugin.register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def consumer_tools():
    return _register_consumer_tools()


# ---------------------------------------------------------------------------
# TestTaxEstimate
# ---------------------------------------------------------------------------


class TestTaxEstimate:
    """Tests for tax_estimate MCP tool."""

    @patch("kiln_pro.tax.TaxCalculator")
    def test_happy_path(self, mock_calc_cls, consumer_tools):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"tax_amount": 1.50, "rate": 0.075}
        mock_calc_cls.return_value.calculate_tax.return_value = mock_result

        result = consumer_tools["tax_estimate"](20.0, "US-CA")

        assert result["success"] is True
        assert result["tax"]["rate"] == 0.075

    @patch("kiln_pro.tax.TaxCalculator")
    def test_failure(self, mock_calc_cls, consumer_tools):
        mock_calc_cls.return_value.calculate_tax.side_effect = RuntimeError("boom")

        result = consumer_tools["tax_estimate"](20.0, "US-CA")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestTaxJurisdictions
# ---------------------------------------------------------------------------


class TestTaxJurisdictions:
    """Tests for tax_jurisdictions MCP tool."""

    @patch("kiln_pro.tax.TaxCalculator")
    def test_happy_path(self, mock_calc_cls, consumer_tools):
        j1 = MagicMock()
        j1.to_dict.return_value = {"code": "US-CA"}
        j2 = MagicMock()
        j2.to_dict.return_value = {"code": "DE"}
        mock_calc_cls.return_value.list_jurisdictions.return_value = [j1, j2]

        result = consumer_tools["tax_jurisdictions"]()

        assert result["success"] is True
        assert result["count"] == 2

    @patch("kiln_pro.tax.TaxCalculator")
    def test_failure(self, mock_calc_cls, consumer_tools):
        mock_calc_cls.return_value.list_jurisdictions.side_effect = RuntimeError("boom")

        result = consumer_tools["tax_jurisdictions"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestTaxJurisdictionLookup
# ---------------------------------------------------------------------------


class TestTaxJurisdictionLookup:
    """Tests for tax_jurisdiction_lookup MCP tool."""

    @patch("kiln_pro.tax.TaxCalculator")
    def test_found(self, mock_calc_cls, consumer_tools):
        jur = MagicMock()
        jur.to_dict.return_value = {"code": "US-CA", "rate": 0.075}
        mock_calc_cls.return_value.get_jurisdiction.return_value = jur

        result = consumer_tools["tax_jurisdiction_lookup"]("US-CA")

        assert result["success"] is True
        assert result["jurisdiction"]["code"] == "US-CA"

    @patch("kiln_pro.tax.TaxCalculator")
    def test_not_found(self, mock_calc_cls, consumer_tools):
        mock_calc_cls.return_value.get_jurisdiction.return_value = None

        result = consumer_tools["tax_jurisdiction_lookup"]("ZZ-XX")

        assert result["success"] is False
        assert "Unknown jurisdiction" in result["error"]["message"]

    @patch("kiln_pro.tax.TaxCalculator")
    def test_failure(self, mock_calc_cls, consumer_tools):
        mock_calc_cls.return_value.get_jurisdiction.side_effect = RuntimeError("boom")

        result = consumer_tools["tax_jurisdiction_lookup"]("US-CA")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestDonateInfo
# ---------------------------------------------------------------------------


class TestDonateInfo:
    """Tests for donate_info MCP tool."""

    @patch("kiln.wallets.get_donation_info")
    def test_happy_path(self, mock_donate, consumer_tools):
        mock_donate.return_value = {"wallets": [{"chain": "ETH", "address": "0x123"}]}

        result = consumer_tools["donate_info"]()

        assert result["success"] is True
        assert "wallets" in result

    @patch("kiln.wallets.get_donation_info")
    def test_failure(self, mock_donate, consumer_tools):
        mock_donate.side_effect = RuntimeError("boom")

        result = consumer_tools["donate_info"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestConsumerOnboarding
# ---------------------------------------------------------------------------


class TestConsumerOnboarding:
    """Tests for consumer_onboarding MCP tool."""

    @patch("kiln.consumer.get_onboarding")
    def test_happy_path(self, mock_onboard, consumer_tools):
        guide = MagicMock()
        guide.to_dict.return_value = {"steps": [{"step": 1}]}
        mock_onboard.return_value = guide

        result = consumer_tools["consumer_onboarding"]()

        assert result["success"] is True
        assert "onboarding" in result

    @patch("kiln.consumer.get_onboarding")
    def test_failure(self, mock_onboard, consumer_tools):
        mock_onboard.side_effect = RuntimeError("boom")

        result = consumer_tools["consumer_onboarding"]()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestValidateShippingAddress
# ---------------------------------------------------------------------------


class TestValidateShippingAddress:
    """Tests for validate_shipping_address MCP tool."""

    @patch("kiln.consumer.validate_address")
    def test_happy_path(self, mock_validate, consumer_tools):
        val_result = MagicMock()
        val_result.to_dict.return_value = {"valid": True, "normalized": {"street": "123 Main St"}}
        mock_validate.return_value = val_result

        result = consumer_tools["validate_shipping_address"](
            street="123 Main St",
            city="Austin",
            country="US",
        )

        assert result["success"] is True
        assert "validation" in result

    @patch("kiln.consumer.validate_address")
    def test_failure(self, mock_validate, consumer_tools):
        mock_validate.side_effect = RuntimeError("boom")

        result = consumer_tools["validate_shipping_address"](
            street="123 Main St",
            city="Austin",
            country="US",
        )

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSupportedShippingCountries
# ---------------------------------------------------------------------------


class TestSupportedShippingCountries:
    """Tests for supported_shipping_countries MCP tool."""

    @patch("kiln.consumer.list_supported_countries")
    def test_happy_path(self, mock_list, consumer_tools):
        mock_list.return_value = [
            {"code": "US", "name": "United States"},
            {"code": "GB", "name": "United Kingdom"},
        ]

        result = consumer_tools["supported_shipping_countries"]()

        assert result["success"] is True
        assert result["count"] == 2

    @patch("kiln.consumer.list_supported_countries")
    def test_failure(self, mock_list, consumer_tools):
        mock_list.side_effect = RuntimeError("boom")

        result = consumer_tools["supported_shipping_countries"]()

        assert result["success"] is False
