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
import os
from pathlib import Path
from typing import Any

from kiln import _pro_order_bridge as _served
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
from kiln.fulfillment_service import (
    NOT_INCLUDED,
    NOT_INCLUDED_CODE,
    WORDED_CANCEL_REFUSALS,
    order_service,
)
from kiln.preview_gate import get_preview_gate
from kiln.tool_annotations import creates, destructive, read_only

_logger = logging.getLogger(__name__)


def _not_included() -> dict[str, Any]:
    """Every order tool's answer when Kiln's servers offer no ordering at the
    address this install asks (:data:`kiln._pro_order_bridge.NOT_OFFERED_CODE`).
    An install without the order service of its own orders THROUGH the
    servers instead (:mod:`kiln._pro_order_bridge`); this sentence is no
    longer what a plain install is told."""
    import kiln.server as _srv

    return _srv._error_dict(NOT_INCLUDED, code=NOT_INCLUDED_CODE)


def _served_miss(door: str, answer: _served.Answer) -> dict[str, Any]:
    """A served ordering door's answer when the servers gave none: the four
    causes, worded for this door, with why beside it and the servers' own
    refusal details (a card to set up, an approval link) when they sent any.
    A cancel refusal the servers already worded for the person rides as it
    is, with its code, as the local door passes it."""
    import kiln.server as _srv

    miss = answer.miss or _served.Miss("unanswered", "KILN_API_HTTP_ERROR", "not an answer")
    if _served.not_offered(miss):
        return _not_included()
    if door == "cancel" and miss.code in WORDED_CANCEL_REFUSALS and miss.detail:
        return _srv._error_dict(miss.detail, code=miss.code, extra=_served.refusal_fields(answer, door))
    return _srv._error_dict(
        _served.words(miss, door), code=miss.code or "KILN_API_HTTP_ERROR",
        extra=_served.refusal_fields(answer, door),
    )


def _material_matches(materials: list[dict[str, Any]], search: str | None, technology: str | None,
                      limit: int) -> list[dict[str, Any]]:
    """The same word-boundary filters the local materials door applies."""
    import re

    out = materials
    if search:
        pat = re.compile(r"(?<![a-z])" + re.escape(search.lower()) + r"(?![a-z])", re.IGNORECASE)
        out = [m for m in out if pat.search(str(m.get("name") or ""))]
    if technology:
        tech_pat = re.compile(r"(?<![a-z])" + re.escape(technology.lower()) + r"(?![a-z])", re.IGNORECASE)
        out = [
            m for m in out
            if tech_pat.search(str(m.get("technology") or "")) or tech_pat.search(str(m.get("name") or ""))
        ]
    return out[:limit]


def _served_materials(search: str | None, technology: str | None, limit: int) -> dict[str, Any]:
    answer = _served.materials()
    if not answer.ok:
        return _served_miss("materials", answer)
    found = [m for m in answer.body.get("materials") or [] if isinstance(m, dict)]
    picked = _material_matches(found, search, technology, limit)
    return {
        "success": True,
        "provider": str(answer.body.get("provider") or _served.DEFAULT_PROVIDER),
        "materials": picked,
        "count": len(picked),
        "served": True,
    }


def _served_quote(
    file_path: str, material_id: str, quantity: int, shipping_country: str,
    jurisdiction: str = "", business_tax_id: str = "",
) -> dict[str, Any]:
    import kiln.server as _srv

    try:
        answer = _served.quote(
            file_path, material_id, quantity, shipping_country,
            jurisdiction=jurisdiction, business_tax_id=business_tax_id,
        )
    except FileNotFoundError as exc:
        return _srv._error_dict(f"Failed to get fulfillment quote: {exc}", code="FILE_NOT_FOUND")
    except ValueError as exc:
        return _srv._error_dict(str(exc), code="UNSUPPORTED_FILE_TYPE")
    if not answer.ok:
        return _served_miss("quote", answer)
    reply: dict[str, Any] = {"success": True}
    tell = answer.body.get("tell_the_person")
    if isinstance(tell, list) and tell:
        reply["tell_the_person"] = tell
    reply["quote"] = _served.folded(answer.body, "quote")
    # Beside the quote too, so a caller that keeps only the token can place
    # the order with it (``fulfillment_order(quote_token=...)``).
    reply["quote_token"] = str(answer.body.get("quote_token") or "")
    return reply


def _served_order_checks(*, quote_id: str, quote_token: str, preview_file_path: str) -> tuple[str, dict[str, Any] | None]:
    """``(token, None)`` when this computer's record of the quote binds the
    order, else ``("", refusal)``.  Run BEFORE the person's two approvals are
    consumed, so a stale quote id costs nothing (the local path validates the
    quote first too).

    Fail-closed: the order is placed only from a quote this computer took
    (its record holds the servers' token and the file that was quoted); a
    ``quote_token`` handed in must be that token; and the file whose preview
    the person approved must be the file that was quoted.  A quote with no
    record here is refused with "quote again here", since nothing could bind
    the approval to it."""
    import kiln.server as _srv

    remembered = _served.quote_token_for(quote_id)
    quoted_file = _served.quoted_file_for(quote_id)
    if not remembered or not quoted_file:
        return "", _srv._error_dict(
            _served.quote_token_unknown_message(quote_id), code=_served.QUOTE_TOKEN_UNKNOWN_CODE,
        )
    if quote_token and quote_token != remembered:
        return "", _srv._error_dict(
            f"The quote_token handed in is not the one Kiln holds for quote {quote_id!r}, so the "
            "order was not placed. Pass the token that quote returned, or quote_id alone.",
            code="QUOTE_TOKEN_MISMATCH",
        )
    if not preview_file_path or not _same_file(quoted_file, preview_file_path):
        return "", _srv._error_dict(
            "The preview the person approved is for a different file than the one quoted "
            f"({Path(preview_file_path).name or 'no file'} was previewed, {Path(quoted_file).name} was quoted), "
            "so the order was not placed. Show the quoted file's preview and approve that.",
            code="PREVIEW_NOT_CONFIRMED",
        )
    return remembered, None


def _served_order(*, token: str, quote_id: str, shipping_option_id: str, shipping_address: dict[str, str]) -> dict[str, Any]:
    """Place the order through Kiln's servers, which hold the quote and
    charge Kiln's fee themselves.  The tax jurisdiction and business tax id
    were pinned to the quote when the servers priced it (``fulfillment_quote``
    takes them on a served install), so they are not sent again here."""
    answer = _served.place(token, quote_id, shipping_option_id, shipping_address)
    if not answer.ok:
        return _served_miss("order", answer)
    reply: dict[str, Any] = {"success": True}
    tell = answer.body.get("tell_the_person")
    if isinstance(tell, list) and tell:
        reply["tell_the_person"] = tell
    reply["order"] = _served.folded(answer.body, "order")
    return reply


def _served_order_status(order_id: str) -> dict[str, Any]:
    answer = _served.order_status(order_id)
    if not answer.ok:
        return _served_miss("status", answer)
    reply: dict[str, Any] = {"success": True}
    tell = answer.body.get("tell_the_person")
    if isinstance(tell, list) and tell:
        reply["tell_the_person"] = tell
    reply["order"] = _served.folded(answer.body, "order")
    return reply


def _served_cancel(order_id: str) -> dict[str, Any]:
    answer = _served.cancel(order_id)
    if not answer.ok:
        return _served_miss("cancel", answer)
    return {"success": True, "order": _served.folded(answer.body, "order")}


def _same_file(a: str, b: str) -> bool:
    """Whether two paths name one file, symlinks followed; two paths that
    cannot both be resolved are compared as written."""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return str(a) == str(b)


#: An order in one of these states on the servers' list is an alert.
_ALERT_STATES = frozenset({"cancelled", "failed"})


def _served_alerts() -> dict[str, Any]:
    """Alerts read off the person's own orders on Kiln's servers: an order
    the print service reports cancelled or failed, and one it did not answer
    about just now.  Honest bound: a stalled order (stuck past its lead
    time) is the local monitor's judgement and is not made here."""
    answer = _served.orders(25)
    if not answer.ok:
        return _served_miss("orders", answer)
    alerts: list[dict[str, Any]] = []
    entries = [e for e in answer.body.get("orders") or [] if isinstance(e, dict)]
    for entry in entries:
        now = entry.get("now") if isinstance(entry.get("now"), dict) else {}
        state = str(now.get("status") or entry.get("recorded_status") or "").lower()
        if state in _ALERT_STATES:
            alert = {"order_id": entry.get("order_id"), "kind": state, "status": state}
        elif entry.get("unanswered"):
            alert = {
                "order_id": entry.get("order_id"), "kind": "unanswered",
                "message": "The print service didn't answer about this order just now.",
            }
        else:
            continue
        if isinstance(entry.get("tell_the_person"), list):
            alert["tell_the_person"] = entry["tell_the_person"]
        alerts.append(alert)
    return {
        "success": True, "alerts": alerts, "count": len(alerts), "orders_checked": len(entries),
        "source": "kiln_servers",
    }


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


def _like_for_like_prices(
    quoted_price: float,
    result: Any,
    *,
    quote_id: str,
    shipping_option_id: str,
) -> tuple[float, float] | None:
    """The quoted and placed prices to compare for a price change, or ``None``.

    A quote's price is the print price alone; a placed order states what
    the print service's checkout charges, which adds shipping and any tax.
    They compare like with like only when the order states its total before
    tax and the quote recorded the shipping option that was chosen: then
    it is the print price plus that shipping against the checkout's total
    before tax (tax is added at the checkout and is never a change of
    price).  Otherwise there is nothing fair to compare, and ``None`` says
    so rather than reading shipping and tax as a price change.
    """
    before_tax = getattr(result, "checkout_net", None)
    if not isinstance(before_tax, (int, float)):
        return None
    try:
        from kiln.quote_cache import get_cached_quote_by_id

        cached = get_cached_quote_by_id(quote_id)
    except Exception:
        cached = None
    options = ((getattr(cached, "metadata", None) or {}).get("shipping_options")) or []
    shipping = next(
        (
            option.get("price")
            for option in options
            if isinstance(option, dict) and option.get("id") == shipping_option_id
        ),
        None,
    )
    if not isinstance(shipping, (int, float)):
        return None
    return quoted_price + float(shipping), float(before_tax)


#: What a person who did not pay is told about Kiln's fee, when nothing was
#: refunded on the spot: an unpaid order expires, and Kiln gives its fee back
#: then (Terms §5.6).
_FEE_IF_UNPAID = "If you don't pay, any fee Kiln charged for it comes back automatically once the order expires."


def _price_change_line(quoted: float, placed: float, currency: str, fee_line: str) -> str:
    """The order is placed and its price changed: say it plainly, keep the choice theirs."""
    return (
        f"Your order was placed, but the checkout total before tax is {placed:.2f} {currency}, "
        f"not the quoted {quoted:.2f} {currency}. Pay only if that's fine; if not, just don't "
        f"pay: an unpaid order is never made and expires on its own. {fee_line}"
    ).strip()


def _known_prices_line(quoted_price: float, result: Any, currency: str) -> str:
    """What is known when the two prices cannot be compared like with like."""
    if getattr(result, "checkout_total", None) is None and isinstance(result.total_price, (int, float)):
        return (
            f"The print service's total for this order is {result.total_price:.2f} {currency}; "
            f"the quoted {quoted_price:.2f} {currency} was the print price alone, before "
            "shipping and tax. Check the total on its checkout before you pay."
        )
    return (
        f"The quoted {quoted_price:.2f} {currency} was the print price alone, before shipping "
        "and tax. Check the total on the print service's checkout before you pay."
    )


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

        @mcp.tool(annotations=read_only("List service materials"))
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
            """
            import re

            import kiln.server as _srv

            service = order_service()
            if service is None:
                return _served_materials(search, technology, limit)
            FulfillmentError = service.FulfillmentError

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

        @mcp.tool(annotations=creates("Get a manufacturing quote"))
        def fulfillment_quote(
            file_path: str,
            material_id: str,
            quantity: int = 1,
            shipping_country: str = "US",
            jurisdiction: str = "",
            business_tax_id: str = "",
        ) -> dict:
            """Get a manufacturing quote for a 3D model from Craftcloud.

            Args:
                file_path: Absolute path to the model file (STL, 3MF, OBJ).
                material_id: Material ID from ``fulfillment_materials``.
                quantity: Number of copies to print (default 1).
                shipping_country: ISO country code for shipping (default "US").
                jurisdiction: Buyer's tax region (e.g. ``"US-CA"``, ``"DE"``),
                    for a total with tax.  On an install that orders through
                    Kiln's servers it is pinned to the quote here, so the
                    order is taxed as the quote said; a local order service
                    takes it at ``fulfillment_order`` instead.
                business_tax_id: The buyer's business tax id (EU VAT, AU ABN)
                    for a reverse-charge order; pinned the same way.

            Uploads the model, returns pricing from Craftcloud's network of 150+
            print services, including unit price, total, lead time, and shipping
            options. A Kiln orchestration fee is shown separately so
            the user sees the full cost before committing.

            If a payment method is linked, a hold is placed on the fee amount
            at quote time (Stripe auth-and-capture).  The hold is captured
            when the order is placed via ``fulfillment_order``, or released
            if the user doesn't proceed.

            The reply opens with ``tell_the_person``: the price and what it
            is made of, timing, what more copies would cost, and the print
            service's order terms with a link to them, already worded for
            the person. Pass every line on to them word for word, in order,
            before they decide; do not summarise, shorten or drop any.
            ``quote.price_sheet`` holds the same numbers as data.

            Use the returned ``quote_id`` with ``fulfillment_order`` to place the
            order.  On an install that orders through Kiln's servers the
            reply also carries ``quote_token``; Kiln keeps it beside the
            quote, and ``fulfillment_order`` accepts it too.
            """
            import kiln.server as _srv

            service = order_service()
            if service is None:
                return _served_quote(
                    file_path, material_id, quantity, shipping_country,
                    jurisdiction=jurisdiction, business_tax_id=business_tax_id,
                )
            FulfillmentError = service.FulfillmentError
            QuoteRequest = service.QuoteRequest
            try:
                from kiln_pro.payments.base import PaymentError
            except ImportError:
                PaymentError = type("PaymentError", (Exception,), {})

            try:
                provider = _srv._get_fulfillment()
                quote = provider.get_quote(
                    QuoteRequest(
                        file_path=file_path,
                        material_id=material_id,
                        file_name=Path(file_path).name,
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
                # The one price sheet every door serves, built by the order
                # service so this tool never folds its own total.
                routing = _srv._provider_routing_metadata(
                    provider.name, shipping_country=shipping_country,
                )
                sheet = getattr(service, "price_sheet", None)
                message = getattr(service, "quote_message", None)
                tell: list[str] = []
                if sheet is not None:
                    kiln_fee = 0.0
                    if fee_calc is not None:
                        kiln_fee = float(
                            getattr(fee_calc, "fee_amount", 0) + getattr(fee_calc, "tax_amount", 0)
                        )
                    # How the price is shown never costs the person the quote.
                    try:
                        quote_data["price_sheet"] = sheet(quote, kiln_fee=kiln_fee)
                        if message is not None:
                            tell = message(quote, quote_data["price_sheet"], routing)
                    except Exception:
                        _logger.warning("Could not word the price for quote %s", quote.quote_id, exc_info=True)
                quote_data.update(routing)
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
                            # Absolute, so the quote is found again from
                            # another working directory: the reader
                            # compares absolute paths.
                            "file_path": os.path.abspath(file_path),
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
                    from kiln.fulfillment_service import server_takes_fee

                    if fee_calc is not None and mgr.available_rails and not server_takes_fee(provider):
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

                # The message first, already worded for the person.
                reply: dict = {"success": True}
                if tell:
                    reply["tell_the_person"] = tell
                reply["quote"] = quote_data
                return reply
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

        @mcp.tool(annotations=destructive("Save a shipping profile"))
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
            if err := _srv._check_auth("write"):
                return err

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

        @mcp.tool(annotations=read_only("List shipping profiles"))
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

        @mcp.tool(annotations=destructive("Delete a shipping profile"))
        def delete_shipping_profile(name: str) -> dict:
            """Delete a saved local shipping profile.

            Args:
                name: Profile name to delete.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("write"):
                return err

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

        @mcp.tool(annotations=destructive("Confirm shipping details"))
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
            if err := _srv._check_auth("write"):
                return err

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

        @mcp.tool(annotations=destructive("Place a manufacturing order"))
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
            quote_token: str = "",
        ) -> dict:
            """Place a manufacturing order based on a previous quote.

            Charges the orchestration fee BEFORE placing the order to prevent
            unpaid orders.  If order placement fails after payment, the
            charge is automatically refunded.  On an install that orders
            through Kiln's servers, the servers charge the fee when they
            place the order and the reply says what they charged.

            Args:
                quote_id: Quote ID from ``fulfillment_quote``.
                quote_token: The ``quote_token`` a quote through Kiln's
                    servers returned.  Optional: Kiln keeps it beside the
                    quote, so ``quote_id`` alone works; when passed it must
                    be that token.  An order is placed only from a quote
                    taken on this computer.
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
                    (the fee is calculated from it when no ``payment_hold_id``
                    is provided).  Required when ``payment_hold_id`` is
                    empty and a payment rail is configured.
                quoted_currency: Currency of ``quoted_price`` (default USD).
                jurisdiction: Buyer's region (e.g. ``"US-CA"``, ``"DE"``, ``"AU"``).
                    When provided, the response includes an accurate total with
                    tax so the user sees exactly what they'll pay — no hidden
                    fees.  Use ``tax_jurisdictions`` to see all supported codes.
                    On an install that orders through Kiln's servers, pass it
                    to ``fulfillment_quote`` instead: the servers pin it to the
                    quote and the order is taxed as the quote said.
                business_tax_id: If the buyer is a registered business, their
                    tax ID (EU VAT number, AU ABN, etc.).  Businesses in the
                    EU, UK, Australia, and Japan are tax-exempt via reverse
                    charge — the tax line shows $0.00.

            Use ``fulfillment_order_status`` to track progress after placing.

            Nothing is made until the person pays on the print service's
            checkout, so the reply opens with ``tell_the_person``: the link to
            pay first, then the checkout total with any tax, the print
            service's order terms and a link to them. Pass every line on to
            the person word for word.

            If the person asks whether they can pay with a particular method,
            answer from ``provider_pay_methods`` in the reply: yes for one it
            lists; for any other, say Kiln doesn't know and that the checkout
            page shows every option for their country. Never claim a method
            that isn't listed.
            """
            import kiln.server as _srv

            service = order_service()
            served = service is None
            if not served:
                FulfillmentError = service.FulfillmentError
                OrderRequest = service.OrderRequest
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
                from kiln_pro.payments.base import PaymentError
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

            if served:
                # The money gate is the servers': they verify the sign-in,
                # hold the quote, apply the fee allowance and the spend caps,
                # and mint both confirmations.  This install's own API-key
                # layer (KILN_AUTH_ENABLED, for a server reachable over a
                # network) applies here as it does to every tool when it is
                # on, and is not demanded: a person at their own agent has
                # no key to set, and demanding one refuses every default
                # install's first order.
                if err := _srv._check_auth("print"):
                    return err
                # This computer's record of the quote binds the order; read
                # it before the approvals are spent, so a stale quote id
                # costs the person nothing.
                token, refused = _served_order_checks(
                    quote_id=quote_id, quote_token=quote_token, preview_file_path=preview_file_path,
                )
                if refused is not None:
                    return refused
                # Both approvals checked here, as above; the servers then
                # mint their own two confirmations for the quote and place
                # the order (kiln._pro_order_bridge.place).  The local
                # tokens are consumed first, as the local path consumes
                # them before it reaches the print service.
                for ok, why in (
                    _validate_preview_confirmation(
                        preview_token=preview_token, preview_file_path=preview_file_path, consume=True,
                    ),
                    _validate_shipping_confirmation_token(
                        shipping_confirmation_token, quote_id=quote_id,
                        shipping_option_id=shipping_option_id,
                        shipping_address=normalized_shipping_address, consume=True,
                    ),
                ):
                    if not ok:
                        return _srv._error_dict(
                            f"A confirmation is no longer valid: {why}. Show the preview and the "
                            "shipping details again before placing the order.",
                            code="PREVIEW_NOT_CONFIRMED",
                        )
                return _served_order(
                    token=token, quote_id=quote_id, shipping_option_id=shipping_option_id,
                    shipping_address=normalized_shipping_address,
                )

            if err := _srv._check_billing_auth("print"):
                return err

            from kiln.licensing import check_tier

            try:
                provider = _srv._get_fulfillment()
                if getattr(provider, "name", "") != "proxy":
                    tier_ok, tier_msg = check_tier(LicenseTier.BUSINESS)
                    if not tier_ok:
                        from kiln.tiers_and_terms import paywall_fields

                        return {
                            "success": False,
                            "error": tier_msg,
                            "code": "LICENSE_REQUIRED",
                            "required_tier": "business",
                            **paywall_fields("business"),
                        }

                # 0. Validate quote is still valid
                quote_validation: Any = None
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

                # 2. Determine price and calculate fee BEFORE placing.  When
                #    Kiln's hosted server places the order it charges the fee
                #    itself, so nothing here records, holds or charges it.
                from kiln.fulfillment_service import fee_taken_by_server, server_takes_fee

                server_fee = server_takes_fee(provider)
                estimated_price = 0.0 if server_fee else quoted_price
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
                if not server_fee and (payment_hold_id or estimated_price > 0):
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
                routing = _srv._provider_routing_metadata(
                    provider.name,
                    provider_order_id=result.order_id or "",
                    shipping_country=str((normalized_shipping_address or {}).get("country") or ""),
                )
                order_data.update(routing)
                message = getattr(service, "order_message", None)
                tell = []
                if message is not None:
                    try:
                        tell = message(result, routing)
                    except Exception:
                        _logger.warning("Could not word order %s", result.order_id, exc_info=True)
                if fee_calc:
                    order_data["kiln_fee"] = fee_calc.to_dict()
                    order_data["total_with_fee"] = float(fee_calc.total_cost)
                upstream_fee = fee_taken_by_server(result)
                if upstream_fee is not None:
                    order_data["kiln_fee"] = upstream_fee
                elif server_fee:
                    order_data.setdefault("warnings", []).append(
                        "Kiln's hosted service placed this order but didn't say what fee it charged; "
                        "Kiln charged nothing for it on this machine."
                    )
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

                # 6. The price, checked after placement: the order exists
                #    whatever this finds, so it is never reported as
                #    blocked.  A real change is said plainly with the pay
                #    step kept; prices that cannot be compared like with
                #    like are stated, never judged.
                response_warnings: list[str] = []
                if quote_validation and quote_validation.warnings:
                    response_warnings.extend(quote_validation.warnings)

                if result.total_price is not None and quote_validation:
                    from kiln.fulfillment.intelligence import _check_price_drift

                    currency = getattr(result, "currency", None) or quoted_currency
                    prices = _like_for_like_prices(
                        quote_validation.quoted_price,
                        result,
                        quote_id=quote_id,
                        shipping_option_id=shipping_option_id,
                    )
                    if prices is None:
                        known = _known_prices_line(quote_validation.quoted_price, result, currency)
                        tell = [*tell[:1], known, *tell[1:]]
                    else:
                        quoted, placed = prices
                        drift_warning, _beyond_limit = _check_price_drift(quoted, placed)
                        if drift_warning:
                            # The order stays payable, so Kiln's fee follows
                            # the order as it does everywhere: it comes back
                            # when the order is cancelled or expires unpaid,
                            # never on the spot.  A refund here would let the
                            # order be paid and made with the fee returned.
                            _logger.warning(
                                "Price changed for quote %s: quoted %.2f, checkout before tax %.2f",
                                quote_id, quoted, placed,
                            )
                            change = _price_change_line(quoted, placed, currency, _FEE_IF_UNPAID)
                            tell = [change, *tell]
                            response_warnings.append(change)
                            order_data["price_change"] = {
                                "quoted": round(quoted, 2),
                                "checkout_before_tax": round(placed, 2),
                                "currency": currency,
                            }

                if response_warnings:
                    order_data["warnings"] = [*order_data.get("warnings", []), *response_warnings]

                reply = {"success": True}
                if tell:
                    reply["tell_the_person"] = tell
                reply["order"] = order_data
                return reply
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_order")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_order: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool(annotations=read_only("Order status"))
        def fulfillment_order_status(order_id: str) -> dict:
            """Check the status of a fulfillment order.

            Args:
                order_id: Order ID from ``fulfillment_order``.

            Returns current order state, tracking info, and estimated delivery.

            The reply opens with ``tell_the_person``: where the order is and,
            while it is unpaid, the link to pay for it. Pass every line on to
            the person word for word.

            Nothing is made until an order is paid on the print service's
            checkout, and Kiln cannot pay it for them. When the person wants to
            pay, asks how to pay, or asks about an order that is not paid yet,
            call this right away for that order and give them the link to pay
            first, before anything else, without waiting to be asked for it.

            If the person asks whether they can pay with a particular method,
            answer from ``provider_pay_methods`` in the reply: yes for one it
            lists; for any other, say Kiln doesn't know and that the checkout
            page shows every option for their country. Never claim a method
            that isn't listed.
            """
            import kiln.server as _srv

            service = order_service()
            if service is None:
                return _served_order_status(order_id)
            FulfillmentError = service.FulfillmentError

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
                # The message first, worded by the order service.  How a
                # status is worded never costs the person the status.
                reply: dict = {"success": True}
                message = getattr(service, "status_message", None)
                if message is not None:
                    try:
                        tell = message(result)
                    except Exception:
                        _logger.warning("Could not word the status of %s", order_id, exc_info=True)
                        tell = []
                    if tell:
                        reply["tell_the_person"] = tell
                reply["order"] = order_data
                return reply
            except (FulfillmentError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to check order status: {exc}. Verify the order_id is correct."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_order_status")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_order_status: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool(annotations=destructive("Cancel a manufacturing order"))
        def fulfillment_cancel(order_id: str) -> dict:
            """Cancel a fulfillment order (if still cancellable).

            Args:
                order_id: Order ID to cancel.

            Only orders that have not yet shipped can be cancelled. When the
            print service has nothing to cancel (an unpaid order is never
            made) or cancels only on a written request, the error message
            says which and how; pass it on to the person as it is.
            """
            import kiln.server as _srv

            service = order_service()
            if service is None:
                # The servers cancel only the caller's own orders and word
                # "nothing to cancel" themselves; the plan rule is theirs
                # too.  This install's API-key layer applies when it is on.
                if err := _srv._check_auth("print"):
                    return err
                return _served_cancel(order_id)
            FulfillmentError = service.FulfillmentError
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
                    from kiln.tiers_and_terms import tier_required_message

                    tier_label = getattr(required, "value", required) if required else "business"
                    return (
                        False,
                        tier_required_message("This feature", str(tier_label)),
                    )

            if err := _srv._check_billing_auth("print"):
                return err
            tier_ok, tier_msg = check_tier(LicenseTier.BUSINESS)
            if not tier_ok:
                from kiln.tiers_and_terms import paywall_fields, signin_hint_fields

                return {
                    "success": False,
                    "error": tier_msg,
                    "code": "LICENSE_REQUIRED",
                    "required_tier": "business",
                    **signin_hint_fields(),
                    **paywall_fields("business"),
                }
            try:
                provider = _srv._get_fulfillment()
                result = provider.cancel_order(order_id)
                return {
                    "success": True,
                    "order": result.to_dict(),
                }
            except (FulfillmentError, RuntimeError) as exc:
                code = getattr(exc, "code", None)
                if code in WORDED_CANCEL_REFUSALS:
                    # Already worded for the person, with what to do next.
                    return _srv._error_dict(str(exc), code=code)
                return _srv._error_dict(
                    f"Failed to cancel order: {exc}. The order may have already shipped."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in fulfillment_cancel")
                return _srv._error_dict(
                    f"Unexpected error in fulfillment_cancel: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool(annotations=read_only("Order alerts"))
        def fulfillment_alerts() -> dict:
            """Check for fulfillment order alerts (stalled, failed, cancelled orders).

            Returns any active alerts from the background fulfillment monitor.
            Alerts are generated when orders are cancelled/failed by the provider
            or have been stuck in processing longer than the expected lead time.
            """
            import kiln.server as _srv

            if order_service() is None:
                return _served_alerts()
            try:
                monitor = _srv._get_fulfillment_monitor()
                if monitor is None:
                    return _served_alerts()
                alerts = monitor.get_alerts()
                return {"success": True, "alerts": alerts, "count": len(alerts)}
            except Exception as exc:
                return _srv._error_dict(f"Failed to check fulfillment alerts: {exc}")

        _logger.debug("Registered fulfillment tools")


plugin = _FulfillmentToolsPlugin()
