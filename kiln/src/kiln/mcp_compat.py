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

import logging
from typing import Any

_logger = logging.getLogger(__name__)

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
    "ask_user_to_confirm",
    "client_capabilities",
    "host_can_ask_the_user",
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


def host_can_ask_the_user(mcp: Any, ctx: Any = None) -> bool:
    """True when the connected host can put a question in front of a person.

    MCP calls this elicitation: the SERVER asks, the CLIENT draws the
    prompt, the human answers.  It matters here because every consent
    Kiln has had until now was the other shape — the server hands the
    agent a token and trusts the agent to have asked.  That proves a
    preview was rendered, never that anyone saw it.

    A host that declares nothing gets ``False`` and keeps the old
    token-based gate.  Not every caller has a person attached: the REST
    proxy runs tools server-side with nobody to ask, and refusing those
    callers would break them rather than protect anyone.
    """
    caps = client_capabilities(mcp, ctx)
    return getattr(caps, "elicitation", None) is not None


async def ask_user_to_confirm(ctx: Any, message: str) -> tuple[str, str]:
    """Ask the person a yes/no question.  Returns ``(action, detail)``.

    ``action`` is one of ``"accept"``, ``"decline"``, ``"cancel"`` or
    ``"unavailable"``.  MCP distinguishes the middle two deliberately —
    declining is an answer, dismissing the dialog is not — and a print is
    worth telling apart, so this does not collapse them.

    ``"unavailable"`` means the question could not be put (no session, an
    SDK that cannot elicit, a transport error).  Callers must treat it as
    "not asked", never as "asked and approved".

    Form-mode elicitation carries a message and a flat schema of
    primitives; it cannot render the model.  So this asks a question, it
    does not show a picture — see ``print_consent`` for how the two are
    kept honest.
    """
    try:
        from pydantic import BaseModel, Field
    except Exception:  # noqa: BLE001 — no pydantic, no elicitation
        return "unavailable", "pydantic_unavailable"

    # Everything about this model is user-visible: hosts render the class
    # name as the dialog title and the docstring as its description, so
    # neither may explain the implementation to the person being asked.
    # (Measured against a live session — the first version put "the spec
    # allows only flat primitives" in front of the user.)
    class StartThisPrint(BaseModel):
        """Confirm before the printer starts."""

        approved: bool = Field(
            default=False,
            description="Yes, start this print. No leaves the printer idle.",
        )

    try:
        result = await ctx.elicit(message=message, schema=StartThisPrint)
    except Exception as exc:  # noqa: BLE001 — a host that cannot answer is not an error
        _logger.debug("Could not ask the user for confirmation: %s", exc)
        return "unavailable", f"{type(exc).__name__}"

    action = str(getattr(result, "action", "") or "").lower()
    if action == "accept":
        data = getattr(result, "data", None)
        if data is None:
            data = getattr(result, "content", None)
        approved = getattr(data, "approved", None)
        if approved is None and isinstance(data, dict):
            approved = data.get("approved")
        # An "accept" carrying approved=False is a person who opened the
        # dialog and said no.  That is a decline, whatever the envelope
        # calls it.
        if bool(approved):
            return "accept", ""
        return "decline", "answered_no"
    if action in ("decline", "cancel"):
        return action, ""
    return "unavailable", f"unexpected_action:{action or 'none'}"


def set_instructions(mcp: Any, text: str) -> None:
    """Replace the server instructions after construction.

    ``instructions`` is a read-only property on both SDK majors, so the
    rebuild-after-config-load path writes to the lowlevel server object.
    """
    lowlevel_server(mcp).instructions = text


#: Marks our wrapper so a second install is a no-op rather than a second layer.
_WRAPPED = "_kiln_wrapped_call_tool"
# The mutator list carried by an installed wrapper.  A SECOND caller
# appends to it instead of being turned away: before this existed the
# already-wrapped guard returned False, so whichever feature installed
# second silently did nothing forever — a wire that reports success by
# staying quiet is the worst shape a wire can have.
_MUTATORS = "_kiln_call_tool_mutators"


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


def _call_tool_args(source: Any) -> dict | None:
    """Best-effort call arguments from a ``tools/call`` handler's input.

    Same shapes as :func:`_call_tool_name`, same posture: anything
    unreadable is ``None``, and a mutator treats that as "the call named
    no arguments" — never as a reason to skip its work.
    """
    for obj in (
        source,
        getattr(source, "params", None),
        getattr(getattr(source, "root", None), "params", None),
    ):
        args = getattr(obj, "arguments", None)
        if isinstance(args, dict):
            return args
        if isinstance(obj, dict):
            candidate = obj.get("arguments")
            if isinstance(candidate, dict):
                return candidate
    return None


def _adapt_mutator(mutate: Any) -> Any:
    """Normalise a mutator to the 4-arg calling convention.

    Mutators predate the ``arguments`` parameter and are registered by
    other modules (and potentially other packages), so the chain accepts
    both shapes: ``fn(result, ctx, name)`` and
    ``fn(result, ctx, name, arguments)``.  Arity is read once here rather
    than probed with a TypeError per call — a mutator that itself raises
    TypeError must surface as ITS failure, not be silently retried with
    fewer arguments.
    """
    import inspect

    try:
        params = [
            p
            for p in inspect.signature(mutate).parameters.values()
            if p.kind
            in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        ]
        wants_args = len(params) >= 4 or any(
            p.kind == p.VAR_POSITIONAL for p in params
        )
    except (TypeError, ValueError):
        wants_args = False
    if wants_args:
        return mutate

    def _three(result: Any, ctx: Any, name: str | None, _args: dict | None) -> None:
        mutate(result, ctx, name)

    return _three


def wrap_call_tool_result(mcp: Any, mutate: Any) -> bool:
    """Wrap the lowlevel ``tools/call`` handler so ``mutate`` sees each result.

    ``mutate(result, ctx, name)`` — or ``mutate(result, ctx, name,
    arguments)`` — is called with the tool result object AFTER the real
    handler produced it, and mutates it in place; its return value is
    ignored and it must not raise (callers wrap their own body).  ``ctx`` is
    the ``ServerRequestContext`` SDK 2 hands the handler — the only place the
    session (and so the host's declared capabilities) lives on 2.x — and None
    on 1.x, where ``client_capabilities`` reads the lowlevel server attribute
    instead.  ``name`` is the called tool's name when the request shape
    yields one (best-effort via ``_call_tool_name``), else None — it lets the
    stage decide per TOOL what to attach, not just per host.  ``arguments``
    is the call's own argument dict when the request shape yields one
    (best-effort via ``_call_tool_args``), else None — it lets a mutator
    attach for the MACHINE a call named, not just the default; a mutator
    declared with three positional parameters simply never sees it
    (arity is read once at registration, in ``_adapt_mutator``).  The
    handler's own return value is passed through untouched, so a wrapper
    that does nothing is invisible.

    Everything the two SDK majors disagree about lives here, because the
    disagreement is total — the handler is keyed by request TYPE on 1.x and by
    the method string ``"tools/call"`` on 2.x; it is called ``handler(req)``
    on 1.x and ``handler(ctx, params)`` on 2.x; and 2.x stores it in a
    ``HandlerEntry`` alongside the params type it must be re-registered with.
    A caller that branched on any of that would be a second place that knows
    which SDK is installed, which is the thing this module exists to prevent.

    DIFFERENT callers COMPOSE; the SAME caller is idempotent.  The first
    install wraps the handler, and each later one appends its mutator to
    that chain in install order — but a mutator whose identity
    (``module.qualname``) is already registered is ignored, so a feature
    whose ``install()`` runs twice still attaches once.  Both halves are
    load-bearing: without composition the second FEATURE was turned away
    with a False nobody checked and silently never ran; without the
    identity guard a re-installed feature would attach twice and pay for
    its work twice.  Each mutator is isolated — one that raises is logged
    and skipped, so it cannot cost a sibling its attach or the caller
    their result.

    Returns True when this mutator is newly registered, False when there
    is no handler to wrap or this exact mutator is already in the chain.
    Never raises for the ordinary reasons; an exotic server object
    propagates, and callers decide.
    """
    server = lowlevel_server(mcp)
    identity = f"{getattr(mutate, '__module__', '?')}."\
               f"{getattr(mutate, '__qualname__', repr(mutate))}"

    def _run_all(
        result: Any, ctx: Any, name: str | None, args: dict | None, chain: list
    ) -> None:
        for _identity, fn in list(chain):
            try:
                fn(result, ctx, name, args)
            except Exception:  # noqa: BLE001 -- one bad mutator, not all
                _logger.debug("call-tool mutator failed", exc_info=True)

    def _wrap(previous: Any) -> Any:
        """Shared body: run the handler, let ``mutate`` see the result."""

        def _apply(resp: Any, ctx: Any, name: str | None, args: dict | None) -> Any:
            # 1.x hands back a ServerResult with the real result on ``.root``;
            # 2.x hands back the CallToolResult itself, which has no ``.root``.
            _run_all(getattr(resp, "root", resp), ctx, name, args, chain)
            return resp

        return _apply

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")
        if entry is None:
            return False
        existing = getattr(entry.handler, _MUTATORS, None)
        if existing is not None:
            if identity in {k for k, _ in existing}:
                return False  # same feature installing twice — attach once
            existing.append((identity, _adapt_mutator(mutate)))
            return True
        previous, params_type = entry.handler, entry.params_type
        chain: list = [(identity, _adapt_mutator(mutate))]
        apply = _wrap(previous)

        async def _wrapped_v2(ctx: Any, params: Any) -> Any:
            return apply(
                await previous(ctx, params),
                ctx,
                _call_tool_name(params),
                _call_tool_args(params),
            )

        setattr(_wrapped_v2, _WRAPPED, True)
        setattr(_wrapped_v2, _MUTATORS, chain)
        server.add_request_handler("tools/call", params_type, _wrapped_v2)
        return True

    from mcp.types import CallToolRequest  # 1.x keys the dict by request type

    handlers = getattr(server, "request_handlers", None) or {}
    previous = handlers.get(CallToolRequest)
    if previous is None:
        return False
    existing = getattr(previous, _MUTATORS, None)
    if existing is not None:
        if identity in {k for k, _ in existing}:
            return False  # same feature installing twice — attach once
        existing.append((identity, _adapt_mutator(mutate)))
        return True
    chain = [(identity, _adapt_mutator(mutate))]
    apply = _wrap(previous)

    async def _wrapped_v1(req: Any) -> Any:
        return apply(
            await previous(req), None, _call_tool_name(req), _call_tool_args(req)
        )

    setattr(_wrapped_v1, _WRAPPED, True)
    setattr(_wrapped_v1, _MUTATORS, chain)
    handlers[CallToolRequest] = _wrapped_v1
    return True
