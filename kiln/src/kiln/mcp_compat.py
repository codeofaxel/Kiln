"""One import point for the MCP SDK's server API across SDK majors.

The 2026-07-28 MCP spec shipped with Python SDK 2.0, which renamed the
server construction surface: ``mcp.server.fastmcp.FastMCP`` became
``mcp.server.mcpserver.MCPServer`` (same decorator-based API), and the
underlying lowlevel server handle moved from ``._mcp_server`` to
``._lowlevel_server``.  Kiln supports both SDK majors so an environment
that pins ``mcp<2`` keeps working while ``mcp>=2`` speaks the new
stateless spec natively.

Import server-API names from here, never from ``mcp.server.fastmcp`` or
``mcp.server.mcpserver`` directly — this module is the only place in
either repo that knows which SDK is installed.
"""

from __future__ import annotations

from typing import Any

try:  # mcp>=2.0 — speaks MCP spec 2026-07-28 (stateless core)
    from mcp.server.mcpserver import (  # type: ignore[import-not-found]
        Context,
        Image,
        MCPServer as FastMCP,
    )

    MCP_SDK_MAJOR = 2
except ImportError:  # mcp 1.x — legacy FastMCP surface
    from mcp.server.fastmcp import Context, FastMCP, Image  # type: ignore

    MCP_SDK_MAJOR = 1

__all__ = [
    "Context",
    "FastMCP",
    "Image",
    "MCP_SDK_MAJOR",
    "lowlevel_server",
    "set_instructions",
]


def lowlevel_server(mcp: Any) -> Any:
    """Return the lowlevel ``Server`` behind a FastMCP/MCPServer instance.

    SDK 2.0 renamed the attribute (``_mcp_server`` -> ``_lowlevel_server``);
    both majors keep the same lowlevel object underneath.
    """
    server = getattr(mcp, "_lowlevel_server", None)
    if server is None:
        server = getattr(mcp, "_mcp_server")
    return server


def set_instructions(mcp: Any, text: str) -> None:
    """Replace the server instructions after construction.

    ``instructions`` is a read-only property on both SDK majors, so the
    rebuild-after-config-load path writes to the lowlevel server object.
    """
    lowlevel_server(mcp).instructions = text
