"""Monitor-twin plugin — publish the live print's sliced file for the web
Monitor's layer viewer.

One tool, one purpose: when the kiln3d.com Monitor is watching a live print
and has no toolpath, it asks this machine (through the bridge relay) to
upload the retained sliced G-code — and the mesh it was sliced from — to the
caller's own Kiln account.  The retention itself happens automatically at
the engine chokepoints (see :mod:`kiln.monitor_twin`); this tool only ships
what is already on disk, and only when someone is actually watching.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MonitorTwinToolsPlugin:
    """Print-twin publishing for the web Monitor.

    Tools:
        - publish_print_twin
    """

    @property
    def name(self) -> str:
        return "monitor_twin_tools"

    @property
    def description(self) -> str:
        return "Publish the live print's sliced file for the web Monitor"

    def register(self, mcp: Any) -> None:
        """Register monitor-twin tools with the MCP server."""

        @mcp.tool()
        def publish_print_twin(printer_name: str | None = None) -> dict:
            """Upload the current print's sliced file so the web Monitor can
            show the object and scrub its layers.

            The web Monitor at kiln3d.com calls this through the Kiln bridge
            while a print is running; there is normally no reason to call it
            by hand.  It reads the engine's own retained copy of the file it
            last sent to the printer (kept per printer under
            ``~/.kiln/monitor_twin``), uploads it — gzipped — to YOUR Kiln
            account, and returns a short-lived artifact token the browser
            uses to fetch the mesh and toolpath.  Nothing is uploaded except
            by this explicit call, and nothing crosses tenants: the token is
            IDOR-checked against your own account on every fetch.

            Honest refusals: prints Kiln did not slice on this machine (a
            pre-sliced upload, a job started at the printer's screen) have
            no retained file, and the response says so rather than guessing.

            Args:
                printer_name: Which printer's active print to publish.  Omit
                    for the machine's single/default printer.

            Returns:
                ``{"success": True, "artifact_token", "stl_url"|null,
                "gcode_url"|null, "file_name", "expires_in"}`` on success;
                ``{"success": False, "code", "message"}`` otherwise.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("monitoring"):
                return err

            try:
                from kiln.monitor_twin import publish

                return publish(printer_name)
            except Exception as exc:  # noqa: BLE001 — a tool answers, never raises
                _logger.exception("publish_print_twin failed")
                return {
                    "success": False,
                    "code": "INTERNAL_ERROR",
                    "message": f"Unexpected error publishing the print twin: {exc}",
                }


plugin = _MonitorTwinToolsPlugin()
