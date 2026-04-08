"""Billing, payment, and invoicing tools plugin.

Extracts billing-domain MCP tools from server.py into a focused plugin
module.  All tools delegate to the ``_billing``, ``_payment_mgr``, and
``_billing_alert_mgr`` lazy singletons defined in server.py.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _BillingToolsPlugin:
    """Billing, payment, invoicing, and payment-setup tools.

    Tools:
        - billing_summary
        - billing_setup_url
        - billing_status
        - billing_history
        - billing_invoice
        - billing_export
        - check_payment_status
        - refund_payment
        - billing_check_setup
        - billing_alerts
        - billing_delete_data
    """

    @property
    def name(self) -> str:
        return "billing_tools"

    @property
    def description(self) -> str:
        return "Billing, payment, invoicing, and payment-setup tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register billing tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # billing_summary
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_summary() -> dict:
            """Get a summary of Kiln orchestration fees for the current month.

            Shows total fees collected, number of outsourced orders, free tier
            usage, and the current fee policy.  Only orders placed through
            external fulfillment services incur fees -- all local printing is free.

            Available on all tiers -- anyone who transacts can view their billing.
            """
            try:
                revenue = _srv._get_billing().monthly_revenue()
                policy = _srv._get_billing()._policy
                return {
                    "success": True,
                    "month_revenue": revenue,
                    "fee_policy": {
                        "orchestration_fee_percent": policy.network_fee_percent,
                        "network_fee_percent": policy.network_fee_percent,
                        "min_fee_usd": policy.min_fee_usd,
                        "max_fee_usd": policy.max_fee_usd,
                        "free_tier_jobs": policy.free_tier_jobs,
                        "currency": policy.currency,
                    },
                    "outsourced_jobs_this_month": _srv._get_billing().network_jobs_this_month(),
                    "network_jobs_this_month": _srv._get_billing().network_jobs_this_month(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in billing_summary")
                return _srv._error_dict(
                    f"Unexpected error in billing_summary: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # billing_setup_url
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_setup_url(rail: str = "stripe") -> dict:
            """Get a URL to link a payment method for Kiln orchestration fees.

            Args:
                rail: Payment rail -- ``"stripe"`` for credit card, ``"crypto"``
                    for USDC on Solana/Base.

            Returns the setup URL.  Open it in a browser to complete payment
            method setup.  After setup, Kiln automatically charges the orchestration
            fee on each outsourced manufacturing order.
            """
            if err := _srv._check_billing_auth("billing"):
                return err
            try:
                mgr = _srv._get_payment_mgr()
                url = mgr.get_setup_url(rail=rail)
                # Include setup_intent_id so the agent can poll for completion.
                setup_intent_id = None
                provider = mgr.get_provider(rail)
                if provider and hasattr(provider, "_pending_setup_intent_id"):
                    setup_intent_id = provider._pending_setup_intent_id
                return {
                    "success": True,
                    "setup_url": url,
                    "rail": rail,
                    "setup_intent_id": setup_intent_id,
                    "next_step": (
                        "Open the setup_url in a browser to complete card setup. "
                        "After the user finishes, call billing_check_setup to "
                        "activate the payment method."
                    ),
                }
            except Exception as exc:
                # Match the original two-tier handling: PaymentError gets its
                # own code attribute, everything else is INTERNAL_ERROR.
                try:
                    from kiln.payments.base import PaymentError
                except ImportError:
                    PaymentError = None
                if PaymentError is not None and isinstance(exc, PaymentError):
                    return _srv._error_dict(
                        f"Failed to generate billing setup URL: {exc}",
                        code=getattr(exc, "code", "PAYMENT_ERROR"),
                    )
                _logger.exception("Unexpected error in billing_setup_url")
                return _srv._error_dict(
                    f"Unexpected error in billing_setup_url: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # billing_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_status() -> dict:
            """Get enriched billing status including payment method info.

            Returns payment method details, monthly spend, spend limits,
            available payment rails, and fee policy.  More detailed than
            ``billing_summary`` -- includes payment infrastructure state.
            """
            if err := _srv._check_billing_auth("billing"):
                return err
            try:
                from kiln.cli.config import get_or_create_user_id

                user_id = get_or_create_user_id()
                mgr = _srv._get_payment_mgr()
                data = mgr.get_billing_status(user_id)
                return {"success": True, **data}
            except Exception as exc:
                _logger.exception("Unexpected error in billing_status")
                return _srv._error_dict(
                    f"Unexpected error in billing_status: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # billing_history
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_history(limit: int = 20) -> dict:
            """Get recent billing charge history with payment outcomes.

            Available on all tiers -- anyone who transacts can view their history.

            Args:
                limit: Maximum number of records to return (default 20).

            Returns charge records including order cost, fee amount, payment
            rail, payment status, and timestamps.
            """
            if err := _srv._check_billing_auth("billing"):
                return err
            try:
                mgr = _srv._get_payment_mgr()
                charges = mgr.get_billing_history(limit=limit)
                return {"success": True, "charges": charges, "count": len(charges)}
            except Exception as exc:
                _logger.exception("Unexpected error in billing_history")
                return _srv._error_dict(
                    f"Unexpected error in billing_history: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # billing_invoice
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_invoice(charge_id: str = "", job_id: str = "") -> dict:
            """Generate an invoice/receipt for a billing charge.

            Args:
                charge_id: The charge ID (from ``billing_history``).
                job_id: Or the job/order ID to look up.

            Returns the invoice as structured data with a human-readable
            receipt and tamper-detection checksum.
            """
            if err := _srv._check_billing_auth("billing"):
                return err
            try:
                try:
                    from kiln.billing_invoice import generate_invoice
                except ImportError:
                    return _srv._error_dict("This feature requires kiln-pro", code="PRO_REQUIRED")

                if charge_id:
                    charges = _srv._get_billing().list_charges(limit=500)
                    charge = next((c for c in charges if c.get("id") == charge_id), None)
                elif job_id:
                    charges = _srv._get_billing().list_charges(limit=500)
                    charge = next((c for c in charges if c.get("job_id") == job_id), None)
                else:
                    return _srv._error_dict(
                        "billing_invoice requires either charge_id (from billing_history) "
                        "or job_id (from fulfillment_order) to look up the charge."
                    )

                if charge is None:
                    return _srv._error_dict("Charge not found.", code="NOT_FOUND")

                invoice = generate_invoice(charge)
                return {
                    "success": True,
                    "invoice": invoice.to_dict(),
                    "receipt_text": invoice.to_receipt_text(),
                }
            except Exception as exc:
                _logger.exception("Error generating invoice")
                return _srv._error_dict(f"Failed to generate invoice: {exc}")

        # ------------------------------------------------------------------
        # billing_export
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_export(format: str = "csv", limit: int = 100) -> dict:
            """Export billing history for accounting.

            Args:
                format: Export format -- ``"csv"`` or ``"json"``.
                limit: Maximum charges to export (default 100).

            Returns billing data suitable for import into accounting
            software (QuickBooks, Xero, etc.).
            """
            if err := _srv._check_billing_auth("billing"):
                return err
            try:
                try:
                    from kiln.billing_invoice import export_billing_csv, generate_invoices
                except ImportError:
                    return _srv._error_dict("This feature requires kiln-pro", code="PRO_REQUIRED")

                charges = _srv._get_billing().list_charges(limit=limit)

                if format == "csv":
                    csv_data = export_billing_csv(charges)
                    return {
                        "success": True,
                        "format": "csv",
                        "data": csv_data,
                        "count": len(charges),
                    }
                else:
                    invoices = generate_invoices(charges)
                    return {
                        "success": True,
                        "format": "json",
                        "invoices": [inv.to_dict() for inv in invoices],
                        "count": len(invoices),
                    }
            except Exception as exc:
                _logger.exception("Error exporting billing data")
                return _srv._error_dict(f"Failed to export billing data: {exc}")

        # ------------------------------------------------------------------
        # check_payment_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def check_payment_status(payment_id: str) -> dict:
            """Check the current status of a pending payment by ID.

            Use this after a payment returns ``processing`` status to poll
            for completion.  Works for both Stripe and Circle payments.

            Args:
                payment_id: The payment/transfer ID to check.
            """
            if err := _srv._check_auth("billing"):
                return err
            try:
                mgr = _srv._get_payment_mgr()
                # Try each registered provider until one recognises the ID
                for name in mgr.available_rails:
                    provider = mgr.get_provider(name)
                    if provider is None:
                        continue
                    try:
                        result = provider.get_payment_status(payment_id)
                        return {
                            "success": True,
                            "payment_id": result.payment_id,
                            "status": result.status.value,
                            "amount": result.amount,
                            "currency": result.currency.value,
                            "rail": result.rail.value if result.rail else name,
                            "tx_hash": result.tx_hash,
                            "provider": name,
                        }
                    except Exception as exc:
                        _logger.debug(
                            "Failed to check payment %s on provider %s: %s",
                            payment_id,
                            name,
                            exc,
                        )
                        continue
                return _srv._error_dict(
                    f"Payment {payment_id!r} not found on any registered provider.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in check_payment_status")
                return _srv._error_dict(
                    f"Unexpected error in check_payment_status: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # refund_payment
        # ------------------------------------------------------------------

        @mcp.tool()
        def refund_payment(payment_id: str, reason: str = "") -> dict:
            """Request a refund for a completed payment.

            Args:
                payment_id: The payment ID from the original charge
                    (found in ``billing_history`` or the ``fulfillment_order`` response).
                reason: Optional reason for the refund (for audit trail).

            Refunds are processed through the original payment rail (Stripe or
            Circle/USDC).  Stripe refunds are typically instant; USDC refunds
            may take a few minutes to confirm on-chain.

            Only completed payments can be refunded.  Authorized holds should
            be released via the fulfillment cancellation flow instead.
            """
            if err := _srv._check_billing_auth("admin"):
                return err
            try:
                from kiln.events import EventType
                from kiln.payments.base import PaymentError

                mgr = _srv._get_payment_mgr()
                # Try each provider until one recognises the payment_id.
                for provider_name in mgr.available_rails:
                    provider = mgr.get_provider(provider_name)
                    if provider is None:
                        continue
                    try:
                        result = provider.refund_payment(payment_id)
                        # Emit refund event.
                        _srv._get_event_bus().publish(
                            EventType.PAYMENT_REFUNDED,
                            {
                                "payment_id": payment_id,
                                "amount": result.amount,
                                "rail": provider_name,
                                "reason": reason,
                                "status": result.status.value,
                            },
                            source="billing",
                        )
                        _logger.info(
                            "Refund processed: payment=%s amount=%.2f rail=%s reason=%s",
                            payment_id,
                            result.amount,
                            provider_name,
                            reason or "(none)",
                        )
                        return {
                            "success": True,
                            "refund": result.to_dict(),
                            "message": (
                                f"Refund of ${result.amount:.2f} initiated via {provider_name}. "
                                "Stripe refunds are typically instant. "
                                "USDC refunds may take a few minutes to confirm."
                            ),
                        }
                    except PaymentError:
                        continue  # Not this provider's payment.
                    except Exception as exc:
                        _logger.debug(
                            "Failed to refund payment %s on provider %s: %s",
                            payment_id,
                            provider_name,
                            exc,
                        )
                        continue
                return _srv._error_dict(
                    f"Payment {payment_id!r} not found in any registered provider. "
                    "Verify the payment_id from billing_history.",
                    code="PAYMENT_NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in refund_payment")
                return _srv._error_dict(f"Refund failed: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # billing_check_setup
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_check_setup() -> dict:
            """Check if billing setup is complete after user visited the setup URL.

            After calling billing_setup_url and the user completes card setup in
            their browser, call this tool to activate the payment method.  Polls
            the Stripe SetupIntent for completion and configures the payment
            method for future charges.
            """
            if err := _srv._check_billing_auth("billing"):
                return err

            try:
                mgr = _srv._get_payment_mgr()
                provider = mgr.get_provider("stripe")
                if provider is None:
                    return _srv._error_dict(
                        "Stripe provider not configured.",
                        code="NO_PROVIDER",
                    )
                if not hasattr(provider, "poll_setup_intent"):
                    return _srv._error_dict(
                        "Provider does not support setup polling.",
                        code="UNSUPPORTED",
                    )
                pm_id = provider.poll_setup_intent()
                if pm_id is None:
                    return {
                        "success": False,
                        "status": "pending",
                        "message": (
                            "Setup not yet complete.  Ask the user to finish "
                            "card setup in their browser, then call this tool again."
                        ),
                    }
                # Activate the payment method on the provider.
                provider.set_payment_method(pm_id)
                # Persist to config so it survives restarts.
                from kiln.cli.config import save_billing_config

                save_billing_config(
                    {
                        "stripe_payment_method_id": pm_id,
                        "stripe_customer_id": getattr(provider, "_customer_id", None),
                    }
                )
                return {
                    "success": True,
                    "status": "active",
                    "payment_method_id": pm_id,
                    "message": "Payment method activated. Billing is now enabled.",
                }
            except Exception as exc:
                _logger.exception("Unexpected error in billing_check_setup")
                return _srv._error_dict(
                    f"Unexpected error in billing_check_setup: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # billing_alerts
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_alerts() -> dict:
            """Check billing system health and active alerts.

            Returns payment failure alerts, spend limit violations, and
            overall payment system health metrics.
            """
            try:
                alert_mgr = _srv._get_billing_alert_mgr()
                return {
                    "success": True,
                    "health": alert_mgr.get_health_summary(),
                    "alerts": alert_mgr.get_alerts(),
                }
            except Exception as exc:
                _logger.exception("Error checking billing alerts")
                return _srv._error_dict(f"Failed to check billing alerts: {exc}")

        # ------------------------------------------------------------------
        # billing_delete_data
        # ------------------------------------------------------------------

        @mcp.tool()
        def billing_delete_data(confirm: str = "") -> dict:
            """Delete all your billing data (GDPR right-to-erasure).

            Args:
                confirm: Must be ``"DELETE"`` to confirm deletion.

            This permanently removes your payment methods and billing
            preferences.  Billing charge records are retained for 7 years
            per tax compliance requirements but can be anonymized on request.

            This action cannot be undone.
            """
            if err := _srv._check_billing_auth("admin"):
                return err
            if confirm != "DELETE":
                return _srv._error_dict(
                    "Destructive operation requires confirmation. "
                    "Call again with confirm='DELETE' to proceed.",
                    code="CONFIRMATION_REQUIRED",
                )
            try:
                db = _srv.get_db()
                # Use a placeholder user_id since we're single-tenant.
                result = db.delete_user_billing_data("default")
                return {
                    "success": True,
                    "deleted": result,
                    "message": (
                        "Payment methods deleted. Billing charge records are "
                        "retained for 7 years per tax compliance. Contact "
                        "support to request full anonymization."
                    ),
                }
            except Exception as exc:
                _logger.exception("Error deleting billing data")
                return _srv._error_dict(f"Failed to delete billing data: {exc}")

        _logger.debug("Registered billing tools")


plugin = _BillingToolsPlugin()
