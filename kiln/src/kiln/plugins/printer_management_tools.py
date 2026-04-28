"""Printer management tools plugin.

Extracts printer discovery, trust whitelist, and lock management MCP tools
from server.py into a focused plugin module.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _PrinterManagementToolsPlugin:
    """Printer discovery, trust whitelist, and printer lock tools.

    Tools:
        - discover_printers
        - list_trusted_printers
        - trust_printer
        - untrust_printer
        - acquire_printer_lock
        - release_printer_lock
    """

    @property
    def name(self) -> str:
        return "printer_management_tools"

    @property
    def description(self) -> str:
        return "Printer discovery, trust whitelist, and printer lock tools"

    def register(self, mcp: Any) -> None:
        """Register printer management tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # discover_printers
        # ------------------------------------------------------------------

        @mcp.tool()
        def discover_printers(timeout: float = 5.0) -> dict:
            """Scan the local network for 3D printers.

            Uses mDNS/Bonjour and HTTP subnet probing to find OctoPrint,
            Moonraker/Creality, Bambu Lab, Elegoo, and Prusa printers on
            the local network.

            Args:
                timeout: Maximum scan duration in seconds (default 5).

            Returns a list of discovered printers with host, port, type, and
            whether the API is reachable.  Use ``register_printer`` to add
            discovered printers to the fleet.
            """
            try:
                from kiln.discovery import discover_printers as _discover

                results = _discover(timeout=timeout)
                return {
                    "success": True,
                    "printers": [p.to_dict() for p in results],
                    "count": len(results),
                    "message": f"Found {len(results)} printer(s) on the network.",
                }
            except Exception as exc:
                _logger.exception("Unexpected error in discover_printers")
                return _srv._error_dict(
                    f"Unexpected error in discover_printers: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # list_trusted_printers
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_trusted_printers() -> dict:
            """Return the list of trusted printer hostnames/IPs.

            Trusted printers are used to flag discovered printers that have been
            explicitly approved by the user, preventing spoofed-printer attacks.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import get_trusted_printers

                trusted = get_trusted_printers()
                return {"success": True, "trusted_printers": trusted, "count": len(trusted)}
            except Exception as exc:
                _logger.exception("Unexpected error in list_trusted_printers")
                return _srv._error_dict(
                    f"Unexpected error in list_trusted_printers: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # trust_printer
        # ------------------------------------------------------------------

        @mcp.tool()
        def trust_printer(host: str) -> dict:
            """Add a printer hostname/IP to the trusted whitelist.

            Trusted printers are flagged during network discovery.  Connecting
            to an untrusted printer should raise a warning.

            Args:
                host: The hostname or IP address to trust.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import add_trusted_printer

                add_trusted_printer(host)
                return {"success": True, "host": host}
            except ValueError as exc:
                return _srv._error_dict(f"Failed to trust printer: {exc}", code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in trust_printer")
                return _srv._error_dict(
                    f"Unexpected error in trust_printer: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # untrust_printer
        # ------------------------------------------------------------------

        @mcp.tool()
        def untrust_printer(host: str) -> dict:
            """Remove a printer hostname/IP from the trusted whitelist.

            Args:
                host: The hostname or IP address to untrust.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import remove_trusted_printer

                remove_trusted_printer(host)
                return {"success": True, "host": host}
            except ValueError as exc:
                return _srv._error_dict(f"Failed to untrust printer: {exc}", code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in untrust_printer")
                return _srv._error_dict(
                    f"Unexpected error in untrust_printer: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # acquire_printer_lock
        # ------------------------------------------------------------------

        @mcp.tool()
        def acquire_printer_lock(
            printer_name: str,
            *,
            holder: str = "agent",
            timeout_seconds: float = 30.0,
        ) -> dict:
            """Acquire an exclusive lock on a printer for safe concurrent access.

            Prevents multiple agents from controlling the same printer simultaneously.

            Args:
                printer_name: Printer to lock.
                holder: Identifier of the lock holder.
                timeout_seconds: Maximum time to wait for the lock.
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.state_lock import get_state_lock_manager

                mgr = get_state_lock_manager()
                acquired = mgr.acquire(printer_name, holder=holder, timeout=timeout_seconds)
                if not acquired:
                    return _srv._error_dict(
                        f"Could not acquire lock on {printer_name!r} within {timeout_seconds}s",
                        code="LOCK_TIMEOUT",
                    )
                return {"success": True, "printer": printer_name, "holder": holder, "locked": True}
            except Exception as exc:
                _logger.exception("Error in acquire_printer_lock")
                return _srv._error_dict(f"Failed to acquire printer lock: {exc}", code="LOCK_ERROR")

        # ------------------------------------------------------------------
        # release_printer_lock
        # ------------------------------------------------------------------

        @mcp.tool()
        def release_printer_lock(printer_name: str, *, holder: str = "agent") -> dict:
            """Release an exclusive lock on a printer.

            Args:
                printer_name: Printer to unlock.
                holder: Identifier of the lock holder (must match acquire).
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.state_lock import get_state_lock_manager

                mgr = get_state_lock_manager()
                released = mgr.release(printer_name, holder=holder)
                return {"success": True, "printer": printer_name, "released": released}
            except Exception as exc:
                _logger.exception("Error in release_printer_lock")
                return _srv._error_dict(
                    f"Failed to release printer lock: {exc}",
                    code="LOCK_ERROR",
                )

        _logger.debug("Registered printer management tools")


plugin = _PrinterManagementToolsPlugin()
