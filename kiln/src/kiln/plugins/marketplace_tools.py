"""Example tool plugin — marketplace convenience tools.

Demonstrates the pattern for migrating MCP tool definitions out of
server.py into focused plugin modules.  Each module exposes a
module-level ``plugin`` variable implementing the
:class:`~kiln.plugin_loader.ToolPlugin` protocol.

To migrate tools from server.py:
1. Create a new module in ``kiln/plugins/``.
2. Define a class with ``name``, ``description``, and ``register(mcp)``.
3. In ``register()``, define tool functions decorated with ``@mcp.tool()``.
4. Assign an instance to ``plugin`` at module level.
5. Remove the original tool definition from server.py.

The :func:`~kiln.plugin_loader.register_all_plugins` loader discovers
this module automatically — no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MarketplaceToolsPlugin:
    """Convenience tools for marketplace interactions.

    These tools complement the existing search/download tools in server.py
    by providing higher-level marketplace status and summary operations.
    """

    @property
    def name(self) -> str:
        return "marketplace_tools"

    @property
    def description(self) -> str:
        return "Marketplace convenience tools (status, diagnostics)"

    def register(self, mcp: Any) -> None:
        """Register marketplace convenience tools with the MCP server."""

        @mcp.tool()
        def marketplace_status() -> dict:
            """Check which 3D model marketplaces are connected and available.

            Returns the list of configured marketplace sources, their
            connection status, and whether credentials are present.  Use
            this to verify marketplace access before searching or
            downloading models.
            """

            # Import server internals lazily to avoid circular imports
            try:
                from kiln.server import (
                    _init_marketplace_registry,
                    _marketplace_registry,
                )
            except ImportError:
                return {
                    "success": False,
                    "error": {
                        "code": "IMPORT_ERROR",
                        "message": "Could not access marketplace registry.",
                        "retryable": False,
                    },
                }

            try:
                if _marketplace_registry.count == 0:
                    _init_marketplace_registry()

                import os

                sources = {
                    "thingiverse": bool(os.environ.get("KILN_THINGIVERSE_TOKEN")),
                    "myminifactory": bool(os.environ.get("KILN_MMF_API_KEY")),
                    "cults3d": bool(os.environ.get("KILN_CULTS3D_USERNAME") and os.environ.get("KILN_CULTS3D_API_KEY")),
                }

                return {
                    "success": True,
                    "connected_count": _marketplace_registry.count,
                    "connected": _marketplace_registry.connected,
                    "credentials_configured": {name: configured for name, configured in sources.items()},
                    "message": (
                        f"{_marketplace_registry.count} marketplace(s) connected"
                        if _marketplace_registry.count > 0
                        else "No marketplaces configured. Set KILN_THINGIVERSE_TOKEN, "
                        "KILN_MMF_API_KEY, or KILN_CULTS3D_USERNAME + "
                        "KILN_CULTS3D_API_KEY to enable marketplace access."
                    ),
                }
            except Exception as exc:
                _logger.exception("Error in marketplace_status")
                return {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"Unexpected error: {exc}",
                        "retryable": True,
                    },
                }

        @mcp.tool()
        def marketplace_diagnostics() -> dict:
            """Run connectivity checks against all configured marketplaces.

            Performs a lightweight probe (empty search) against each
            connected marketplace and reports which ones are reachable.
            Useful for debugging download failures.
            """
            try:
                from kiln.server import (
                    _init_marketplace_registry,
                    _marketplace_registry,
                )
            except ImportError:
                return {
                    "success": False,
                    "error": {
                        "code": "IMPORT_ERROR",
                        "message": "Could not access marketplace registry.",
                        "retryable": False,
                    },
                }

            try:
                if _marketplace_registry.count == 0:
                    _init_marketplace_registry()

                if _marketplace_registry.count == 0:
                    return {
                        "success": False,
                        "error": {
                            "code": "NO_MARKETPLACES",
                            "message": (
                                "No marketplace credentials configured. "
                                "Set at least one of: KILN_THINGIVERSE_TOKEN, "
                                "KILN_MMF_API_KEY, "
                                "KILN_CULTS3D_USERNAME + KILN_CULTS3D_API_KEY."
                            ),
                            "retryable": False,
                        },
                    }

                results = _marketplace_registry.search_all(
                    "benchy",
                    page=1,
                    per_page=1,
                )
                return {
                    "success": True,
                    "searched": results.searched,
                    "failed": results.failed,
                    "skipped": results.skipped,
                    "summary": results.summary,
                    "message": (f"Probed {len(results.searched)} marketplace(s). {len(results.failed)} failure(s)."),
                }
            except Exception as exc:
                _logger.exception("Error in marketplace_diagnostics")
                return {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"Unexpected error: {exc}",
                        "retryable": True,
                    },
                }

        _logger.debug("Registered marketplace convenience tools")


plugin = _MarketplaceToolsPlugin()
