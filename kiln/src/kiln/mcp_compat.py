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

STILL OWED, and the reason the 3D stage is not yet whole on SDK 2: the host
capability read.  ``local_stage._declared_extensions`` reaches
``lowlevel_server(mcp).request_context`` — 1.x parks the request context on
the lowlevel server, and SDK 2 removed that attribute outright and passes a
``ServerRequestContext`` as the handler's FIRST ARGUMENT instead.  So on SDK 2
that read raises, is caught, and reads as "the host declared nothing" — which
means ``host_renders_apps`` is always False and geometry is never attached,
even though the resource registers and the token hook now installs.  The fix
belongs here as a ``client_capabilities(mcp, ctx=None)`` that prefers a passed
``ctx`` and falls back to the 1.x attribute, with ``wrap_call_tool_result``
handing its ``ctx`` down to the mutate callback.  Deliberately not added
un-wired: an accessor with no callers is how a thing looks tested when it is
not.
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
    "wrap_call_tool_result",
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


#: Marks our wrapper so a second install is a no-op rather than a second layer.
_WRAPPED = "_kiln_wrapped_call_tool"


def wrap_call_tool_result(mcp: Any, mutate: Any) -> bool:
    """Wrap the lowlevel ``tools/call`` handler so ``mutate`` sees each result.

    ``mutate(result)`` is called with the tool result object AFTER the real
    handler produced it, and mutates it in place; its return value is ignored
    and it must not raise (callers wrap their own body).  The handler's own
    return value is passed through untouched, so a wrapper that does nothing
    is invisible.

    Everything the two SDK majors disagree about lives here, because the
    disagreement is total — the handler is keyed by request TYPE on 1.x and by
    the method string ``"tools/call"`` on 2.x; it is called ``handler(req)``
    on 1.x and ``handler(ctx, params)`` on 2.x; and 2.x stores it in a
    ``HandlerEntry`` alongside the params type it must be re-registered with.
    A caller that branched on any of that would be a second place that knows
    which SDK is installed, which is the thing this module exists to prevent.

    Returns True when a wrapper was installed, False when there was no handler
    to wrap or ours is already in place.  Never raises for the ordinary
    reasons; an exotic server object propagates, and callers decide.
    """
    server = lowlevel_server(mcp)

    def _wrap(previous: Any) -> Any:
        """Shared body: run the handler, let ``mutate`` see the result."""

        def _apply(resp: Any) -> Any:
            # 1.x hands back a ServerResult with the real result on ``.root``;
            # 2.x hands back the CallToolResult itself, which has no ``.root``.
            mutate(getattr(resp, "root", resp))
            return resp

        return _apply

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")
        if entry is None or getattr(entry.handler, _WRAPPED, False):
            return False
        previous, params_type = entry.handler, entry.params_type
        apply = _wrap(previous)

        async def _wrapped_v2(ctx: Any, params: Any) -> Any:
            return apply(await previous(ctx, params))

        setattr(_wrapped_v2, _WRAPPED, True)
        server.add_request_handler("tools/call", params_type, _wrapped_v2)
        return True

    from mcp.types import CallToolRequest  # 1.x keys the dict by request type

    handlers = getattr(server, "request_handlers", None) or {}
    previous = handlers.get(CallToolRequest)
    if previous is None or getattr(previous, _WRAPPED, False):
        return False
    apply = _wrap(previous)

    async def _wrapped_v1(req: Any) -> Any:
        return apply(await previous(req))

    setattr(_wrapped_v1, _WRAPPED, True)
    handlers[CallToolRequest] = _wrapped_v1
    return True
