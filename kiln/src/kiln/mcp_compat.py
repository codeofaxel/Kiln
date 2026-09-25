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

import contextlib
import contextvars
import inspect
import logging
import os
import time
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
    from mcp.server.mcpserver.exceptions import (  # type: ignore[import-not-found]
        ToolError,
    )
    from mcp.server.mcpserver.resources import (  # type: ignore[import-not-found]
        FunctionResource,
    )

    MCP_SDK_MAJOR = 2
except ImportError:  # mcp 1.x — legacy FastMCP surface
    from mcp.server.fastmcp import Context, FastMCP, Image  # type: ignore
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore
    from mcp.server.fastmcp.resources import FunctionResource  # type: ignore

    MCP_SDK_MAJOR = 1

__all__ = [
    "Context",
    "FastMCP",
    "FunctionResource",
    "Image",
    "MCP_SDK_MAJOR",
    "RESTART_HANDSHAKE_ENV",
    "RESTART_MARKER_ENV",
    "ToolError",
    "ask_user_to_confirm",
    "capture_request_context",
    "client_capabilities",
    "client_info",
    "current_session",
    "host_can_ask_the_user",
    "install_uninitialized_request_guard",
    "lowlevel_server",
    "restart_keeps_connection",
    "result_is_error",
    "result_structured_content",
    "set_result_structured_content",
    "set_instructions",
    "stamp_restart",
    "set_tool_input_schema",
    "tool_input_schema",
    "call_registered_tool",
    "tool_result_blocks",
    "uninitialized_request_message",
    "wrap_call_tool_result",
    "wrap_list_tools_result",
]


def tool_result_blocks(result: Any) -> Any:
    """The content blocks of a ``call_tool`` result, whichever SDK ran it.

    SDK 1 answers with the block list itself, or a ``(blocks, structured)``
    tuple when the caller asked it to convert; SDK 2 answers with a
    ``CallToolResult`` that carries them on ``.content``.  Readers go
    through here so neither shape has to be known twice.
    """
    if isinstance(result, tuple):
        result = result[0]
    return getattr(result, "content", result)


async def call_registered_tool(mcp: Any, name: str, arguments: Any = None, *, context: Any = None):
    """Run a registered tool the way a host's ``tools/call`` does, on either SDK.

    The tool manager's ``call_tool`` grew a required ``context`` parameter in
    SDK 2: on SDK 1 it defaults to ``None`` and a two-argument call works, so a
    caller written against SDK 1 passes locally and raises ``TypeError:
    call_tool() missing 1 required positional argument`` on a tree that
    installs SDK 2 — which is CI.  The parameter is read off the signature
    rather than assumed, so a third spelling fails here, once, instead of at
    every call site.

    ``None`` is a legitimate context: the manager only hands it to the tool
    body when the tool declares a context parameter, and a tool that does is
    not one a test drives this way.
    """
    manager = getattr(mcp, "_tool_manager", mcp)
    call = manager.call_tool
    kwargs: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        if "context" in inspect.signature(call).parameters:
            kwargs["context"] = context
    return await call(name, dict(arguments or {}), **kwargs)


_SCHEMA_ATTRS = ("input_schema", "inputSchema")


def tool_input_schema(tool: Any) -> Any:
    """The ``inputSchema`` of a ``Tool`` object, whichever SDK built it.

    SDK 1 keeps the wire name as the attribute; SDK 2 renamed the field
    ``input_schema`` and kept ``inputSchema`` only as its alias, which
    attribute access does not see.  ``None`` when the object has neither.
    """
    for attr in _SCHEMA_ATTRS:
        if hasattr(tool, attr):
            return getattr(tool, attr)
    return None


def set_tool_input_schema(tool: Any, schema: Any) -> None:
    """Replace a ``Tool`` object's schema under the attribute its SDK uses."""
    for attr in _SCHEMA_ATTRS:
        if hasattr(tool, attr):
            setattr(tool, attr, schema)
            return
    raise AttributeError(f"{type(tool).__name__} carries no input schema")


_STRUCTURED_ATTRS = ("structured_content", "structuredContent")
_IS_ERROR_ATTRS = ("is_error", "isError")


def result_structured_content(result: Any) -> Any:
    """The ``structuredContent`` of a ``CallToolResult``, whichever SDK built it.

    Same rename as the schema pair above: SDK 2 calls the fields
    ``structured_content`` and ``is_error`` and keeps the wire names only as
    aliases, which attribute access does not see.  ``None`` when the object
    has neither.
    """
    for attr in _STRUCTURED_ATTRS:
        if hasattr(result, attr):
            return getattr(result, attr)
    return None


def set_result_structured_content(result: Any, value: Any) -> None:
    """Replace a result's structured content under the attribute its SDK uses.

    A write by the SDK 1 name is not merely lost on SDK 2: the result is a
    pydantic model with no field of that name, so the assignment raises --
    and every mutator on the ``tools/call`` chain swallows its own errors by
    design, so the stage, the monitor and the notes all went missing on
    SDK 2 with nothing said.
    """
    for attr in _STRUCTURED_ATTRS:
        if hasattr(result, attr):
            setattr(result, attr, value)
            return
    raise AttributeError(f"{type(result).__name__} carries no structured content")


def result_is_error(result: Any) -> bool:
    """Whether a ``CallToolResult`` reports a tool error, whichever SDK built it."""
    for attr in _IS_ERROR_ATTRS:
        if hasattr(result, attr):
            return bool(getattr(result, attr))
    return False


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


#: The handler ctx made ambient by :func:`capture_request_context`, so a
#: callee too deep to be handed one can still find the session.  SDK 1
#: never needs it: its lowlevel dispatcher sets an equivalent contextvar
#: before every handler, and ``current_session`` reads that instead.
_AMBIENT_CTX: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "kiln_mcp_request_ctx", default=None
)


def current_session(mcp: Any, ctx: Any = None) -> Any | None:
    """The connected host's session, or None.

    The session is what server-to-client notifications go out on
    (``send_tool_list_changed`` and friends), and the two majors disagree
    about where it lives — so they disagree HERE, once, rather than at
    each call site.  Same precedence as :func:`client_capabilities`, which
    answers a different question about the same object:

    1. a ``ctx`` the handler was invoked with — on SDK 2 the
       ``ServerRequestContext`` carries the session as a field, and it is
       the only place the session lives;
    2. the ambient ctx from :func:`capture_request_context`, for code
       (like the stage's resource read) that runs inside a request but is
       handed no ctx by the SDK;
    3. the 1.x location, where the lowlevel server carries the request
       context as an attribute — an attribute SDK 2 removed outright.

    Never raises: "no session" is a legitimate answer.  Diagnostics read
    resources with no request in flight at all, and the REST proxy runs
    tools with no connected host to notify.
    """
    for candidate in (ctx, _AMBIENT_CTX.get()):
        session = getattr(candidate, "session", None)
        if session is not None:
            return session
    try:
        return lowlevel_server(mcp).request_context.session
    except Exception:  # noqa: BLE001 — no session is a legitimate answer
        return None


def client_info(mcp: Any, ctx: Any = None) -> Any | None:
    """The connected host's ``clientInfo`` — its name and version — or None.

    The two majors spell the field differently on the parsed initialize
    params: SDK 1 keeps the wire's ``clientInfo``, SDK 2 renames it
    ``client_info``.  A reader that asked for only the first got None for
    every host on SDK 2, and nothing said so, so both spellings are
    resolved here, once, over the session :func:`current_session` finds.
    Never raises: "no session" and "no clientInfo" are legitimate answers.
    """
    params = getattr(current_session(mcp, ctx), "client_params", None)
    for attr in ("client_info", "clientInfo"):
        info = getattr(params, attr, None)
        if info is not None:
            return info
    return None


def capture_request_context(mcp: Any, method: str) -> bool:
    """Make *method*'s handler ctx ambient for the duration of the call.

    SDK 1 does this itself — its lowlevel dispatcher sets a contextvar
    before every handler, which is why the 1.x fallback in
    :func:`current_session` finds a session at all — so this is a no-op
    there and returns False.

    SDK 2 removed both that contextvar and ``Server.request_context``,
    handing the ``ServerRequestContext`` to the handler as an argument
    instead.  That is fine for a handler, and useless to anything the
    handler calls that the SDK does not thread a ctx through: a
    ``FunctionResource`` function takes no ctx on EITHER major (neither
    injects one), so a resource that needs the session had no route to it
    on 2.x.  Wrapping the one handler restores the ambient the rest of
    the code was already written against.

    Idempotent per method: wrapping twice would nest two identical
    context sets, and the second install is how a feature that runs
    ``install()`` again pays for the same wire twice.

    :returns: True when this call installed the capture.
    """
    if MCP_SDK_MAJOR < 2:
        return False
    server = lowlevel_server(mcp)
    entry = server.get_request_handler(method)
    if entry is None:
        return False
    previous, params_type = entry.handler, entry.params_type
    if getattr(previous, _CAPTURES, False):
        return False

    async def _wrapped(ctx: Any, params: Any) -> Any:
        token = _AMBIENT_CTX.set(ctx)
        try:
            return await previous(ctx, params)
        finally:
            _AMBIENT_CTX.reset(token)

    setattr(_wrapped, _CAPTURES, True)
    server.add_request_handler(method, params_type, _wrapped)
    return True


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


#: A yes sooner than this after the question was put was not read by a
#: person.  Reading the shortest question the dialog asks takes longer.
#: ``KILN_DIALOG_MIN_READ_S`` overrides it (``0`` turns the rule off: a
#: test host answers in no time at all).
MIN_HUMAN_ANSWER_S = 1.0


def _min_human_answer_s() -> float:
    raw = os.environ.get("KILN_DIALOG_MIN_READ_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(MIN_HUMAN_ANSWER_S)


async def ask_user_to_confirm(
    ctx: Any, message: str, *, offer_window: bool = True, offer_fleet: bool = False,
):
    """Ask the person whether this print may start — and, where a standing
    window can be honoured, for how long and where prints may start
    without asking.  Returns a :class:`kiln.print_consent.DialogAnswer`.

    The form and the parser are ``kiln.print_consent``'s
    (:func:`~kiln.print_consent.dialog_form`,
    :func:`~kiln.print_consent.answer_from_content`) — the same ones the
    hosted wire and a native app's sheet use — so this function is only
    the MCP host's way of drawing them: ``ctx.elicit`` sends the SDK's
    ``elicitation/create`` request to the client and the client's
    JSON-RPC response is the answer.  The agent's only channel to this
    server is ``tools/call``; nothing it can call reaches this function
    or supplies a result to it.  That is what lets a "for a while" answer
    open a standing window honestly — the person picked it, on a dialog
    the host drew.

    ``"unavailable"`` means the question could not be put (no session, an
    SDK that cannot elicit, a transport error) — or that the host's answer
    was not on the form.  Callers treat it as "not asked", never as
    "asked and approved".

    Form-mode elicitation carries a message and a flat schema of
    primitives; it cannot render the model.  So this asks a question, it
    does not show a picture — see ``print_consent`` for how the two are
    kept honest.
    """
    from kiln.print_consent import DialogAnswer, answer_from_content, dialog_form

    try:
        schema = dialog_form(offer_window=offer_window, offer_fleet=offer_fleet)
    except Exception as exc:  # noqa: BLE001 — no pydantic, no elicitation
        _logger.debug("Could not build the confirmation form: %s", exc)
        return DialogAnswer("unavailable", f"form_unavailable:{type(exc).__name__}")

    asked = message
    for last_try in (False, True):
        started = time.monotonic()
        try:
            result = await ctx.elicit(message=asked, schema=schema)
        except Exception as exc:  # noqa: BLE001 — a host that cannot answer is not an error
            _logger.debug("Could not ask the user for confirmation: %s", exc)
            return DialogAnswer("unavailable", f"{type(exc).__name__}")
        elapsed = time.monotonic() - started
        data = getattr(result, "data", None)
        if data is None:
            data = getattr(result, "content", None)
        answer = answer_from_content(
            getattr(result, "action", ""), data, offer_window=offer_window, offer_fleet=offer_fleet,
        )
        if not (answer.accepted and elapsed < _min_human_answer_s()):
            return answer
        # A yes that lands before a person could have read the question was
        # not read by a person: a host's own hook, or a click that was not
        # looking.  Ask once more, saying so; a second instant yes is no
        # answer at all, and the caller moves to the next door.
        if last_try:
            break
        _logger.warning("approval dialog answered in %d ms — faster than a person reads; asking again", int(elapsed * 1000))
        asked = (
            "(Asked again: the first answer came back faster than a person could have read this.)\n\n"
            + message
        )
    return DialogAnswer("unavailable", "answered_too_fast")


def set_instructions(mcp: Any, text: str) -> None:
    """Replace the server instructions after construction.

    ``instructions`` is a read-only property on both SDK majors, so the
    rebuild-after-config-load path writes to the lowlevel server object.
    """
    lowlevel_server(mcp).instructions = text


#: Marks our wrapper so a second install is a no-op rather than a second layer.
#: Marks a handler already wrapped by :func:`capture_request_context`.
_CAPTURES = "_kiln_captures_request_ctx"

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

    def _three(result: Any, ctx: Any, name: str | None, _args: dict | None) -> Any:
        # Handed back, not dropped: a coroutine function's coroutine has
        # to reach the chain runner that awaits it.
        return mutate(result, ctx, name)

    return _three


def wrap_call_tool_result(mcp: Any, mutate: Any) -> bool:
    """Wrap the lowlevel ``tools/call`` handler so ``mutate`` sees each result.

    ``mutate(result, ctx, name)`` — or ``mutate(result, ctx, name,
    arguments)`` — is called with the tool result object AFTER the real
    handler produced it, and mutates it in place; its return value is
    ignored and it must not raise (callers wrap their own body).  It may be
    a coroutine function: the coroutine is awaited in place, on the
    handler's own loop, so a mutator with blocking work to do (an upload)
    hands that work to a thread and the server keeps serving meanwhile —
    a plain function would stall the whole stdio server for the transfer.
    A sync mutator is called exactly as before.  ``ctx`` is
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

    async def _run_all(
        result: Any, ctx: Any, name: str | None, args: dict | None, chain: list
    ) -> None:
        import inspect

        for _identity, fn in list(chain):
            try:
                out = fn(result, ctx, name, args)
                if inspect.isawaitable(out):
                    await out
            except Exception:  # noqa: BLE001 -- one bad mutator, not all
                _logger.debug("call-tool mutator failed", exc_info=True)

    def _wrap(previous: Any) -> Any:
        """Shared body: run the handler, let ``mutate`` see the result."""

        async def _apply(resp: Any, ctx: Any, name: str | None, args: dict | None) -> Any:
            # 1.x hands back a ServerResult with the real result on ``.root``;
            # 2.x hands back the CallToolResult itself, which has no ``.root``.
            await _run_all(getattr(resp, "root", resp), ctx, name, args, chain)
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
            return await apply(
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
        return await apply(
            await previous(req), None, _call_tool_name(req), _call_tool_args(req)
        )

    setattr(_wrapped_v1, _WRAPPED, True)
    setattr(_wrapped_v1, _MUTATORS, chain)
    handlers[CallToolRequest] = _wrapped_v1
    return True


_LIST_MUTATORS = "_kiln_list_tools_mutators"


def wrap_list_tools_result(mcp: Any, mutate: Any) -> bool:
    """Wrap the lowlevel ``tools/list`` handler so ``mutate`` sees each answer.

    ``mutate(tools)`` is called with the list of ``Tool`` objects AFTER the
    real handler built it, and works on them in place — it must not raise
    (callers wrap their own body); its return value is ignored.  This is the
    one door every client's view of a tool's schema passes through, on every
    transport, for every plugin that registered into this server, so a
    change to what Kiln PUBLISHES about a tool belongs here and nowhere else.

    The SDK majors disagree about the handler exactly as they do for
    ``tools/call`` (see ``wrap_call_tool_result``): keyed by request type and
    called ``handler(req)`` with the result on ``.root`` on 1.x; keyed by the
    method string, called ``handler(ctx, params)``, and answering the
    ``ListToolsResult`` itself on 2.x.  Same composition rule too — different
    callers chain in install order, the same caller (by ``module.qualname``)
    attaches once.

    Returns True when this mutator is newly registered, False when there is
    no handler to wrap or this exact mutator is already in the chain.
    """
    server = lowlevel_server(mcp)
    identity = f"{getattr(mutate, '__module__', '?')}."\
               f"{getattr(mutate, '__qualname__', repr(mutate))}"

    def _run_all(tools: Any, chain: list) -> None:
        for _identity, fn in list(chain):
            try:
                fn(tools)
            except Exception:  # noqa: BLE001 -- one bad mutator, not all
                _logger.debug("list-tools mutator failed", exc_info=True)

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/list")
        if entry is None:
            return False
        existing = getattr(entry.handler, _LIST_MUTATORS, None)
        if existing is not None:
            if identity in {k for k, _ in existing}:
                return False
            existing.append((identity, mutate))
            return True
        previous, params_type = entry.handler, entry.params_type
        chain: list = [(identity, mutate)]

        async def _wrapped_v2(ctx: Any, params: Any) -> Any:
            resp = await previous(ctx, params)
            _run_all(getattr(resp, "tools", None) or [], chain)
            return resp

        setattr(_wrapped_v2, _LIST_MUTATORS, chain)
        server.add_request_handler("tools/list", params_type, _wrapped_v2)
        return True

    from mcp.types import ListToolsRequest  # 1.x keys the dict by request type

    handlers = getattr(server, "request_handlers", None) or {}
    previous = handlers.get(ListToolsRequest)
    if previous is None:
        return False
    existing = getattr(previous, _LIST_MUTATORS, None)
    if existing is not None:
        if identity in {k for k, _ in existing}:
            return False
        existing.append((identity, mutate))
        return True
    chain = [(identity, mutate)]

    async def _wrapped_v1(req: Any) -> Any:
        resp = await previous(req)
        result = getattr(resp, "root", resp)
        _run_all(getattr(result, "tools", None) or [], chain)
        return resp

    setattr(_wrapped_v1, _LIST_MUTATORS, chain)
    handlers[ListToolsRequest] = _wrapped_v1
    return True


# ---------------------------------------------------------------------------
# A restart that keeps the connection
# ---------------------------------------------------------------------------
#
# ``restart_server`` re-execs over the running process, and ``os.execve``
# keeps the stdio pipe: the host sees no disconnect and never repeats the
# ``initialize`` handshake, so its next request lands on a fresh process
# that has not been initialized.  Both SDK majors refuse that with the
# JSON-RPC boilerplate "Invalid request parameters", which reads as a bad
# argument.  Measured 2026-09-23, twice: the agent blamed a parameter it had
# passed (``printer_status(detail=...)``, then ``slice_model(...,
# printer_name=...)``), neither of which had changed in any commit between
# the restarts, and dropped it.  The refused state never clears on its own:
# the same call retried on the same connection is refused again
# (reproduced against a real ``kiln serve`` and ``restart_server``).
#
# The handshake belongs to the pipe, not to the process: the host made it,
# once, with this connection.  So the process that owns the pipe records it
# (the client's ``initialize`` params and the negotiated version),
# ``restart_server`` hands it to the process it execs, and that process
# takes it up on the first request — as if it had answered ``initialize``
# itself, capabilities included, so a host that can put a consent dialog in
# front of the person still can.  A request that arrives before any
# handshake and with none handed down is still refused, in words that name
# the restart when there was one.  One place for both majors, like
# everything else in this module; the server installs it once at startup.

#: Set in the environment ``restart_server`` hands its child (an ISO time),
#: so the fresh process can name the restart in what it says.
RESTART_MARKER_ENV = "KILN_RESTARTED_AT"

#: Set beside it: the handshake this connection made, as JSON —
#: ``{"params": <initialize params, wire form>, "protocol_version": ...}``.
RESTART_HANDSHAKE_ENV = "KILN_RESTART_HANDSHAKE"

#: The SDK's own words for the refusal, on both majors.
_SDK_UNINITIALIZED_TEXT = "Invalid request parameters"

_GUARDED = "_kiln_guards_uninitialized_requests"

#: This process's connection handshake, once ``initialize`` completed or a
#: handed-down one was taken up.  What ``restart_server`` passes on.
_handshake: dict[str, Any] | None = None

#: A handshake handed down by ``restart_server`` that no request has taken
#: up yet.  Consumed by the first request, whatever it is.
_inherited: dict[str, Any] | None = None


def stamp_restart(env: dict[str, str]) -> dict[str, str]:
    """Mark *env* — the environment a restart hands its child — with the
    moment of the restart and, when this connection has one, its handshake.
    Returns *env*."""
    import json
    from datetime import datetime

    env[RESTART_MARKER_ENV] = datetime.now().astimezone().isoformat(timespec="seconds")
    if _handshake is not None:
        env[RESTART_HANDSHAKE_ENV] = json.dumps(_handshake)
    else:
        env.pop(RESTART_HANDSHAKE_ENV, None)
    return env


def restart_keeps_connection() -> bool:
    """Whether a restart now would hand this connection's handshake on."""
    return _handshake is not None


def _take_inherited_handshake() -> None:
    """Pick up a handshake ``restart_server`` handed this process, once.

    Popped from the environment, so nothing this process spawns inherits a
    connection it does not have."""
    import json
    import os

    global _inherited
    raw = os.environ.pop(RESTART_HANDSHAKE_ENV, None)
    if not raw:
        return
    try:
        record = json.loads(raw)
    except ValueError:
        _logger.warning("restart handed down an unreadable MCP handshake; ignoring it")
        return
    if isinstance(record, dict) and isinstance(record.get("params"), dict):
        _inherited = record


def _restart_clock() -> str:
    """The wall-clock time of the restart this process came from, or ``""``."""
    import os

    raw = (os.environ.get(RESTART_MARKER_ENV) or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw).strftime("%H:%M:%S")
    except ValueError:
        return raw


def uninitialized_request_message(method: str | None = None) -> str:
    """What to say when a request arrives on a connection that never
    completed the MCP handshake — in words that name the cause."""
    what = "this tool call" if (method or "tools/call") == "tools/call" else f"this {method} request"
    when = _restart_clock()
    if when:
        cause = (
            f"Kiln's server restarted at {when} (restart_server) and this "
            "connection has not re-initialized since"
        )
    else:
        cause = "this connection to Kiln has not completed the MCP initialize handshake"
    return (
        f"{cause}, so {what} never ran — the parameters you passed were not "
        "the problem. Retry it unchanged once; if it is refused again, the "
        "Kiln MCP server needs reconnecting in the app (or a new chat)."
    )


def install_uninitialized_request_guard(mcp: Any) -> bool:
    """Keep the connection across ``restart_server``, and explain a refusal
    when it cannot be kept — on whichever SDK is running.

    1.x: the handshake gate lives in ``ServerSession._received_request``,
    which raises RuntimeError and lets the receive loop write the
    boilerplate.  The guard wraps that method on the class (sessions are
    built per connection deep inside ``Server.run``, so the class is the one
    place): it records the handshake when ``initialize`` completes, takes up
    a handed-down one before the gate would refuse, and otherwise answers
    the request itself, with the sentence, before the loop can.

    2.x: the gate lives in the per-connection runner, inside the innermost
    link of ``Server.middleware``; a middleware appended there does the same
    three things around it.

    Idempotent.  Never raises: a server that cannot keep a connection still
    serves.  Returns True when the guard is in place.
    """
    try:
        _take_inherited_handshake()
        if MCP_SDK_MAJOR >= 2:
            return _guard_v2(mcp)
        return _guard_v1()
    except Exception:  # noqa: BLE001 — an unexplained refusal is not a failed server
        _logger.debug("uninitialized-request guard not installed", exc_info=True)
        return False


def _take_up() -> dict[str, Any] | None:
    """The handed-down handshake, consumed — or None when there is none."""
    global _inherited, _handshake
    record, _inherited = _inherited, None
    if record is None:
        return None
    _handshake = record
    client = (record.get("params") or {}).get("clientInfo") or {}
    _logger.info(
        "Kept the MCP connection across restart_server: took up the handshake "
        "%s %s made before the restart.",
        client.get("name", "the client"), client.get("version", ""),
    )
    return record


def _guard_v1() -> bool:
    from mcp.server import session as _session_mod
    from mcp.types import INVALID_PARAMS, ErrorData, InitializeRequest, InitializeRequestParams

    cls = _session_mod.ServerSession
    if getattr(cls, _GUARDED, False):
        return True
    original = cls._received_request
    initialized = _session_mod.InitializationState.Initialized

    async def _guarded(self: Any, responder: Any) -> Any:
        global _handshake, _inherited
        request = getattr(responder.request, "root", None)
        is_initialize = isinstance(request, InitializeRequest)
        if is_initialize:
            _inherited = None  # the client is handshaking afresh
        elif getattr(self, "_initialization_state", initialized) != initialized and _inherited:
            record = _take_up()
            if record is not None:
                self._client_params = InitializeRequestParams.model_validate(record["params"])
                self._initialization_state = initialized
        try:
            result = await original(self, responder)
        except RuntimeError:
            if (
                getattr(self, "_initialization_state", initialized) == initialized
                or getattr(responder, "_completed", False)
            ):
                raise  # not the handshake gate — the SDK's own business
            method = getattr(request, "method", None)
            message = uninitialized_request_message(method)
            _logger.warning("refused %s before initialization: %s", method, message)
            with responder:
                await responder.respond(ErrorData(code=INVALID_PARAMS, message=message, data=""))
            return None
        if is_initialize and getattr(self, "_client_params", None) is not None:
            from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
            from mcp.types import LATEST_PROTOCOL_VERSION

            params = self._client_params
            requested = getattr(params, "protocolVersion", None)
            _handshake = {
                "params": params.model_dump(mode="json", by_alias=True, exclude_none=True),
                "protocol_version": (
                    requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
                ),
            }
        return result

    cls._received_request = _guarded  # type: ignore[method-assign]
    setattr(cls, _GUARDED, True)
    return True


def _guard_v2(mcp: Any) -> bool:
    from mcp.shared.exceptions import MCPError  # type: ignore[import-not-found]
    from mcp.types import INVALID_PARAMS, InitializeRequestParams

    middleware = getattr(lowlevel_server(mcp), "middleware", None)
    if not isinstance(middleware, list):
        return False
    if any(getattr(m, _GUARDED, False) for m in middleware):
        return True

    def _adopt(connection: Any, record: dict[str, Any]) -> None:
        connection.client_params = InitializeRequestParams.model_validate(record["params"], by_name=False)
        version = record.get("protocol_version")
        try:
            from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS  # type: ignore[import-not-found]

            if version in HANDSHAKE_PROTOCOL_VERSIONS:
                connection.protocol_version = version
        except ImportError:
            pass
        # The client sent ``notifications/initialized`` to the process
        # before this one; server-initiated requests (a consent dialog) are
        # allowed from here, as they were there.
        connection.initialized.set()

    class _Guard:
        async def __call__(self, ctx: Any, call_next: Any) -> Any:
            global _handshake, _inherited
            method = getattr(ctx, "method", None)
            connection = getattr(getattr(ctx, "session", None), "_connection", None)
            if method == "initialize":
                _inherited = None  # the client is handshaking afresh
            elif (
                _inherited
                and connection is not None
                and not getattr(connection, "initialize_accepted", True)
            ):
                record = _take_up()
                if record is not None:
                    _adopt(connection, record)
            try:
                result = await call_next(ctx)
            except MCPError as exc:
                error = getattr(exc, "error", None)
                if (
                    error is None
                    or error.code != INVALID_PARAMS
                    or error.message != _SDK_UNINITIALIZED_TEXT
                    or getattr(connection, "initialize_accepted", True)
                ):
                    raise
                message = uninitialized_request_message(method)
                _logger.warning("refused %s before initialization: %s", method, message)
                raise MCPError(code=INVALID_PARAMS, message=message, data="") from exc
            if method == "initialize" and isinstance(result, dict):
                params = InitializeRequestParams.model_validate(dict(ctx.params or {}), by_name=False)
                _handshake = {
                    "params": params.model_dump(mode="json", by_alias=True, exclude_none=True),
                    "protocol_version": result.get("protocolVersion"),
                }
            return result

    setattr(_Guard, _GUARDED, True)
    middleware.append(_Guard())
    return True
