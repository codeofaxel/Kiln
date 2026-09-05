"""Kiln's inline print monitor, served by a locally installed Kiln.

WHAT THIS IS
------------
When an agent checks on a print, the host can open a live monitor panel
right in the conversation — state, progress, wall-clock finish, temps, and
the camera when a print is running — instead of leaving a status paragraph
to go stale in the transcript.  That panel is an MCP App (SEP-1865), the
same mechanism as the 3D stage next door in :mod:`kiln.local_stage`, and
this module is the same shape of door: resource registration, tool
stamping, and a result hook, all pure add-ons.

The panel document comes from :mod:`kiln.stage_cache` (fetched from the
hosted API, cached on disk); the payload it renders is the
``kiln.monitor.v1`` wire from :mod:`kiln.monitor_payload` — the same
contract the hosted connector's panel speaks, composed here from this
process's own printer registry.  This process holds the printer connection
(for Bambu, the one MQTT session), which is exactly why the panel must be
served from HERE and never from a sibling process.

WHY THE PAYLOAD RIDES THE RESULT
--------------------------------
Measured on a local stdio server (recorded in ``local_stage.py``): a
rendered view does NOT get permission to call tools back through the host.
So the snapshot rides each monitor result, and the agent's own watch loop —
the repeated ``monitor_print`` calls it was already making — is the
heartbeat that keeps the panel current.  Re-measuring that constraint is
what the diagnostics verb below exists for: if a current host grants the
callback, the panel live-polls and the ride-along becomes the fallback.

THE ONE PROCESS THAT OWNS THE PRINTER
-------------------------------------
Measured 2026-08-25 against a real Bambu A1, with sixteen ``kiln serve``
processes alive on one machine: the printer answered ping with no loss
while this door's own status read said ``connected: false, state:
"offline"`` — and ``lsof`` showed why.  Only four or five processes held
an ESTABLISHED TCP session to the printer's MQTT port; every other one sat
in SYN_SENT.  The machine does not REFUSE the extra connections (no RST,
which would be a fast, legible error); it silently ignores the SYN, so a
losing process retries until it times out and then honestly reports the
printer offline.  Trimming back to three servers moved a starved process
to ESTABLISHED in about twelve seconds, and the same status read returned
the live machine.

So the constraint is a small connection CEILING, not the single slot it
was first written up as — and the consequence for this module is the same
either way, only firmer: the panel must be served by a process that
ALREADY holds a session to the printer.  Spawning a sibling process to
serve a monitor is the one implementation that cannot work, because the
sibling is exactly the process the ceiling starves.  That is why this door
lives inside the server that owns the connection, and why its status axis
is a direct in-process read rather than a call to anything else.

THE ACCOUNT AXIS
----------------
The panel is free; using it live asks for the free Kiln account.  The
payload carries ``account.signed_in`` (resolved the same way
``monitor_twin.publish`` resolves it) and the panel renders the sign-in
invitation instead of the live view when it is false.  This gates the
premium RENDERING only: the text report, the camera snapshot file, and
every reading in it stay exactly as available as they have always been,
signed in or not — the floor never moves.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from kiln.mcp_compat import wrap_call_tool_result
from kiln.monitor_payload import (
    MONITOR_STRUCTURED_CONTENT_KEY,
    compose_monitor_payload,
    is_active_print_state,
)

logger = logging.getLogger(__name__)

#: Opt out of the inline monitor (matches ``KILN_NO_LOCAL_STAGE`` next door).
_OPT_OUT_ENV = "KILN_NO_LOCAL_MONITOR"

#: Registers the panel's poll verb on the standing tool surface — the
#: ``KILN_LOCAL_STAGE_DIAGNOSTICS`` precedent.  Off by default: the verb is
#: not useful to a person or an agent.  Its measurement purpose: with this
#: on, a rendered panel that CAN call tools back through the host will show
#: ``kiln_monitor_snapshot`` calls in the server log with no agent
#: involvement — the stdio-callback re-measure the spec's smoke checklist
#: asks for.
_DIAGNOSTICS_ENV = "KILN_LOCAL_MONITOR_DIAGNOSTICS"

#: Opt out of the camera frame riding the result payload.  A frame is the
#: biggest rider (~100 KB of base64), and on hosts that serialize
#: structuredContent into model context it costs tokens the way the stage's
#: inline geometry does — this is that lever's monitor twin
#: (``KILN_STAGE_INLINE_GEOMETRY`` precedent).
_INLINE_CAMERA_ENV = "KILN_MONITOR_INLINE_CAMERA"

#: The ui:// URI monitor tool declarations point at via _meta.ui.resourceUri.
PRINT_MONITOR_RESOURCE_URI = "ui://kiln/print-monitor"

#: Resource name shown in host resource listings.
PRINT_MONITOR_RESOURCE_NAME = "kiln_print_monitor"

#: Appended to every monitor tool's description at stamp time — the
#: ``STAGE_DESCRIPTION_CLAUSE`` precedent: the ``_meta`` stamp is what a
#: HOST reads, but an AGENT deciding which tool to call reads descriptions,
#: and a capability that lives only in ``_meta`` is one no keyword search
#: over the tool surface can find.  Derived from roster membership, never
#: hand-typed per tool.
MONITOR_DESCRIPTION_CLAUSE = (
    "INLINE LIVE MONITOR: on success this tool also opens Kiln's inline "
    "print monitor — a live panel with progress, temperatures, and the "
    "camera while a print runs — in hosts that render MCP Apps panels. The "
    "panel refreshes with each monitoring call, so keep watching through "
    "this tool rather than narrating stale numbers. Free with a Kiln "
    "sign-in; the text report below is always complete on its own."
)

#: Tools whose success result is a monitoring answer about a printer, so
#: opening (or refreshing) the live panel on it is what the user wanted.
#:
#: Reviewed, not derived — with the reviewing anchored, like the stage's
#: roster.  IN: the one-shot report and its structured/vision sibling —
#: both are "how's my print?" answered, and the agent's repeated calls to
#: them are the panel's heartbeat.  OUT, with reasons: ``printer_status``
#: is programmatic plumbing called from prep flows where a panel would be
#: noise; ``watch_print``/``watch_print_status``/``stop_watch_print`` are
#: watcher bookkeeping whose results attribute a printer only indirectly
#: (a wrong-machine panel is worse than none); ``printer_snapshot`` returns
#: a frame, not a state answer.
MONITOR_TOOLS: frozenset[str] = frozenset(
    {
        "monitor_print",
        "monitor_print_vision",
    }
)

#: Set once a host reads the monitor document — proof this host renders MCP
#: Apps, same one-way latch as the stage's.
_host_read_the_monitor = False

#: One log line per process — did this host take the monitor payload?
_signal_logged = False


def enabled() -> bool:
    """Whether the inline monitor runs at all on this install."""
    return (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def diagnostics_enabled() -> bool:
    return (os.environ.get(_DIAGNOSTICS_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def inline_camera_enabled() -> bool:
    return (os.environ.get(_INLINE_CAMERA_ENV) or "").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _host_renders_apps(mcp: Any, ctx: Any = None) -> bool:
    """Whether attaching the payload buys anything on this host.

    The capability question is the stage's, answered by the stage's own
    check (one engine, not a second copy) — plus this door's own latch: a
    host that read the MONITOR document is about to render the panel even
    if it never read the stage.
    """
    if _host_read_the_monitor:
        return True
    from kiln import local_stage

    return local_stage.host_renders_apps(mcp, ctx)


def _signed_in() -> bool:
    """Whether this machine has a Kiln ACCOUNT — the account axis.

    Deliberately NOT ``resolve_api_bearer().token``, which is the question
    "can I call the API right now".  Measured 2026-08-25 on a real
    machine: a signed-in enterprise account whose refresh had been
    rejected the previous day resolved to no bearer, and this panel would
    have shown its own owner the sign-in invitation.  A lapsed session and
    a stranger are not the same person, and the codebase already knows the
    difference (``auth_session._signin_hint`` speaks to the first).

    The deeper reason the bearer is the wrong question: this door is
    DIRECT.  It reads the printer in-process and never calls the API at
    all, so gating it on a live bearer gates a local capability on the
    network — which would rope every user the moment they went offline,
    in a panel whose whole point is that it works without the cloud.  A
    session that needs renewing is surfaced where a bearer is actually
    required (publishing, hosted calls), not here.

    So: an operator license, or a completed sign-in on disk.  No network,
    no refresh, no raising — an unreadable auth state reads as no account,
    which is recoverable because the next result re-resolves it.
    """
    try:
        if (os.environ.get("KILN_LICENSE_KEY") or "").strip():
            return True
        from kiln.auth_session import _read_tokens

        stored = _read_tokens()
        # The identity, not the credential: either field means someone
        # completed a sign-in on this machine.
        return bool(stored.get("auth_uid") or stored.get("email"))
    except Exception:  # noqa: BLE001 — auth trouble is not a monitor failure
        logger.debug("monitor account axis unresolved", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Payload composition — the direct transport
# ---------------------------------------------------------------------------


def _direct_status(printer_name: str | None) -> tuple[dict | None, dict | None]:
    """(status, status_failure) from this process's own registry.

    The status axis is literally ``printer_status(detail="lite")`` — the
    same tool the hosted door relays — so the panel sees one shape on every
    door.  Imported lazily from the server module, which is importable by
    the time anything calls this (it installed us).

    The refusal is UNWRAPPED here, because ``_error_dict`` nests it:
    ``{"success": false, "error": {"code", "message", "retryable"}}``.
    Reading ``error`` as a string put a dict in the failure's ``message``
    and the fallback word in its ``code`` — and the panel resolver, which
    tells "no printer configured" from "printer offline" by reading
    exactly those two, then showed a user with no printer set up the
    remedy for an unplugged one (measured against a real machine
    2026-08-25).  Both shapes are accepted: a flat refusal from any other
    caller still reads correctly.
    """
    from kiln.server import printer_status

    answer = printer_status(printer_name=printer_name, detail="lite")
    if isinstance(answer, dict) and answer.get("success") is False:
        err = answer.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message")
        else:
            code = answer.get("code")
            message = err
        return None, {
            "code": str(code or "TOOL_FAILURE"),
            "message": str(message or "printer_status refused."),
        }
    return (answer if isinstance(answer, dict) else None), None


def _camera_frame(printer_name: str | None, status: dict | None) -> tuple[str | None, str | None]:
    """(base64 frame, note).  The room-camera rule lives HERE, server-side:
    a frame is fetched only while the just-read state is an active print."""
    state = ((status or {}).get("printer") or {}).get("state")
    if not is_active_print_state(state):
        return None, "camera is off while no print is active"
    try:
        from kiln.server import _get_adapter, _get_registry

        adapter = _get_registry().get(printer_name) if printer_name else _get_adapter()
        raw = adapter.get_snapshot()
        if not raw:
            return None, "no camera available"
        return base64.b64encode(raw).decode("ascii"), None
    except Exception as exc:  # noqa: BLE001 — camera absent is context, not error
        logger.debug("monitor camera frame unavailable: %s", exc)
        return None, "camera unavailable"


def _coverage_block(printer_name: str | None) -> dict[str, Any] | None:
    """kiln-pro's coverage block for this printer's model, when present.

    One helper serves every door (``kiln.server._coverage_block_for``); this
    is the local panel's call to it.  Without kiln-pro there is no block, the
    same way there is no camera frame without a camera.
    """
    from kiln.server import _coverage_block_for

    return _coverage_block_for(printer_name)


def compose_local_payload(
    printer_name: str | None = None,
    include_camera: bool = False,
) -> dict[str, Any]:
    """The ``kiln.monitor.v1`` snapshot for THIS machine's printer.

    Direct transport: the bridge axis reports the spec's direct block —
    there is no bridge and none is needed, and the panel's resolver must be
    able to tell that from "bridge up".  Matches the hosted door's direct
    mode byte for byte.
    """
    status, status_failure = _direct_status(printer_name)
    camera_b64: str | None = None
    camera_note: str | None = None
    if include_camera:
        camera_b64, camera_note = _camera_frame(printer_name, status)
    payload = compose_monitor_payload(
        bridge={
            "online": True,
            "paired": True,
            "lastSeenAt": None,
            "transport": "direct",
        },
        bridge_error=None,
        status=status,
        status_failure=status_failure,
        camera_base64=camera_b64,
        camera_note=camera_note,
        account={"signed_in": _signed_in()},
        coverage=_coverage_block(printer_name),
    )
    if printer_name:
        # The panel polls with the same argument the entry call named, so a
        # multi-printer setup keeps watching the machine it asked about.
        payload["printer_name_arg"] = printer_name
    return payload


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _register_resource(mcp: Any) -> bool:
    """Register ``ui://kiln/print-monitor``, served from the on-disk cache.

    Lazy read at ``resources/read``, like the stage's: a server that
    started before the cache was warm serves the panel the moment the
    download lands, and one that never got a document raises there rather
    than at boot.
    """
    from kiln import stage_cache
    from kiln.mcp_compat import FunctionResource

    def _document() -> str:
        global _host_read_the_monitor
        doc = stage_cache.monitor_document()
        if not doc:
            raise ValueError(
                "Kiln's print monitor panel has not been downloaded on this "
                "machine yet."
            )
        # Only a host about to render the panel asks for this.
        _host_read_the_monitor = True
        # Door parity, the stage's rule: the read is the earliest proof a
        # rendered View will exist, and it precedes the View's first
        # tools/call — so the poll verb is registered here, off the
        # standing surface for hosts that never render panels, while a
        # host that does render can never call into a missing verb.
        _register_snapshot_verb(mcp)
        return doc

    mcp.add_resource(
        FunctionResource(
            # A plain str on purpose — the one input both SDK majors accept
            # (see the stage's registration for the 1.x/2.x split).
            uri=PRINT_MONITOR_RESOURCE_URI,
            name=PRINT_MONITOR_RESOURCE_NAME,
            title="Kiln Print Monitor",
            description=(
                "Live inline monitor for a running print — camera, progress, "
                "temperatures, and connection state on Kiln's dark surface."
            ),
            mime_type="text/html;profile=mcp-app",
            meta={"ui": {"prefersBorder": False}},
            fn=_document,
        )
    )
    return True


def _register_snapshot_verb(mcp: Any) -> bool:
    """Register ``kiln_monitor_snapshot`` — the panel's own poll verb.

    Idempotent and never raises.  App-only (``visibility: ["app"]``): hosts
    hide it from the model.  Whether a locally rendered view can reach it AT
    ALL is the measured question this door is built around — see the module
    docstring — and registering it costs nothing when the answer stays no.
    """
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
        if isinstance(registry, dict) and "kiln_monitor_snapshot" in registry:
            return True

        @mcp.tool(
            name="kiln_monitor_snapshot",
            meta={"ui": {"resourceUri": PRINT_MONITOR_RESOURCE_URI,
                         "visibility": ["app"]}},
        )
        def kiln_monitor_snapshot(
            printer_name: str | None = None, include_camera: bool = False
        ) -> dict:
            """Internal support for Kiln's inline print monitor.

            Returns the composed printer snapshot the rendered panel polls.
            Called by the panel itself; not useful to call directly.
            """
            return {
                MONITOR_STRUCTURED_CONTENT_KEY: compose_local_payload(
                    printer_name=printer_name, include_camera=include_camera
                )
            }

        return True
    except Exception:
        logger.warning("local monitor: snapshot verb failed", exc_info=True)
        return False


def _stamp_tools(mcp: Any) -> int:
    """Point the monitor tools at the panel, and say so in words."""
    stamped = 0
    registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    for name, tool in registry.items():
        if name not in MONITOR_TOOLS:
            continue
        meta = dict(getattr(tool, "meta", None) or {})
        ui = dict(meta.get("ui") or {})
        ui["resourceUri"] = PRINT_MONITOR_RESOURCE_URI
        meta["ui"] = ui
        try:
            tool.meta = meta
            stamped += 1
        except Exception:  # noqa: BLE001 — a frozen model is not fatal
            continue
        desc = getattr(tool, "description", None) or ""
        if MONITOR_DESCRIPTION_CLAUSE in desc:
            continue  # second install — already said
        try:
            tool.description = (
                f"{desc}\n\n{MONITOR_DESCRIPTION_CLAUSE}"
                if desc
                else MONITOR_DESCRIPTION_CLAUSE
            )
        except Exception:  # noqa: BLE001 — the _meta stamp above still holds
            continue
    return stamped


def _install_result_hook(mcp: Any) -> bool:
    """Attach the monitor payload to monitor-tool results.

    Gated STRICTLY by roster name, the OPPOSITE of the stage's fail-open
    rule, because the tradeoff inverts: the stage's hook reads a result it
    already has, so attaching to an unknown tool costs bytes once — this
    hook performs a printer status read, and composing one for every tool
    call on the server would poll the machine as a side effect of
    unrelated work.  An unreadable request shape skips the attach and
    costs one stale panel paint; the next monitor call refreshes it.
    """

    def _attach(inner: Any, ctx: Any, name: str | None, args: dict | None) -> None:
        try:
            if name not in MONITOR_TOOLS:
                return
            if getattr(inner, "isError", False):
                return
            if not _host_renders_apps(mcp, ctx):
                _log_signal_once(attaching=False)
                return
            _log_signal_once(attaching=True)
            printer_name = None
            if isinstance(args, dict):
                pn = args.get("printer_name")
                if isinstance(pn, str) and pn:
                    printer_name = pn
            payload = compose_local_payload(
                printer_name=printer_name,
                include_camera=inline_camera_enabled(),
            )
            sc = getattr(inner, "structuredContent", None)
            sc = dict(sc) if isinstance(sc, dict) else {}
            sc[MONITOR_STRUCTURED_CONTENT_KEY] = payload
            inner.structuredContent = sc
        except Exception:  # noqa: BLE001 — a panel must never break a tool
            logger.debug("local monitor payload not attached", exc_info=True)

    return wrap_call_tool_result(mcp, _attach)


def _log_signal_once(attaching: bool) -> None:
    global _signal_logged
    if _signal_logged:
        return
    _signal_logged = True
    logger.info(
        "inline monitor: read_monitor=%s -> payload %s",
        _host_read_the_monitor,
        "attached" if attaching else "withheld (text report only)",
    )


def install(mcp: Any) -> dict[str, Any]:
    """Register the monitor resource and stamp the monitor tools.

    Returns a small summary for the log.  Never raises: a live panel that
    breaks the server is worse than no live panel.
    """
    out: dict[str, Any] = {"enabled": enabled(), "resource": False, "stamped": 0}
    if not enabled():
        return out

    try:
        out["resource"] = _register_resource(mcp)
    except Exception:
        logger.warning("local monitor: resource registration failed", exc_info=True)
        return out

    if diagnostics_enabled():
        out["snapshot_tool"] = _register_snapshot_verb(mcp)

    try:
        out["stamped"] = _stamp_tools(mcp)
    except Exception:
        logger.warning("local monitor: tool stamping failed", exc_info=True)

    try:
        out["hook"] = _install_result_hook(mcp)
    except Exception:
        logger.warning("local monitor: result hook failed", exc_info=True)

    logger.debug(
        "inline monitor ready: resource=%s stamped=%d hook=%s diagnostics=%s",
        out["resource"], out["stamped"], out.get("hook"), diagnostics_enabled(),
    )
    return out


def _reset_for_tests() -> None:
    global _host_read_the_monitor, _signal_logged
    _host_read_the_monitor = False
    _signal_logged = False
