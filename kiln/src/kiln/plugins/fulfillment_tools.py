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

from kiln.fulfillment_profiles import (
    delete_shipping_profile as _delete_shipping_profile,
)
from kiln.fulfillment_profiles import (
    get_shipping_profile as _get_shipping_profile,
)
from kiln.fulfillment_profiles import (
    issue_shipping_confirmation_token as _issue_shipping_confirmation_token,
)
from kiln.fulfillment_profiles import (
    list_shipping_profiles as _list_shipping_profiles,
)
from kiln.fulfillment_profiles import (
    normalize_shipping_address as _normalize_shipping_address,
)
from kiln.fulfillment_profiles import (
    save_shipping_profile as _save_shipping_profile,
)
from kiln.fulfillment_profiles import (
    validate_shipping_confirmation_token as _validate_shipping_confirmation_token,
)
from kiln.preview_gate import get_preview_gate

_logger = logging.getLogger(__name__)


def _resolve_shipping_address(
    shipping_address: dict[str, Any] | None,
    shipping_profile_name: str = "",
) -> dict[str, str]:
    """Resolve an explicit address or a saved profile into provider-ready fields."""
    if shipping_address and shipping_profile_name:
        raise ValueError("Provide either shipping_address or shipping_profile_name, not both.")
    if shipping_profile_name:
        return _get_shipping_profile(shipping_profile_name).shipping_address
    return _normalize_shipping_address(shipping_address)


def _normalize_save_profile_decision(raw: str) -> str:
    """Normalize an explicit save-profile decision."""
    value = raw.strip().lower().replace("-", "_")
    if value in {"yes", "y", "save", "true"}:
        return "save"
    if value in {"no", "n", "do_not_save", "dont_save", "false"}:
        return "do_not_save"
    return ""


def _validate_preview_confirmation(
    *,
    preview_token: str,
    preview_file_path: str,
    consume: bool,
) -> tuple[bool, str | None]:
    """Validate fulfillment preview confirmation against the rendered file."""
    if not preview_token:
        return False, "missing_preview_token"
    if not preview_file_path:
        return False, "missing_preview_file_path"
    return get_preview_gate().validate(
        preview_token,
        preview_file_path,
        consume=consume,
    )


class _FulfillmentToolsPlugin:
    """External manufacturing (Craftcloud) fulfillment tools.

    Tools:
        - fulfillment_materials
        - fulfillment_quote
        - save_shipping_profile
        - list_shipping_profiles
        - delete_shipping_profile
        - issue_shipping_confirmation_token
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
        def fulfillment_materials(
            search: str | None = None,
            technology: str | None = None,
            limit: int = 50,
        ) -> dict:
            """List available materials from external manufacturing services.

            Returns materials with technology (FDM, SLA, SLS, etc.), color,
            finish, and pricing.  Use the material ``id`` when requesting a quote
            with ``fulfillment_quote``.

            The full catalog contains 2000+ materials.  Use the optional filter
            parameters to narrow results so agents can find the right material
            without overwhelming context windows.

            Args:
                search: Filter materials whose name contains this term
                    (word-boundary match, case-insensitive).  E.g. ``"nylon"``
                    matches "Nylon 12" and "Glass-filled Nylon" but not
                    "Carbonylon".
                technology: Filter by manufacturing technology (word-boundary
                    match, case-insensitive).  Common values: ``"SLS"``,
                    ``"FDM"``, ``"SLA"``, ``"MJF"``, ``"DMLS"``.  Matches
                    against both the technology field and the material name.
                limit: Maximum number of materials to return (default 50).

            Direct Craftcloud mode requires the operator's own
            ``KILN_CRAFTCLOUD_API_KEY``. Normal users should use the hosted
            kiln-pro proxy so Craftcloud access stays server-side and quota
            enforcement applies.
            """
            import re

            import kiln.server as _srv
            try:
                from kiln.fulfillment import FulfillmentError
            except ImportError:
                return _srv._error_dict(
                    "Fulfillment module is not available (kiln-pro required).",
                    code="NOT_AVAILABLE",
                )

            try:
                provider = _srv._get_fulfillment()
                materials = provider.list_materials()

                if search:
                    pat = re.compile(
                        r'(?<![a-z])' + re.escape(search.lower()) + r'(?![a-z])',
                        re.IGNORECASE,
                    )
                    materials = [m for m in materials if pat.search(m.name)]
                if technology:
                    tech_pat = re.compile(
                        r'(?<![a-z])' + re.escape(technology.lower()) + r'(?![a-z])',
                        re.IGNORECASE,
                    )
                    materials = [
                        m for m in materials
                        if tech_pat.search(m.technology or '') or tech_pat.search(m.name)
                    ]
                materials = materials[:limit]

                return {
                    "success": True,
                    "provider": provider.name,
                    "materials": [m.to_dict() for m in materials],
                    "count": len(materials),
                }
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to list fulfillment materials: {exc}. Check fulfillment provider credentials or license."
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
            options. A Kiln orchestration fee is shown separately so
            the user sees the full cost before committing.

            If a payment method is linked, a hold is placed on the fee amount
            at quote time (Stripe auth-and-capture).  The hold is captured
            when the order is placed via ``fulfillment_order``, or released
            if the user doesn't proceed.

            Use the returned ``quote_id`` with ``fulfillment_order`` to place the
            order.
            """
            import kiln.server as _srv
            try:
                from kiln.fulfillment import FulfillmentError, QuoteRequest
            except ImportError:
                return _srv._error_dict(
                    "Fulfillment module is not available (kiln-pro required).",
                    code="NOT_AVAILABLE",
                )
            try:
                from kiln.payments.base import PaymentError
            except ImportError:
                PaymentError = type("PaymentError", (Exception,), {})

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
                quote_data = quote.to_dict()
                billing = _srv._get_billing()
                fee_calc = None
                if billing is not None:
                    fee_calc = billing.calculate_fee(
                        quote.total_price,
                        currency=quote.currency,
                    )
                    quote_data["kiln_fee"] = fee_calc.to_dict()
                    quote_data["total_with_fee"] = float(fee_calc.total_cost)
                quote_data.update(_srv._provider_routing_metadata(provider.name))
                quote_data["provider_quote_id"] = quote.quote_id
                try:
                    from kiln.quote_cache import cache_quote

                    cache_quote(
                        provider.name,
                        str(getattr(quote, "material", "") or material_id),
                        material_id,
                        quantity,
                        float(quote.total_price),
                        quote.currency,
                        int(quote.lead_time_days or 0),
                        quote_id=quote.quote_id,
                        metadata={
                            "file_path": file_path,
                            "shipping_country": shipping_country,
                            "shipping_options": [
                                option.to_dict() for option in quote.shipping_options
                            ],
                        },
                    )
                except Exception:
                    _logger.debug(
                        "Could not cache fulfillment quote %s",
                        quote.quote_id,
                        exc_info=True,
                    )

                # Try to authorize (hold) the fee at quote time.
                try:
                    mgr = _srv._get_payment_mgr()
                    if fee_calc is not None and mgr.available_rails:
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
                    f"Failed to get fulfillment quote: {exc}. Check fulfillment provider credentials or license."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_quote")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_quote: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def save_shipping_profile(
            name: str,
            shipping_address: dict[str, Any],
            overwrite: bool = False,
            set_default: bool = False,
            consent_to_store: bool = False,
        ) -> dict:
            """Save a local shipping profile after explicit user consent.

            Args:
                name: Profile name, e.g. ``"home"`` or ``"office"``.
                shipping_address: Full shipping contact/address dict. Keys:
                    ``first_name``, ``last_name``, ``email``, ``phone``,
                    ``street``, ``city``, ``postal_code``, ``country``;
                    include ``state`` for US addresses.
                overwrite: Replace an existing profile with the same name.
                set_default: Make this the default shipping profile.
                consent_to_store: Must be ``True``. This is the explicit
                    consent gate for storing personal contact/address data.
            """
            import kiln.server as _srv

            if not consent_to_store:
                return _srv._error_dict(
                    "Shipping profiles store personal contact/address data. "
                    "Only call this after the user explicitly asks Kiln to remember it, "
                    "then pass consent_to_store=True.",
                    code="CONSENT_REQUIRED",
                )
            try:
                profile = _save_shipping_profile(
                    name,
                    shipping_address,
                    overwrite=overwrite,
                    set_default=set_default,
                )
                return {
                    "success": True,
                    "profile": profile.to_dict(include_address=False),
                    "message": "Shipping profile saved locally with user-only file permissions.",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in save_shipping_profile")
                return _srv._error_dict(
                    f"Unexpected error in save_shipping_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def list_shipping_profiles(include_addresses: bool = False) -> dict:
            """List saved local shipping profiles.

            Args:
                include_addresses: Include full contact/address fields. Defaults
                    to ``False`` so listing profiles does not expose personal
                    details unless the user is actively reviewing them.
            """
            import kiln.server as _srv

            try:
                profiles = _list_shipping_profiles()
                return {
                    "success": True,
                    "count": len(profiles),
                    "profiles": [
                        profile.to_dict(include_address=include_addresses)
                        for profile in profiles
                    ],
                }
            except Exception as exc:
                _logger.exception("Unexpected error in list_shipping_profiles")
                return _srv._error_dict(
                    f"Unexpected error in list_shipping_profiles: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def delete_shipping_profile(name: str) -> dict:
            """Delete a saved local shipping profile.

            Args:
                name: Profile name to delete.
            """
            import kiln.server as _srv

            try:
                deleted = _delete_shipping_profile(name)
                return {
                    "success": True,
                    "deleted": deleted,
                    "message": (
                        "Shipping profile deleted."
                        if deleted
                        else "Shipping profile did not exist."
                    ),
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in delete_shipping_profile")
                return _srv._error_dict(
                    f"Unexpected error in delete_shipping_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def issue_shipping_confirmation_token(
            quote_id: str,
            shipping_option_id: str = "",
            shipping_address: dict[str, Any] | None = None,
            shipping_profile_name: str = "",
            save_profile_decision: str = "",
            save_profile_name: str = "",
            overwrite_saved_profile: bool = False,
            set_default_profile: bool = False,
            ttl_seconds: int = 600,
        ) -> dict:
            """Issue a single-use token after the user confirms shipping details.

            Call this only after showing the normalized contact/shipping address
            and selected shipping option to the user, asking whether Kiln should
            save the address as a profile, and receiving approval.
            ``fulfillment_order`` refuses to place an order without this token.

            Args:
                quote_id: Quote ID from ``fulfillment_quote``.
                shipping_option_id: Shipping option ID selected from the quote.
                shipping_address: Explicit full shipping address.
                shipping_profile_name: Saved profile name to use instead of
                    passing ``shipping_address``.
                save_profile_decision: Required when ``shipping_address`` is
                    provided directly. Pass ``"save"`` only after the user says
                    yes; pass ``"do_not_save"`` after the user says no.
                save_profile_name: Profile name to save when
                    ``save_profile_decision`` is ``"save"``.
                overwrite_saved_profile: Replace an existing profile with the
                    same name when saving.
                set_default_profile: Make the saved profile the default.
                ttl_seconds: Token lifetime, default 10 minutes.
            """
            import kiln.server as _srv

            try:
                address = _resolve_shipping_address(
                    shipping_address,
                    shipping_profile_name,
                )
                saved_profile = None
                normalized_decision = "already_saved_profile" if shipping_profile_name else ""
                if not shipping_profile_name:
                    normalized_decision = _normalize_save_profile_decision(save_profile_decision)
                    if not normalized_decision:
                        return _srv._error_dict(
                            "Before issuing a shipping confirmation token, ask the user "
                            "whether Kiln should save this shipping contact/address as a "
                            "local profile. Then pass save_profile_decision='save' or "
                            "save_profile_decision='do_not_save'.",
                            code="SAVE_PROFILE_DECISION_REQUIRED",
                        )
                    if normalized_decision == "save":
                        profile_name = save_profile_name.strip()
                        if not profile_name:
                            return _srv._error_dict(
                                "Profile name is required when save_profile_decision='save'.",
                                code="PROFILE_NAME_REQUIRED",
                            )
                        saved_profile = _save_shipping_profile(
                            profile_name,
                            address,
                            overwrite=overwrite_saved_profile,
                            set_default=set_default_profile,
                        ).to_dict(include_address=False)
                token = _issue_shipping_confirmation_token(
                    quote_id=quote_id,
                    shipping_option_id=shipping_option_id,
                    shipping_address=address,
                    ttl_seconds=ttl_seconds,
                )
                result = {
                    "success": True,
                    "token": token.token,
                    "expires_at": token.issued_at + token.ttl_seconds,
                    "ttl_seconds": ttl_seconds,
                    "shipping_address": address,
                    "save_profile_decision": normalized_decision,
                    "saved_profile": saved_profile,
                    "usage_hint": (
                        "Pass this token as shipping_confirmation_token to "
                        "fulfillment_order with the same quote, shipping option, "
                        "and shipping address/profile."
                    ),
                }
                return result
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in issue_shipping_confirmation_token")
                return _srv._error_dict(
                    f"Unexpected error in issue_shipping_confirmation_token: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def fulfillment_order(
            quote_id: str,
            shipping_option_id: str = "",
            shipping_address: dict[str, Any] | None = None,
            shipping_profile_name: str = "",
            preview_token: str = "",
            preview_file_path: str = "",
            shipping_confirmation_token: str = "",
            payment_hold_id: str = "",
            quoted_price: float = 0.0,
            quoted_currency: str = "USD",
            jurisdiction: str = "",
            business_tax_id: str = "",
        ) -> dict:
            """Place a manufacturing order based on a previous quote.

            Charges the orchestration fee BEFORE placing the order to prevent
            unpaid orders.  If order placement fails after payment, the
            charge is automatically refunded.

            Args:
                quote_id: Quote ID from ``fulfillment_quote``.
                shipping_option_id: Shipping option ID from the quote's
                    ``shipping_options`` list.
                shipping_address: Optional shipping contact/address dict for
                    provider checkout. Keys: ``first_name``, ``last_name``,
                    ``email``, ``phone``, ``street``, ``city``,
                    ``postal_code``, ``country``; include ``state`` for US.
                shipping_profile_name: Saved shipping profile name to use
                    instead of passing ``shipping_address``.
                preview_token: Token from ``issue_preview_token`` after the
                    rendered model preview was shown to and approved by the user.
                preview_file_path: Exact model file path that was previewed.
                    The preview token is validated against this file's bytes.
                shipping_confirmation_token: Token from
                    ``issue_shipping_confirmation_token`` after the contact and
                    shipping address were shown to and approved by the user.
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
            try:
                from kiln.fulfillment import FulfillmentError, OrderRequest
                from kiln.fulfillment.intelligence import QuoteValidation
            except ImportError:
                return _srv._error_dict(
                    "Fulfillment module is not available (kiln-pro required).",
                    code="NOT_AVAILABLE",
                )
            try:
                from kiln.licensing import LicenseTier
            except ImportError:
                class _DummyTier:
                    PRO = "pro"
                    ENTERPRISE = "enterprise"
                    BUSINESS = "business"
                    FREE = "free"
                LicenseTier = _DummyTier
            try:
                from kiln.payments.base import PaymentError
            except ImportError:
                PaymentError = type("PaymentError", (Exception,), {})

            try:
                normalized_shipping_address = _resolve_shipping_address(
                    shipping_address,
                    shipping_profile_name,
                )
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")

            preview_ok, preview_reason = _validate_preview_confirmation(
                preview_token=preview_token,
                preview_file_path=preview_file_path,
                consume=False,
            )
            if not preview_ok:
                return _srv._error_dict(
                    "Fulfillment order requires a human-approved rendered preview. "
                    "Show the user the preview for preview_file_path, call "
                    "issue_preview_token(file_path=preview_file_path) after approval, "
                    "then pass preview_token and preview_file_path to fulfillment_order. "
                    f"Preview check failed: {preview_reason}.",
                    code="PREVIEW_NOT_CONFIRMED",
                )

            shipping_ok, shipping_reason = _validate_shipping_confirmation_token(
                shipping_confirmation_token,
                quote_id=quote_id,
                shipping_option_id=shipping_option_id,
                shipping_address=normalized_shipping_address,
                consume=False,
            )
            if not shipping_ok:
                return _srv._error_dict(
                    "Fulfillment order requires user-confirmed contact/shipping details. "
                    "Show the user the normalized shipping_address and selected shipping "
                    "option, call issue_shipping_confirmation_token after approval, then "
                    "pass shipping_confirmation_token to fulfillment_order. "
                    f"Shipping confirmation failed: {shipping_reason}.",
                    code="SHIPPING_NOT_CONFIRMED",
                )

            if err := _srv._check_billing_auth("print"):
                return err

            from kiln.licensing import check_tier

            try:
                provider = _srv._get_fulfillment()
                if getattr(provider, "name", "") != "proxy":
                    tier_ok, tier_msg = check_tier(LicenseTier.BUSINESS)
                    if not tier_ok:
                        return {
                            "success": False,
                            "error": tier_msg,
                            "code": "LICENSE_REQUIRED",
                            "required_tier": "business",
                        }

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

                # 1. Consume confirmation tokens before any payment/provider call.
                preview_ok, preview_reason = _validate_preview_confirmation(
                    preview_token=preview_token,
                    preview_file_path=preview_file_path,
                    consume=True,
                )
                if not preview_ok:
                    return _srv._error_dict(
                        f"Preview confirmation is no longer valid: {preview_reason}. "
                        "Render/show the preview again before placing the order.",
                        code="PREVIEW_NOT_CONFIRMED",
                    )
                shipping_ok, shipping_reason = _validate_shipping_confirmation_token(
                    shipping_confirmation_token,
                    quote_id=quote_id,
                    shipping_option_id=shipping_option_id,
                    shipping_address=normalized_shipping_address,
                    consume=True,
                )
                if not shipping_ok:
                    return _srv._error_dict(
                        f"Shipping confirmation is no longer valid: {shipping_reason}. "
                        "Show the shipping details again before placing the order.",
                        code="SHIPPING_NOT_CONFIRMED",
                    )

                # 2. Determine price and calculate fee BEFORE placing.
                estimated_price = quoted_price
                currency = quoted_currency
                pay_result = None
                fee_calc = None

                # 2a. Early spend limit check (before any work).
                if estimated_price and estimated_price > 0:
                    billing = _srv._get_billing()
                    if billing is None:
                        return _srv._error_dict(
                            "Billing module is unavailable. Cannot place a paid fulfillment order.",
                            code="BILLING_UNAVAILABLE",
                        )
                    fee_estimate = billing.calculate_fee(
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

                # 3. Charge / capture payment BEFORE placing the order.
                if payment_hold_id or estimated_price > 0:
                    billing = _srv._get_billing()
                    if billing is None:
                        return _srv._error_dict(
                            "Billing module is unavailable. Cannot place a paid fulfillment order.",
                            code="BILLING_UNAVAILABLE",
                        )
                    if estimated_price > 0:
                        fee_calc = billing.calculate_fee(
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
                                    fee_calc = billing.calculate_fee(0.0)
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
                                fee_calc, _charge_id = billing.calculate_and_record_fee(
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

                # 4. Place the order AFTER payment succeeds.
                try:
                    result = provider.place_order(
                        OrderRequest(
                            quote_id=quote_id,
                            shipping_option_id=shipping_option_id,
                            shipping_address=normalized_shipping_address,
                            preview_confirmed=True,
                            shipping_confirmed=True,
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

                # 5. Build response.
                order_data = result.to_dict()
                order_data.update(
                    _srv._provider_routing_metadata(
                        provider.name,
                        provider_order_id=result.order_id or "",
                    )
                )
                if fee_calc:
                    order_data["kiln_fee"] = fee_calc.to_dict()
                    order_data["total_with_fee"] = float(fee_calc.total_cost)
                if pay_result:
                    order_data["payment"] = pay_result.to_dict()

                    if result.order_id and result.order_id != quote_id:
                        try:
                            billing = _srv._get_billing()
                            if billing is None:
                                raise RuntimeError("Billing module unavailable")
                            billing.record_charge(
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

                # 6. Price-drift check
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
            try:
                from kiln.fulfillment import FulfillmentError
            except ImportError:
                return _srv._error_dict(
                    "Fulfillment module is not available (kiln-pro required).",
                    code="NOT_AVAILABLE",
                )

            try:
                provider = _srv._get_fulfillment()
                result = provider.get_order_status(order_id)
                order_data = result.to_dict()
                order_data.update(
                    _srv._provider_routing_metadata(
                        provider.name,
                        provider_order_id=order_id,
                    )
                )
                return {
                    "success": True,
                    "order": order_data,
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
            try:
                from kiln.fulfillment import FulfillmentError
            except ImportError:
                return _srv._error_dict(
                    "Fulfillment module is not available (kiln-pro required).",
                    code="NOT_AVAILABLE",
                )
            try:
                from kiln.licensing import LicenseTier, check_tier
            except ImportError:
                class _DummyTier:
                    PRO = "pro"
                    ENTERPRISE = "enterprise"
                    BUSINESS = "business"
                    FREE = "free"
                LicenseTier = _DummyTier
                def check_tier(required, *_a, **_kw):
                    tier_label = getattr(required, "value", required) if required else "business"
                    return (False, (
                        f"This feature requires Kiln {str(tier_label).title()}. "
                        "Already subscribed? Run `kiln login` to sync this machine. "
                        "Otherwise: https://kiln3d.com/pricing"
                    ))

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
                if monitor is None:
                    return _srv._error_dict(
                        "Fulfillment monitor is not available (kiln-pro required).",
                        code="NOT_AVAILABLE",
                    )
                alerts = monitor.get_alerts()
                return {"success": True, "alerts": alerts, "count": len(alerts)}
            except Exception as exc:
                return _srv._error_dict(f"Failed to check fulfillment alerts: {exc}")

        _logger.debug("Registered fulfillment tools")


plugin = _FulfillmentToolsPlugin()
