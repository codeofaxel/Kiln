"""Print service, marketplace publishing, and revenue tools plugin.

Provides MCP tools for publishing models to marketplaces, tracking
creator revenue, and orchestrating print-as-a-service orders.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_logger = logging.getLogger(__name__)


class _ServiceToolsPlugin:
    """Print service, marketplace publishing, and revenue tools.

    Tools:
        - publish_model
        - generate_print_certificate
        - list_published_models
        - record_revenue
        - revenue_dashboard
        - model_revenue
        - create_print_service_order
        - print_service_quote
        - print_service_status
        - cancel_print_service_order
    """

    @property
    def name(self) -> str:
        return "service_tools"

    @property
    def description(self) -> str:
        return "Marketplace publishing, revenue tracking, and print-as-a-service tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register service tools with the MCP server."""

        @mcp.tool()
        def publish_model(
            file_path: str,
            title: str,
            description: str,
            tags: list[str],
            category: str,
            license: str = "cc-by",
            target_marketplaces: list[str] | None = None,
            include_certificate: bool = True,
            include_print_settings: bool = True,
        ) -> dict:
            """Publish a 3D model to one or more marketplaces.

            Validates the model, optionally generates a print "birth
            certificate" from print history, and uploads to the specified
            marketplaces.

            Args:
                file_path: Path to the 3D model file (STL, 3MF, OBJ).
                title: Listing title for the model.
                description: Listing description (Markdown supported).
                tags: List of tags for discoverability.
                category: Model category (e.g. "tools", "art", "gadgets").
                license: License type — ``"cc-by"``, ``"cc-by-sa"``,
                    ``"cc-by-nc"``, ``"gpl"``, or ``"public_domain"``.
                target_marketplaces: Marketplaces to publish to.
                    Defaults to ``["thingiverse"]``.
                include_certificate: Attach print certificate if available.
                include_print_settings: Include recommended print settings.
            """
            import kiln.server as _srv
            from kiln.marketplace_publish import PublishRequest
            from kiln.marketplace_publish import publish_model as _publish

            if err := _srv._check_auth("publish"):
                return err

            try:
                req = PublishRequest(
                    file_path=file_path,
                    title=title,
                    description=description,
                    tags=tags,
                    category=category,
                    license=license,
                    target_marketplaces=target_marketplaces or ["thingiverse"],
                    include_certificate=include_certificate,
                    include_print_settings=include_print_settings,
                )
                result = _publish(req)
                return {
                    "success": result.successful_count > 0,
                    "data": result.to_dict(),
                    "message": (
                        f"Published to {result.successful_count} marketplace(s), "
                        f"{result.failed_count} failed."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in publish_model")
                return _srv._error_dict(
                    f"Unexpected error in publish_model: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def generate_print_certificate(file_path: str) -> dict:
            """Generate a print "birth certificate" for a 3D model.

            Queries print history for the file and builds a certificate
            containing tested printers, materials, success rate, and
            recommended settings.

            Args:
                file_path: Path to the 3D model file.
            """
            import kiln.server as _srv
            from kiln.marketplace_publish import format_certificate_markdown
            from kiln.marketplace_publish import generate_print_certificate as _gen_cert

            try:
                cert = _gen_cert(file_path)
                if cert is None:
                    return {
                        "success": True,
                        "certificate": None,
                        "message": "No print history found for this file.",
                    }
                return {
                    "success": True,
                    "certificate": cert.to_dict(),
                    "markdown": format_certificate_markdown(cert),
                    "message": (
                        f"Certificate generated: {cert.total_prints} prints, "
                        f"{cert.success_rate * 100:.0f}% success rate."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in generate_print_certificate")
                return _srv._error_dict(
                    f"Unexpected error in generate_print_certificate: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def list_published_models(
            marketplace: str | None = None,
            limit: int = 50,
        ) -> dict:
            """List models that have been published to marketplaces.

            Args:
                marketplace: Filter by marketplace name (optional).
                limit: Maximum number of results (default 50).
            """
            import kiln.server as _srv
            from kiln.marketplace_publish import (
                list_published_models as _list_published,
            )

            try:
                models = _list_published(marketplace=marketplace, limit=limit)
                return {
                    "success": True,
                    "models": models,
                    "count": len(models),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in list_published_models")
                return _srv._error_dict(
                    f"Unexpected error in list_published_models: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def record_revenue(
            model_id: str,
            marketplace: str,
            amount_usd: float,
            transaction_type: str = "sale",
            currency: str = "USD",
            description: str = "",
        ) -> dict:
            """Record a revenue event (sale, royalty, tip, or refund).

            Args:
                model_id: File hash or listing ID of the model.
                marketplace: Marketplace name (e.g. ``"thingiverse"``).
                amount_usd: Amount in USD.
                transaction_type: Type — ``"sale"``, ``"royalty"``,
                    ``"tip"``, or ``"refund"``.
                currency: Currency code (default ``"USD"``).
                description: Optional description of the transaction.
            """
            import kiln.server as _srv
            from kiln.revenue_tracking import RevenueEntry
            from kiln.revenue_tracking import record_revenue as _record

            if err := _srv._check_auth("revenue"):
                return err

            try:
                entry = RevenueEntry(
                    model_id=model_id,
                    marketplace=marketplace,
                    amount_usd=amount_usd,
                    currency=currency,
                    transaction_type=transaction_type,
                    description=description,
                    timestamp=time.time(),
                )
                _record(entry)
                return {
                    "success": True,
                    "entry": entry.to_dict(),
                    "message": f"Recorded {transaction_type} of ${amount_usd:.2f} for {model_id}.",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in record_revenue")
                return _srv._error_dict(
                    f"Unexpected error in record_revenue: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def revenue_dashboard(days: int = 30) -> dict:
            """Get aggregate revenue analytics dashboard.

            Returns total revenue, sales count, top models, monthly
            trends, and marketplace breakdown.

            Args:
                days: Number of days to include (default 30).
            """
            import kiln.server as _srv
            from kiln.revenue_tracking import get_revenue_dashboard

            try:
                dashboard = get_revenue_dashboard(days=days)
                return {
                    "success": True,
                    "dashboard": dashboard.to_dict(),
                    "message": (
                        f"Revenue dashboard: ${dashboard.total_revenue_usd:.2f} total, "
                        f"{dashboard.total_sales} sales across {dashboard.total_models} models."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in revenue_dashboard")
                return _srv._error_dict(
                    f"Unexpected error in revenue_dashboard: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def model_revenue(model_id: str) -> dict:
            """Get revenue summary for a specific model.

            Args:
                model_id: File hash or listing ID of the model.
            """
            import kiln.server as _srv
            from kiln.revenue_tracking import get_model_revenue

            try:
                summary = get_model_revenue(model_id)
                return {
                    "success": True,
                    "summary": summary.to_dict(),
                    "message": (
                        f"Model {summary.title}: ${summary.net_revenue_usd:.2f} net revenue, "
                        f"{summary.total_sales} sales."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in model_revenue")
                return _srv._error_dict(
                    f"Unexpected error in model_revenue: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def create_print_service_order(
            model_path: str | None = None,
            model_url: str | None = None,
            prompt: str | None = None,
            material: str = "pla",
            intent: str | None = None,
            quantity: int = 1,
            color: str | None = None,
            prefer_local: bool = True,
            printer_name: str | None = None,
            max_budget_usd: float | None = None,
        ) -> dict:
            """Create a Print-as-a-Service order and get a quote.

            Provide ONE of ``model_path``, ``model_url``, or ``prompt``.
            Returns a quote with local and fulfillment options. Call
            ``print_service_quote`` with the order ID to confirm.

            Args:
                model_path: Local path to a 3D model file.
                model_url: URL to download a 3D model.
                prompt: Text prompt for AI model generation.
                material: Material type (default ``"pla"``).
                intent: Print intent — ``"strong"``, ``"pretty"``, or ``"cheap"``.
                quantity: Number of copies (default 1).
                color: Desired color (optional).
                prefer_local: Prefer local printing (default True).
                printer_name: Specific local printer name (optional).
                max_budget_usd: Maximum budget in USD (optional).
            """
            import kiln.server as _srv
            from kiln.print_service import PrintServiceRequest, create_print_order

            if err := _srv._check_auth("print_service"):
                return err

            try:
                req = PrintServiceRequest(
                    model_path=model_path,
                    model_url=model_url,
                    prompt=prompt,
                    material=material,
                    intent=intent,
                    quantity=quantity,
                    color=color,
                    prefer_local=prefer_local,
                    printer_name=printer_name,
                    max_budget_usd=max_budget_usd,
                )
                quote = create_print_order(req)
                return {
                    "success": True,
                    "quote": quote.to_dict(),
                    "message": (
                        f"Order {quote.order_id} created. Recommended: {quote.recommended} "
                        f"(${quote.total_cost_usd:.2f}, ~{quote.estimated_time_hours:.1f}h). "
                        f"Call print_service_quote to confirm."
                    ),
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in create_print_service_order")
                return _srv._error_dict(
                    f"Unexpected error in create_print_service_order: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def print_service_quote(
            order_id: str,
            option: str = "recommended",
        ) -> dict:
            """Confirm a print service order and start processing.

            Args:
                order_id: The order ID from ``create_print_service_order``.
                option: ``"local"``, ``"fulfillment"``, or ``"recommended"``.
            """
            import kiln.server as _srv
            from kiln.print_service import confirm_print_order

            if err := _srv._check_auth("print_service"):
                return err

            try:
                order = confirm_print_order(order_id, option=option)
                return {
                    "success": True,
                    "order": order.to_dict(),
                    "message": f"Order {order_id} confirmed. Status: {order.status}.",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in print_service_quote")
                return _srv._error_dict(
                    f"Unexpected error in print_service_quote: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def print_service_status(order_id: str) -> dict:
            """Check the status of a print service order.

            Args:
                order_id: The order ID to check.
            """
            import kiln.server as _srv
            from kiln.print_service import get_order_status

            try:
                order = get_order_status(order_id)
                return {
                    "success": True,
                    "order": order.to_dict(),
                    "message": f"Order {order_id}: {order.status} (step: {order.current_step}).",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in print_service_status")
                return _srv._error_dict(
                    f"Unexpected error in print_service_status: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def cancel_print_service_order(order_id: str) -> dict:
            """Cancel a print service order.

            Only orders that have not started printing can be cancelled.

            Args:
                order_id: The order ID to cancel.
            """
            import kiln.server as _srv
            from kiln.print_service import cancel_order

            if err := _srv._check_auth("print_service"):
                return err

            try:
                result = cancel_order(order_id)
                return result
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in cancel_print_service_order")
                return _srv._error_dict(
                    f"Unexpected error in cancel_print_service_order: {exc}",
                    code="INTERNAL_ERROR",
                )

        _logger.debug("Registered service tools")


plugin = _ServiceToolsPlugin()
