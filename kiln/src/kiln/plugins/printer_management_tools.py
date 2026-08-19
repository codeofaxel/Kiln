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
        - hand_back_printer
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
        # hand_back_printer
        # ------------------------------------------------------------------

        @mcp.tool()
        def hand_back_printer(printer_name: str | None = None) -> dict:
            """Tell Kiln you are taking a printer from here, so it can move on.

            Below the fleet tier Kiln works with one printer at a time: the
            machine it started a print on, or one it is watching for you.
            This hands that machine back — you keep the print, Kiln stops
            being the one driving it, and its attention is free for another
            printer.

            Nothing is cancelled and nothing is paused.  The print carries on
            exactly as it was; this only changes which machine Kiln considers
            itself responsible for.

            Called with no arguments it reports which printer Kiln is working
            with, without changing anything, so you can always find out where
            its attention is before moving it.

            One thing worth knowing before you do it: Kiln will come back to
            this print once if you need it to, and after that it stays with
            whatever machine it moved to until this print finishes.  Going
            back and forth between two running printers is what the fleet
            tier is for.

            Args:
                printer_name: Printer to hand back.  Omit to report only.
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.printers.engagement import (
                    current,
                    hand_back,
                    reason_in_english,
                )

                engagement = current()
                if printer_name is None:
                    if engagement is None:
                        return {
                            "success": True,
                            "engaged_with": None,
                            "message": "Kiln is not working with a printer right now.",
                        }
                    return {
                        "success": True,
                        "engaged_with": engagement.label,
                        "since": engagement.since,
                        "because": reason_in_english(engagement.reason),
                        "message": (
                            f"Kiln is working with {engagement.label}. "
                            f"Hand it back to move Kiln to another printer."
                        ),
                    }

                adapter = _srv._get_registry().get(printer_name)

                # Handing a machine back means Kiln stops watching it, so any
                # live watch on it ends HERE rather than discovering the
                # change on its next poll.  The watcher copes with that race
                # on its own, but a background thread finding out by being
                # refused is a worse way to end something the user asked to
                # end.
                stopped_watches = []
                try:
                    from kiln.printers.engagement import machine_id

                    target = machine_id(adapter)
                    for watch_id, watcher in list(_srv._watchers.items()):
                        watched = getattr(watcher, "_adapter", None)
                        if watched is not None and machine_id(watched) == target:
                            _srv._watchers.pop(watch_id, None)
                            watcher.stop()
                            stopped_watches.append(watch_id)
                except Exception:
                    _logger.debug("could not stop watches on hand-back", exc_info=True)

                report = hand_back(adapter)
                if not report.get("released"):
                    return _srv._error_dict(
                        report.get("reason", "Nothing to hand back."),
                        code="NOT_ENGAGED",
                    )
                name = report.get("printer", printer_name)
                left = report.get("returns_left", 0)
                # State the consequence HERE, not when they discover it.
                consequence = (
                    "Kiln can come back to this print once if you need it to."
                    if left
                    else "Kiln has already come back to this print once, so it "
                    "will stay with the next printer until this one finishes."
                )
                return {
                    "success": True,
                    "printer": name,
                    "stopped_watches": stopped_watches,
                    "message": (
                        f"Kiln has stepped off {name}. The print carries on exactly "
                        f"as it was, and Kiln is free for another printer. {consequence}"
                    ),
                    "returns_left": left,
                }
            except Exception as exc:
                _logger.exception("Error in hand_back_printer")
                return _srv._error_dict(
                    f"Failed to hand back printer: {exc}", code="INTERNAL_ERROR",
                )

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
