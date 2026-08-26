"""``wrap_call_tool_result`` against the SDK that is actually installed.

The stage's token hook wraps the lowlevel ``tools/call`` handler, and the two
SDK majors disagree about that handler completely: 1.x keys it by request
TYPE in a plain dict and calls it ``handler(req)``; 2.x keys it by the method
string ``"tools/call"``, wraps it in a ``HandlerEntry`` carrying the params
type, and calls it ``handler(ctx, params)``.  ``mcp_compat`` absorbs all of
that so ``local_stage`` describes only WHAT to attach.

This file necessarily speaks both dialects — a compat test is the one place
that should — and asserts the same three properties either way: the wrapper
runs, the handler's own return value survives it, and installing twice does
not stack two layers.

It is behavioural on purpose.  ``install()`` reporting ``token_hook: True``
only says a wrapper was registered; on SDK 2 that flag was False for a
different reason at every stage of fixing this, and a flag is not a rendered
panel.
"""

from __future__ import annotations

import anyio
import pytest

from kiln.mcp_compat import (
    MCP_SDK_MAJOR,
    FastMCP,
    lowlevel_server,
    wrap_call_tool_result,
)


class _Result:
    """Stands in for a CallToolResult — only the field the hook mutates."""

    def __init__(self) -> None:
        self.structuredContent: dict = {"success": True}


def _server_with_base_handler(result: _Result):
    """A real server whose ``tools/call`` returns ``result``.

    Returns ``(server, invoke)`` where ``invoke()`` drives whatever handler is
    currently registered, with the argument shape that major expects.
    """
    mcp = FastMCP("wrapper-probe")

    @mcp.tool(name="t")
    def _t() -> dict:  # registers the real tools/call handler to displace
        return {"ok": True}

    server = lowlevel_server(mcp)

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")

        async def _base(_ctx, _params):
            return result

        server.add_request_handler("tools/call", entry.params_type, _base)

        def _invoke(ctx=None):
            handler = server.get_request_handler("tools/call").handler
            return anyio.run(handler, ctx, None)

        return mcp, _invoke

    from mcp.types import CallToolRequest

    handlers = server.request_handlers

    async def _base_v1(_req):
        # 1.x returns a ServerResult wrapping the real result on ``.root``.
        return type("ServerResult", (), {"root": result})()

    handlers[CallToolRequest] = _base_v1

    def _invoke(ctx=None):
        # 1.x handlers take no ctx; the wrapper hands the callback None.
        return anyio.run(handlers[CallToolRequest], None)

    return mcp, _invoke


def test_the_wrapper_actually_sees_the_result():
    """The property the 3D stage depends on: something can mutate the result
    on its way out, on whichever SDK is installed."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    assert wrap_call_tool_result(mcp, lambda inner, ctx, name: inner.structuredContent.update(seen=True))
    invoke()

    assert result.structuredContent.get("seen") is True, (
        f"the wrapper never ran on SDK major {MCP_SDK_MAJOR}"
    )


def test_the_handlers_own_return_value_survives():
    """A wrapper that swallowed the response would break every tool call, not
    just the stage — the failure mode worth being loudest about."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)
    wrap_call_tool_result(mcp, lambda inner, ctx, name: None)

    resp = invoke()

    assert resp is not None
    assert getattr(resp, "root", resp) is result


def test_installing_twice_does_not_stack():
    """``install()`` runs per server, and a doubled wrapper would attach the
    token twice and pay for the geometry twice."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    calls: list[int] = []
    assert wrap_call_tool_result(mcp, lambda inner, ctx, name: calls.append(1)) is True
    assert wrap_call_tool_result(mcp, lambda inner, ctx, name: calls.append(1)) is False

    invoke()
    assert len(calls) == 1


def test_the_callback_receives_this_calls_request_context():
    """On SDK 2 the per-call ctx is the ONLY place the session lives; a
    wrapper that dropped it would make every host capability read answer
    "declared nothing" — silently, per call.  On 1.x there is no per-call
    ctx and None is the contract (the session lives on the server there)."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    seen: list = []
    wrap_call_tool_result(mcp, lambda inner, ctx, name: seen.append((inner, ctx)))

    if MCP_SDK_MAJOR >= 2:
        sentinel = object()
        invoke(sentinel)
        assert seen == [(result, sentinel)]
    else:
        invoke()
        assert seen == [(result, None)]


def test_no_handler_to_wrap_is_a_false_not_a_crash():
    """A server with nothing registered is a legitimate state — the stage
    reports it and moves on rather than taking the server down with it."""
    mcp = FastMCP("empty-probe")
    server = lowlevel_server(mcp)

    if MCP_SDK_MAJOR >= 2:
        if server.get_request_handler("tools/call") is not None:
            pytest.skip("this SDK registers tools/call eagerly")
    else:
        from mcp.types import CallToolRequest

        server.request_handlers.pop(CallToolRequest, None)

    assert wrap_call_tool_result(mcp, lambda inner, ctx, name: None) is False


def test_the_callback_receives_the_tool_name():
    """The stage decides per TOOL what to attach, so the name must survive
    the wrapper — through whichever request shape this SDK uses.  A shape
    that yields no name hands the callback None (the fail-open reading);
    that path is covered by every other test here, which invokes with
    params=None."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    seen: list = []
    wrap_call_tool_result(mcp, lambda inner, ctx, name: seen.append(name))

    from mcp.types import CallToolRequestParams

    params = CallToolRequestParams(name="probe_tool", arguments={})
    server = lowlevel_server(mcp)

    if MCP_SDK_MAJOR >= 2:
        handler = server.get_request_handler("tools/call").handler
        anyio.run(handler, None, params)
    else:
        from mcp.types import CallToolRequest

        req = CallToolRequest(method="tools/call", params=params)
        anyio.run(server.request_handlers[CallToolRequest], req)

    assert seen == ["probe_tool"], (
        f"tool name did not reach the callback on SDK major {MCP_SDK_MAJOR}: {seen}"
    )


def test_a_four_arg_callback_receives_the_call_arguments():
    """The monitor attaches for the MACHINE a call named, so the call's own
    arguments must survive the wrapper — while three-arg mutators (the
    stage, anything older) keep working unchanged beside it."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    seen: list = []
    legacy: list = []
    wrap_call_tool_result(
        mcp, lambda inner, ctx, name, args: seen.append((name, args))
    )

    def _legacy(inner, ctx, name):
        legacy.append(name)

    wrap_call_tool_result(mcp, _legacy)

    from mcp.types import CallToolRequestParams

    params = CallToolRequestParams(
        name="probe_tool", arguments={"printer_name": "workshop-a1"}
    )
    server = lowlevel_server(mcp)

    if MCP_SDK_MAJOR >= 2:
        handler = server.get_request_handler("tools/call").handler
        anyio.run(handler, None, params)
    else:
        from mcp.types import CallToolRequest

        req = CallToolRequest(method="tools/call", params=params)
        anyio.run(server.request_handlers[CallToolRequest], req)

    assert seen == [("probe_tool", {"printer_name": "workshop-a1"})], (
        f"call arguments did not reach the callback on SDK major "
        f"{MCP_SDK_MAJOR}: {seen}"
    )
    assert legacy == ["probe_tool"], "a three-arg mutator must keep working"


def test_an_argless_request_shape_hands_the_callback_none():
    """Every other test invokes with params=None; this pins that the 4-arg
    convention reads that shape as None rather than crashing the chain."""
    result = _Result()
    mcp, invoke = _server_with_base_handler(result)

    seen: list = []
    wrap_call_tool_result(mcp, lambda inner, ctx, name, args: seen.append(args))
    invoke()
    assert seen == [None]
