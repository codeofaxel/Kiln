"""Partner-provider integration tools plugin.

Canonical tools are provider-oriented and integration-scoped:

- ``connect_provider_account``
- ``sync_provider_capacity``
- ``list_provider_capacity``
- ``find_provider_capacity``
- ``submit_provider_job``
- ``provider_job_status``

Legacy ``network_*`` aliases were removed in v0.4.1 (deprecated since v0.2.0).
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _NetworkToolsPlugin:
    """Partner-provider integration tools (3DOS-backed).

    Tools:
        - connect_provider_account
        - sync_provider_capacity
        - list_provider_capacity
        - find_provider_capacity
        - submit_provider_job
        - provider_job_status
    """

    @property
    def name(self) -> str:
        return "provider_integration_tools"

    @property
    def description(self) -> str:
        return "Partner-provider integration tools (3DOS-backed)"

    def register(self, mcp: Any) -> None:
        """Register provider integration tools with the MCP server."""

        @mcp.tool()
        def connect_provider_account(
            name: str,
            location: str,
            capabilities: dict[str, Any] | None = None,
            price_per_gram: float | None = None,
        ) -> dict:
            """Connect a local printer to a provider account (integration path).

            Args:
                name: Human-readable printer name (e.g. "Prusa MK4 #2").
                location: Geographic location (e.g. "Austin, TX").
                capabilities: Optional dict of printer capabilities (build volume,
                    supported materials, etc.).
                price_per_gram: Price per gram of filament in USD (optional).

            Registers this printer with the configured partner provider
            integration (currently 3DOS).
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                listing = client.register_printer(
                    name=name,
                    location=location,
                    capabilities=capabilities,
                    price_per_gram=price_per_gram,
                )
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "printer": listing.to_dict(),
                    "integration_scope": "provider",
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in connect_provider_account")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def sync_provider_capacity(
            printer_id: str | None = None,
            available: bool | None = None,
        ) -> dict:
            """Sync local printer capacity/availability to the provider integration.

            Args:
                printer_id: Optional ID of a registered provider printer.
                available: Optional availability update for ``printer_id``.

            If ``printer_id`` and ``available`` are provided, updates that
            listing first, then returns the current provider-side capacity view.
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                updated = False
                if printer_id is not None and available is not None:
                    client.update_printer_status(printer_id=printer_id, available=available)
                    updated = True
                printers = client.list_my_printers()
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "printer_id": printer_id,
                    "available": available,
                    "updated": updated,
                    "printers": [p.to_dict() for p in printers],
                    "count": len(printers),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in sync_provider_capacity")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def list_provider_capacity() -> dict:
            """List printers registered with connected provider integrations.

            Returns all provider-side listings associated with this
            integration account.
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                printers = client.list_my_printers()
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "printers": [p.to_dict() for p in printers],
                    "count": len(printers),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in list_provider_capacity")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def find_provider_capacity(
            material: str,
            location: str | None = None,
        ) -> dict:
            """Find available provider capacity by material/location.

            Args:
                material: Material type to filter by (e.g. "PLA", "PETG", "ABS").
                location: Optional geographic filter (e.g. "Austin, TX").

            Returns provider-side capacity listings that match the request.
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                printers = client.find_printers(material=material, location=location)
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "printers": [p.to_dict() for p in printers],
                    "count": len(printers),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in find_provider_capacity")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def submit_provider_job(
            file_url: str,
            material: str,
            printer_id: str | None = None,
        ) -> dict:
            """Submit a print job through a connected provider integration.

            Args:
                file_url: Public URL of the model file to print.
                material: Material to print with (e.g. "PLA", "PETG").
                printer_id: Optional target printer ID.  If omitted, provider
                    auto-assigns the best available printer.

            Returns a provider-managed job reference. Use
            ``provider_job_status`` to track progress.
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                job = client.submit_network_job(
                    file_url=file_url,
                    material=material,
                    printer_id=printer_id,
                )
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "job": job.to_dict(),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in submit_provider_job")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def provider_job_status(job_id: str) -> dict:
            """Check status of a provider-managed remote job.

            Args:
                job_id: Job ID from ``submit_provider_job``.
            """
            from kiln.gateway.threedos import ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                job = client.get_network_job(job_id=job_id)
                return {
                    "success": True,
                    "provider_name": "3dos",
                    "job": job.to_dict(),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in provider_job_status")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered provider integration tools")


plugin = _NetworkToolsPlugin()
