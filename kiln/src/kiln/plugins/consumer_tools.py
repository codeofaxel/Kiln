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
        - suggest_material_for_order
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
            fee_amount: float = 0.0,
            jurisdiction: str = "",
            business_tax_id: str = "",
            manufacturer_quote_usd: float = 0.0,
            currency: str = "USD",
            user_email: str = "",
        ) -> dict:
            """Preview the complete price breakdown — including tax — before placing an order.

            Terms §7 contract: "Preview the tax for any order with the
            ``tax_estimate`` tool before placing it.  We display the full
            fee + tax breakdown before charging.  No hidden fees."

            This tool fulfills that contract by returning the same line
            items the order will commit to at charge time.  Per Terms §7
            tax is computed on the orchestration fee ONLY — never on
            the manufacturer's quoted total.  The manufacturer is
            responsible for any taxes on their own charges.

            Two calling shapes are supported:

            1. **Canonical preview** (recommended): pass
               ``manufacturer_quote_usd`` and the tool returns the
               full breakdown — ``manufacturer_quote``,
               ``orchestration_fee``, ``tax_on_fee``, ``total`` — so
               the agent can show the user the exact line items that
               match the eventual charge.  This is the shape Terms §7
               commits to.
            2. **Tax-only legacy** (compatibility): pass ``fee_amount``
               with no ``manufacturer_quote_usd`` and the tool returns
               the older ``{tax: {...}}`` shape, useful for callers
               that already know the fee and only want the tax line.

            Args:
                fee_amount: Legacy — the platform fee amount (from the
                    quote's ``kiln_fee``).  Used only when
                    ``manufacturer_quote_usd`` is omitted.
                jurisdiction: Where the buyer is located (e.g.
                    ``"US-CA"``, ``"DE"``, ``"AU"``).  Use
                    ``tax_jurisdictions`` to see all supported codes.
                    When empty in canonical-preview mode, no tax is
                    applied (preview shows manuf + fee only).
                business_tax_id: If the buyer is a business, their tax
                    ID (e.g. EU VAT number).  In the EU, UK, Australia,
                    and Japan, businesses are exempt — the tax line
                    shows $0.00 with a note that reverse charge
                    applies.
                manufacturer_quote_usd: Provider's quoted price (e.g.
                    from ``fulfillment_quote``).  When non-zero,
                    triggers canonical-preview mode.
                currency: Currency of the manufacturer quote (default
                    USD).  Tax rates are applied at the standard
                    jurisdiction rate regardless.
                user_email: Buyer's email — affects the free-tier
                    waiver (first 3 fulfillment orders/month per user
                    are fee-free).  Available in canonical mode only.

            Returns:
                Canonical mode (``manufacturer_quote_usd > 0``):
                    ``{success, manufacturer_quote, orchestration_fee,
                    tax_on_fee, total, currency, fee_waived,
                    fee_waiver_reason, tax_jurisdiction,
                    tax_rate_percent, tax_reverse_charge, note}``.
                Legacy mode (``manufacturer_quote_usd == 0``):
                    ``{success, tax: {...}}``.

            Read-only — charges no card, contacts no provider.
            """
            from kiln.server import _error_dict

            try:
                from kiln_pro.tax import TaxCalculator
            except ImportError:
                return {
                    "status": "error",
                    "error": "Tax calculation requires Kiln Pro. Already subscribed? Run `kiln login` to sync this machine. Otherwise: https://kiln3d.com/pricing",
                    "code": "PRO_REQUIRED",
                }

            # Canonical-preview mode: walk the fee through BillingLedger
            # so the tax-on-fee invariant (Terms §7) is enforced by the
            # same code path that fires at charge time — no shape drift.
            if manufacturer_quote_usd and manufacturer_quote_usd > 0:
                try:
                    from kiln.server import _get_billing
                    ledger = _get_billing()
                    fee_calc = ledger.calculate_fee(
                        manufacturer_quote_usd,
                        currency=currency,
                        jurisdiction=jurisdiction or None,
                        business_tax_id=business_tax_id or None,
                        user_email=user_email or None,
                    )
                    fee_dict = fee_calc.to_dict()
                    return {
                        "success": True,
                        "manufacturer_quote": float(fee_calc.job_cost),
                        "orchestration_fee": float(fee_calc.fee_amount),
                        "tax_on_fee": float(fee_calc.tax_amount),
                        "total": float(fee_calc.total_cost),
                        "currency": fee_calc.currency,
                        "fee_waived": bool(fee_calc.waived),
                        "fee_waiver_reason": fee_calc.waiver_reason,
                        "tax_jurisdiction": fee_calc.tax_jurisdiction,
                        "tax_rate_percent": fee_dict.get(
                            "tax_rate_percent", 0.0,
                        ),
                        "tax_reverse_charge": bool(
                            fee_calc.tax_reverse_charge,
                        ),
                        "note": (
                            "Tax is computed on the orchestration fee "
                            "only (Kiln's slice).  The manufacturer is "
                            "responsible for any taxes on their charges."
                        ),
                    }
                except Exception as exc:
                    return _error_dict(
                        f"Preview failed: {exc}",
                        code="PREVIEW_ERROR",
                    )

            # Legacy tax-only mode — preserved for compatibility.
            try:
                calc = TaxCalculator()
                result = calc.calculate_tax(
                    fee_amount,
                    jurisdiction,
                    business_tax_id=business_tax_id or None,
                )
                return {"success": True, "tax": result.to_dict()}
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
                from kiln_pro.tax import TaxCalculator
            except ImportError:
                return {
                    "status": "error",
                    "error": "Tax calculation requires Kiln Pro. Already subscribed? Run `kiln login` to sync this machine. Otherwise: https://kiln3d.com/pricing",
                    "code": "PRO_REQUIRED",
                }

            try:
                calc = TaxCalculator()
                jurisdictions = [j.to_dict() for j in calc.list_jurisdictions()]
                return {
                    "success": True,
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
                from kiln_pro.tax import TaxCalculator
            except ImportError:
                return {
                    "status": "error",
                    "error": "Tax calculation requires Kiln Pro. Already subscribed? Run `kiln login` to sync this machine. Otherwise: https://kiln3d.com/pricing",
                    "code": "PRO_REQUIRED",
                }

            try:
                calc = TaxCalculator()
                jur = calc.get_jurisdiction(code)
                if jur is None:
                    return _error_dict(
                        f"Unknown jurisdiction: {code}. Use tax_jurisdictions to see all supported codes."
                    )
                return {"success": True, "jurisdiction": jur.to_dict()}
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
        def suggest_material_for_order(
            use_case: str,
            budget: str = "",
            need_weather_resistant: bool = False,
            need_food_safe: bool = False,
            need_high_detail: bool = False,
            need_high_strength: bool = False,
        ) -> dict:
            """Suggest a material when ordering a print from a fulfillment provider.

            Use this when routing a print job to a fulfillment provider
            and need to pick the right material + technology for the order.

            **Which material tool to use:**

            - Ordering a print from a service? → ``suggest_material_for_order`` (this tool)
            - Designing a part and need engineering specs? → ``recommend_design_material``
            - Quick intent-based pick for your own printer? → ``recommend_material``

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
                _logger.exception("Unexpected error in suggest_material_for_order")
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
