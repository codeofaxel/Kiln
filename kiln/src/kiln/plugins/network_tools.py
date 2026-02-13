"""3DOS network integration tools plugin.

Provides MCP tools for registering printers, finding available printers,
submitting jobs, and tracking job status on the 3DOS distributed
manufacturing network.

Migrated from server.py to reduce monolith size.  The original tool
definitions in server.py remain authoritative until removed; this plugin
is the extraction target.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)


class _NetworkToolsPlugin:
    """3DOS network integration tools.

    Tools:
        - network_register_printer
        - network_update_printer
        - network_list_printers
        - network_find_printers
        - network_submit_job
        - network_job_status
    """

    @property
    def name(self) -> str:
        return "network_tools"

    @property
    def description(self) -> str:
        return "3DOS network integration tools"

    def register(self, mcp: Any) -> None:
        """Register 3DOS network tools with the MCP server."""

        @mcp.tool()
        def network_register_printer(
            name: str,
            location: str,
            capabilities: Optional[Dict[str, Any]] = None,
            price_per_gram: Optional[float] = None,
        ) -> dict:
            """Register a local printer on the 3DOS distributed manufacturing network.

            Args:
                name: Human-readable printer name (e.g. "Prusa MK4 #2").
                location: Geographic location (e.g. "Austin, TX").
                capabilities: Optional dict of printer capabilities (build volume,
                    supported materials, etc.).
                price_per_gram: Price per gram of filament in USD (optional).

            Makes this printer available for remote print jobs from the 3DOS
            network.  Requires ``KILN_3DOS_API_KEY`` to be set.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
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
                    "printer": listing.to_dict(),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_register_printer")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def network_update_printer(
            printer_id: str,
            available: bool,
        ) -> dict:
            """Update a printer's availability on the 3DOS network.

            Args:
                printer_id: ID of the registered printer.
                available: Whether the printer is available for new jobs.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                client.update_printer_status(printer_id=printer_id, available=available)
                return {
                    "success": True,
                    "printer_id": printer_id,
                    "available": available,
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_update_printer")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def network_list_printers() -> dict:
            """List printers registered by this account on the 3DOS network.

            Returns all printers that this Kiln instance has registered,
            including their current availability status and pricing.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                printers = client.list_my_printers()
                return {
                    "success": True,
                    "printers": [p.to_dict() for p in printers],
                    "count": len(printers),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_list_printers")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def network_find_printers(
            material: str,
            location: Optional[str] = None,
        ) -> dict:
            """Search for available printers on the 3DOS network.

            Args:
                material: Material type to filter by (e.g. "PLA", "PETG", "ABS").
                location: Optional geographic filter (e.g. "Austin, TX").

            Returns printers that can handle the requested material.  Use the
            printer ``id`` from the results with ``network_submit_job`` to
            target a specific printer.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                printers = client.find_printers(material=material, location=location)
                return {
                    "success": True,
                    "printers": [p.to_dict() for p in printers],
                    "count": len(printers),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_find_printers")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def network_submit_job(
            file_url: str,
            material: str,
            printer_id: Optional[str] = None,
        ) -> dict:
            """Submit a print job to the 3DOS distributed manufacturing network.

            Args:
                file_url: Public URL of the model file to print.
                material: Material to print with (e.g. "PLA", "PETG").
                printer_id: Optional target printer ID.  If omitted, 3DOS
                    auto-assigns the best available printer.

            Returns the network job with ID, status, and estimated cost.
            Use ``network_job_status`` to track progress.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
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
                    "job": job.to_dict(),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_submit_job")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def network_job_status(job_id: str) -> dict:
            """Check the status of a job on the 3DOS network.

            Args:
                job_id: Job ID from ``network_submit_job``.
            """
            from kiln.gateway.threedos import ThreeDOSClient, ThreeDOSError
            from kiln.server import _error_dict, _get_threedos_client

            try:
                client = _get_threedos_client()
                job = client.get_network_job(job_id=job_id)
                return {
                    "success": True,
                    "job": job.to_dict(),
                }
            except (ThreeDOSError, ValueError) as exc:
                return _error_dict(str(exc))
            except Exception as exc:
                _logger.exception("Unexpected error in network_job_status")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered 3DOS network tools")


plugin = _NetworkToolsPlugin()
