"""Firmware management tools plugin.

Extracts firmware status, update, and rollback MCP tools from server.py
into a focused plugin module.  Covers both adapter-level (single printer)
and fleet-level (by printer name) firmware operations.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _FirmwareToolsPlugin:
    """Firmware status, update, and rollback tools.

    Tools:
        - firmware_status
        - check_firmware_status
        - update_printer_firmware
        - rollback_printer_firmware
    """

    @property
    def name(self) -> str:
        return "firmware_tools"

    @property
    def description(self) -> str:
        return "Firmware status, update, and rollback tools"

    def register(self, mcp: Any) -> None:
        """Register firmware tools with the MCP server."""

        import kiln.server as _srv
        from kiln.printers.base import PrinterError

        # ------------------------------------------------------------------
        # firmware_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def firmware_status() -> dict:
            """Check firmware updates on the default/connected printer (adapter-level, no name needed).

            For fleet setups where you need to check a specific printer by name,
            use ``check_firmware_status`` instead.  Returns a list of firmware
            components (e.g. Klipper, Moonraker, OctoPrint) with current and
            available versions, plus whether
            an update is available.

            Not all printer backends support firmware updates.  Bambu and
            PrusaConnect printers will return an ``UNSUPPORTED`` error.
            """
            try:
                adapter = _srv._get_adapter()
                if not adapter.capabilities.can_update_firmware:
                    return _srv._error_dict(
                        "This printer backend does not support firmware updates.",
                        code="UNSUPPORTED",
                    )
                status = adapter.get_firmware_status()
                if status is None:
                    return _srv._error_dict("Could not retrieve firmware status.", code="UNAVAILABLE")
                return {
                    "success": True,
                    "busy": status.busy,
                    "updates_available": status.updates_available,
                    "components": [
                        {
                            "name": c.name,
                            "current_version": c.current_version,
                            "remote_version": c.remote_version,
                            "update_available": c.update_available,
                            "rollback_version": c.rollback_version,
                            "component_type": c.component_type,
                            "channel": c.channel,
                        }
                        for c in status.components
                    ],
                }
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to get firmware status: {exc}. Check that the printer is online."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in firmware_status")
                return _srv._error_dict(
                    f"Unexpected error in firmware_status: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # check_firmware_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def check_firmware_status(printer_name: str) -> dict:
            """Check firmware version for a specific printer by name (fleet-level, firmware manager).

            Use this in multi-printer setups. For single-printer setups where you
            don't need to specify a name, use ``firmware_status`` instead.

            Args:
                printer_name: Printer to check.
            """
            try:
                from kiln.firmware import get_firmware_manager

                mgr = get_firmware_manager()
                info = mgr.check_version(printer_name)
                return {"success": True, "firmware": info.to_dict()}
            except Exception as exc:
                _logger.exception("Error in check_firmware_status")
                return _srv._error_dict(
                    f"Failed to check firmware status: {exc}",
                    code="FIRMWARE_ERROR",
                )

        # ------------------------------------------------------------------
        # update_printer_firmware
        # ------------------------------------------------------------------

        @mcp.tool()
        def update_printer_firmware(
            printer_name: str,
            *,
            target_version: str | None = None,
        ) -> dict:
            """Start a firmware update on a specific printer by name (fleet-level, supports version pinning).

            Use this in multi-printer setups. For single-printer setups, use ``update_firmware`` instead.

            Args:
                printer_name: Printer to update.
                target_version: Specific version to update to (latest if None).
            """
            if err := _srv._check_auth("firmware"):
                return err

            try:
                from kiln.firmware import get_firmware_manager

                mgr = get_firmware_manager()
                result = mgr.update_firmware(printer_name, target_version=target_version)
                return {"success": True, "update": result.to_dict()}
            except Exception as exc:
                _logger.exception("Error in update_printer_firmware")
                return _srv._error_dict(
                    f"Failed to update printer firmware: {exc}",
                    code="FIRMWARE_ERROR",
                )

        # ------------------------------------------------------------------
        # rollback_printer_firmware
        # ------------------------------------------------------------------

        @mcp.tool()
        def rollback_printer_firmware(
            printer_name: str,
            *,
            target_version: str | None = None,
        ) -> dict:
            """Rollback firmware on a specific printer by name (fleet-level, supports version pinning).

            Use this in multi-printer setups. For single-printer setups, use ``rollback_firmware`` instead.

            Args:
                printer_name: Printer to rollback.
                target_version: Specific version to rollback to.
            """
            if err := _srv._check_auth("firmware"):
                return err

            try:
                from kiln.firmware import get_firmware_manager

                mgr = get_firmware_manager()
                result = mgr.rollback_firmware(printer_name, target_version=target_version)
                return {"success": True, "rollback": result.to_dict()}
            except Exception as exc:
                _logger.exception("Error in rollback_printer_firmware")
                return _srv._error_dict(
                    f"Failed to rollback printer firmware: {exc}",
                    code="FIRMWARE_ERROR",
                )

        _logger.debug("Registered firmware tools")


plugin = _FirmwareToolsPlugin()
