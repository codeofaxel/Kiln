"""One import point for the MCP SDK's server API across SDK majors.

The 2026-07-28 MCP spec shipped with Python SDK 2.0, which renamed the
server construction surface: ``mcp.server.fastmcp.FastMCP`` became
``mcp.server.mcpserver.MCPServer`` (same decorator-based API), the
resource types moved with it (``mcp.server.fastmcp.resources`` ->
``mcp.server.mcpserver.resources``), and the underlying lowlevel server
handle moved from ``._mcp_server`` to ``._lowlevel_server``.  Kiln supports
both SDK majors so an environment that pins ``mcp<2`` keeps working while
``mcp>=2`` speaks the new stateless spec natively.

Import server-API names from here, never from ``mcp.server.fastmcp`` or
``mcp.server.mcpserver`` directly — this module is the only place in
either repo that knows which SDK is installed.

``mcp.server.fastmcp`` does not merely move under SDK 2, it stops existing,
so a direct import of it is an ImportError on every install that resolved
``mcp>=1.0`` after 2.0 shipped (2026-07-28).  ``local_stage`` imported
``FunctionResource`` from that path and its registration is wrapped in
``except Exception``, so the whole 3D stage went quietly missing —
``install()`` returned every flag False and logged one warning.  That is why
``FunctionResource`` lives here now, and why
``tests/test_mcp_compat_is_the_only_door.py`` fails any new direct import
rather than trusting the next author to remember this paragraph.

STILL OWED, and the reason the 3D stage is not yet whole on SDK 2:
``local_stage._install_result_hook`` wraps the lowlevel ``CallToolRequest``
handler to mint the artifact tokens the stage renders from.  SDK 2 rekeyed
that dispatch from request TYPE to method STRING (``"tools/call"``) and wraps
each handler in a ``HandlerEntry``, reached via ``get_request_handler`` /
``add_request_handler`` rather than a plain dict.  Until that hook is ported,
an SDK-2 install registers the stage resource but never attaches a token, so
the panel has nothing to draw and the still image carries the result.  The
port belongs here, as the handler-accessor pair the hook can call on either
major — not as a branch inside ``local_stage``.
"""

from __future__ import annotations

from typing import Any

try:  # mcp>=2.0 — speaks MCP spec 2026-07-28 (stateless core)
    from mcp.server.mcpserver import (  # type: ignore[import-not-found]
        Context,
        Image,
    )
    from mcp.server.mcpserver import (
        MCPServer as FastMCP,
    )
    from mcp.server.mcpserver.resources import (  # type: ignore[import-not-found]
        FunctionResource,
    )

    MCP_SDK_MAJOR = 2
except ImportError:  # mcp 1.x — legacy FastMCP surface
    from mcp.server.fastmcp import Context, FastMCP, Image  # type: ignore
    from mcp.server.fastmcp.resources import FunctionResource  # type: ignore

    MCP_SDK_MAJOR = 1

__all__ = [
    "Context",
    "FastMCP",
    "FunctionResource",
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
        server = mcp._mcp_server
    return server


def set_instructions(mcp: Any, text: str) -> None:
    """Replace the server instructions after construction.

    ``instructions`` is a read-only property on both SDK majors, so the
    rebuild-after-config-load path writes to the lowlevel server object.
    """
    lowlevel_server(mcp).instructions = text
