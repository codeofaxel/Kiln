"""Kiln's inline 3D stage, served by a locally installed Kiln.

WHAT THIS IS
------------
When a make finishes, the host can open a 3D panel right in the
conversation — drag to rotate, look underneath, check the back — instead of
handing over a flat PNG.  That panel is an MCP App (SEP-1865): a ``ui://``
HTML resource the host renders, pointed at by ``_meta.ui.resourceUri`` on
the tools that produce geometry.

It used to be reachable only through Kiln's hosted connection.  This module
serves it from a local ``kiln serve``, using nothing but public Kiln: the
stage document comes from :mod:`kiln.stage_cache`, the geometry from
:mod:`kiln.mesh_payload`.  A free install gets the same stage as a paid one.

WHY THE GEOMETRY RIDES THE RESULT
---------------------------------
There are two ways to get a mesh into a rendered panel.  The lean one is a
small token in the result plus a tool the panel calls back to fetch the
geometry — that is what the hosted connector does, and it costs the
conversation nothing.  Measured, on a local stdio server: the panel does
NOT get permission to call tools back through the host, so that path leaves
the stage sitting on its waiting animation forever with no way to say why.

So the payload rides the result.  That is not free — an 80k-triangle mesh
is ~1.9 MB of base64, and geometry nobody draws is fed straight into the
model's context.  Hence the TWO gates below, both of which must pass: the
host has actually shown it supports MCP Apps (else no panel ever opens),
and the called tool is stamped to open the stage (else the host draws
nothing for this result either — a slicer echoing the path it just sliced
must not pay for a panel it cannot have).  Everyone else gets the ordinary
result they get today, plus a resource and some ``_meta`` they will never
look at.

The still image is the floor under all of it, always.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from kiln.mcp_compat import client_capabilities, lowlevel_server, wrap_call_tool_result
from kiln.mesh_payload import VIEWER_STRUCTURED_CONTENT_KEY, mesh_to_viewer_payload

logger = logging.getLogger(__name__)

#: Opt out of the inline stage (matches ``KILN_NO_STAGE_LINKS`` next door).
#: The stage is ON by default: a flag that decides whether a user can turn
#: their own part over is a two-tier experience with no second tier.
_OPT_OUT_ENV = "KILN_NO_LOCAL_STAGE"

#: Registers the two support verbs — the panel's own fetch tool and a smoke
#: test.  Off by default: neither is useful to a person or an agent, and a
#: tool nobody should call does not belong on the standing tool surface.
_DIAGNOSTICS_ENV = "KILN_LOCAL_STAGE_DIAGNOSTICS"

#: Extension identifier from SEP-1865 — hosts negotiate MCP Apps under this.
MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"

#: The spec-mandated mimetype for MCP App HTML resources (exact string).
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"

#: The ui:// URI tool declarations point at via _meta.ui.resourceUri.
MESH_VIEWER_RESOURCE_URI = "ui://kiln/mesh-viewer"

#: Resource name shown in host resource listings.
MESH_VIEWER_RESOURCE_NAME = "kiln_mesh_viewer"

#: Appended to every stage tool's description at stamp time, so the stage is
#: discoverable where agents actually look — the tool listing that keyword
#: search runs over.  The stage machinery itself is invisible in schemas: it
#: rides ``_meta`` and the result hook, so before this clause no docstring
#: anywhere said the panel exists, and an agent that searched the tool
#: surface for "interactive 3D viewer" concluded — reasonably, wrongly —
#: that Kiln ends at a PNG, and shipped seven stills to a user who had asked
#: for the stage by name.  Derived from roster membership, never hand-typed
#: per tool: a hand-copy across ninety-odd docstrings is drift with a head
#: start.
STAGE_DESCRIPTION_CLAUSE = (
    "INLINE 3D STAGE: on success this tool also opens Kiln's interactive 3D "
    "stage — an inline viewer panel the user can orbit, zoom, and turn over "
    "— in hosts that render MCP Apps panels (Kiln's hosted connection "
    "attaches a browser stage link for hosts that don't). Oversized meshes "
    "are decimated automatically for the stage; the PNG preview is the "
    "floor, not the whole experience."
)

#: Tools whose success result reliably names a mesh the user just made or
#: changed, so opening a 3D panel on it is what they wanted.
#:
#: THE ONE LIST, READ BY BOTH DOORS.  A local ``kiln serve`` stamps from it
#: below; Kiln's hosted connector imports it rather than keeping a second
#: copy.  It used to be two hand-typed frozensets, one per repo — identical
#: the day they were written and with nothing to keep them that way, which is
#: how ``import_external_mesh`` (the door CAD files and marketplace downloads
#: arrive through) served a perfect viewer payload into a panel no host was
#: ever told to draw.
#:
#: Reviewed, not derived — but the reviewing is anchored, not remembered.
#: Every tool wired to the preview chokepoint belongs here unless the
#: downstream stage-coverage ledger records a reason otherwise, and a new
#: mesh-returning tool on neither list fails that coverage gate at
#: conception.  The reasons a tool sits OUT: its output is a print or
#: gcode artifact rather than a design mesh; it is a bookkeeping act on
#: geometry the user has already seen (branch/save/sign ceremonies keep
#: their PNG receipt, not a panel); it is an N-result batch; or the value it
#: changes does not survive into the stage payload (a colored result shown
#: gray reads as failure — the color tools sat out on exactly that until
#: the encoder learned to bake per-part 3MF colors into vertex colors).
VIEWER_TOOLS: frozenset[str] = frozenset(
    {
        "add_feature_during_print",
        "add_mesh_chamfer",
        "add_mesh_fillet",
        "add_pin_joints",
        "add_qr_to_product",
        "apply_decoration",
        "apply_decoration_preset",
        "apply_design_reinforcements",
        "apply_geometric_texture",
        "apply_image_texture",
        "apply_mid_print_decoration_plan",
        "apply_procedural_texture",
        "attach_part_feature",
        "auto_add_rubber_feet",
        "auto_color_by_height",
        "auto_color_by_region",
        "auto_multicolor_from_texture",
        "boolean_mesh_op",
        "build_organic_mesh",
        "center_model_on_bed",
        "change_part_color",
        "cherry_pick_decoration_modification",
        "cherry_pick_feature_modification",
        "cherry_pick_modification",
        "compile_scad",
        "compose_assembly_parts",
        "compose_models",
        "compose_multicolor_3mf",
        "compose_part_from_primitives",
        "decorate_during_print",
        "decorate_surface",
        "design_session",
        "download_generated_model",
        "extract_model_from_3mf",
        "generate_ashtray",
        "generate_bookmark",
        "generate_coaster",
        "generate_decorated_product",
        "generate_fridge_magnet",
        "generate_frisbee",
        "generate_from_template",
        "generate_jewelry_tray",
        "generate_keychain",
        "generate_license_plate_frame",
        "generate_model_with_provider",
        "generate_nameplate",
        "generate_ornament",
        "generate_pen_cup",
        "generate_pet_bowl",
        "generate_pet_tag",
        "generate_product_base",
        "generate_qr_decoration",
        "generate_rolling_tray",
        "generate_soap_dish",
        "generate_wall_plaque",
        "hollow_mesh_model",
        "import_external_feature",
        "import_external_mesh",
        "import_model_parts",
        "import_step_file",
        "iterate_design",
        "keep_painted_detail",
        "make_printable",
        "merge_decoration_preset_branches",
        "merge_design_branches",
        "merge_feature_branches",
        "merge_mesh_files",
        "merge_stl",
        "mirror_mesh_model",
        "optimize_print_orientation",
        "paint_mesh_regions",
        "optimize_template_params",
        "plan_mid_print_decoration",
        "prepare_ai_model_for_print",
        "preview_decorated_mesh",
        "preview_mid_print_session",
        "rebase_design_branch",
        "rebase_feature_branch",
        "rebase_preset_branch",
        "rebuild_design",
        "recover_texture_detail",
        "remove_mesh_floating_regions",
        "repair_mesh",
        "repair_mesh_advanced",
        "rescale_model",
        "rollback_design_version",
        "rollback_feature",
        "rotate_model",
        "scale_mesh_to_fit",
        "simplify_mesh_model",
        "smart_decorate",
        "smart_generate_from_template",
        "splice_mesh_at_z",
        "split_mesh_by_component",
        "split_mesh_to_fit",
        "thicken_mesh_walls",
        "tweak_and_compile_scad",
    }
)

#: token -> mesh path.  Bounded; oldest dropped first.  In-memory only: a
#: stage token is meaningless across a restart because the conversation that
#: held it is gone too.
_tokens: dict[str, str] = {}
_TOKENS_MAX = 64
_lock = threading.Lock()

_MESH_SUFFIXES = frozenset({".stl", ".3mf", ".obj"})

#: The encoder's budget for a payload that rides a conversation.  Lower than
#: the encoder's own 8 MB default because this one is not a download — it is
#: bytes inside a tool result, and a mesh past this gets the honest "too big"
#: card plus the still image instead.
_MAX_INLINE_PAYLOAD_BYTES = 6 * 1024 * 1024

#: Set once a host reads the stage document — proof, not a guess, that this
#: host renders MCP Apps.  Process-wide because a stdio server serves one
#: host; the declared capability below is what covers the first call.
_host_read_the_stage = False

#: One log line per process, so the first real run answers "did this host
#: take the geometry?" without anyone having to instrument it.
_signal_logged = False


def enabled() -> bool:
    """Whether the inline stage runs at all on this install."""
    return (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def inline_geometry_enabled() -> bool:
    """Whether geometry rides the RESULT, or only the token does.

    Default ON — the lean alternative (token-only result; the panel
    lazy-fetches through the host's ``tools/call`` proxy) depends on the
    host declaring ``serverTools``, which the server cannot see at attach
    time.  Measured 2026-08-19 on the host most installs run: the inline
    payload (~1.9 MB base64 in structuredContent) is ALSO surfaced to the
    agent as tool-result text, truncating at the client's output cap and
    spending ~25k tokens of the agent's context per stamped make on
    geometry the agent cannot read.  ``KILN_STAGE_INLINE_GEOMETRY=0`` is
    the experiment lever: flip it, and if the panel's lazy fetch works on
    that host, the lean result should become the default there.
    """
    return (os.environ.get("KILN_STAGE_INLINE_GEOMETRY") or "").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def diagnostics_enabled() -> bool:
    return (os.environ.get(_DIAGNOSTICS_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


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


# ---------------------------------------------------------------------------
# Does this host render MCP Apps?
# ---------------------------------------------------------------------------


def _declared_extensions(mcp: Any, ctx: Any = None) -> dict[str, Any]:
    """What the connected host declared it supports, as a flat dict.

    Reads both keys the extension mechanism has been spelled with —
    ``capabilities.extensions`` (SEP-1865) and ``capabilities.experimental``
    (where SDKs park unrecognised extensions).  Never raises: no session,
    an exotic SDK, or a host that declared nothing all read as "nothing".

    ``ctx`` is the request context a handler was invoked with; on SDK 2 it is
    the only place the session lives, so callers inside the result hook must
    forward it or a 2.x host always reads as "declared nothing".
    """
    out: dict[str, Any] = {}
    caps = client_capabilities(mcp, ctx)
    if caps is None:
        return out
    # ``extensions`` is not a modelled field on every SDK, so it arrives as
    # an extra rather than an attribute — check both places it can land.
    for block in (
        getattr(caps, "extensions", None),
        (getattr(caps, "model_extra", None) or {}).get("extensions"),
        getattr(caps, "experimental", None),
    ):
        if isinstance(block, dict):
            out.update(block)
    return out


def host_renders_apps(mcp: Any, ctx: Any = None) -> bool:
    """Whether it is safe — and useful — to put geometry in the result.

    Two positive signals, either one sufficient:

    * the host **declared** the MCP Apps extension at initialize, or
    * the host has **read the stage document** this session, which no host
      does unless it is about to render the panel.

    Absent both, the result stays lean.  That is the honest default: a host
    that does not render the panel would be handed ~1.9 MB of base64 per
    make and nothing to show for it.  A host that renders but declares
    nothing pays for it once — its first make is a still image, and the
    resource read it performs to draw that first panel turns the stage on
    for the rest of the session.

    The declaration is the path that matters in practice.  Claude Desktop
    sends it at initialize, under ``capabilities.extensions``, naming the
    same mimetype this module serves — captured from a real handshake, not
    assumed, and pinned in the tests.  So the first make of a session opens
    the panel; the resource-read signal is the safety net for a host that
    renders without saying so.
    """
    if _host_read_the_stage:
        return True
    return MCP_APPS_EXTENSION_ID in _declared_extensions(mcp, ctx)


def _log_signal_once(mcp: Any, attaching: bool, ctx: Any = None) -> None:
    """State, once, what this host declared and what we did about it."""
    global _signal_logged
    if _signal_logged:
        return
    _signal_logged = True
    try:
        session = getattr(ctx, "session", None)
        if session is None:
            session = lowlevel_server(mcp).request_context.session
        info = session.client_params.clientInfo
        who = f"{getattr(info, 'name', '?')}/{getattr(info, 'version', '?')}"
    except Exception:  # noqa: BLE001
        who = "unknown host"
    logger.info(
        "inline stage: host=%s declared=%s read_stage=%s -> geometry %s",
        who,
        sorted(_declared_extensions(mcp, ctx)) or "none",
        _host_read_the_stage,
        "attached" if attaching else "withheld (still image only)",
    )


# ---------------------------------------------------------------------------
# Reading a mesh back out of a finished tool call
# ---------------------------------------------------------------------------


def _mesh_from_result_json(text: str) -> str | None:
    """The mesh a serialised tool result names, if any."""
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — prose content, not a result envelope
        return None
    if not isinstance(parsed, dict) or parsed.get("success") is False:
        return None
    from kiln.stage_link import find_mesh_path

    return find_mesh_path(parsed)


def _result_as_dict(result: Any) -> dict | None:
    """The tool's own return value, parsed back out of its content blocks."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def token_for_call_result(result: Any) -> str | None:
    """Mint a stage token for a finished ``CallToolResult``, or ``None``.

    Reads the SERIALISED result rather than a dict, because by the time a
    tool call reaches the one place every tool passes through, FastMCP has
    already converted the return value into content blocks — measured, not
    assumed.

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


def _payload_for_mesh(mesh: str, **encode: Any) -> dict:
    """Encode *mesh* and stamp this install's print bed onto it.

    The one place a payload is built for the local stage.  Both doors below
    go through it so neither can ship geometry with no bed under it — the
    stage draws the plate from what arrives here, and a payload that names no
    plate falls back to a reference square for a bed it knows nothing about.

    Raises whatever the encoder raises; each door decides what to say about
    it, since one of them is answering a person and the other is not.
    """
    from kiln.stage_plate import attach_stage_plate

    return attach_stage_plate(mesh_to_viewer_payload(mesh, **encode))


def _inline_payload(token: str) -> dict | None:
    """The viewer payload for a minted token, encoded to the inline budget.

    The budget is handed to the encoder rather than checked afterwards, so a
    mesh too big to ride the wire comes back as the honest "too big" card the
    panel can show — not as a silent ``None`` that leaves the stage waiting
    on geometry nobody is going to send.
    """
    mesh = resolve(token)
    if not mesh:
        return None
    try:
        return _payload_for_mesh(mesh, max_bytes=_MAX_INLINE_PAYLOAD_BYTES)
    except Exception:  # noqa: BLE001 — no payload is not a failed tool call
        logger.debug("inline payload unavailable", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


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


def _register_resource(mcp: Any) -> bool:
    """Register ``ui://kiln/mesh-viewer``, served from the on-disk cache.

    The document is read lazily, at ``resources/read`` — so a server that
    started before the cache was warm still serves the stage the moment the
    download lands, and one that never got a document raises there rather
    than at boot.
    """
    from kiln import stage_cache
    from kiln.mcp_compat import FunctionResource

    def _document() -> str:
        global _host_read_the_stage
        doc = stage_cache.document()
        if not doc:
            # Nothing cached and nothing to invent.  The host reports the
            # resource unavailable and the still image carries the result.
            raise ValueError(
                "Kiln's 3D stage has not been downloaded on this machine yet."
            )
        # Only a host about to render the panel asks for this.
        _host_read_the_stage = True
        # Door parity: every stamped declaration promises the rendered View
        # a working fetch verb on THIS door (the View lazy-fetches when a
        # result carries a token but no inline geometry).  The read is the
        # earliest proof a View will exist, and it precedes the View's first
        # tools/call — so registering here keeps the verb off the standing
        # tool surface for hosts that never render panels, while a host
        # that does render can never call into a missing verb.
        _register_payload_verb(mcp)
        return doc

    mcp.add_resource(
        FunctionResource(
            # A plain str on purpose: SDK 1.x declares this ``AnyUrl`` and
            # coerces the string for us, while 2.x declares it ``str`` and
            # REJECTS an AnyUrl.  The string is the one input both accept.
            uri=MESH_VIEWER_RESOURCE_URI,
            name=MESH_VIEWER_RESOURCE_NAME,
            title="Kiln Mesh Viewer",
            description=(
                "Interactive inline 3D stage for Kiln mesh results — orbit, "
                "zoom, and turntable on Kiln's dark stage."
            ),
            mime_type=MCP_APP_MIME_TYPE,
            meta={"ui": {"prefersBorder": False}},
            fn=_document,
        )
    )
    return True


def _register_payload_verb(mcp: Any) -> bool:
    """Register ``kiln_viewer_payload`` — the View's lazy mesh fetch.

    Idempotent and never raises.  Called from two places: the stage-document
    read (door parity — a host that renders the panel gets the verb before
    its View's first ``tools/call``; see ``_document``) and the diagnostics
    path (which forces it at install for smoke-testing).  Serves the
    operator's own local files at full fidelity — the hosted door's
    charge-on-keep wall guards artifact tokens, which never exist here;
    a local token resolves only to a mesh this machine already made.
    """
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
        if isinstance(registry, dict) and "kiln_viewer_payload" in registry:
            return True

        @mcp.tool(
            name="kiln_viewer_payload",
            meta={"ui": {"resourceUri": MESH_VIEWER_RESOURCE_URI,
                         "visibility": ["app"]}},
        )
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
                payload = _payload_for_mesh(mesh)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"Could not read that mesh: {exc}"}
            return {VIEWER_STRUCTURED_CONTENT_KEY: payload}

        return True
    except Exception:
        logger.warning("local stage: payload tool failed", exc_info=True)
        return False


def _register_diagnostics(mcp: Any, out: dict[str, Any]) -> None:
    """The smoke test, plus the fetch verb forced at install.  Off by default."""
    out["payload_tool"] = _register_payload_verb(mcp)

    try:
        @mcp.tool(name="stage_smoke_test",
                  meta={"ui": {"resourceUri": MESH_VIEWER_RESOURCE_URI}})
        def stage_smoke_test() -> dict:
            """Open a small test cube on Kiln's 3D stage.

            Diagnostic: makes a 20mm cube and hands it back the same way a
            real design would, so the only question left is whether this app
            renders the panel.
            """
            mesh = _write_test_cube()
            if mesh is None:
                return {"success": False, "error": "Could not write the test cube."}
            return {
                "success": True,
                "stl_path": mesh,
                "message": (
                    "Made a 20mm test cube. If a 3D panel opened above this "
                    "message, the inline stage works."
                ),
            }

        out["smoke_tool"] = True
    except Exception:
        logger.warning("local stage: smoke tool failed", exc_info=True)


def _stamp_tools(mcp: Any) -> int:
    """Point the mesh-producing tools at the stage, and say so in words.

    Mutating meta after registration keeps this a pure add-on: no tool's
    signature, return annotation, or body is touched.  The description
    clause rides the same pass: the ``_meta`` stamp is what a HOST reads,
    but an AGENT deciding which tool to call reads descriptions — and a
    capability that lives only in ``_meta`` is one no keyword search over
    the tool surface can ever find.
    """
    stamped = 0
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
            stamped += 1
        except Exception:  # noqa: BLE001 — a frozen model is not fatal
            continue
        desc = getattr(tool, "description", None) or ""
        if STAGE_DESCRIPTION_CLAUSE in desc:
            continue  # second install — already said
        try:
            tool.description = (
                f"{desc}\n\n{STAGE_DESCRIPTION_CLAUSE}"
                if desc
                else STAGE_DESCRIPTION_CLAUSE
            )
        except Exception:  # noqa: BLE001 — the _meta stamp above still holds
            continue
    return stamped


def _tool_opens_stage(mcp: Any, name: str | None) -> bool:
    """Whether the named tool's declaration points at the stage.

    The stamp on the registered tool object is the single decision — the
    roster stamps the mesh tools, the diagnostics verbs stamp themselves at
    registration — so nothing here keeps a second list.  A host only opens
    the panel for a stamped tool, which means geometry attached to an
    UNSTAMPED tool's result is dead weight: ``slice_model`` echoing the path
    it just sliced was shipping megabytes of base64 no panel would ever draw.

    Every unreadable shape fails OPEN.  Withholding geometry from a rendered
    panel starves it for the whole call — the panel cannot call tools back
    on a local stdio server — while attaching to a tool nobody panels costs
    bytes once.  Only a tool this can SEE is unstamped is withheld.
    """
    if not name:
        return True
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
        if not isinstance(registry, dict) or name not in registry:
            return True
        meta = getattr(registry[name], "meta", None) or {}
        ui = meta.get("ui") or {}
        return ui.get("resourceUri") == MESH_VIEWER_RESOURCE_URI
    except Exception:  # noqa: BLE001
        return True


def _install_result_hook(mcp: Any) -> bool:
    """Attach the token (and, for a panel that will open, the geometry).

    This has to happen at the LOWLEVEL handler.  The tool-manager hook that
    the telemetry counters use runs with ``convert_result=True``, so the
    value there is already a list of content blocks and a dict mutation is
    silently lost — measured, after writing it the other way first.

    The geometry rides only when BOTH gates pass: the host renders MCP Apps
    (else nobody draws it), and the tool is stamped to open the stage (else
    the host draws nothing for this result either).  The token always rides.
    """
    def _attach(inner: Any, ctx: Any, name: str | None) -> None:
        """Mutate one tool result in place.  Deliberately knows no SDK detail —
        ``wrap_call_tool_result`` owns every difference between majors, and
        this stays the description of WHAT to attach.  ``ctx`` is the request
        context of THIS call (None on 1.x), forwarded so the capability read
        can see the session on SDK 2; ``name`` is the called tool when the
        request shape yields one, else None (which reads as "attach")."""
        try:
            token = token_for_call_result(inner)
            if not token:
                return
            sc = getattr(inner, "structuredContent", None)
            if not isinstance(sc, dict):
                # The tool had none.  Seed it from the result the tool
                # actually returned, because a host that prefers
                # structuredContent will show THIS and nothing else —
                # seeding it with only the token would hide the tool's
                # own output from the agent (measured: success, paths
                # and message all vanished from the visible result).
                sc = _result_as_dict(inner) or {}
            else:
                sc = dict(sc)
            artifact = dict(sc.get("artifact") or {})
            artifact["artifact_token"] = token
            sc["artifact"] = artifact
            attaching = host_renders_apps(mcp, ctx)
            _log_signal_once(mcp, attaching, ctx)
            if attaching and _tool_opens_stage(mcp, name) and inline_geometry_enabled():
                payload = _inline_payload(token)
                if payload is not None:
                    # A STEP import's analytic truth rides the payload so
                    # the stage labels the model as CAD over its display
                    # tessellation — or says the facts are unavailable,
                    # which is still the truth.
                    facts = sc.get("cad_facts")
                    if isinstance(facts, dict):
                        from kiln.mesh_payload import attach_cad_facts

                        attach_cad_facts(payload, facts)
                    sc[VIEWER_STRUCTURED_CONTENT_KEY] = payload
            inner.structuredContent = sc
        except Exception:  # noqa: BLE001
            logger.debug("local stage token not attached", exc_info=True)

    return wrap_call_tool_result(mcp, _attach)


def install(mcp: Any) -> dict[str, Any]:
    """Register the stage resource and stamp the mesh tools.

    Returns a small summary for the log.  Never raises: a 3D panel that
    breaks the server is worse than no 3D panel.
    """
    out: dict[str, Any] = {"enabled": enabled(), "resource": False,
                           "payload_tool": False, "stamped": 0}
    if not enabled():
        return out

    try:
        out["resource"] = _register_resource(mcp)
    except Exception:
        # FastMCP warns and keeps the first registration on a duplicate, so
        # a second install is not the failure this catches — an exotic
        # server object or an SDK without the resource API is.
        logger.warning("local stage: resource registration failed", exc_info=True)
        return out

    if diagnostics_enabled():
        _register_diagnostics(mcp, out)

    try:
        out["stamped"] = _stamp_tools(mcp)
    except Exception:
        logger.warning("local stage: tool stamping failed", exc_info=True)

    try:
        out["token_hook"] = _install_result_hook(mcp)
    except Exception:
        logger.warning("local stage: result hook failed", exc_info=True)

    logger.debug(
        "inline stage ready: resource=%s stamped=%d hook=%s diagnostics=%s",
        out["resource"], out["stamped"], out.get("token_hook"),
        diagnostics_enabled(),
    )
    return out


def _reset_for_tests() -> None:
    global _host_read_the_stage, _signal_logged
    _tokens.clear()
    _host_read_the_stage = False
    _signal_logged = False
