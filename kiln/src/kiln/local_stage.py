"""EXPERIMENT (dark by default): serve the inline 3D stage from a LOCAL Kiln.

Set ``KILN_LOCAL_STAGE=1`` before starting the server to try it.  Unset —
which is every install — nothing here runs and nothing changes.

WHAT IS BEING TESTED
--------------------
Kiln's inline stage (the 3D panel that opens inside a conversation) is served
today only by the hosted connector.  Everything needed to serve it from a
local stdio server has been verified to work at the protocol level: the
``ui://`` resource registers on a local FastMCP and reads back with the
MCP-App mimetype, and ``_meta.ui.resourceUri`` survives ``tools/list``
without having to retype a single tool's return annotation.

The ONE thing local testing cannot answer is whether a HOST renders an MCP
App offered over stdio, or only over a remote connector.  That is host
behaviour.  This module exists to ask it, cheaply, before five pieces get
built on the assumption.

THE SHAPE, AND WHY
------------------
The payload does NOT ride the tool result.  Measured: putting it there costs
~1.9 MB of base64 in the conversation per call, which would wreck the context
window.  Instead the result carries a small opaque token, and the stage calls
``kiln_viewer_payload`` itself to fetch the geometry — the same lean handoff
the hosted connector already uses in production, so the payload costs the
conversation nothing.

The token is an opaque id, never a path: it round-trips through the host, and
absolute paths from a user's disk have no business in a conversation.

Depends on kiln-pro for the stage bundle and the payload encoder.  If the
test greenlights the idea, those move so a free local install gets it too;
building that move first would be building on the guess.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENABLE_ENV = "KILN_LOCAL_STAGE"

#: token -> mesh path.  Bounded; oldest dropped first.  In-memory only: a
#: stage token is meaningless across a restart because the conversation that
#: held it is gone too.
_tokens: dict[str, str] = {}
_TOKENS_MAX = 64
_lock = threading.Lock()

_MESH_SUFFIXES = frozenset({".stl", ".3mf", ".obj"})


def enabled() -> bool:
    return (os.environ.get(_ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _mint(mesh_path: str) -> str:
    token = secrets.token_urlsafe(18)
    with _lock:
        if len(_tokens) >= _TOKENS_MAX:
            _tokens.pop(next(iter(_tokens)), None)
        _tokens[token] = mesh_path
    return token


def resolve(token: str) -> str | None:
    with _lock:
        return _tokens.get(token)


def _mesh_from_result_json(text: str) -> str | None:
    """The mesh a serialised tool result names, if any."""
    import json

    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — prose content, not a result envelope
        return None
    if not isinstance(parsed, dict) or parsed.get("success") is False:
        return None
    from kiln.stage_link import find_mesh_path

    return find_mesh_path(parsed)


def token_for_call_result(result: Any) -> str | None:
    """Mint a stage token for a finished ``CallToolResult``, or ``None``.

    Reads the SERIALISED result rather than a dict, because by the time a
    tool call reaches the one place every tool passes through, FastMCP has
    already converted the return value into content blocks — measured, not
    assumed.  The token goes back as ``structuredContent``, which the stage
    reads first and which costs the conversation nothing; rewriting the
    serialised text to inject it would be how a wire format gets corrupted.

    Never raises.
    """
    if not enabled():
        return None
    try:
        if getattr(result, "isError", False):
            return None
        existing = getattr(result, "structuredContent", None)
        if isinstance(existing, dict):
            art = existing.get("artifact")
            if isinstance(art, dict) and art.get("artifact_token"):
                return None  # already carries the hosted shape
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            mesh = _mesh_from_result_json(text)
            if not mesh:
                continue
            path = Path(mesh)
            if path.suffix.lower() not in _MESH_SUFFIXES or not path.is_file():
                continue
            return _mint(mesh)
    except Exception as exc:  # noqa: BLE001 — a stage must never break a tool
        logger.debug("local stage token not minted: %s", exc)
    return None


def _write_test_cube() -> str | None:
    """A 20mm binary-STL cube in a temp file.  No dependencies on purpose —
    the diagnostic must not fail for a reason unrelated to what it tests."""
    import struct
    import tempfile

    s = 20.0
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
             (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
    try:
        blob = bytearray(b"\x00" * 80) + struct.pack("<I", len(faces))
        for a, b, c in faces:
            blob += struct.pack("<3f", 0.0, 0.0, 0.0)
            for idx in (a, b, c):
                blob += struct.pack("<3f", *v[idx])
            blob += struct.pack("<H", 0)
        fd, path = tempfile.mkstemp(suffix=".stl", prefix="kiln_stage_smoke_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(blob))
        return path
    except Exception:  # noqa: BLE001
        logger.debug("test cube not written", exc_info=True)
        return None


def install(mcp: Any) -> dict[str, Any]:
    """Register the stage resource + payload tool, and stamp mesh tools.

    Returns a small summary for the log.  Never raises: an experiment that
    breaks the server is not an experiment anybody can run.
    """
    out: dict[str, Any] = {"enabled": enabled(), "resource": False,
                           "payload_tool": False, "stamped": 0}
    if not enabled():
        return out
    try:
        from kiln_pro._rest.mcp_apps import (
            MESH_VIEWER_RESOURCE_URI,
            VIEWER_TOOLS,
            mesh_to_viewer_payload,
            register_mcp_apps,
        )
    except Exception:
        logger.warning(
            "%s is set but kiln-pro is not importable — the stage bundle and "
            "payload encoder live there for now, so the local stage stays off.",
            _ENABLE_ENV,
        )
        return out

    try:
        register_mcp_apps(mcp)
        out["resource"] = True
    except Exception:
        logger.warning("local stage: resource registration failed", exc_info=True)
        return out

    # The stage's own fetch.  Not useful to a human or an agent — the panel
    # calls it.
    try:
        @mcp.tool(name="kiln_viewer_payload")
        def kiln_viewer_payload(artifact_token: str) -> dict:
            """Internal support for Kiln's inline 3D viewer.

            Returns the viewer-grade mesh payload for a token the viewer was
            handed.  Called by the rendered panel itself; not useful to call
            directly.
            """
            mesh = resolve(artifact_token)
            if not mesh:
                return {"success": False, "error": "Unknown or expired viewer token."}
            try:
                payload = mesh_to_viewer_payload(mesh)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"Could not read that mesh: {exc}"}
            return {"kiln_viewer": payload}

        out["payload_tool"] = True
    except Exception:
        logger.warning("local stage: payload tool failed", exc_info=True)

    # An unmistakable name, so the experiment answers the question it asks.
    # A machine running this flag typically also has the plain local server
    # and the hosted connector attached, and all three offer the ordinary
    # mesh tools — so asking for one of those would be answered by whichever
    # server the host felt like, and a panel (or no panel) would prove
    # nothing about THIS one.  Only this server has this tool.
    try:
        @mcp.tool(name="stage_smoke_test",
                  meta={"ui": {"resourceUri": MESH_VIEWER_RESOURCE_URI}})
        def stage_smoke_test() -> dict:
            """Open a small test cube on Kiln's 3D stage.

            Diagnostic for the local inline-stage experiment: makes a 20mm
            cube and hands it back the same way a real design would, so the
            only question left is whether this app renders the panel.
            """
            mesh = _write_test_cube()
            if mesh is None:
                return {"success": False, "error": "Could not write the test cube."}
            return {
                "success": True,
                "stl_path": mesh,
                "message": (
                    "Made a 20mm test cube. If a 3D panel opened above this "
                    "message, the local inline stage works."
                ),
            }

        out["smoke_tool"] = True
    except Exception:
        logger.warning("local stage: smoke tool failed", exc_info=True)

    # Point the mesh-producing tools at the stage.  Mutating meta after
    # registration keeps this a pure add-on: no tool's signature, return
    # annotation, or body is touched, so nothing can regress when the flag
    # is off.
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
        for name, tool in registry.items():
            if name not in VIEWER_TOOLS:
                continue
            meta = dict(getattr(tool, "meta", None) or {})
            ui = dict(meta.get("ui") or {})
            ui["resourceUri"] = MESH_VIEWER_RESOURCE_URI
            meta["ui"] = ui
            try:
                tool.meta = meta
                out["stamped"] += 1
            except Exception:  # noqa: BLE001 — a frozen model is not fatal
                continue
    except Exception:
        logger.warning("local stage: tool stamping failed", exc_info=True)

    # The token has to be attached at the LOWLEVEL handler.  The tool-manager
    # hook that the telemetry counters use runs with convert_result=True, so
    # the value there is already a list of content blocks and a dict mutation
    # is silently lost — measured, after writing it the other way first.
    try:
        from mcp.types import CallToolRequest

        handlers = getattr(mcp._mcp_server, "request_handlers", None) or {}
        prev = handlers.get(CallToolRequest)
        if prev is not None and not getattr(prev, "_kiln_local_stage", False):

            async def _with_stage_token(req):
                resp = await prev(req)
                try:
                    inner = getattr(resp, "root", resp)
                    token = token_for_call_result(inner)
                    if token:
                        sc = dict(getattr(inner, "structuredContent", None) or {})
                        artifact = dict(sc.get("artifact") or {})
                        artifact["artifact_token"] = token
                        sc["artifact"] = artifact
                        inner.structuredContent = sc
                except Exception:  # noqa: BLE001
                    logger.debug("local stage token not attached", exc_info=True)
                return resp

            _with_stage_token._kiln_local_stage = True
            handlers[CallToolRequest] = _with_stage_token
            out["token_hook"] = True
    except Exception:
        logger.warning("local stage: token hook failed", exc_info=True)

    logger.info(
        "local inline stage ON (experiment): resource=%s payload_tool=%s "
        "stamped=%d token_hook=%s",
        out["resource"], out["payload_tool"], out["stamped"], out.get("token_hook"),
    )
    return out
