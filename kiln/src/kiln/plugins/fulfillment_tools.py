"""Fulfillment tools plugin.

Extracts external manufacturing / fulfillment MCP tools from server.py into
a focused plugin module.  Provides tools for listing materials, getting
quotes, placing orders, checking order status, cancelling orders, and
checking alerts from background fulfillment monitors.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _FulfillmentToolsPlugin:
    """External manufacturing (Craftcloud) fulfillment tools.

    Tools:
        - fulfillment_materials
        - fulfillment_quote
        - fulfillment_order
        - fulfillment_order_status
        - fulfillment_cancel
        - fulfillment_alerts
    """

    @property
    def name(self) -> str:
        return "fulfillment_tools"

    @property
    def description(self) -> str:
        return "External manufacturing fulfillment tools (Craftcloud)"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register fulfillment tools with the MCP server."""

        @mcp.tool()
        def fulfillment_materials() -> dict:
            """List available materials from external manufacturing services.

            Returns materials with technology (FDM, SLA, SLS, etc.), color,
            finish, and pricing.  Use the material ``id`` when requesting a quote
            with ``fulfillment_quote``.

            Requires ``KILN_CRAFTCLOUD_API_KEY`` to be set.
            """
            import kiln.server as _srv
            from kiln.fulfillment import FulfillmentError

            try:
                provider = _srv._get_fulfillment()
                materials = provider.list_materials()
                return {
                    "success": True,
                    "provider": provider.name,
                    "materials": [m.to_dict() for m in materials],
                    "count": len(materials),
                }
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to list fulfillment materials: {exc}. Check that KILN_CRAFTCLOUD_API_KEY is set."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_materials")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_materials: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def fulfillment_quote(
            file_path: str,
            material_id: str,
            quantity: int = 1,
            shipping_country: str = "US",
        ) -> dict:
            """Get a manufacturing quote for a 3D model from Craftcloud.

            Args:
                file_path: Absolute path to the model file (STL, 3MF, OBJ).
                material_id: Material ID from ``fulfillment_materials``.
                quantity: Number of copies to print (default 1).
                shipping_country: ISO country code for shipping (default "US").

            Uploads the model, returns pricing from Craftcloud's network of 150+
            print services, including unit price, total, lead time, and shipping
            options.  A Kiln platform fee is shown separately so the user sees
            the full cost before committing.

            If a payment method is linked, a hold is placed on the fee amount
            at quote time (Stripe auth-and-capture).  The hold is captured
            when the order is placed via ``fulfillment_order``, or released
            if the user doesn't proceed.

            Use the returned ``quote_id`` with ``fulfillment_order`` to place the
            order.
            """
            import kiln.server as _srv
            from kiln.fulfillment import FulfillmentError, QuoteRequest
            from kiln.payments.base import PaymentError

            try:
                provider = _srv._get_fulfillment()
                quote = provider.get_quote(
                    QuoteRequest(
                        file_path=file_path,
                        material_id=material_id,
                        quantity=quantity,
                        shipping_country=shipping_country,
                    )
                )
                fee_calc = _srv._billing.calculate_fee(
                    quote.total_price,
                    currency=quote.currency,
                )
                quote_data = quote.to_dict()
                quote_data["kiln_fee"] = fee_calc.to_dict()
                quote_data["total_with_fee"] = fee_calc.total_cost

                # Try to authorize (hold) the fee at quote time.
                try:
                    mgr = _srv._get_payment_mgr()
                    if mgr.available_rails:
                        auth_result = mgr.authorize_fee(
                            quote.quote_id,
                            fee_calc,
                        )
                        if auth_result.payment_id:
                            quote_data["payment_hold"] = {
                                "payment_id": auth_result.payment_id,
                                "status": auth_result.status.value,
                            }
                except (PaymentError, Exception):
                    # Hold failed — fee will be collected at order time.
                    pass

                return {
                    "success": True,
                    "quote": quote_data,
                }
            except FileNotFoundError as exc:
                return _srv._error_dict(
                    f"Failed to get fulfillment quote: {exc}", code="FILE_NOT_FOUND"
                )
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to get fulfillment quote: {exc}. Check that KILN_CRAFTCLOUD_API_KEY is set."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_quote")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_quote: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def fulfillment_order(
            quote_id: str,
            shipping_option_id: str = "",
            payment_hold_id: str = "",
            quoted_price: float = 0.0,
            quoted_currency: str = "USD",
            jurisdiction: str = "",
            business_tax_id: str = "",
        ) -> dict:
            """Place a manufacturing order based on a previous quote.

            Charges the platform fee BEFORE placing the order to prevent
            unpaid orders.  If order placement fails after payment, the
            charge is automatically refunded.

            Args:
                quote_id: Quote ID from ``fulfillment_quote``.
                shipping_option_id: Shipping option ID from the quote's
                    ``shipping_options`` list.
                payment_hold_id: PaymentIntent ID from the quote's
                    ``payment_hold`` field.  If provided, the previously
                    authorized hold is captured before placing the order.
                    This is the preferred payment flow.
                quoted_price: Total price returned by ``fulfillment_quote``
                    (used to calculate the fee when no ``payment_hold_id``
                    is provided).  Required when ``payment_hold_id`` is
                    empty and a payment rail is configured.
                quoted_currency: Currency of ``quoted_price`` (default USD).
                jurisdiction: Buyer's region (e.g. ``"US-CA"``, ``"DE"``, ``"AU"``).
                    When provided, the response includes an accurate total with
                    tax so the user sees exactly what they'll pay — no hidden
                    fees.  Use ``tax_jurisdictions`` to see all supported codes.
                business_tax_id: If the buyer is a registered business, their
                    tax ID (EU VAT number, AU ABN, etc.).  Businesses in the
                    EU, UK, Australia, and Japan are tax-exempt via reverse
                    charge — the tax line shows $0.00.

            Use ``fulfillment_order_status`` to track progress after placing.
            """
            import kiln.server as _srv
            from kiln.fulfillment import FulfillmentError, OrderRequest
            from kiln.fulfillment.intelligence import QuoteValidation
            from kiln.licensing import LicenseTier
            from kiln.payments.base import PaymentError

            if err := _srv._check_billing_auth("print"):
                return err

            # Require BUSINESS tier (same gate as original)
            ok, tier_msg = _srv._billing.__class__.__mro__  # noqa: F841 — tier check below
            from kiln.licensing import check_tier

            tier_ok, tier_msg = check_tier(LicenseTier.BUSINESS)
            if not tier_ok:
                return {
                    "success": False,
                    "error": tier_msg,
                    "code": "LICENSE_REQUIRED",
                    "required_tier": "business",
                }

            try:
                provider = _srv._get_fulfillment()

                # 0. Validate quote is still valid
                quote_validation: QuoteValidation | None = None
                try:
                    quote_validation = _srv._validate_quote_for_order(
                        quote_id,
                        provider_name=provider.name,
                    )
                except FulfillmentError as exc:
                    return _srv._error_dict(
                        f"Quote validation failed: {exc}",
                        code=getattr(exc, "code", None) or "QUOTE_INVALID",
                    )

                # 1. Determine price and calculate fee BEFORE placing.
                estimated_price = quoted_price
                currency = quoted_currency
                pay_result = None
                fee_calc = None

                # 1a. Early spend limit check (before any work).
                if estimated_price and estimated_price > 0:
                    fee_estimate = _srv._billing.calculate_fee(
                        estimated_price,
                        currency=currency,
                        jurisdiction=jurisdiction or None,
                        business_tax_id=business_tax_id or None,
                    )
                    if not fee_estimate.waived and fee_estimate.fee_amount > 0:
                        mgr = _srv._get_payment_mgr()
                        ok, reason = mgr.check_spend_limits(fee_estimate.fee_amount)
                        if not ok:
                            return _srv._error_dict(
                                f"Order would exceed spend limits: {reason}. "
                                "Adjust limits in billing settings before placing this order.",
                                code="SPEND_LIMIT",
                            )

                # 2. Charge / capture payment BEFORE placing the order.
                if payment_hold_id or estimated_price > 0:
                    if estimated_price > 0:
                        fee_calc = _srv._billing.calculate_fee(
                            estimated_price,
                            currency=currency,
                            jurisdiction=jurisdiction or None,
                            business_tax_id=business_tax_id or None,
                        )

                    try:
                        mgr = _srv._get_payment_mgr()
                        if mgr.available_rails:
                            if payment_hold_id:
                                if fee_calc is None:
                                    fee_calc = _srv._billing.calculate_fee(0.0)
                                pay_result = mgr.capture_fee(
                                    payment_hold_id,
                                    quote_id,
                                    fee_calc,
                                )
                            elif fee_calc:
                                pay_result = mgr.charge_fee(quote_id, fee_calc)
                            else:
                                return _srv._error_dict(
                                    "Cannot place order: no payment hold and no "
                                    "quoted_price provided.  Re-run fulfillment_quote "
                                    "to get pricing, then pass payment_hold_id or "
                                    "quoted_price.",
                                    code="MISSING_PRICE",
                                )
                        else:
                            if estimated_price > 0:
                                fee_calc, _charge_id = _srv._billing.calculate_and_record_fee(
                                    quote_id,
                                    estimated_price,
                                    currency=currency,
                                    jurisdiction=jurisdiction or None,
                                    business_tax_id=business_tax_id or None,
                                )
                    except PaymentError as pe:
                        return _srv._error_dict(
                            f"Payment failed: {pe}. Order was NOT placed. Please update your payment method and try again.",
                            code="PAYMENT_ERROR",
                        )

                # 3. Place the order AFTER payment succeeds.
                try:
                    result = provider.place_order(
                        OrderRequest(
                            quote_id=quote_id,
                            shipping_option_id=shipping_option_id,
                        )
                    )
                except (FulfillmentError, RuntimeError) as exc:
                    refund_warning = _srv._refund_after_order_failure(
                        pay_result,
                        payment_hold_id,
                    )
                    msg = f"Order placement failed: {exc}. "
                    if refund_warning:
                        msg += refund_warning
                    else:
                        msg += "Your payment has been refunded automatically."
                    return _srv._error_dict(msg)

                # 4. Build response.
                order_data = result.to_dict()
                if fee_calc:
                    order_data["kiln_fee"] = fee_calc.to_dict()
                    order_data["total_with_fee"] = fee_calc.total_cost
                if pay_result:
                    order_data["payment"] = pay_result.to_dict()

                    if result.order_id and result.order_id != quote_id:
                        try:
                            _srv._billing.record_charge(
                                result.order_id,
                                fee_calc,
                                payment_id=pay_result.payment_id,
                                payment_rail=pay_result.rail.value,
                                payment_status=pay_result.status.value,
                            )
                        except Exception:
                            _logger.debug(
                                "Could not link charge to order %s",
                                result.order_id,
                            )

                # 5. Price-drift check
                response_warnings: list[str] = []
                if quote_validation and quote_validation.warnings:
                    response_warnings.extend(quote_validation.warnings)

                if result.total_price is not None and quote_validation:
                    from kiln.fulfillment.intelligence import _check_price_drift

                    drift_warning, should_block = _check_price_drift(
                        quote_validation.quoted_price,
                        result.total_price,
                    )
                    if should_block:
                        _logger.error(
                            "Price drift BLOCKED order for quote %s: %s",
                            quote_id,
                            drift_warning,
                        )
                        refund_warning = _srv._refund_after_order_failure(
                            pay_result,
                            payment_hold_id,
                        )
                        msg = drift_warning or "Price drift exceeded safety limit."
                        if refund_warning:
                            msg += f" {refund_warning}"
                        else:
                            msg += " Your payment has been refunded automatically."
                        return _srv._error_dict(msg, code="PRICE_DRIFT_BLOCKED")
                    if drift_warning:
                        _logger.warning(
                            "Price drift detected for quote %s: %s",
                            quote_id,
                            drift_warning,
                        )
                        response_warnings.append(drift_warning)

                if response_warnings:
                    order_data["warnings"] = response_warnings

                return {
                    "success": True,
                    "order": order_data,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_order")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_order: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def fulfillment_order_status(order_id: str) -> dict:
            """Check the status of a fulfillment order.

            Args:
                order_id: Order ID from ``fulfillment_order``.

            Returns current order state, tracking info, and estimated delivery.
            """
            import kiln.server as _srv
            from kiln.fulfillment import FulfillmentError

            try:
                provider = _srv._get_fulfillment()
                result = provider.get_order_status(order_id)
                return {
                    "success": True,
                    "order": result.to_dict(),
                }
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to check order status: {exc}. Verify the order_id is correct."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_order_status")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_order_status: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def fulfillment_cancel(order_id: str) -> dict:
            """Cancel a fulfillment order (if still cancellable).

            Args:
                order_id: Order ID to cancel.

            Only orders that have not yet shipped can be cancelled.
            """
            import kiln.server as _srv
            from kiln.fulfillment import FulfillmentError
            from kiln.licensing import LicenseTier, check_tier

            if err := _srv._check_billing_auth("print"):
                return err
            tier_ok, tier_msg = check_tier(LicenseTier.BUSINESS)
            if not tier_ok:
                return {
                    "success": False,
                    "error": tier_msg,
                    "code": "LICENSE_REQUIRED",
                    "required_tier": "business",
                }
            try:
                provider = _srv._get_fulfillment()
                result = provider.cancel_order(order_id)
                return {
                    "success": True,
                    "order": result.to_dict(),
                }
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to cancel order: {exc}. The order may have already shipped."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_cancel")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_cancel: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def fulfillment_alerts() -> dict:
            """Check for fulfillment order alerts (stalled, failed, cancelled orders).

            Returns any active alerts from the background fulfillment monitor.
            Alerts are generated when orders are cancelled/failed by the provider
            or have been stuck in processing longer than the expected lead time.
            """
            import kiln.server as _srv

            try:
                monitor = _srv._get_fulfillment_monitor()
                alerts = monitor.get_alerts()
                return {"success": True, "alerts": alerts, "count": len(alerts)}
            except Exception as exc:
                return _srv._error_dict(f"Failed to check fulfillment alerts: {exc}")

        _logger.debug("Registered fulfillment tools")


plugin = _FulfillmentToolsPlugin()
