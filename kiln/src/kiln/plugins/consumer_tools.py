"""Consumer-facing tools plugin (tax, shipping, onboarding).

Provides MCP tools for tax estimation, jurisdiction lookup, donation info,
consumer onboarding, address validation, material recommendations, price
estimates, timeline estimates, and shipping country support.

Migrated from server.py to reduce monolith size.  The original tool
definitions in server.py remain authoritative until removed; this plugin
is the extraction target.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _ConsumerToolsPlugin:
    """Consumer-facing tools (tax, shipping, onboarding).

    Tools:
        - tax_estimate
        - tax_jurisdictions
        - tax_jurisdiction_lookup
        - donate_info
        - consumer_onboarding
        - validate_shipping_address
        - recommend_material
        - estimate_price
        - estimate_timeline
        - supported_shipping_countries
    """

    @property
    def name(self) -> str:
        return "consumer_tools"

    @property
    def description(self) -> str:
        return "Consumer-facing tools (tax, shipping, onboarding)"

    def register(self, mcp: Any) -> None:
        """Register consumer-facing tools with the MCP server."""

        @mcp.tool()
        def tax_estimate(
            fee_amount: float,
            jurisdiction: str,
            business_tax_id: str = "",
        ) -> dict:
            """Preview the complete price breakdown -- including tax -- before placing an order.

            Call this after ``fulfillment_quote`` to show the user exactly what
            they'll pay: manufacturing cost + Kiln fee + applicable tax.  No
            surprises at checkout.

            Args:
                fee_amount: The platform fee amount (from the quote's ``kiln_fee``).
                jurisdiction: Where the buyer is located (e.g. "US-CA", "DE", "AU").
                    Use ``tax_jurisdictions`` to see all supported codes.
                business_tax_id: If the buyer is a business, their tax ID
                    (e.g. EU VAT number).  In the EU, UK, Australia, and Japan,
                    businesses are exempt -- the tax line will show $0.00 with
                    a note that reverse charge applies.

            Returns the tax amount, rate, type, and exemption status.
            """
            from kiln.server import _error_dict

            try:
                from kiln.tax import TaxCalculator

                calc = TaxCalculator()
                result = calc.calculate_tax(
                    fee_amount,
                    jurisdiction,
                    business_tax_id=business_tax_id or None,
                )
                return {"status": "success", "tax": result.to_dict()}
            except Exception as exc:
                return _error_dict(f"Tax calculation failed: {exc}")

        @mcp.tool()
        def tax_jurisdictions() -> dict:
            """List all 22 supported regions so the agent can match the user's location.

            Returns jurisdiction codes, tax types, and rates for the US (8 states),
            EU (7 countries), UK, Canada (4 provinces), Australia, and Japan.
            Pass the matching code to ``fulfillment_order`` or ``tax_estimate``
            to include tax in the price breakdown.
            """
            from kiln.server import _error_dict

            try:
                from kiln.tax import TaxCalculator

                calc = TaxCalculator()
                jurisdictions = [j.to_dict() for j in calc.list_jurisdictions()]
                return {
                    "status": "success",
                    "jurisdictions": jurisdictions,
                    "count": len(jurisdictions),
                }
            except Exception as exc:
                return _error_dict(f"Failed to list jurisdictions: {exc}")

        @mcp.tool()
        def tax_jurisdiction_lookup(code: str) -> dict:
            """Look up tax details for a specific region (rate, type, B2B exemptions).

            Args:
                code: Jurisdiction code (e.g. "US-CA", "DE", "GB", "AU").
                    Use ``tax_jurisdictions`` to browse all codes.
            """
            from kiln.server import _error_dict

            try:
                from kiln.tax import TaxCalculator

                calc = TaxCalculator()
                jur = calc.get_jurisdiction(code)
                if jur is None:
                    return _error_dict(
                        f"Unknown jurisdiction: {code}. Use tax_jurisdictions to see all supported codes."
                    )
                return {"status": "success", "jurisdiction": jur.to_dict()}
            except Exception as exc:
                return _error_dict(f"Jurisdiction lookup failed: {exc}")

        @mcp.tool()
        def donate_info() -> dict:
            """Get crypto wallet addresses to tip/donate to the Kiln project.

            Kiln is free, open-source software.  This tool returns wallet
            addresses (with ENS/SNS domains) where users can send tips in
            SOL, ETH, USDC, or other tokens to support development.

            No payment is required -- Kiln is fully functional without donating.
            """
            from kiln.server import _error_dict

            try:
                from kiln.wallets import get_donation_info

                return {"success": True, **get_donation_info()}
            except Exception as exc:
                _logger.exception("Unexpected error in donate_info")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def consumer_onboarding() -> dict:
            """Get the guided onboarding workflow for users without a 3D printer.

            Returns a step-by-step guide covering model discovery/generation,
            material recommendations, pricing, ordering, and delivery tracking.
            Perfect for first-time users who want to manufacture a custom part.
            """
            from kiln.consumer import get_onboarding
            from kiln.server import _error_dict

            try:
                guide = get_onboarding()
                return {
                    "success": True,
                    "onboarding": guide.to_dict(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in consumer_onboarding")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def validate_shipping_address(
            street: str,
            city: str,
            country: str,
            state: str = "",
            postal_code: str = "",
        ) -> dict:
            """Validate and normalize a shipping address for fulfillment orders.

            Args:
                street: Street address (e.g. "123 Main St").
                city: City name.
                country: ISO 3166-1 alpha-2 country code (e.g. "US", "GB", "DE").
                state: State/province (recommended for US addresses).
                postal_code: ZIP/postal code (validated per country format).

            Checks required fields, validates postal codes per country (US ZIP,
            Canadian postal, UK postcode), and returns warnings for missing optional
            fields.  Use the ``normalized`` address in the response when placing
            fulfillment orders.
            """
            from kiln.consumer import validate_address
            from kiln.server import _error_dict

            try:
                result = validate_address(
                    {
                        "street": street,
                        "city": city,
                        "state": state,
                        "postal_code": postal_code,
                        "country": country,
                    }
                )
                return {
                    "success": True,
                    "validation": result.to_dict(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in validate_shipping_address")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def recommend_material(
            use_case: str,
            budget: str = "",
            need_weather_resistant: bool = False,
            need_food_safe: bool = False,
            need_high_detail: bool = False,
            need_high_strength: bool = False,
        ) -> dict:
            """Recommend the best 3D printing material for a consumer use case.

            Args:
                use_case: What the part is for. Options: decorative, functional,
                    mechanical, prototype, miniature, jewelry, enclosure, wearable,
                    outdoor, food_safe.
                budget: Price preference: "budget", "mid", or "premium". Empty = any.
                need_weather_resistant: Only recommend weather-resistant materials.
                need_food_safe: Only recommend food-safe materials.
                need_high_detail: Prefer high-detail materials (SLA/MJF).
                need_high_strength: Prefer high-strength materials (SLS/MJF).

            Returns ranked material recommendations with technology, reasoning,
            price tier, and which fulfillment provider to use.
            """
            from kiln.server import _error_dict

            try:
                from kiln.consumer import recommend_material as _recommend

                guide = _recommend(
                    use_case,
                    budget=budget or None,
                    need_weather_resistant=need_weather_resistant,
                    need_food_safe=need_food_safe,
                    need_high_detail=need_high_detail,
                    need_high_strength=need_high_strength,
                )
                return {
                    "success": True,
                    "recommendation": guide.to_dict(),
                }
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_INPUT")
            except Exception as exc:
                _logger.exception("Unexpected error in recommend_material")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def estimate_price(
            technology: str,
            volume_cm3: float | None = None,
            dimensions_x_mm: float | None = None,
            dimensions_y_mm: float | None = None,
            dimensions_z_mm: float | None = None,
            quantity: int = 1,
        ) -> dict:
            """Get an instant price estimate before requesting a full quote.

            Args:
                technology: Manufacturing technology: FDM, SLA, SLS, MJF, or DMLS.
                volume_cm3: Part volume in cubic centimeters (if known).
                dimensions_x_mm: Bounding box X dimension in mm (alternative to volume).
                dimensions_y_mm: Bounding box Y dimension in mm.
                dimensions_z_mm: Bounding box Z dimension in mm.
                quantity: Number of copies (default 1).

            Returns a low/high price range based on typical per-cm3 pricing for
            the technology.  For exact pricing, use ``fulfillment_quote`` with a
            real model file.

            Either ``volume_cm3`` or all three dimension parameters must be provided.
            """
            from kiln.server import _error_dict

            try:
                from kiln.consumer import estimate_price as _estimate

                dims = None
                if dimensions_x_mm and dimensions_y_mm and dimensions_z_mm:
                    dims = {"x": dimensions_x_mm, "y": dimensions_y_mm, "z": dimensions_z_mm}
                result = _estimate(
                    technology,
                    volume_cm3=volume_cm3,
                    dimensions_mm=dims,
                    quantity=quantity,
                )
                return {
                    "success": True,
                    "estimate": result.to_dict(),
                }
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_INPUT")
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_price")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def estimate_timeline(
            technology: str,
            shipping_days: int | None = None,
            quantity: int = 1,
            country: str = "US",
        ) -> dict:
            """Estimate order-to-delivery timeline with per-stage breakdown.

            Args:
                technology: Manufacturing technology (FDM, SLA, SLS, MJF, DMLS).
                shipping_days: Known shipping days from a quote (optional).
                quantity: Number of copies (larger quantities add production time).
                country: Destination country code for shipping estimate fallback.

            Returns a stage-by-stage timeline (order confirmation, production,
            quality check, packaging, shipping) with estimated days per stage
            and a total delivery date.
            """
            from kiln.server import _error_dict

            try:
                from kiln.consumer import estimate_timeline as _timeline

                timeline = _timeline(
                    technology,
                    shipping_days=shipping_days,
                    quantity=quantity,
                    country=country,
                )
                return {
                    "success": True,
                    "timeline": timeline.to_dict(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in estimate_timeline")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def supported_shipping_countries() -> dict:
            """List all countries supported for fulfillment shipping.

            Returns ISO country codes and full names for all 23+ countries
            where Kiln fulfillment providers can ship manufactured parts.
            """
            from kiln.consumer import list_supported_countries
            from kiln.server import _error_dict

            try:
                countries = list_supported_countries()
                return {
                    "success": True,
                    "countries": countries,
                    "count": len(countries),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in supported_shipping_countries")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered consumer-facing tools")


plugin = _ConsumerToolsPlugin()
