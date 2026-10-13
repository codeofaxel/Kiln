"""``design_session`` is the front door for making an object -- said on every make door.

THE GAP THIS CLOSES
===================
Kiln has one orchestrator for making a thing -- ``design_session`` -- and
some forty tools that can also produce a mesh: the keyless OpenSCAD compile,
the product generators, the template builders, the cloud providers, the
imports and the marketplace download.  Each of those is an ENGINE the front
door calls, or an alternate birth method whose result joins the same
lifecycle (goal, version history, provenance, the check against what was
asked).  Nothing on the wire said so.  Measured 2026-10-12 on the hosted
connector: asked for "a 20 mm cube and a quote", an agent called
``compile_scad`` directly and then the quote tool; ``design_session`` was
never called, so the make had no goal, no history, no provenance and no
intent check.  The tool list showed the front door and the engine side by
side, and the connector's instructions never said which to open first.

THE FIX, AT THE ENGINE LEVEL
============================
One roster of make doors, one clause, one place.  Every surface that serves a
tool description reads from here, so the sentence cannot drift and a new
make door cannot ship silent:

* a local ``kiln serve`` stamps the clause onto the registered tool objects
  (:func:`install`, run from the server's ``main`` beside the stage stamp);
* kiln-pro's manifest builder and its REST / hosted-connector description
  chokepoint import :data:`MAKE_DOOR_TOOLS` and :func:`make_door_clause`;
* the server instructions, local and hosted, open the make story with
  :func:`front_door_line`;
* ``compile_scad`` -- the door a search for a make lands on most -- opens
  with :data:`ENGINE_LEAD` while the real front door is registered, so the
  engine says what it is in its first words rather than in a closing clause.

The roster names tools from BOTH repos, like ``kiln.local_stage.VIEWER_TOOLS``
does, so the public install can stamp the stubs it registers for kiln-pro's
tools with the same words.

THE ANCHOR
==========
A hand list rots.  :func:`make_door_candidates` derives, from a live
registry, every tool that LOOKS like a make door -- a stage tool that takes
no file in (it births geometry rather than editing a file it was handed), or
any tool with a make-shaped name (``generate_`` / ``import_`` / ``download_``
/ ``build_`` / ``compose_`` / ``iterate_``) -- and the tests in both repos
assert every candidate is either in :data:`MAKE_DOOR_TOOLS` or recorded in
:data:`NOT_A_MAKE_DOOR` with a reason.  A new make tool with no decision
fails at conception.

WHEN THE FRONT DOOR IS NOT INSTALLED
====================================
A plain install (public Kiln, no kiln-pro) registers ``design_session`` only
as a stub that runs on Kiln's servers for a signed-in person.  The clause
then says so, and says that ``compile_scad`` IS the make -> preview -> iterate
loop on that install -- the public fallback the product rule promises.
Presence is judged by the REAL tool (:func:`front_door_present`): a stub is
not the front door, it is the hosted one behind a sign-in.

A MAKE THAT NAMES NO GOAL
=========================
Words alone did not steer it.  Measured 2026-10-13 on the hosted connector,
after every make door said the above: asked for a cube and a quote, an agent
still compiled directly.  So where the real front door is installed, the one
helper every make door calls (:func:`join_goal`) opens a design session for a
make that names no goal -- or names one this install cannot find -- through
kiln-pro's opener, which records the make there as the session's first
version and leaves the goal sidecar beside the file; the result's
``saved_goal`` block says the session's id in one line, so the next make
iterates inside it.  The make is never refused, held back or changed: when
the opener is missing (an older kiln-pro) or fails, the failure is logged and
the result carries the one-line note saying what was skipped; when the
person's store does not take the session within the opener's short budget,
the result says the make was not saved to their history, and no version is
claimed.  A plain install is untouched, because there ``compile_scad`` IS
the loop.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The orchestrator every make starts with.
FRONT_DOOR = "design_session"

#: The engine that IS the loop when the front door is not installed.
FALLBACK_LOOP = "compile_scad"

#: Every door a make can start through, in either repo.  A member is a tool
#: whose success births geometry that was not there before -- from code, a
#: template, a prompt, a photo, a provider, an imported file or a
#: marketplace download.  Tools that EDIT a mesh they were handed (repair,
#: texture, split, colour) are not here: the mesh already belongs to a
#: session by the time they see it.
MAKE_DOOR_TOOLS: frozenset[str] = frozenset(
    {
        # --- public Kiln: the keyless loop and its siblings
        "compile_scad",
        "tweak_and_compile_scad",
        "compose_part_from_primitives",
        "design_to_gcode_pipeline",
        "rebuild_design",
        # --- public Kiln: templates
        "generate_from_template",
        "smart_generate_from_template",
        "optimize_template_params",
        "generate_template_variations",
        # --- public Kiln: the closed loop and the opt-in providers
        "iterate_design",
        "generate_model",
        "generate_model_from_image",
        "generate_model_with_provider",
        "generate_and_print",
        "download_generated_model",
        # --- public Kiln: other birth methods into the same lifecycle
        "import_step_file",
        "download_model",
        # --- kiln-pro: the fast parametric paths design_session routes to
        "generate_ashtray",
        "generate_bookmark",
        "generate_coaster",
        "generate_decorated_product",
        "generate_fridge_magnet",
        "generate_frisbee",
        "generate_jewelry_tray",
        "generate_keychain",
        "generate_license_plate_frame",
        "generate_nameplate",
        "generate_ornament",
        "generate_pen_cup",
        "generate_pet_bowl",
        "generate_pet_tag",
        "generate_product_base",
        "generate_rolling_tray",
        "generate_soap_dish",
        "generate_wall_plaque",
        "batch_generate_products",
        # --- kiln-pro: organic recipes and the import birth methods
        "build_organic_mesh",
        "import_external_mesh",
        "import_model_parts",
    }
)

#: Candidates (see :func:`make_door_candidates`) that are NOT make doors,
#: each with the reason.  A row here is a recorded decision the tests check
#: for staleness; it never silences a tool that later becomes a make door,
#: because the clause is stamped from :data:`MAKE_DOOR_TOOLS` alone.
NOT_A_MAKE_DOOR: dict[str, str] = {
    # Stage tools with no file in: they act on a design, a print or a
    # version that already exists.
    "apply_mid_print_decoration_plan": "continues a print that is already running",
    "auto_add_rubber_feet": "edits a product that already exists (path rides its arguments dict)",
    "cherry_pick_decoration_modification": "moves a change between versions of a preset that exists",
    "cherry_pick_feature_modification": "moves a change between versions of a feature that exists",
    "cherry_pick_modification": "moves a change between versions of a design that exists",
    "compose_assembly_parts": "arranges parts that were already made into an assembly",
    "compose_models": "merges meshes that already exist",
    "compose_multicolor_3mf": "packs meshes that already exist into one multi-colour print file",
    "generate_qr_decoration": "a decoration for a product that already exists",
    "merge_decoration_preset_branches": "merges versions of a preset that exists",
    "merge_design_branches": "merges versions of a design that exists",
    "merge_feature_branches": "merges versions of a feature that exists",
    "preview_mid_print_session": "shows a print that is already running",
    "rebase_design_branch": "rewrites history of a design that exists",
    "rebase_feature_branch": "rewrites history of a feature that exists",
    "rebase_preset_branch": "rewrites history of a preset that exists",
    "rollback_design_version": "returns a design that exists to an earlier version",
    "rollback_feature": "returns a feature that exists to an earlier version",
    "smart_decorate": "decorates a product that already exists (path rides its arguments dict)",
    # Make-shaped names that make no object.
    "build_generation_prompt": "writes a prompt; makes nothing",
    "build_material_overrides": "slicer settings for a material; makes nothing",
    "build_parametric_prompt": "writes a prompt; makes nothing",
    "download_and_upload": "hands a marketplace file straight to a printer; download_model is the door that brings one in",
    "generate_adaptive_slicing_plan": "a slicing plan for a model that exists",
    "generate_assembly_manual": "a document about a design, not a design",
    "generate_magnet_qc_plan": "a QC plan, not a part",
    "generate_print_certificate": "a certificate for a print that happened",
    "generate_recovery_gcode": "resumes a print that failed",
    "generate_technical_drawing": "a document about a design, not a design",
    "generate_texture": "textures a mesh that already exists",
    "import_external_feature": "a feature joins the feature library, not a design",
    "import_third_party_calibration": "calibration data, not a part",
    "iterate_decoration": "iterates a decoration, not an object",
}

#: The parameter a make door takes to join a saved goal: the goal's id, as
#: ``design_session`` returns it.  Every make door takes it, or says in
#: :data:`GOAL_NAMED_ELSEWHERE` where the goal is named instead -- so "an
#: imported part joins the goal too" is a property of the roster, checked
#: against the registry, not a sentence.
GOAL_PARAM = "brief_id"

#: Make doors that cannot take the goal themselves, and where it is named.
#: Two shapes: a door that starts a cloud job (the mesh arrives through
#: another door, which takes the goal), and a door that makes a BATCH of
#: variants (one goal per make; the variant kept is made through its own
#: door).  Each value is the sentence stamped on that door in place of the
#: ``brief_id`` tail.
GOAL_NAMED_ELSEWHERE: dict[str, str] = {
    "generate_model": (
        "The model arrives through `download_generated_model`; give that "
        f"call `{GOAL_PARAM}` set to the goal_id, so the result joins the goal."
    ),
    "generate_model_from_image": (
        "The model arrives through `download_generated_model`; give that "
        f"call `{GOAL_PARAM}` set to the goal_id, so the result joins the goal."
    ),
    "optimize_template_params": (
        "This makes a batch of variants, so it names no goal; make the one "
        "the person keeps through `generate_from_template` with "
        f"`{GOAL_PARAM}` set to the goal_id."
    ),
    "generate_template_variations": (
        "This makes a batch of variants, so it names no goal; make the one "
        "the person keeps through `generate_from_template` with "
        f"`{GOAL_PARAM}` set to the goal_id."
    ),
    "batch_generate_products": (
        "This makes a batch of products, so it names no goal; make the one "
        "the person keeps through its own generator with "
        f"`{GOAL_PARAM}` set to the goal_id."
    ),
}

#: A make-shaped tool name: the second anchor of :func:`make_door_candidates`.
_MAKE_SHAPED_NAME = re.compile(r"^(?:generate|import|download|build|compose|iterate)_")

#: Parameter names that hand a tool a file to work ON (never its output).
_INPUT_PATH_PARAM = re.compile(r"(?:^|_)paths?$")
_OUTPUT_PARAM_PREFIXES = ("output_", "dest_")
#: A path to SOURCE is not a file worked on: compiling a ``.scad`` file
#: births a mesh exactly as compiling the same code inline does.
_SOURCE_PATH_PARAMS = frozenset({"scad_path"})


def takes_a_file(param_names: Iterable[str]) -> bool:
    """Whether a signature names a file-shaped INPUT (``file_path``,
    ``mesh_path``, ``file_paths``) -- geometry the tool works on.  An output
    location (``output_path``, ``dest_dir``) and a source-code path
    (``scad_path``) do not count: a birth names where its result goes, and
    code is what it is born from."""
    for name in param_names:
        if name.startswith(_OUTPUT_PARAM_PREFIXES) or name in _SOURCE_PATH_PARAMS:
            continue
        if _INPUT_PATH_PARAM.search(name):
            return True
    return False


def make_door_candidates(
    params_by_tool: Mapping[str, Iterable[str]],
    stage_tools: Iterable[str] | None = None,
) -> set[str]:
    """Every registered tool that LOOKS like a make door and so owes a
    decision: a stage tool that takes no file in, or a make-shaped name.
    ``params_by_tool`` maps tool name -> its parameter names, read off the
    live registry; the front door itself is never a candidate."""
    if stage_tools is None:
        from kiln.local_stage import VIEWER_TOOLS as stage_tools
    stage = set(stage_tools)
    out: set[str] = set()
    for name, params in params_by_tool.items():
        if name == FRONT_DOOR:
            continue
        if _MAKE_SHAPED_NAME.match(name) or (name in stage and not takes_a_file(params)):
            out.add(name)
    return out


def registry_params(mcp: Any) -> dict[str, list[str]]:
    """Tool name -> parameter names, off a FastMCP registry."""
    out: dict[str, list[str]] = {}
    registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    for name, tool in registry.items():
        schema = getattr(tool, "parameters", None) or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        out[name] = sorted(props) if isinstance(props, dict) else []
    return out


# ---------------------------------------------------------------------------
# The words -- defined once, composed per surface.
# ---------------------------------------------------------------------------

#: What the front door gives a person, said the same way everywhere, and
#: true at every tier: the goal is kept free; checking a result against it
#: is the paid layer (``design_session`` says the same of itself).
_WHAT_IT_KEEPS = (
    "it keeps the goal (what was asked, the numbers settled, the picture "
    "shown, what was ruled out) and every make against it, and on Kiln Pro "
    "checks the result against that goal"
)

#: The marker every tool clause opens with; a test's one anchor.
MAKE_DOOR_MARKER = "MAKE DOORS:"

#: The head every make door's clause opens with while the real front door
#: is registered.  The tail says how THIS door joins the goal (below).
ENGINE_CLAUSE_HEAD = (
    f"{MAKE_DOOR_MARKER} start with `{FRONT_DOOR}`, Kiln's front door for making "
    f"an object — {_WHAT_IT_KEEPS}."
)

#: The tail for a door that takes the goal's id itself.  It names both
#: spellings on purpose: design_session hands back a ``goal_id``, and the
#: make doors took that id as ``brief_id`` long before this roster, so the
#: sentence carries the agent from one name to the other.
ENGINE_CLAUSE_TAIL = (
    f"Then make here with `{GOAL_PARAM}` set to the goal_id it returned, so "
    "this result joins the goal; an imported or downloaded part joins the "
    "same way."
)

#: The clause as the common case reads (``compile_scad``, every generator,
#: every import); a door in :data:`GOAL_NAMED_ELSEWHERE` carries its own tail.
ENGINE_CLAUSE = f"{ENGINE_CLAUSE_HEAD} {ENGINE_CLAUSE_TAIL}"

#: Stamped on every make door when the front door is absent or a stub: the
#: public fallback, where the keyless compile IS the loop.  A saved goal
#: lives with the front door, so none can be joined on this install.
FALLBACK_CLAUSE = (
    f"{MAKE_DOOR_MARKER} `{FRONT_DOOR}`, Kiln's front door for making an object, "
    "is not installed here (signed in to Kiln it runs on Kiln's servers). On "
    f"this install `{FALLBACK_LOOP}` IS the make → preview → iterate loop: make "
    "it, show the preview, iterate."
)

#: The sentence ``compile_scad`` -- the fallback loop -- opens with while the
#: real front door is registered.  A search for a make lands on the compile
#: more often than on the front door (it is the door an agent that knows
#: OpenSCAD reaches for), and a closing clause is the part of a long
#: description a reader skips; so the engine says what it is first.  Not
#: stamped on a plain install, where the compile IS the loop.
ENGINE_LEAD = (
    f"The OpenSCAD engine that `{FRONT_DOOR}` calls: to make something for a "
    f"person, start with `{FRONT_DOOR}`."
)

#: The one sentence the server instructions open the make story with.
FRONT_DOOR_LINE = (
    f"MAKE: a make starts with `{FRONT_DOOR}`, Kiln's front door for making an "
    f"object — {_WHAT_IT_KEEPS}. Every other make tool (the OpenSCAD compile, "
    "the product generators, the template builders, the imports and the "
    f"marketplace download) is a path it routes to: pass the goal_id it "
    f"returned as that tool's {GOAL_PARAM}, so the result joins the goal, an "
    "imported or downloaded part included."
)

#: The keyless make, in one place: what an agent does after starting a goal
#: when it writes the design itself, and what it does on an install without
#: the front door.  The skill manifest, get_started and the photo door's
#: redirect read it, so the default path is said one way everywhere.
KEYLESS_MAKE_LINE = (
    f"Write the OpenSCAD yourself and make it with {FRONT_DOOR}"
    '(verb="generate", goal_id=<the goal_id>, scad_code=<your OpenSCAD>): '
    "Kiln compiles it locally, free, with no API key, on every tier. On an "
    f"install without {FRONT_DOOR}, compile it with {FALLBACK_LOOP}, which "
    "is the make → preview → iterate loop there."
)

#: The instructions' extra sentence on an install without the front door.
FRONT_DOOR_FALLBACK_LINE = (
    f"On this install `{FRONT_DOOR}` is not installed (signed in to Kiln it runs "
    f"on Kiln's servers), so `{FALLBACK_LOOP}` IS the make → preview → "
    "iterate loop: make it, show the preview, iterate."
)


def make_door_clause(front_door_present: bool = True, name: str | None = None) -> str:
    """The clause ``name``'s description carries on this install: the shared
    head, then how this door joins the goal -- by ``brief_id``, or the
    sentence :data:`GOAL_NAMED_ELSEWHERE` records for it."""
    if not front_door_present:
        return FALLBACK_CLAUSE
    elsewhere = GOAL_NAMED_ELSEWHERE.get(name or "")
    if elsewhere:
        return f"{ENGINE_CLAUSE_HEAD} {elsewhere}"
    return ENGINE_CLAUSE


def front_door_line(front_door_present: bool = True) -> str:
    """The instructions' make story on this install."""
    if front_door_present:
        return FRONT_DOOR_LINE
    return f"{FRONT_DOOR_LINE} {FRONT_DOOR_FALLBACK_LINE}"


#: The module public Kiln registers kiln-pro's served tools under when
#: kiln-pro is not installed (``kiln.server._register_pro_tool_stubs``).
_STUB_MODULE = "kiln.pro_stubs"


def front_door_present(mcp: Any) -> bool:
    """Whether the REAL front door is registered -- kiln-pro's tool, not the
    stub public Kiln registers from the manifest.  A stub only works for a
    signed-in person through Kiln's servers, so an install that has only
    the stub gets the fallback words."""
    registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    tool = registry.get(FRONT_DOOR)
    if tool is None:
        return False
    fn = getattr(tool, "fn", None)
    module = getattr(fn, "__module__", "") or ""
    return module != _STUB_MODULE


#: A make-door clause is one line that opens with the marker; replacing it
#: keeps whatever stood before it, a space included.
_CLAUSE_PARAGRAPH = re.compile(rf"{re.escape(MAKE_DOOR_MARKER)}[^\n]*")


def with_make_door_clause(name: str, description: str, front_door_present: bool = True) -> str:
    """``description`` with this install's clause, for a make door; unchanged
    otherwise.  The fallback loop also opens with :data:`ENGINE_LEAD` while
    the real front door is here.  Idempotent, and a clause from another
    install (a stub's description arrives carrying the hosted wording) is
    replaced, so one install speaks with one voice."""
    if name not in MAKE_DOOR_TOOLS:
        return description
    clause = make_door_clause(front_door_present, name)
    text = description or ""
    if clause not in text:
        if MAKE_DOOR_MARKER in text:
            text = _CLAUSE_PARAGRAPH.sub(clause, text, count=1)
        else:
            text = f"{text}\n\n{clause}" if text else clause
    return _with_engine_lead(name, text, front_door_present)


def _with_engine_lead(name: str, text: str, front_door_present: bool) -> str:
    """``text`` opening with :data:`ENGINE_LEAD` when ``name`` is the fallback
    loop and the real front door is here; without it otherwise.  Idempotent."""
    if name != FALLBACK_LOOP:
        return text
    has_lead = text.startswith(ENGINE_LEAD)
    if front_door_present and not has_lead:
        return f"{ENGINE_LEAD}\n\n{text}"
    if not front_door_present and has_lead:
        return text[len(ENGINE_LEAD):].lstrip("\n")
    return text


def install(mcp: Any) -> dict[str, Any]:
    """Stamp the clause onto every registered make door.

    Run from ``kiln serve``'s ``main`` after every tool and plugin has
    registered -- a stamp over an empty registry stamps nothing.  Mutates
    descriptions only, like the stage stamp: no signature, return annotation
    or body is touched.  Never raises: a missing sentence is worse than a
    missing panel but still not worth a server that will not start.
    """
    out: dict[str, Any] = {"front_door_present": False, "stamped": 0}
    try:
        present = front_door_present(mcp)
        out["front_door_present"] = present
        registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
        for name, tool in registry.items():
            if name not in MAKE_DOOR_TOOLS:
                continue
            desc = getattr(tool, "description", None) or ""
            new = with_make_door_clause(name, desc, present)
            if new == desc:
                continue
            try:
                tool.description = new
                out["stamped"] += 1
            except Exception:  # noqa: BLE001 -- a frozen model is not fatal
                continue
    except Exception:  # noqa: BLE001
        logger.warning("front door: make-door stamping failed", exc_info=True)
    return out



# ---------------------------------------------------------------------------
# Joining a goal -- the one helper every make door calls.
# ---------------------------------------------------------------------------

#: The key a make door's result carries the join under -- the quote's block
#: about a file uses the same noun (``saved_goal``), so an agent reads one
#: word for one thing.
SAVED_GOAL_KEY = "saved_goal"

#: How a session opened for a make is said, after the clause naming why.
#: One line: the session's id, the version the make is kept as, and how the
#: next make (or the front door itself) iterates inside it.
_KEPT_IN_SESSION = (
    "is kept there as version {version}; pass " + GOAL_PARAM + " {brief_id} "
    "on the next make, or goal_id {brief_id} to `" + FRONT_DOOR + "`, to keep "
    "iterating inside it."
)

#: What the result's ``saved_goal`` block says, per outcome.  Describes what
#: Kiln did with the id it was handed, never where the file came from.
GOAL_NOTES = {
    "joined": "This make joins saved goal {brief_id}: it is recorded against the goal.",
    "not_found": (
        "This make could not be recorded against saved goal {brief_id} (no such "
        "goal on this install, or no file to record), so it stands on its own; "
        "it was made as asked."
    ),
    "unavailable": (
        "Saved goals live with `design_session`, which is not installed here, so "
        "this make could not join goal {brief_id}; it was made as asked."
    ),
    # A make that named no goal, where the front door IS here: the session
    # opened for it, said once, with the id to carry forward.
    "opened": "No goal was named, so this make opened design session {brief_id} and "
    + _KEPT_IN_SESSION,
    # A make that named a goal this install cannot find: the same, saying so.
    "opened_instead": (
        "Saved goal {named} was not found here, so this make opened design "
        "session {brief_id} and " + _KEPT_IN_SESSION
    ),
    # The session was built but the person's store did not take it in time:
    # said plainly, so no agent claims a version that is not there.
    "not_saved": (
        "Kiln could not save this make to your history just now; it is still "
        "yours below."
    ),
    # Said on a make that named no goal when no session could be opened for
    # it (an older kiln-pro, or the opener failed): the moment the agent
    # compiled directly is the moment to say what it skipped.  Stateless and
    # short on purpose -- one line, never a refusal.
    "none_named": (
        f"No saved goal named. Start with `{FRONT_DOOR}` to keep the goal (free) "
        "and, on Kiln Pro, have the result checked against it; then make with "
        f"{GOAL_PARAM} set to its goal_id."
    ),
    # Kiln's hosted service infers a goal from the request when a make names
    # none and no session could be opened for it, and keeps it: the reply
    # says which, instead of telling the agent no goal was kept.
    "inferred": (
        "Kiln kept a goal it inferred from this request (goal_id {brief_id}). "
        f"Refine it with `{FRONT_DOOR}(verb=\"update\", goal_id=\"{{brief_id}}\")`, "
        f"and make the next version with {GOAL_PARAM} set to that goal_id."
    ),
}


def _envelope_of(result: Any) -> dict[str, Any] | None:
    """The make's envelope: the result itself, or the one dict beside the
    preview image a make door hands back inline (``[Image, envelope]``, the
    shape every generator returns on a local install).  ``None`` when there
    is nothing to speak in."""
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        envelopes = [item for item in result if isinstance(item, dict)]
        if len(envelopes) == 1:
            return envelopes[0]
    return None


def _was_made(result: dict[str, Any]) -> bool:
    """Whether a make door's envelope reports a make (the compile answers
    ``success``, the generators ``status``)."""
    return result.get("success", True) is not False and result.get("status") != "error"


def _open_session(result: dict[str, Any], mesh_path: str | None) -> dict[str, Any] | None:
    """A design session opened for this make by kiln-pro's opener, or ``None``.

    ``None`` when the opener is not there (an older kiln-pro), found nothing
    to record (no file), or failed -- and a failure is logged, because the
    make it was meant to keep has already happened and must still be
    returned.  The door that made it, and what it was asked, are the tool
    being dispatched (:mod:`kiln.tool_context`), so a generator's session
    records the caller's choices rather than the generator's defaults."""
    try:
        from kiln_pro.design_brief.explicit_attach import open_session_for_make
    except Exception:  # noqa: BLE001 -- an older kiln-pro: the note stands in
        return None
    try:
        from kiln.tool_context import current_tool_arguments, current_tool_name

        door, arguments = current_tool_name(), current_tool_arguments()
    except Exception:  # noqa: BLE001
        door, arguments = None, {}
    try:
        opened = open_session_for_make(
            result, mesh_path=mesh_path, door=door, arguments=arguments
        )
    except Exception:  # noqa: BLE001 -- the make is kept whatever the session does
        logger.warning("front door: opening a session for a make failed", exc_info=True)
        return None
    if isinstance(opened, dict) and (opened.get("brief_id") or opened.get("saved") is False):
        return opened
    return None


def _session_block(opened: dict[str, Any], named: str = "") -> dict[str, Any]:
    """The ``saved_goal`` block for a make kept in the session ``opened``
    names: a fresh one (the make is its first version), or the one a goal
    sidecar beside the file already names -- or none, when the person's
    store did not take it in time."""
    if opened.get("saved") is False:
        return {
            "brief_id": named,
            "joined": False,
            "saved": False,
            "note": GOAL_NOTES["not_saved"],
        }
    brief_id = str(opened["brief_id"])
    if opened.get("opened") is False:
        return {
            "brief_id": brief_id,
            "joined": True,
            "note": GOAL_NOTES["joined"].format(brief_id=brief_id),
        }
    version = opened.get("version") or 1
    note = GOAL_NOTES["opened_instead" if named else "opened"].format(
        brief_id=brief_id, version=version, named=named
    )
    return {
        "brief_id": brief_id,
        "joined": True,
        "opened": True,
        "version": version,
        "note": note,
    }


def record_inferred_goal(result: Any, brief_id: str) -> Any:
    """Say in ``result`` that Kiln kept a goal it INFERRED for a make that
    named none, replacing the "no goal named" nudge the make door wrote when
    no session could be opened for it.  Called by whatever did the inferring
    (Kiln's hosted dispatcher), which stands down when the make door already
    kept the make in a session; a no-op without an id."""
    envelope = _envelope_of(result)
    if envelope is None or not brief_id:
        return result
    envelope[SAVED_GOAL_KEY] = {
        "brief_id": brief_id,
        "joined": True,
        "inferred": True,
        "note": GOAL_NOTES["inferred"].format(brief_id=brief_id),
    }
    return result


def join_goal(brief_id: str, result: Any, mesh_path: str | None = None) -> Any:
    """Record ``result`` -- a make door's success envelope -- against the saved
    goal ``brief_id``, and say in the envelope what happened.

    The join itself is kiln-pro's (the goal store and the intent record are
    the front door's own); this public helper is the one door every make
    tool calls, so a plain install answers honestly instead of silently
    dropping the id.  ``mesh_path`` names the file when the envelope keeps
    it under a key the join does not read.  A result handed back as
    ``[Image, envelope]`` is spoken to through its envelope and returned as
    it came.  Never raises.

    Where the real front door is installed, a make that names no goal, or a
    goal this install cannot find, gets a design session of its own: the
    make is kept there as the first version and the block names the session
    (``GOAL_NOTES["opened"]``).  When no session can be opened the block
    falls back to saying what was skipped.  On a plain install a make that
    names no goal leaves the envelope untouched.
    """
    envelope = _envelope_of(result)
    if envelope is None:
        return result
    _speak(brief_id, envelope, mesh_path)
    return result


def _speak(brief_id: str, result: dict[str, Any], mesh_path: str | None) -> None:
    """:func:`join_goal`'s body, on the envelope itself."""
    try:
        from kiln_pro.design_brief.explicit_attach import attach_brief_id_to_result
    except Exception:  # noqa: BLE001 -- a plain install
        attach_brief_id_to_result = None
    if not brief_id:
        # A make that named no goal.  On a plain install the fallback clause
        # already says compile_scad is the loop, and pointing at a door that
        # is not here would be noise.  Where the front door is installed, the
        # make gets a session of its own, or is told once what it skipped.
        if (
            attach_brief_id_to_result is None
            or not _was_made(result)
            or SAVED_GOAL_KEY in result
        ):
            return
        opened = _open_session(result, mesh_path)
        result[SAVED_GOAL_KEY] = (
            _session_block(opened)
            if opened
            else {"brief_id": "", "joined": False, "note": GOAL_NOTES["none_named"]}
        )
        return
    outcome = "unavailable"
    if attach_brief_id_to_result is not None:
        probe: dict[str, Any] = dict(result)
        if mesh_path:
            probe["local_path"] = str(mesh_path)
        try:
            joined = bool(attach_brief_id_to_result(brief_id, probe))
            if not joined and mesh_path:
                # The join is idempotent: a goal sidecar already beside the
                # file is a make already recorded, not a miss.
                from kiln.original_design import _intent_sidecar_exists

                joined = _intent_sidecar_exists(str(mesh_path))
            outcome = "joined" if joined else "not_found"
        except Exception:  # noqa: BLE001 -- a join must never cost the make
            logger.debug("front door: goal join failed", exc_info=True)
            outcome = "not_found"
    if outcome == "not_found" and _was_made(result):
        # A goal this install cannot find: the make still gets a session.
        opened = _open_session(result, mesh_path)
        if opened:
            result[SAVED_GOAL_KEY] = _session_block(opened, named=brief_id)
            return
    result[SAVED_GOAL_KEY] = {
        "brief_id": brief_id,
        "joined": outcome == "joined",
        "note": GOAL_NOTES[outcome].format(brief_id=brief_id),
    }
