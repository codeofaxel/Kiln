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

The host capability read lives here too (``client_capabilities``), because
the majors disagree about where the request context IS: 1.x parks it on the
lowlevel server (``request_context``), and SDK 2 removed that attribute
outright and passes a ``ServerRequestContext`` as the handler's first
argument instead.  Before this accessor existed, ``local_stage`` read the
1.x attribute directly; on SDK 2 that raised, was caught, and read as "the
host declared nothing" — so ``host_renders_apps`` was always False and
geometry was never attached, even with the resource registered and the
token hook installed.  ``wrap_call_tool_result`` hands the handler's ctx
down to the mutate callback so the stage can ask about the caller that is
actually on the wire.
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
    "client_capabilities",
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


def client_capabilities(mcp: Any, ctx: Any = None) -> Any | None:
    """What the connected host declared it supports, or None.

    Prefer the ``ctx`` a handler was invoked with — on SDK 2 that is the
    ``ServerRequestContext`` and the ONLY place the session lives.  Absent a
    ctx, fall back to the 1.x location, where the lowlevel server carries the
    request context as an attribute.
    """
    if ctx is not None:
        session = getattr(ctx, "session", None)
        if session is not None:
            return getattr(getattr(session, "client_params", None), "capabilities", None)
    try:
        return lowlevel_server(mcp).request_context.session.client_params.capabilities
    except Exception:  # noqa: BLE001 — "no session" is a legitimate answer
        return None


def set_instructions(mcp: Any, text: str) -> None:
    """Replace the server instructions after construction.

    ``instructions`` is a read-only property on both SDK majors, so the
    rebuild-after-config-load path writes to the lowlevel server object.
    """
    lowlevel_server(mcp).instructions = text


#: Marks our wrapper so a second install is a no-op rather than a second layer.
_WRAPPED = "_kiln_wrapped_call_tool"


def _call_tool_name(source: Any) -> str | None:
    """Best-effort tool name from whatever a ``tools/call`` handler was handed.

    2.x hands the handler the params object (``.name`` directly); 1.x hands
    the whole request (``.params.name``, sometimes behind a ``.root``
    wrapper).  Anything unreadable is ``None`` — the stage treats an unknown
    name as "attach as before", so a shape this misses costs bytes on one
    call, never a starved panel.
    """
    for obj in (
        source,
        getattr(source, "params", None),
        getattr(getattr(source, "root", None), "params", None),
    ):
        name = getattr(obj, "name", None)
        if isinstance(name, str) and name:
            return name
        if isinstance(obj, dict):
            candidate = obj.get("name")
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def wrap_call_tool_result(mcp: Any, mutate: Any) -> bool:
    """Wrap the lowlevel ``tools/call`` handler so ``mutate`` sees each result.

    ``mutate(result, ctx, name)`` is called with the tool result object AFTER
    the real handler produced it, and mutates it in place; its return value is
    ignored and it must not raise (callers wrap their own body).  ``ctx`` is
    the ``ServerRequestContext`` SDK 2 hands the handler — the only place the
    session (and so the host's declared capabilities) lives on 2.x — and None
    on 1.x, where ``client_capabilities`` reads the lowlevel server attribute
    instead.  ``name`` is the called tool's name when the request shape
    yields one (best-effort via ``_call_tool_name``), else None — it lets the
    stage decide per TOOL what to attach, not just per host.  The handler's
    own return value is passed through untouched, so a wrapper that does
    nothing is invisible.

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

        def _apply(resp: Any, ctx: Any, name: str | None) -> Any:
            # 1.x hands back a ServerResult with the real result on ``.root``;
            # 2.x hands back the CallToolResult itself, which has no ``.root``.
            mutate(getattr(resp, "root", resp), ctx, name)
            return resp

        return _apply

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")
        if entry is None or getattr(entry.handler, _WRAPPED, False):
            return False
        previous, params_type = entry.handler, entry.params_type
        apply = _wrap(previous)

        async def _wrapped_v2(ctx: Any, params: Any) -> Any:
            return apply(await previous(ctx, params), ctx, _call_tool_name(params))

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
        return apply(await previous(req), None, _call_tool_name(req))

    setattr(_wrapped_v1, _WRAPPED, True)
    handlers[CallToolRequest] = _wrapped_v1
    return True
