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

WHY THE GEOMETRY DOES NOT RIDE THE RESULT
-----------------------------------------
There are two ways to get a mesh into a rendered panel.  The lean one is a
small token in the result plus a tool the panel calls back to fetch the
geometry — that is what the hosted connector has always done, and it costs
the conversation nothing.  The other inlines the whole mesh as base64 in
``structuredContent``.

Inlining was the default until 2026-08-30, on the reasoning that a panel
on a local stdio server could not call tools back.  Two facts retired it:

* **The hosts that render this panel also feed ``structuredContent`` to the
  model as text.**  So a stamped make spent ~25k tokens on geometry no
  model can read — and, the part that actually breaks the make, TRUNCATED
  the tool's own result at the client's output cap, so ``mesh_path``, the
  fit verdict and the self-check bundle never reached the agent that had to
  act on them.  Paying the entire result to draw a panel is not a trade any
  user would choose.  (Measured 2026-08-19; hit again live 2026-08-30 on
  ``build_organic_mesh``, whose result truncated mid-payload.)
* **The lazy fetch is not theoretical.**  ``kiln_viewer_payload`` is a
  standing tool on this door from install, and serving geometry through it
  — never in the result — is the only way the hosted door has ever worked.

So the token rides the result and the geometry does not.  The View fetches
through the host's ``tools/call`` proxy where the host offers one, and
shows an honest card where it does not; the conversation's PNG carries that
case, as it always did.  The gates below still decide whether the *token*
buys a panel: the host has shown it supports MCP Apps, and the called tool
is stamped to open the stage (a slicer echoing the path it just sliced must
not pay for a panel it cannot have).

``KILN_STAGE_INLINE_GEOMETRY=1`` restores the inline payload, for a host
that renders panels, cannot proxy tools back, and whose operator has
decided the context is worth it.  It is an opt-in to a measured cost, not a
tuning knob.

The hosted door already carries this rule in its own words, about its own
camera frame: a base64 blob in ``structuredContent`` is a token bomb that
says nothing.  This is the same rule, on the other door.

WHAT LEAN COSTS, SAID OUT LOUD
------------------------------
Geometry that rode the result lived in the transcript forever; a token
resolves only while this process holds it.  So a panel re-rendered from
scrollback after ``_TOKENS_MAX`` further makes — or after any restart —
fetches a dead token and shows its "preview unavailable" card over the
PNG that is still sitting in the conversation.  That is the trade, and it
is the right one: the alternative was truncating every make's result so a
scrolled-back panel could redraw.  The hosted door has always paid it
(its artifact tokens expire), and the ceiling is set high enough below
that a live session never meets it.

The still image is the floor under all of it, always.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from kiln.mcp_compat import (
    capture_request_context,
    client_capabilities,
    lowlevel_server,
    wrap_call_tool_result,
)
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
    "INLINE 3D STAGE: on success this tool opens Kiln's interactive 3D "
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
#: conception.  The reasons a tool sits OUT: it is a bookkeeping act on
#: geometry the user has already seen (branch/save/sign ceremonies keep
#: their PNG receipt, not a panel); it is an N-result batch or a fleet
#: door (N stages is spam, and those doors are the paid half); or the
#: value it changes does not survive into the stage payload (a colored
#: result shown gray reads as failure — the color tools sat out on exactly
#: that until the encoder learned to bake per-part 3MF colors into vertex
#: colors).
#:
#: THE SLICE DOORS ARE IN.  They sat out under an inline-era rule — "its
#: output is a print or gcode artifact rather than a design mesh" — whose
#: real reason was cost: ``slice_model`` echoing the path it just sliced
#: shipped megabytes of base64 no panel would draw.  Only a token rides a
#: result now, so that cost is gone, and the exclusion outlived it.  What
#: a slice decides is exactly what no design-mesh stage ever showed: the
#: bed position (the fit gate may have moved the part), which filament
#: each colour maps to and whether the paint survived the slicer, and the
#: slicer's own additions — supports, brim, a prime tower.  Measured
#: 2026-09-21 on a re-sliced three-colour jar: a 30 mm prime tower and a
#: centring, decided and shown to nobody.  A slice door's result names the
#: mesh the slicer was handed (``stage_mesh_path``; see
#: :func:`kiln.stage_link.find_mesh_path`), and the stage dresses it in
#: the slice's own skirt and tower from this machine's ledger.  The start
#: token stays the only wall: nothing here gates ``upload_file``.
#:
#: ``show_on_stage`` is the one door on this list that makes nothing: it
#: opens the stage on a file that already exists, a sliced print file
#: included.  Until 2026-09-22 no door did, so the print gate — which asks
#: for the stage first — could only be answered for an existing file by
#: re-making or re-slicing it; an agent told "inline stage first, do not
#: re-slice" reached the link door and nothing better.  ``visualize_model``
#: stays off: it is the still door, called after every make round, and a
#: second panel of the mesh the make just opened is ceremony.
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
        "design_to_gcode_pipeline",
        "download_generated_model",
        "extract_model_from_3mf",
        "generate_and_print",
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
        "reslice_with_overrides",
        "rollback_design_version",
        "rollback_feature",
        "rotate_model",
        "run_reslice_and_print",
        "scale_mesh_to_fit",
        "separate_overlapping_parts",
        "show_on_stage",
        "simplify_mesh_model",
        "slice_and_estimate",
        "slice_and_print",
        "slice_model",
        "smart_decorate",
        "smart_generate_from_template",
        "splice_mesh_at_z",
        "split_mesh_by_component",
        "split_mesh_to_fit",
        "thicken_mesh_walls",
        "tweak_and_compile_scad",
    }
)

#: token -> mesh path.  Bounded; oldest dropped first.  The fast path for
#: the process that minted; the shared ledger under ``~/.kiln`` is the
#: truth every OTHER Kiln server on this machine answers from, because a
#: desktop host routes a panel's fetch over whichever session's connection
#: it holds, not necessarily the minting one (see _ledger_write).  The
#: ledger also survives restarts, so a scrolled-back panel redraws as long
#: as its temp file still exists; a deleted mesh gets the honest "could
#: not read" card, which the verb already says.  See WHAT LEAN COSTS.
_tokens: dict[str, str] = {}
#: How many makes back a panel can still fetch its mesh.  Raised from 64
#: when the result went lean: an evicted token used to cost nothing (the
#: geometry had already ridden the result), and now it is the whole route
#: to the mesh, so the ceiling is what decides whether a panel re-rendered
#: from scrollback still draws.  512 entries measure 107 KB against 13 KB
#: at 64 — a rounding error next to one 1.9 MB payload this change stopped
#: sending, and about a full day of makes rather than an hour.
_TOKENS_MAX = 512
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

#: Seconds a minted token is given for the panel's fetch to arrive before
#: the mint counts as unfetched.  Measured hosts fetch the moment the panel
#: renders — well under a second — so a few seconds is generous.  Never
#: slept through: the clock is read on the NEXT result, so no tool call
#: ever waits on a panel (see panel_fetches_stalled).
_FETCH_GRACE_S = 3.0

#: The stage's own monotonic clock — a name, so a test can move it.
_now = time.monotonic

#: token -> (mesh path, minted-at monotonic, minted-at wall clock), for
#: mints a rendered panel was expected to fetch and has not yet.  An entry
#: leaves on its fetch, or when it crosses the grace and is judged.
_awaiting_fetch: dict[str, tuple[str, float, float]] = {}

#: What the last stage result drew (:func:`kiln.stage_link.stage_identity`),
#: so the next one can say when it draws the very same thing.  The HOST
#: opens a panel for every call to a stamped tool — the server cannot keep
#: a second panel from appearing, and a result that carried no token would
#: open that panel on nothing — so the one decision the server owns is to
#: say, in the result, that the panel above already shows this.  Measured
#: 2026-09-23: ``slice_model`` drew a sliced plate and ``show_on_stage`` on
#: the same ``.gcode.3mf`` drew it again, identically, with nothing saying
#: so.  Only an exact repeat is called one: a changed file, a new pose or
#: EXTRAS that arrived since all change the tag.
_last_staged: str = ""

#: The sentence a repeat carries, ahead of the door's own reason.
REPEAT_NOTE = (
    "Same file as the stage result just before this one — byte for byte, "
    "with the same slice — so this panel shows nothing new; the stage was "
    "already showing it, and the call above could have been skipped."
)

#: Set once a mint crossed the grace with no fetch on record anywhere.
#: Sticky until a fetch lands.  Read by the result hook and by
#: ``visualize_model``; it never blocks anything.
_fetches_stalled = False

#: Set once a rendered panel has fetched geometry — from this process, or
#: from a sibling server this process learned of through the shared record.
#: Until then the panel is DECLARED, not proven: a host holding a tool list
#: it cached before a restart_server declares the panel and cannot draw it,
#: and the first make after such a restart used to ride alone — the link
#: arrived only on the NEXT result, once the grace had judged the first
#: mint unfetched (2026-09-21: a re-sliced jar, shown to nobody).  So the
#: browser link rides beside the token until a fetch lands, and the result
#: goes lean again after.  A host that draws no panel never proves one, so
#: for it the link is simply the stage.  Never sticky across a restart: the
#: fresh process starts unproven, and restart_server forgets it before the
#: exec so a restart that fails to exec is not a restart that lied.
_panel_proven = False

#: What every door says while the panel is not fetching — the make results
#: and ``visualize_model`` carry this same sentence, so an agent reading
#: either learns the browser link IS the stage today, and why.
PANEL_FETCH_FALLBACK_NOTE = (
    "Kiln's inline 3D panel is not fetching geometry on this connection (the "
    "host may be holding a tool list it cached before a restart_server), so "
    "the viewer_url IS the 3D stage for now: hand it to the user. Reconnecting "
    "the Kiln MCP server in the host, or opening a new chat, brings the panel "
    "back."
)


def enabled() -> bool:
    """Whether the inline stage runs at all on this install."""
    return (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def inline_geometry_enabled() -> bool:
    """Whether geometry rides the RESULT, or only the token does.

    Default OFF — the reasoning is in the module docstring, and it is not a
    preference: a host that renders the panel also hands
    ``structuredContent`` to the model, so inlining costs ~25k tokens per
    make AND truncates the tool's own output at the client's cap, which is
    the half that breaks the make.  The panel fetches the geometry itself
    through ``kiln_viewer_payload``, exactly as the hosted door has always
    served it.

    ``KILN_STAGE_INLINE_GEOMETRY=1`` opts a host back in — it renders
    panels, it cannot proxy ``tools/call`` back to this server, and its
    operator would rather spend the context than lose the panel.  Anything
    else reads as off, the bare-value spellings included, so the old
    ``=0`` that used to mean "lean" still means lean.
    """
    return (os.environ.get("KILN_STAGE_INLINE_GEOMETRY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def diagnostics_enabled() -> bool:
    return (os.environ.get(_DIAGNOSTICS_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _token_ledger_path() -> Path:
    """The machine-wide token ledger, next to the other ``~/.kiln`` stores."""
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    return home / "stage_tokens.json"


def _ledger_write(token: str, mesh_path: str) -> None:
    """Record a token in the shared ledger, best-effort and atomic.

    WHY A FILE.  The in-memory dict assumes the panel's fetch comes back to
    the PROCESS that minted the token.  It does not: a desktop host runs one
    Kiln server per open session and routes a rendered panel's ``tools/call``
    over whichever of those connections it holds — measured live 2026-09-01,
    twelve fetches in one evening, every one answered "unknown token" by a
    server that never minted it while the minting server sat idle.  The
    ledger makes the token machine-wide: any Kiln server on this machine can
    resolve it to the local mesh path, which is the same trust domain the
    mesh itself lives in.  Bounded like the dict, oldest first; 0600 because
    a token is a capability, even a local one.

    Never raises — a mesh nobody can fetch later must not fail the make now.
    """
    try:
        import tempfile

        path = _token_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            entries = json.loads(path.read_text())
            if not isinstance(entries, dict):
                entries = {}
        except (OSError, ValueError):
            entries = {}
        entries[token] = mesh_path
        while len(entries) > _TOKENS_MAX:
            entries.pop(next(iter(entries)), None)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".stage_tokens_")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(entries, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:  # noqa: BLE001
        logger.debug("stage token ledger write skipped", exc_info=True)


def _ledger_read(token: str) -> str | None:
    """The ledger's answer for a token minted by any process, or ``None``."""
    try:
        entries = json.loads(_token_ledger_path().read_text())
        value = entries.get(token) if isinstance(entries, dict) else None
        return value if isinstance(value, str) and value else None
    except Exception:  # noqa: BLE001
        return None


def _record(token: str, mesh_path: str) -> str:
    """Bind *token* to *mesh_path* in memory AND in the shared ledger.

    The recording half of :func:`_mint`, split out because a token minted
    ELSEWHERE sometimes has to resolve here too (see ``_adopt``).  One path
    rather than two: the bound of the fast-path dict and the ledger write
    have to stay in step, and two copies of that pairing would not.
    """
    with _lock:
        # Only evict when this is a NEW token.  Re-binding an existing one is
        # not growth, and treating it as growth would drop an unrelated live
        # token every time a result was re-staged.
        if len(_tokens) >= _TOKENS_MAX and token not in _tokens:
            _tokens.pop(next(iter(_tokens)), None)
        _tokens[token] = mesh_path
    _ledger_write(token, mesh_path)
    return token


def _mint(mesh_path: str) -> str:
    return _record(secrets.token_urlsafe(18), mesh_path)


def _adopt(token: str, mesh_path: str) -> str:
    """Let a token minted elsewhere resolve to a local mesh on this door.

    A kiln-pro tool that stores its result in the cloud returns a HOSTED
    artifact token.  The viewer presents whatever token the payload carries
    to this door's payload verb, which knows only tokens this machine
    minted, so the fetch was refused and the stage came up grey (measured
    2026-09-01, on a paint that minted a cloud artifact).

    The mesh is the operator's own local file either way, so the honest fix
    is to make the hosted token resolve to it rather than to rewrite the
    field ``keep_design`` and ``viewer_url`` read.  Recorded through the same
    ledger as a minted token, so a sibling Kiln server answers it too.
    """
    return _record(token, mesh_path)


def resolve(token: str) -> str | None:
    with _lock:
        hit = _tokens.get(token)
    # The minting process answers from memory; every OTHER Kiln server on
    # this machine answers from the shared ledger.  See _ledger_write.
    return hit or _ledger_read(token)


# ---------------------------------------------------------------------------
# Did the panel actually fetch?
# ---------------------------------------------------------------------------
#
# A host can declare MCP Apps, load the panel HTML, and still never call
# ``kiln_viewer_payload`` — measured 2026-09-19, a whole day of makes with
# zero fetches, because the host was holding a tool list it cached before a
# ``restart_server`` (it does not re-list on ``tools/list_changed``).  The
# declaration said "panel", so the token rode alone and no link was
# attached: neither the panel nor the link worked, and nothing said so.
#
# The stage cannot make the host fetch.  What it can do is notice that a
# fetch it expected never came, and put the browser stage link on the next
# result as if the host drew no panel — which, in every way that matters
# to the user, it did not.


def _expect_fetch(token: str, mesh_path: str) -> None:
    """A panel will open on this result and must come back for the mesh."""
    with _lock:
        _awaiting_fetch[token] = (mesh_path, _now(), time.time())


def _fetch_arrived(token: str) -> None:
    """The panel fetched: whatever was feared, fetches are arriving."""
    global _fetches_stalled, _panel_proven
    with _lock:
        _awaiting_fetch.pop(token, None)
        _fetches_stalled = False
        _panel_proven = True


def panel_proven() -> bool:
    """Whether a rendered panel has fetched geometry since this server
    started — the one fact that separates a panel the host can draw from
    one it merely declared."""
    return _panel_proven


def forget_panel_proof() -> None:
    """The next result must prove the panel again.  Called before a
    restart, whose fresh process starts unproven anyway; saying it here
    keeps THIS process honest if the exec never happens."""
    global _panel_proven
    with _lock:
        _panel_proven = False


def _fetched_by_any_server(mesh_path: str, since_wall: float) -> bool:
    """Whether SOME Kiln server on this machine served a panel fetch for
    *mesh_path* at or after the mint.

    Read from the shared stage record, not this process's memory: a desktop
    host routes a panel's fetch over whichever session's connection it
    holds (measured 2026-09-01), so the minting process is routinely not
    the one that sees the fetch.  Without this, every multi-session desktop
    would read a working panel as broken.
    """
    try:
        from kiln.preview_evidence import evidence_for

        facts = evidence_for(mesh_path).get("stage")
        if not isinstance(facts, dict) or facts.get("via") != "panel_fetch":
            return False
        at = facts.get("at")
        # A second of slack: two clocks, one file, no ordering guarantee.
        return isinstance(at, (int, float)) and at >= since_wall - 1.0
    except Exception:  # noqa: BLE001
        return False


def panel_fetches_stalled() -> bool:
    """Whether the rendered panel's fetches are failing to arrive.

    Judges every mint that has crossed the grace since the last look: one
    fetched by this process or by any sibling server is fine; one nobody
    fetched flips the flag.  Sticky until a fetch lands, and read on the
    NEXT result — this never waits for anything.
    """
    global _fetches_stalled, _panel_proven
    now = _now()
    with _lock:
        due = {
            tok: entry
            for tok, entry in _awaiting_fetch.items()
            if now - entry[1] >= _FETCH_GRACE_S
        }
        for tok in due:
            _awaiting_fetch.pop(tok, None)
    if not due:
        return _fetches_stalled
    unfetched = [
        mesh for mesh, _minted, wall in due.values()
        if not _fetched_by_any_server(mesh, wall)
    ]
    with _lock:
        was = _fetches_stalled
        _fetches_stalled = bool(unfetched)
        if len(unfetched) < len(due):
            # A sibling served at least one of these: the panel is real.
            _panel_proven = True
    if unfetched and not was:
        logger.warning(
            "inline stage: the panel's fetch for %s never arrived within %.0fs — "
            "results carry the browser stage link until a fetch lands; the host "
            "may be holding a tool list cached before a restart_server, so "
            "reconnect the Kiln MCP server in the host or open a new chat",
            Path(unfetched[0]).name,
            _FETCH_GRACE_S,
        )
    return _fetches_stalled


def panel_fetch_fallback_note() -> str | None:
    """The sentence every door carries while fetches are not arriving, or
    ``None`` — one source, so no two results explain it differently."""
    return PANEL_FETCH_FALLBACK_NOTE if panel_fetches_stalled() else None


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


def _log_signal_once(mcp: Any, renders: bool, ctx: Any = None) -> None:
    """State, once, what this host declared and what the panel gets.

    ``renders`` is what the panel declared, not the geometry verdict: the result
    is lean by default whatever the host declared, so this line reports the
    mesh route — the View's own fetch, or the opted-in inline payload —
    rather than claiming an attach that no longer happens.
    """
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
    if not renders:
        route = "no panel (still image only)"
    elif inline_geometry_enabled():
        route = "inlined into the result (KILN_STAGE_INLINE_GEOMETRY opt-in)"
    else:
        # About the HOST's declaration, not about this call: whether the
        # panel actually fetches is a separate fact (``panel_proven``),
        # and every result says which door it took in ``shown``.
        route = "panel fetches it via kiln_viewer_payload (unproven until a fetch lands)"
    logger.info(
        "inline stage: host=%s declared=%s read_stage=%s -> geometry %s",
        who,
        sorted(_declared_extensions(mcp, ctx)) or "none",
        _host_read_the_stage,
        route,
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
        hosted_token: str | None = None
        existing = getattr(result, "structuredContent", None)
        if isinstance(existing, dict):
            art = existing.get("artifact")
            if isinstance(art, dict) and art.get("artifact_token"):
                # NOT a reason to bail out.  The hosted shape is what the
                # viewer will present to this door, and this door only knows
                # local tokens — so leaving now is precisely what produced the
                # grey stage.  Carry the token down and bind it to the mesh.
                hosted_token = str(art["artifact_token"]) or None
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
            if hosted_token:
                return _adopt(hosted_token, mesh)
            return _mint(mesh)
    except Exception as exc:  # noqa: BLE001 — a stage must never break a tool
        logger.debug("local stage token not minted: %s", exc)
    return None


def _center_on_plate(payload: dict | None) -> dict | None:
    """Kept for callers and tests that reach it here; the centring itself
    now lives beside the plate in :func:`kiln.stage_plate.stand_on_plate`,
    where every door that stamps a bed gets it."""
    from kiln.stage_plate import stand_on_plate

    return stand_on_plate(payload)


def _payload_for_mesh(mesh: str, **encode: Any) -> dict:
    """Encode *mesh*, stand it on the plate, and stamp this install's bed on.

    The one place a payload is built for the stage — all FOUR doors go
    through it: the result hook, the panel's lazy fetch, the still
    renderer in :mod:`kiln.stage_still`, which photographs this same stage,
    and the software painter in :mod:`kiln.stage_paint`, which paints it
    where no browser can photograph it.  The painter read files itself
    until 2026-09-22 and so never saw a painted part's colours.
    None of them can ship geometry with no bed under it: the stage draws the
    plate from what arrives here, and a payload that names no plate falls
    back to a reference square for a bed it knows nothing about.  Same
    reasoning puts the centring here: a door that forgot it would draw a
    correct bed with the part parked in a corner of it.

    The stills door used to build its payload directly and was therefore
    exempt from both — so a still and the live stage disagreed about the
    same mesh.  That is the failure this docstring is here to prevent, and
    it is why the count above is worth keeping accurate: a fifth caller
    that reaches for ``mesh_to_viewer_payload`` instead of this function
    silently opts out of the bed and the centring.

    Raises whatever the encoder raises; each door decides what to say about
    it, since they are not all answering a person.

    The mesh path rides along so the plate door can find the slice this
    machine made of it and lay the skirt, brim, prime tower and supports
    around the part (:mod:`kiln.slicer_geometry`) — a mesh nobody sliced
    gets exactly the payload it always did.
    """
    from kiln.stage_plate import attach_stage_plate

    return attach_stage_plate(mesh_to_viewer_payload(mesh, **encode), mesh_path=mesh)


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

    async def _document() -> str:
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
        #
        # Announced only on the TRANSITION: _register_payload_verb answers
        # "is the verb available", which is True on every later read too,
        # and notifying there would tell the host to re-list its tools once
        # per panel for a list that did not change.
        # Belt and braces: install() already registered the verb; a server
        # whose install was partial still gets it before the View's first
        # tools/call.  Idempotent, so this costs nothing when it holds.
        _register_payload_verb(mcp)
        return doc

    # SDK 2 hands the request ctx to the handler and nowhere else, and a
    # FunctionResource function takes no ctx on either major — so without
    # this the read above has no route to the session it must notify.
    # No-op on 1.x, whose dispatcher already sets an equivalent ambient.
    capture_request_context(mcp, "resources/read")

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


def _payload_verb_registered(mcp: Any) -> bool:
    """Whether ``kiln_viewer_payload`` is already on this server.

    Its own function because two callers need the same unreadable-registry
    tolerance: an exotic server object reads as "not registered", which
    makes the register call a no-op rather than an exception.
    """
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
        return isinstance(registry, dict) and "kiln_viewer_payload" in registry
    except Exception:  # noqa: BLE001
        return False


def _register_payload_verb(mcp: Any) -> bool:
    """Register ``kiln_viewer_payload`` — the View's lazy mesh fetch.

    Idempotent and never raises.  Registered at ``install``, so it is on
    the tool list a host caches at initialize.

    It used to be registered late, at the first stage-document read, to
    keep a verb nobody should call by hand off the standing surface, and
    the server sent ``notifications/tools/list_changed`` so a host that
    validates ``tools/call`` names could learn it.  Measured 2026-09-01 on
    the Claude desktop host (clientInfo ``mcp/0.1.0``): the panel rendered,
    the verb registered, the notice went out, and the host never re-listed
    — hours later its tool list still lacked the verb, so every panel's
    fetch failed into "Preview unavailable".  With the lean result this
    verb is the ONLY route to a mesh; it cannot hang on a notification a
    host may ignore.  The cost is one app-visibility tool on the list,
    hidden from the model by hosts that honour ``_meta.ui.visibility``.
    The alternative was a 3D panel that never works.

    Serves the operator's own local files at full fidelity — the hosted
    door's charge-on-keep wall guards artifact tokens, which never exist
    here; a local token resolves only to a mesh this machine already made.
    """
    try:
        if _payload_verb_registered(mcp):
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
            # The panel fetched this geometry: the strongest evidence the
            # stage showed it.  Recorded by this door, for the print gate.
            from kiln.preview_evidence import record as _record_evidence

            _record_evidence("stage", mesh, via="panel_fetch")
            # ...and proof that fetches reach this process at all, which
            # is what the link fallback waits on.  Beside the result, never
            # inside it: the lean payload contract is the panel's.
            _fetch_arrived(artifact_token)
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


def _registry_knows(mcp: Any, name: str | None) -> bool:
    """Whether *name* is a tool this server registered — the difference
    between "stamped, and its result names no mesh" and "a request shape
    whose name could not be read", which fails open above."""
    if not name:
        return False
    try:
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
        return isinstance(registry, dict) and name in registry
    except Exception:  # noqa: BLE001
        return False


#: How long a result waits for the link door before answering without it.
#: The upload runs in a thread either way; past this the result goes out
#: with ``shown`` saying the link is still uploading, and the door's own
#: content-addressed cache hands the finished link to the NEXT result for
#: the same bytes.  Bounded because a first make on a no-panel host must not
#: sit behind the link door's own 20-second ceiling on a large mesh.
_LINK_BUDGET_S = 8.0


async def _attach_link_within_budget(sc: dict, mesh: str) -> bool:
    """Attach the browser link to *sc* if the link door answers in time.

    Returns ``True`` when nothing is pending — the link rode, or the door
    refused and recorded why — and ``False`` when the upload is still
    running past the budget.  The door mutates the dict it is handed, so
    it is handed a scratch one: a thread that finishes after the budget
    must not edit a result the server has already serialised.
    """
    import asyncio

    from kiln.stage_link import attach_stage_link_async

    scratch: dict[str, Any] = {}
    try:
        await asyncio.wait_for(
            attach_stage_link_async(scratch, mesh_path=mesh or None),
            timeout=_LINK_BUDGET_S,
        )
    except asyncio.TimeoutError:
        return False
    except Exception:  # noqa: BLE001 — a link is furniture, never a failed make
        logger.debug("stage link not attached", exc_info=True)
        return True
    for key in ("viewer_url", "viewer_expires_at", "viewer_hint"):
        if key in scratch:
            sc[key] = scratch[key]
    return True


def _repeat_of_the_last(mesh: str) -> bool:
    """Whether *mesh* is exactly what the last stage result drew — and
    remember it for the next one.  Every stage result passes through here,
    whichever door it takes, so "the last one" means the last stage result
    of this server process."""
    global _last_staged
    if not mesh:
        return False
    try:
        from kiln.stage_link import stage_identity

        identity = stage_identity(Path(mesh))
    except Exception:  # noqa: BLE001 — an unreadable file is not a repeat
        identity = ""
    with _lock:
        same = bool(identity) and identity == _last_staged
        _last_staged = identity
    return same


def _shown(
    *, opens: bool, stalled: bool, proven: bool, linked: bool,
    link_pending: bool = False, mesh: str,
) -> dict[str, str]:
    """Which door this result took, and why, in a sentence.

    ``door`` is the best door the result is KNOWN to carry — ``panel``
    (the host draws it and a panel has fetched from this server), ``link``
    (a ``viewer_url`` is the stage today) or ``none`` (neither; the still
    image is the floor).  A declared panel that has never fetched is not
    known to work — the print gate says the same (``panel_proven``) — so
    until one does the link is the door, and the token rides for the panel
    to prove itself.  The still door writes its own ``shown`` (``still``):
    one vocabulary across every door, so an agent reads the same field
    whichever tool it came through, and a refusal is always a sentence,
    never a code.
    """
    from kiln.stage_link import last_refusal, refusal_sentence

    file = Path(mesh).name if mesh else ""
    if opens and not stalled and proven:
        return {
            "door": "panel", "file": file,
            "reason": (
                "the host draws Kiln's inline 3D panel and has fetched "
                "geometry from this server before"
            ),
        }
    if opens and stalled:
        base = "the inline panel is not fetching geometry on this connection"
    elif opens:
        base = (
            "the host declared Kiln's inline 3D panel but no panel has fetched "
            "from this server yet"
        )
    else:
        base = "this host draws no MCP Apps panel"
    if linked:
        reason = (
            PANEL_FETCH_FALLBACK_NOTE if opens and stalled else
            f"{base}, so the browser link is the stage: hand the user the viewer_url"
        )
        return {"door": "link", "file": file, "reason": reason}
    if link_pending:
        reason = (
            f"{base}, and the browser link is still uploading — it rides the "
            "next result for this file, or call visualize_model for it"
        )
    else:
        refusal = refusal_sentence(last_refusal(mesh) if mesh else None)
        reason = f"{base}, and no browser link could be issued: {refusal}. The still image is the floor."
    return {"door": "none", "file": file, "reason": reason}


def _install_result_hook(mcp: Any) -> bool:
    """Attach the token (and, for a panel that will open, the geometry).

    This has to happen at the LOWLEVEL handler.  The tool-manager hook that
    the telemetry counters use runs with ``convert_result=True``, so the
    value there is already a list of content blocks and a dict mutation is
    silently lost — measured, after writing it the other way first.

    The token always rides — it is a short string, and it is what the View
    presents to fetch the mesh.  The geometry rides only when an operator
    has opted in with ``KILN_STAGE_INLINE_GEOMETRY=1`` AND both stage gates
    pass: the host renders MCP Apps (else nobody draws it), and the tool is
    stamped to open the stage (else the host draws nothing for this result
    either).  Off by default, because on the hosts that render the panel
    the geometry lands in the model's context and truncates the tool's own
    output there — see the module docstring.

    The browser stage link rides too, whenever the panel is not KNOWN to
    work: while no panel has fetched from this server (``panel_proven``),
    while fetches have stopped arriving (``panel_fetches_stalled``), and
    always for a host that declared no panel — for it the link is the
    stage.  Once a fetch lands the result goes lean again.  The upload
    goes to a thread (``attach_stage_link_async``), so the server keeps
    serving while it runs, and the content-addressed cache means a mesh
    already linked costs nothing.

    Only a tool whose declaration opens the stage is touched at all: a
    token on any other result is a dead handle.  Every touched result
    says which door it took and why (``shown``), in a sentence.
    """
    async def _attach(inner: Any, ctx: Any, name: str | None) -> None:
        """Mutate one tool result in place.  Deliberately knows no SDK detail —
        ``wrap_call_tool_result`` owns every difference between majors, and
        this stays the description of WHAT to attach.  ``ctx`` is the request
        context of THIS call (None on 1.x), forwarded so the capability read
        can see the session on SDK 2; ``name`` is the called tool when the
        request shape yields one, else None (which reads as "attach")."""
        try:
            # Judged BEFORE this call mints, so the grace runs between
            # results and a result never counts against itself.
            stalled = panel_fetches_stalled()
            if not _tool_opens_stage(mcp, name):
                # No panel opens for this tool.  A token here would be a
                # dead handle — a ledger write and a promise nothing keeps.
                return
            token = token_for_call_result(inner)
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
            if not token:
                if _registry_knows(mcp, name) and sc.get("success") is not False:
                    # A stamped tool whose success names no mesh on disk:
                    # the host opens the panel on nothing.  Say so.
                    sc["shown"] = {
                        "door": "none",
                        "file": "",
                        "reason": (
                            "this result names no mesh the stage can show (the "
                            "slicer was handed a file the stage cannot draw, or "
                            "the file is gone), so the panel the host opens has "
                            "nothing to draw"
                        ),
                    }
                    inner.structuredContent = sc
                return
            artifact = dict(sc.get("artifact") or {})
            artifact["artifact_token"] = token
            sc["artifact"] = artifact
            renders = host_renders_apps(mcp, ctx)
            _log_signal_once(mcp, renders, ctx)
            # A panel opens for this result only when the host draws panels
            # (the tool's own stamp was checked above).
            opens = renders
            mesh = resolve(token) or ""
            # Opt-in FIRST: with inline geometry off — the default — there is
            # nothing to decide and no mesh to read off disk, so the ordinary
            # path never pays for an encode whose result it would discard.
            if inline_geometry_enabled() and opens:
                payload = _inline_payload(token)
                if payload is not None:
                    # Geometry rode the result to a host that draws the
                    # panel — the inline route's equivalent of a fetch.
                    from kiln.preview_evidence import record as _record_evidence

                    _record_evidence("stage", mesh, via="inline")
                    # A STEP import's analytic truth rides the payload so
                    # the stage labels the model as CAD over its display
                    # tessellation — or says the facts are unavailable,
                    # which is still the truth.
                    facts = sc.get("cad_facts")
                    if isinstance(facts, dict):
                        from kiln.mesh_payload import attach_cad_facts

                        attach_cad_facts(payload, facts)
                    sc[VIEWER_STRUCTURED_CONTENT_KEY] = payload
            elif opens and mesh:
                # Lean: the panel must come back for this mesh.  Noted, so
                # the NEXT result can tell whether it did.
                _expect_fetch(token, mesh)
            proven = _panel_proven
            repeat = _repeat_of_the_last(mesh)
            link_pending = False
            if not opens or stalled or not proven:
                # The panel is not known to work for this host — none was
                # declared, fetches stopped, or none has landed yet.  The
                # link rides: the upload runs in a thread, bounded, and the
                # note says why the link is the stage today.
                link_pending = not await _attach_link_within_budget(sc, mesh)
                if sc.get("viewer_url") and opens and stalled:
                    sc["stage_fallback"] = PANEL_FETCH_FALLBACK_NOTE
            sc["shown"] = _shown(
                opens=opens, stalled=stalled, proven=proven,
                linked=bool(sc.get("viewer_url")), link_pending=link_pending,
                mesh=mesh,
            )
            if repeat:
                # Said, never suppressed: the token still rides and the
                # panel still draws — it is the host's panel to open.
                sc["shown"]["repeat"] = True
                sc["shown"]["reason"] = f"{REPEAT_NOTE} {sc['shown']['reason']}"
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

    # Standing, not lazy — see _register_payload_verb for the measurement.
    out["payload_tool"] = _register_payload_verb(mcp)
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
    global _host_read_the_stage, _signal_logged, _fetches_stalled, _panel_proven
    global _last_staged
    _last_staged = ""
    _tokens.clear()
    _awaiting_fetch.clear()
    _fetches_stalled = False
    _panel_proven = False
    _host_read_the_stage = False
    _signal_logged = False
