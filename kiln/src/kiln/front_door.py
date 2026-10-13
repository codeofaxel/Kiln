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
  :func:`front_door_line`.

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
        if _MAKE_SHAPED_NAME.match(name):
            out.add(name)
        elif name in stage and not takes_a_file(params):
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

#: What the front door gives a person, said the same way everywhere.
_WHAT_IT_KEEPS = (
    "it keeps the goal, the version history and the provenance, and checks "
    "the result against what was asked"
)

#: The marker every tool clause opens with; a test's one anchor.
MAKE_DOOR_MARKER = "MAKE DOORS:"

#: Stamped on every make door while the real front door is registered.
ENGINE_CLAUSE = (
    f"{MAKE_DOOR_MARKER} this tool is an engine `{FRONT_DOOR}` calls. An agent "
    f"making something for a person starts with `{FRONT_DOOR}`, Kiln's front "
    f"door for making an object -- {_WHAT_IT_KEEPS} -- and reaches this tool "
    "through it; a part imported or downloaded joins a design_session the "
    "same way."
)

#: Stamped on every make door when the front door is absent or a stub: the
#: public fallback, where the keyless compile IS the loop.
FALLBACK_CLAUSE = (
    f"{MAKE_DOOR_MARKER} `{FRONT_DOOR}`, Kiln's front door for making an object, "
    "is not installed here (signed in to Kiln it runs on Kiln's servers). On "
    f"this install `{FALLBACK_LOOP}` IS the make -> preview -> iterate loop -- "
    "make it, show the preview, iterate -- and an imported or downloaded part "
    "joins that loop the same way."
)

#: The one sentence the server instructions open the make story with.
FRONT_DOOR_LINE = (
    f"MAKE: a make starts with `{FRONT_DOOR}`, Kiln's front door for making an "
    f"object -- {_WHAT_IT_KEEPS}. Every other make tool (the OpenSCAD compile, "
    "the product generators, the template builders, the imports and the "
    "marketplace download) is an engine it calls, and an imported part joins "
    "a design_session too."
)

#: The instructions' extra sentence on an install without the front door.
FRONT_DOOR_FALLBACK_LINE = (
    f"On this install `{FRONT_DOOR}` is not installed (signed in to Kiln it runs "
    f"on Kiln's servers), so `{FALLBACK_LOOP}` IS the make -> preview -> "
    "iterate loop: make it, show the preview, iterate."
)


def make_door_clause(front_door_present: bool = True) -> str:
    """The clause a make door's description carries on this install."""
    return ENGINE_CLAUSE if front_door_present else FALLBACK_CLAUSE


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


def with_make_door_clause(name: str, description: str, front_door_present: bool = True) -> str:
    """``description`` with this install's clause, for a make door; unchanged
    otherwise.  Idempotent, and a clause from another install (a stub's
    description arrives carrying the hosted wording) is replaced, so one
    install speaks with one voice."""
    if name not in MAKE_DOOR_TOOLS:
        return description
    clause = make_door_clause(front_door_present)
    text = description or ""
    for other in (ENGINE_CLAUSE, FALLBACK_CLAUSE):
        if other != clause and other in text:
            text = text.replace(other, clause)
    if clause in text:
        return text
    return f"{text}\n\n{clause}" if text else clause


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
